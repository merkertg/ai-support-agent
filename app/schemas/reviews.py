"""Решение сотрудника по предложению агента и состояние процесса LangGraph."""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from app.schemas.drafts import Reply
from app.schemas.resources import (
    CreateSchema,
    ReadSchema,
    StrictPositiveId,
    TicketStatus,
)

ReviewDecision = Literal["approve", "reject"]
SuggestionStatus = Literal["pending", "approved", "rejected"]
WorkflowState = Literal["running", "awaiting_review", "delivered", "rejected", "failed"]
Comment = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]


class ReviewRequest(CreateSchema):
    """Решение: одобрить (с необязательной правкой текста) или отклонить с причиной.

    reviewer_id выбирает сотрудника из таблицы users. Это не авторизация:
    на учебном этапе API не проверяет, кто именно отправил запрос.
    """

    decision: ReviewDecision
    reviewer_id: StrictPositiveId
    final_content: Reply | None = None
    comment: Comment | None = None

    @model_validator(mode="after")
    def decision_fields(self) -> Self:
        """Требовать причину отклонения и запретить правку текста при отказе."""
        if self.decision == "reject":
            if self.comment is None:
                raise ValueError("Для отклонения укажите comment с причиной")
            if self.final_content is not None:
                raise ValueError("final_content допустим только при одобрении")
        return self


class SuggestionDetailRead(ReadSchema):
    """Предложение с обращением, решением сотрудника и доставленным сообщением."""

    id: int
    agent_run_id: int
    ticket_id: int
    suggestion_type: Literal["reply", "action"]
    content: str
    status: SuggestionStatus
    reviewed_by: int | None
    reviewed_at: datetime | None
    final_content: str | None
    review_comment: str | None
    delivered_message_id: int | None
    created_at: datetime


class TicketMessageRead(ReadSchema):
    """Сообщение обращения, созданное учебной доставкой одобренного ответа."""

    id: int
    ticket_id: int
    user_id: int | None
    author_type: Literal["customer", "operator", "agent"]
    content: str
    created_at: datetime


class WorkflowRead(BaseModel):
    """Состояние графа LangGraph, восстановленное из checkpoint в PostgreSQL."""

    model_config = ConfigDict(extra="forbid")
    thread_id: str
    state: WorkflowState
    # Следующий узел графа; ["human_review"] — граф ждёт решения сотрудника.
    next: list[str]
    run_id: int | None
    suggestion_id: int | None


class ReviewResultRead(BaseModel):
    """Итог решения: предложение, сообщение (при одобрении) и статус обращения."""

    model_config = ConfigDict(extra="forbid")
    suggestion: SuggestionDetailRead
    message: TicketMessageRead | None
    ticket_status: TicketStatus
    workflow: WorkflowRead | None
