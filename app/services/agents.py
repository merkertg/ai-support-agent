"""Запуск агента с короткими SQL-транзакциями и сохранением проверяемого итога."""

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.config import get_agent_settings
from app.agent.contracts import AgentContext, AgentProgress, AgentProvider
from app.agent.provider import get_agent_provider
from app.agent.runtime import AgentRuntime
from app.agent.tools import SQLToolExecutor
from app.knowledge.index import IndexFactory
from app.llm.errors import LLMError, LLMUnavailableError
from app.models import AgentRun, AgentSuggestion, Ticket
from app.schemas.agents import AgentRunRead, AnalyzeRequest, SuggestionRead
from app.schemas.drafts import AgentDraftContent
from app.services.errors import ResourceNotFound
from app.services.knowledge import open_configured_index

# Фабрика возвращается из Depends без вызова. Существование обращения
# проверяем раньше конфигурации внешнего провайдера, как и в /draft.
ProviderFactory = Callable[[], AgentProvider]


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
    response = AgentRunRead(
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
    # DTO скопирован из ORM: чтение можно закончить и вернуть соединение в пул.
    session.rollback()
    return response


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
    settings = get_agent_settings()
    executor = SQLToolExecutor(engine, context, knowledge=knowledge_factory)
    runtime = AgentRuntime(provider, executor, settings)
    with session.begin():
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
        finish_run(session, run_id, runtime.progress, error=error)
        raise AgentRunFailure.of(error, run_id) from None
    except Exception:
        # Неожиданная ошибка также получает конечный статус и безопасный текст.
        # Если сама БД недоступна, запись finish_run откатится и общий handler
        # вернёт 503; обещать сохранение failed в этом случае невозможно.
        error = LLMUnavailableError()
        finish_run(session, run_id, runtime.progress, error=error)
        raise AgentRunFailure.of(error, run_id) from None
    finish_run(
        session, run_id, outcome.progress, result=outcome.final, model=outcome.model
    )
    return read_run(session, run_id)
