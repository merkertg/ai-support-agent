"""Завершение брошенных запусков без повторения анализа и отправки ответа."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import func, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import DatabaseNotConfigured, get_engine
from app.models import AgentRun
from app.observability import database_fields, get_logger, log_event

# Берём верхнюю границу настройки, а не текущее значение .env: перезапуск
# мог уменьшить лимит с 120 до 60 секунд, пока старый worker ещё работает.
MAX_AGENT_TIMEOUT_SECONDS = 120
STALE_RUNNING_GRACE_SECONDS = 60
STALE_RUNNING_SECONDS = MAX_AGENT_TIMEOUT_SECONDS + STALE_RUNNING_GRACE_SECONDS
RECOVERY_INTERVAL_SECONDS = 30
RECOVERY_ERROR = (
    "Запуск прерван: результат не сохранён за предельное время. "
    "Повторите анализ обращения."
)
logger = get_logger("recovery")


class RunRecoverySettings(BaseSettings):
    """Разрешить явно отключить монитор; в обычном приложении он включён."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    run_recovery_enabled: bool = True


def stale_cutoff():
    """Вычислить порог часами PostgreSQL, которыми задан started_at."""
    return func.now() - timedelta(seconds=STALE_RUNNING_SECONDS)


def recover_stale_runs(
    session: Session, *, ticket_id: int | None = None
) -> list[tuple[int, int]]:
    """Пометить старые running как failed внутри транзакции вызывающей стороны.

    Условный UPDATE повторно проверяет статус после ожидания блокировки:
    успевший completed не будет перезаписан. Предложения и сообщения не
    создаём. Существующие steps/tool_trace сохраняем как след диагностики.
    """
    # Ограничение действует и для фонового прохода, и перед новым анализом.
    # При занятой строке откат безопаснее бесконечного ожидания HTTP-запроса.
    session.execute(text("SET LOCAL statement_timeout = '3s'"))
    session.execute(text("SET LOCAL lock_timeout = '1s'"))
    statement = (
        update(AgentRun)
        .where(AgentRun.status == "running", AgentRun.started_at <= stale_cutoff())
        .values(
            status="failed",
            finished_at=func.now(),
            error_message=RECOVERY_ERROR,
            result=None,
        )
        .returning(AgentRun.id, AgentRun.ticket_id)
        .execution_options(synchronize_session=False)
    )
    if ticket_id is not None:
        statement = statement.where(AgentRun.ticket_id == ticket_id)
    return [(row.id, row.ticket_id) for row in session.execute(statement)]


def log_recovered_runs(recovered: list[tuple[int, int]]) -> None:
    """Записать подтверждённые commit события без текста обращения и ключей."""
    for run_id, ticket_id in recovered:
        log_event(
            logger,
            logging.WARNING,
            "agent_run_recovered",
            run_id=run_id,
            ticket_id=ticket_id,
            status="failed",
        )


def recover_once() -> None:
    """Выполнить один ограниченный проход; сбой БД повторится на следующем тике.

    Проверка работает с существующей схемой и не запускает миграции. Текст
    драйвера, SQL, параметры и адрес БД не включаются в журнал ошибки.
    """
    try:
        with Session(get_engine()) as session, session.begin():
            recovered = recover_stale_runs(session)
        log_recovered_runs(recovered)
    except (DatabaseNotConfigured, ValidationError, SQLAlchemyError) as error:
        log_event(
            logger,
            logging.WARNING,
            "agent_run_recovery_failed",
            **database_fields(error),
        )


async def recovery_loop(stop: asyncio.Event, *, interval: float) -> None:
    """Проверять БД по таймеру; ожидание и SQL не блокируют event loop API.

    Первый проход через interval: импорт и короткие проверки /health не
    требуют БД. stop будит ожидание сразу. Уже начавшийся ограниченный SQL
    завершается до закрытия пула, поэтому при shutdown не остаётся потока.
    """
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            await asyncio.to_thread(recover_once)


@asynccontextmanager
async def recovery_monitor() -> AsyncIterator[None]:
    """Связать один фоновый монитор с временем жизни процесса HTTP-приложения."""
    if not RunRecoverySettings().run_recovery_enabled:
        yield
        return
    stop = asyncio.Event()
    task = asyncio.create_task(
        recovery_loop(stop, interval=RECOVERY_INTERVAL_SECONDS),
        name="agent-run-recovery",
    )
    try:
        yield
    finally:
        stop.set()
        await task
