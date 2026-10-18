"""Политика возраста зависимостей: свежие выпуски и lock без дат не проходят."""

from datetime import UTC, datetime, timedelta

import pytest

from scripts import check_dependency_age as age

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
MIN_AGE = timedelta(days=14)
OLD_ENOUGH = "2026-10-04T11:59:59Z"
TOO_FRESH = "2026-10-17T09:00:00Z"
PYPROJECT = '[tool.uv]\nexclude-newer = "2026-10-04T00:00:00Z"\n'


def package(name: str, *times: str, source: dict | None = None) -> dict:
    """Собрать запись lock с указанными временами загрузки файлов."""
    return {
        "name": name,
        "version": "1.0.0",
        "source": source or {"registry": "https://pypi.org/simple"},
        "wheels": [
            {"url": f"https://files/{name}.whl", "upload-time": t} for t in times
        ],
    }


def lock_text(*packages: dict) -> str:
    """Записать минимальный uv.lock в TOML, понятный tomllib."""
    lines = ["version = 1", ""]
    for item in packages:
        source = ", ".join(
            f'{key} = "{value}"' for key, value in item["source"].items()
        )
        lines += [
            "[[package]]",
            f'name = "{item["name"]}"',
            f'version = "{item["version"]}"',
            f"source = {{ {source} }}",
            "wheels = [",
            *[
                f'  {{ url = "{wheel["url"]}", '
                f'upload-time = "{wheel["upload-time"]}" }},'
                for wheel in item["wheels"]
            ],
            "]",
            "",
        ]
    return "\n".join(lines)


def test_release_time_is_the_earliest_file_of_the_version() -> None:
    """Поздняя загрузка отдельного wheel не делает версию моложе."""
    item = package("lib", TOO_FRESH, OLD_ENOUGH)
    assert age.release_time(item) == age.parse_time(OLD_ENOUGH)


def test_fresh_and_undated_registry_packages_fail() -> None:
    """Слишком свежий пакет и пакет без upload-time попадают в отчёт."""
    lock = {
        "package": [
            package("old", OLD_ENOUGH),
            package("fresh", TOO_FRESH),
            package("undated"),
        ]
    }
    errors, checked = age.check_lock(lock, NOW, MIN_AGE)
    assert checked == 2
    assert any(
        line.startswith("fresh 1.0.0: опубликован 2026-10-17") for line in errors
    )
    assert any(
        line.startswith("undated 1.0.0: в uv.lock нет upload-time") for line in errors
    )
    assert not any(line.startswith("old ") for line in errors)


def test_project_itself_is_not_checked() -> None:
    """Виртуальный пакет проекта не имеет даты PyPI и не считается нарушением."""
    lock = {"package": [package("ai-support-agent", source={"virtual": "."})]}
    assert age.check_lock(lock, NOW, MIN_AGE) == ([], 0)


@pytest.mark.parametrize(
    ("pyproject", "expected"),
    [
        ({"tool": {"uv": {"exclude-newer": "2026-10-04T00:00:00Z"}}}, []),
        ({"tool": {"uv": {"exclude-newer": "2026-10-10T00:00:00Z"}}}, ["ближе 14"]),
        ({}, ["нет [tool.uv] exclude-newer"]),
    ],
)
def test_exclude_newer_must_respect_the_same_age(pyproject, expected) -> None:
    """Граница uv тоже не может пропускать выпуски моложе двух недель."""
    errors = age.check_exclude_newer(pyproject, NOW, MIN_AGE)
    assert len(errors) == len(expected)
    assert all(part in line for part, line in zip(expected, errors, strict=True))


def test_cli_exit_codes(tmp_path, capsys) -> None:
    """0 — политика соблюдена, 1 — свежий пакет, 2 — неверный lock."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(PYPROJECT, encoding="utf-8")
    lock = tmp_path / "uv.lock"
    common = ["--lock", str(lock), "--pyproject", str(pyproject)]
    common += ["--now", "2026-10-18T12:00:00Z"]

    lock.write_text(lock_text(package("old", OLD_ENOUGH)), encoding="utf-8")
    assert age.main(common) == 0
    assert "Dependency age: OK, 1 пакетов" in capsys.readouterr().out

    lock.write_text(lock_text(package("fresh", TOO_FRESH)), encoding="utf-8")
    assert age.main(common) == 1
    assert "fresh 1.0.0" in capsys.readouterr().err

    lock.write_text("not = [valid", encoding="utf-8")
    assert age.main(common) == 2
    assert "invalid input" in capsys.readouterr().err


def test_committed_lock_follows_the_policy_today() -> None:
    """Зафиксированный lock соответствует политике; со временем пакеты только старше."""
    assert age.main([]) == 0
