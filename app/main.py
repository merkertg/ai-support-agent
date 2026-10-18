"""Точка входа API: создаём приложение и описываем проверку его работы."""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from fastapi import Depends, FastAPI, HTTPException
from psycopg import Error as PsycopgError
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.agents import agent_error_handler
from app.api.agents import router as agents_router
from app.api.drafts import llm_error_handler
from app.api.drafts import router as drafts_router
from app.api.errors import database_error_handler, resource_error_handler
from app.api.knowledge import router as knowledge_router
from app.api.operator import router as operator_router
from app.api.reviews import router as reviews_router
from app.api.routes import router as api_router
from app.db.session import DatabaseNotConfigured, dispose_engine, get_session
from app.llm.errors import LLMError
from app.services.agents import AgentRunFailure
from app.services.errors import ResourceError
from app.workflow.checkpoint import close_checkpointer

# Путь не зависит от каталога, из которого запущен Uvicorn.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
READINESS_ERROR = "База данных недоступна или её схема не готова"


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Освободить пул PostgreSQL после остановки HTTP-приложения.

    При старте не открываем соединения и не создаём таблицы: за изменение
    схемы отвечает явный запуск Alembic, локально или в сервисе migrations.
    """
    try:
        yield
    finally:
        dispose_engine()
        close_checkpointer()


def get_readiness_session() -> Iterator[Session]:
    """Преобразовать ошибки настройки и БД в безопасный ответ HTTP 503.

    yield from передаёт сессию обработчику и возвращает сюда его исключения.
    Поэтому ошибка первого SQL-запроса тоже попадает в этот except.
    В HTTP-ответ никогда не включаем текст драйвера с адресом подключения.
    """
    try:
        yield from get_session()
    except (DatabaseNotConfigured, ValidationError, SQLAlchemyError):
        raise HTTPException(status_code=503, detail=READINESS_ERROR) from None


def migration_heads() -> set[str]:
    """Прочитать ожидаемые версии схемы из файлов миграций в приложении."""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    return set(scripts.get_heads())


# Создаём один объект приложения. Uvicorn найдёт его по адресу app.main:app.
# Эти метаданные отображаются в документации API на странице /docs.
app = FastAPI(
    title="AI Support Agent",
    description="AI-powered technical support system",
    version="0.1.0",
    lifespan=lifespan,
)


# Регистрируем обработчик GET-запроса по пути /health.
# tags группирует этот маршрут в разделе Health на странице /docs.
@app.get("/health", tags=["Health"])
async def health() -> dict[str, str]:
    """Проверить, что приложение принимает и обрабатывает HTTP-запросы.

    FastAPI вызывает функцию при GET /health. Аргументы не нужны.
    Возвращаем словарь со статусом и именем сервиса; FastAPI преобразует
    его в JSON-ответ с кодом 200. Аннотация dict[str, str] описывает
    ожидаемые типы ключей и значений возвращаемого словаря.

    Здесь нет запросов к базе или модели: статус относится к самому API.
    """
    # async def объявляет асинхронную функцию. Она также может выполнять
    # простые действия без await, как в этой проверке.
    return {
        # "ok" означает, что обработчик дошёл до формирования ответа.
        "status": "ok",
        # Имя позволяет понять, какой именно сервис ответил на запрос.
        "service": "ai-support-agent",
    }


@app.get("/ready", tags=["Health"])
def ready(
    session: Annotated[Session, Depends(get_readiness_session)],
) -> dict[str, str]:
    """Проверить PostgreSQL и применение всех миграций перед приёмом трафика.

    /health показывает, что API живо. /ready дополнительно выполняет SQL и
    сверяет alembic_version с актуальными head-версиями из проекта. Пустая,
    недоступная или устаревшая БД получает HTTP 503; схема здесь не меняется.
    Синхронный обработчик FastAPI выполняет в рабочем потоке, поэтому
    ожидание Psycopg не блокирует основной цикл асинхронного приложения.
    """
    session.execute(text("SELECT 1"))
    current_revisions = set(
        session.execute(text("SELECT version_num FROM alembic_version")).scalars()
    )
    try:
        expected_revisions = migration_heads()
    except (CommandError, OSError):
        raise HTTPException(status_code=503, detail=READINESS_ERROR) from None
    if not expected_revisions or current_revisions != expected_revisions:
        raise HTTPException(status_code=503, detail=READINESS_ERROR)
    return {"status": "ok", "database": "ok"}


# REST-слой подключаем отдельно: проверки /health и /ready сохраняют свой контракт.
app.include_router(api_router)
app.include_router(drafts_router)
app.include_router(agents_router)
app.include_router(knowledge_router)
app.include_router(reviews_router)
app.include_router(operator_router)
app.add_exception_handler(AgentRunFailure, agent_error_handler)
app.add_exception_handler(LLMError, llm_error_handler)
app.add_exception_handler(ResourceError, resource_error_handler)
app.add_exception_handler(SQLAlchemyError, database_error_handler)
# PostgresSaver использует psycopg напрямую, без обёртки SQLAlchemy.
app.add_exception_handler(PsycopgError, database_error_handler)
