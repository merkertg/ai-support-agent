"""Проверить комментарии Python и исполнимый синтаксис примеров документации.

Скрипт использует только стандартную библиотеку. Bash-примеры передаются
в `bash -n`: команды из инструкций никогда не выполняются этой проверкой.
"""

import ast
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
LINK = re.compile(r"!?\[[^\]\n]*\]\((<[^>]+>|[^\s)]+)(?:\s+\"[^\"]*\")?\)")


def check_python(path: Path) -> list[str]:
    """Потребовать пояснение у каждой функции и класса, включая вложенные."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeError) as error:
        return [f"{path}: неверный Python: {error}"]
    return [
        f"{path}:{node.lineno}: нет docstring у {node.name}"
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not ast.get_docstring(node)
    ]


def check_link(path: Path, target: str, root: Path) -> str | None:
    """Проверить существование локальной ссылки, пропустив внешние адреса и якоря."""
    parsed = urlsplit(target.strip("<>"))
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    destination = (path.parent / unquote(parsed.path)).resolve()
    if not destination.is_relative_to(root.resolve()):
        return f"ссылка выходит за пределы проекта: {target}"
    if not destination.exists():
        return f"локальная ссылка не найдена: {target}"
    return None


def check_example(language: str, body: str) -> str | None:
    """Проверить Bash без исполнения и JSON без допуска синтаксических ошибок."""
    if language in {"bash", "sh", "shell"}:
        result = subprocess.run(
            ["bash", "-n"], input=body, text=True, capture_output=True, check=False
        )
        if result.returncode:
            return f"неверный Bash: {result.stderr.strip()}"
    elif language == "json":
        try:
            json.loads(body)
        except ValueError as error:
            return f"неверный JSON: {error}"
    return None


def check_markdown(path: Path, root: Path) -> tuple[list[str], Counter]:
    """Разобрать блоки кода и ссылки Markdown с учётом длины ограждения."""
    errors = []
    counts = Counter()
    opening: tuple[str, str, int] | None = None
    body = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        fence = FENCE.match(line)
        if opening is not None:
            delimiter, language, start = opening
            if (
                fence
                and fence[1][0] == delimiter[0]
                and len(fence[1]) >= len(delimiter)
                and not fence[2].strip()
            ):
                problem = check_example(language, "\n".join(body))
                if problem:
                    errors.append(f"{path}:{start}: {problem}")
                counts[f"{language or 'text'}_blocks"] += 1
                opening, body = None, []
            else:
                body.append(line)
            continue
        if fence:
            language = fence[2].strip().split(maxsplit=1)
            opening = (fence[1], language[0].lower() if language else "", line_number)
            continue
        # Содержимое inline code не является Markdown-ссылкой.
        prose = re.sub(r"`+[^`]*`+", "", line)
        for link in LINK.finditer(prose):
            counts["links"] += 1
            problem = check_link(path, link[1], root)
            if problem:
                errors.append(f"{path}:{line_number}: {problem}")
    if opening is not None:
        errors.append(f"{path}:{opening[2]}: незакрытый блок кода")
    return errors, counts


def inspect_project(root: Path) -> tuple[list[str], Counter]:
    """Собрать ошибки исходников и инструкций без запуска приложения или сети."""
    errors = []
    counts = Counter()
    for directory in ("app", "scripts", "migrations", "tests"):
        for path in sorted((root / directory).rglob("*.py")):
            if "__pycache__" not in path.parts:
                errors.extend(check_python(path))
                counts["python_files"] += 1
    markdown = sorted((root / "docs").rglob("*.md")) + [root / "README.md"]
    for path in markdown:
        problems, statistics = check_markdown(path, root)
        errors.extend(problems)
        counts.update(statistics)
        counts["markdown_files"] += 1
    return errors, counts


def main() -> int:
    """Показать все замечания сразу и вернуть код ошибки для GitHub Actions."""
    errors, counts = inspect_project(PROJECT_ROOT)
    print(json.dumps(dict(sorted(counts.items())), ensure_ascii=False))
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Комментарии, локальные ссылки и примеры Bash/JSON проверены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
