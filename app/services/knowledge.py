"""Поиск по базе знаний и состояние индекса для HTTP-маршрутов."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.knowledge.config import get_knowledge_settings
from app.knowledge.documents import GENERAL_SCOPE
from app.knowledge.embeddings import get_embedder
from app.knowledge.errors import KnowledgeConfigurationError, KnowledgeError
from app.knowledge.index import IndexFactory, KnowledgeIndex, open_index
from app.models import KnowledgeDocument
from app.schemas.knowledge import (
    KnowledgeHitRead,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    KnowledgeStatus,
)


def open_configured_index() -> KnowledgeIndex:
    """Открыть индекс по настройкам окружения; без QDRANT_URL — ошибка настройки."""
    return open_index(get_knowledge_settings())


def search_knowledge(
    request: KnowledgeSearchRequest, factory: IndexFactory
) -> KnowledgeSearchResponse:
    """Найти фрагменты; код продукта добавляет к его статьям общие статьи.

    Поиск только читает Qdrant: PostgreSQL здесь не нужен, и SQL-соединение
    не открывается на время запроса эмбеддингов.
    """
    scopes = (
        [request.product_code, GENERAL_SCOPE]
        if request.product_code is not None
        else None
    )
    with factory() as index:
        hits = index.search(request.query, scopes, request.limit)
        signature = index.embedder.signature
    return KnowledgeSearchResponse(
        query=request.query,
        embedding_signature=signature,
        results=[
            KnowledgeHitRead(
                document_key=hit.document_key,
                title=hit.title,
                section=hit.section,
                text=hit.text,
                score=hit.score,
                source_path=hit.source_path,
                scope=hit.scope,
            )
            for hit in hits
        ],
    )


def knowledge_status(session: Session, factory: IndexFactory) -> KnowledgeStatus:
    """Показать настройки, состояние коллекции и число статей в реестре.

    Сбой Qdrant не делает статус ошибкой: ответ сообщает index=unavailable.
    Эмбеддинги при этом не вычисляются, платных запросов нет.
    """
    documents = session.scalar(select(func.count()).select_from(KnowledgeDocument))
    # Количество скопировано: чтение БД закончено до обращения к Qdrant.
    session.rollback()
    settings = get_knowledge_settings()
    try:
        embedder = get_embedder(settings)
        model, dimensions = embedder.model, embedder.dimensions
        configured = settings.qdrant_url is not None
    except KnowledgeConfigurationError:
        model, dimensions, configured = None, settings.embedding_dimensions, False
    state, points = "not_configured", None
    if configured:
        try:
            with factory() as index:
                state = index.state()
                if state == "ready":
                    points = index.count()
        except KnowledgeError:
            state = "unavailable"
    return KnowledgeStatus(
        configured=configured,
        embedding_provider=settings.embedding_provider,
        embedding_model=model,
        dimensions=dimensions,
        collection=settings.qdrant_collection,
        index=state,
        points=points,
        documents=documents or 0,
    )
