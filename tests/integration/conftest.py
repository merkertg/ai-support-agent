"""Запускать интеграционные проверки только в явно выбранных тестовых БД и Qdrant."""

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from qdrant_client import QdrantClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from app.config import Settings
from app.knowledge.embeddings import MockEmbedder
from app.knowledge.index import KnowledgeIndex
from app.models import User
from app.schemas.auth import TokenCreate
from app.services import auth

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def staff_principal() -> None:
    """Отменить подмену аутентификации unit-тестов: здесь проверяются токены в БД.

    Фикстура с тем же именем в tests/conftest.py подставляет администратора без
    SQL. Интеграционные запросы передают настоящий заголовок Authorization.
    """
    return None


def issue_staff_token(
    engine: Engine,
    *,
    role: str = "admin",
    email: str = "staff-admin@example.com",
    name: str = "Тестовый администратор",
) -> tuple[int, str]:
    """Создать сотрудника в тестовой БД и выдать ему токен через сервис."""
    with Session(engine) as session:
        user = User(name=name, email=email, role=role)
        session.add(user)
        session.commit()
        issued = auth.issue_token(session, user.id, TokenCreate(name="tests"))
    return issued.user_id, issued.token


def bearer(token: str) -> dict[str, str]:
    """Заголовок запроса с токеном сотрудника."""
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def staff_headers(migrated_engine: Engine) -> dict[str, str]:
    """Заголовок администратора тестовой БД для обычных запросов API."""
    _, token = issue_staff_token(migrated_engine)
    return bearer(token)


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """Отказаться от сброса схемы без TEST_DATABASE_URL и суффикса _test.

    Не используем DATABASE_URL или .env как запасной вариант: там могут
    находиться данные разработчика. На GitHub отсутствие адреса — ошибка,
    локально это осознанный skip, явно показанный в отчёте Pytest.
    """
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        message = "Укажите TEST_DATABASE_URL для отдельной PostgreSQL-базы *_test"
        if os.environ.get("CI", "").lower() in {"1", "true", "yes"}:
            pytest.fail(message)
        pytest.skip(message)
    settings = Settings(database_url=value, _env_file=None)
    database = make_url(settings.database_url).database
    if not database or not database.endswith("_test"):
        pytest.fail("Интеграционные тесты разрешены только для базы с суффиксом _test")
    return value


@pytest.fixture(scope="session")
def test_engine(test_database_url: str):
    """Использовать отдельный пул с тайм-аутом и закрыть его после тестов."""
    engine = create_engine(
        test_database_url, pool_pre_ping=True, connect_args={"connect_timeout": 3}
    )
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def migration_config(test_database_url: str) -> Config:
    """Передать Alembic безопасно выбранный URL без изменения локального .env."""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    # attributes не применяет %-интерполяцию ConfigParser к паролю в URL.
    config.attributes["database_url"] = test_database_url
    return config


@pytest.fixture
def migrated_engine(test_engine: Engine, migration_config: Config):
    """Для каждого теста создать чистую схему только через версии Alembic.

    Сброс работает лишь после проверки имени *_test в test_database_url.
    create_all здесь намеренно отсутствует: иначе тест не докажет, что
    пользователь действительно может создать БД штатными миграциями.
    """
    command.downgrade(migration_config, "base")
    command.upgrade(migration_config, "head")
    try:
        yield test_engine
    finally:
        command.downgrade(migration_config, "base")


def required_test_url(name: str) -> str:
    """Прочитать явный адрес тестового сервиса: локально skip, в CI — ошибка."""
    value = os.environ.get(name)
    if not value:
        message = f"Укажите {name} для отдельного тестового сервиса"
        if os.environ.get("CI", "").lower() in {"1", "true", "yes"}:
            pytest.fail(message)
        pytest.skip(message)
    return value


@pytest.fixture(scope="session")
def test_qdrant_url() -> str:
    """Использовать только явно заданный тестовый Qdrant, а не QDRANT_URL из .env."""
    return required_test_url("TEST_QDRANT_URL")


@pytest.fixture
def qdrant_collection(test_qdrant_url: str) -> Iterator[str]:
    """Выдать уникальную коллекцию test_* и удалить её после теста.

    Тест не трогает другие коллекции сервера, даже если адрес указан неверно.
    """
    name = f"test_{uuid4().hex[:12]}"
    try:
        yield name
    finally:
        client = QdrantClient(url=test_qdrant_url, check_compatibility=False)
        try:
            if client.collection_exists(name):
                client.delete_collection(name)
        finally:
            client.close()


@pytest.fixture
def index_factory(test_qdrant_url: str, qdrant_collection: str):
    """Открывать индекс тестовой коллекции так же, как настоящая фабрика."""

    def factory() -> KnowledgeIndex:
        """Создать новый клиент на каждый вызов, как в работающем API."""
        return KnowledgeIndex(
            QdrantClient(url=test_qdrant_url, timeout=5, check_compatibility=False),
            qdrant_collection,
            MockEmbedder(256),
            min_score=0.2,
        )

    return factory
