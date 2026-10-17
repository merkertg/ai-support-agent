"""Ленивое подключение к PostgreSQL и ограниченные временем жизни сессии."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from app.config import get_settings


class DatabaseNotConfigured(RuntimeError):
    """Сообщить об отсутствующей настройке без раскрытия адреса или пароля."""


@lru_cache
def get_engine() -> Engine:
    """Создать пул при первом обращении; импорт приложения не требует БД.

    create_engine подготавливает пул, а настоящее соединение открывается
    только при первом SQL-запросе. pool_pre_ping проверяет выданное из пула
    соединение; connect_timeout ограничивает ожидание недоступного сервера.
    """
    database_url = get_settings().database_url
    if database_url is None:
        raise DatabaseNotConfigured("DATABASE_URL не задан")
    return create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
    )


def get_session() -> Iterator[Session]:
    """Выдать сессию запроса, откатить ошибку и всегда освободить соединение.

    Операция записи сама вызывает session.commit(), когда все её действия
    успешно завершены. Эта зависимость не выполняет скрытый commit после
    каждого запроса. Session.close() откатывает незавершённую транзакцию.
    """
    with Session(get_engine()) as session:
        try:
            yield session
        except Exception:
            session.rollback()
            raise


def dispose_engine() -> None:
    """Закрыть существующий пул при остановке, не создавая новый двигатель."""
    # Если /health был единственным запросом, движок вообще не создавался.
    if get_engine.cache_info().currsize:
        get_engine().dispose()
        get_engine.cache_clear()
