"""Решение сотрудника по предложению агента и учебная доставка ответа.

Одобрение создаёт сообщение обращения от имени сотрудника и переводит
обращение в waiting_customer. Письмо клиенту не отправляется: сообщение
в PostgreSQL и есть «доставка» в этом учебном проекте. Отклонение только
сохраняет решение и причину; обращение не меняется. Закрытое обращение
(resolved или closed) ответ не получает: его предложение можно только отклонить.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentRun, AgentSuggestion, Ticket, TicketMessage, User
from app.schemas.resources import CLOSED_TICKET_STATUSES
from app.schemas.reviews import SuggestionDetailRead, SuggestionStatus
from app.services.errors import ResourceConflict, ResourceNotFound

SUGGESTION_NOT_FOUND = "Предложение агента не найдено"
ALREADY_REVIEWED = "Предложение уже рассмотрено"
TICKET_CLOSED = (
    "Обращение закрыто: одобрение и доставка недоступны. "
    "Отклоните предложение или откройте обращение заново"
)


def suggestion_read(
    suggestion: AgentSuggestion, ticket_id: int
) -> SuggestionDetailRead:
    """Скопировать предложение в DTO вместе с id обращения из запуска."""
    return SuggestionDetailRead(
        id=suggestion.id,
        agent_run_id=suggestion.agent_run_id,
        ticket_id=ticket_id,
        suggestion_type=suggestion.suggestion_type,
        content=suggestion.content,
        status=suggestion.status,
        reviewed_by=suggestion.reviewed_by,
        reviewed_at=suggestion.reviewed_at,
        final_content=suggestion.final_content,
        review_comment=suggestion.review_comment,
        delivered_message_id=suggestion.delivered_message_id,
        created_at=suggestion.created_at,
    )


def list_suggestions(
    session: Session,
    *,
    status: SuggestionStatus | None = None,
    ticket_id: int | None = None,
    offset: int = 0,
    limit: int = 20,
) -> list[SuggestionDetailRead]:
    """Прочитать очередь предложений: фильтры по статусу и обращению."""
    statement = select(AgentSuggestion, AgentRun.ticket_id).join(
        AgentRun, AgentRun.id == AgentSuggestion.agent_run_id
    )
    if status is not None:
        statement = statement.where(AgentSuggestion.status == status)
    if ticket_id is not None:
        statement = statement.where(AgentRun.ticket_id == ticket_id)
    rows = session.execute(
        statement.order_by(AgentSuggestion.id).offset(offset).limit(limit)
    ).all()
    result = [suggestion_read(suggestion, ticket) for suggestion, ticket in rows]
    session.rollback()
    return result


def get_suggestion(session: Session, suggestion_id: int) -> SuggestionDetailRead:
    """Прочитать одно предложение или вернуть 404."""
    row = session.execute(
        select(AgentSuggestion, AgentRun.ticket_id)
        .join(AgentRun, AgentRun.id == AgentSuggestion.agent_run_id)
        .where(AgentSuggestion.id == suggestion_id)
    ).first()
    if row is None:
        raise ResourceNotFound(SUGGESTION_NOT_FOUND)
    result = suggestion_read(*row)
    session.rollback()
    return result


def check_reviewable(
    session: Session, suggestion_id: int, reviewer_id: int, decision: str
) -> AgentRun:
    """До продолжения графа проверить предложение, сотрудника и обращение.

    Проверка выполняется раньше resume: иначе неверный id сотрудника или
    закрытое обращение «израсходовали» бы ожидание графа до отказа в узле.
    """
    suggestion = session.get(AgentSuggestion, suggestion_id)
    if suggestion is None:
        raise ResourceNotFound(SUGGESTION_NOT_FOUND)
    if suggestion.status != "pending":
        raise ResourceConflict(ALREADY_REVIEWED)
    reviewer = session.get(User, reviewer_id)
    if reviewer is None:
        raise ResourceNotFound("Сотрудник не найден")
    if not reviewer.is_active:
        raise ResourceConflict("Сотрудник неактивен и не может рассматривать")
    run = session.get(AgentRun, suggestion.agent_run_id)
    if run is None:
        raise ResourceNotFound(SUGGESTION_NOT_FOUND)
    if decision == "approve":
        ticket = session.get(Ticket, run.ticket_id)
        if ticket.status in CLOSED_TICKET_STATUSES:
            raise ResourceConflict(TICKET_CLOSED)
    return run


def apply_review(session: Session, suggestion_id: int, review: dict) -> dict:
    """Атомарно сохранить решение; при одобрении создать сообщение обращения.

    SELECT … FOR UPDATE блокирует предложение и обращение до commit: второе
    параллельное решение дождётся первого и получит 409, а не двойную доставку.
    Возвращает только JSON-совместимые значения для состояния графа.
    """
    with session.begin():
        suggestion = session.scalar(
            select(AgentSuggestion)
            .where(AgentSuggestion.id == suggestion_id)
            .with_for_update()
        )
        if suggestion is None:
            raise ResourceNotFound(SUGGESTION_NOT_FOUND)
        if suggestion.status != "pending":
            raise ResourceConflict(ALREADY_REVIEWED)
        reviewer = session.get(User, review["reviewer_id"])
        if reviewer is None or not reviewer.is_active:
            raise ResourceNotFound("Сотрудник не найден")
        run = session.get(AgentRun, suggestion.agent_run_id)
        ticket = session.scalar(
            select(Ticket).where(Ticket.id == run.ticket_id).with_for_update()
        )
        if review["decision"] == "approve" and ticket.status in CLOSED_TICKET_STATUSES:
            # Статус мог смениться после предварительной проверки: решает блокировка.
            raise ResourceConflict(TICKET_CLOSED)
        reviewed_at = datetime.now(UTC)
        message_id = None
        if review["decision"] == "approve":
            content = review.get("final_content") or suggestion.content
            message = TicketMessage(
                ticket_id=ticket.id,
                user_id=reviewer.id,
                author_type="operator",
                content=content,
            )
            session.add(message)
            session.flush()
            message_id = message.id
            suggestion.status = "approved"
            suggestion.final_content = content
            suggestion.delivered_message_id = message_id
            # Ответ «отправлен» клиенту: дальше обращение ждёт его реакции.
            ticket.status = "waiting_customer"
        else:
            suggestion.status = "rejected"
        suggestion.reviewed_by = reviewer.id
        suggestion.reviewed_at = reviewed_at
        suggestion.review_comment = review.get("comment")
        ticket_status = ticket.status
        ticket_id = ticket.id
    return {
        "suggestion_id": suggestion_id,
        "ticket_id": ticket_id,
        "message_id": message_id,
        "ticket_status": ticket_status,
    }
