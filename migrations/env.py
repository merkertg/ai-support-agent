"""Запуск Alembic с URL из настроек и поддержкой изолированных тестов."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection

import app.models  # noqa: F401 — регистрирует все модели в общей metadata.
from app.config import get_settings
from app.db.base import Base

config = context.config
if config.config_file_name is not None:
    # Не отключать журналы, созданные до миграции: при запуске Alembic в том же
    # процессе (тесты) иначе замолчал бы JSON-журнал приложения «app».
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata
# Таблицы checkpointer LangGraph создаёт миграция 0004 по DDL библиотеки, но
# SQLAlchemy-моделей у них нет: alembic check не должен предлагать их удалить.
LANGGRAPH_TABLES = frozenset(
    {"checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes"}
)


def include_name(name: str | None, type_: str, parent_names: dict) -> bool:
    """Сравнивать с моделями все объекты, кроме таблиц checkpointer LangGraph."""
    if type_ == "table":
        return name not in LANGGRAPH_TABLES
    return True


def get_database_url() -> str:
    """Возвращает URL без вывода пароля и интерполяции символа процента."""
    # Тест может передать собственную изолированную БД без изменения окружения.
    database_url = config.attributes.get("database_url")
    if database_url is None:
        database_url = get_settings().database_url
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL не задан. Создайте .env по .env.example "
            "или передайте DATABASE_URL в окружении."
        )
    return str(database_url)


def run_migrations_offline() -> None:
    """Генерирует SQL миграции без подключения к PostgreSQL."""
    context.configure(
        url=get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def migrate_with_connection(connection: Connection) -> None:
    """Применяет миграции через переданное соединение, включая тестовое."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Открывает короткое соединение и применяет версионированные миграции."""
    connection = config.attributes.get("connection")
    if connection is not None:
        migrate_with_connection(connection)
        return

    # URL передаётся прямо SQLAlchemy: ConfigParser не обрабатывает пароль с %.
    engine = create_engine(get_database_url(), poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            migrate_with_connection(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
