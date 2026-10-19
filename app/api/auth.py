"""Текущий сотрудник и управление его API-токенами (только администратор)."""

from fastapi import APIRouter, Depends, status

from app.api.dependencies import ApiSession
from app.api.routes import Limit, Offset, ResourceId
from app.api.security import (
    AUTH_RESPONSES,
    WRITE_RESPONSES,
    CurrentPrincipal,
    admin_reader,
    admin_writer,
    get_principal,
)
from app.schemas.auth import CurrentUserRead, TokenCreate, TokenIssued, TokenRead
from app.schemas.resources import ErrorRead
from app.services import auth

router = APIRouter(
    prefix="/api",
    tags=["Auth"],
    dependencies=[Depends(get_principal)],
    responses=AUTH_RESPONSES
    | {503: {"model": ErrorRead, "description": "База данных временно недоступна"}},
)
NOT_FOUND = {404: {"model": ErrorRead, "description": "Сотрудник или токен не найден"}}


@router.get("/auth/me", response_model=CurrentUserRead)
def read_current_user(principal: CurrentPrincipal) -> CurrentUserRead:
    """Показать, от чьего имени и с какой ролью выполняются запросы токена."""
    return CurrentUserRead(
        id=principal.user_id,
        name=principal.name,
        email=principal.email,
        role=principal.role,
        token_id=principal.token_id,
        token_name=principal.token_name,
        token_expires_at=principal.token_expires_at,
    )


@router.post(
    "/users/{resource_id}/tokens",
    response_model=TokenIssued,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(admin_writer)],
    responses=WRITE_RESPONSES
    | NOT_FOUND
    | {409: {"model": ErrorRead, "description": "Сотрудник неактивен"}},
)
def issue_token(
    resource_id: ResourceId, payload: TokenCreate, session: ApiSession
) -> TokenIssued:
    """Выдать сотруднику токен; секрет в поле token показывается один раз."""
    return auth.issue_token(session, resource_id, payload)


@router.get(
    "/users/{resource_id}/tokens",
    response_model=list[TokenRead],
    dependencies=[Depends(admin_reader)],
    responses={403: WRITE_RESPONSES[403]} | NOT_FOUND,
)
def list_tokens(
    resource_id: ResourceId, session: ApiSession, offset: Offset = 0, limit: Limit = 20
) -> list[TokenRead]:
    """Прочитать токены сотрудника без секретов и хешей."""
    return auth.list_tokens(session, resource_id, offset=offset, limit=limit)


@router.delete(
    "/tokens/{resource_id}",
    response_model=TokenRead,
    dependencies=[Depends(admin_writer)],
    responses=WRITE_RESPONSES | NOT_FOUND,
)
def revoke_token(resource_id: ResourceId, session: ApiSession) -> TokenRead:
    """Отозвать токен: следующий запрос с ним получит 401. Повтор безопасен."""
    return auth.revoke_token(session, resource_id)
