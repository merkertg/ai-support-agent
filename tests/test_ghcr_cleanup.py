"""Проверить отбор старых SHA-версий GHCR без сети, GitHub API и registry."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from unittest.mock import MagicMock, Mock
from urllib.error import HTTPError, URLError

import pytest

from scripts import ghcr_cleanup as cleanup
from scripts import release_delivery as delivery

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
INDEX = delivery.INDEX_TYPES[1]
MANIFEST = delivery.MANIFEST_TYPES[1]


def encode(data):
    """Получить bytes и digest manifest, как их хранит registry."""
    body = json.dumps(data, sort_keys=True).encode()
    return body, "sha256:" + hashlib.sha256(body).hexdigest()


class Graph:
    """Пакет GHCR в памяти: версии GitHub API и manifest registry."""

    def __init__(self):
        """Начать с пустого пакета."""
        self.store = {}
        self.versions = []

    def add(self, data, *tags, age=0):
        """Сохранить manifest и, если есть теги или возраст, версию пакета."""
        body, digest = encode(data)
        self.store[digest] = body
        updated = NOW - timedelta(days=age)
        self.versions.append(
            cleanup.Version(len(self.versions) + 1, digest, tuple(tags), updated)
        )
        return digest

    def image(self, name, *tags, age=0):
        """Одиночный manifest образа одной архитектуры."""
        config = "sha256:" + hashlib.sha256(name.encode()).hexdigest()
        data = {
            "schemaVersion": 2,
            "mediaType": MANIFEST,
            "config": {"mediaType": "config", "digest": config, "size": 1},
            "layers": [],
        }
        return self.add(data, *tags, age=age)

    def index(self, children, *tags, age=0, note=""):
        """Общий index amd64 и arm64, как после imagetools create."""
        data = {
            "annotations": {"note": note},
            "schemaVersion": 2,
            "mediaType": INDEX,
            "manifests": [
                {
                    "mediaType": MANIFEST,
                    "digest": digest,
                    "size": 1,
                    "platform": {"os": "linux", "architecture": arch},
                }
                for arch, digest in zip(("amd64", "arm64"), children, strict=True)
            ],
        }
        return self.add(data, *tags, age=age)

    def signature(self, subject, kind="sign", *, age=0):
        """Подпись cosign v3: артефакт с subject и тег sha256-<hex> на index."""
        artifact = {
            "schemaVersion": 2,
            "mediaType": MANIFEST,
            "artifactType": "application/vnd.dev.sigstore.bundle.v0.3+json",
            "layers": [],
            "subject": {"mediaType": MANIFEST, "digest": subject, "size": 1},
            "annotations": {"kind": kind},
        }
        signed = self.add(artifact, age=age)
        fallback = {
            "schemaVersion": 2,
            "mediaType": INDEX,
            "manifests": [{"mediaType": MANIFEST, "digest": signed, "size": 1}],
        }
        tag = "sha256-" + subject.removeprefix("sha256:")
        return signed, self.add(fallback, tag, age=age)

    def fetch(self, digest):
        """Прочитать manifest; None означает HTTP 404."""
        body = self.store.get(digest)
        return None if body is None else json.loads(body)

    def plan(self, days=30):
        """Составить план очистки этого пакета."""
        return cleanup.plan_cleanup(self.versions, self.fetch, now=NOW, days=days)


@pytest.fixture
def package():
    """Типичный пакет: старые и свежие main, latest, два релиза и подписи."""
    graph = Graph()
    sha = {name: f"sha-{name * 40}" for name in "abcde"}
    found = {}
    # a: старый main с подписями index, SBOM amd64 и прежним тегом .sig cosign v2.
    found["a_amd"] = graph.image("a-amd", sha["a"] + "-amd64", age=45)
    found["a_arm"] = graph.image("a-arm", sha["a"] + "-arm64", age=45)
    found["a_idx"] = graph.index((found["a_amd"], found["a_arm"]), sha["a"], age=45)
    found["a_sig"] = graph.signature(found["a_idx"], age=45)
    found["a_sbom"] = graph.signature(found["a_amd"], "attest", age=45)
    legacy = {"schemaVersion": 2, "mediaType": MANIFEST, "layers": [{"x": 1}]}
    found["a_legacy"] = graph.add(
        legacy, "sha256-" + found["a_idx"].removeprefix("sha256:") + ".sig", age=45
    )
    # b: тоже старый, но на него указывает latest.
    found["b_amd"] = graph.image("b-amd", sha["b"] + "-amd64", age=40)
    found["b_arm"] = graph.image("b-arm", sha["b"] + "-arm64", age=40)
    found["b_idx"] = graph.index(
        (found["b_amd"], found["b_arm"]), sha["b"], "latest", age=40
    )
    found["b_sig"] = graph.signature(found["b_idx"], age=40)
    found["b_sbom"] = graph.signature(found["b_arm"], "attest", age=40)
    # c: свежий main; его arm64 совпал со старым образом из a.
    found["c_amd"] = graph.image("c-amd", sha["c"] + "-amd64", age=2)
    found["c_idx"] = graph.index((found["c_amd"], found["a_arm"]), sha["c"], age=2)
    # d: старый main, чьи образы позже вошли в релиз v1.1.0 без своих тегов.
    found["d_amd"] = graph.image("d-amd", sha["d"] + "-amd64", age=60)
    found["d_arm"] = graph.image("d-arm", sha["d"] + "-arm64", age=60)
    found["d_idx"] = graph.index((found["d_amd"], found["d_arm"]), sha["d"], age=60)
    found["v110"] = graph.index(
        (found["d_amd"], found["d_arm"]), "v1.1.0", age=55, note="release"
    )
    # Релиз v1.0.0 с тегами архитектур и SBOM; старше любого срока.
    found["r_amd"] = graph.image("r-amd", "v1.0.0-amd64", age=90)
    found["r_arm"] = graph.image("r-arm", "v1.0.0-arm64", age=90)
    found["r_idx"] = graph.index((found["r_amd"], found["r_arm"]), "v1.0.0", age=90)
    found["r_sig"] = graph.signature(found["r_idx"], age=90)
    found["r_sbom"] = graph.signature(found["r_amd"], "attest", age=90)
    # Незнакомый тег и manifest без тега и связей тоже остаются.
    found["manual"] = graph.image("manual", "debug-build", age=100)
    found["orphan"] = graph.image("orphan", age=100)
    # e: SHA-тег и релизный тег на одной версии.
    found["e_amd"] = graph.image("e-amd", sha["e"] + "-amd64", "v2.0.0-amd64", age=70)
    return graph, found


def removed(plan):
    """Digest версий, выбранных для удаления."""
    return [item["digest"] for item in plan["remove"]]


def test_old_sha_versions_go_with_their_signatures_and_nothing_else(package):
    """Старый main уходит вместе с подписями; latest, релизы и общее остаются."""
    graph, found = package
    plan = graph.plan()
    expected = {
        found["a_idx"],
        found["a_amd"],
        *found["a_sig"],
        *found["a_sbom"],
        found["a_legacy"],
        found["d_idx"],
    }
    assert set(removed(plan)) == expected
    kept = {version.digest for version in graph.versions} - expected
    # Всё, на что ссылаются latest и релизы, и общий со свежим main arm64.
    for name in ("b_idx", "b_amd", "b_arm", "a_arm", "d_amd", "d_arm", "r_amd"):
        assert found[name] in kept
    for name in ("b_sig", "b_sbom", "r_sig", "r_sbom"):
        assert set(found[name]) <= kept
    assert {found["manual"], found["orphan"], found["e_amd"], found["v110"]} <= kept
    assert plan["protected"] == 7
    assert plan["recent"] == 2


def test_parents_are_deleted_before_children(package):
    """Сначала index, затем его manifest и подписи: обрыв не ломает живой index."""
    graph, found = package
    order = removed(graph.plan())
    assert order.index(found["a_idx"]) < order.index(found["a_amd"])
    assert order.index(found["a_amd"]) < order.index(found["a_sbom"][0])


def test_reasons_describe_every_candidate(package):
    """Каждая версия плана объяснена: SHA-тег, подпись или дочерний manifest."""
    graph, found = package
    reasons = {item["digest"]: item["reason"] for item in graph.plan()["remove"]}
    assert reasons[found["a_idx"]] == "SHA-теги старше срока"
    assert reasons[found["a_sig"][1]] == "подпись или attestation удаляемого образа"
    assert reasons[found["a_sig"][0]] == "manifest или подпись удаляемого образа"


def test_longer_retention_keeps_everything(package):
    """Если все SHA-версии моложе срока, план пуст."""
    graph, _ = package
    assert removed(graph.plan(days=365)) == []


def test_untagged_child_of_old_index_is_removed_when_unshared():
    """Containerd: manifest без тега внутри старого index уходит вместе с ним."""
    graph = Graph()
    child = graph.image("child", age=50)
    other = graph.image("other", age=50)
    root = graph.index((child, other), "sha-" + "f" * 40, age=50)
    assert set(removed(graph.plan())) == {root, child, other}


def test_missing_listed_manifest_stops_planning():
    """404 не доказывает отсутствие связей: план останавливается без удаления."""
    graph = Graph()
    root = graph.image("gone", "sha-" + "f" * 40, age=50)
    del graph.store[root]
    with pytest.raises(delivery.DeliveryError, match="недоступен"):
        graph.plan()


@pytest.mark.parametrize(
    "manifests",
    [None, {}, [None], [{"digest": "sha256:short"}], [{"size": 1}]],
)
def test_malformed_index_stops_planning(manifests):
    """Неизвестные связи — ошибка, а не повод удалить потенциально нужное."""
    graph = Graph()
    graph.add(
        {"schemaVersion": 2, "mediaType": INDEX, "manifests": manifests}, "latest"
    )
    graph.image("old", "sha-" + "f" * 40, age=50)
    with pytest.raises(delivery.DeliveryError, match="manifests"):
        graph.plan()


@pytest.mark.parametrize("days", [0, -1, 3651])
def test_retention_must_be_reasonable(days):
    """Срок 0 удалил бы и только что опубликованный main; это запрещено."""
    with pytest.raises(delivery.DeliveryError, match="Срок"):
        cleanup.plan_cleanup([], Mock(), now=NOW, days=days)


@pytest.mark.parametrize(
    ("tags", "kind"),
    [
        ((), "untagged"),
        (("sha-" + "a" * 40,), "sha"),
        (("sha-" + "a" * 40 + "-amd64", "sha-" + "b" * 40 + "-arm64"), "sha"),
        (("sha256-" + "a" * 64,), "referrer"),
        (("sha256-" + "a" * 64 + ".att",), "referrer"),
        (("latest",), "protected"),
        (("v1.2.3",), "protected"),
        (("sha-" + "a" * 40, "latest"), "protected"),
        (("sha-" + "a" * 40, "sha256-" + "a" * 64), "protected"),
        (("sha-" + "a" * 39,), "protected"),
        (("sha-" + "a" * 40 + "-s390x",), "protected"),
        (("main",), "protected"),
    ],
)
def test_version_kind(tags, kind):
    """Только полностью SHA-теги считаются кандидатами; остальное защищено."""
    assert cleanup.Version(1, "sha256:" + "0" * 64, tags, NOW).kind == kind


def api_version(**changes):
    """Версия в формате REST API GitHub Packages."""
    item = {
        "id": 7,
        "name": "sha256:" + "1" * 64,
        "created_at": "2026-09-01T10:00:00Z",
        "updated_at": "2026-09-02T10:00:00Z",
        "metadata": {"container": {"tags": ["sha-" + "a" * 40]}},
    }
    item.update(changes)
    return item


def test_parse_versions_uses_latest_change_time():
    """Добавленный позже тег делает версию свежей: берётся updated_at."""
    (version,) = cleanup.parse_versions([api_version()])
    assert version.id == 7 and version.tags == ("sha-" + "a" * 40,)
    assert version.updated == datetime(2026, 9, 2, 10, tzinfo=UTC)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [None],
        [api_version(id=True)],
        [api_version(id=0)],
        [api_version(name="sha256:short")],
        [api_version(metadata={})],
        [api_version(metadata={"container": {"tags": [1]}})],
        [api_version(updated_at=None)],
        [api_version(updated_at="вчера")],
        [api_version(updated_at="2026-09-02T10:00:00")],
    ],
)
def test_parse_versions_rejects_unexpected_api_data(payload):
    """Неожиданный ответ GitHub API останавливает очистку до удаления."""
    with pytest.raises(delivery.DeliveryError):
        cleanup.parse_versions(payload)


def response(data, status=200):
    """HTTP-ответ GitHub API с JSON-телом."""
    result = MagicMock()
    result.__enter__.return_value = result
    result.read.return_value = b"" if data is None else json.dumps(data).encode()
    return result


@pytest.mark.parametrize(
    ("owner", "prefix"),
    [("User", "/users/merkertg"), ("Organization", "/orgs/merkertg")],
)
def test_packages_lists_all_pages_for_user_or_organization(owner, prefix):
    """Версии читаются постранично из пакета владельца репозитория."""
    packages = cleanup.Packages(
        "TOKEN", "https://api.github.com", "merkertg/ai-support-agent"
    )
    packages.opener = Mock()
    page = [api_version(id=index + 1) for index in range(100)]
    packages.opener.open.side_effect = [
        response({"owner": {"login": "merkertg", "type": owner}}),
        response(page),
        response([api_version(id=101)]),
    ]
    assert len(packages.versions("ai-support-agent")) == 101
    urls = [call.args[0].full_url for call in packages.opener.open.call_args_list]
    assert urls[0] == "https://api.github.com/repos/merkertg/ai-support-agent"
    assert urls[2] == (
        f"https://api.github.com{prefix}/packages/container/ai-support-agent"
        "/versions?state=active&per_page=100&page=2"
    )
    request = packages.opener.open.call_args.args[0]
    assert request.get_header("Authorization") == "Bearer TOKEN"
    assert "TOKEN" not in request.full_url


def test_packages_delete_uses_version_id():
    """Удаление адресует одну версию по её id в пакете владельца."""
    packages = cleanup.Packages(
        "TOKEN", "https://api.github.com", "merkertg/ai-support-agent"
    )
    packages.opener = Mock()
    packages.opener.open.side_effect = [
        response({"owner": {"login": "merkertg", "type": "User"}}),
        response(None, 204),
    ]
    version = cleanup.Version(42, "sha256:" + "1" * 64, (), NOW)
    packages.delete("ai-support-agent-ui", version)
    request = packages.opener.open.call_args.args[0]
    assert request.get_method() == "DELETE"
    assert request.full_url.endswith(
        "/users/merkertg/packages/container/ai-support-agent-ui/versions/42"
    )


@pytest.mark.parametrize(
    "error",
    [
        HTTPError("https://api.github.com", 403, "TOKEN detail", {}, BytesIO()),
        URLError("TOKEN unreachable"),
        TimeoutError(),
    ],
)
def test_packages_errors_fail_closed_without_secrets(error):
    """403 (нет роли Admin), сеть и тайм-аут останавливают очистку."""
    packages = cleanup.Packages(
        "TOKEN", "https://api.github.com", "merkertg/ai-support-agent"
    )
    packages.opener = Mock()
    packages.opener.open.side_effect = error
    with pytest.raises(delivery.DeliveryError) as raised:
        packages.versions("ai-support-agent")
    assert "TOKEN" not in str(raised.value)


@pytest.mark.parametrize(
    "owner",
    [None, {}, {"owner": {"login": "x", "type": "Bot"}}, {"owner": {"type": "User"}}],
)
def test_packages_require_known_owner(owner):
    """Без понятного владельца путь пакета не строится."""
    packages = cleanup.Packages(
        "TOKEN", "https://api.github.com", "merkertg/ai-support-agent"
    )
    packages.opener = Mock()
    packages.opener.open.return_value = response(owner)
    with pytest.raises(delivery.DeliveryError, match="владельца"):
        packages.versions("ai-support-agent")


def test_packages_reject_bad_json_and_oversized_answers():
    """Повреждённый или огромный ответ API не превращается в пустой список."""
    packages = cleanup.Packages(
        "TOKEN", "https://api.github.com", "merkertg/ai-support-agent"
    )
    packages.opener = Mock()
    broken = response(None)
    broken.read.return_value = b"{"
    packages.opener.open.return_value = broken
    with pytest.raises(delivery.DeliveryError, match="JSON"):
        packages.request("GET", "/x")
    broken.read.return_value = b" " * (cleanup.MAX_RESPONSE + 1)
    with pytest.raises(delivery.DeliveryError, match="большой"):
        packages.request("GET", "/x")


@pytest.mark.parametrize(
    ("token", "url", "repository"),
    [
        ("", "https://api.github.com", "merkertg/ai-support-agent"),
        ("TOKEN", "http://api.github.com", "merkertg/ai-support-agent"),
        ("TOKEN", "https://api.github.com", "merkertg/x;id"),
        ("TOKEN", "https://api.github.com", "../x"),
    ],
)
def test_packages_require_token_https_and_valid_repository(token, url, repository):
    """Токен не отправляется без HTTPS и в путь не попадает чужое имя."""
    with pytest.raises(delivery.DeliveryError):
        cleanup.Packages(token, url, repository)


def test_manifest_reader_checks_body_digest():
    """Manifest с другим содержимым, чем digest версии, останавливает очистку."""
    body, digest = encode({"schemaVersion": 2})
    registry = Mock()
    registry.fetch.return_value = (json.loads(body), body, {})
    fetch = cleanup.manifest_reader(registry, "merkertg/ai-support-agent")
    assert fetch(digest) == {"schemaVersion": 2}
    with pytest.raises(delivery.DeliveryError, match="другое содержимое"):
        fetch("sha256:" + "9" * 64)
    registry.fetch.return_value = None
    assert fetch(digest) is None


class FakePackages:
    """GitHub Packages API в памяти для двух пакетов проекта."""

    def __init__(self, graphs):
        """graphs: имя пакета → Graph."""
        self.graphs = graphs
        self.deleted = []

    def versions(self, package):
        """Вернуть версии пакета."""
        return list(self.graphs[package].versions)

    def version(self, package, version_id):
        """Перечитать версию непосредственно перед удалением."""
        return next(v for v in self.graphs[package].versions if v.id == version_id)

    def delete(self, package, version):
        """Записать удаление версии."""
        self.deleted.append((package, version.digest))


class FakeRegistry:
    """Registry для clean_packages: manifest по digest из графа пакета."""

    host = "ghcr.io"

    def __init__(self, graphs):
        """graphs: имя пакета → Graph."""
        self.graphs = graphs

    def fetch(self, remote, digest):
        """Вернуть ответ registry или None для 404."""
        body = self.graphs[remote.split("/")[1]].store.get(digest)
        return None if body is None else (json.loads(body), body, {})


@pytest.fixture
def both(package):
    """API — типичный пакет; UI — только один старый main."""
    graph, found = package
    ui = Graph()
    ui_old = ui.image("ui-old", "sha-" + "a" * 40, age=45)
    graphs = {"ai-support-agent": graph, "ai-support-agent-ui": ui}
    return graphs, found, ui_old


def test_dry_run_deletes_nothing(both):
    """Без --delete составляется план для API и UI, но ничего не удаляется."""
    graphs, found, ui_old = both
    packages = FakePackages(graphs)
    report = cleanup.clean_packages(
        packages,
        FakeRegistry(graphs),
        "Merkertg/AI-Support-Agent",
        days=30,
        delete=False,
        now=NOW,
    )
    assert packages.deleted == []
    assert [plan["package"] for plan in report] == [
        "ghcr.io/merkertg/ai-support-agent",
        "ghcr.io/merkertg/ai-support-agent-ui",
    ]
    assert removed(report[1]) == [ui_old] and report[1]["deleted"] == []


def test_delete_removes_exactly_the_plan(both):
    """С --delete удаляются ровно версии плана, родители раньше детей."""
    graphs, found, ui_old = both
    packages = FakePackages(graphs)
    report = cleanup.clean_packages(
        packages,
        FakeRegistry(graphs),
        "merkertg/ai-support-agent",
        days=30,
        delete=True,
        now=NOW,
    )
    api = [
        digest for package, digest in packages.deleted if package == "ai-support-agent"
    ]
    assert api == removed(report[0])
    assert ("ai-support-agent-ui", ui_old) in packages.deleted
    assert len(report[0]["deleted"]) == len(report[0]["remove"])


def test_protected_version_in_plan_is_never_deleted(both, monkeypatch):
    """Второй барьер: даже ошибочный план не удаляет версию с latest."""
    graphs, found, _ = both
    latest = next(v for v in graphs["ai-support-agent"].versions if "latest" in v.tags)
    monkeypatch.setattr(
        cleanup,
        "plan_cleanup",
        lambda *args, **kwargs: {"remove": [{"id": latest.id}]},
    )
    packages = FakePackages(graphs)
    with pytest.raises(delivery.DeliveryError, match="Защищённая"):
        cleanup.clean_packages(
            packages,
            FakeRegistry(graphs),
            "merkertg/ai-support-agent",
            days=30,
            delete=True,
            now=NOW,
        )
    assert packages.deleted == []


def test_main_writes_plan_and_summary(both, monkeypatch, tmp_path, capsys):
    """CLI без --delete пишет JSON-план и сводку Actions с пометкой dry-run."""
    graphs, _, _ = both
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_TOKEN", "TOKEN")
    monkeypatch.setenv("GITHUB_ACTOR", "actor")
    monkeypatch.setenv("GITHUB_REPOSITORY", "merkertg/ai-support-agent")
    packages = FakePackages(graphs)
    monkeypatch.setattr(cleanup, "Packages", lambda *args: packages)
    monkeypatch.setattr(
        cleanup, "Registry", lambda *args, **kwargs: FakeRegistry(graphs)
    )
    monkeypatch.setattr(cleanup, "datetime", Mock(now=Mock(return_value=NOW)))
    output = tmp_path / "plan" / "ghcr.json"
    assert cleanup.main(["--days", "30", "--output", str(output)]) == 0
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["delete"] is False and len(data["packages"]) == 2
    assert "dry-run" in summary.read_text(encoding="utf-8")
    assert "dry-run" in capsys.readouterr().out
    assert packages.deleted == []


def test_main_without_token_fails(monkeypatch, capsys):
    """Без GITHUB_TOKEN очистка останавливается с понятной ошибкой."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "merkertg/ai-support-agent")
    assert cleanup.main([]) == 1
    assert "GITHUB_TOKEN" in capsys.readouterr().err


def test_summary_truncates_long_plans(capsys, monkeypatch):
    """Длинный план сокращается в сводке; полный список остаётся в JSON."""
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    item = {"digest": "sha256:" + "1" * 64, "tags": [], "reason": "r"}
    plan = {
        "package": "ghcr.io/x/y",
        "versions": 60,
        "protected": 0,
        "recent": 0,
        "keep": 0,
        "remove": [item] * 60,
        "deleted": [],
    }
    cleanup.write_summary([plan], days=30, delete=True)
    out = capsys.readouterr().out
    assert "ещё 10" in out and "без тега" in out and "(удаление)" in out


def test_referrers_without_fallback_tag_follow_their_subject():
    """Registry с Referrers API: подпись без тега находится по полю subject."""
    graph = Graph()
    old = graph.image("old", "sha-" + "a" * 40, age=50)
    kept = graph.image("kept", "latest", age=50)
    signatures = {}
    for name, target in (("old", old), ("kept", kept)):
        signed, fallback = graph.signature(target, age=50)
        graph.versions = [v for v in graph.versions if v.digest != fallback]
        signatures[name] = signed
    assert set(removed(graph.plan())) == {old, signatures["old"]}


@pytest.mark.parametrize("tag", ["latest", "v1.0.0", "sha-" + "a" * 40])
def test_missing_kept_index_stops_entire_plan(tag):
    """Если живой index не читается, его старые дочерние SHA нельзя удалить."""
    graph = Graph()
    amd = graph.image("shared-amd", "sha-" + "b" * 40 + "-amd64", age=60)
    arm = graph.image("shared-arm", "sha-" + "b" * 40 + "-arm64", age=60)
    root = graph.index((amd, arm), tag, age=0)
    del graph.store[root]
    with pytest.raises(delivery.DeliveryError, match="недоступен"):
        graph.plan()


def test_missing_referenced_manifest_stops_entire_plan():
    """Digest в защищённом index обязан читаться, даже если REST его не перечислил."""
    graph = Graph()
    child = graph.image("missing-child", age=60)
    arm = graph.image("arm", age=60)
    graph.index((child, arm), "latest")
    graph.versions = [version for version in graph.versions if version.digest != child]
    del graph.store[child]
    with pytest.raises(delivery.DeliveryError, match="недоступен"):
        graph.plan()


def test_retained_untagged_index_keeps_its_old_sha_children():
    """Несвязанный index без тега остаётся и не теряет общий со старым main child."""
    graph = Graph()
    shared = graph.image("shared", "sha-" + "b" * 40 + "-amd64", age=60)
    old_arm = graph.image("old-arm", age=60)
    old = graph.index((shared, old_arm), "sha-" + "b" * 40, age=60)
    other_arm = graph.image("kept-arm", age=60)
    kept = graph.index((shared, other_arm), age=60)
    assert set(removed(graph.plan())) == {old, old_arm}
    assert shared not in removed(graph.plan()) and kept not in removed(graph.plan())


def test_protected_oci_artifact_keeps_its_subject():
    """Артефакт с незнакомым тегом сохраняется вместе с исходным образом subject."""
    graph = Graph()
    image = graph.image("old", "sha-" + "a" * 40, age=60)
    graph.add(
        {"schemaVersion": 2, "mediaType": MANIFEST, "subject": {"digest": image}},
        "manual-attestation",
        age=60,
    )
    assert removed(graph.plan()) == []


@pytest.mark.parametrize("subject", [None, {}, {"digest": "sha256:short"}, "invalid"])
def test_malformed_subject_stops_plan(subject):
    """Неизвестный subject OCI не считается отсутствием связи."""
    graph = Graph()
    graph.add({"schemaVersion": 2, "mediaType": MANIFEST, "subject": subject}, "latest")
    with pytest.raises(delivery.DeliveryError, match="subject"):
        graph.plan()


def test_index_without_manifests_stops_plan():
    """Пустое описание index не доказывает отсутствие дочерних образов."""
    graph = Graph()
    graph.add({"schemaVersion": 2, "mediaType": INDEX}, "latest")
    with pytest.raises(delivery.DeliveryError, match="не содержит"):
        graph.plan()


def test_packages_reads_current_version_before_delete():
    """Повторное чтение адресует ровно текущий version ID пакета."""
    packages = cleanup.Packages("TOKEN", "https://api.github.com", "owner/repo")
    packages.base = "/users/owner"
    body = {
        "id": 8,
        "name": "sha256:" + "a" * 64,
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-02T00:00:00Z",
        "metadata": {"container": {"tags": ["latest"]}},
    }
    packages.request = Mock(return_value=body)
    current = packages.version("repo", 8)
    packages.request.assert_called_once_with(
        "GET", "/users/owner/packages/container/repo/versions/8"
    )
    assert current.id == 8 and current.kind == "protected"


def test_ui_graph_failure_happens_before_api_delete(both):
    """Нечитаемый UI останавливает оба пакета, пока API ещё не изменён."""
    graphs, _, _ = both
    graphs["ai-support-agent-ui"].add(
        {"schemaVersion": 2, "mediaType": INDEX}, "latest"
    )
    packages = FakePackages(graphs)
    with pytest.raises(delivery.DeliveryError, match="не содержит"):
        cleanup.clean_packages(
            packages,
            FakeRegistry(graphs),
            "merkertg/ai-support-agent",
            days=30,
            delete=True,
            now=NOW,
        )
    assert packages.deleted == []


@pytest.mark.parametrize("changed", ["ai-support-agent", "ai-support-agent-ui"])
def test_changed_package_snapshot_stops_both_before_delete(both, changed):
    """Новый тег в любом пакете между планами и удалением требует нового запуска."""
    graphs, _, _ = both
    packages = FakePackages(graphs)
    original = packages.versions
    calls = {}

    def versions(package):
        """На второй запрос одного пакета имитировать появление защищённого тега."""
        calls[package] = calls.get(package, 0) + 1
        values = original(package)
        if package == changed and calls[package] >= 2:
            values[0] = replace(values[0], tags=(*values[0].tags, "v4.0.0"))
        return values

    packages.versions = versions
    with pytest.raises(delivery.DeliveryError, match="изменились после плана"):
        cleanup.clean_packages(
            packages,
            FakeRegistry(graphs),
            "merkertg/ai-support-agent",
            days=30,
            delete=True,
            now=NOW,
        )
    assert packages.deleted == []


@pytest.mark.parametrize("change", ["latest", "new-sha", "digest", "date", "id"])
def test_changed_candidate_is_not_deleted(both, change):
    """Теги, digest, ID и даты сверяются заново перед каждой отдельной DELETE."""
    graphs, _, _ = both
    packages = FakePackages(graphs)
    original = packages.version

    def version(package, version_id):
        """Имитировать изменение, случившееся уже после повторного полного списка."""
        current = original(package, version_id)
        if change == "latest":
            return replace(current, tags=(*current.tags, "latest"))
        if change == "new-sha":
            return replace(current, tags=(*current.tags, "sha-" + "f" * 40))
        if change == "digest":
            return replace(current, digest="sha256:" + "f" * 64)
        if change == "id":
            return replace(current, id=current.id + 1)
        return replace(current, updated=NOW)

    packages.version = version
    with pytest.raises(delivery.DeliveryError, match="изменена после плана"):
        cleanup.clean_packages(
            packages,
            FakeRegistry(graphs),
            "merkertg/ai-support-agent",
            days=30,
            delete=True,
            now=NOW,
        )
    assert packages.deleted == []


def test_current_version_read_failure_stops_delete(both):
    """При недоступном повторном GET никаких DELETE по старому плану нет."""
    graphs, _, _ = both
    packages = FakePackages(graphs)
    packages.version = Mock(side_effect=delivery.DeliveryError("HTTP 404"))
    with pytest.raises(delivery.DeliveryError, match="HTTP 404"):
        cleanup.clean_packages(
            packages,
            FakeRegistry(graphs),
            "merkertg/ai-support-agent",
            days=30,
            delete=True,
            now=NOW,
        )
    assert packages.deleted == []


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"schemaVersion": True, "mediaType": INDEX},
        {"schemaVersion": 1, "mediaType": INDEX},
        {"schemaVersion": 2, "mediaType": "application/unknown"},
    ],
)
def test_unknown_manifest_format_stops_plan(data):
    """Нераспознанный формат не разрешает делать вывод, что связей нет."""
    graph = Graph()
    graph.add(data, "latest")
    with pytest.raises(delivery.DeliveryError, match="неподдерживаемый формат"):
        graph.plan()


@pytest.mark.parametrize("same", ["id", "digest"])
def test_duplicate_versions_stop_plan(same):
    """Дубликат ID/digest означает непоследовательный снимок страниц API."""
    graph = Graph()
    graph.image("one", "sha-" + "a" * 40, age=60)
    graph.image("two", "sha-" + "b" * 40, age=60)
    first, second = graph.versions
    change = {same: getattr(first, same)}
    graph.versions[1] = replace(second, **change)
    with pytest.raises(delivery.DeliveryError, match="дубликаты"):
        graph.plan()


def test_duplicate_in_fresh_snapshot_stops_delete(both):
    """Повтор ID во втором снимке не скрывается преобразованием списка в словарь."""
    graphs, _, _ = both
    packages = FakePackages(graphs)
    original = packages.versions
    calls = {}

    def versions(package):
        """После плана имитировать дубликат на стыке страниц GitHub API."""
        calls[package] = calls.get(package, 0) + 1
        values = original(package)
        return values + values[:1] if calls[package] > 1 else values

    packages.versions = versions
    with pytest.raises(delivery.DeliveryError, match="изменились после плана"):
        cleanup.clean_packages(
            packages,
            FakeRegistry(graphs),
            "merkertg/ai-support-agent",
            days=30,
            delete=True,
            now=NOW,
        )
    assert packages.deleted == []
