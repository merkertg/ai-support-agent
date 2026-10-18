"""Вход анализа и сохранённый результат, который обязательно проверяет человек."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.agent.contracts import ToolTrace
from app.schemas.drafts import AgentResultRead, Provider, TokenUsage
from app.schemas.resources import CreateSchema, LicenseKey


class AnalyzeRequest(CreateSchema):
    """Принять только необязательную подсказку ключа без подмены данных обращения."""

    license_key: LicenseKey | None = None


class SuggestionRead(BaseModel):
    """Показать сохранённое предложение, не выдавая его за отправленный ответ."""

    model_config = ConfigDict(from_attributes=True)
    id: int
    content: str
    status: Literal["pending", "approved", "rejected"]
    requires_human_review: Literal[True] = True


class AgentRunRead(BaseModel):
    """Вернуть итог или ошибку запуска вместе с ограниченной трассой инструментов."""

    id: int
    ticket_id: int
    status: Literal["pending", "running", "completed", "failed"]
    provider: Provider
    model: str | None
    started_at: datetime
    finished_at: datetime | None
    error_message: str | None
    steps: int
    tool_trace: list[ToolTrace]
    usage: TokenUsage | None
    result: AgentResultRead | None
    suggestion: SuggestionRead | None
    # Поток LangGraph процесса проверки; None — запуск создан до этапа 07.
    workflow_thread_id: str | None = None


class AgentErrorRead(BaseModel):
    """Привязать ожидаемую ошибку к сохранённому запуску для последующего GET."""

    detail: str
    run_id: int
