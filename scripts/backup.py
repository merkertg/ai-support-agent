"""Резервная копия и восстановление PostgreSQL и Qdrant Compose-проекта.

Подкоманды:

- `create` — `pg_dump` внутри контейнера `db` и snapshot коллекции Qdrant
  в новую папку `backups/<проект>-<время UTC>/` с `manifest.json`: версия Alembic,
  время, SHA256 и размер файлов, число строк каждой таблицы и точек Qdrant;
- `verify` — проверка папки без Docker: манифест, SHA256, размеры и формат файлов;
- `restore` — сначала копия, версия Alembic, остановленный API и пустота цели;
  затем `pg_restore` во временную БД, сверка строк, загрузка snapshot в Qdrant
  и только после этого замена рабочей БД. Непустую цель заменяет лишь `--force`.

Копия секретна: в ней клиенты, лицензии, переписка и SHA-256 токенов сотрудников.
Папка `backups/` исключена из Git и Docker-контекста; хранить копии нужно вне
репозитория, как описано в docs/instructions/backup.md. Скрипт использует только
стандартную библиотеку: CI запускает его системным `python3` без uv.
"""

import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = PROJECT_ROOT / "migrations" / "versions"
DEFAULT_OUTPUT = PROJECT_ROOT / "backups"
DEFAULT_PROJECT = "ai-support-agent"
DEFAULT_COLLECTION = "support_knowledge"
FORMAT = "ai-support-backup/1"
MANIFEST = "manifest.json"
POSTGRES_FILE = "postgres.dump"
QDRANT_FILE = "qdrant.snapshot"
# Сервисы Compose, которые пишут в PostgreSQL и Qdrant: restore требует их остановки.
WRITERS = ("api", "migrate")
# Временная БД восстановления; PostgreSQL ограничивает имя 63 байтами.
RESTORE_SUFFIX = "_restoring"
MAX_IDENTIFIER = 63
CHUNK = 1024 * 1024
HTTP_TIMEOUT = 300
# Полный путь из PATH; тесты подменяют кортеж на `python fake_docker.py`.
DOCKER: tuple[str, ...] = (shutil.which("docker") or "docker",)

# Одна строка JSON: таблица схемы public → точное число строк в ней.
TABLES_SQL = """
SELECT coalesce(json_object_agg(table_name, (xpath('/row/n/text()', query_to_xml(
    format('SELECT count(*) AS n FROM %I.%I', table_schema, table_name),
    false, true, '')))[1]::text::bigint ORDER BY table_name), '{}'::json)
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE';
"""


class BackupError(RuntimeError):
    """Понятная причина, по которой копия не создана, не проверена или не применена."""


@dataclass(frozen=True)
class DatabaseState:
    """Таблицы БД с числом строк и текущая ревизия Alembic (None — схемы нет)."""

    tables: dict[str, int]
    revision: str | None

    @property
    def rows(self) -> int:
        """Сумма строк всех таблиц."""
        return sum(self.tables.values())


def run(
    argv: list[str], *, stdin: Path | str | None = None, stdout: Path | None = None
) -> str:
    """Выполнить команду Docker и вернуть stdout; код ошибки → BackupError.

    stdin — файл (поток для pg_restore) или текст (SQL для psql). Если задан
    stdout, вывод пишется прямо в закрытый для других файл: дамп не проходит
    через память. SQL и данные передаются потоками, а не аргументами процесса.
    """
    target = private_file(stdout) if stdout is not None else subprocess.PIPE
    source: Any = subprocess.DEVNULL
    text = None
    try:
        if isinstance(stdin, Path):
            source = stdin.open("rb")
        elif isinstance(stdin, str):
            source, text = None, stdin.encode("utf-8")
        result = subprocess.run(
            argv,
            input=text,
            stdin=source,
            stdout=target,
            stderr=subprocess.PIPE,
            cwd=PROJECT_ROOT,
            check=False,
        )
    except FileNotFoundError:
        raise BackupError(
            "docker не найден: установи Docker Desktop или Docker Engine"
        ) from None
    finally:
        for stream in (source, target):
            if hasattr(stream, "close"):
                stream.close()
    if result.returncode:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        # Последние строки достаточно точно называют причину и не заполняют лог.
        detail = "\n".join(stderr.splitlines()[-5:]) or "без сообщения"
        shown = " ".join(argv[len(DOCKER) :][:8])
        raise BackupError(f"docker {shown} → код {result.returncode}: {detail}")
    return "" if stdout is not None else result.stdout.decode("utf-8")


def private_file(path: Path) -> BinaryIO:
    """Создать новый файл, доступный только владельцу (0600 на Linux и macOS).

    O_EXCL не даёт перезаписать существующий файл, O_BINARY отключает перевод
    строк Windows.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    return os.fdopen(os.open(path, flags, 0o600), "wb")


def sha256(path: Path) -> str:
    """SHA256 файла, прочитанного по частям: дамп может быть больше памяти."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Postgres:
    """PostgreSQL в контейнере: клиентские утилиты того же образа через docker exec.

    pg_dump, pg_restore и psql совпадают по версии с сервером, а соединение идёт
    через локальный сокет контейнера: пароль не нужен и нигде не передаётся.
    """

    prefix: tuple[str, ...]
    user: str
    database: str

    @classmethod
    def connect(
        cls, prefix: tuple[str, ...], database: str | None = None
    ) -> "Postgres":
        """Узнать пользователя и БД из POSTGRES_USER/POSTGRES_DB контейнера."""
        lines = run([*prefix, "printenv", "POSTGRES_USER", "POSTGRES_DB"]).splitlines()
        if len(lines) != 2 or not all(lines):
            raise BackupError("в контейнере PostgreSQL нет POSTGRES_USER и POSTGRES_DB")
        return cls(prefix, lines[0], database or lines[1])

    def psql(self, sql: str, database: str | None = None, **variables: str) -> str:
        """Выполнить SQL из stdin; переменные подставляются psql как :'имя'/:"имя"."""
        options = [arg for k, v in variables.items() for arg in ("-v", f"{k}={v}")]
        argv = [
            *self.prefix,
            "psql",
            "-X",
            "-q",
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            self.user,
            "-d",
            database or self.database,
            *options,
        ]
        return run(argv, stdin="SET client_min_messages = warning;\n" + sql)

    def server_version(self) -> int:
        """Номер версии сервера, например 170011 для PostgreSQL 17.11."""
        return int(self.psql("SHOW server_version_num;", "postgres").strip())

    def exists(self, database: str) -> bool:
        """Есть ли такая БД на сервере."""
        sql = "SELECT count(*) FROM pg_database WHERE datname = :'name';"
        return self.psql(sql, "postgres", name=database).strip() == "1"

    def state(self, database: str | None = None) -> DatabaseState:
        """Число строк каждой таблицы public и ревизия Alembic, если она есть."""
        try:
            tables = json.loads(self.psql(TABLES_SQL, database))
        except ValueError:
            raise BackupError("psql вернул неожиданный список таблиц") from None
        if not isinstance(tables, dict) or not all(
            type(count) is int and count >= 0 for count in tables.values()
        ):
            raise BackupError("psql вернул неожиданный список таблиц")
        revision = None
        if "alembic_version" in tables:
            versions = self.psql(
                "SELECT version_num FROM alembic_version;", database
            ).split()
            # История миграций проекта линейна: ровно одна текущая ревизия.
            if len(versions) != 1:
                raise BackupError(f"в alembic_version {len(versions)} ревизий вместо 1")
            revision = versions[0]
        return DatabaseState(tables, revision)

    def dump(self, path: Path) -> None:
        """Записать БД в файл custom-формата pg_dump (сжатый, для pg_restore)."""
        argv = [*self.prefix, "pg_dump", "-U", self.user, "-d", self.database]
        run([*argv, "--format=custom", "--no-password"], stdout=path)

    def recreate(self, database: str) -> None:
        """Создать пустую БД, удалив прежнюю с тем же именем и её соединения."""
        sql = 'DROP DATABASE IF EXISTS :"name" WITH (FORCE);\nCREATE DATABASE :"name";'
        self.psql(sql, "postgres", name=database)

    def drop(self, database: str) -> None:
        """Удалить БД, если она есть."""
        self.psql(
            'DROP DATABASE IF EXISTS :"name" WITH (FORCE);', "postgres", name=database
        )

    def restore(self, path: Path, database: str) -> None:
        """Применить дамп одной транзакцией: ошибка не оставит половину данных.

        --no-owner делает владельцем текущего пользователя: копия подходит стенду
        с другим POSTGRES_USER.
        """
        argv = [*self.prefix, "pg_restore", "-U", self.user, "-d", database]
        options = ["--no-owner", "--no-privileges", "--exit-on-error"]
        run([*argv, *options, "--single-transaction"], stdin=path)

    def replace(self, source: str, target: str) -> None:
        """Заменить target восстановленной БД source: удалить старую и переименовать."""
        sql = (
            'DROP DATABASE IF EXISTS :"target" WITH (FORCE);\n'
            'ALTER DATABASE :"source" RENAME TO :"target";'
        )
        self.psql(sql, "postgres", source=source, target=target)


class NoRedirect(HTTPRedirectHandler):
    """Не следовать перенаправлениям: Qdrant API отвечает напрямую."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Считать любой ответ 3xx ошибкой запроса."""
        return None


def reason(error: HTTPError) -> str:
    """Текст ошибки из JSON-ответа Qdrant или стандартная причина HTTP."""
    try:
        return str(json.loads(error.read())["status"]["error"])
    except (ValueError, KeyError, TypeError, OSError):
        return str(error.reason)


@dataclass(frozen=True)
class Qdrant:
    """Коллекция Qdrant через HTTP API: snapshot, загрузка и точный подсчёт точек."""

    url: str
    collection: str
    timeout: float = HTTP_TIMEOUT

    def at(self, suffix: str = "") -> str:
        """Путь API внутри коллекции, например /collections/<имя>/snapshots."""
        return f"/collections/{quote(self.collection, safe='')}{suffix}"

    def open(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ):
        """Отправить запрос и вернуть открытый ответ; HTTP-ошибка → BackupError.

        Адрес локальный, поэтому системный прокси не используется.
        """
        address = (
            self.url.rstrip("/") + path + (f"?{urlencode(query)}" if query else "")
        )
        if isinstance(body, dict):
            body = json.dumps(body).encode("utf-8")
            headers = {"Content-Type": "application/json", **(headers or {})}
        request = Request(address, data=body, headers=headers or {}, method=method)
        try:
            return build_opener(ProxyHandler({}), NoRedirect).open(
                request, timeout=self.timeout
            )
        except HTTPError as error:
            raise BackupError(
                f"Qdrant {method} {path}: HTTP {error.code}: {reason(error)}"
            ) from None
        except (URLError, OSError) as error:
            detail = getattr(error, "reason", error)
            raise BackupError(f"Qdrant недоступен по {self.url}: {detail}") from None

    def call(self, method: str, path: str, field: str = "result", **options) -> Any:
        """Выполнить запрос и вернуть поле JSON-ответа (обычно result)."""
        with self.open(method, path, **options) as response:
            try:
                return json.loads(response.read())[field]
            except (ValueError, KeyError, TypeError):
                raise BackupError(
                    f"Qdrant вернул неожиданный ответ на {method} {path}"
                ) from None

    def version(self) -> str:
        """Версия сервера Qdrant из корневого адреса API."""
        return str(self.call("GET", "/", field="version"))

    def exists(self) -> bool:
        """Есть ли коллекция."""
        return bool(self.call("GET", self.at("/exists"))["exists"])

    def count(self) -> int:
        """Точное число точек коллекции (points_count в описании приблизительный)."""
        result = self.call("POST", self.at("/points/count"), body={"exact": True})
        return int(result["count"])

    def snapshot(self, path: Path) -> None:
        """Создать snapshot, скачать его в файл и удалить с сервера Qdrant.

        Без удаления каждый запуск оставлял бы копию коллекции в контейнере.
        Контрольная сумма скачанного файла сверяется с SHA256 от Qdrant.
        """
        created = self.call("POST", self.at("/snapshots"), query={"wait": "true"})
        name = self.at(f"/snapshots/{quote(created['name'], safe='')}")
        try:
            digest = hashlib.sha256()
            with self.open("GET", name) as response, private_file(path) as file:
                while chunk := response.read(CHUNK):
                    digest.update(chunk)
                    file.write(chunk)
        except BaseException:
            # Исходная ошибка важнее: сбой уборки snapshot её не скрывает.
            try:
                self.call("DELETE", name, query={"wait": "true"})
            except BackupError:
                pass
            raise
        self.call("DELETE", name, query={"wait": "true"})
        expected = created.get("checksum")
        if expected and expected != digest.hexdigest():
            raise BackupError("SHA256 скачанного snapshot не совпал с ответом Qdrant")

    def upload(self, path: Path, checksum: str) -> None:
        """Восстановить коллекцию из файла: данные snapshot заменяют текущие.

        Тело multipart передаётся по частям; Qdrant сам сверяет checksum до
        восстановления и отклоняет повреждённый файл.
        """
        boundary = uuid.uuid4().hex
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="snapshot"; filename="{QDRANT_FILE}"'
            "\r\nContent-Type: application/octet-stream\r\n\r\n"
        ).encode()
        tail = f"\r\n--{boundary}--\r\n".encode()

        def chunks() -> Iterator[bytes]:
            """Заголовок части, содержимое файла по 1 МиБ и завершающая граница."""
            yield head
            with path.open("rb") as file:
                while chunk := file.read(CHUNK):
                    yield chunk
            yield tail

        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(head) + path.stat().st_size + len(tail)),
        }
        query = {"wait": "true", "priority": "snapshot", "checksum": checksum}
        self.call(
            "POST",
            self.at("/snapshots/upload"),
            query=query,
            body=chunks(),
            headers=headers,
        )

    def delete(self) -> None:
        """Удалить коллекцию целиком."""
        self.call("DELETE", self.at(), query={"wait": "true"})


@dataclass(frozen=True)
class Target:
    """Сервисы одного стенда: PostgreSQL, коллекция Qdrant и имя Compose-проекта.

    project равен None, если PostgreSQL выбран через --db-container: тогда
    Compose не участвует и проверку остановленного API выполняет человек.
    """

    postgres: Postgres
    qdrant: Qdrant
    project: str | None


def compose(project: str, *arguments: str) -> list[str]:
    """Команда docker compose для явно указанного проекта."""
    return [*DOCKER, "compose", "--project-name", project, *arguments]


def published_url(project: str, service: str, port: int) -> str:
    """Адрес опубликованного на компьютере порта сервиса Compose."""
    address = run(compose(project, "port", service, str(port))).strip()
    host, _, number = address.rpartition(":")
    if not number.isdigit():
        raise BackupError(f"сервис {service} не публикует порт {port}: {address!r}")
    # 0.0.0.0 и :: означают «все адреса»: подключаемся к локальному.
    if host in {"", "0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    return f"http://{host}:{number}"


def resolve_target(arguments: argparse.Namespace) -> Target:
    """Собрать Target из аргументов CLI: Compose-сервисы или явный контейнер."""
    project = None
    if arguments.db_container:
        prefix = (*DOCKER, "exec", "-i", arguments.db_container)
    else:
        project = arguments.project_name
        prefix = tuple(compose(project, "exec", "-T", arguments.db_service))
    url = arguments.qdrant_url
    if url is None:
        if project is None:
            raise BackupError("с --db-container укажи также --qdrant-url")
        url = published_url(project, arguments.qdrant_service, 6333)
    postgres = Postgres.connect(prefix, arguments.database)
    return Target(postgres, Qdrant(url, arguments.collection), project)


def running_services(project: str) -> set[str]:
    """Имена запущенных сервисов Compose-проекта."""
    output = run(compose(project, "ps", "--status", "running", "--services"))
    return set(output.split())


def revision_chain(directory: Path = MIGRATIONS) -> list[str]:
    """Ревизии Alembic проекта от первой к head, прочитанные из файлов миграций.

    Файлы разбираются ast без импорта Alembic и SQLAlchemy: скрипт работает
    системным python3. Ветвления история проекта не содержит.
    """
    parents: dict[str, object] = {}
    for path in sorted(directory.glob("*.py")):
        values = {}
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            elif isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            else:
                continue
            for name in targets:
                if isinstance(name, ast.Name) and name.id in {
                    "revision",
                    "down_revision",
                }:
                    values[name.id] = ast.literal_eval(value)
        if isinstance(values.get("revision"), str):
            parents[values["revision"]] = values.get("down_revision")
    children: dict[object, str] = {}
    for revision, parent in parents.items():
        if not (parent is None or isinstance(parent, str)) or parent in children:
            raise BackupError("история миграций нелинейна: restore её не поддерживает")
        children[parent] = revision
    chain, current = [], None
    while current in children:
        current = children[current]
        chain.append(current)
    if len(chain) != len(parents):
        raise BackupError("история миграций не образует одну цепочку от base до head")
    return chain


def load_manifest(path: Path) -> dict:
    """Прочитать и проверить структуру manifest.json папки копии."""
    try:
        manifest = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BackupError(f"{path}: нет {MANIFEST} — это не папка копии") from None
    except (ValueError, UnicodeError, OSError) as error:
        raise BackupError(f"{path / MANIFEST}: не читается как JSON: {error}") from None
    problem = manifest_problem(manifest)
    if problem:
        raise BackupError(f"{path / MANIFEST}: {problem}")
    return manifest


def manifest_problem(manifest: object) -> str | None:
    """Первое найденное нарушение формата манифеста или None."""
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        return f"неизвестный формат, ожидается {FORMAT}"
    postgres, qdrant, files = (
        manifest.get(key) for key in ("postgres", "qdrant", "files")
    )
    revision = manifest.get("alembic_revision")
    if not isinstance(revision, str) or not revision:
        return "нет версии Alembic"
    if not isinstance(manifest.get("created_at"), str):
        return "нет времени создания"
    if not isinstance(postgres, dict) or postgres.get("file") != POSTGRES_FILE:
        return "нет описания дампа PostgreSQL"
    tables = postgres.get("tables")
    if not isinstance(tables, dict) or not all(
        type(count) is int and count >= 0 for count in tables.values()
    ):
        return "неверное число строк таблиц"
    if type(postgres.get("server_version_num")) is not int:
        return "нет версии сервера PostgreSQL"
    if (
        not isinstance(qdrant, dict)
        or qdrant.get("file") not in {QDRANT_FILE, None}
        or not isinstance(qdrant.get("collection"), str)
        or type(qdrant.get("points")) is not int
        or qdrant["points"] < 0
    ):
        return "нет описания snapshot Qdrant"
    expected = {POSTGRES_FILE} | ({QDRANT_FILE} if qdrant["file"] else set())
    if not isinstance(files, dict) or set(files) != expected:
        return f"список файлов должен быть {sorted(expected)}"
    for name, meta in files.items():
        if (
            not isinstance(meta, dict)
            or not isinstance(meta.get("sha256"), str)
            or len(meta["sha256"]) != 64
            or type(meta.get("bytes")) is not int
            or meta["bytes"] < 0
        ):
            return f"неверные SHA256 или размер {name}"
    return None


def verify_backup(path: Path) -> dict:
    """Проверить копию без Docker и вернуть её манифест.

    Сверяются размер и SHA256 каждого файла, подпись PGDMP дампа custom-формата
    и структура tar-архива snapshot. Перечисляются все найденные проблемы.
    """
    manifest = load_manifest(path)
    problems = []
    for name, meta in manifest["files"].items():
        file = path / name
        if not file.is_file():
            problems.append(f"{name}: файл отсутствует")
            continue
        if file.stat().st_size != meta["bytes"]:
            problems.append(f"{name}: размер отличается от манифеста")
        elif sha256(file) != meta["sha256"]:
            problems.append(f"{name}: SHA256 не совпадает — файл изменён или повреждён")
    dump = path / POSTGRES_FILE
    if dump.is_file():
        with dump.open("rb") as file:
            if file.read(5) != b"PGDMP":
                problems.append(f"{POSTGRES_FILE}: это не дамп pg_dump custom-формата")
    snapshot = path / QDRANT_FILE
    if manifest["qdrant"]["file"] and snapshot.is_file():
        if not tarfile.is_tarfile(snapshot):
            problems.append(f"{QDRANT_FILE}: это не tar-архив snapshot Qdrant")
    if problems:
        raise BackupError(
            f"{path}: копия не прошла проверку:\n- " + "\n- ".join(problems)
        )
    return manifest


def summary(path: Path, manifest: dict) -> dict:
    """Краткий отчёт о копии без данных: только счётчики, версии и размеры."""
    tables = manifest["postgres"]["tables"]
    return {
        "path": str(path),
        "created_at": manifest["created_at"],
        "alembic_revision": manifest["alembic_revision"],
        "tables": len(tables),
        "rows": sum(tables.values()),
        "collection": manifest["qdrant"]["collection"],
        "points": manifest["qdrant"]["points"],
        "bytes": {
            name: meta["bytes"] for name, meta in sorted(manifest["files"].items())
        },
    }


def create_backup(
    target: Target, output_dir: Path = DEFAULT_OUTPUT, *, now: datetime | None = None
) -> dict:
    """Создать папку копии: дамп, snapshot и манифест.

    Файлы пишутся в скрытую папку .<имя>.partial и переименовываются только
    после успеха: прерванный запуск не оставит похожую на копию папку.
    Строки и точки считаются до и после копирования; расхождение значит, что
    данные менялись, и копия отклоняется.
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    name = f"{target.project or 'container'}-{moment:%Y%m%dT%H%M%SZ}"
    final, partial = output_dir / name, output_dir / f".{name}.partial"
    if final.exists() or partial.exists():
        raise BackupError(f"{final} уже существует")
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    partial.mkdir(mode=0o700)
    try:
        postgres, qdrant = target.postgres, target.qdrant
        before = postgres.state()
        if before.revision is None:
            raise BackupError(
                f"в БД {postgres.database} нет alembic_version: "
                "сначала примени миграции"
            )
        indexed = qdrant.exists()
        points = qdrant.count() if indexed else 0
        postgres.dump(partial / POSTGRES_FILE)
        if indexed:
            qdrant.snapshot(partial / QDRANT_FILE)
        if postgres.state() != before or (qdrant.count() if indexed else 0) != points:
            raise BackupError(
                "данные менялись во время копии: останови api (docker compose stop "
                "api ui) или повтори create, когда запись закончится"
            )
        files = [POSTGRES_FILE] + ([QDRANT_FILE] if indexed else [])
        manifest = {
            "format": FORMAT,
            "created_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "alembic_revision": before.revision,
            "source": {"project": target.project, "database": postgres.database},
            "postgres": {
                "file": POSTGRES_FILE,
                "server_version_num": postgres.server_version(),
                "tables": before.tables,
            },
            "qdrant": {
                "file": QDRANT_FILE if indexed else None,
                "collection": qdrant.collection,
                "points": points,
                "version": qdrant.version(),
            },
            "files": {
                file: {
                    "sha256": sha256(partial / file),
                    "bytes": (partial / file).stat().st_size,
                }
                for file in files
            },
        }
        with private_file(partial / MANIFEST) as file:
            file.write(json.dumps(manifest, ensure_ascii=False, indent=2).encode())
            file.write(b"\n")
        partial.rename(final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return summary(final, manifest)


def check_target(target: Target, manifest: dict, *, force: bool) -> list[str]:
    """Проверки до изменений: версия Alembic, остановленный API, версия и пустота цели.

    Возвращает описание данных, которые restore заменит (пусто — цель пуста).
    """
    chain = revision_chain()
    revision = manifest["alembic_revision"]
    if revision not in chain:
        raise BackupError(
            f"копия создана со схемой Alembic {revision}, которой нет в миграциях "
            f"этого кода (head {chain[-1] if chain else 'нет'}): восстанавливай "
            "кодом той версии, что создала копию, или новее"
        )
    if target.project is not None:
        writers = sorted(running_services(target.project) & set(WRITERS))
        if writers:
            raise BackupError(
                f"в проекте {target.project} запущены сервисы {', '.join(writers)}: "
                f"останови их (docker compose --project-name {target.project} "
                "stop api ui) и повтори restore"
            )
    postgres, qdrant = target.postgres, target.qdrant
    source = manifest["postgres"]["server_version_num"]
    current = postgres.server_version()
    if current // 10000 < source // 10000:
        raise BackupError(
            f"дамп PostgreSQL {source // 10000} нельзя восстановить "
            f"в более старый PostgreSQL {current // 10000}"
        )
    occupied = []
    if postgres.exists(postgres.database):
        state = postgres.state()
        if state.tables:
            occupied.append(
                f"PostgreSQL {postgres.database}: {len(state.tables)} таблиц, "
                f"{state.rows} строк, Alembic {state.revision or 'нет'}"
            )
    if qdrant.exists():
        occupied.append(f"Qdrant {qdrant.collection}: {qdrant.count()} точек")
    if occupied and not force:
        raise BackupError(
            "цель не пуста, restore заменил бы данные:\n- "
            + "\n- ".join(occupied)
            + "\nСначала создай свежую копию цели, затем повтори restore с --force"
        )
    return occupied


def restore_backup(path: Path, target: Target, *, force: bool = False) -> dict:
    """Восстановить копию в PostgreSQL и Qdrant цели.

    Дамп сначала применяется к временной БД <имя>_restoring и сверяется с
    манифестом по строкам и ревизии; затем загружается snapshot. Рабочая БД
    заменяется последней: любая ошибка до этого её не меняет.
    """
    manifest = verify_backup(path)
    replaced = check_target(target, manifest, force=force)
    postgres, qdrant = target.postgres, target.qdrant
    temporary = postgres.database + RESTORE_SUFFIX
    if len(temporary.encode()) > MAX_IDENTIFIER:
        raise BackupError(f"имя БД {postgres.database} слишком длинное для restore")
    expected = DatabaseState(
        manifest["postgres"]["tables"], manifest["alembic_revision"]
    )
    postgres.recreate(temporary)
    try:
        postgres.restore(path / POSTGRES_FILE, temporary)
        if postgres.state(temporary) != expected:
            raise BackupError("строки восстановленной БД не совпали с манифестом")
        if manifest["qdrant"]["file"]:
            qdrant.upload(path / QDRANT_FILE, manifest["files"][QDRANT_FILE]["sha256"])
            if qdrant.count() != manifest["qdrant"]["points"]:
                raise BackupError("число точек Qdrant не совпало с манифестом")
        elif qdrant.exists():
            # Копия сделана до первой индексации: коллекции в ней не было.
            qdrant.delete()
    except BaseException:
        try:
            postgres.drop(temporary)
        except BackupError:
            pass
        raise
    try:
        postgres.replace(temporary, postgres.database)
    except BackupError as error:
        raise BackupError(
            f"{error}\nВосстановленные данные остались в БД {temporary}: "
            f"переименуй её в {postgres.database} вручную"
        ) from None
    head = revision_chain()[-1]
    return {
        **summary(path, manifest),
        "database": postgres.database,
        "replaced": replaced,
        "head": head,
        # Старую схему поднимет сервис migrate при следующем up api.
        "migration_needed": manifest["alembic_revision"] != head,
    }


def use_utf8_output() -> None:
    """Печатать UTF-8 и в перенаправленный вывод Windows, где по умолчанию cp1252."""
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def add_target_options(parser: argparse.ArgumentParser) -> None:
    """Параметры выбора стенда, общие для create и restore."""
    parser.add_argument(
        "--project-name",
        default=os.environ.get("COMPOSE_PROJECT_NAME") or DEFAULT_PROJECT,
        help="Compose-проект (по умолчанию COMPOSE_PROJECT_NAME или ai-support-agent)",
    )
    parser.add_argument("--db-service", default="db", help="сервис PostgreSQL")
    parser.add_argument(
        "--db-container",
        help="контейнер PostgreSQL вместо Compose-сервиса (docker exec)",
    )
    parser.add_argument(
        "--database", help="имя БД (по умолчанию POSTGRES_DB контейнера)"
    )
    parser.add_argument("--qdrant-service", default="qdrant", help="сервис Qdrant")
    parser.add_argument(
        "--qdrant-url", help="адрес Qdrant (по умолчанию опубликованный порт сервиса)"
    )
    parser.add_argument(
        "--collection",
        default=os.environ.get("QDRANT_COLLECTION") or DEFAULT_COLLECTION,
        help="коллекция Qdrant (по умолчанию support_knowledge)",
    )


def build_parser() -> argparse.ArgumentParser:
    """Описание подкоманд create, verify и restore."""
    parser = argparse.ArgumentParser(
        description="Резервная копия PostgreSQL и Qdrant стенда AI Support Agent."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="создать копию работающего стенда")
    add_target_options(create)
    create.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="куда положить папку копии (по умолчанию backups/ проекта)",
    )
    verify = commands.add_parser("verify", help="проверить папку копии без Docker")
    verify.add_argument("path", type=Path)
    restore = commands.add_parser("restore", help="восстановить копию в стенд")
    restore.add_argument("path", type=Path)
    add_target_options(restore)
    restore.add_argument(
        "--force",
        action="store_true",
        help="заменить непустые PostgreSQL и Qdrant цели",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Выполнить подкоманду и напечатать JSON-отчёт; ошибка → код 1 и причина."""
    use_utf8_output()
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "verify":
            result = summary(arguments.path, verify_backup(arguments.path))
        elif arguments.command == "create":
            result = create_backup(resolve_target(arguments), arguments.output_dir)
        else:
            # Чужая папка отклоняется до первого обращения к Docker.
            load_manifest(arguments.path)
            target = resolve_target(arguments)
            result = restore_backup(arguments.path, target, force=arguments.force)
    except BackupError as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
