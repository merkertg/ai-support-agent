"""Проверить передачу обоих образов и immutable release-теги без GHCR и Docker."""

import hashlib
import json
import subprocess
from io import BytesIO
from unittest.mock import MagicMock, Mock
from urllib.error import HTTPError, URLError

import pytest

from scripts import release_delivery as delivery

COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
API_ID = "sha256:" + "1" * 64
UI_ID = "sha256:" + "2" * 64
OTHER_ID = "sha256:" + "3" * 64
API_ARM_ID = "sha256:" + "4" * 64
UI_ARM_ID = "sha256:" + "5" * 64
API_REPOSITORY = "merkertg/ai-support-agent"
UI_REPOSITORY = API_REPOSITORY + "-ui"
# Порядок LOCAL_IMAGES: API amd64, UI amd64, API arm64, UI arm64.
IMAGES = dict(
    zip(delivery.LOCAL_IMAGES, (API_ID, UI_ID, API_ARM_ID, UI_ARM_ID), strict=True)
)
REMOTES = (API_REPOSITORY, UI_REPOSITORY)


def correct_platform(image):
    """Вернуть платформу, которую CI собрал под этим именем."""
    return f"linux/{delivery.ARCHITECTURE[image]}"


def git(*arguments):
    """Выполнить локальную Git-команду только во временном репозитории теста."""
    return subprocess.run(
        ["git", *arguments], encoding="utf-8", capture_output=True, check=True
    ).stdout.strip()


@pytest.fixture
def release_repository(tmp_path, monkeypatch):
    """Создать main с двумя commit и отдельную ветку без сетевого remote."""
    monkeypatch.chdir(tmp_path)
    git("init", "--initial-branch=main")
    git("config", "user.name", "CI test")
    git("config", "user.email", "test@example.invalid")
    git("commit", "--allow-empty", "-m", "first")
    first = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-m", "second")
    main = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", main)
    return first, main


@pytest.mark.parametrize("annotated", [False, True])
def test_release_allows_ancestor_of_main_and_peels_annotated_tag(
    release_repository, annotated
):
    """Исторический commit main допускается для обоих типов явно созданного тега."""
    first, _ = release_repository
    args = ["tag", "v1.2.3", first]
    if annotated:
        args += ["--annotate", "--message", "release"]
    git(*args)
    assert delivery.validate_release("refs/tags/v1.2.3", first) == "v1.2.3"


def test_release_rejects_side_branch_and_moved_tag(release_repository):
    """Невошедшая в main ветка и изменённая ссылка тега блокируют gates."""
    first, main = release_repository
    git("checkout", "-b", "side", first)
    git("commit", "--allow-empty", "-m", "side")
    side = git("rev-parse", "HEAD")
    git("tag", "v1.2.3", side)
    with pytest.raises(delivery.DeliveryError, match="истории origin/main"):
        delivery.validate_release("refs/tags/v1.2.3", side)
    with pytest.raises(delivery.DeliveryError, match="Тег изменён"):
        delivery.validate_release("refs/tags/v1.2.3", main)


@pytest.mark.parametrize(
    "version",
    ["v0.0.0", "v1.2.3", "v100.23.456"],
)
def test_stable_version_format(version):
    """Принимается полный стабильный номер с явным префиксом v."""
    assert delivery.require_version(version) == version


@pytest.mark.parametrize(
    "version",
    [
        "1.2.3",
        "v1.2",
        "v01.2.3",
        "v1.02.3",
        "v1.2.03",
        "v1.2.3-rc1",
        "v1.2.3+build",
        "v1.2.3\n",
        "v1.2.3;touch marker",
        "v$(id).2.3",
        "v1.2.3/other",
        "v١.2.3",
        "v" + "1" * 129 + ".2.3",
    ],
)
def test_malformed_versions_never_reach_git_or_shell(monkeypatch, version):
    """Пробелы, injection, leading zero и неподдерживаемые prerelease отклоняются."""
    runner = Mock(side_effect=AssertionError("Нельзя запускать процесс"))
    monkeypatch.setattr(delivery, "command", runner)
    with pytest.raises(delivery.DeliveryError, match="строгий формат"):
        delivery.validate_release("refs/tags/" + version, COMMIT)
    runner.assert_not_called()


@pytest.mark.parametrize(
    "ref",
    ["refs/heads/main", "refs/pull/3/merge", "refs/heads/automation/dependencies"],
)
def test_non_release_checks_do_not_query_tag_history(monkeypatch, ref):
    """PR и workflow_dispatch проходят policy без публикации релизной версии."""
    runner = Mock()
    monkeypatch.setattr(delivery, "command", runner)
    assert delivery.validate_release(ref, COMMIT) == ""
    runner.assert_not_called()


def test_export_and_load_preserve_all_tested_image_ids(monkeypatch, tmp_path):
    """Один архив с четырьмя образами экспортируется и загружается без сборки."""
    runner = Mock(return_value="")
    monkeypatch.setattr(delivery, "command", runner)
    monkeypatch.setattr(delivery, "local_id", IMAGES.__getitem__)
    monkeypatch.setattr(delivery, "local_platform", correct_platform)
    delivery.export_images(tmp_path, COMMIT)
    metadata = json.loads((tmp_path / "tested-images.json").read_text(encoding="utf-8"))
    assert metadata == {"commit": COMMIT, "images": IMAGES}
    assert runner.call_args.args[-4:] == delivery.LOCAL_IMAGES
    assert runner.call_args.args[:2] == ("docker", "save")
    assert delivery.verify_images(tmp_path, COMMIT) == IMAGES
    assert runner.call_args.args[:2] == ("docker", "load")
    assert all("build" not in call.args for call in runner.call_args_list)


def test_wrong_architecture_blocks_export(monkeypatch, tmp_path):
    """amd64-образ под именем arm64 не попадёт в артефакт публикации."""
    monkeypatch.setattr(delivery, "command", Mock(return_value=""))
    monkeypatch.setattr(delivery, "local_id", IMAGES.__getitem__)
    monkeypatch.setattr(delivery, "local_platform", lambda image: "linux/amd64")
    with pytest.raises(delivery.DeliveryError, match="linux/arm64"):
        delivery.export_images(tmp_path, COMMIT)
    assert not (tmp_path / "tested-images.json").exists()


@pytest.mark.parametrize(
    "metadata",
    [
        {"commit": OTHER_COMMIT, "images": IMAGES},
        {"commit": COMMIT, "images": {delivery.LOCAL_IMAGES[0]: API_ID}},
        {"commit": COMMIT, "images": {**IMAGES, "evil:ci": API_ID}},
        {"commit": COMMIT, "images": {**IMAGES, delivery.LOCAL_IMAGES[3]: "short-id"}},
        {"commit": COMMIT, "images": []},
        {"commit": COMMIT, "images": IMAGES, "extra": True},
        [],
    ],
)
def test_invalid_artifact_is_rejected_before_docker_load(
    monkeypatch, tmp_path, metadata
):
    """Чужой commit, недостающий образ и повреждённые ID блокируются до загрузки."""
    (tmp_path / "tested-images.json").write_text(json.dumps(metadata), encoding="utf-8")
    runner = Mock()
    monkeypatch.setattr(delivery, "command", runner)
    with pytest.raises(delivery.DeliveryError):
        delivery.verify_images(tmp_path, COMMIT)
    runner.assert_not_called()


def test_loaded_image_id_mismatch_blocks_publication(monkeypatch, tmp_path):
    """Исправленный metadata не скрывает подмену содержимого Docker-архива."""
    (tmp_path / "tested-images.json").write_text(
        json.dumps({"commit": COMMIT, "images": IMAGES}), encoding="utf-8"
    )
    monkeypatch.setattr(delivery, "command", Mock())
    monkeypatch.setattr(delivery, "local_id", Mock(return_value=OTHER_ID))
    with pytest.raises(delivery.DeliveryError, match="не совпадает"):
        delivery.verify_images(tmp_path, COMMIT)


def test_loaded_image_with_wrong_platform_blocks_publication(monkeypatch, tmp_path):
    """Совпавший ID не спасает образ, загруженный для другой архитектуры."""
    (tmp_path / "tested-images.json").write_text(
        json.dumps({"commit": COMMIT, "images": IMAGES}), encoding="utf-8"
    )
    monkeypatch.setattr(delivery, "command", Mock())
    monkeypatch.setattr(delivery, "local_id", IMAGES.__getitem__)
    monkeypatch.setattr(delivery, "local_platform", lambda image: "linux/s390x")
    with pytest.raises(delivery.DeliveryError, match="собран не для"):
        delivery.verify_images(tmp_path, COMMIT)


def encode(data):
    """Получить bytes manifest и его digest, как registry."""
    body = json.dumps(data, sort_keys=True).encode()
    return body, "sha256:" + hashlib.sha256(body).hexdigest()


class FakeRegistry:
    """Registry в памяти: docker push, buildx imagetools create и чтение manifest."""

    host = "ghcr.io"

    def __init__(self, mode="config"):
        """mode задаёт, как CI видит ID образа: config, manifest или index."""
        self.mode = mode
        self.store = {}
        self.events = []
        self.tags = {}
        self.payloads = {image: self.payload(image) for image in delivery.LOCAL_IMAGES}
        self.tested = dict(IMAGES)
        if mode != "config":
            self.tested = {
                image: encode(data)[1] for image, data in self.payloads.items()
            }

    def payload(self, image):
        """Manifest, который получит registry после docker push этого образа."""
        arch = delivery.ARCHITECTURE[image]
        manifest = single_manifest(IMAGES[image], media_type=delivery.MANIFEST_TYPES[0])
        if self.mode != "index":
            return manifest
        body, digest = encode(manifest)
        self.put_digest(image, manifest)
        return {
            "schemaVersion": 2,
            "mediaType": delivery.INDEX_TYPES[1],
            "manifests": [
                {
                    "mediaType": delivery.MANIFEST_TYPES[0],
                    "digest": digest,
                    "size": len(body),
                    "platform": {"os": "linux", "architecture": arch},
                },
                {
                    "mediaType": delivery.MANIFEST_TYPES[1],
                    "digest": OTHER_ID,
                    "size": 10,
                    "platform": {"os": "unknown", "architecture": "unknown"},
                },
            ],
        }

    def put_digest(self, image, data):
        """Сохранить дочерний manifest index во всех репозиториях проекта."""
        body, digest = encode(data)
        for remote in REMOTES:
            self.store[remote, digest] = body

    def put(self, remote, tag, data):
        """Записать manifest под тегом и под его digest."""
        body, digest = encode(data)
        self.store[remote, tag] = body
        self.store[remote, digest] = body
        return digest

    def fetch(self, remote, reference):
        """Прочитать manifest, как настоящий Registry.fetch."""
        self.events.append(("fetch", remote, reference))
        body = self.store.get((remote, reference))
        if body is None:
            return None
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        return json.loads(body), body, {"Docker-Content-Digest": digest}

    def inspect(self, remote, tag):
        """Сверить manifest настоящим parser проекта."""
        self.events.append(("inspect", remote, tag))
        response = self.fetch(remote, tag)
        return None if response is None else delivery.manifest_identity(*response)

    def command(self, *arguments):
        """Имитировать tag, push и imagetools create; остальное — ошибка."""
        self.events.append(arguments)
        if arguments[:2] == ("docker", "tag"):
            self.tags[arguments[3]] = arguments[2]
        elif arguments[:2] == ("docker", "push"):
            remote, tag = arguments[2].removeprefix("ghcr.io/").rsplit(":", 1)
            self.put(remote, tag, self.payloads[self.tags[arguments[2]]])
        else:
            assert arguments[:5] == (
                "docker",
                "buildx",
                "imagetools",
                "create",
                "--tag",
            )
            remote, tag = arguments[5].removeprefix("ghcr.io/").rsplit(":", 1)
            manifests = []
            for source in arguments[6:]:
                source_remote, digest = source.removeprefix("ghcr.io/").split("@")
                assert source_remote == remote
                data = json.loads(self.store[remote, digest])
                if data["mediaType"] in delivery.INDEX_TYPES:
                    manifests.extend(data["manifests"])
                else:
                    arch = {IMAGES[i]: delivery.ARCHITECTURE[i] for i in IMAGES}[
                        data["config"]["digest"]
                    ]
                    manifests.append(
                        {
                            "mediaType": data["mediaType"],
                            "digest": digest,
                            "size": 100,
                            "platform": {"os": "linux", "architecture": arch},
                        }
                    )
            self.put(
                remote,
                tag,
                {
                    "schemaVersion": 2,
                    "mediaType": delivery.INDEX_TYPES[0],
                    "manifests": manifests,
                },
            )
        return ""

    def pushes(self):
        """Список ссылок, отправленных docker push или imagetools."""
        return [
            event[2] if event[1] == "push" else event[5]
            for event in self.events
            if event[:2] == ("docker", "push") or event[1:2] == ("buildx",)
        ]


@pytest.fixture
def registry(monkeypatch):
    """Подменить Docker и registry без сетевых запросов или обращения к daemon."""
    fake = FakeRegistry()
    monkeypatch.setattr(delivery, "command", fake.command)
    return fake


def publish(registry, **options):
    """Опубликовать четыре проверенных образа с выбранной политикой тегов."""
    return delivery.publish_images(
        registry.tested, API_REPOSITORY, COMMIT, registry, **options
    )


def platform_children(registry, remote, tag):
    """Прочитать архитектуры общего тега."""
    return delivery.index_platforms(registry.fetch(remote, tag)[0])


def test_main_publishes_multiarch_sha_and_latest_for_both_images(registry):
    """Текущий main: теги архитектур, общий SHA-index и latest для API и UI."""
    report = publish(registry, main_sha=COMMIT)
    assert len(report) == 2
    for item, remote in zip(report, REMOTES, strict=True):
        children = platform_children(registry, remote, f"sha-{COMMIT}")
        assert set(children) == {"amd64", "arm64"}
        for arch in delivery.PLATFORMS:
            arch_digest = registry.inspect(remote, f"sha-{COMMIT}-{arch}")[1]
            assert children[arch] == arch_digest
            assert item["platforms"][arch] == f"ghcr.io/{remote}@{arch_digest}"
        assert platform_children(registry, remote, "latest") == children
        assert item["latest"] == "опубликован и проверен"
        assert item["digest"] == (
            f"ghcr.io/{remote}@{registry.inspect(remote, f'sha-{COMMIT}')[1]}"
        )


def test_old_main_never_overwrites_latest(registry):
    """Поздно завершившийся старый commit не трогает latest ни одного пакета."""
    publish(registry, main_sha=OTHER_COMMIT)
    assert all(not reference.endswith(":latest") for reference in registry.pushes())
    assert all((remote, "latest") not in registry.store for remote in REMOTES)


def test_release_preflights_everything_before_first_write(registry):
    """Версия и теги архитектур обоих пакетов проверяются до первой записи."""
    report = publish(registry, version="v1.2.3", main_sha=COMMIT)
    first_write = next(
        index for index, event in enumerate(registry.events) if event[0] == "docker"
    )
    checked = {event[1:] for event in registry.events[:first_write]}
    for remote in REMOTES:
        assert {(remote, "v1.2.3-amd64"), (remote, "v1.2.3-arm64")} <= checked
        assert (remote, "v1.2.3") in checked
        assert set(platform_children(registry, remote, "v1.2.3")) == {"amd64", "arm64"}
    assert all("релизного тега" in item["latest"] for item in report)
    assert all(not reference.endswith(":latest") for reference in registry.pushes())


def test_conflicting_ui_arm64_version_prevents_any_push(registry):
    """Занятый другим образом UI arm64 останавливает релиз до записи API."""
    registry.put(UI_REPOSITORY, "v1.2.3-arm64", single_manifest(OTHER_ID))
    with pytest.raises(delivery.DeliveryError, match="занята другим образом"):
        publish(registry, version="v1.2.3")
    assert registry.pushes() == []


def test_existing_version_index_with_other_children_is_a_conflict(registry):
    """Общий тег версии, указывающий на другой manifest, не перезаписывается."""
    publish(registry, version="v1.2.3")
    registry.put(
        API_REPOSITORY,
        "v1.2.3",
        {
            "schemaVersion": 2,
            "mediaType": delivery.INDEX_TYPES[0],
            "manifests": [
                {
                    "mediaType": delivery.MANIFEST_TYPES[0],
                    "digest": OTHER_ID,
                    "size": 1,
                    "platform": {"os": "linux", "architecture": "amd64"},
                }
            ],
        },
    )
    registry.events.clear()
    with pytest.raises(delivery.DeliveryError, match="занята другим образом"):
        publish(registry, version="v1.2.3")
    assert registry.pushes() == []


def test_rebuilt_main_commit_replaces_its_sha_index(registry):
    """Повторная сборка того же commit main переписывает SHA-index, а не падает."""
    for remote in REMOTES:
        registry.put(remote, f"sha-{COMMIT}", single_manifest(OTHER_ID))
    report = publish(registry)
    for item, remote in zip(report, REMOTES, strict=True):
        children = platform_children(registry, remote, f"sha-{COMMIT}")
        assert set(children) == {"amd64", "arm64"}
        assert item["digest"].endswith(registry.inspect(remote, f"sha-{COMMIT}")[1])
    assert f"ghcr.io/{API_REPOSITORY}:sha-{COMMIT}" in registry.pushes()


def test_legacy_single_arch_version_is_not_replaced(registry):
    """Версия, выпущенная раньше только для amd64, считается занятой."""
    registry.put(API_REPOSITORY, "v1.2.3", single_manifest(API_ID))
    with pytest.raises(delivery.DeliveryError, match="занята другим образом"):
        publish(registry, version="v1.2.3")
    assert registry.pushes() == []


def test_identical_partial_release_recovers_only_missing_parts(registry):
    """Повтор failed publish дописывает недостающее; полный повтор ничего не пишет."""
    publish(registry, version="v1.2.3")
    del registry.store[UI_REPOSITORY, "v1.2.3"]
    del registry.store[UI_REPOSITORY, "v1.2.3-arm64"]
    registry.events.clear()
    publish(registry, version="v1.2.3")
    assert registry.pushes() == [
        f"ghcr.io/{UI_REPOSITORY}:v1.2.3-arm64",
        f"ghcr.io/{UI_REPOSITORY}:v1.2.3",
    ]
    registry.events.clear()
    publish(registry, version="v1.2.3")
    assert registry.pushes() == []


def test_registry_network_failure_before_push_fails_closed(registry):
    """Ошибка проверки не является доказательством отсутствия занятого тега."""
    registry.inspect = Mock(side_effect=delivery.DeliveryError("HTTP 403"))
    with pytest.raises(delivery.DeliveryError, match="403"):
        publish(registry, version="v1.2.3")
    assert registry.pushes() == []


def test_post_push_config_mismatch_fails(registry):
    """Успех Docker push недостаточен: удалённый config ID обязан совпасть."""
    registry.payloads[delivery.LOCAL_IMAGES[2]] = single_manifest(OTHER_ID)
    with pytest.raises(delivery.DeliveryError, match="не совпадает с tested image ID"):
        publish(registry)


def test_index_with_wrong_child_is_rejected_after_create(registry, monkeypatch):
    """imagetools create обязан сослаться ровно на опубликованные manifest."""
    original = registry.command

    def tamper(*arguments):
        """Подменить источник arm64 на amd64 при создании общего тега."""
        if arguments[1:2] == ("buildx",):
            arguments = (*arguments[:-1], arguments[-2])
        return original(*arguments)

    monkeypatch.setattr(delivery, "command", tamper)
    with pytest.raises(delivery.DeliveryError):
        publish(registry, main_sha=COMMIT)


@pytest.mark.parametrize("mode", ["config", "manifest", "index"])
def test_each_image_store_identity_publishes_exact_images(monkeypatch, mode):
    """Classic Docker и containerd: SHA, latest и повтор версии без лишних записей."""
    fake = FakeRegistry(mode)
    monkeypatch.setattr(delivery, "command", fake.command)
    report = delivery.publish_images(
        fake.tested, API_REPOSITORY, COMMIT, fake, main_sha=COMMIT
    )
    for item, remote in zip(report, REMOTES, strict=True):
        children = platform_children(fake, remote, f"sha-{COMMIT}")
        assert set(children) == {"amd64", "arm64"}
        assert platform_children(fake, remote, "latest") == children
    delivery.publish_images(fake.tested, API_REPOSITORY, COMMIT, fake, version="v1.2.3")
    fake.events.clear()
    delivery.publish_images(fake.tested, API_REPOSITORY, COMMIT, fake, version="v1.2.3")
    assert fake.pushes() == []


@pytest.mark.parametrize(
    "repository", ["evil/owner/name", "owner/name;id", "../name", "owner/name\n"]
)
def test_untrusted_repository_names_never_reach_docker(monkeypatch, repository):
    """Имя registry строится только из допустимого namespace/repository."""
    runner = Mock()
    monkeypatch.setattr(delivery, "command", runner)
    with pytest.raises(delivery.DeliveryError, match="имя GitHub"):
        delivery.publish_images(IMAGES, repository, COMMIT, Mock())
    runner.assert_not_called()


def test_index_platforms_skip_attestations_and_reject_strangers():
    """Attestation пропускается; чужая платформа или повтор архитектуры — ошибка."""
    data = image_index()
    assert delivery.index_platforms(data) == {"amd64": API_ID}
    strange = image_index()
    strange["manifests"][0]["platform"] = {"os": "windows", "architecture": "amd64"}
    with pytest.raises(delivery.DeliveryError, match="неожиданную платформу"):
        delivery.index_platforms(strange)
    double = image_index()
    double["manifests"][1] = dict(double["manifests"][0])
    with pytest.raises(delivery.DeliveryError, match="неожиданную платформу"):
        delivery.index_platforms(double)
    with pytest.raises(delivery.DeliveryError, match="обеих архитектур"):
        delivery.index_platforms(single_manifest())


def single_manifest(image_id=API_ID, *, media_type=delivery.MANIFEST_TYPES[1]):
    """Описать корректный одиночный OCI/Docker manifest с config ID."""
    return {
        "schemaVersion": 2,
        "mediaType": media_type,
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": image_id,
            "size": 123,
        },
        "layers": [],
    }


def image_index(*, media_type=delivery.INDEX_TYPES[1]):
    """Описать OCI index с образом и отдельным provenance attestation."""
    return {
        "schemaVersion": 2,
        "mediaType": media_type,
        "manifests": [
            {
                "mediaType": delivery.MANIFEST_TYPES[1],
                "digest": API_ID,
                "size": 7412,
                "platform": {"os": "linux", "architecture": "amd64"},
            },
            {
                "mediaType": delivery.MANIFEST_TYPES[1],
                "digest": UI_ID,
                "size": 566,
                "platform": {"os": "unknown", "architecture": "unknown"},
                "annotations": {"vnd.docker.reference.type": "attestation-manifest"},
            },
        ],
    }


def http_response(data, *, digest=None):
    """Создать контекстный HTTPS-ответ с сырым JSON и его digest."""
    body = json.dumps(data).encode()
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = body
    response.headers = {
        "Docker-Content-Digest": digest or "sha256:" + hashlib.sha256(body).hexdigest()
    }
    return response


def test_registry_authenticates_and_verifies_manifest_body_digest():
    """Registry сопоставляет config ID и raw manifest digest, кешируя только token."""
    registry = delivery.Registry("actor", "TOP-SECRET")
    registry.opener = Mock()
    manifest = http_response(single_manifest())
    registry.opener.open.side_effect = [
        http_response({"token": "BEARER"}),
        manifest,
        manifest,
    ]
    first = registry.inspect(API_REPOSITORY, "v1.2.3")
    assert first[0] == API_ID
    assert registry.inspect(API_REPOSITORY, "v1.2.3") == first
    assert registry.opener.open.call_count == 3
    calls = registry.opener.open.call_args_list
    assert calls[0].args[0].full_url.startswith("https://ghcr.io/token?")
    assert calls[1].args[0].get_header("Authorization") == "Bearer BEARER"
    assert "TOP-SECRET" not in calls[0].args[0].full_url


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_registry_errors_never_become_missing_manifest(status):
    """Отказ авторизации, rate limit и сбой registry останавливают публикацию."""
    registry = delivery.Registry("actor", "TOP-SECRET")
    registry.opener = Mock()
    registry.opener.open.side_effect = HTTPError(
        "https://ghcr.io/test", status, "private detail", {}, BytesIO()
    )
    with pytest.raises(delivery.DeliveryError, match=f"HTTP {status}") as error:
        registry.request("https://ghcr.io/test", "Bearer SECRET", manifest=True)
    assert "SECRET" not in str(error.value)
    assert "private detail" not in str(error.value)


def test_only_manifest_404_counts_as_absence():
    """404 manifest разрешает новый тег; 404 token endpoint остаётся ошибкой."""
    registry = delivery.Registry("actor", "TOP-SECRET")
    registry.opener = Mock()
    registry.opener.open.side_effect = HTTPError(
        "https://ghcr.io/test", 404, "missing", {}, BytesIO()
    )
    assert (
        registry.request("https://ghcr.io/test", "Bearer SECRET", manifest=True) is None
    )
    with pytest.raises(delivery.DeliveryError, match="HTTP 404"):
        registry.request("https://ghcr.io/token", "Basic SECRET")


def test_network_errors_do_not_print_secret_context():
    """Сетевые диагностические сообщения не выводят исходные credentials."""
    registry = delivery.Registry("actor", "TOP-SECRET")
    registry.opener = Mock()
    registry.opener.open.side_effect = URLError("sensitive TOP-SECRET")
    with pytest.raises(delivery.DeliveryError, match="проверить GHCR") as error:
        registry.request("https://ghcr.io/test", "Bearer SECRET", manifest=True)
    assert "SECRET" not in str(error.value)


def test_manifest_digest_mismatch_is_rejected():
    """Подменённые bytes и повреждённый index не проходят проверку tested ID."""
    registry = delivery.Registry("actor", "TOP-SECRET")
    registry.tokens[API_REPOSITORY] = "BEARER"
    registry.opener = Mock()
    registry.opener.open.return_value = http_response(
        single_manifest(), digest=OTHER_ID
    )
    with pytest.raises(delivery.DeliveryError, match="Digest manifest"):
        registry.inspect(API_REPOSITORY, "v1.2.3")
    registry.opener.open.return_value = http_response({"manifests": []})
    with pytest.raises(delivery.DeliveryError, match="одного проверенного образа"):
        registry.inspect(API_REPOSITORY, "v1.2.3")


def test_redirects_never_forward_authorization():
    """Даже HTTPS redirect не пересылает registry credentials другому серверу."""
    assert (
        delivery.NoRedirect().redirect_request(
            None, None, 302, "", {}, "https://evil.example"
        )
        is None
    )


def test_publication_summary_lists_index_and_architectures(
    monkeypatch, tmp_path, capsys
):
    """Summary содержит общий тег, digest index и digest каждой архитектуры."""
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    report = [
        {
            "image": "api:v1.2.3",
            "digest": API_ID,
            "platforms": {"amd64": "api@amd", "arm64": "api@arm"},
            "latest": "skip",
        },
        {"image": "ui:v1.2.3", "digest": UI_ID, "platforms": {}, "latest": "skip"},
    ]
    delivery.write_summary(report)
    text = summary.read_text(encoding="utf-8")
    assert "api:v1.2.3" in text and "ui:v1.2.3" in capsys.readouterr().out
    assert API_ID in text and UI_ID in text
    assert "- arm64: `api@arm`" in text


@pytest.mark.parametrize("payload", [[], None, {"token": ""}, {"token": 4}])
def test_malformed_token_response_blocks_delivery(payload):
    """Неожиданный JSON token endpoint останавливает job с понятной ошибкой."""
    registry = delivery.Registry("actor", "SECRET")
    registry.opener = Mock()
    registry.opener.open.return_value = http_response(payload)
    with pytest.raises(delivery.DeliveryError, match="не выдал токен"):
        registry.inspect(API_REPOSITORY, "v1.2.3")


@pytest.mark.parametrize("payload", [{"config": None}, {"config": "wrong"}, None, []])
def test_malformed_manifest_config_blocks_delivery(payload):
    """Некорректный config не становится признаком существующего tested image."""
    registry = delivery.Registry("actor", "SECRET")
    registry.tokens[API_REPOSITORY] = "BEARER"
    registry.opener = Mock()
    registry.opener.open.return_value = http_response(payload)
    with pytest.raises(delivery.DeliveryError, match="одного проверенного образа"):
        registry.inspect(API_REPOSITORY, "v1.2.3")


@pytest.mark.parametrize("media_type", delivery.INDEX_TYPES)
def test_registry_accepts_verified_oci_index_and_docker_manifest_list(media_type):
    """Index с attestations сверяется целиком, его дочерние digest не становятся ID."""
    registry = delivery.Registry("actor", "SECRET")
    registry.tokens[API_REPOSITORY] = "BEARER"
    registry.opener = Mock()
    data = image_index(media_type=media_type)
    response = http_response(data)
    registry.opener.open.return_value = response
    digest = "sha256:" + hashlib.sha256(json.dumps(data).encode()).hexdigest()
    assert registry.inspect(API_REPOSITORY, "v1.2.3") == (digest, digest)
    assert digest not in (API_ID, UI_ID)
    accept = registry.opener.open.call_args.args[0].get_header("Accept")
    assert all(
        value in accept for value in (*delivery.INDEX_TYPES, *delivery.MANIFEST_TYPES)
    )


@pytest.mark.parametrize(
    "update",
    [
        {"manifests": []},
        {"manifests": None},
        {"manifests": {}},
        {"schemaVersion": 1},
        {"schemaVersion": 2.0},
        {"mediaType": "application/octet-stream"},
        {"mediaType": []},
    ],
)
def test_malformed_index_is_not_treated_as_tested_image(update):
    """Валидный hash повреждённого/неподдерживаемого JSON не разрешает публикацию."""
    data = image_index() | update
    response = http_response(data)
    with pytest.raises(delivery.DeliveryError):
        delivery.manifest_identity(data, response.read(), response.headers)


@pytest.mark.parametrize(
    "descriptor",
    [
        None,
        [],
        {},
        {"mediaType": delivery.MANIFEST_TYPES[1], "digest": API_ID},
        {"mediaType": delivery.MANIFEST_TYPES[1], "digest": "sha256:short", "size": 1},
        {"mediaType": delivery.MANIFEST_TYPES[1], "digest": None, "size": 1},
        {"mediaType": "application/unknown", "digest": API_ID, "size": 1},
        {"mediaType": [], "digest": API_ID, "size": 1},
        {"mediaType": delivery.MANIFEST_TYPES[1], "digest": API_ID, "size": True},
        {"mediaType": delivery.MANIFEST_TYPES[1], "digest": API_ID, "size": 0},
        {"mediaType": delivery.MANIFEST_TYPES[1], "digest": API_ID, "size": -2},
    ],
)
def test_index_with_invalid_descriptor_fails_closed(descriptor):
    """Пустой/неверный descriptor нельзя использовать для оправдания чужого ID."""
    data = image_index() | {"manifests": [descriptor]}
    response = http_response(data)
    with pytest.raises(delivery.DeliveryError, match="descriptor"):
        delivery.manifest_identity(data, response.read(), response.headers)


@pytest.mark.parametrize("media_type", delivery.MANIFEST_TYPES)
def test_classic_image_config_identity_remains_supported(media_type):
    """Классический одиночный Docker/OCI manifest по-прежнему даёт config ID."""
    data = single_manifest(media_type=media_type)
    response = http_response(data)
    found = delivery.manifest_identity(data, response.read(), response.headers)
    assert found[0] == API_ID and found[1] != API_ID


def test_index_digest_mismatch_is_rejected_before_identity():
    """Даже корректная структура index требует совпадения hash всего тела."""
    data = image_index()
    response = http_response(data, digest=OTHER_ID)
    with pytest.raises(delivery.DeliveryError, match="Digest manifest"):
        delivery.manifest_identity(data, response.read(), response.headers)
