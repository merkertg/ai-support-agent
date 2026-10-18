"""История, сообщения и агрегаты панели без прямого доступа UI к PostgreSQL."""

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.models import AgentRun, AgentSuggestion, Ticket, TicketMessage
from app.schemas.agents import AgentRunRead
from app.schemas.operator import DashboardRead, TicketStatusUpdate
from app.schemas.resources import CLOSED_TICKET_STATUSES, TicketRead
from app.schemas.reviews import TicketMessageRead
from app.services.agents import run_read
from app.services.errors import ResourceNotFound


def list_runs(
    session: Session, *, ticket_id: int | None, offset: int, limit: int
) -> list[AgentRunRead]:
    """Прочитать страницу последних запусков и их предложения двумя SQL-запросами."""
    statement = select(AgentRun)
    if ticket_id is not None:
        statement = statement.where(AgentRun.ticket_id == ticket_id)
    runs = session.scalars(
        statement.order_by(AgentRun.id.desc()).offset(offset).limit(limit)
    ).all()
    suggestions = {}
    if runs:
        for suggestion in session.scalars(
            select(AgentSuggestion)
            .where(AgentSuggestion.agent_run_id.in_([run.id for run in runs]))
            .order_by(AgentSuggestion.id)
        ):
            suggestions.setdefault(suggestion.agent_run_id, suggestion)
    result = [run_read(run, suggestions.get(run.id)) for run in runs]
    session.rollback()
    return result


def list_messages(
    session: Session, ticket_id: int, *, offset: int, limit: int
) -> list[TicketMessageRead]:
    """Прочитать только сообщения выбранного обращения в порядке их создания."""
    if session.get(Ticket, ticket_id) is None:
        raise ResourceNotFound("Обращение не найдено")
    messages = session.scalars(
        select(TicketMessage)
        .where(TicketMessage.ticket_id == ticket_id)
        .order_by(TicketMessage.id)
        .offset(offset)
        .limit(limit)
    )
    result = [TicketMessageRead.model_validate(message) for message in messages]
    session.rollback()
    return result


def update_ticket_status(
    session: Session, ticket_id: int, payload: TicketStatusUpdate
) -> TicketRead:
    """Явно сохранить ручной статус; модель не получает такого инструмента."""
    ticket = session.scalar(
        select(Ticket).where(Ticket.id == ticket_id).with_for_update()
    )
    if ticket is None:
        raise ResourceNotFound("Обращение не найдено")
    ticket.status = payload.status
    session.commit()
    session.refresh(ticket)
    result = TicketRead.model_validate(ticket)
    session.rollback()
    return result


def read_dashboard(session: Session) -> DashboardRead:
    """Посчитать показатели на сервере, не выгружая всю историю в Streamlit.

    Открытые — status=open; высокий приоритет — high/critical среди незакрытых.
    Доля одобрения считает только принятые решения. Среднее время включает
    только completed-запуски с обеими корректными временными отметками.
    """
    tickets_total, open_tickets, high_priority = session.execute(
        select(
            func.count(Ticket.id),
            func.count(Ticket.id).filter(Ticket.status == "open"),
            func.count(Ticket.id).filter(
                Ticket.priority.in_(["high", "critical"]),
                Ticket.status.not_in(sorted(CLOSED_TICKET_STATUSES)),
            ),
        )
    ).one()
    completed, failed, analyzed, average = session.execute(
        select(
            func.count(AgentRun.id).filter(AgentRun.status == "completed"),
            func.count(AgentRun.id).filter(AgentRun.status == "failed"),
            func.count(distinct(AgentRun.ticket_id)).filter(
                AgentRun.status == "completed"
            ),
            func.avg(
                func.extract("epoch", AgentRun.finished_at - AgentRun.started_at)
            ).filter(
                AgentRun.status == "completed",
                AgentRun.finished_at >= AgentRun.started_at,
            ),
        )
    ).one()
    pending, approved, rejected = session.execute(
        select(
            func.count(AgentSuggestion.id).filter(AgentSuggestion.status == "pending"),
            func.count(AgentSuggestion.id).filter(AgentSuggestion.status == "approved"),
            func.count(AgentSuggestion.id).filter(AgentSuggestion.status == "rejected"),
        )
    ).one()
    result = DashboardRead(
        tickets_total=tickets_total,
        open_tickets=open_tickets,
        high_priority_tickets=high_priority,
        analyzed_tickets=analyzed,
        completed_runs=completed,
        failed_runs=failed,
        pending_suggestions=pending,
        approved_suggestions=approved,
        rejected_suggestions=rejected,
        average_analysis_seconds=float(average) if average is not None else None,
        approval_rate=approved / (approved + rejected) if approved + rejected else None,
    )
    session.rollback()
    return result
