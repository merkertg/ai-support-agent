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
    path.write_text(source)
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
    path.write_text(body)
    errors, _ = checks.check_markdown(path, tmp_path)
    assert len(errors) == 1
    assert expected in errors[0]


def test_fences_links_and_bash_are_checked_without_execution(tmp_path):
    """Пример Bash не исполняется, а текст внутри вложенного ограждения игнорируется."""
    marker = tmp_path / "must-not-exist"
    (tmp_path / "other file.md").write_text("# Цель\n")
    path = tmp_path / "guide.md"
    path.write_text(
        f"```bash\ntouch '{marker}'\n```\n"
        '```json\n{"ok": true}\n```\n'
        "````markdown\n```bash\nif broken\n```\n[нет](absent.md)\n````\n"
        "[Локально](<other file.md>) [Сеть](https://example.org/no-fetch)\n"
        "[Якорь](#цель) `[Не ссылка](missing.md)`\n"
    )
    errors, counts = checks.check_markdown(path, tmp_path)
    assert errors == []
    assert counts["bash_blocks"] == counts["json_blocks"] == 1
    assert counts["markdown_blocks"] == 1
    assert counts["links"] == 3
    assert not marker.exists()
