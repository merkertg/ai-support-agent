"""Запуск агента с короткими SQL-транзакциями и сохранением проверяемого итога."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.config import get_agent_settings
from app.agent.contracts import AgentContext, AgentProgress, AgentProvider
from app.agent.provider import get_agent_provider
from app.agent.runtime import AgentRuntime
from app.agent.tools import SQLToolExecutor
from app.knowledge.index import IndexFactory
from app.llm.errors import LLMError, LLMUnavailableError
from app.models import AgentRun, AgentSuggestion, Ticket
from app.observability import get_logger, log_event, upstream_fields
from app.schemas.agents import AgentRunRead, AnalyzeRequest, SuggestionRead
from app.schemas.drafts import AgentDraftContent
from app.schemas.resources import CLOSED_TICKET_STATUSES
from app.services.errors import ResourceConflict, ResourceNotFound
from app.services.knowledge import open_configured_index

# Фабрика возвращается из Depends без вызова. Существование обращения
# проверяем раньше конфигурации внешнего провайдера, как и в /draft.
ProviderFactory = Callable[[], AgentProvider]
# Запас поверх лимита времени агента: running старше лимита и запаса оставлен
# аварийно остановленным процессом и не блокирует новый анализ обращения.
STALE_RUNNING_GRACE_SECONDS = 60
logger = get_logger("agent")
TICKET_CLOSED = "Обращение закрыто: анализ недоступен"


class AgentRunFailure(Exception):
    """Связать безопасную ошибку модели с уже сохранённым неудачным запуском."""

    def __init__(self, status_code: int, detail: str, run_id: int) -> None:
        """Хранить только публичный статус и текст, не исходную ошибку SDK."""
        self.status_code = status_code
        self.detail = detail
        self.run_id = run_id
        super().__init__(self.detail)

    @classmethod
    def of(cls, error: LLMError, run_id: int) -> "AgentRunFailure":
        """Скопировать постоянные статус и текст безопасной ошибки."""
        return cls(error.status_code, error.detail, run_id)


def read_run(session: Session, run_id: int) -> AgentRunRead:
    """Собрать независимый DTO запуска и единственного черновика, если он есть."""
    run = session.get(AgentRun, run_id)
    if run is None:
        raise ResourceNotFound("Запуск агента не найден")
    suggestion = session.scalar(
        select(AgentSuggestion)
        .where(AgentSuggestion.agent_run_id == run_id)
        .order_by(AgentSuggestion.id)
        .limit(1)
    )
    response = run_read(run, suggestion)
    # DTO скопирован из ORM: чтение можно закончить и вернуть соединение в пул.
    session.rollback()
    return response


def run_read(run: AgentRun, suggestion: AgentSuggestion | None) -> AgentRunRead:
    """Скопировать запуск и его предложение в независимый DTO без SQL-запросов."""
    return AgentRunRead(
        id=run.id,
        ticket_id=run.ticket_id,
        status=run.status,
        provider=run.provider,
        model=run.model,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error_message=run.error_message,
        steps=run.steps,
        tool_trace=run.tool_trace,
        usage=run.usage,
        result=run.result,
        suggestion=SuggestionRead.model_validate(suggestion) if suggestion else None,
        workflow_thread_id=run.workflow_thread_id,
    )


def finish_run(
    session: Session,
    run_id: int,
    progress: AgentProgress,
    *,
    result: AgentDraftContent | None = None,
    error: LLMError | None = None,
    model: str | None = None,
) -> None:
    """Атомарно записать прогресс и либо pending-предложение, либо ошибку."""
    with session.begin():
        run = session.get(AgentRun, run_id)
        if run is None:
            raise ResourceNotFound("Запуск агента не найден")
        snapshot = progress.to_dict()
        run.steps = snapshot["steps"]
        run.tool_trace = snapshot["tool_trace"]
        run.usage = snapshot["usage"]
        run.finished_at = datetime.now(UTC)
        if model is not None:
            run.model = model
        if error is not None:
            run.status = "failed"
            run.error_message = error.detail
            run.result = None
        else:
            # Только проверенный черновик создаёт предложение. Транзакция
            # сохраняет completed и pending вместе, без промежуточного успеха.
            if result is None:
                raise ValueError("Для успешного запуска требуется результат")
            run.status = "completed"
            run.error_message = None
            run.result = result.model_dump(mode="json")
            session.add(
                AgentSuggestion(
                    agent_run_id=run_id,
                    suggestion_type="reply",
                    status="pending",
                    content=result.draft_reply,
                )
            )


def ensure_analyzable(session: Session, ticket: Ticket, timeout_seconds: float) -> None:
    """Запретить анализ закрытого обращения и второй черновик того же обращения.

    Иначе у обращения копились бы несколько pending-предложений, и одобрение
    каждого доставило бы клиенту отдельный ответ. Окончательная проверка идёт
    под блокировкой строки обращения, поэтому параллельные запуски не обходят её.
    """
    if ticket.status in CLOSED_TICKET_STATUSES:
        raise ResourceConflict(TICKET_CLOSED)
    pending = session.scalar(
        select(AgentSuggestion.id)
        .join(AgentRun, AgentRun.id == AgentSuggestion.agent_run_id)
        .where(AgentRun.ticket_id == ticket.id, AgentSuggestion.status == "pending")
        .order_by(AgentSuggestion.id)
        .limit(1)
    )
    if pending is not None:
        raise ResourceConflict(
            f"У обращения уже есть предложение {pending} на проверке: "
            "примите по нему решение до нового анализа"
        )
    # Время сравнивается на стороне PostgreSQL: started_at задаёт тот же сервер.
    window = func.make_interval(
        0, 0, 0, 0, 0, 0, timeout_seconds + STALE_RUNNING_GRACE_SECONDS
    )
    running = session.scalar(
        select(AgentRun.id)
        .where(
            AgentRun.ticket_id == ticket.id,
            AgentRun.status == "running",
            AgentRun.started_at > func.now() - window,
        )
        .order_by(AgentRun.id)
        .limit(1)
    )
    if running is not None:
        raise ResourceConflict(f"Анализ обращения уже выполняется: запуск {running}")


def analyze_ticket(
    session: Session,
    ticket_id: int,
    payload: AnalyzeRequest,
    provider_factory: ProviderFactory = get_agent_provider,
    knowledge_factory: IndexFactory | None = open_configured_index,
    *,
    workflow_thread_id: str | None = None,
) -> AgentRunRead:
    """Прочитать границы доступа, освободить БД и выполнить ограниченный цикл.

    До модели сохраняется running. Во время сетевого ожидания HTTP-сессия
    не удерживает транзакцию или соединение; инструменты открывают отдельные
    короткие сессии только для чтения. Агент не меняет обращение и не отправляет
    ответ: успешный результат всегда остаётся pending-предложением человеку.
    """
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        raise ResourceNotFound("Обращение не найдено")
    settings = get_agent_settings()
    # Быстрый отказ до настройки провайдера; повтор под блокировкой ниже.
    ensure_analyzable(session, ticket, settings.agent_timeout_seconds)
    context = AgentContext(
        ticket_id=ticket.id,
        customer_id=ticket.customer_id,
        product_id=ticket.product_id,
        license_key_hint=payload.license_key,
    )
    engine = session.get_bind()
    session.rollback()
    # Некорректная конфигурация не должна оставлять фиктивный running.
    provider = provider_factory()
    executor = SQLToolExecutor(engine, context, knowledge=knowledge_factory)
    runtime = AgentRuntime(provider, executor, settings)
    with session.begin():
        # FOR UPDATE выстраивает параллельные анализы одного обращения в очередь:
        # второй запрос увидит running первого и получит 409.
        locked = session.scalar(
            select(Ticket)
            .where(Ticket.id == ticket_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked is None:
            raise ResourceNotFound("Обращение не найдено")
        ensure_analyzable(session, locked, settings.agent_timeout_seconds)
        run = AgentRun(
            ticket_id=ticket_id,
            status="running",
            provider=provider.provider,
            model=provider.model,
            # Связь с потоком LangGraph, который ждёт решения сотрудника.
            workflow_thread_id=workflow_thread_id,
        )
        session.add(run)
        session.flush()
        run_id = run.id
    # Из ORM после commit не читаем поля: expire_on_commit открыл бы новый SQL
    # при обращении к ним прямо перед длительным вызовом модели.
    try:
        outcome = runtime.run(context)
    except LLMError as error:
        log_event(
            logger,
            logging.WARNING,
            "agent_run_failed",
            run_id=run_id,
            ticket_id=ticket_id,
            provider=provider.provider,
            error=type(error).__name__,
            status=error.status_code,
            steps=runtime.progress.steps,
            **upstream_fields(error),
        )
        finish_run(session, run_id, runtime.progress, error=error)
        raise AgentRunFailure.of(error, run_id) from None
    except Exception:
        # Неожиданная ошибка также получает конечный статус и безопасный текст.
        # Если сама БД недоступна, запись finish_run откатится и общий handler
        # вернёт 503; обещать сохранение failed в этом случае невозможно.
        log_event(
            logger,
            logging.ERROR,
            "agent_run_crashed",
            exc_info=True,
            run_id=run_id,
            ticket_id=ticket_id,
            provider=provider.provider,
            steps=runtime.progress.steps,
        )
        error = LLMUnavailableError()
        finish_run(session, run_id, runtime.progress, error=error)
        raise AgentRunFailure.of(error, run_id) from None
    finish_run(
        session, run_id, outcome.progress, result=outcome.final, model=outcome.model
    )
    log_event(
        logger,
        logging.INFO,
        "agent_run_completed",
        run_id=run_id,
        ticket_id=ticket_id,
        provider=provider.provider,
        steps=outcome.progress.steps,
        sources=len(outcome.final.sources),
    )
    return read_run(session, run_id)
