"""Запуск и продолжение процесса проверки: HTTP ↔ LangGraph ↔ PostgreSQL."""

from collections.abc import Callable
from contextlib import suppress
from uuid import uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.knowledge.index import IndexFactory
from app.models import AgentRun, Ticket, TicketMessage
from app.schemas.agents import AgentRunRead, AnalyzeRequest
from app.schemas.reviews import (
    ReviewRequest,
    ReviewResultRead,
    TicketMessageRead,
    WorkflowRead,
)
from app.services import agents, reviews
from app.services.agents import AgentRunFailure, ProviderFactory
from app.services.errors import ResourceConflict, ResourceNotFound
from app.workflow.graph import REVIEW_NODE, WorkflowContext, build_graph

CheckpointerFactory = Callable[[], BaseCheckpointSaver]
NO_WORKFLOW = "У запуска нет процесса проверки: он создан до этапа 07"


def thread_config(thread_id: str) -> dict:
    """Указать LangGraph поток, состояние которого хранит checkpointer."""
    return {"configurable": {"thread_id": thread_id}}


def review_applier(session: Session) -> Callable[[int, dict], dict]:
    """Привязать сохранение решения к сессии текущего HTTP-запроса."""

    def apply(suggestion_id: int, review: dict) -> dict:
        """Сохранить решение одной транзакцией сервиса reviews."""
        return reviews.apply_review(session, suggestion_id, review)

    return apply


def start_analysis(
    session: Session,
    ticket_id: int,
    payload: AnalyzeRequest,
    provider_factory: ProviderFactory,
    knowledge_factory: IndexFactory | None,
    checkpointer_factory: CheckpointerFactory,
) -> AgentRunRead:
    """Запустить граф: анализ агентом и остановка в ожидании решения сотрудника.

    Подсказка ключа лицензии передаётся узлу через замыкание и не попадает
    в состояние графа: checkpoint хранится в PostgreSQL открытым JSON.
    """
    checkpointer = checkpointer_factory()
    thread_id = uuid4().hex
    analyzed: dict[str, AgentRunRead] = {}

    def analyze(ticket: int, thread: str) -> dict:
        """Вызвать агента этапов 05–06 и сообщить графу id запуска и предложения."""
        try:
            run = agents.analyze_ticket(
                session,
                ticket,
                payload,
                provider_factory,
                knowledge_factory,
                workflow_thread_id=thread,
            )
        except AgentRunFailure as failure:
            # Неудачный запуск уже сохранён как failed; граф завершается.
            return {
                "outcome": "failed",
                "run_id": failure.run_id,
                "error": {"status_code": failure.status_code, "detail": failure.detail},
            }
        analyzed["run"] = run
        return {
            "outcome": "awaiting_review",
            "run_id": run.id,
            "suggestion_id": run.suggestion.id,
        }

    graph = build_graph(checkpointer)
    context = WorkflowContext(analyze=analyze, apply_review=review_applier(session))
    try:
        state = graph.invoke(
            {"ticket_id": ticket_id, "thread_id": thread_id},
            thread_config(thread_id),
            context=context,
        )
    except Exception:
        # Запуск не начался (нет обращения, неверные настройки, сбой БД):
        # пустой поток не оставляем. Сбой удаления не скрывает исходную ошибку.
        with suppress(Exception):
            checkpointer.delete_thread(thread_id)
        raise
    if state.get("outcome") == "failed":
        error = state["error"]
        raise AgentRunFailure(error["status_code"], error["detail"], state["run_id"])
    return analyzed["run"]


def read_workflow(
    session: Session, run_id: int, checkpointer_factory: CheckpointerFactory
) -> WorkflowRead:
    """Восстановить состояние графа запуска из checkpoint без его продолжения."""
    run = session.get(AgentRun, run_id)
    if run is None:
        raise ResourceNotFound("Запуск агента не найден")
    thread_id = run.workflow_thread_id
    session.rollback()
    if thread_id is None:
        raise ResourceNotFound(NO_WORKFLOW)
    return workflow_snapshot(thread_id, checkpointer_factory)


def workflow_snapshot(
    thread_id: str, checkpointer_factory: CheckpointerFactory
) -> WorkflowRead:
    """Прочитать последний checkpoint потока и назвать понятное состояние."""
    snapshot = build_graph(checkpointer_factory()).get_state(thread_config(thread_id))
    values = snapshot.values or {}
    if not values:
        raise ResourceNotFound("Состояние процесса проверки не найдено")
    following = list(snapshot.next)
    if following == [REVIEW_NODE]:
        state = "awaiting_review"
    elif values.get("outcome") in {"delivered", "rejected", "failed"} and not following:
        state = values["outcome"]
    else:
        state = "running"
    return WorkflowRead(
        thread_id=thread_id,
        state=state,
        next=following,
        run_id=values.get("run_id"),
        suggestion_id=values.get("suggestion_id"),
    )


def review_suggestion(
    session: Session,
    suggestion_id: int,
    request: ReviewRequest,
    checkpointer_factory: CheckpointerFactory,
) -> ReviewResultRead:
    """Применить решение сотрудника: продолжить граф или сохранить напрямую.

    Advisory lock PostgreSQL по id предложения выстраивает параллельные
    решения в очередь: второе увидит, что предложение уже рассмотрено.
    Предложения, созданные до этапа 07, не имеют потока и решаются напрямую.
    """
    review = request.model_dump() | {"review_id": uuid4().hex}
    engine = session.get_bind()
    with Session(engine) as lock_session, lock_session.begin():
        lock_session.execute(select(func.pg_advisory_xact_lock(suggestion_id)))
        run = reviews.check_reviewable(lock_session, suggestion_id, request.reviewer_id)
        thread_id = run.workflow_thread_id
        if thread_id is None:
            reviews.apply_review(session, suggestion_id, review)
        else:
            graph = build_graph(checkpointer_factory())
            config = thread_config(thread_id)
            if graph.get_state(config).next != (REVIEW_NODE,):
                raise ResourceConflict("Процесс проверки не ожидает решения")
            context = WorkflowContext(
                analyze=_no_analysis, apply_review=review_applier(session)
            )
            state = graph.invoke(Command(resume=review), config, context=context)
            # Если граф продолжил другой запрос, наше решение не применялось.
            if state.get("applied_review_id") != review["review_id"]:
                raise ResourceConflict(reviews.ALREADY_REVIEWED)
    return review_result(session, suggestion_id, thread_id, checkpointer_factory)


def _no_analysis(ticket_id: int, thread_id: str) -> dict:
    """Продолжение после решения не запускает анализ повторно."""
    raise RuntimeError("Повторный анализ при продолжении процесса недопустим")


def review_result(
    session: Session,
    suggestion_id: int,
    thread_id: str | None,
    checkpointer_factory: CheckpointerFactory,
) -> ReviewResultRead:
    """Прочитать сохранённое решение, сообщение и статус обращения."""
    suggestion = reviews.get_suggestion(session, suggestion_id)
    message = None
    if suggestion.delivered_message_id is not None:
        message = TicketMessageRead.model_validate(
            session.get(TicketMessage, suggestion.delivered_message_id)
        )
    ticket_status = session.get(Ticket, suggestion.ticket_id).status
    session.rollback()
    workflow = workflow_snapshot(thread_id, checkpointer_factory) if thread_id else None
    return ReviewResultRead(
        suggestion=suggestion,
        message=message,
        ticket_status=ticket_status,
        workflow=workflow,
    )
