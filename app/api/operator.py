"""HTTP-контракт панели оператора: сервер остаётся владельцем бизнес-данных."""

from fastapi import APIRouter, Depends

from app.api.dependencies import ApiSession
from app.api.routes import FilterId, Limit, Offset, ResourceId
from app.api.security import (
    AUTH_RESPONSES,
    WRITE_RESPONSES,
    get_principal,
    operator_writer,
)
from app.schemas.agents import AgentRunRead
from app.schemas.operator import DashboardRead, TicketStatusUpdate
from app.schemas.resources import ErrorRead, TicketRead
from app.schemas.reviews import TicketMessageRead
from app.services import operator

router = APIRouter(
    prefix="/api",
    tags=["Operator"],
    dependencies=[Depends(get_principal)],
    responses=AUTH_RESPONSES
    | {503: {"model": ErrorRead, "description": "База данных недоступна"}},
)


@router.get("/dashboard", response_model=DashboardRead)
def get_dashboard(session: ApiSession) -> DashboardRead:
    """Прочитать реальные SQL-показатели вместо подсчёта одной страницы UI."""
    return operator.read_dashboard(session)


@router.get("/agent-runs", response_model=list[AgentRunRead])
def get_runs(
    session: ApiSession,
    ticket_id: FilterId = None,
    offset: Offset = 0,
    limit: Limit = 20,
) -> list[AgentRunRead]:
    """Прочитать историю, включая failed/running, без нового вызова модели."""
    return operator.list_runs(session, ticket_id=ticket_id, offset=offset, limit=limit)


@router.get(
    "/tickets/{resource_id}/messages",
    response_model=list[TicketMessageRead],
    responses={404: {"model": ErrorRead, "description": "Обращение не найдено"}},
)
def get_messages(
    resource_id: ResourceId,
    session: ApiSession,
    offset: Offset = 0,
    limit: Limit = 20,
) -> list[TicketMessageRead]:
    """Прочитать сообщения одного обращения с ограничением размера страницы."""
    return operator.list_messages(session, resource_id, offset=offset, limit=limit)


@router.patch(
    "/tickets/{resource_id}",
    response_model=TicketRead,
    dependencies=[Depends(operator_writer)],
    responses=WRITE_RESPONSES
    | {404: {"model": ErrorRead, "description": "Обращение не найдено"}},
)
def patch_ticket(
    resource_id: ResourceId, payload: TicketStatusUpdate, session: ApiSession
) -> TicketRead:
    """Изменить статус только по явному запросу сотрудника; не менять другие поля."""
    return operator.update_ticket_status(session, resource_id, payload)
