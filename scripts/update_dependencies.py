"""Подготовить обновление Python-зависимостей и открыть одну PR без автослияния.

Подготовка работает без токена. Публикация принимает только два заранее
разрешённых файла; доверенный скрипт выполняется из main, а не из ветки PR.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

FILES = ("pyproject.toml", "uv.lock")
BRANCH = "automation/python-dependencies"
CUTOFF = re.compile(r'(?m)^exclude-newer[ \t]*=[ \t]*"[^"\n]+"[ \t]*$')
SHA = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9_.-]*")
MAX_RESPONSE_BYTES = 2_000_000


def cutoff_value(now: datetime) -> str:
    """Выбрать полночь UTC не ближе четырнадцати полных дней к текущему моменту."""
    if now.tzinfo is None:
        raise ValueError("текущее время должно содержать часовой пояс")
    cutoff = (now.astimezone(UTC) - timedelta(days=14)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


def update_cutoff(source: str, now: datetime) -> str:
    """Изменить только единственную границу [tool.uv], сохранив комментарии."""
    document = tomllib.loads(source)
    previous = document.get("tool", {}).get("uv", {}).get("exclude-newer")
    if not isinstance(previous, str) or len(CUTOFF.findall(source)) != 1:
        raise ValueError("нужна единственная строковая настройка exclude-newer")
    replacement = cutoff_value(now)
    previous_time = datetime.fromisoformat(previous.replace("Z", "+00:00"))
    if previous_time.tzinfo is None:
        raise ValueError("существующая граница должна содержать часовой пояс")
    if previous_time > now - timedelta(days=14):
        raise ValueError("существующая граница нарушает политику четырнадцати дней")
    return CUTOFF.sub(f'exclude-newer = "{replacement}"', source)


def prepare(root: Path, uv: str, now: datetime) -> None:
    """Сдвинуть дату, обновить lock без сборки пакетов и проверить возраст версий."""
    project = root / "pyproject.toml"
    project.write_text(
        update_cutoff(project.read_text(encoding="utf-8"), now), encoding="utf-8"
    )
    # --no-build запрещает запуск setup.py/PEP 517 backend из загруженного пакета.
    subprocess.run([uv, "lock", "--upgrade", "--no-build"], cwd=root, check=True)
    subprocess.run(
        [
            sys.executable,
            "scripts/check_dependency_age.py",
            "--min-age-days",
            "14",
            "--now",
            now.isoformat(),
        ],
        cwd=root,
        check=True,
    )


def validated_files(root: Path, artifact: Path, now: datetime) -> dict[str, str]:
    """Разрешить только lock и изменение даты pyproject, отклонив посторонние файлы."""
    if {path.name for path in artifact.iterdir()} != set(FILES):
        raise ValueError("артефакт должен содержать только pyproject.toml и uv.lock")
    files = {}
    for name in FILES:
        path = artifact / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 10_000_000:
            raise ValueError(f"недопустимый файл артефакта: {name}")
        files[name] = path.read_text(encoding="utf-8")
        tomllib.loads(files[name])
    original = (root / "pyproject.toml").read_text(encoding="utf-8")
    if files["pyproject.toml"] != update_cutoff(original, now):
        raise ValueError("в pyproject.toml разрешено менять только дату exclude-newer")
    lock = tomllib.loads(files["uv.lock"])
    for package in lock.get("package", []):
        source = package.get("source", {})
        if source not in ({"registry": "https://pypi.org/simple"}, {"virtual": "."}):
            raise ValueError("lock содержит незапланированный источник зависимостей")
    subprocess.run(
        [
            sys.executable,
            "scripts/check_dependency_age.py",
            "--lock",
            str(artifact / "uv.lock"),
            "--pyproject",
            str(artifact / "pyproject.toml"),
            "--min-age-days",
            "14",
            "--now",
            now.isoformat(),
        ],
        cwd=root,
        check=True,
    )
    return files


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Отклонять редирект, чтобы Authorization не ушёл на другой адрес."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        """Не создавать новый HTTP-запрос даже при ответе доверенного API."""
        return None


class GitHub:
    """Отправлять ограниченные JSON-запросы GitHub API без вывода токена и ответов."""

    def __init__(self, repository: str, token: str):
        """Зафиксировать репозиторий и токен только для текущего процесса writer."""
        if not REPOSITORY.fullmatch(repository) or not token:
            raise ValueError("нужны корректный GITHUB_REPOSITORY и GH_TOKEN")
        self.repository = repository
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, method: str, suffix: str, data: dict | None = None):
        """Вызвать API репозитория, преобразовав отсутствие ветки в None."""
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.repository}/{suffix}",
            data=None if data is None else json.dumps(data).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ai-support-dependency-updater",
            },
            method=method,
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise RuntimeError("GitHub API: ответ превышает 2 MB")
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as error:
            if error.code == 404 and method == "GET" and suffix.startswith("git/ref/"):
                return None
            raise RuntimeError(
                f"GitHub API: HTTP {error.code} ({method}); проверьте права Actions"
            ) from None


def publish(
    root: Path, artifact: Path, base_sha: str, now: datetime, api: GitHub
) -> None:
    """Обновить зарезервированную ветку, одну PR и явно запустить обычный CI."""
    if not SHA.fullmatch(base_sha):
        raise ValueError("нужен полный SHA доверенной main")
    files = validated_files(root, artifact, now)
    main = api.request("GET", "git/ref/heads/main")
    if main is None or main["object"]["sha"] != base_sha:
        raise ValueError("main изменилась после подготовки; повторите workflow")
    if all(
        content == (root / name).read_text(encoding="utf-8")
        for name, content in files.items()
    ):
        print("Обновлений нет; PR и запуск CI не нужны.")
        return
    base = api.request("GET", f"git/commits/{base_sha}")
    tree = api.request(
        "POST",
        "git/trees",
        {
            "base_tree": base["tree"]["sha"],
            "tree": [
                {"path": name, "mode": "100644", "type": "blob", "content": content}
                for name, content in files.items()
            ],
        },
    )
    commit = api.request(
        "POST",
        "git/commits",
        {
            "message": "Update Python dependencies after 14-day release window",
            "tree": tree["sha"],
            "parents": [base_sha],
        },
    )
    existing = api.request("GET", f"git/ref/heads/{BRANCH}")
    if existing is None:
        api.request(
            "POST", "git/refs", {"ref": f"refs/heads/{BRANCH}", "sha": commit["sha"]}
        )
    else:
        # Эта ветка принадлежит автоматике: main здесь никогда не перезаписывается.
        api.request(
            "PATCH", f"git/refs/heads/{BRANCH}", {"sha": commit["sha"], "force": True}
        )
    owner = api.repository.split("/", 1)[0]
    pulls = api.request("GET", f"pulls?state=open&base=main&head={owner}:{BRANCH}")
    description = {
        "title": "Обновить Python-зависимости после 14 дней на PyPI",
        "body": (
            "Обновлены только pyproject.toml и uv.lock. Пакеты опубликованы "
            "не менее 14 дней назад. Требуются полный CI, аудит и проверка "
            "контейнеров; автоматическое слияние выключено.\n\n"
            "CI and GHCR запускается отдельно через workflow_dispatch для "
            "этой ветки. GitHub также может запросить ручное одобрение "
            "pull_request-проверок.\n\nИнструкция: docs/DEPENDENCY_UPDATES.md."
        ),
    }
    if pulls:
        api.request("PATCH", f"pulls/{pulls[0]['number']}", description)
    else:
        api.request("POST", "pulls", description | {"base": "main", "head": BRANCH})
    # workflow_dispatch запускается и с GITHUB_TOKEN, без PAT/GitHub App.
    api.request("POST", "actions/workflows/ci.yml/dispatches", {"ref": BRANCH})
    print(f"PR подготовлена в {BRANCH}; CI запущен без публикации Docker-образов.")


def use_utf8_output() -> None:
    """Печатать UTF-8 и при перенаправлении вывода в Windows, где по умолчанию cp1252.

    Консоль Windows и так получает Unicode; меняется только поток в файл или pipe.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Выбрать подготовку либо публикацию и выдать безопасную причину отказа."""
    use_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["prepare", "publish"])
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--base-sha")
    parser.add_argument("--now", default=None)
    args = parser.parse_args(argv)
    try:
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(UTC)
        if args.mode == "prepare":
            prepare(args.root, args.uv, now)
        else:
            if args.artifact is None or args.base_sha is None:
                parser.error("publish требует --artifact и --base-sha")
            api = GitHub(
                os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GH_TOKEN", "")
            )
            publish(args.root, args.artifact.resolve(), args.base_sha, now, api)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Dependency update: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
