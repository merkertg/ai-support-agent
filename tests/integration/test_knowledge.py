"""База знаний на настоящих PostgreSQL и Qdrant: индексация, поиск и агент."""

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.orm import Session

from app.agent.config import get_agent_settings
from app.api.dependencies import get_api_session
from app.api.knowledge import get_knowledge_factory
from app.knowledge.config import DEFAULT_KNOWLEDGE_DIR, get_knowledge_settings
from app.knowledge.documents import load_documents
from app.knowledge.ingest import ingest
from app.llm.config import get_llm_settings
from app.main import app
from app.models import Customer, KnowledgeDocument, Product, Ticket

pytestmark = pytest.mark.integration
DOCUMENT_KEYS = {
    document.metadata.document_key for document in load_documents(DEFAULT_KNOWLEDGE_DIR)
}


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[None]:
    """Mock-провайдеры и стандартные лимиты вне локального .env."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    for name in ("OPENAI_API_KEY", "AGENT_MAX_STEPS", "AGENT_MAX_TOOL_CALLS"):
        monkeypatch.delenv(name, raising=False)
    for cache in (get_llm_settings, get_agent_settings, get_knowledge_settings):
        cache.cache_clear()
    yield
    for cache in (get_llm_settings, get_agent_settings, get_knowledge_settings):
        cache.cache_clear()


@pytest.fixture
def knowledge_copy(tmp_path: Path) -> Path:
    """Скопировать статьи репозитория, чтобы тест мог их изменять."""
    target = tmp_path / "knowledge"
    shutil.copytree(DEFAULT_KNOWLEDGE_DIR, target)
    return target


def registry(engine: Engine) -> dict[str, KnowledgeDocument]:
    """Прочитать реестр статей новой сессией."""
    with Session(engine) as session:
        rows = session.scalars(select(KnowledgeDocument)).all()
        session.expunge_all()
        return {row.document_key: row for row in rows}


def test_ingest_indexes_articles_and_registry_matches_qdrant(
    migrated_engine: Engine, index_factory, knowledge_copy: Path
) -> None:
    """Первая индексация записывает все статьи в Qdrant и реестр PostgreSQL."""
    with Session(migrated_engine) as session, session.begin():
        session.add(Product(name="TechFlow Server", code="techflow-server"))
    documents = load_documents(knowledge_copy)
    with index_factory() as index:
        report = ingest(migrated_engine, index, documents)
        assert sorted(report.indexed) == sorted(DOCUMENT_KEYS)
        assert report.unchanged == report.removed == []
        assert index.count() == report.chunks == sum(len(d.chunks) for d in documents)
    rows = registry(migrated_engine)
    assert set(rows) == DOCUMENT_KEYS
    server = rows["KB-SERVER-START"]
    assert server.product_code == "techflow-server"
    # Продукт есть в каталоге — заполнена справочная связь.
    assert server.product_id is not None
    assert rows["KB-DESKTOP-SYNC"].product_id is None
    assert rows["KB-LICENSE-ACTIVATION"].product_code is None
    assert all(
        row.embedding_signature == "mock:mock-hash-v1:256" for row in rows.values()
    )
    assert all(row.indexed_at is not None for row in rows.values())
    assert all(len(row.checksum) == 64 for row in rows.values())


def test_repeated_ingest_changes_nothing(
    migrated_engine: Engine, index_factory, knowledge_copy: Path
) -> None:
    """Повторный запуск без изменений не переписывает векторы и реестр."""
    documents = load_documents(knowledge_copy)
    with index_factory() as index:
        ingest(migrated_engine, index, documents)
    before = {key: row.indexed_at for key, row in registry(migrated_engine).items()}
    with index_factory() as index:
        report = ingest(migrated_engine, index, documents)
    assert report.indexed == report.removed == []
    assert sorted(report.unchanged) == sorted(DOCUMENT_KEYS)
    after = {key: row.indexed_at for key, row in registry(migrated_engine).items()}
    assert after == before


def test_changed_article_is_reindexed_and_old_chunks_removed(
    migrated_engine: Engine, index_factory, knowledge_copy: Path
) -> None:
    """Изменённая статья индексируется заново; лишние старые фрагменты удаляются."""
    with index_factory() as index:
        ingest(migrated_engine, index, load_documents(knowledge_copy))
        old_count = index.count("KB-SERVER-START")
    path = knowledge_copy / "kb-server-start.md"
    header = path.read_text(encoding="utf-8").split("\n---\n", 1)[0]
    path.write_text(
        header + "\n---\n\n## Новая версия\n\nПорт 9443 занят другим процессом.\n",
        encoding="utf-8",
    )
    with index_factory() as index:
        report = ingest(migrated_engine, index, load_documents(knowledge_copy))
        assert report.indexed == ["KB-SERVER-START"]
        assert index.count("KB-SERVER-START") == 1 < old_count
        hits = index.search("порт 9443 занят", ["techflow-server"], 3)
    assert hits[0].document_key == "KB-SERVER-START"
    assert "9443" in hits[0].text
    assert registry(migrated_engine)["KB-SERVER-START"].chunk_count == 1


def test_removed_article_disappears_from_qdrant_and_registry(
    migrated_engine: Engine, index_factory, knowledge_copy: Path
) -> None:
    """Статья, удалённая из каталога, больше не находится и не числится в реестре."""
    with index_factory() as index:
        ingest(migrated_engine, index, load_documents(knowledge_copy))
    (knowledge_copy / "kb-password-reset.md").unlink()
    with index_factory() as index:
        report = ingest(migrated_engine, index, load_documents(knowledge_copy))
        assert report.removed == ["KB-PASSWORD-RESET"]
        assert index.count("KB-PASSWORD-RESET") == 0
        hits = index.search("сброс пароля пользователя", None, 5)
        assert "KB-PASSWORD-RESET" not in {hit.document_key for hit in hits}
    assert "KB-PASSWORD-RESET" not in registry(migrated_engine)


def test_lost_qdrant_points_are_restored_from_unchanged_files(
    migrated_engine: Engine, index_factory, knowledge_copy: Path
) -> None:
    """Если точки пропали из Qdrant, реестр не мешает их восстановить."""
    documents = load_documents(knowledge_copy)
    with index_factory() as index:
        ingest(migrated_engine, index, documents)
        index.remove_document("KB-SERVER-LOGS")
        report = ingest(migrated_engine, index, documents)
        assert report.indexed == ["KB-SERVER-LOGS"]
        assert index.count("KB-SERVER-LOGS") > 0


def test_orphan_points_without_registry_rows_are_removed(
    migrated_engine: Engine, index_factory, knowledge_copy: Path
) -> None:
    """Точки статьи, которой нет ни в каталоге, ни в реестре, тоже удаляются."""
    with index_factory() as index:
        ingest(migrated_engine, index, load_documents(knowledge_copy))
    with Session(migrated_engine) as session, session.begin():
        session.execute(
            KnowledgeDocument.__table__.delete().where(
                KnowledgeDocument.document_key == "KB-BACKUP-RESTORE"
            )
        )
    (knowledge_copy / "kb-backup-restore.md").unlink()
    with index_factory() as index:
        report = ingest(migrated_engine, index, load_documents(knowledge_copy))
        assert report.removed == ["KB-BACKUP-RESTORE"]
        assert "KB-BACKUP-RESTORE" not in index.document_keys()


@pytest.fixture
def api_client(
    migrated_engine: Engine,
    index_factory,
    test_qdrant_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """HTTP-клиент с тестовыми PostgreSQL и коллекцией Qdrant."""
    # Статус берёт признак настройки из окружения, индекс — из фабрики теста.
    monkeypatch.setenv("QDRANT_URL", test_qdrant_url)
    get_knowledge_settings.cache_clear()

    def session_override() -> Iterator[Session]:
        """Открыть сессию тестовой БД *_test на каждый запрос."""
        with Session(migrated_engine) as session:
            yield session

    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_api_session] = session_override
    app.dependency_overrides[get_knowledge_factory] = lambda: index_factory
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def test_http_search_and_status_use_real_qdrant(
    api_client: TestClient, migrated_engine: Engine, index_factory
) -> None:
    """До индексации статус missing; после — ready и поиск по продукту."""
    status = api_client.get("/api/knowledge/status").json()
    assert (status["index"], status["documents"]) == ("missing", 0)
    missing = api_client.post("/api/knowledge/search", json={"query": "сервер"})
    assert missing.status_code == 503
    assert "ingest" in missing.json()["detail"]
    with index_factory() as index:
        report = ingest(migrated_engine, index, load_documents(DEFAULT_KNOWLEDGE_DIR))
    status = api_client.get("/api/knowledge/status").json()
    assert status["index"] == "ready"
    assert status["points"] == report.chunks
    assert status["documents"] == len(DOCUMENT_KEYS)
    response = api_client.post(
        "/api/knowledge/search",
        json={
            "query": "Сервер не запускается после обновления",
            "product_code": "techflow-desktop",
        },
    )
    assert response.status_code == 200, response.text
    # Статья о сервере не видна для другого продукта, общие статьи доступны.
    assert all(hit["scope"] != "techflow-server" for hit in response.json()["results"])
    response = api_client.post(
        "/api/knowledge/search",
        json={"query": "Сервер не запускается после обновления"},
    )
    assert response.json()["results"][0]["document_key"] == "KB-SERVER-START"


def test_agent_cites_articles_found_in_real_qdrant(
    api_client: TestClient, migrated_engine: Engine, index_factory
) -> None:
    """Агент находит статью продукта обращения и сохраняет её ключ в sources."""
    with index_factory() as index:
        ingest(migrated_engine, index, load_documents(DEFAULT_KNOWLEDGE_DIR))
    with Session(migrated_engine) as session, session.begin():
        customer = Customer(name="Клиент", email="kb-agent@example.test")
        product = Product(name="TechFlow Server", code="techflow-server")
        session.add_all([customer, product])
        session.flush()
        ticket = Ticket(
            customer_id=customer.id,
            product_id=product.id,
            subject="Сервер не запускается после обновления",
            description="Служба останавливается через несколько секунд после старта.",
        )
        session.add(ticket)
        session.flush()
        ticket_id = ticket.id
    response = api_client.post(f"/api/tickets/{ticket_id}/analyze")
    assert response.status_code == 201, response.text
    run = response.json()
    search = run["tool_trace"][-1]
    assert search["name"] == "search_knowledge"
    assert search["result"]["ok"] is True
    found = [item["document_key"] for item in search["result"]["data"]["documents"]]
    assert found[0] == "KB-SERVER-START"
    assert run["result"]["sources"][0] == "KB-SERVER-START"
    assert set(run["result"]["sources"]) <= set(found)
    assert "KB-SERVER-START" in run["suggestion"]["content"]
    stored = api_client.get(f"/api/agent-runs/{run['id']}").json()
    assert stored == run


def test_upgrade_from_stage05_keeps_knowledge_rows(
    migrated_engine: Engine, migration_config: Config
) -> None:
    """Обновление 0002 → 0003 сохраняет строки реестра и задаёт значения."""
    command.downgrade(migration_config, "0002_agent_runtime")
    columns = {
        column["name"]
        for column in inspect(migrated_engine).get_columns("knowledge_documents")
    }
    assert "chunk_count" not in columns
    with migrated_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO knowledge_documents (document_key, title, source_path) "
                "VALUES ('KB-LEGACY', 'Старая статья', 'knowledge/legacy.md')"
            )
        )
    command.upgrade(migration_config, "head")
    command.check(migration_config)
    row = registry(migrated_engine)["KB-LEGACY"]
    assert (row.title, row.chunk_count, row.product_code) == ("Старая статья", 0, None)
    assert row.embedding_signature is None
    assert row.indexed_at is None
