"""Проверить резервное копирование без Docker: подменённые контейнеры и Qdrant.

Команды Docker перехватывает FakeDocker — PostgreSQL в памяти, понимающий
printenv, psql, pg_dump и pg_restore. Qdrant — настоящий локальный HTTP-сервер
с теми же маршрутами snapshot API. Сама функция run проверяется отдельными
дочерними процессами Python.
"""

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from scripts import backup

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backup.py"
HEAD = "0005_api_tokens"
NOW = datetime(2026, 10, 19, 5, 30, tzinfo=UTC)
TABLES = {"alembic_version": 1, "customers": 3, "tickets": 5, "api_tokens": 2}
POINTS = [
    {"id": 1, "vector": [0.1, 0.2], "payload": {"document_key": "KB-SERVER-START"}},
    {"id": 2, "vector": [0.3, 0.4], "payload": {"document_key": "KB-DESKTOP-SYNC"}},
]


class FakeDocker:
    """Контейнер PostgreSQL в памяти вместо docker exec и docker compose.

    Каждая БД — словарь {"tables": ..., "revision": ...}. pg_dump пишет её
    в файл после подписи PGDMP, pg_restore читает обратно. hooks позволяют
    изменить состояние или вызвать ошибку на конкретной команде.
    """

    def __init__(self, qdrant_port: int = 6335) -> None:
        """Стенд с одной рабочей БД ai_support на актуальной ревизии."""
        self.databases = {
            "postgres": {"tables": {}, "revision": None},
            "ai_support": {"tables": dict(TABLES), "revision": HEAD},
        }
        self.version = 170011
        self.running = {"db", "qdrant"}
        self.qdrant_port = qdrant_port
        self.port_output: str | None = None
        self.calls: list[list[str]] = []
        self.hooks: dict[str, object] = {}

    def __call__(self, argv, *, stdin=None, stdout=None) -> str:
        """Выполнить команду так, как её выполнил бы Docker."""
        self.calls.append(list(argv))
        if "exec" not in argv:
            if "port" in argv:
                return self.port_output or f"127.0.0.1:{self.qdrant_port}\n"
            if "ps" in argv:
                return "".join(f"{name}\n" for name in sorted(self.running))
            raise AssertionError(f"неожиданная команда {argv}")
        command = argv[argv.index("exec") + 3 :]
        name = command[0]
        hook = self.hooks.get(name)
        if callable(hook):
            hook(command, stdin)
        if name == "printenv":
            return "support\nai_support\n"
        database = command[command.index("-d") + 1]
        if name == "pg_dump":
            data = json.dumps(self.databases[database]).encode()
            # Как настоящий run: файл дампа доступен только владельцу.
            with backup.private_file(Path(stdout)) as file:
                file.write(b"PGDMP" + data)
            return ""
        if name == "pg_restore":
            if self.databases[database]["tables"]:
                raise backup.BackupError("relation already exists")
            content = Path(stdin).read_bytes()
            self.databases[database] = json.loads(content[5:])
            return ""
        assert name == "psql"
        variables = dict(
            command[index + 1].split("=", 1)
            for index, item in enumerate(command)
            if item == "-v" and not command[index + 1].startswith("ON_ERROR_STOP")
        )
        return self.psql(stdin, database, variables)

    def psql(self, sql: str, database: str, variables: dict[str, str]) -> str:
        """Ответить на запросы, которые отправляет scripts/backup.py."""
        assert sql.startswith("SET client_min_messages = warning;")
        if "server_version_num" in sql:
            return f"{self.version}\n"
        if "pg_database" in sql:
            return "1\n" if variables["name"] in self.databases else "0\n"
        if "information_schema.tables" in sql:
            return json.dumps(self.databases[database]["tables"]) + "\n"
        if "alembic_version" in sql:
            return f"{self.databases[database]['revision']}\n"
        if "RENAME" in sql:
            self.databases.pop(variables["target"], None)
            source = self.databases.pop(variables["source"])
            self.databases[variables["target"]] = source
            return ""
        if "CREATE DATABASE" in sql:
            self.databases[variables["name"]] = {"tables": {}, "revision": None}
            return ""
        if "DROP DATABASE" in sql:
            self.databases.pop(variables["name"], None)
            return ""
        raise AssertionError(f"неожиданный SQL {sql}")


def snapshot_bytes(points: list[dict]) -> bytes:
    """Tar-архив, как snapshot Qdrant, с точками коллекции внутри."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        data = json.dumps(points).encode()
        info = tarfile.TarInfo("points.json")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class FakeQdrant:
    """Локальный HTTP-сервер с маршрутами коллекций и snapshot Qdrant 1.19."""

    def __init__(self) -> None:
        """Одна коллекция support_knowledge с двумя точками."""
        self.collections = {"support_knowledge": [dict(point) for point in POINTS]}
        self.snapshots: dict[str, bytes] = {}
        self.requests: list[tuple[str, str]] = []
        self.wrong_checksum = False
        self.failures: dict[tuple[str, str], int] = {}
        self.after_snapshot = None
        fake = self

        class Handler(BaseHTTPRequestHandler):
            """Обработать запрос клиента scripts/backup.py."""

            def log_message(self, *args) -> None:
                """Не печатать журнал запросов в вывод Pytest."""

            def do_GET(self) -> None:
                """Чтение версии, существования коллекции и файла snapshot."""
                fake.handle(self, "GET")

            def do_POST(self) -> None:
                """Подсчёт точек, создание и загрузка snapshot."""
                fake.handle(self, "POST")

            def do_DELETE(self) -> None:
                """Удаление snapshot или коллекции."""
                fake.handle(self, "DELETE")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # Короткий интервал опроса: shutdown в конце теста не ждёт 0.5 с.
        self.thread = threading.Thread(
            target=self.server.serve_forever, args=(0.02,), daemon=True
        )
        self.thread.start()

    @property
    def url(self) -> str:
        """Адрес запущенного сервера."""
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        """Остановить сервер после теста."""
        self.server.shutdown()
        self.server.server_close()

    def reply(self, handler, status: int, payload: object) -> None:
        """Отправить JSON-ответ."""
        body = json.dumps(payload).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def handle(self, handler, method: str) -> None:
        """Разобрать путь и выполнить операцию, как Qdrant."""
        parts = urlsplit(handler.path)
        path, query = unquote(parts.path), parse_qs(parts.query)
        self.requests.append((method, path))
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else b""
        for (fail_method, prefix), status in self.failures.items():
            if method == fail_method and path.startswith(prefix):
                error = {"status": {"error": "Service internal error"}}
                return self.reply(handler, status, error)
        if path == "/":
            return self.reply(handler, 200, {"title": "qdrant", "version": "1.19.1"})
        match = re.fullmatch(r"/collections/([^/]+)(/.*)?", path)
        assert match, path
        name, rest = match[1], match[2] or ""
        points = self.collections.get(name)
        if rest == "/exists":
            return self.reply(handler, 200, {"result": {"exists": points is not None}})
        if points is None and not rest.startswith("/snapshots/upload"):
            error = {"status": {"error": f"Collection `{name}` doesn't exist!"}}
            return self.reply(handler, 404, error)
        if rest == "/points/count":
            assert json.loads(body) == {"exact": True}
            return self.reply(handler, 200, {"result": {"count": len(points)}})
        if rest == "/snapshots" and method == "POST":
            data = snapshot_bytes(points)
            snapshot = f"{name}-1-2026-10-19.snapshot"
            self.snapshots[snapshot] = data
            checksum = hashlib.sha256(data).hexdigest()
            if self.wrong_checksum:
                checksum = "0" * 64
            if self.after_snapshot:
                self.after_snapshot()
            result = {"name": snapshot, "size": len(data), "checksum": checksum}
            return self.reply(handler, 200, {"result": result})
        if rest.startswith("/snapshots/") and rest != "/snapshots/upload":
            snapshot = rest.removeprefix("/snapshots/")
            if method == "DELETE":
                self.snapshots.pop(snapshot)
                return self.reply(handler, 200, {"result": True})
            data = self.snapshots[snapshot]
            handler.send_response(200)
            handler.send_header("Content-Type", "application/octet-stream")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
            return None
        if rest == "/snapshots/upload":
            assert query["priority"] == ["snapshot"] and query["wait"] == ["true"]
            boundary = handler.headers["Content-Type"].split("boundary=")[1].encode()
            start = body.index(b"\r\n\r\n") + 4
            data = body[start : body.rindex(b"\r\n--" + boundary + b"--")]
            if hashlib.sha256(data).hexdigest() != query["checksum"][0]:
                error = {"status": {"error": "Checksum mismatch"}}
                return self.reply(handler, 400, error)
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                restored = json.load(archive.extractfile("points.json"))
            self.collections[name] = restored
            return self.reply(handler, 200, {"result": True})
        if rest == "" and method == "DELETE":
            del self.collections[name]
            return self.reply(handler, 200, {"result": True})
        raise AssertionError(f"{method} {path}")


@pytest.fixture
def qdrant() -> Iterator[FakeQdrant]:
    """Фиктивный Qdrant на свободном порту."""
    server = FakeQdrant()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch, qdrant: FakeQdrant) -> FakeDocker:
    """Перехватить все команды Docker скрипта."""
    fake = FakeDocker(qdrant.server.server_port)
    monkeypatch.setattr(backup, "run", fake)
    return fake


def target(qdrant: FakeQdrant, project: str | None = "ai-support-ci") -> backup.Target:
    """Цель через настоящий Postgres.connect, выполняемый подменённым Docker."""
    prefix = ("docker", "compose", "--project-name", "p", "exec", "-T", "db")
    postgres = backup.Postgres.connect(prefix)
    return backup.Target(
        postgres, backup.Qdrant(qdrant.url, "support_knowledge"), project
    )


def make_backup(docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path) -> Path:
    """Создать копию стенда и вернуть путь к её папке."""
    result = backup.create_backup(target(qdrant), tmp_path / "backups", now=NOW)
    return Path(result["path"])


def clear_target(docker: FakeDocker, qdrant: FakeQdrant) -> None:
    """Имитировать чистые тома: пустая БД и нет коллекции."""
    docker.databases["ai_support"] = {"tables": {}, "revision": None}
    qdrant.collections.clear()


def rewrite_manifest(path: Path, change) -> None:
    """Изменить manifest.json копии функцией change."""
    manifest = json.loads((path / backup.MANIFEST).read_text(encoding="utf-8"))
    change(manifest)
    (path / backup.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")


def test_revision_chain_reads_project_migrations_in_order() -> None:
    """Цепочка Alembic берётся из файлов migrations/versions от первой до head."""
    chain = backup.revision_chain()
    assert chain[0] == "0001_stage02_schema"
    assert chain[-1] == HEAD
    assert len(chain) == len(set(chain)) == 5


def write_migration(folder: Path, revision: str, parent: object) -> None:
    """Файл миграции с ревизией и родителем в формате Alembic."""
    text = f"revision: str = {revision!r}\ndown_revision = {parent!r}\nx = 1\n"
    (folder / f"{revision}.py").write_text(text, encoding="utf-8")


def test_revision_chain_rejects_branches_merges_and_gaps(tmp_path: Path) -> None:
    """Ветвление, слияние и разрыв цепочки останавливают restore до изменений."""
    write_migration(tmp_path, "a", None)
    write_migration(tmp_path, "b", "a")
    assert backup.revision_chain(tmp_path) == ["a", "b"]
    write_migration(tmp_path, "c", "a")
    with pytest.raises(backup.BackupError, match="нелинейна"):
        backup.revision_chain(tmp_path)
    (tmp_path / "c.py").write_text("revision = 'c'\ndown_revision = ('a', 'b')\n")
    with pytest.raises(backup.BackupError, match="нелинейна"):
        backup.revision_chain(tmp_path)
    write_migration(tmp_path, "c", "missing")
    with pytest.raises(backup.BackupError, match="одну цепочку"):
        backup.revision_chain(tmp_path)


def test_create_writes_private_backup_with_manifest(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Копия: дамп, snapshot и манифест с Alembic, временем, SHA256 и строками."""
    result = backup.create_backup(target(qdrant), tmp_path / "backups", now=NOW)
    path = Path(result["path"])
    assert path == tmp_path / "backups" / "ai-support-ci-20261019T053000Z"
    assert sorted(item.name for item in path.iterdir()) == [
        "manifest.json",
        "postgres.dump",
        "qdrant.snapshot",
    ]
    assert [item.name for item in path.parent.iterdir()] == [path.name]
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "ai-support-backup/1"
    assert manifest["created_at"] == "2026-10-19T05:30:00Z"
    assert manifest["alembic_revision"] == HEAD
    assert manifest["source"] == {"project": "ai-support-ci", "database": "ai_support"}
    assert manifest["postgres"]["tables"] == TABLES
    assert manifest["postgres"]["server_version_num"] == 170011
    assert manifest["qdrant"] == {
        "file": "qdrant.snapshot",
        "collection": "support_knowledge",
        "points": 2,
        "version": "1.19.1",
    }
    for name, meta in manifest["files"].items():
        data = (path / name).read_bytes()
        assert meta == {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    assert result["rows"] == sum(TABLES.values()) and result["tables"] == 4
    assert result["points"] == 2
    # Snapshot скачан и удалён с сервера: в контейнере Qdrant копий не остаётся.
    assert qdrant.snapshots == {}
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o700
        assert {(item.stat().st_mode & 0o777) for item in path.iterdir()} == {0o600}


def test_create_without_collection_records_missing_index(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """До первой индексации коллекции нет: в копии только дамп PostgreSQL."""
    qdrant.collections.clear()
    path = make_backup(docker, qdrant, tmp_path)
    manifest = backup.verify_backup(path)
    assert manifest["qdrant"]["file"] is None and manifest["qdrant"]["points"] == 0
    assert set(manifest["files"]) == {"postgres.dump"}
    assert not (path / "qdrant.snapshot").exists()


def test_create_requires_migrated_database(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """БД без alembic_version не копируется, папка копии не остаётся."""
    docker.databases["ai_support"] = {"tables": {"notes": 1}, "revision": None}
    with pytest.raises(backup.BackupError, match="нет alembic_version"):
        make_backup(docker, qdrant, tmp_path)
    assert list((tmp_path / "backups").iterdir()) == []


def test_create_rejects_rows_changed_during_dump(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Новая строка во время pg_dump делает копию несогласованной: она удаляется."""

    def add_ticket(command, stdin) -> None:
        """Сотрудник создал обращение, пока шёл дамп."""
        docker.databases["ai_support"]["tables"]["tickets"] += 1

    docker.hooks["pg_dump"] = add_ticket
    with pytest.raises(backup.BackupError, match="данные менялись"):
        make_backup(docker, qdrant, tmp_path)
    assert list((tmp_path / "backups").iterdir()) == []


def test_create_rejects_points_changed_during_snapshot(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Индексация во время snapshot меняет число точек: копия отклоняется."""
    qdrant.after_snapshot = lambda: qdrant.collections["support_knowledge"].append(
        {"id": 3, "vector": [0.5, 0.6], "payload": {}}
    )
    with pytest.raises(backup.BackupError, match="данные менялись"):
        make_backup(docker, qdrant, tmp_path)
    assert list((tmp_path / "backups").iterdir()) == []


def test_create_rejects_snapshot_with_wrong_checksum(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Скачанный snapshot сверяется с SHA256 от Qdrant."""
    qdrant.wrong_checksum = True
    with pytest.raises(backup.BackupError, match="SHA256 скачанного snapshot"):
        make_backup(docker, qdrant, tmp_path)
    assert qdrant.snapshots == {}
    assert list((tmp_path / "backups").iterdir()) == []


def test_create_removes_server_snapshot_when_download_fails(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Ошибка скачивания не оставляет snapshot на сервере и папку копии."""
    qdrant.failures[("GET", "/collections/support_knowledge/snapshots/")] = 500
    with pytest.raises(backup.BackupError, match="HTTP 500: Service internal error"):
        make_backup(docker, qdrant, tmp_path)
    assert qdrant.snapshots == {}
    assert list((tmp_path / "backups").iterdir()) == []


def test_create_keeps_original_error_when_cleanup_also_fails(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Сбой удаления snapshot после ошибки скачивания не скрывает исходную причину."""
    qdrant.failures[("GET", "/collections/support_knowledge/snapshots/")] = 503
    qdrant.failures[("DELETE", "/collections/support_knowledge/snapshots/")] = 500
    with pytest.raises(backup.BackupError, match="HTTP 503"):
        make_backup(docker, qdrant, tmp_path)


def test_create_refuses_to_overwrite_existing_backup(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Папка с тем же временем не перезаписывается."""
    make_backup(docker, qdrant, tmp_path)
    with pytest.raises(backup.BackupError, match="уже существует"):
        make_backup(docker, qdrant, tmp_path)


def test_create_cleans_partial_folder_on_interrupt(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Ctrl+C во время дампа не оставляет незавершённую папку."""

    def interrupt(command, stdin) -> None:
        """Пользователь прервал команду."""
        raise KeyboardInterrupt

    docker.hooks["pg_dump"] = interrupt
    with pytest.raises(KeyboardInterrupt):
        make_backup(docker, qdrant, tmp_path)
    assert list((tmp_path / "backups").iterdir()) == []


def test_verify_accepts_intact_backup(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Неизменённая копия проходит офлайн-проверку."""
    path = make_backup(docker, qdrant, tmp_path)
    assert backup.verify_backup(path)["alembic_revision"] == HEAD


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (lambda p: (p / "postgres.dump").write_bytes(b"PGDMX"), "размер"),
        (lambda p: (p / "qdrant.snapshot").unlink(), "qdrant.snapshot: файл"),
        (
            lambda p: (p / "postgres.dump").write_bytes(
                b"PGDMQ" + (p / "postgres.dump").read_bytes()[5:]
            ),
            "SHA256 не совпадает",
        ),
    ],
)
def test_verify_reports_damaged_files(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path, damage, message
) -> None:
    """Изменённый, обрезанный или удалённый файл обнаруживается по манифесту."""
    path = make_backup(docker, qdrant, tmp_path)
    damage(path)
    with pytest.raises(backup.BackupError, match=message):
        backup.verify_backup(path)


def test_verify_checks_file_formats(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Подпись PGDMP и tar-архив проверяются даже при совпавших SHA256."""
    path = make_backup(docker, qdrant, tmp_path)
    (path / "postgres.dump").write_bytes(b"not a dump")
    (path / "qdrant.snapshot").write_bytes(b"not a tar archive")

    def trust_files(manifest: dict) -> None:
        """Записать в манифест суммы подменённых файлов."""
        for name in manifest["files"]:
            data = (path / name).read_bytes()
            manifest["files"][name] = {
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }

    rewrite_manifest(path, trust_files)
    with pytest.raises(backup.BackupError) as error:
        backup.verify_backup(path)
    assert "custom-формата" in str(error.value) and "tar-архив" in str(error.value)


def test_verify_rejects_folder_without_manifest(tmp_path: Path) -> None:
    """Произвольная папка не считается копией."""
    with pytest.raises(backup.BackupError, match="нет manifest.json"):
        backup.verify_backup(tmp_path)
    (tmp_path / "manifest.json").write_text("{", encoding="utf-8")
    with pytest.raises(backup.BackupError, match="не читается как JSON"):
        backup.verify_backup(tmp_path)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda m: m.update(format="other/1"), "неизвестный формат"),
        (lambda m: m.update(alembic_revision=""), "нет версии Alembic"),
        (lambda m: m.pop("created_at"), "нет времени создания"),
        (lambda m: m["postgres"].update(file="x.sql"), "нет описания дампа"),
        (lambda m: m["postgres"]["tables"].update(tickets=-1), "число строк"),
        (lambda m: m["postgres"]["tables"].update(tickets=True), "число строк"),
        (lambda m: m["postgres"].pop("server_version_num"), "версии сервера"),
        (lambda m: m["qdrant"].update(points=-1), "snapshot Qdrant"),
        (lambda m: m["qdrant"].update(file="../x"), "snapshot Qdrant"),
        (lambda m: m["files"].pop("qdrant.snapshot"), "список файлов"),
        (lambda m: m["files"].update(extra={}), "список файлов"),
        (lambda m: m["files"]["postgres.dump"].update(sha256="ab"), "SHA256 или"),
        (lambda m: m["files"]["postgres.dump"].update(bytes=-1), "SHA256 или"),
    ],
)
def test_verify_rejects_malformed_manifest(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path, change, message
) -> None:
    """Неверная структура манифеста называет первое нарушение."""
    path = make_backup(docker, qdrant, tmp_path)
    rewrite_manifest(path, change)
    with pytest.raises(backup.BackupError, match=message):
        backup.verify_backup(path)


def test_restore_into_clean_target_recovers_rows_and_points(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """В пустые тома копия восстанавливается без --force."""
    path = make_backup(docker, qdrant, tmp_path)
    clear_target(docker, qdrant)
    result = backup.restore_backup(path, target(qdrant))
    assert docker.databases["ai_support"] == {"tables": TABLES, "revision": HEAD}
    assert "ai_support_restoring" not in docker.databases
    assert qdrant.collections["support_knowledge"] == POINTS
    assert result["replaced"] == [] and result["migration_needed"] is False
    assert result["database"] == "ai_support" and result["head"] == HEAD
    # Дамп загружается во временную БД, а рабочая заменяется последней.
    commands = [call[call.index("exec") + 3] for call in docker.calls if "exec" in call]
    restore_index = commands.index("pg_restore")
    assert commands[restore_index + 1 :].count("psql") >= 2
    assert qdrant.requests[-2][1].endswith("/snapshots/upload")


def test_restore_refuses_non_empty_target_without_force(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Непустые БД и коллекция без --force не меняются и описываются в ошибке."""
    path = make_backup(docker, qdrant, tmp_path)
    docker.databases["ai_support"]["tables"]["tickets"] = 9
    with pytest.raises(backup.BackupError) as error:
        backup.restore_backup(path, target(qdrant))
    message = str(error.value)
    assert "PostgreSQL ai_support: 4 таблиц, 15 строк, Alembic 0005" in message
    assert "Qdrant support_knowledge: 2 точек" in message and "--force" in message
    assert docker.databases["ai_support"]["tables"]["tickets"] == 9
    assert not any("pg_restore" in call for call in docker.calls)


def test_restore_with_force_replaces_changed_data(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """С --force изменённые после копии строки и точки заменяются копией."""
    path = make_backup(docker, qdrant, tmp_path)
    docker.databases["ai_support"]["tables"]["tickets"] = 9
    qdrant.collections["support_knowledge"].append({"id": 9, "vector": [1, 1]})
    result = backup.restore_backup(path, target(qdrant), force=True)
    assert docker.databases["ai_support"]["tables"] == TABLES
    assert qdrant.collections["support_knowledge"] == POINTS
    assert len(result["replaced"]) == 2


def test_restore_checks_alembic_revision_before_changes(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Копия неизвестной (более новой) схемы отклоняется до первой команды Docker."""
    path = make_backup(docker, qdrant, tmp_path)
    rewrite_manifest(path, lambda m: m.update(alembic_revision="0099_future"))
    clear_target(docker, qdrant)
    goal = target(qdrant)
    calls = len(docker.calls)
    with pytest.raises(backup.BackupError, match="0099_future.*head 0005"):
        backup.restore_backup(path, goal, force=True)
    assert len(docker.calls) == calls


def test_restore_of_older_schema_reports_needed_migration(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Копия прошлой ревизии восстанавливается; migrate затем поднимет её до head."""
    docker.databases["ai_support"]["revision"] = "0004_review_workflow"
    path = make_backup(docker, qdrant, tmp_path)
    clear_target(docker, qdrant)
    result = backup.restore_backup(path, target(qdrant))
    assert result["alembic_revision"] == "0004_review_workflow"
    assert result["migration_needed"] is True


def test_restore_requires_stopped_api(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Работающий api мог бы писать во время замены: restore отказывается."""
    path = make_backup(docker, qdrant, tmp_path)
    clear_target(docker, qdrant)
    docker.running |= {"api", "ui"}
    with pytest.raises(backup.BackupError, match="запущены сервисы api: останови"):
        backup.restore_backup(path, target(qdrant))
    # Для явного контейнера Compose-проекта нет: проверку выполняет человек.
    backup.restore_backup(path, target(qdrant, project=None))
    assert docker.databases["ai_support"]["tables"] == TABLES


def test_restore_refuses_older_postgres_major(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Дамп PostgreSQL 17 не применяется к PostgreSQL 16."""
    path = make_backup(docker, qdrant, tmp_path)
    clear_target(docker, qdrant)
    docker.version = 160004
    with pytest.raises(backup.BackupError, match="PostgreSQL 17 .* PostgreSQL 16"):
        backup.restore_backup(path, target(qdrant))


def test_failed_pg_restore_keeps_target_and_drops_temporary_database(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Ошибка pg_restore не трогает рабочую БД и коллекцию."""
    path = make_backup(docker, qdrant, tmp_path)
    docker.databases["ai_support"]["tables"]["tickets"] = 9

    def fail(command, stdin) -> None:
        """pg_restore завершился ошибкой."""
        raise backup.BackupError("pg_restore: error: out of disk space")

    docker.hooks["pg_restore"] = fail
    qdrant.collections["support_knowledge"].append({"id": 9})
    with pytest.raises(backup.BackupError, match="out of disk space"):
        backup.restore_backup(path, target(qdrant), force=True)
    assert docker.databases["ai_support"]["tables"]["tickets"] == 9
    assert "ai_support_restoring" not in docker.databases
    assert len(qdrant.collections["support_knowledge"]) == 3


def test_restore_creates_missing_target_database(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Если рабочей БД нет совсем (её удалили), restore создаёт её из копии."""
    path = make_backup(docker, qdrant, tmp_path)
    del docker.databases["ai_support"]
    qdrant.collections.clear()
    result = backup.restore_backup(path, target(qdrant))
    assert result["replaced"] == []
    assert docker.databases["ai_support"] == {"tables": TABLES, "revision": HEAD}


def test_failed_cleanup_keeps_original_restore_error(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Сбой удаления временной БД не подменяет причину ошибки restore."""
    path = make_backup(docker, qdrant, tmp_path)
    clear_target(docker, qdrant)

    def fail_restore(command, stdin) -> None:
        """pg_restore завершился ошибкой."""
        raise backup.BackupError("pg_restore: error: invalid archive")

    def fail_cleanup(command, stdin) -> None:
        """Удаление временной БД (без CREATE) тоже не удалось."""
        if "DROP DATABASE" in stdin and "CREATE" not in stdin:
            raise backup.BackupError("connection lost")

    docker.hooks.update(pg_restore=fail_restore, psql=fail_cleanup)
    with pytest.raises(backup.BackupError, match="invalid archive"):
        backup.restore_backup(path, target(qdrant))
    assert docker.databases["ai_support"]["tables"] == {}


def test_restore_rejects_rows_that_differ_from_manifest(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Сверка строк временной БД с манифестом блокирует замену."""
    path = make_backup(docker, qdrant, tmp_path)
    rewrite_manifest(path, lambda m: m["postgres"]["tables"].update(tickets=6))
    clear_target(docker, qdrant)
    with pytest.raises(backup.BackupError, match="не совпали с манифестом"):
        backup.restore_backup(path, target(qdrant))
    assert docker.databases["ai_support"]["tables"] == {}
    assert "ai_support_restoring" not in docker.databases


def test_restore_rejects_points_that_differ_from_manifest(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Число точек после загрузки snapshot сверяется с манифестом."""
    path = make_backup(docker, qdrant, tmp_path)
    rewrite_manifest(path, lambda m: m["qdrant"].update(points=5))
    clear_target(docker, qdrant)
    with pytest.raises(backup.BackupError, match="точек Qdrant"):
        backup.restore_backup(path, target(qdrant))
    assert docker.databases["ai_support"]["tables"] == {}


def test_restore_reports_qdrant_checksum_rejection(
    docker: FakeDocker,
    qdrant: FakeQdrant,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Qdrant сам сверяет checksum; его отказ останавливает restore."""
    path = make_backup(docker, qdrant, tmp_path)

    def wrong_digest(manifest: dict) -> None:
        """Указать для snapshot чужую сумму (как при подмене файла)."""
        manifest["files"]["qdrant.snapshot"]["sha256"] = "f" * 64

    rewrite_manifest(path, wrong_digest)
    clear_target(docker, qdrant)
    # Офлайн-проверку обходим, чтобы дойти до проверки самим Qdrant.
    monkeypatch.setattr(backup, "verify_backup", backup.load_manifest)
    with pytest.raises(backup.BackupError, match="HTTP 400: Checksum mismatch"):
        backup.restore_backup(path, target(qdrant))
    assert docker.databases["ai_support"]["tables"] == {}
    assert "ai_support_restoring" not in docker.databases


def test_restore_without_snapshot_removes_target_collection(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Копия без индекса удаляет коллекцию цели (при --force)."""
    qdrant.collections.clear()
    path = make_backup(docker, qdrant, tmp_path)
    qdrant.collections["support_knowledge"] = [{"id": 1}]
    backup.restore_backup(path, target(qdrant), force=True)
    assert qdrant.collections == {}
    # Без коллекции у цели удалять нечего.
    docker.databases["ai_support"] = {"tables": {}, "revision": None}
    backup.restore_backup(path, target(qdrant))
    assert qdrant.collections == {}


def test_restore_keeps_restored_database_when_rename_fails(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Если не удалось переименовать, восстановленная БД сохраняется и названа."""
    path = make_backup(docker, qdrant, tmp_path)
    clear_target(docker, qdrant)

    def fail_rename(command, stdin) -> None:
        """ALTER DATABASE RENAME завершился ошибкой."""
        if "RENAME" in stdin:
            raise backup.BackupError("database is being accessed by other users")

    docker.hooks["psql"] = fail_rename
    with pytest.raises(backup.BackupError, match="остались в БД ai_support_restoring"):
        backup.restore_backup(path, target(qdrant))
    assert docker.databases["ai_support_restoring"]["tables"] == TABLES


def test_restore_rejects_too_long_database_name(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Имя временной БД не должно превышать 63 байта PostgreSQL."""
    path = make_backup(docker, qdrant, tmp_path)
    long_name = "d" * 60
    docker.databases[long_name] = {"tables": {}, "revision": None}
    prefix = ("docker", "exec", "-i", "db")
    postgres = backup.Postgres.connect(prefix, long_name)
    goal = backup.Target(postgres, backup.Qdrant(qdrant.url, "empty"), None)
    with pytest.raises(backup.BackupError, match="слишком длинное"):
        backup.restore_backup(path, goal)


def test_postgres_rejects_unexpected_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Неожиданный вывод printenv, списка таблиц и alembic_version — ошибка."""
    outputs = iter(["support\n", "not json", "[1, 2]"])
    monkeypatch.setattr(backup, "run", lambda *args, **kwargs: next(outputs))
    with pytest.raises(backup.BackupError, match="POSTGRES_USER"):
        backup.Postgres.connect(("docker", "exec", "-i", "db"))
    postgres = backup.Postgres(("docker",), "support", "ai_support")
    with pytest.raises(backup.BackupError, match="список таблиц"):
        postgres.state()
    with pytest.raises(backup.BackupError, match="список таблиц"):
        postgres.state()
    monkeypatch.setattr(
        backup,
        "run",
        lambda argv, stdin=None, **kw: (
            '{"alembic_version": 1}' if "information_schema" in stdin else "a\nb\n"
        ),
    )
    with pytest.raises(backup.BackupError, match="2 ревизий вместо 1"):
        postgres.state()


@pytest.mark.parametrize(
    ("output", "url"),
    [
        ("127.0.0.1:6335\n", "http://127.0.0.1:6335"),
        ("0.0.0.0:6340\n", "http://127.0.0.1:6340"),
        ("[::]:6341\n", "http://127.0.0.1:6341"),
        ("[::1]:6342\n", "http://[::1]:6342"),
    ],
)
def test_published_url_uses_local_address(
    docker: FakeDocker, output: str, url: str
) -> None:
    """Опубликованный порт Qdrant превращается в локальный адрес."""
    docker.port_output = output
    assert backup.published_url("p", "qdrant", 6333) == url


def test_published_url_rejects_unpublished_port(docker: FakeDocker) -> None:
    """Без опубликованного порта адрес Qdrant нужно указать явно."""
    docker.port_output = "\n"
    with pytest.raises(backup.BackupError, match="не публикует порт"):
        backup.published_url("p", "qdrant", 6333)


def test_qdrant_errors_are_explained(qdrant: FakeQdrant) -> None:
    """HTTP-ошибка, недоступный сервер и чужой ответ дают понятную причину."""
    client = backup.Qdrant(qdrant.url, "missing")
    with pytest.raises(backup.BackupError, match="HTTP 404: Collection `missing`"):
        client.count()
    qdrant.failures[("GET", "/")] = 502
    with pytest.raises(backup.BackupError, match="HTTP 502"):
        client.version()
    url = qdrant.url
    qdrant.close()
    with pytest.raises(backup.BackupError, match="Qdrant недоступен"):
        backup.Qdrant(url, "support_knowledge", timeout=2).exists()


def test_qdrant_rejects_response_without_result(qdrant: FakeQdrant) -> None:
    """Ответ без ожидаемого поля JSON не принимается молча."""
    client = backup.Qdrant(qdrant.url, "support_knowledge")
    with pytest.raises(backup.BackupError, match="неожиданный ответ на GET /"):
        client.call("GET", "/", field="result")


def test_qdrant_reason_falls_back_to_http_reason() -> None:
    """Если тело ошибки не JSON, показывается стандартная причина HTTP."""
    error = backup.HTTPError("http://q", 500, "Server Error", {}, io.BytesIO(b"<html"))
    assert backup.reason(error) == "Server Error"


def child(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Запустить CLI отдельным процессом Python, как в CI."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        cwd=cwd,
        check=False,
    )


def test_run_streams_files_text_and_reports_failures(tmp_path: Path) -> None:
    """run передаёт файлы и текст потоками, а код ошибки превращает в BackupError."""
    source = tmp_path / "source.bin"
    source.write_bytes(b"\x00PGDMP\r\n" * 1000)
    copy = [
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
    ]
    assert backup.run(copy, stdin=source, stdout=tmp_path / "copy.bin") == ""
    assert (tmp_path / "copy.bin").read_bytes() == source.read_bytes()
    assert backup.run(copy, stdin="SELECT 'тест';") == "SELECT 'тест';"
    assert backup.run([sys.executable, "-c", "print('ok')"]).strip() == "ok"
    failing = [
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('\\n'.join(map(str, range(9)))); sys.exit(3)",
    ]
    with pytest.raises(backup.BackupError) as error:
        backup.run(failing)
    message = str(error.value)
    assert "код 3" in message and "8" in message and "\n0\n" not in message
    with pytest.raises(backup.BackupError, match="docker не найден"):
        backup.run([str(tmp_path / "no-such-docker")])
    # Существующий файл результата не перезаписывается.
    with pytest.raises(FileExistsError):
        backup.run(copy, stdin="x", stdout=tmp_path / "copy.bin")


def test_main_create_verify_and_restore_print_json(
    docker: FakeDocker,
    qdrant: FakeQdrant,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI проходит create → verify → restore и печатает отчёты JSON."""
    output = tmp_path / "out"
    assert (
        backup.main(["create", "--project-name", "demo", "--output-dir", str(output)])
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    assert created["path"].startswith(str(output / "demo-"))
    compose = [*backup.DOCKER, "compose", "--project-name", "demo"]
    assert docker.calls[0][: len(compose)] == compose
    assert backup.main(["verify", created["path"]]) == 0
    assert json.loads(capsys.readouterr().out)["points"] == 2
    assert backup.main(["restore", created["path"], "--project-name", "demo"]) == 1
    assert "цель не пуста" in capsys.readouterr().err
    arguments = ["restore", created["path"], "--project-name", "demo", "--force"]
    assert backup.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["database"] == "ai_support"


def test_main_with_explicit_container_needs_qdrant_url(
    docker: FakeDocker,
    qdrant: FakeQdrant,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--db-container обходит Compose; адрес Qdrant тогда обязателен."""
    arguments = ["create", "--db-container", "pg", "--output-dir", str(tmp_path)]
    assert backup.main(arguments) == 1
    assert "--qdrant-url" in capsys.readouterr().err
    arguments += ["--qdrant-url", qdrant.url, "--database", "ai_support"]
    assert backup.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["path"].startswith(
        str(tmp_path / "container-")
    )
    assert docker.calls[0][: len(backup.DOCKER) + 3] == [
        *backup.DOCKER,
        "exec",
        "-i",
        "pg",
    ]


def test_main_restore_rejects_foreign_folder_before_docker(
    docker: FakeDocker, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Папка без манифеста отклоняется до обращения к Docker."""
    assert backup.main(["restore", str(tmp_path)]) == 1
    assert "нет manifest.json" in capsys.readouterr().err
    assert docker.calls == []


def test_cli_verify_in_child_process(
    docker: FakeDocker, qdrant: FakeQdrant, tmp_path: Path
) -> None:
    """Отдельный процесс CLI проверяет копию и сообщает о повреждении кодом 1."""
    path = make_backup(docker, qdrant, tmp_path)
    result = child("verify", str(path), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["alembic_revision"] == HEAD
    with (path / "qdrant.snapshot").open("ab") as file:
        file.write(b"tail")
    result = child("verify", str(path), cwd=tmp_path)
    assert result.returncode == 1
    assert "qdrant.snapshot: размер отличается" in result.stderr
    result = child("--help", cwd=tmp_path)
    assert result.returncode == 0 and "create" in result.stdout
