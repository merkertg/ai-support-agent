"""Публичные данные панели оператора и явное изменение статуса обращения."""

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.resources import CreateSchema, TicketStatus


class TicketStatusUpdate(CreateSchema):
    """Разрешить сотруднику изменить только статус, сохранив остальные поля."""

    status: TicketStatus


class DashboardRead(BaseModel):
    """Вернуть агрегаты SQL; отсутствие измерений не подменяется нулём."""

    model_config = ConfigDict(extra="forbid")
    tickets_total: int = Field(ge=0)
    open_tickets: int = Field(ge=0)
    high_priority_tickets: int = Field(ge=0)
    analyzed_tickets: int = Field(ge=0)
    completed_runs: int = Field(ge=0)
    failed_runs: int = Field(ge=0)
    pending_suggestions: int = Field(ge=0)
    approved_suggestions: int = Field(ge=0)
    rejected_suggestions: int = Field(ge=0)
    average_analysis_seconds: float | None = Field(ge=0)
    approval_rate: float | None = Field(ge=0, le=1)
