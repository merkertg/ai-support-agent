"""Токены сотрудников: выдача, список без секретов и текущий пользователь."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.resources import CreateSchema, ReadSchema, UserRole

TokenName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
# Бессрочных токенов нет: утёкший токен перестаёт работать не позже чем через год.
MAX_TOKEN_DAYS = 365
DEFAULT_TOKEN_DAYS = 90
TokenDays = Annotated[int, Field(ge=1, le=MAX_TOKEN_DAYS, strict=True)]


class TokenCreate(CreateSchema):
    """Подпись нового токена и срок его действия в днях (по умолчанию 90)."""

    name: TokenName
    expires_in_days: TokenDays = DEFAULT_TOKEN_DAYS


class TokenRead(ReadSchema):
    """Сведения о токене без секрета: по ним токен находят и отзывают."""

    id: int
    user_id: int
    name: str
    token_prefix: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


class TokenIssued(TokenRead):
    """Только что выданный токен: секрет показывается один раз и не хранится."""

    token: str


class CurrentUserRead(BaseModel):
    """Сотрудник, от имени которого выполняется запрос, и его текущий токен."""

    model_config = ConfigDict(extra="forbid")
    id: int
    name: str
    email: str
    role: UserRole
    token_id: int
    token_name: str
    token_expires_at: datetime
