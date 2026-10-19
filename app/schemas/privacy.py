"""Запрос и отчёты обезличивания: клиент по запросу и обращения по сроку хранения."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.schemas.resources import CreateSchema


class CustomerErase(CreateSchema):
    """Подтверждение администратора: текущий email клиента, введённый вручную.

    Так случайный id в запросе не обезличит другого клиента: нужно знать его
    адрес. Сравнение без учёта регистра; адрес не повторяется в ответах.
    """

    confirm_email: Annotated[EmailStr, Field(max_length=320)]


class AnonymizationCounts(BaseModel):
    """Сколько записей изменено; тексты и сами данные в отчёт не входят."""

    model_config = ConfigDict(extra="forbid")
    tickets: int = 0
    messages: int = 0
    agent_runs: int = 0
    suggestions: int = 0
    # Потоки LangGraph, чьи checkpoint удалены: в них были тексты решения.
    workflow_threads: int = 0


class ErasureReport(AnonymizationCounts):
    """Итог обезличивания клиента по его запросу."""

    customer_id: int
    erased_at: datetime
    # Лицензии остаются у обезличенной записи клиента: это учёт продукта.
    licenses_kept: int


class RetentionSkip(BaseModel):
    """Обращение, которое срок хранения не обезличил, и безопасная причина."""

    model_config = ConfigDict(extra="forbid")
    ticket_id: int
    reason: str


class RetentionReport(AnonymizationCounts):
    """Итог прохода срока хранения; при applied=false — только план."""

    applied: bool
    older_than_days: int
    cutoff: datetime
    ticket_ids: list[int]
    skipped: list[RetentionSkip]
