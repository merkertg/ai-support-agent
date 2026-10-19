"""HTTP-поиск по базе знаний и состояние индекса Qdrant."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import ApiSession
from app.api.security import (
    AUTH_RESPONSES,
    WRITE_RESPONSES,
    get_principal,
    operator_writer,
)
from app.schemas.knowledge import (
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    KnowledgeStatus,
)
from app.schemas.resources import ErrorRead
from app.services import knowledge
from app.services.knowledge import IndexFactory

router = APIRouter(
    prefix="/api/knowledge",
    tags=["Knowledge"],
    dependencies=[Depends(get_principal)],
    responses=AUTH_RESPONSES,
)


def get_knowledge_factory() -> IndexFactory:
    """Выдать ленивую фабрику индекса: тело запроса проверяется раньше настроек."""
    return knowledge.open_configured_index


@router.get(
    "/status",
    response_model=KnowledgeStatus,
    responses={503: {"model": ErrorRead, "description": "БД или настройки недоступны"}},
)
def get_knowledge_status(
    session: ApiSession,
    factory: Annotated[IndexFactory, Depends(get_knowledge_factory)],
) -> KnowledgeStatus:
    """Проверить коллекцию и реестр без вычисления эмбеддингов."""
    return knowledge.knowledge_status(session, factory)


@router.post(
    "/search",
    response_model=KnowledgeSearchResponse,
    # Поиск ничего не пишет, но может вызывать платные эмбеддинги: тоже под лимитом.
    dependencies=[Depends(operator_writer)],
    responses=WRITE_RESPONSES
    | {
        502: {"model": ErrorRead, "description": "Некорректный ответ эмбеддингов"},
        503: {"model": ErrorRead, "description": "Индекс или Qdrant недоступны"},
        504: {"model": ErrorRead, "description": "Истёк тайм-аут поиска"},
    },
)
def search_knowledge(
    payload: KnowledgeSearchRequest,
    factory: Annotated[IndexFactory, Depends(get_knowledge_factory)],
) -> KnowledgeSearchResponse:
    """Найти фрагменты статей по тексту запроса; ничего не записывает.

    product_code добавляет фильтр: статьи этого продукта и общие статьи.
    Без кода продукта поиск идёт по всем статьям текущего индекса.
    """
    return knowledge.search_knowledge(payload, factory)
