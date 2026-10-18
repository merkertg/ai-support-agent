"""Очередь предложений, решение сотрудника и состояние процесса LangGraph."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import ApiSession
from app.api.routes import FilterId, Limit, Offset, ResourceId
from app.schemas.resources import ErrorRead
from app.schemas.reviews import (
    ReviewRequest,
    ReviewResultRead,
    SuggestionDetailRead,
    SuggestionStatus,
    WorkflowRead,
)
from app.services import reviews
from app.workflow import service as workflow
from app.workflow.checkpoint import get_checkpointer
from app.workflow.service import CheckpointerFactory

router = APIRouter(prefix="/api", tags=["Review"])


def get_checkpointer_factory() -> CheckpointerFactory:
    """Выдать ленивую фабрику checkpointer: тело проверяется раньше соединения."""
    return get_checkpointer


@router.get("/suggestions", response_model=list[SuggestionDetailRead])
def list_suggestions(
    session: ApiSession,
    offset: Offset = 0,
    limit: Limit = 20,
    status: Annotated[SuggestionStatus | None, Query()] = None,
    ticket_id: FilterId = None,
) -> list[SuggestionDetailRead]:
    """Прочитать очередь предложений; status=pending — ожидающие решения."""
    return reviews.list_suggestions(
        session, status=status, ticket_id=ticket_id, offset=offset, limit=limit
    )


@router.get(
    "/suggestions/{resource_id}",
    response_model=SuggestionDetailRead,
    responses={404: {"model": ErrorRead, "description": "Предложение не найдено"}},
)
def get_suggestion(
    resource_id: ResourceId, session: ApiSession
) -> SuggestionDetailRead:
    """Прочитать предложение, решение по нему и id доставленного сообщения."""
    return reviews.get_suggestion(session, resource_id)


@router.post(
    "/suggestions/{resource_id}/review",
    response_model=ReviewResultRead,
    responses={
        404: {
            "model": ErrorRead,
            "description": "Предложение или сотрудник не найдены",
        },
        409: {"model": ErrorRead, "description": "Предложение уже рассмотрено"},
        503: {"model": ErrorRead, "description": "База данных временно недоступна"},
    },
)
def review_suggestion(
    resource_id: ResourceId,
    payload: ReviewRequest,
    session: ApiSession,
    checkpointer_factory: Annotated[
        CheckpointerFactory, Depends(get_checkpointer_factory)
    ],
) -> ReviewResultRead:
    """Одобрить или отклонить предложение и продолжить граф LangGraph.

    Одобрение создаёт сообщение обращения от имени сотрудника (учебная
    доставка, письмо не отправляется) и переводит обращение в waiting_customer.
    """
    return workflow.review_suggestion(
        session, resource_id, payload, checkpointer_factory
    )


@router.get(
    "/agent-runs/{resource_id}/workflow",
    response_model=WorkflowRead,
    responses={
        404: {"model": ErrorRead, "description": "Процесс не найден"},
        409: {
            "model": ErrorRead,
            "description": "Идентификаторы процесса не совпадают",
        },
        503: {"model": ErrorRead, "description": "База данных временно недоступна"},
    },
)
def get_workflow(
    resource_id: ResourceId,
    session: ApiSession,
    checkpointer_factory: Annotated[
        CheckpointerFactory, Depends(get_checkpointer_factory)
    ],
) -> WorkflowRead:
    """Прочитать граф и восстановить checkpoint по БД без повторной доставки."""
    return workflow.read_workflow(session, resource_id, checkpointer_factory)
