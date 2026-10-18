"""Политика обновлений, границы write-job и создание одной проверяемой PR."""

import json
import subprocess
import urllib.error
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from scripts import update_dependencies as updater

NOW = datetime(2026, 10, 19, 12, 37, tzinfo=UTC)
BASE = "a" * 40
SOURCE = (
    '# комментарий\n[project]\nname = "demo"\n\n[tool.uv]\n'
    'exclude-newer = "2026-10-04T00:00:00Z"\n\n# конец\n'
)
LOCK = (
    'version = 1\n[[package]]\nname = "demo"\nversion = "0.1.0"\n'
    'source = {virtual = "."}\n'
)


@pytest.fixture
def proposed(tmp_path, monkeypatch):
    """Создать доверенную базу, два предложенных файла и перехватить процессы."""
    root, artifact = tmp_path / "root", tmp_path / "artifact"
    root.mkdir()
    artifact.mkdir()
    (root / "pyproject.toml").write_text(SOURCE)
    (root / "uv.lock").write_text(LOCK)
    (artifact / "pyproject.toml").write_text(updater.update_cutoff(SOURCE, NOW))
    (artifact / "uv.lock").write_text(LOCK)
    process = Mock()
    monkeypatch.setattr(updater.subprocess, "run", process)
    return root, artifact, process


def api_for_publish(*, existing=False, pulls=False, changed_base=False):
    """Подменить GitHub API, сохранив реальные ветвления публикации."""
    api = Mock(repository="merkertg/ai-support-agent")

    def response(method, suffix, data=None):
        """Вернуть только ответы, которые действительно запрашивает publisher."""
        if suffix == "git/ref/heads/main":
            return {"object": {"sha": "b" * 40 if changed_base else BASE}}
        if suffix == f"git/commits/{BASE}":
            return {"tree": {"sha": "base-tree"}}
        if suffix == "git/trees":
            return {"sha": "updated-tree"}
        if suffix == "git/commits":
            return {"sha": "updated-commit"}
        if suffix == f"git/ref/heads/{updater.BRANCH}":
            return {"object": {"sha": "old"}} if existing else None
        if suffix.startswith("pulls?"):
            return [{"number": 31}] if pulls else []
        return None

    api.request.side_effect = response
    return api


def test_cutoff_uses_utc_midnight_and_preserves_other_text():
    """Полночь UTC даёт как минимум 14 полных дней, комментарии не теряются."""
    result = updater.update_cutoff(SOURCE, NOW)
    assert result == SOURCE.replace("2026-10-04", "2026-10-05")
    assert (
        updater.cutoff_value(datetime.fromisoformat("2026-10-19T02:00:00+05:00"))
        == "2026-10-04T00:00:00Z"
    )


@pytest.mark.parametrize(
    "source",
    [
        SOURCE.replace("2026-10-04", "2026-10-18"),
        SOURCE.replace('Z"', '"'),
        SOURCE.replace(
            'exclude-newer = "2026-10-04T00:00:00Z"', "exclude-newer = false"
        ),
        SOURCE.replace("[tool.uv]", "[other]"),
    ],
)
def test_invalid_or_recent_existing_cutoff_is_rejected(source):
    """Нельзя обходить карантин или молча переносить настройку другого раздела."""
    with pytest.raises(ValueError):
        updater.update_cutoff(source, NOW)


def test_naive_current_time_is_rejected():
    """Дата без часового пояса не может определять окно безопасности."""
    with pytest.raises(ValueError, match="часовой пояс"):
        updater.cutoff_value(NOW.replace(tzinfo=None))


def test_prepare_disables_builds_and_checks_age(proposed):
    """Резолвер не запускает backend пакетов, затем проверяются даты lock."""
    root, _, process = proposed
    updater.prepare(root, "trusted-uv", NOW)
    assert process.call_args_list[0].args[0] == [
        "trusted-uv",
        "lock",
        "--upgrade",
        "--no-build",
    ]
    assert "scripts/check_dependency_age.py" in process.call_args_list[1].args[0]
    assert all(call.kwargs["check"] for call in process.call_args_list)


def test_validated_files_keep_two_allowed_paths(proposed):
    """Writer повторно проверяет возраст без запуска резолвера или установки."""
    root, artifact, process = proposed
    assert set(updater.validated_files(root, artifact, NOW)) == set(updater.FILES)
    command = process.call_args.args[0]
    assert "--lock" in command and "--pyproject" in command
    assert "uv" not in command


@pytest.mark.parametrize(
    "change", ["extra", "symlink", "dependency", "source", "oversized"]
)
def test_writer_rejects_unexpected_artifacts(proposed, change):
    """Нельзя внести код, новые источники, подмену файла или списка пакетов."""
    root, artifact, process = proposed
    if change == "extra":
        (artifact / "payload.py").write_text("print('unexpected')")
    elif change == "symlink":
        (artifact / "uv.lock").unlink()
        (artifact / "uv.lock").symlink_to(root / "uv.lock")
    elif change == "dependency":
        path = artifact / "pyproject.toml"
        path.write_text(path.read_text().replace('name = "demo"', 'name = "other"'))
    elif change == "source":
        (artifact / "uv.lock").write_text(
            LOCK.replace('virtual = "."', 'git = "https://evil.example/repo"')
        )
    else:
        (artifact / "uv.lock").write_bytes(b" " * 10_000_001)
    with pytest.raises(ValueError):
        updater.validated_files(root, artifact, NOW)
    process.assert_not_called()


def test_age_failure_prevents_network_mutations(proposed):
    """Свежий пакет блокирует публикацию раньше любого обращения к GitHub."""
    root, artifact, process = proposed
    process.side_effect = subprocess.CalledProcessError(1, "age check")
    api = api_for_publish()
    with pytest.raises(subprocess.CalledProcessError):
        updater.publish(root, artifact, BASE, NOW, api)
    api.request.assert_not_called()


def test_new_pr_commits_only_allowed_files_and_dispatches_ci(proposed):
    """Первая PR использует main как родителя и отдельный CI без записи в main."""
    root, artifact, _ = proposed
    api = api_for_publish()
    updater.publish(root, artifact, BASE, NOW, api)
    calls = api.request.call_args_list
    tree = next(
        call.args[2] for call in calls if call.args[:2] == ("POST", "git/trees")
    )
    assert {item["path"] for item in tree["tree"]} == set(updater.FILES)
    commit = next(
        call.args[2] for call in calls if call.args[:2] == ("POST", "git/commits")
    )
    assert commit["parents"] == [BASE]
    assert any(call.args[:2] == ("POST", "pulls") for call in calls)
    assert calls[-1].args == (
        "POST",
        "actions/workflows/ci.yml/dispatches",
        {"ref": updater.BRANCH},
    )
    assert not any(call.args[:2] == ("PATCH", "git/refs/heads/main") for call in calls)


def test_existing_bot_branch_and_pr_are_reused(proposed):
    """Повторный запуск обновляет одну зарезервированную ветку и ту же PR."""
    root, artifact, _ = proposed
    api = api_for_publish(existing=True, pulls=True)
    updater.publish(root, artifact, BASE, NOW, api)
    calls = api.request.call_args_list
    assert any(
        call.args[:2] == ("PATCH", f"git/refs/heads/{updater.BRANCH}") for call in calls
    )
    assert any(call.args[:2] == ("PATCH", "pulls/31") for call in calls)
    assert not any(call.args[:2] == ("POST", "pulls") for call in calls)


def test_main_change_stops_stale_proposal(proposed):
    """Обновившаяся main требует нового запуска, а не перезаписи чужих изменений."""
    root, artifact, _ = proposed
    api = api_for_publish(changed_base=True)
    with pytest.raises(ValueError, match="main изменилась"):
        updater.publish(root, artifact, BASE, NOW, api)
    assert api.request.call_count == 1


def test_no_changes_do_not_create_commit_or_pr(proposed):
    """Если файл и дата уже актуальны, лишняя PR и CI не создаются."""
    root, artifact, _ = proposed
    (root / "pyproject.toml").write_text((artifact / "pyproject.toml").read_text())
    api = api_for_publish()
    updater.publish(root, artifact, BASE, NOW, api)
    assert api.request.call_count == 1


@pytest.mark.parametrize("sha", ["main", "a" * 39, "A" * 40, "../main"])
def test_writer_requires_full_commit_sha(proposed, sha):
    """Текст ветки не подменяет конкретный доверенный коммит."""
    root, artifact, _ = proposed
    api = api_for_publish()
    with pytest.raises(ValueError, match="полный SHA"):
        updater.publish(root, artifact, sha, NOW, api)
    api.request.assert_not_called()


@pytest.mark.parametrize(
    "repository,token",
    [("../other", "token"), ("owner/repo/extra", "token"), ("owner/repo", "")],
)
def test_api_requires_repository_and_token(repository, token):
    """Нельзя отправлять токен произвольному хосту через подмену имени репозитория."""
    with pytest.raises(ValueError):
        updater.GitHub(repository, token)


def test_api_encodes_json_and_does_not_print_token(monkeypatch, capsys):
    """Запрос идёт только в GitHub, токен остаётся в заголовке Authorization."""
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b'{"sha":"created"}'
    open_url = Mock(return_value=response)
    monkeypatch.setattr(
        updater.urllib.request, "build_opener", Mock(return_value=Mock(open=open_url))
    )
    api = updater.GitHub("merkertg/ai-support-agent", "private-token")
    assert api.request("POST", "git/trees", {"tree": []}) == {"sha": "created"}
    request = open_url.call_args.args[0]
    assert (
        request.full_url
        == "https://api.github.com/repos/merkertg/ai-support-agent/git/trees"
    )
    assert json.loads(request.data) == {"tree": []}
    assert request.headers["Authorization"] == "Bearer private-token"
    assert capsys.readouterr().out == ""


def test_api_reports_permission_error_without_body_or_token(monkeypatch):
    """Запрет создания PR сообщает код ошибки, не раскрывая содержимое ответа."""
    error = urllib.error.HTTPError(
        "https://api.github.com", 403, "private-token", {}, None
    )
    monkeypatch.setattr(
        updater.urllib.request,
        "build_opener",
        Mock(return_value=Mock(open=Mock(side_effect=error))),
    )
    api = updater.GitHub("merkertg/ai-support-agent", "private-token")
    with pytest.raises(RuntimeError, match="HTTP 403") as result:
        api.request("POST", "pulls", {})
    assert "private-token" not in str(result.value)


def test_missing_branch_is_none(monkeypatch):
    """Отсутствующая bot-ветка означает создание, остальные ошибки не скрываются."""
    error = urllib.error.HTTPError("https://api.github.com", 404, "missing", {}, None)
    monkeypatch.setattr(
        updater.urllib.request,
        "build_opener",
        Mock(return_value=Mock(open=Mock(side_effect=error))),
    )
    api = updater.GitHub("merkertg/ai-support-agent", "token")
    assert api.request("GET", f"git/ref/heads/{updater.BRANCH}") is None
    with pytest.raises(RuntimeError, match="HTTP 404"):
        api.request("GET", "pulls?state=open")


def test_redirect_handler_never_forwards_authorization():
    """Даже HTTPS-редирект не создаёт повторный запрос с токеном."""
    request = updater.urllib.request.Request(
        "https://api.github.com/repos/merkertg/ai-support-agent/git/trees",
        headers={"Authorization": "Bearer private-token"},
    )
    assert (
        updater.NoRedirect().redirect_request(
            request, None, 302, "Found", {}, "https://other.example/collect"
        )
        is None
    )
    api = updater.GitHub("merkertg/ai-support-agent", "private-token")
    assert any(
        isinstance(handler, updater.NoRedirect) for handler in api.opener.handlers
    )


def test_api_redirect_is_failure_without_second_request(monkeypatch):
    """Ответ 302 завершает публикацию, токен не отправляется по Location."""
    error = urllib.error.HTTPError(
        "https://api.github.com",
        302,
        "Found",
        {"Location": "https://other.example/collect"},
        None,
    )
    opener = Mock(open=Mock(side_effect=error))
    monkeypatch.setattr(
        updater.urllib.request, "build_opener", Mock(return_value=opener)
    )
    api = updater.GitHub("merkertg/ai-support-agent", "private-token")
    with pytest.raises(RuntimeError, match="HTTP 302"):
        api.request("POST", "git/trees", {})
    assert opener.open.call_count == 1


def test_api_limits_response_before_json_decoding(monkeypatch):
    """Слишком большой ответ отклоняется после ограниченного чтения."""
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b" " * (updater.MAX_RESPONSE_BYTES + 1)
    opener = Mock(open=Mock(return_value=response))
    monkeypatch.setattr(
        updater.urllib.request, "build_opener", Mock(return_value=opener)
    )
    api = updater.GitHub("merkertg/ai-support-agent", "private-token")
    with pytest.raises(RuntimeError, match="2 MB"):
        api.request("GET", "git/commits/" + BASE)
    response.read.assert_called_once_with(updater.MAX_RESPONSE_BYTES + 1)
