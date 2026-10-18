"""Защитить CI-проверку документации от ложного успеха и исполнения примеров."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_project.py"
SPEC = importlib.util.spec_from_file_location("project_checks", SCRIPT)
checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checks)


@pytest.mark.parametrize(
    "source,missing",
    [
        (
            'def outer():\n    """Пояснение."""\n    def inner():\n        pass\n',
            "inner",
        ),
        ("class Empty:\n    pass\n", "Empty"),
        ("async def work():\n    pass\n", "work"),
    ],
)
def test_missing_nested_and_async_explanations(tmp_path, source, missing):
    """Нельзя спрятать функцию без пояснения внутри другой функции или класса."""
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    errors = checks.check_python(path)
    assert len(errors) == 1
    assert missing in errors[0]


@pytest.mark.parametrize(
    "body,expected",
    [
        ('```json\n{"ok": }\n```', "неверный JSON"),
        ("```bash\nif true; then\n```", "неверный Bash"),
        ("```text\nнет завершения", "незакрытый"),
        ("[Инструкция](missing.md)", "не найдена"),
        ("[Вне проекта](../outside.md)", "за пределы"),
    ],
)
def test_invalid_documentation_fails(tmp_path, body, expected):
    """Повреждённые ссылки, код и ограждения блокируют CI с понятным сообщением."""
    path = tmp_path / "guide.md"
    path.write_text(body, encoding="utf-8")
    errors, _ = checks.check_markdown(path, tmp_path)
    assert len(errors) == 1
    assert expected in errors[0]


def test_fences_links_and_bash_are_checked_without_execution(tmp_path):
    """Пример Bash не исполняется, а текст внутри вложенного ограждения игнорируется."""
    marker = tmp_path / "must-not-exist"
    (tmp_path / "other file.md").write_text("# Цель\n", encoding="utf-8")
    path = tmp_path / "guide.md"
    path.write_text(
        f"```bash\ntouch '{marker}'\n```\n"
        '```json\n{"ok": true}\n```\n'
        "````markdown\n```bash\nif broken\n```\n[нет](absent.md)\n````\n"
        "[Локально](<other file.md>) [Сеть](https://example.org/no-fetch)\n"
        "[Якорь](#цель) `[Не ссылка](missing.md)`\n",
        encoding="utf-8",
    )
    errors, counts = checks.check_markdown(path, tmp_path)
    assert errors == []
    assert counts["bash_blocks"] == counts["json_blocks"] == 1
    assert counts["markdown_blocks"] == 1
    assert counts["links"] == 3
    assert not marker.exists()


def test_powershell_is_counted_as_unchecked_without_pwsh(tmp_path, monkeypatch):
    """Без pwsh пример не выдаётся за проверенный: он попадает в отдельный счётчик."""
    monkeypatch.setattr(checks, "PWSH", None)
    path = tmp_path / "guide.md"
    path.write_text("```powershell\nif (\n```\n", encoding="utf-8")
    errors, counts = checks.check_markdown(path, tmp_path)
    assert errors == []
    assert counts["powershell_blocks"] == counts["powershell_unchecked"] == 1


def test_powershell_parse_error_is_reported(tmp_path, monkeypatch):
    """Ошибка парсера pwsh становится ошибкой проверки с текстом сообщения."""
    calls = []

    class Result:
        """Ответ парсера с синтаксической ошибкой."""

        returncode = 1
        stdout = b"Missing closing ')'"

    def fake_run(command, **kwargs):
        """Проверить вызов: только разбор файла, без выполнения примера."""
        calls.append(command)
        script = command[-1]
        assert "Parser]::ParseFile(" in script and "Invoke-Expression" not in script
        return Result()

    monkeypatch.setattr(checks, "PWSH", "pwsh")
    monkeypatch.setattr(checks.subprocess, "run", fake_run)
    path = tmp_path / "guide.md"
    path.write_text("```powershell\nif (\n```\n", encoding="utf-8")
    errors, counts = checks.check_markdown(path, tmp_path)
    assert len(calls) == 1 and calls[0][:3] == ["pwsh", "-NoProfile", "-NonInteractive"]
    assert errors == [f"{path}:1: неверный PowerShell: Missing closing ')'"]
    assert counts["powershell_unchecked"] == 0


def test_ci_requires_powershell_parser(monkeypatch, capsys):
    """С --require-powershell непроверенные PowerShell-примеры — ошибка."""
    monkeypatch.setattr(checks, "PWSH", None)
    assert checks.main(["--require-powershell"]) == 1
    assert "pwsh не найден" in capsys.readouterr().err
