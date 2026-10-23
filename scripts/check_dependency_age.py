"""Проверить, что каждая зависимость из uv.lock опубликована не менее N дней назад.

Политика проекта: новая версия пакета попадает в lock после двух недель
на PyPI либо по обоснованному исключению для точной security-версии.
uv записывает в lock время загрузки каждого файла; скрипт берёт самое
раннее время версии и сравнивает его с текущим моментом. Дополнительно
проверяется дата `[tool.uv] exclude-newer` в pyproject.toml: по ней uv отбирает
версии при `uv lock`. Используется только стандартная библиотека, без сети.
"""

import argparse
import sys
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MIN_AGE_DAYS = 14


def parse_time(value: str) -> datetime:
    """Прочитать время ISO 8601 и потребовать часовой пояс."""
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(f"время без часового пояса: {value}")
    return moment


def release_time(package: dict) -> datetime | None:
    """Вернуть самое раннее время загрузки файлов версии или None без данных."""
    files = list(package.get("wheels", []))
    if "sdist" in package:
        files.append(package["sdist"])
    times = [parse_time(item["upload-time"]) for item in files if "upload-time" in item]
    return min(times) if times else None


def security_exceptions(pyproject: dict) -> dict[str, str]:
    """Прочитать точные security-версии; пустое обоснование запрещает допуск."""
    rules = (
        pyproject.get("tool", {})
        .get("dependency-age", {})
        .get("security-exceptions", {})
    )
    if not isinstance(rules, dict):
        raise ValueError("security-exceptions должен быть таблицей точных версий")
    exceptions = {}
    for name, rule in rules.items():
        if not isinstance(rule, dict) or any(
            not isinstance(rule.get(key), str) or not rule[key].strip()
            for key in ("version", "reason", "advisory")
        ):
            raise ValueError(
                f"{name}: security-исключению нужны version, reason, advisory"
            )
        exceptions[name] = rule["version"]
    return exceptions


def check_lock(
    lock: dict,
    now: datetime,
    min_age: timedelta,
    exceptions: dict[str, str] | None = None,
) -> tuple[list[str], int]:
    """Найти слишком свежие пакеты и пакеты реестра без даты публикации."""
    errors = []
    checked = 0
    cutoff = now - min_age
    for package in lock.get("package", []):
        if "registry" not in package.get("source", {}):
            # Сам проект (virtual или editable) на PyPI не публикуется.
            continue
        name = f"{package['name']} {package['version']}"
        released = release_time(package)
        if released is None:
            errors.append(f"{name}: в uv.lock нет upload-time, возраст не проверить")
            continue
        checked += 1
        allowed = (exceptions or {}).get(package["name"]) == package["version"]
        if released > cutoff and (not allowed or released > now):
            age = (now - released).days
            errors.append(f"{name}: опубликован {released.date()}, возраст {age} дн.")
    return errors, checked


def check_exclude_newer(
    pyproject: dict,
    now: datetime,
    min_age: timedelta,
    exceptions: dict[str, str] | None = None,
) -> list[str]:
    """Потребовать границу uv не ближе минимального возраста к текущей дате."""
    value = pyproject.get("tool", {}).get("uv", {}).get("exclude-newer")
    if value is None:
        return ["pyproject.toml: нет [tool.uv] exclude-newer"]
    if parse_time(value) > now - min_age:
        return [
            f"pyproject.toml: exclude-newer {value} ближе "
            f"{min_age.days} дн. к текущей дате"
        ]
    errors = []
    bounds = pyproject.get("tool", {}).get("uv", {}).get("exclude-newer-package", {})
    for name, bound in bounds.items():
        if not isinstance(bound, str):
            errors.append(f"{name}: exclude-newer-package требует фиксированную дату")
        elif parse_time(bound) > now:
            errors.append(f"{name}: exclude-newer-package не может быть в будущем")
        elif parse_time(bound) > now - min_age and name not in (exceptions or {}):
            errors.append(f"{name}: свежая граница требует точного security-исключения")
    return errors


def use_utf8_output() -> None:
    """Печатать UTF-8 и при перенаправлении вывода в Windows, где по умолчанию cp1252.

    Консоль Windows и так получает Unicode; меняется только поток в файл или pipe.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Вернуть 0 при соблюдении политики, 1 при нарушении, 2 при неверных файлах."""
    use_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=PROJECT_ROOT / "uv.lock")
    parser.add_argument(
        "--pyproject", type=Path, default=PROJECT_ROOT / "pyproject.toml"
    )
    parser.add_argument("--min-age-days", type=int, default=DEFAULT_MIN_AGE_DAYS)
    # Фиксированное «сейчас» нужно тестам; в CI используется текущее время.
    parser.add_argument("--now", type=parse_time, default=None)
    arguments = parser.parse_args(argv)
    if arguments.min_age_days < 0:
        parser.error("--min-age-days не может быть отрицательным")
    now = arguments.now or datetime.now(UTC)
    min_age = timedelta(days=arguments.min_age_days)
    try:
        lock = tomllib.loads(arguments.lock.read_text(encoding="utf-8"))
        pyproject = tomllib.loads(arguments.pyproject.read_text(encoding="utf-8"))
        exceptions = security_exceptions(pyproject)
        errors, checked = check_lock(lock, now, min_age, exceptions)
        errors += check_exclude_newer(pyproject, now, min_age, exceptions)
    except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError) as error:
        print(f"Dependency age: invalid input: {error}", file=sys.stderr)
        return 2
    if errors:
        print(
            f"Dependency age: FAIL, нужно не меньше {min_age.days} дн. на PyPI:",
            file=sys.stderr,
        )
        for line in errors:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(
        f"Dependency age: OK, {checked} пакетов проверены; граница "
        f"{(now - min_age).date()} ({min_age.days} дн. до {now.date()})."
    )
    if exceptions:
        print(
            "Точные security-исключения: "
            + ", ".join(f"{name}=={version}" for name, version in exceptions.items())
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
