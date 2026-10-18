"""Индекс и поиск на Qdrant в памяти процесса: без сервера, сети и ключей."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from qdrant_client import QdrantClient, models
from sqlalchemy import Engine

import app.agent.tools as tools
from app.agent.contracts import AgentContext
from app.knowledge.config import DEFAULT_KNOWLEDGE_DIR
from app.knowledge.documents import Chunk, load_documents
from app.knowledge.embeddings import MockEmbedder
from app.knowledge.errors import (
    KnowledgeIndexMismatchError,
    KnowledgeIndexMissingError,
    KnowledgeUnavailableError,
)
from app.knowledge.index import KnowledgeIndex, point_id

DOCUMENTS = load_documents(DEFAULT_KNOWLEDGE_DIR)
BY_KEY = {document.metadata.document_key: document for document in DOCUMENTS}


def new_index(dimensions: int = 256, collection: str = "kb") -> KnowledgeIndex:
    """Создать пустой индекс поверх отдельного Qdrant в памяти."""
    return KnowledgeIndex(
        QdrantClient(location=":memory:"),
        collection,
        MockEmbedder(dimensions),
        min_score=0.2,
    )


@pytest.fixture
def index() -> KnowledgeIndex:
    """Проиндексировать все статьи репозитория в новый индекс."""
    value = new_index()
    value.ensure_collection()
    for document in DOCUMENTS:
        value.replace_document(document)
    return value


def test_missing_collection_is_reported_and_search_refused() -> None:
    """До индексации поиск отвечает понятной ошибкой, а не пустым результатом."""
    value = new_index()
    assert value.state() == "missing"
    with pytest.raises(KnowledgeIndexMissingError):
        value.search("сервер", None, 5)


def test_collection_with_other_dimensions_is_incompatible() -> None:
    """Векторы другой размерности нельзя смешивать в одной коллекции."""
    client = QdrantClient(location=":memory:")
    client.create_collection(
        "kb",
        vectors_config=models.VectorParams(size=64, distance=models.Distance.COSINE),
    )
    value = KnowledgeIndex(client, "kb", MockEmbedder(256), min_score=0.2)
    assert value.state() == "incompatible"
    with pytest.raises(KnowledgeIndexMismatchError):
        value.ensure_collection()
    with pytest.raises(KnowledgeIndexMismatchError):
        value.search("сервер", None, 5)


def test_all_chunks_are_indexed_with_deterministic_ids(index) -> None:
    """Каждый фрагмент — одна точка; повторная запись не создаёт дубликатов."""
    total = sum(len(document.chunks) for document in DOCUMENTS)
    assert index.count() == total
    assert index.document_keys() == set(BY_KEY)
    index.replace_document(BY_KEY["KB-SERVER-START"])
    assert index.count() == total
    assert point_id("KB-A", 0) == point_id("KB-A", 0) != point_id("KB-A", 1)


def test_shorter_document_removes_old_tail_chunks(index) -> None:
    """После сокращения статьи старые лишние фрагменты не находятся поиском."""
    document = BY_KEY["KB-SERVER-START"]
    shorter = replace(document, chunks=(Chunk(0, "Кратко", "Сервер не запускается."),))
    assert index.replace_document(shorter) == 1
    assert index.count("KB-SERVER-START") == 1
    index.remove_document("KB-SERVER-START")
    assert index.count("KB-SERVER-START") == 0
    assert "KB-SERVER-START" not in index.document_keys()


def test_product_scope_filters_other_products_but_keeps_general(index) -> None:
    """Статьи другого продукта не попадают в выдачу, общие статьи доступны."""
    query = "Сервер не запускается после обновления"
    server = index.search(query, ["techflow-server", "general"], 5)
    assert server[0].document_key == "KB-SERVER-START"
    assert server[0].source_path == "knowledge/kb-server-start.md"
    desktop = index.search(query, ["techflow-desktop", "general"], 5)
    assert all(hit.scope in {"techflow-desktop", "general"} for hit in desktop)
    license_hits = index.search("лицензия истекла", ["techflow-desktop", "general"], 3)
    assert license_hits[0].document_key == "KB-LICENSE-ACTIVATION"


def test_scores_are_sorted_and_above_threshold(index) -> None:
    """Поиск возвращает результаты по убыванию сходства и не ниже порога."""
    hits = index.search("журнал сервера диагностический архив", None, 10)
    assert hits
    assert [hit.score for hit in hits] == sorted(
        (hit.score for hit in hits), reverse=True
    )
    assert all(hit.score >= 0.2 for hit in hits)
    assert hits[0].document_key == "KB-SERVER-LOGS"


def test_per_document_mode_returns_distinct_articles(index) -> None:
    """Для агента лучший фрагмент каждой статьи, без повторов."""
    hits = index.search("сервер обновление лицензия журнал", None, 5, per_document=True)
    keys = [hit.document_key for hit in hits]
    assert len(keys) == len(set(keys)) >= 2


def test_unrelated_or_empty_query_finds_nothing(index) -> None:
    """Запрос без значимых слов не выполняется, посторонний — ниже порога."""
    assert index.search("и в на что", None, 5) == []
    assert index.search("рецепт борща со сметаной", None, 5) == []


def test_points_from_other_embedding_space_are_ignored(index) -> None:
    """Векторы другой модели той же размерности не участвуют в поиске."""
    other = KnowledgeIndex(index.client, "kb", MockEmbedder(256), min_score=0.2)
    other.embedder.model = "another-model"
    assert other.count() == 0
    assert other.search("Сервер не запускается", None, 5) == []


def test_qdrant_failures_become_safe_errors() -> None:
    """Сетевой сбой клиента не выдаёт адрес и текст исходного исключения."""
    client = Mock(spec=QdrantClient)
    client.collection_exists.side_effect = OSError("connect to http://secret-host")
    value = KnowledgeIndex(client, "kb", MockEmbedder(256), min_score=0.2)
    with pytest.raises(KnowledgeUnavailableError) as error:
        value.state()
    assert "secret-host" not in str(error.value)


def executor(knowledge) -> tools.SQLToolExecutor:
    """Исполнитель с доверенной областью; продукт подставляется тестом."""
    return tools.SQLToolExecutor(
        Mock(spec=Engine),
        AgentContext(1, 2, 3),
        checked_at=datetime(2026, 10, 18, tzinfo=UTC),
        knowledge=knowledge,
    )


def test_search_tool_limits_text_and_returns_distinct_documents(index) -> None:
    """Результат инструмента ограничен по размеру и содержит область поиска."""
    result = executor(lambda: index)._search(
        "Сервер не запускается после обновления", "techflow-server", 3
    )
    assert result["ok"] is True
    documents = result["data"]["documents"]
    assert documents[0]["document_key"] == "KB-SERVER-START"
    assert len({item["document_key"] for item in documents}) == len(documents) <= 3
    assert all(len(item["text"]) <= tools.MAX_SOURCE_CHARACTERS for item in documents)
    assert result["data"]["scope"] == ["techflow-server", "general"]
    size = len(json.dumps(result, ensure_ascii=False))
    assert size < tools.MAX_TOOL_RESULT_CHARACTERS


def test_search_tool_reports_unavailable_knowledge_as_tool_error() -> None:
    """Без настроек или при сбое Qdrant модель получает ошибку, а не исключение."""
    assert executor(None)._search("сервер", "techflow-server", 3)["error"] == {
        "code": "knowledge_unavailable",
        "message": "База знаний не настроена.",
    }
    missing = new_index()
    result = executor(lambda: missing)._search("сервер", "techflow-server", 3)
    assert result["error"]["code"] == "knowledge_unavailable"


def test_mock_hash_collision_cannot_return_unrelated_recipe(index) -> None:
    """Случайная коллизия хеша не превращает рецепт в техническую инструкцию."""
    query = "Рецепт борща со сметаной и свёклой"
    # При размерности 256 исходный cosine действительно даёт ложный результат.
    raw = index.client.query_points(
        index.collection,
        query=index.embedder.embed([query])[0],
        limit=30,
        score_threshold=index.min_score,
        with_payload=True,
    ).points
    assert any(point.payload["document_key"] == "KB-SUPPORT-DATA" for point in raw)
    assert index.search(query, ["techflow-server", "general"], 5) == []


def test_mock_brand_without_content_is_not_evidence(index) -> None:
    """Общий бренд не делает все учебные статьи релевантными пустому вопросу."""
    assert index.search("TechFlow", None, 5) == []
    assert index.search("TechFlow сброс пароля", ["general"], 5)[0].document_key == (
        "KB-PASSWORD-RESET"
    )


def test_lexical_collision_guard_does_not_filter_semantic_provider() -> None:
    """Реальные эмбеддинги вправе находить смысл без общей лексической основы."""

    class SemanticFixtureEmbedder:
        """Вернуть заданный семантический вектор без SDK, сети и API-ключа."""

        dimensions = 64
        signature = "openai:fixture:64"
        provider = "openai"
        model = "fixture"

        def embed(self, texts: list[str]) -> list[list[float]]:
            """Смоделировать одинаковый смысл текстов с разными словами."""
            return [[1.0, *([0.0] * 63)] for _ in texts]

    with KnowledgeIndex(
        QdrantClient(location=":memory:"),
        "semantic_fixture",
        SemanticFixtureEmbedder(),
        min_score=0.2,
    ) as value:
        value.ensure_collection()
        value.replace_document(BY_KEY["KB-PASSWORD-RESET"])
        # Этого слова в статье нет: лексический guard применился бы ошибочно.
        hits = value.search("authentication", ["general"], 5, per_document=True)
        assert [hit.document_key for hit in hits] == ["KB-PASSWORD-RESET"]
