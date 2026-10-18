"""PostgreSQL checkpointer LangGraph: состояние графа переживает перезапуск API.

Таблицы checkpoint_* создаёт миграция 0004, поэтому setup() здесь не нужен.
Пул psycopg выдаёт соединение только на время записи или чтения checkpoint:
пока узел ждёт модель, соединение checkpointer не занято.
"""

import threading

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.db.session import DatabaseNotConfigured

_lock = threading.Lock()
_pool: ConnectionPool | None = None
_saver: PostgresSaver | None = None


def conninfo(database_url: str) -> str:
    """Преобразовать URL SQLAlchemy (postgresql+psycopg://) в адрес libpq."""
    return (
        make_url(database_url)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )


def open_saver(database_url: str) -> tuple[ConnectionPool, PostgresSaver]:
    """Создать пул и checkpointer; autocommit и dict_row требует PostgresSaver."""
    pool = ConnectionPool(
        conninfo(database_url),
        min_size=0,
        max_size=4,
        timeout=5,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "connect_timeout": 3,
        },
        open=True,
    )
    return pool, PostgresSaver(pool)


def get_checkpointer() -> PostgresSaver:
    """Вернуть общий для процесса checkpointer; соединения открываются лениво."""
    global _pool, _saver
    with _lock:
        if _saver is None:
            database_url = get_settings().database_url
            if database_url is None:
                raise DatabaseNotConfigured("DATABASE_URL не задан")
            _pool, _saver = open_saver(database_url)
        return _saver


def close_checkpointer() -> None:
    """Закрыть пул при остановке приложения; повторный вызов безопасен."""
    global _pool, _saver
    with _lock:
        if _pool is not None:
            _pool.close()
        _pool, _saver = None, None
