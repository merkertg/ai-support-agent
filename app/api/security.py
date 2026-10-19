"""Аутентификация по токену сотрудника, проверка роли и лимит изменений.

Каждый маршрут /api/* требует заголовок Authorization: Bearer <токен>.
Открыты только /health, /ready и документация: им не нужны данные клиентов.
Отказ возвращает одинаковый 401 для отсутствующего, чужого, истёкшего и
отозванного токена; причина видна только в журнале API. Сам токен никогда
не записывается в журнал и не возвращается в ответах.
"""

import logging
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.dependencies import ApiSession
from app.api.rate_limit import write_limiter
from app.config import get_settings
from app.observability import get_logger, log_event
from app.schemas.resources import ROLE_LEVELS, ErrorRead, UserRole
from app.services import auth
from app.services.auth import AuthenticationFailed, Principal

NOT_AUTHENTICATED = "Нужен действующий токен сотрудника (Authorization: Bearer …)"
FORBIDDEN = "Недостаточно прав: действие доступно роли {role} и выше"
TOO_MANY_WRITES = "Слишком много изменений подряд: повторите через {seconds} с"
logger = get_logger("auth")

# auto_error=False: отказ формирует этот модуль, чтобы ответ всегда был 401 JSON.
bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="StaffToken",
    description="API-токен сотрудника, выданный администратором (asa_…).",
)

# Общие ответы для Swagger: их возвращают зависимости этого модуля.
# AUTH_RESPONSES описывает любой маршрут /api/*, WRITE_RESPONSES — изменения.
AUTH_RESPONSES: dict[int | str, dict] = {
    401: {"model": ErrorRead, "description": "Нет действующего токена сотрудника"},
}
WRITE_RESPONSES: dict[int | str, dict] = {
    403: {"model": ErrorRead, "description": "Роль не допускает действие"},
    429: {"model": ErrorRead, "description": "Превышен лимит изменений в минуту"},
}


def reject(request: Request, reason: str) -> HTTPException:
    """Записать причину отказа и вернуть одинаковую ошибку 401 клиенту."""
    log_event(logger, logging.INFO, "auth_failed", reason=reason, path=request.url.path)
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=NOT_AUTHENTICATED,
        headers={"WWW-Authenticate": "Bearer"},
    )


def bearer_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> str:
    """Прочитать токен до открытия сессии: без него запрос не трогает БД."""
    if credentials is None:
        raise reject(request, "missing")
    token = credentials.credentials
    if not auth.is_well_formed(token):
        raise reject(request, "malformed")
    return token


def get_principal(
    request: Request,
    token: Annotated[str, Depends(bearer_token)],
    session: ApiSession,
) -> Principal:
    """Найти сотрудника по токену и отметить его id в журнале запроса.

    FastAPI кеширует зависимость в пределах запроса: маршрут, роль и лимит
    получают один и тот же объект без повторного SQL.
    """
    try:
        principal = auth.authenticate(session, token)
    except AuthenticationFailed as failure:
        raise reject(request, failure.reason) from None
    # RequestContextMiddleware добавит user_id в строку журнала этого запроса.
    request.state.user_id = principal.user_id
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def ensure_role(principal: Principal, role: UserRole) -> None:
    """Отказать с 403, если роль сотрудника ниже требуемой."""
    if ROLE_LEVELS.get(principal.role, 0) < ROLE_LEVELS[role]:
        log_event(
            logger,
            logging.INFO,
            "auth_forbidden",
            user_id=principal.user_id,
            role=principal.role,
            required=role,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=FORBIDDEN.format(role=role)
        )


def require_role(role: UserRole) -> Callable[..., Principal]:
    """Создать зависимость чтения, доступную роли role и старшим ролям."""

    def check(principal: CurrentPrincipal) -> Principal:
        """Вернуть сотрудника, если его роль достаточна."""
        ensure_role(principal, role)
        return principal

    return check


def require_writer(role: UserRole) -> Callable[..., Principal]:
    """Создать зависимость изменения: роль, затем лимит частоты сотрудника.

    Отказ по роли не расходует лимит. Тело запроса FastAPI проверяет после
    зависимостей, поэтому неаутентифицированный запрос получает 401, а не 422.
    """

    def check(principal: CurrentPrincipal) -> Principal:
        """Вернуть сотрудника, если роль подходит и лимит не исчерпан."""
        ensure_role(principal, role)
        per_minute = get_settings().write_rate_limit_per_minute
        retry_after = write_limiter.acquire(principal.user_id, per_minute)
        if retry_after is not None:
            log_event(
                logger,
                logging.WARNING,
                "rate_limited",
                user_id=principal.user_id,
                retry_after=retry_after,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=TOO_MANY_WRITES.format(seconds=retry_after),
                headers={"Retry-After": str(retry_after)},
            )
        return principal

    return check


# Готовые зависимости маршрутов. Чтение доступно любому активному сотруднику
# (зависимость get_principal на уровне router), изменения — по ролям ниже.
operator_writer = require_writer("operator")
reviewer_writer = require_writer("reviewer")
admin_writer = require_writer("admin")
admin_reader = require_role("admin")
ReviewerWriter = Annotated[Principal, Depends(reviewer_writer)]
