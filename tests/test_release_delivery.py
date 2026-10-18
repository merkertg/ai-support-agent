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
API_REPOSITORY = "merkertg/ai-support-agent"
UI_REPOSITORY = API_REPOSITORY + "-ui"
IMAGES = dict(zip(delivery.LOCAL_IMAGES, (API_ID, UI_ID), strict=True))


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


def test_export_and_load_preserve_both_tested_image_ids(monkeypatch, tmp_path):
    """Один архив экспортируется и загружается без docker build и с обоими ID."""
    runner = Mock(return_value="")
    monkeypatch.setattr(delivery, "command", runner)
    monkeypatch.setattr(delivery, "local_id", IMAGES.__getitem__)
    delivery.export_images(tmp_path, COMMIT)
    metadata = json.loads((tmp_path / "tested-images.json").read_text(encoding="utf-8"))
    assert metadata == {"commit": COMMIT, "images": IMAGES}
    assert runner.call_args.args[-2:] == delivery.LOCAL_IMAGES
    assert runner.call_args.args[:2] == ("docker", "save")
    assert delivery.verify_images(tmp_path, COMMIT) == IMAGES
    assert runner.call_args.args[:2] == ("docker", "load")
    assert all("build" not in call.args for call in runner.call_args_list)


@pytest.mark.parametrize(
    "metadata",
    [
        {"commit": OTHER_COMMIT, "images": IMAGES},
        {"commit": COMMIT, "images": {delivery.LOCAL_IMAGES[0]: API_ID}},
        {"commit": COMMIT, "images": {**IMAGES, "evil:ci": API_ID}},
        {"commit": COMMIT, "images": {**IMAGES, delivery.LOCAL_IMAGES[1]: "short-id"}},
        {"commit": COMMIT, "images": []},
        {"commit": COMMIT, "images": IMAGES, "extra": True},
        [],
    ],
)
def test_invalid_artifact_is_rejected_before_docker_load(
    monkeypatch, tmp_path, metadata
):
    """Чужой commit, недостающий UI и повреждённые ID блокируются до загрузки."""
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


class FakeRegistry:
    """Хранить manifest в памяти и отмечать порядок preflight и записи Docker."""

    def __init__(self):
        """Начать с отсутствующих удалённых тегов и пустого журнала событий."""
        self.references = {}
        self.events = []
        self.tags = {}

    def inspect(self, repository, tag):
        """Прочитать удалённый тег, не изменяя registry."""
        self.events.append(("inspect", repository, tag))
        return self.references.get((repository, tag))

    def command(self, *arguments):
        """Имитировать только tag/push: любые другие команды считаются ошибкой."""
        self.events.append(arguments)
        assert arguments[:2] in (("docker", "tag"), ("docker", "push"))
        if arguments[1] == "tag":
            self.tags[arguments[3]] = IMAGES[arguments[2]]
        else:
            remote, tag = arguments[2].removeprefix("ghcr.io/").split(":")
            image_id = self.tags[arguments[2]]
            self.references[remote, tag] = (image_id, image_id)
        return ""


@pytest.fixture
def registry(monkeypatch):
    """Подменить Docker и registry без сетевых запросов или обращения к daemon."""
    fake = FakeRegistry()
    monkeypatch.setattr(delivery, "command", fake.command)
    return fake


def publish(registry, **options):
    """Проверить одинаковую пару API/UI образов с выбранной политикой тегов."""
    return delivery.publish_images(IMAGES, API_REPOSITORY, COMMIT, registry, **options)


def test_main_publishes_sha_and_latest_for_both_images(registry):
    """Текущий main публикует две пары SHA/latest и возвращает два digest."""
    report = publish(registry, main_sha=COMMIT)
    assert len(report) == 2
    assert set(registry.references) == {
        (repository, tag)
        for repository in (API_REPOSITORY, UI_REPOSITORY)
        for tag in (f"sha-{COMMIT}", "latest")
    }
    assert all(item["latest"] == "опубликован и проверен" for item in report)


def test_old_main_never_overwrites_latest(registry):
    """Поздно завершившийся старый commit публикует только SHA для API/UI."""
    publish(registry, main_sha=OTHER_COMMIT)
    assert set(registry.references) == {
        (API_REPOSITORY, f"sha-{COMMIT}"),
        (UI_REPOSITORY, f"sha-{COMMIT}"),
    }


def test_release_preflights_both_images_before_first_push(registry):
    """Обе ссылки проверяются прежде первой записи, latest/SHA релиз не меняет."""
    report = publish(registry, version="v1.2.3", main_sha=COMMIT)
    assert registry.events[:2] == [
        ("inspect", API_REPOSITORY, "v1.2.3"),
        ("inspect", UI_REPOSITORY, "v1.2.3"),
    ]
    assert set(registry.references) == {
        (API_REPOSITORY, "v1.2.3"),
        (UI_REPOSITORY, "v1.2.3"),
    }
    assert all("релизного тега" in item["latest"] for item in report)


def test_conflicting_ui_version_prevents_any_api_push(registry):
    """Занятый другим UI образом номер останавливает доставку до записи API."""
    registry.references[UI_REPOSITORY, "v1.2.3"] = (OTHER_ID, OTHER_ID)
    with pytest.raises(delivery.DeliveryError, match="занята другим образом"):
        publish(registry, version="v1.2.3")
    assert all(event[0] == "inspect" for event in registry.events)
    assert (API_REPOSITORY, "v1.2.3") not in registry.references


def test_identical_partial_release_recovers_only_missing_ui(registry):
    """Повтор failed publish использует тот же артефакт и дописывает только UI."""
    registry.references[API_REPOSITORY, "v1.2.3"] = (API_ID, API_ID)
    publish(registry, version="v1.2.3")
    assert [event for event in registry.events if event[:2] == ("docker", "push")] == [
        ("docker", "push", f"ghcr.io/{UI_REPOSITORY}:v1.2.3")
    ]
    registry.events.clear()
    publish(registry, version="v1.2.3")
    assert all(event[0] == "inspect" for event in registry.events)


def test_registry_network_failure_before_push_fails_closed(registry):
    """Ошибка проверки не является доказательством отсутствия занятого тега."""
    registry.inspect = Mock(side_effect=delivery.DeliveryError("HTTP 403"))
    with pytest.raises(delivery.DeliveryError, match="403"):
        publish(registry, version="v1.2.3")
    assert registry.events == []


def test_post_push_config_mismatch_fails(registry):
    """Успех Docker push недостаточен: удалённый config ID обязан совпасть."""
    registry.inspect = Mock(return_value=(OTHER_ID, OTHER_ID))
    with pytest.raises(delivery.DeliveryError, match="не совпадает"):
        publish(registry)


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


def test_publication_summary_lists_both_digests(monkeypatch, tmp_path, capsys):
    """Summary содержит API/UI и адреса для точного docker pull по digest."""
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    report = [
        {"image": "api:v1.2.3", "digest": API_ID, "latest": "skip"},
        {"image": "ui:v1.2.3", "digest": UI_ID, "latest": "skip"},
    ]
    delivery.write_summary(report)
    assert "api:v1.2.3" in summary.read_text(encoding="utf-8")
    assert "ui:v1.2.3" in capsys.readouterr().out
    assert API_ID in summary.read_text(encoding="utf-8") and UI_ID in summary.read_text(
        encoding="utf-8"
    )


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


@pytest.mark.parametrize("mode", ["config", "manifest", "index"])
def test_main_and_release_publish_exact_api_ui_with_each_image_store(monkeypatch, mode):
    """API/UI SHA/latest и повтор версии проверяются реальным parser для трёх ID."""
    from urllib.parse import urlsplit

    payloads = {
        API_REPOSITORY: single_manifest(API_ID),
        UI_REPOSITORY: single_manifest(UI_ID),
    }
    if mode == "index":
        payloads = {
            remote: image_index() | {"annotations": {"test.component": remote}}
            for remote in payloads
        }
    digests = {
        remote: "sha256:" + hashlib.sha256(json.dumps(data).encode()).hexdigest()
        for remote, data in payloads.items()
    }
    tested_ids = dict(
        zip(
            delivery.LOCAL_IMAGES,
            (
                (API_ID, UI_ID)
                if mode == "config"
                else (digests[API_REPOSITORY], digests[UI_REPOSITORY])
            ),
            strict=True,
        )
    )
    states = set()
    commands = []
    registry = delivery.Registry("actor", "SECRET")
    registry.tokens = {remote: "BEARER" for remote in payloads}
    registry.opener = Mock()

    def open_manifest(request, timeout):
        """Отдать registry JSON только после смоделированного push нужной ссылки."""
        remote, tag = (
            urlsplit(request.full_url).path.removeprefix("/v2/").split("/manifests/")
        )
        if (remote, tag) not in states:
            raise HTTPError(request.full_url, 404, "missing", {}, BytesIO())
        return http_response(payloads[remote])

    def docker_command(*arguments):
        """Записать remote reference после Docker push без настоящей публикации."""
        commands.append(arguments)
        assert arguments[:2] in (("docker", "tag"), ("docker", "push"))
        if arguments[1] == "push":
            states.add(tuple(arguments[2].removeprefix("ghcr.io/").rsplit(":", 1)))
        return ""

    registry.opener.open.side_effect = open_manifest
    monkeypatch.setattr(delivery, "command", docker_command)
    report = delivery.publish_images(
        tested_ids, API_REPOSITORY, COMMIT, registry, main_sha=COMMIT
    )
    assert states == {
        (remote, tag) for remote in payloads for tag in (f"sha-{COMMIT}", "latest")
    }
    assert {item["digest"] for item in report} == {
        f"ghcr.io/{remote}@{digest}" for remote, digest in digests.items()
    }
    delivery.publish_images(
        tested_ids, API_REPOSITORY, COMMIT, registry, version="v1.2.3"
    )
    before = len(commands)
    delivery.publish_images(
        tested_ids, API_REPOSITORY, COMMIT, registry, version="v1.2.3"
    )
    assert len(commands) == before
    assert states == {
        (remote, tag)
        for remote in payloads
        for tag in (f"sha-{COMMIT}", "latest", "v1.2.3")
    }


def test_child_manifest_id_cannot_approve_conflicting_index_version(monkeypatch):
    """Совпадение с одним child digest index не равно совпадению tested index ID."""
    registry = delivery.Registry("actor", "SECRET")
    registry.tokens = {API_REPOSITORY: "BEARER", UI_REPOSITORY: "BEARER"}
    registry.opener = Mock()
    registry.opener.open.side_effect = [
        http_response(single_manifest()),
        http_response(image_index()),
    ]
    runner = Mock()
    monkeypatch.setattr(delivery, "command", runner)
    with pytest.raises(delivery.DeliveryError, match="занята другим образом"):
        delivery.publish_images(
            IMAGES, API_REPOSITORY, COMMIT, registry, version="v1.2.3"
        )
    runner.assert_not_called()


def test_index_digest_mismatch_is_rejected_before_identity():
    """Даже корректная структура index требует совпадения hash всего тела."""
    data = image_index()
    response = http_response(data, digest=OTHER_ID)
    with pytest.raises(delivery.DeliveryError, match="Digest manifest"):
        delivery.manifest_identity(data, response.read(), response.headers)
