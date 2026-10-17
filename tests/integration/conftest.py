"""Запускать интеграционные проверки только в явно выбранной тестовой БД."""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url

from app.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
