"""Строгие формы контекста, черновика и безопасного статуса языковой модели."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.resources import PositiveId, TicketPriority

# Эти значения образуют JSON Schema, которую также получает реальная модель.
DraftCategory = Literal["technical_issue", "license_question", "how_to", "other"]
Provider = Literal["mock", "openai"]
Summary = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]
Reply = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=6000)
]


class DraftContent(BaseModel):
    """Проверить все три обязательных поля ответа и запретить лишние поля."""

    model_config = ConfigDict(extra="forbid")
    summary: Summary
    category: DraftCategory
    draft_reply: Reply


class DraftContext(BaseModel):
    """Передать минимум данных обращения без клиента, почты и ключа лицензии."""

    model_config = ConfigDict(extra="forbid")
    ticket_id: PositiveId
    subject: str
    description: str
    priority: TicketPriority
    product_name: str
    product_code: str


class TokenUsage(BaseModel):
    """Показать только проверенные неотрицательные счётчики токенов."""

    model_config = ConfigDict(extra="forbid")
    input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)
    total_tokens: int = Field(ge=0, strict=True)


class DraftResult(BaseModel):
    """Вернуть проверенный текст и сведения о фактически выбранном провайдере."""

    model_config = ConfigDict(extra="forbid")
    content: DraftContent
    provider: Provider
    model: str
    usage: TokenUsage | None = None


class DraftResponse(DraftResult):
    """Привязать черновик к обращению и явно потребовать проверку человеком."""

    ticket_id: PositiveId
    requires_human_review: Literal[True] = True


class LLMStatus(BaseModel):
    """Показать конфигурацию без ключа, сетевого запроса и проверки баланса."""

    model_config = ConfigDict(extra="forbid")
    provider: Provider
    model: str | None
    configured: bool
