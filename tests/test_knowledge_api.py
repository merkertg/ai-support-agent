"""HTTP-поиск по базе знаний без сервера Qdrant: индекс в памяти и заглушка БД."""

from collections.abc import Iterator
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from sqlalchemy.orm import Session

import app.main as application
from app.api.dependencies import get_api_session
from app.api.knowledge import get_knowledge_factory
from app.knowledge.config import DEFAULT_KNOWLEDGE_DIR, get_knowledge_settings
from app.knowledge.documents import load_documents
from app.knowledge.embeddings import MockEmbedder
from app.knowledge.errors import KnowledgeTimeoutError, KnowledgeUnavailableError
from app.knowledge.index import KnowledgeIndex

KNOWLEDGE_ENV = (
    "QDRANT_URL",
    "QDRANT_COLLECTION",
    "EMBEDDING_PROVIDER",
    "OPENAI_EMBEDDING_MODEL",
    "EMBEDDING_DIMENSIONS",
)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path) -> Iterator[None]:
    """Задать явный адрес Qdrant и mock-эмбеддинги вне локального .env."""
    for name in KNOWLEDGE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.test:6333")
    get_knowledge_settings.cache_clear()
    yield
    get_knowledge_settings.cache_clear()


@pytest.fixture
def client_with_index() -> Iterator[tuple[TestClient, list[KnowledgeIndex], Mock]]:
    """Каждый запрос получает новый индекс в памяти с проиндексированными статьями."""
    opened: list[KnowledgeIndex] = []
    documents = load_documents(DEFAULT_KNOWLEDGE_DIR)
    session = Mock(spec=Session)
    session.scalar.return_value = len(documents)

    def factory() -> KnowledgeIndex:
        """Открыть индекс так же, как это делает настоящая фабрика."""
        index = KnowledgeIndex(
            QdrantClient(location=":memory:"), "kb", MockEmbedder(256), min_score=0.2
        )
        index.ensure_collection()
        for document in documents:
            index.replace_document(document)
        opened.append(index)
        return index

    def session_override() -> Iterator[Session]:
        """Вернуть заглушку БД: статус читает только число статей реестра."""
        yield session

    previous = application.app.dependency_overrides.copy()
    application.app.dependency_overrides[get_knowledge_factory] = lambda: factory
    application.app.dependency_overrides[get_api_session] = session_override
    try:
        with TestClient(application.app) as client:
            yield client, opened, session
    finally:
        application.app.dependency_overrides.clear()
        application.app.dependency_overrides.update(previous)


def test_search_returns_scored_fragments_with_sources(client_with_index) -> None:
    """Поиск по продукту находит его статью; ответ содержит путь источника."""
    client, opened, _ = client_with_index
    response = client.post(
        "/api/knowledge/search",
        json={
            "query": "  Сервер не запускается после обновления ",
            "product_code": "techflow-server",
            "limit": 3,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["query"] == "Сервер не запускается после обновления"
    assert body["embedding_signature"] == "mock:mock-hash-v1:256"
    assert 1 <= len(body["results"]) <= 3
    top = body["results"][0]
    assert top["document_key"] == "KB-SERVER-START"
    assert top["source_path"] == "knowledge/kb-server-start.md"
    assert top["scope"] == "techflow-server"
    assert set(top) == {
        "document_key",
        "title",
        "section",
        "text",
        "score",
        "source_path",
        "scope",
    }
    assert len(opened) == 1


def test_other_product_does_not_see_server_articles(client_with_index) -> None:
    """Фильтр по продукту оставляет только его статьи и общие."""
    client, _, _ = client_with_index
    response = client.post(
        "/api/knowledge/search",
        json={"query": "сервер журнал резервная копия", "product_code": "other"},
    )
    assert response.status_code == 200, response.text
    assert all(hit["scope"] == "general" for hit in response.json()["results"])


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"query": "ok"},
        {"query": "я" * 301},
        {"query": "сервер", "limit": 0},
        {"query": "сервер", "limit": 11},
        {"query": "сервер", "limit": "3"},
        {"query": "сервер", "scope": "general"},
        {"query": "сервер", "product_code": ""},
    ],
)
def test_invalid_body_is_rejected_before_index_is_opened(
    client_with_index, body: dict
) -> None:
    """Неверное тело — 422 без обращения к Qdrant и эмбеддингам."""
    client, opened, _ = client_with_index
    response = client.post("/api/knowledge/search", json=body)
    assert response.status_code == 422, response.text
    assert opened == []


@pytest.mark.parametrize("error", [KnowledgeUnavailableError, KnowledgeTimeoutError])
def test_index_errors_are_safe(client_with_index, error: type) -> None:
    """Ошибка Qdrant или тайм-аут дают постоянный текст без деталей."""
    client, _, _ = client_with_index

    def failing():
        """Сымитировать сбой при открытии индекса."""
        raise error()

    application.app.dependency_overrides[get_knowledge_factory] = lambda: failing
    response = client.post("/api/knowledge/search", json={"query": "сервер"})
    assert response.status_code == error.status_code
    assert response.json() == {"detail": error.detail}


def test_missing_qdrant_url_returns_configuration_error(monkeypatch) -> None:
    """Без QDRANT_URL настоящая фабрика отвечает 503 и не обращается к сети."""
    monkeypatch.delenv("QDRANT_URL")
    get_knowledge_settings.cache_clear()
    with TestClient(application.app) as client:
        response = client.post("/api/knowledge/search", json={"query": "сервер"})
    assert response.status_code == 503
    assert "настройки базы знаний" in response.json()["detail"]


def test_status_reports_ready_index_and_registry_count(client_with_index) -> None:
    """Статус показывает число точек Qdrant и статей реестра без эмбеддингов."""
    client, _, session = client_with_index
    response = client.get("/api/knowledge/status")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "configured": True,
        "embedding_provider": "mock",
        "embedding_model": "mock-hash-v1",
        "dimensions": 256,
        "collection": "support_knowledge",
        "index": "ready",
        "points": body["points"],
        "documents": 7,
    }
    assert body["points"] >= body["documents"]
    session.rollback.assert_called_once()


def test_status_reports_unavailable_qdrant_without_failing(client_with_index) -> None:
    """Недоступный Qdrant отражается в статусе, а не превращается в 503."""
    client, _, _ = client_with_index

    def failing():
        """Сымитировать недоступный сервер Qdrant."""
        raise KnowledgeUnavailableError()

    application.app.dependency_overrides[get_knowledge_factory] = lambda: failing
    body = client.get("/api/knowledge/status").json()
    assert body["index"] == "unavailable"
    assert body["points"] is None


def test_status_without_qdrant_url_is_not_configured(client_with_index, monkeypatch):
    """Отсутствие адреса — явное not_configured без попытки соединения."""
    client, opened, _ = client_with_index
    monkeypatch.delenv("QDRANT_URL")
    get_knowledge_settings.cache_clear()
    body = client.get("/api/knowledge/status").json()
    assert (body["configured"], body["index"], body["points"]) == (
        False,
        "not_configured",
        None,
    )
    assert opened == []


def test_openapi_documents_knowledge_operations() -> None:
    """Swagger описывает поиск, статус и ограничения тела запроса."""
    schema = application.app.openapi()
    assert "post" in schema["paths"]["/api/knowledge/search"]
    assert "get" in schema["paths"]["/api/knowledge/status"]
    request = schema["components"]["schemas"]["KnowledgeSearchRequest"]
    assert request["additionalProperties"] is False
    assert request["properties"]["limit"]["maximum"] == 10
