"""API-токены сотрудников: выдача, проверка и отзыв без хранения секрета.

Токен — 256 случайных бит из модуля secrets с префиксом asa_. В БД хранится
только SHA-256: секрет такой длины не подбирается перебором или по словарю,
поэтому медленный хеш паролей (bcrypt, Argon2) не нужен, а токен находится
по уникальному индексу за один запрос. Сроки и отзыв сравниваются со временем
PostgreSQL, а не с часами процесса API.
"""

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ApiToken, User
from app.schemas.auth import TokenCreate, TokenIssued, TokenRead
from app.services.errors import ResourceConflict, ResourceNotFound

TOKEN_PREFIX = "asa_"
# token_urlsafe(32) даёт ровно 43 символа алфавита base64url без «=».
TOKEN_PATTERN = re.compile(r"asa_[A-Za-z0-9_-]{43}")
# «asa_» и 8 случайных символов: достаточно, чтобы узнать токен в списке.
DISPLAY_PREFIX_LENGTH = 12
USER_NOT_FOUND = "Сотрудник не найден"
TOKEN_NOT_FOUND = "Токен не найден"
INACTIVE_USER = "Сотрудник неактивен: токен не выдан"


@dataclass(frozen=True)
class Principal:
    """Проверенный сотрудник запроса; объект не связан с сессией SQLAlchemy."""

    user_id: int
    name: str
    email: str
    role: str
    token_id: int
    token_name: str
    token_expires_at: datetime


class AuthenticationFailed(Exception):
    """Отказ в доступе; причина пишется в журнал, но не возвращается клиенту."""

    def __init__(self, reason: str) -> None:
        """Сохранить короткую причину: malformed, unknown, revoked и т. п."""
        self.reason = reason
        super().__init__(reason)


def generate_token() -> str:
    """Создать новый секрет из криптографически стойкого генератора."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def is_well_formed(token: str) -> bool:
    """Отсеять заведомо чужую строку до обращения к базе данных."""
    return TOKEN_PATTERN.fullmatch(token) is not None


def hash_token(token: str) -> str:
    """Вычислить SHA-256, который хранится в БД вместо самого токена."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def authenticate(session: Session, token: str) -> Principal:
    """Найти действующий токен активного сотрудника или отказать с причиной.

    Транзакция чтения завершается до возврата: обработчик маршрута получает
    сессию без открытой транзакции и может начать свою (session.begin()).
    """
    if not is_well_formed(token):
        raise AuthenticationFailed("malformed")
    try:
        row = session.execute(
            select(ApiToken, User, ApiToken.expires_at > func.now())
            .join(User, User.id == ApiToken.user_id)
            .where(ApiToken.token_hash == hash_token(token))
        ).first()
        if row is None:
            raise AuthenticationFailed("unknown")
        record, user, current = row
        if record.revoked_at is not None:
            raise AuthenticationFailed("revoked")
        if not current:
            raise AuthenticationFailed("expired")
        if not user.is_active:
            raise AuthenticationFailed("inactive")
        return Principal(
            user_id=user.id,
            name=user.name,
            email=user.email,
            role=user.role,
            token_id=record.id,
            token_name=record.name,
            token_expires_at=record.expires_at,
        )
    finally:
        session.rollback()


def issue_token(session: Session, user_id: int, payload: TokenCreate) -> TokenIssued:
    """Выдать токен активному сотруднику и вернуть секрет единственный раз."""
    user = session.get(User, user_id)
    if user is None:
        session.rollback()
        raise ResourceNotFound(USER_NOT_FOUND)
    if not user.is_active:
        session.rollback()
        raise ResourceConflict(INACTIVE_USER)
    secret = generate_token()
    record = ApiToken(
        user_id=user.id,
        name=payload.name,
        token_hash=hash_token(secret),
        token_prefix=secret[:DISPLAY_PREFIX_LENGTH],
        # Срок считает PostgreSQL от того же now(), что и created_at.
        expires_at=func.now() + func.make_interval(0, 0, 0, payload.expires_in_days),
    )
    try:
        session.add(record)
        session.commit()
        session.refresh(record)
    except IntegrityError as error:
        session.rollback()
        # Сотрудника могли удалить между проверкой и INSERT.
        if getattr(error.orig, "sqlstate", None) == "23503":
            raise ResourceNotFound(USER_NOT_FOUND) from None
        raise
    result = TokenIssued(**token_read(record).model_dump(), token=secret)
    session.rollback()
    return result


def token_read(record: ApiToken) -> TokenRead:
    """Скопировать сведения о токене без хеша и секрета."""
    return TokenRead.model_validate(record)


def list_tokens(
    session: Session, user_id: int, *, offset: int = 0, limit: int = 20
) -> list[TokenRead]:
    """Прочитать страницу токенов сотрудника, включая отозванные и истёкшие."""
    if session.get(User, user_id) is None:
        session.rollback()
        raise ResourceNotFound(USER_NOT_FOUND)
    records = session.scalars(
        select(ApiToken)
        .where(ApiToken.user_id == user_id)
        .order_by(ApiToken.id)
        .offset(offset)
        .limit(limit)
    )
    result = [token_read(record) for record in records]
    session.rollback()
    return result


def revoke_token(session: Session, token_id: int) -> TokenRead:
    """Отозвать токен; повторный отзыв ничего не меняет и не считается ошибкой."""
    record = session.scalar(
        select(ApiToken).where(ApiToken.id == token_id).with_for_update()
    )
    if record is None:
        session.rollback()
        raise ResourceNotFound(TOKEN_NOT_FOUND)
    if record.revoked_at is None:
        record.revoked_at = func.now()
    session.commit()
    session.refresh(record)
    result = token_read(record)
    session.rollback()
    return result
