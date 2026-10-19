"""Резервная копия на настоящих PostgreSQL и Qdrant: копия → очистка → восстановление.

pg_dump и pg_restore выполняются внутри контейнера тестовой PostgreSQL
(db-test локально, service container в GitHub Actions), Qdrant — тестовый
сервис с уникальной коллекцией test_*. Сравниваются число строк и содержимое
каждой таблицы, SHA256 файлов копии и все точки Qdrant вместе с векторами.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from sqlalchemy import Engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.knowledge.config import DEFAULT_KNOWLEDGE_DIR
from app.knowledge.documents import load_documents
from app.knowledge.ingest import ingest
from app.models import Customer, License, Product, Ticket, User
from app.schemas.auth import TokenCreate
from app.services import auth
from scripts import backup

pytestmark = pytest.mark.integration


def postgres_container(database_url: str) -> str:
    """Контейнер тестовой PostgreSQL: TEST_POSTGRES_CONTAINER или по порту URL.

    Ищется контейнер, публикующий порт TEST_DATABASE_URL на 5432/tcp: это
    именно та БД, к которой подключаются остальные интеграционные тесты.
    """
    explicit = os.environ.get("TEST_POSTGRES_CONTAINER")
    if explicit:
        return explicit
    port = make_url(database_url).port or 5432
    docker = shutil.which("docker")
    listing = ""
    if docker:
        listing = subprocess.run(
            [docker, "ps", "--format", "{{.ID}}\t{{.Ports}}"],
            capture_output=True,
            encoding="utf-8",
            check=False,
        ).stdout
    published = re.compile(rf"(?:^|[\s,])(?:[0-9.]+|\[[0-9a-f:]*\]):{port}->5432/tcp")
    for line in listing.splitlines():
        identifier, _, ports = line.partition("\t")
        if published.search(ports):
            return identifier
    message = (
        f"Не найден контейнер PostgreSQL с портом {port}: запусти db-test "
        "или укажи TEST_POSTGRES_CONTAINER"
    )
    if os.environ.get("CI", "").lower() in {"1", "true", "yes"}:
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture
def backup_target(
    test_database_url: str, test_qdrant_url: str, qdrant_collection: str
) -> backup.Target:
    """Цель копии: тестовая БД *_test в своём контейнере и коллекция test_*.

    Имя БД передаётся явно из проверенного TEST_DATABASE_URL, поэтому restore
    не может заменить другую БД того же сервера.
    """
    container = postgres_container(test_database_url)
    database = make_url(test_database_url).database
    prefix = (*backup.DOCKER, "exec", "-i", container)
    postgres = backup.Postgres.connect(prefix, database)
    return backup.Target(
        postgres, backup.Qdrant(test_qdrant_url, qdrant_collection), None
    )


@pytest.fixture
def seeded(migrated_engine: Engine, index_factory) -> tuple[int, str]:
    """Каталог, лицензия, обращение, сотрудник с токеном и проиндексированные статьи."""
    with Session(migrated_engine) as session, session.begin():
        customer = Customer(name="Клиент копии", email="backup@example.test")
        product = Product(name="TechFlow Server", code="techflow-server")
        session.add_all([customer, product])
        session.flush()
        session.add(
            License(
                customer_id=customer.id,
                product_id=product.id,
                license_key="LIC-BACKUP-1",
                status="active",
            )
        )
        session.add(
            Ticket(
                customer_id=customer.id,
                product_id=product.id,
                subject="Сервер не запускается после обновления",
                description="Служба останавливается сразу после старта.",
            )
        )
        staff = User(
            name="Администратор копии", email="admin@example.test", role="admin"
        )
        session.add(staff)
        session.flush()
        staff_id = staff.id
    with Session(migrated_engine) as session:
        issued = auth.issue_token(session, staff_id, TokenCreate(name="backup"))
    with index_factory() as index:
        ingest(migrated_engine, index, load_documents(DEFAULT_KNOWLEDGE_DIR))
    return issued.user_id, issued.token


def table_checksums(engine: Engine) -> dict[str, tuple[int, str]]:
    """Число строк и MD5 содержимого каждой таблицы public в стабильном порядке."""
    with engine.connect() as connection:
        names = connection.scalars(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                "ORDER BY table_name"
            )
        ).all()
        rows = {}
        for name in names:
            query = (
                "SELECT count(*), md5(coalesce(string_agg(t::text, chr(10) "
                f"ORDER BY t::text), '')) FROM public.\"{name}\" t"
            )
            rows[name] = tuple(connection.execute(text(query)).one())
        return rows


def qdrant_points(url: str, collection: str) -> list[tuple[str, dict, list]]:
    """Все точки коллекции: id, payload и вектор, отсортированные по id."""
    client = QdrantClient(url=url, timeout=10, check_compatibility=False)
    try:
        if not client.collection_exists(collection):
            return []
        points, _ = client.scroll(
            collection, limit=10_000, with_payload=True, with_vectors=True
        )
    finally:
        client.close()
    return sorted((str(point.id), point.payload, point.vector) for point in points)


def database_exists(engine: Engine, name: str) -> bool:
    """Есть ли БД с таким именем на тестовом сервере."""
    with engine.connect() as connection:
        query = text("SELECT count(*) FROM pg_database WHERE datname = :name")
        return connection.scalar(query, {"name": name}) == 1


def wipe(engine: Engine, url: str, collection: str) -> None:
    """Очистить цель как при потере томов: пустая схема public и нет коллекции."""
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    client = QdrantClient(url=url, timeout=10, check_compatibility=False)
    try:
        client.delete_collection(collection)
    finally:
        client.close()


def test_backup_wipe_restore_round_trip(
    migrated_engine: Engine,
    seeded: tuple[int, str],
    backup_target: backup.Target,
    test_qdrant_url: str,
    qdrant_collection: str,
    tmp_path: Path,
) -> None:
    """Копия, полная очистка и restore возвращают строки, суммы и точки Qdrant."""
    user_id, token = seeded
    before = table_checksums(migrated_engine)
    points = qdrant_points(test_qdrant_url, qdrant_collection)
    assert before["api_tokens"][0] == 1 and before["knowledge_documents"][0] >= 1
    assert len(points) >= before["knowledge_documents"][0]

    created = backup.create_backup(backup_target, tmp_path)
    path = Path(created["path"])
    manifest = backup.verify_backup(path)
    assert manifest["alembic_revision"] == backup.revision_chain()[-1]
    assert manifest["postgres"]["tables"] == {
        name: rows for name, (rows, _) in before.items()
    }
    assert manifest["qdrant"]["points"] == len(points)
    # verify_backup уже сверил SHA256 и размеры; здесь — что файлы настоящие.
    assert (path / backup.POSTGRES_FILE).read_bytes()[:5] == b"PGDMP"

    wipe(migrated_engine, test_qdrant_url, qdrant_collection)
    assert table_checksums(migrated_engine) == {}
    assert qdrant_points(test_qdrant_url, qdrant_collection) == []

    restored = backup.restore_backup(path, backup_target)
    assert restored["replaced"] == [] and restored["migration_needed"] is False
    assert table_checksums(migrated_engine) == before
    assert qdrant_points(test_qdrant_url, qdrant_collection) == points
    assert not database_exists(
        migrated_engine, backup_target.postgres.database + "_restoring"
    )
    # Токен хранится только как SHA-256 и после восстановления снова принимается.
    with Session(migrated_engine) as session:
        assert auth.authenticate(session, token).user_id == user_id


def test_restore_refuses_non_empty_target_and_force_replaces_changes(
    migrated_engine: Engine,
    seeded: tuple[int, str],
    backup_target: backup.Target,
    index_factory,
    test_qdrant_url: str,
    qdrant_collection: str,
    tmp_path: Path,
) -> None:
    """Без --force данные после копии сохраняются; с --force заменяются копией."""
    before = table_checksums(migrated_engine)
    points = qdrant_points(test_qdrant_url, qdrant_collection)
    path = Path(backup.create_backup(backup_target, tmp_path)["path"])

    with Session(migrated_engine) as session, session.begin():
        session.add(Customer(name="Новый клиент", email="after-backup@example.test"))
    with index_factory() as index:
        index.remove_document("KB-SERVER-START")
    changed = table_checksums(migrated_engine)
    changed_points = qdrant_points(test_qdrant_url, qdrant_collection)
    assert changed["customers"][0] == before["customers"][0] + 1
    assert len(changed_points) < len(points)

    with pytest.raises(backup.BackupError, match="цель не пуста"):
        backup.restore_backup(path, backup_target)
    assert table_checksums(migrated_engine) == changed
    assert qdrant_points(test_qdrant_url, qdrant_collection) == changed_points

    result = backup.restore_backup(path, backup_target, force=True)
    assert len(result["replaced"]) == 2
    assert table_checksums(migrated_engine) == before
    assert qdrant_points(test_qdrant_url, qdrant_collection) == points
