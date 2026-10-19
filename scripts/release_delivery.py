"""Передать проверенные API/UI образы amd64 и arm64 в GHCR без повторной сборки.

Каждая архитектура публикуется отдельным тегом `<тег>-amd64` и `<тег>-arm64`,
а общий тег (`sha-…`, `latest`, `vX.Y.Z`) — это image index обеих архитектур:
`docker pull` выбирает нужную сам. Версионные ссылки проверяются до первой
записи в registry: чужую версию не перезаписываем, повтор идентичного образа
разрешаем. После публикации cosign keyless подписывает digest index из отчёта
и прикладывает к manifest каждой архитектуры SBOM того же проверенного образа.
Секреты читаются только из окружения; команды Docker и cosign получают
отдельные аргументы без shell.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

PLATFORMS = ("amd64", "arm64")
# Проверенные в CI образы: компонент → архитектура → локальное имя.
COMPONENTS = {
    "api": {"amd64": "ai-support-agent:ci", "arm64": "ai-support-agent:ci-arm64"},
    "ui": {"amd64": "ai-support-agent-ui:ci", "arm64": "ai-support-agent-ui:ci-arm64"},
}
LOCAL_IMAGES = tuple(
    COMPONENTS[component][arch] for arch in PLATFORMS for component in COMPONENTS
)
ARCHITECTURE = {
    COMPONENTS[component][arch]: arch for component in COMPONENTS for arch in PLATFORMS
}
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
REPOSITORY = re.compile(r"[a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_.-]*")
# Имя из GITHUB_REPOSITORY сохраняет регистр: так оно записано в сертификате.
GITHUB_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*")
# Keyless-подпись: сертификат Fulcio выдаётся по OIDC-токену этого workflow.
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
WORKFLOW_PATH = ".github/workflows/ci.yml"
SBOM_IMAGE_ID = "aquasecurity:trivy:ImageID"
MANIFEST_TYPES = (
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
)
INDEX_TYPES = (
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
)
MANIFEST_ACCEPT = ", ".join((*MANIFEST_TYPES, *INDEX_TYPES))


class DeliveryError(RuntimeError):
    """Остановить доставку при конфликте или непроверенном состоянии registry."""


class NoRedirect(HTTPRedirectHandler):
    """Не пересылать Authorization на другой адрес по HTTP redirect."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        """Отклонить перенаправление вместо повторной отправки секрета."""
        return None


def command(*arguments: str) -> str:
    """Выполнить команду без shell и вернуть stdout, сохранив stderr в job log."""
    result = subprocess.run(
        arguments, encoding="utf-8", stdout=subprocess.PIPE, check=True
    )
    return result.stdout.strip()


def require_commit(value: str) -> str:
    """Допустить только полный SHA GitHub commit, пригодный для метаданных."""
    if not COMMIT.fullmatch(value):
        raise DeliveryError("Нужен полный SHA коммита из 40 строчных hex-символов")
    return value


def require_version(value: str) -> str:
    """Разрешить стабильный vMAJOR.MINOR.PATCH без leading zero и суффиксов."""
    if not VERSION.fullmatch(value) or len(value) > 128:
        raise DeliveryError(
            "Версия должна иметь строгий формат vX.Y.Z без ведущих нулей"
        )
    return value


def validate_release(ref: str, commit: str) -> str:
    """Проверить тег и достижимость его commit из origin/main, не меняя Git."""
    require_commit(commit)
    if not ref.startswith("refs/tags/"):
        return ""
    version = require_version(ref.removeprefix("refs/tags/"))
    resolved = command("git", "rev-parse", "--verify", f"{ref}^{{commit}}")
    if resolved != commit:
        raise DeliveryError("Тег изменён или указывает не на проверяемый commit")
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "refs/remotes/origin/main"],
        check=False,
    )
    if result.returncode:
        raise DeliveryError("Релиз разрешён только для commit в истории origin/main")
    return version


def local_id(image: str) -> str:
    """Прочитать полный ID образа из Docker и отклонить неожиданный результат."""
    value = command("docker", "image", "inspect", "--format", "{{.Id}}", image)
    if not IMAGE_ID.fullmatch(value):
        raise DeliveryError("Docker вернул неверный ID проверенного образа")
    return value


def local_platform(image: str) -> str:
    """Прочитать ОС и архитектуру локального образа, например linux/arm64."""
    return command(
        "docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image
    )


def require_platforms() -> None:
    """Убедиться, что под каждым именем лежит образ своей архитектуры."""
    for image in LOCAL_IMAGES:
        if local_platform(image) != f"linux/{ARCHITECTURE[image]}":
            raise DeliveryError(
                f"Образ {image} собран не для linux/{ARCHITECTURE[image]}"
            )


def export_images(directory: Path, commit: str) -> None:
    """Сохранить четыре протестированных образа и их ID одним набором артефактов."""
    require_commit(commit)
    require_platforms()
    directory.mkdir(parents=True, exist_ok=True)
    metadata = {
        "commit": commit,
        "images": {image: local_id(image) for image in LOCAL_IMAGES},
    }
    command(
        "docker",
        "save",
        "--output",
        str(directory / "tested-images.tar"),
        *LOCAL_IMAGES,
    )
    (directory / "tested-images.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def verify_images(directory: Path, commit: str) -> dict[str, str]:
    """Загрузить архив и сверить commit, четыре image ID и их платформы."""
    require_commit(commit)
    metadata = json.loads(
        (directory / "tested-images.json").read_text(encoding="utf-8")
    )
    if not isinstance(metadata, dict) or set(metadata) != {"commit", "images"}:
        raise DeliveryError("Неверные метаданные артефакта образов")
    images = metadata["images"]
    if (
        metadata["commit"] != commit
        or not isinstance(images, dict)
        or set(images) != set(LOCAL_IMAGES)
        or any(
            not isinstance(item, str) or not IMAGE_ID.fullmatch(item)
            for item in images.values()
        )
    ):
        raise DeliveryError(
            "Артефакт содержит другой commit или неверный набор образов"
        )
    command("docker", "load", "--input", str(directory / "tested-images.tar"))
    if any(local_id(image) != images[image] for image in LOCAL_IMAGES):
        raise DeliveryError("Загруженный образ не совпадает с проверенным image ID")
    require_platforms()
    return images


def manifest_identity(data: object, body: bytes, headers) -> tuple[str, str]:
    """Проверить manifest/index и вернуть допустимый image ID и точный digest.

    Classic Docker использует config ID. Containerd может хранить ID manifest
    или OCI index вместе с attestations: такой ID обязан совпасть с digest
    всего ответа, а не с произвольным дочерним manifest из списка.
    """
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    if headers.get("Docker-Content-Digest") != digest:
        raise DeliveryError("Digest manifest не совпал с ответом GHCR")
    if (
        not isinstance(data, dict)
        or type(data.get("schemaVersion")) is not int
        or data["schemaVersion"] != 2
    ):
        raise DeliveryError(
            "Ожидался manifest одного проверенного образа или image index"
        )
    media_type = data.get("mediaType")
    if media_type in INDEX_TYPES:
        manifests = data.get("manifests")
        if not isinstance(manifests, list) or not manifests:
            raise DeliveryError("Ожидался непустой список manifests image index")
        for item in manifests:
            if (
                not isinstance(item, dict)
                or item.get("mediaType") not in (*MANIFEST_TYPES, *INDEX_TYPES)
                or not isinstance(item.get("digest"), str)
                or not IMAGE_ID.fullmatch(item["digest"])
                or type(item.get("size")) is not int
                or item["size"] <= 0
            ):
                raise DeliveryError("Неверный descriptor manifest в image index")
        return digest, digest
    config_data = data.get("config")
    config = config_data.get("digest") if isinstance(config_data, dict) else None
    if (
        media_type not in MANIFEST_TYPES
        or not isinstance(config, str)
        or not IMAGE_ID.fullmatch(config)
        or not isinstance(data.get("layers"), list)
    ):
        raise DeliveryError(
            "Ожидался manifest одного проверенного образа или image index"
        )
    return config, digest


def index_platforms(data: object) -> dict[str, str]:
    """Вернуть digest manifest для linux/amd64 и linux/arm64 из image index.

    Attestation-manifest (платформа unknown) пропускаются. Другие платформы
    или повтор архитектуры означают не тот index, который мы публикуем.
    """
    if not isinstance(data, dict) or data.get("mediaType") not in INDEX_TYPES:
        raise DeliveryError("Ожидался image index обеих архитектур")
    found: dict[str, str] = {}
    for item in data.get("manifests") or []:
        platform = item.get("platform") if isinstance(item, dict) else None
        if not isinstance(platform, dict) or platform.get("os") == "unknown":
            continue
        arch = platform.get("architecture")
        if platform.get("os") != "linux" or arch not in PLATFORMS or arch in found:
            raise DeliveryError("Image index содержит неожиданную платформу")
        found[arch] = item["digest"]
    return found


class Registry:
    """Проверять manifest GHCR с явной авторизацией и без записи токена в лог."""

    host = "ghcr.io"

    def __init__(self, actor: str, token: str, *, scope: str = "pull,push") -> None:
        """Создать HTTPS-клиент; отсутствие credentials блокирует публикацию.

        Очистке пакетов достаточно scope `pull`: она читает manifest, а версии
        удаляет через REST API GitHub.
        """
        if not actor or not token:
            raise DeliveryError("Для GHCR нужны GITHUB_ACTOR и GITHUB_TOKEN")
        self.authorization = (
            "Basic " + base64.b64encode(f"{actor}:{token}".encode()).decode()
        )
        self.scope = scope
        self.tokens: dict[str, str] = {}
        self.opener = build_opener(NoRedirect())

    def request(self, url: str, authorization: str, *, manifest: bool = False):
        """Прочитать JSON; только 404 manifest трактуется как свободная ссылка."""
        request = Request(
            url,
            headers={
                "Authorization": authorization,
                "Accept": MANIFEST_ACCEPT if manifest else "application/json",
            },
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                body = response.read(1_048_577)
                if len(body) > 1_048_576:
                    raise DeliveryError("GHCR вернул слишком большой ответ")
                return json.loads(body), body, response.headers
        except HTTPError as error:
            if manifest and error.code == 404:
                return None
            raise DeliveryError(f"GHCR отклонил проверку: HTTP {error.code}") from None
        except (URLError, TimeoutError, ValueError, OSError):
            raise DeliveryError(
                "Не удалось достоверно проверить GHCR; публикация остановлена"
            ) from None

    def inspect(self, repository: str, tag: str) -> tuple[str, str] | None:
        """Вернуть config/index ID и manifest digest либо отсутствие тега по 404."""
        response = self.fetch(repository, tag)
        if response is None:
            return None
        return manifest_identity(*response)

    def fetch(self, repository: str, reference: str):
        """Прочитать manifest или index по тегу либо digest; None — только 404."""
        if repository not in self.tokens:
            query = urlencode(
                {"service": "ghcr.io", "scope": f"repository:{repository}:{self.scope}"}
            )
            response = self.request(
                f"https://ghcr.io/token?{query}", self.authorization
            )
            data = response[0]
            token = data.get("token") if isinstance(data, dict) else None
            if not isinstance(token, str) or not token:
                raise DeliveryError("GHCR не выдал токен доступа к пакету")
            self.tokens[repository] = token
        return self.request(
            f"https://ghcr.io/v2/{repository}/manifests/{reference}",
            f"Bearer {self.tokens[repository]}",
            manifest=True,
        )


def platform_children(registry, remote: str, digests: dict[str, str]) -> dict:
    """Определить, какие manifest каждой архитектуры попадут в общий index.

    Classic Docker публикует одиночный manifest. Хранилище containerd может
    опубликовать index с attestation: тогда берётся его manifest нужной ОС.
    """
    children = {}
    for arch, digest in digests.items():
        response = registry.fetch(remote, digest)
        if response is None:
            raise DeliveryError(f"Опубликованный {remote}@{digest} не найден")
        data = response[0]
        if isinstance(data, dict) and data.get("mediaType") in INDEX_TYPES:
            nested = index_platforms(data)
            if set(nested) != {arch}:
                raise DeliveryError(f"{remote}@{digest} содержит не только {arch}")
            children[arch] = nested[arch]
        else:
            children[arch] = digest
    return children


def verified_index(
    registry, remote: str, tag: str, children: dict, *, replace: bool = False
) -> str | None:
    """Вернуть digest общего тега, если это index ровно с этими manifest.

    С replace=True другой index или прежний одиночный manifest означают
    «нужно записать заново»: так ведёт себя SHA-тег main при повторной
    сборке того же commit. Ошибки сети и целостности ответа остаются ошибками.
    """
    response = registry.fetch(remote, tag)
    if response is None:
        return None
    data, body, headers = response
    digest = manifest_identity(data, body, headers)[1]
    try:
        matches = index_platforms(data) == children
    except DeliveryError:
        if not replace:
            raise
        matches = False
    if matches:
        return digest
    if replace:
        return None
    raise DeliveryError(
        f"ghcr.io/{remote}:{tag} не совпадает с проверенными amd64 и arm64"
    )


def publish_images(
    images: dict[str, str],
    repository: str,
    commit: str,
    registry: Registry,
    *,
    version: str = "",
    main_sha: str = "",
) -> list[dict]:
    """Проверить ссылки и опубликовать проверенные API/UI для amd64 и arm64."""
    require_commit(commit)
    repository = repository.lower()
    if not REPOSITORY.fullmatch(repository) or len(repository) > 250:
        raise DeliveryError("Неверное имя GitHub-репозитория для GHCR")
    if version:
        require_version(version)
    elif main_sha:
        require_commit(main_sha)
    host = registry.host
    targets = {"api": repository, "ui": repository + "-ui"}
    tag = version or f"sha-{commit}"
    # Версия проверяется целиком до первой записи: конфликт UI не должен
    # менять API, а занятый тег архитектуры — общий index.
    existing: dict[str, dict] = {}
    if version:
        for component, remote in targets.items():
            found = {}
            for arch in PLATFORMS:
                image = COMPONENTS[component][arch]
                found[arch] = registry.inspect(remote, f"{tag}-{arch}")
                if found[arch] and images[image] not in found[arch]:
                    raise DeliveryError(
                        f"Версия ghcr.io/{remote}:{tag}-{arch} "
                        "уже занята другим образом"
                    )
            if registry.fetch(remote, tag) is not None:
                if not all(found.values()):
                    raise DeliveryError(
                        f"Версия ghcr.io/{remote}:{tag} уже занята другим образом"
                    )
                children = platform_children(
                    registry, remote, {arch: found[arch][1] for arch in PLATFORMS}
                )
                try:
                    verified_index(registry, remote, tag, children)
                except DeliveryError:
                    raise DeliveryError(
                        f"Версия ghcr.io/{remote}:{tag} уже занята другим образом"
                    ) from None
            existing[remote] = found
    report = []
    for component, remote in targets.items():
        digests = {}
        for arch in PLATFORMS:
            image = COMPONENTS[component][arch]
            arch_tag = f"{tag}-{arch}"
            found = existing.get(remote, {}).get(arch)
            if not found:
                reference = f"{host}/{remote}:{arch_tag}"
                command("docker", "tag", image, reference)
                command("docker", "push", reference)
                found = registry.inspect(remote, arch_tag)
            if not found or images[image] not in found:
                raise DeliveryError(
                    f"Опубликованный {host}/{remote}:{arch_tag} "
                    "не совпадает с tested image ID"
                )
            digests[arch] = found[1]
        children = platform_children(registry, remote, digests)
        sources = [f"{host}/{remote}@{digests[arch]}" for arch in PLATFORMS]
        # Версия защищена preflight; SHA-тег main после повторной сборки
        # того же commit указывает на прежние образы и переписывается.
        index = verified_index(registry, remote, tag, children, replace=not version)
        if index is None:
            command(
                "docker",
                "buildx",
                "imagetools",
                "create",
                "--tag",
                f"{host}/{remote}:{tag}",
                *sources,
            )
            index = verified_index(registry, remote, tag, children)
        if index is None:
            raise DeliveryError(f"Общий тег {host}/{remote}:{tag} не появился")
        latest = (
            "пропущен для релизного тега" if version else "пропущен: main уже изменён"
        )
        item = {
            "component": component,
            "image": f"{host}/{remote}:{tag}",
            "digest": f"{host}/{remote}@{index}",
            "platforms": {
                arch: f"{host}/{remote}@{digests[arch]}" for arch in PLATFORMS
            },
            # Manifest, на которые ссылается index: к ним прикладывается SBOM.
            "manifests": {
                arch: f"{host}/{remote}@{children[arch]}" for arch in PLATFORMS
            },
            "tested": {arch: images[COMPONENTS[component][arch]] for arch in PLATFORMS},
        }
        if not version and main_sha == commit:
            command(
                "docker",
                "buildx",
                "imagetools",
                "create",
                "--tag",
                f"{host}/{remote}:latest",
                *sources,
            )
            latest_index = verified_index(registry, remote, "latest", children)
            if latest_index is None:
                raise DeliveryError(f"latest для {remote} не появился после записи")
            latest = "опубликован и проверен"
            # Обычно совпадает с digest SHA-index; иначе подписываются оба.
            item["latest_digest"] = f"{host}/{remote}@{latest_index}"
        item["latest"] = latest
        report.append(item)
    return report


def digest_reference(value: object, host: str, remote: str) -> str:
    """Принять только адрес `host/remote@sha256:…` нужного пакета, без тега."""
    prefix = f"{host}/{remote}@"
    if (
        not isinstance(value, str)
        or not value.startswith(prefix)
        or not IMAGE_ID.fullmatch(value.removeprefix(prefix))
    ):
        raise DeliveryError(f"Отчёт публикации содержит неверный digest {remote}")
    return value


def signing_targets(report: object, repository: str, *, host: str = "ghcr.io"):
    """Проверить отчёт publish и вернуть, что подписывать для API и UI.

    Подписываются digest index (и `latest`, если его digest другой), SBOM
    прикладывается к manifest архитектуры. Теги в подпись не попадают: тег
    можно переписать, digest — нет.
    """
    repository = repository.lower()
    if not REPOSITORY.fullmatch(repository) or len(repository) > 250:
        raise DeliveryError("Неверное имя GitHub-репозитория для GHCR")
    targets = {"api": repository, "ui": repository + "-ui"}
    if (
        not isinstance(report, list)
        or len(report) != len(targets)
        or {item.get("component") for item in report if isinstance(item, dict)}
        != set(targets)
    ):
        raise DeliveryError("Отчёт публикации должен описывать ровно API и UI")
    result = []
    for item in sorted(report, key=lambda entry: entry["component"]):
        component = item["component"]
        remote = targets[component]
        indexes = [digest_reference(item.get("digest"), host, remote)]
        if "latest_digest" in item:
            latest = digest_reference(item["latest_digest"], host, remote)
            if latest not in indexes:
                indexes.append(latest)
        manifests, tested = item.get("manifests"), item.get("tested")
        if (
            not isinstance(manifests, dict)
            or not isinstance(tested, dict)
            or set(manifests) != set(PLATFORMS)
            or set(tested) != set(PLATFORMS)
            or any(
                not isinstance(value, str) or not IMAGE_ID.fullmatch(value)
                for value in tested.values()
            )
        ):
            raise DeliveryError(f"Отчёт публикации {remote} без manifest архитектур")
        result.append(
            {
                "component": component,
                "indexes": indexes,
                "manifests": {
                    arch: digest_reference(manifests[arch], host, remote)
                    for arch in PLATFORMS
                },
                "tested": dict(tested),
            }
        )
    return result


def sbom_path(directory: Path, image: str) -> Path:
    """Файл CycloneDX, который Trivy пишет в CI для локального образа."""
    return directory / f"{image.replace(':', '-')}.cdx.json"


def verify_sboms(directory: Path, images: dict[str, str]) -> dict[str, Path]:
    """Найти SBOM каждого из четырёх проверенных образов и сверить его image ID.

    Trivy записывает в `metadata.component` свойство ImageID — тот же ID, что
    `docker image inspect` и `tested-images.json`. SBOM другой сборки не
    прикладывается к опубликованному образу.
    """
    found = {}
    for image in LOCAL_IMAGES:
        path = sbom_path(directory, image)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise DeliveryError(
                f"Нет корректного SBOM {path.name} для {image}"
            ) from None
        if not isinstance(data, dict):
            data = {}
        metadata = data.get("metadata")
        component = metadata.get("component") if isinstance(metadata, dict) else None
        properties = (
            component.get("properties") if isinstance(component, dict) else None
        )
        identifiers = {
            entry.get("value")
            for entry in (properties if isinstance(properties, list) else [])
            if isinstance(entry, dict) and entry.get("name") == SBOM_IMAGE_ID
        }
        if data.get("bomFormat") != "CycloneDX" or identifiers != {images[image]}:
            raise DeliveryError(f"SBOM {path.name} не относится к проверенному {image}")
        found[image] = path
    return found


def signing_identity(server: str, workflow_ref: str, repository: str, ref: str) -> str:
    """Собрать identity сертификата: workflow ci.yml этого репозитория и ref.

    Fulcio записывает её в сертификат keyless-подписи. Подпись другого
    workflow, ветки или fork не пройдёт `cosign verify` с этой identity.
    """
    expected = f"{repository}/{WORKFLOW_PATH}@{ref}"
    is_release = ref.startswith("refs/tags/") and VERSION.fullmatch(
        ref.removeprefix("refs/tags/")
    )
    if (
        server != "https://github.com"
        or not GITHUB_REPOSITORY.fullmatch(repository)
        or not (ref == "refs/heads/main" or is_release)
        or workflow_ref != expected
    ):
        raise DeliveryError("Подписывать может только ci.yml из main или vX.Y.Z")
    return f"{server}/{expected}"


def sign_images(
    report: object,
    repository: str,
    commit: str,
    identity: str,
    sbom_directory: Path,
    *,
    host: str = "ghcr.io",
) -> list[str]:
    """Подписать index обоих пакетов, приложить SBOM и проверить результат.

    `cosign sign` и `cosign attest` получают только адреса `@sha256:…` из
    отчёта publish. Затем `cosign verify` и `verify-attestation` требуют
    identity этого workflow, OIDC-издателя GitHub и SHA проверяемого commit.
    """
    require_commit(commit)
    targets = signing_targets(report, repository, host=host)
    images = {
        COMPONENTS[item["component"]][arch]: item["tested"][arch]
        for item in targets
        for arch in PLATFORMS
    }
    sboms = verify_sboms(sbom_directory, images)
    for item in targets:
        for reference in item["indexes"]:
            command("cosign", "sign", "--yes", reference)
        for arch in PLATFORMS:
            sbom = sboms[COMPONENTS[item["component"]][arch]]
            command(
                "cosign",
                "attest",
                "--yes",
                "--type",
                "cyclonedx",
                "--predicate",
                str(sbom),
                item["manifests"][arch],
            )
    policy = (
        "--certificate-identity",
        identity,
        "--certificate-oidc-issuer",
        OIDC_ISSUER,
        "--certificate-github-workflow-sha",
        commit,
    )
    lines = ["## Подписи и SBOM", "", f"- Identity: `{identity}`"]
    for item in targets:
        for reference in item["indexes"]:
            command("cosign", "verify", *policy, reference)
            lines.append(f"- Подпись index проверена: `{reference}`")
        for arch in PLATFORMS:
            reference = item["manifests"][arch]
            command(
                "cosign",
                "verify-attestation",
                "--type",
                "cyclonedx",
                *policy,
                reference,
            )
            lines.append(f"- SBOM CycloneDX {arch} проверен: `{reference}`")
    return lines


def write_summary(report: list[dict]) -> None:
    """Показать общие ссылки, digest index и обеих архитектур в сводке Actions."""
    lines = ["## Проверенные API и UI образы (amd64 и arm64)", ""]
    for item in report:
        lines.extend(
            [
                f"- Образ: `{item['image']}`",
                f"- Digest index: `{item['digest']}`",
                *[
                    f"- {arch}: `{reference}`"
                    for arch, reference in item.get("platforms", {}).items()
                ],
                f"- latest: {item['latest']}",
                "",
            ]
        )
    append_summary(lines)


def append_summary(lines: list[str]) -> None:
    """Напечатать Markdown и дописать его в сводку задания GitHub Actions."""
    result = "\n".join(lines) + "\n"
    print(result)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(result)


def use_utf8_output() -> None:
    """Печатать UTF-8 и при перенаправлении вывода в Windows, где по умолчанию cp1252.

    Консоль Windows и так получает Unicode; меняется только поток в файл или pipe.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def main() -> int:
    """Запустить один этап workflow и вернуть безопасное сообщение при ошибке."""
    use_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("validate", "export", "publish", "sign"))
    parser.add_argument("--directory", type=Path, default=Path("image-artifact"))
    # SBOM из задания container и отчёт publish, который читает sign.
    parser.add_argument("--sbom-directory", type=Path, default=Path("sbom"))
    parser.add_argument("--report", type=Path, default=Path("published-images.json"))
    arguments = parser.parse_args()
    commit = os.environ.get("GITHUB_SHA", "")
    ref = os.environ.get("GITHUB_REF", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    try:
        if arguments.operation == "validate":
            version = validate_release(ref, commit)
            if output := os.environ.get("GITHUB_OUTPUT"):
                with Path(output).open("a", encoding="utf-8") as stream:
                    stream.write(f"version={version}\n")
            print(f"Проверено: {version or 'проверка ветки/PR без version-тега'}")
        elif arguments.operation == "export":
            export_images(arguments.directory, commit)
        elif arguments.operation == "publish":
            version = validate_release(ref, commit)
            images = verify_images(arguments.directory, commit)
            # Отсутствующий или чужой SBOM останавливает job до первой записи.
            verify_sboms(arguments.sbom_directory, images)
            registry = Registry(
                os.environ.get("GITHUB_ACTOR", ""), os.environ.get("GITHUB_TOKEN", "")
            )
            report = publish_images(
                images,
                repository,
                commit,
                registry,
                version=version,
                main_sha=os.environ.get("MAIN_SHA", ""),
            )
            arguments.report.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            write_summary(report)
        else:
            validate_release(ref, commit)
            identity = signing_identity(
                os.environ.get("GITHUB_SERVER_URL", ""),
                os.environ.get("GITHUB_WORKFLOW_REF", ""),
                repository,
                ref,
            )
            report = json.loads(arguments.report.read_text(encoding="utf-8"))
            append_summary(
                sign_images(
                    report, repository, commit, identity, arguments.sbom_directory
                )
            )
    except (DeliveryError, subprocess.CalledProcessError, OSError, ValueError) as error:
        print(f"Доставка остановлена: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
