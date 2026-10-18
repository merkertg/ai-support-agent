"""Qdrant-индекс фрагментов: коллекция, запись статьи, удаление и поиск.

Qdrant хранит точки: вектор фрагмента и payload — ключ статьи, раздел,
текст, область (код продукта или general) и подпись пространства векторов.
Поиск сравнивает вектор запроса только с точками той же подписи.
"""

import math
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Self

import httpx
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import ApiException, ResponseHandlingException

from app.knowledge.config import KnowledgeSettings
from app.knowledge.documents import SourceDocument
from app.knowledge.embeddings import (
    STOP_WORDS,
    Embedder,
    MockEmbedder,
    get_embedder,
    is_meaningful,
    stems,
)
from app.knowledge.errors import (
    KnowledgeConfigurationError,
    KnowledgeError,
    KnowledgeIndexMismatchError,
    KnowledgeIndexMissingError,
    KnowledgeTimeoutError,
    KnowledgeUnavailableError,
)

# Постоянное пространство имён: одинаковые ключ и номер дают одинаковый ID точки.
POINT_NAMESPACE = uuid.UUID("6f3f6a52-5b43-4b5e-9a35-6c1f1c7f0a06")
KEYWORD_FIELDS = ("document_key", "scope", "embedding_signature")
SCROLL_PAGE = 256
# Общее имя семейства встречается во всех статьях и не подтверждает их релевантность.
MOCK_BRAND_STEMS = frozenset(stems("TechFlow"))


def shares_lexical_stem(query: str, candidate: str) -> bool:
    """Отделить настоящее совпадение слов от случайной коллизии mock-хеша.

    Векторы и подпись пространства не меняются. Это только дополнительный
    фильтр mock после Qdrant: OpenAI-эмбеддинги могут находить синонимы без
    общих основ, поэтому для них эта проверка никогда не применяется.
    """
    meaningful = set(stems(query)) - STOP_WORDS - MOCK_BRAND_STEMS
    return bool(meaningful & (set(stems(candidate)) - STOP_WORDS))


@dataclass(frozen=True)
class SearchHit:
    """Найденный фрагмент: источник, текст и косинусное сходство с запросом."""

    document_key: str
    title: str
    section: str
    text: str
    score: float
    source_path: str
    scope: str


def point_id(document_key: str, chunk_index: int) -> str:
    """Вычислить ID точки, чтобы повторная индексация перезаписывала ту же точку."""
    return str(uuid.uuid5(POINT_NAMESPACE, f"{document_key}:{chunk_index}"))


@contextmanager
def qdrant_errors() -> Iterator[None]:
    """Заменить ошибки клиента Qdrant постоянными безопасными сообщениями."""
    try:
        yield
    except KnowledgeError:
        raise
    except ResponseHandlingException as error:
        # Клиент оборачивает сетевые ошибки httpx; тайм-аут отделяем от сбоя.
        if isinstance(error.source, httpx.TimeoutException):
            raise KnowledgeTimeoutError() from None
        raise KnowledgeUnavailableError() from None
    except (ApiException, httpx.HTTPError, OSError):
        raise KnowledgeUnavailableError() from None


class KnowledgeIndex:
    """Работать с одной коллекцией через один провайдер эмбеддингов."""

    def __init__(
        self,
        client: QdrantClient,
        collection: str,
        embedder: Embedder,
        *,
        min_score: float,
    ) -> None:
        """Принять готовый клиент: тесты передают Qdrant в памяти процесса."""
        self.client = client
        self.collection = collection
        self.embedder = embedder
        self.min_score = min_score

    def __enter__(self) -> Self:
        """Использовать индекс в with: соединения закроются и при ошибке."""
        return self

    def __exit__(self, *exc_info) -> None:
        """Закрыть HTTP-клиент Qdrant после запроса или индексации."""
        self.close()

    def close(self) -> None:
        """Освободить ресурсы клиента; повторный вызов безопасен."""
        self.client.close()

    def _same_space(self) -> models.FieldCondition:
        """Ограничить точки текущим провайдером, моделью и размерностью."""
        return models.FieldCondition(
            key="embedding_signature",
            match=models.MatchValue(value=self.embedder.signature),
        )

    def _document(self, document_key: str) -> models.FieldCondition:
        """Выбрать все точки одной статьи."""
        return models.FieldCondition(
            key="document_key", match=models.MatchValue(value=document_key)
        )

    def state(self) -> str:
        """Вернуть ready, missing или incompatible без изменения коллекции."""
        with qdrant_errors():
            if not self.client.collection_exists(self.collection):
                return "missing"
            vectors = self.client.get_collection(self.collection).config.params.vectors
        if (
            not isinstance(vectors, models.VectorParams)
            or vectors.size != self.embedder.dimensions
        ):
            return "incompatible"
        return "ready"

    def require_ready(self) -> None:
        """Перед поиском потребовать существующую коллекцию нужной размерности."""
        state = self.state()
        if state == "missing":
            raise KnowledgeIndexMissingError()
        if state == "incompatible":
            raise KnowledgeIndexMismatchError()

    def ensure_collection(self) -> None:
        """Создать коллекцию и индексы payload либо проверить существующую."""
        if self.state() == "missing":
            with qdrant_errors():
                self.client.create_collection(
                    self.collection,
                    vectors_config=models.VectorParams(
                        size=self.embedder.dimensions, distance=models.Distance.COSINE
                    ),
                )
                for field in KEYWORD_FIELDS:
                    self.client.create_payload_index(
                        self.collection,
                        field_name=field,
                        field_schema=models.PayloadSchemaType.KEYWORD,
                    )
        self.require_ready()

    def count(self, document_key: str | None = None) -> int:
        """Посчитать точки текущего пространства: всей коллекции или одной статьи."""
        conditions = [self._same_space()]
        if document_key is not None:
            conditions.append(self._document(document_key))
        with qdrant_errors():
            return self.client.count(
                self.collection,
                count_filter=models.Filter(must=conditions),
                exact=True,
            ).count

    def document_keys(self) -> set[str]:
        """Прочитать ключи всех статей в коллекции, включая статьи без реестра."""
        keys: set[str] = set()
        offset = None
        with qdrant_errors():
            while True:
                points, offset = self.client.scroll(
                    self.collection,
                    limit=SCROLL_PAGE,
                    offset=offset,
                    with_payload=["document_key"],
                    with_vectors=False,
                )
                keys.update(
                    str(point.payload["document_key"])
                    for point in points
                    if point.payload and "document_key" in point.payload
                )
                if offset is None:
                    return keys

    def replace_document(self, document: SourceDocument) -> int:
        """Записать все фрагменты статьи и удалить её лишние старые фрагменты.

        Сначала вычисляются все векторы: сбой эмбеддингов не оставит статью
        наполовину обновлённой. Затем upsert перезаписывает точки с теми же ID,
        а delete убирает фрагменты, номер которых больше нового количества.
        """
        key = document.metadata.document_key
        vectors = self.embedder.embed(
            [document.embedding_text(chunk) for chunk in document.chunks]
        )
        points = [
            models.PointStruct(
                id=point_id(key, chunk.index),
                vector=vector,
                payload={
                    "document_key": key,
                    "title": document.metadata.title,
                    "section": chunk.section,
                    "text": chunk.text,
                    "chunk_index": chunk.index,
                    "scope": document.scope,
                    "source_path": document.source_path,
                    "checksum": document.checksum,
                    "product_version": document.metadata.product_version,
                    "embedding_signature": self.embedder.signature,
                },
            )
            for chunk, vector in zip(document.chunks, vectors, strict=True)
        ]
        with qdrant_errors():
            self.client.upsert(self.collection, points=points, wait=True)
            self.client.delete(
                self.collection,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            self._document(key),
                            models.FieldCondition(
                                key="chunk_index",
                                range=models.Range(gte=len(points)),
                            ),
                        ]
                    )
                ),
                wait=True,
            )
        return len(points)

    def remove_document(self, document_key: str) -> None:
        """Удалить все точки статьи, которой больше нет в каталоге."""
        with qdrant_errors():
            self.client.delete(
                self.collection,
                points_selector=models.FilterSelector(
                    filter=models.Filter(must=[self._document(document_key)])
                ),
                wait=True,
            )

    def search(
        self,
        query: str,
        scopes: list[str] | None,
        limit: int,
        *,
        per_document: bool = False,
    ) -> list[SearchHit]:
        """Найти близкие фрагменты; scopes ограничивает продукт и общие статьи.

        per_document оставляет лучший фрагмент каждой статьи: агенту полезнее
        несколько разных источников, чем пять разделов одной статьи.
        """
        self.require_ready()
        if not is_meaningful(query):
            return []
        vector = self.embedder.embed([query])[0]
        if not any(vector):
            return []
        conditions = [self._same_space()]
        if scopes is not None:
            conditions.append(
                models.FieldCondition(key="scope", match=models.MatchAny(any=scopes))
            )
        with qdrant_errors():
            response = self.client.query_points(
                self.collection,
                query=vector,
                query_filter=models.Filter(must=conditions),
                # Для выбора по статьям запрашиваем запас фрагментов.
                limit=limit * 4 if per_document else limit,
                with_payload=True,
                score_threshold=self.min_score,
            )
        hits: list[SearchHit] = []
        seen: set[str] = set()
        for point in response.points:
            payload = point.payload or {}
            try:
                hit = SearchHit(
                    document_key=str(payload["document_key"]),
                    title=str(payload["title"]),
                    section=str(payload["section"]),
                    text=str(payload["text"]),
                    score=round(float(point.score), 4),
                    source_path=str(payload["source_path"]),
                    scope=str(payload["scope"]),
                )
            except (KeyError, TypeError, ValueError):
                # Повреждённую точку не показываем как найденный источник.
                continue
            if not math.isfinite(hit.score):
                continue
            if isinstance(self.embedder, MockEmbedder) and not shares_lexical_stem(
                query, f"{hit.title}\n{hit.section}\n{hit.text}"
            ):
                # Косинус mock может быть высоким из-за коллизий 256 координат.
                # Дополнительный фильтр требует хотя бы одно реальное общее слово.
                continue
            if per_document:
                if hit.document_key in seen:
                    continue
                seen.add(hit.document_key)
            hits.append(hit)
        return hits[:limit]


def open_index(settings: KnowledgeSettings) -> KnowledgeIndex:
    """Создать клиент Qdrant и провайдера эмбеддингов по проверенным настройкам.

    Конструктор клиента не обращается к серверу: проверка версии отключена,
    поскольку версии клиента и образа Qdrant закреплены вместе.
    """
    if settings.qdrant_url is None:
        raise KnowledgeConfigurationError()
    embedder = get_embedder(settings)
    client = QdrantClient(
        url=settings.qdrant_url,
        timeout=settings.qdrant_timeout_seconds,
        check_compatibility=False,
    )
    return KnowledgeIndex(
        client,
        settings.qdrant_collection,
        embedder,
        min_score=settings.knowledge_min_score,
    )


# Фабрика открывает индекс по требованию: тесты подставляют Qdrant в памяти.
IndexFactory = Callable[[], KnowledgeIndex]
