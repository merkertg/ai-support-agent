"""Проверить агента на PostgreSQL: сохранённый запуск, область данных и миграция."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from sqlalchemy import Engine, func, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent.config import get_agent_settings
from app.agent.contracts import AgentContext
from app.agent.errors import AgentStepLimitError, AgentToolLimitError
from app.agent.provider import MOCK_MARKER, MockAgentProvider
from app.agent.tools import SQLToolExecutor, ToolDataUnavailable
from app.api.agents import get_agent_factory
from app.api.dependencies import get_api_session
from app.api.knowledge import get_knowledge_factory
from app.db.base import Base
from app.knowledge.config import DEFAULT_KNOWLEDGE_DIR
from app.knowledge.documents import load_documents
from app.knowledge.embeddings import MockEmbedder
from app.knowledge.index import KnowledgeIndex
from app.llm.config import get_llm_settings
from app.llm.errors import LLMConfigurationError, LLMTimeoutError, LLMUnavailableError
from app.main import app
from app.models import AgentRun, AgentSuggestion, Customer, License, Product, Ticket

pytestmark = pytest.mark.integration
LICENSE_KEY = "LIC-AGENT-PRIVATE"
CUSTOMER_EMAIL = "agent-customer@example.test"
AGENT_TABLES = {"agent_runs", "agent_suggestions"}


@pytest.fixture(autouse=True)
def mock_provider(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[None]:
    """Изолировать тест от локального .env: только mock и стандартные лимиты."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "AGENT_MAX_STEPS",
        "AGENT_MAX_TOOL_CALLS",
        "AGENT_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    get_llm_settings.cache_clear()
    get_agent_settings.cache_clear()
    try:
        yield
    finally:
        get_llm_settings.cache_clear()
        get_agent_settings.cache_clear()


def memory_index_factory(engine: Engine | None = None):
    """Открывать индекс статей репозитория в памяти процесса без сервера Qdrant.

    Если передан engine, фабрика проверяет, что в момент поиска ни одно
    SQL-соединение пула не занято.
    """
    documents = load_documents(DEFAULT_KNOWLEDGE_DIR)

    def factory() -> KnowledgeIndex:
        """Создать заполненный индекс для одного вызова инструмента."""
        if engine is not None:
            assert engine.pool.checkedout() == 0
        index = KnowledgeIndex(
            QdrantClient(location=":memory:"), "kb", MockEmbedder(256), min_score=0.2
        )
        index.ensure_collection()
        for document in documents:
            index.replace_document(document)
        return index

    return factory


@pytest.fixture
def agent_client(migrated_engine: Engine) -> Iterator[tuple[TestClient, list[Session]]]:
    """Выдать каждому запросу отдельную наблюдаемую Session тестовой БД *_test."""
    sessions: list[Session] = []

    def session_override() -> Iterator[Session]:
        """Сохранить ссылку, чтобы проверить отсутствие транзакции во время модели."""
        with Session(migrated_engine) as session:
            sessions.append(session)
            try:
                yield session
            except Exception:
                session.rollback()
                raise

    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_api_session] = session_override
    factory = memory_index_factory(migrated_engine)
    app.dependency_overrides[get_knowledge_factory] = lambda: factory
    try:
        with TestClient(app) as client:
            yield client, sessions
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)


@pytest.fixture
def scope(migrated_engine: Engine) -> dict[str, int]:
    """Создать текущее обращение и соседние записи другого клиента и продукта.

    Текущая область — клиент A и продукт P. Остальные записи нужны, чтобы
    доказать: инструменты не раскрывают чужие лицензии и обращения.
    """
    now = datetime.now(UTC)
    with Session(migrated_engine) as session, session.begin():
        customer = Customer(name="Клиент A", email=CUSTOMER_EMAIL, company="Альфа")
        other_customer = Customer(name="Клиент B", email="other@example.test")
        # Коды совпадают со статьями базы знаний: поиск найдёт статьи продукта.
        product = Product(name="TechFlow Server", code="techflow-server")
        other_product = Product(name="TechFlow Desktop", code="techflow-desktop")
        session.add_all([customer, other_customer, product, other_product])
        session.flush()

        def license_record(key: str, customer_id: int, product_id: int, **values):
            """Добавить лицензию; даты по умолчанию делают её действующей."""
            values.setdefault("issued_at", now - timedelta(days=30))
            values.setdefault("expires_at", now + timedelta(days=335))
            record = License(
                license_key=key,
                customer_id=customer_id,
                product_id=product_id,
                **values,
            )
            session.add(record)
            return record

        license_record(LICENSE_KEY, customer.id, product.id)
        license_record("LIC-A-OTHER-PRODUCT", customer.id, other_product.id)
        license_record("LIC-B-SAME-PRODUCT", other_customer.id, product.id)
        license_record("LIC-A-REVOKED", customer.id, product.id, status="revoked")
        license_record(
            "LIC-A-OVERDUE",
            customer.id,
            product.id,
            issued_at=now - timedelta(days=400),
            expires_at=now - timedelta(days=35),
        )

        def ticket(customer_id: int, product_id: int, subject: str) -> Ticket:
            """Добавить обращение с обязательными полями и открытым статусом."""
            record = Ticket(
                customer_id=customer_id,
                product_id=product_id,
                subject=subject,
                description=f"{subject}: подробности для проверки агента.",
                priority="high",
                status="open",
            )
            session.add(record)
            session.flush()
            return record

        previous = ticket(customer.id, product.id, "Предыдущее обращение A/P")
        other_product_ticket = ticket(customer.id, other_product.id, "Обращение A/Q")
        other_customer_ticket = ticket(other_customer.id, product.id, "Обращение B/P")
        current = ticket(customer.id, product.id, "Сервер не запускается")
        return {
            "customer": customer.id,
            "other_customer": other_customer.id,
            "product": product.id,
            "other_product": other_product.id,
            "ticket": current.id,
            "previous": previous.id,
            "other_product_ticket": other_product_ticket.id,
            "other_customer_ticket": other_customer_ticket.id,
        }


def domain_snapshot(engine: Engine) -> dict[str, list[tuple]]:
    """Прочитать все таблицы, кроме записей агента, в порядке первичных ключей."""
    with engine.connect() as connection:
        return {
            table.name: [
                tuple(row)
                for row in connection.execute(
                    select(table).order_by(*table.primary_key.columns)
                )
            ]
            for table in Base.metadata.sorted_tables
            if table.name not in AGENT_TABLES
        }


def count(engine: Engine, model) -> int:
    """Посчитать строки новой сессией, независимой от HTTP-запроса."""
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(model))


def use_provider(provider) -> None:
    """Подменить провайдера через Depends, как это делает настоящий маршрут."""
    app.dependency_overrides[get_agent_factory] = lambda: lambda: provider


class ObservingProvider(MockAgentProvider):
    """Учебный mock, который проверяет состояние БД перед каждым шагом модели."""

    def __init__(self, engine: Engine, sessions: list[Session]) -> None:
        """Запомнить пул и HTTP-сессии, которые не должны быть заняты моделью."""
        self.engine = engine
        self.sessions = sessions
        self.turns = 0

    def next_turn(self, *args, **kwargs):
        """Во время ожидания модели нет транзакции и занятого соединения пула."""
        assert self.sessions
        assert all(not session.in_transaction() for session in self.sessions)
        assert self.engine.pool.checkedout() == 0
        self.turns += 1
        return super().next_turn(*args, **kwargs)


class FailingProvider(MockAgentProvider):
    """Выполнить первый настоящий шаг mock и прервать второй заданной ошибкой."""

    def __init__(self, error: Exception) -> None:
        """Сохранить ошибку второго шага."""
        self.error = error
        self.turns = 0

    def next_turn(self, *args, **kwargs):
        """Первый шаг вызывает get_ticket, второй имитирует сбой провайдера."""
        self.turns += 1
        if self.turns == 2:
            raise self.error
        return super().next_turn(*args, **kwargs)


def test_mock_analysis_persists_completed_run_and_one_pending_suggestion(
    agent_client: tuple[TestClient, list[Session]],
    scope: dict[str, int],
    migrated_engine: Engine,
) -> None:
    """Успешный запуск сохраняется атомарно, читается GET и не меняет обращение."""
    client, _ = agent_client
    before = domain_snapshot(migrated_engine)
    response = client.post(
        f"/api/tickets/{scope['ticket']}/analyze", json={"license_key": LICENSE_KEY}
    )
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["ticket_id"] == scope["ticket"]
    assert run["status"] == "completed"
    assert run["provider"] == "mock"
    assert run["model"] == "mock-agent-v1"
    assert run["error_message"] is None
    assert run["finished_at"] is not None
    assert run["steps"] == 3
    assert [item["name"] for item in run["tool_trace"]] == [
        "get_ticket",
        "get_customer",
        "get_ticket_history",
        "get_license",
        "search_knowledge",
    ]
    assert all(item["result"]["ok"] is True for item in run["tool_trace"])
    license_call = run["tool_trace"][3]
    assert license_call["arguments"] == {"license_key": "***"}
    assert license_call["result"]["data"]["valid"] is True
    history = run["tool_trace"][2]["result"]["data"]["tickets"]
    assert [item["id"] for item in history] == [scope["previous"]]
    assert set(run["result"]) == {"summary", "category", "draft_reply", "sources"}
    # Ссылки — только на статьи, найденные поиском в этом запуске.
    found = {
        item["document_key"]
        for item in run["tool_trace"][4]["result"]["data"]["documents"]
    }
    assert run["result"]["sources"][0] == "KB-SERVER-START"
    assert set(run["result"]["sources"]) <= found
    assert run["result"]["category"] == "license_question"
    assert MOCK_MARKER in run["result"]["draft_reply"]
    assert "лицензия действующая" in run["result"]["draft_reply"]
    assert run["suggestion"]["status"] == "pending"
    assert run["suggestion"]["requires_human_review"] is True
    assert run["suggestion"]["content"] == run["result"]["draft_reply"]
    # Ни email клиента, ни сырой ключ лицензии не сохраняются и не возвращаются.
    assert CUSTOMER_EMAIL not in response.text
    assert LICENSE_KEY not in response.text

    stored = client.get(f"/api/agent-runs/{run['id']}")
    assert stored.status_code == 200, stored.text
    assert stored.json() == run

    with Session(migrated_engine) as session:
        record = session.get(AgentRun, run["id"])
        assert record.status == "completed"
        suggestions = session.scalars(
            select(AgentSuggestion).where(AgentSuggestion.agent_run_id == run["id"])
        ).all()
        assert len(suggestions) == 1
        assert suggestions[0].status == "pending"
        assert suggestions[0].suggestion_type == "reply"
        assert suggestions[0].reviewed_by is None
        assert suggestions[0].reviewed_at is None
    assert LICENSE_KEY not in json.dumps(record.tool_trace, ensure_ascii=False)
    assert domain_snapshot(migrated_engine) == before


def test_analysis_without_license_hint_does_not_claim_license_check(
    agent_client: tuple[TestClient, list[Session]],
    scope: dict[str, int],
) -> None:
    """Без ключа mock вызывает три инструмента и пишет, что лицензия не проверена."""
    client, _ = agent_client
    response = client.post(f"/api/tickets/{scope['ticket']}/analyze")
    assert response.status_code == 201, response.text
    run = response.json()
    assert [item["name"] for item in run["tool_trace"]] == [
        "get_ticket",
        "get_customer",
        "get_ticket_history",
        "search_knowledge",
    ]
    assert run["result"]["category"] == "technical_issue"
    assert "Лицензия не проверялась" in run["result"]["draft_reply"]


def test_second_analysis_waits_for_pending_suggestion(
    agent_client: tuple[TestClient, list[Session]],
    scope: dict[str, int],
    migrated_engine: Engine,
) -> None:
    """Пока предложение ждёт решения, второй анализ того же обращения — 409.

    Иначе у обращения копились бы черновики, и одобрение каждого доставило бы
    клиенту отдельный ответ.
    """
    client, _ = agent_client
    first = client.post(f"/api/tickets/{scope['ticket']}/analyze")
    second = client.post(f"/api/tickets/{scope['ticket']}/analyze")
    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    suggestion_id = first.json()["suggestion"]["id"]
    assert f"предложение {suggestion_id} на проверке" in second.json()["detail"]
    assert count(migrated_engine, AgentRun) == 1
    assert count(migrated_engine, AgentSuggestion) == 1


def test_running_analysis_blocks_until_it_is_stale(
    agent_client: tuple[TestClient, list[Session]],
    scope: dict[str, int],
    migrated_engine: Engine,
) -> None:
    """Свежий running означает идущий анализ; брошенный после сбоя — не мешает."""
    client, _ = agent_client
    with Session(migrated_engine) as session, session.begin():
        stuck = AgentRun(ticket_id=scope["ticket"], status="running", model="m")
        session.add(stuck)
        session.flush()
        stuck_id = stuck.id
    blocked = client.post(f"/api/tickets/{scope['ticket']}/analyze")
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"] == (
        f"Анализ обращения уже выполняется: запуск {stuck_id}"
    )
    # Старше лимита агента и запаса: процесс, начавший анализ, уже не работает.
    with migrated_engine.begin() as connection:
        connection.execute(
            update(AgentRun)
            .where(AgentRun.id == stuck_id)
            .values(started_at=func.now() - text("interval '1 hour'"))
        )
    resumed = client.post(f"/api/tickets/{scope['ticket']}/analyze")
    assert resumed.status_code == 201, resumed.text
    assert count(migrated_engine, AgentRun) == 2


@pytest.mark.parametrize("status", ["resolved", "closed"])
def test_closed_ticket_is_not_analyzed(
    agent_client: tuple[TestClient, list[Session]],
    scope: dict[str, int],
    migrated_engine: Engine,
    status: str,
) -> None:
    """Закрытое обращение не получает новый запуск и черновик."""
    client, _ = agent_client
    changed = client.patch(f"/api/tickets/{scope['ticket']}", json={"status": status})
    assert changed.status_code == 200, changed.text
    response = client.post(f"/api/tickets/{scope['ticket']}/analyze")
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "Обращение закрыто: анализ недоступен"}
    assert count(migrated_engine, AgentRun) == 0


def test_postgres_connection_is_released_while_provider_waits(
    agent_client: tuple[TestClient, list[Session]],
    scope: dict[str, int],
    migrated_engine: Engine,
) -> None:
    """Ни HTTP-сессия, ни инструменты не держат соединение во время шага модели."""
    client, sessions = agent_client
    provider = ObservingProvider(migrated_engine, sessions)
    use_provider(provider)
    response = client.post(
        f"/api/tickets/{scope['ticket']}/analyze", json={"license_key": LICENSE_KEY}
    )
    assert response.status_code == 201, response.text
    assert provider.turns == 3
    assert migrated_engine.pool.checkedout() == 0


@pytest.mark.parametrize(
    ("variable", "value", "error", "trace_length"),
    [
        # Mock с ключом требует три шага: второй завершается без финала.
        ("AGENT_MAX_STEPS", "2", AgentStepLimitError, 5),
        # Пакет второго шага (четыре вызова) не помещается в лимит и не исполняется.
        ("AGENT_MAX_TOOL_CALLS", "2", AgentToolLimitError, 1),
    ],
)
def test_limit_stop_persists_failed_run_with_partial_progress(
    agent_client: tuple[TestClient, list[Session]],
    scope: dict[str, int],
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
    error: type,
    trace_length: int,
) -> None:
    """Остановка по лимиту сохраняет failed, прогресс и не создаёт предложение."""
    client, _ = agent_client
    monkeypatch.setenv(variable, value)
    get_agent_settings.cache_clear()
    before = domain_snapshot(migrated_engine)
    response = client.post(
        f"/api/tickets/{scope['ticket']}/analyze", json={"license_key": LICENSE_KEY}
    )
    assert response.status_code == error.status_code, response.text
    body = response.json()
    assert body["detail"] == error.detail
    assert isinstance(body["run_id"], int)

    stored = client.get(f"/api/agent-runs/{body['run_id']}")
    assert stored.status_code == 200, stored.text
    run = stored.json()
    assert run["status"] == "failed"
    assert run["error_message"] == error.detail
    assert run["steps"] == 2
    assert len(run["tool_trace"]) == trace_length
    assert run["result"] is None
    assert run["suggestion"] is None
    assert run["finished_at"] is not None
    assert count(migrated_engine, AgentSuggestion) == 0
    # none_as_null хранит отсутствие итога как SQL NULL, а не JSON null.
    with migrated_engine.connect() as connection:
        nulls = connection.execute(
            text("SELECT result IS NULL, usage IS NULL FROM agent_runs WHERE id = :id"),
            {"id": body["run_id"]},
        ).one()
    assert tuple(nulls) == (True, True)
    assert domain_snapshot(migrated_engine) == before


@pytest.mark.parametrize(
    ("raised", "public"),
    [
        (LLMTimeoutError(), LLMTimeoutError()),
        # Неожиданная ошибка не раскрывает свой текст: наружу идёт безопасный 503.
        (RuntimeError("sk-secret-token в сообщении SDK"), LLMUnavailableError()),
    ],
)
def test_provider_failure_after_first_step_keeps_trace_without_suggestion(
    agent_client: tuple[TestClient, list[Session]],
    scope: dict[str, int],
    migrated_engine: Engine,
    raised: Exception,
    public,
) -> None:
    """Сбой второго шага оставляет выполненный get_ticket в сохранённом failed."""
    client, _ = agent_client
    use_provider(FailingProvider(raised))
    response = client.post(f"/api/tickets/{scope['ticket']}/analyze")
    assert response.status_code == public.status_code, response.text
    body = response.json()
    assert body["detail"] == public.detail
    assert "sk-secret-token" not in response.text

    with Session(migrated_engine) as session:
        run = session.get(AgentRun, body["run_id"])
        assert run.status == "failed"
        assert run.error_message == public.detail
        assert run.steps == 2
        assert [item["name"] for item in run.tool_trace] == ["get_ticket"]
        assert run.result is None
    assert count(migrated_engine, AgentSuggestion) == 0


def test_unknown_ticket_returns_404_before_provider_configuration(
    agent_client: tuple[TestClient, list[Session]],
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Неизвестное обращение проверяется раньше ключа и не создаёт запуск."""
    client, _ = agent_client
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    get_llm_settings.cache_clear()
    response = client.post("/api/tickets/9999999/analyze")
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "Обращение не найдено"}
    assert count(migrated_engine, AgentRun) == 0


def test_unconfigured_real_provider_returns_503_without_running_record(
    agent_client: tuple[TestClient, list[Session]],
    scope: dict[str, int],
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Без ключа OpenAI запуск не начинается и фиктивный running не остаётся."""
    client, _ = agent_client
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "explicit-model")
    get_llm_settings.cache_clear()
    response = client.post(f"/api/tickets/{scope['ticket']}/analyze")
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": LLMConfigurationError.detail}
    assert count(migrated_engine, AgentRun) == 0


def test_unknown_run_returns_404(
    agent_client: tuple[TestClient, list[Session]],
) -> None:
    """GET читает только существующую запись и не запускает модель."""
    client, _ = agent_client
    response = client.get("/api/agent-runs/9999999")
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "Запуск агента не найден"}


def executor_for(engine: Engine, scope: dict[str, int]) -> SQLToolExecutor:
    """Создать исполнитель с той же серверной областью, что и сервис анализа."""
    return SQLToolExecutor(
        engine,
        AgentContext(
            ticket_id=scope["ticket"],
            customer_id=scope["customer"],
            product_id=scope["product"],
        ),
    )


@pytest.mark.parametrize(
    ("license_key", "expected"),
    [
        (LICENSE_KEY, True),
        ("LIC-A-REVOKED", False),
        # Статус active не заменяет проверку срока действия.
        ("LIC-A-OVERDUE", False),
        ("LIC-A-OTHER-PRODUCT", None),
        ("LIC-B-SAME-PRODUCT", None),
        ("LIC-DOES-NOT-EXIST", None),
    ],
)
def test_license_tool_checks_validity_only_inside_ticket_scope(
    migrated_engine: Engine, scope: dict[str, int], license_key: str, expected
) -> None:
    """Чужая лицензия неотличима от отсутствующей; срок проверяется отдельно."""
    executor = executor_for(migrated_engine, scope)
    arguments, result = executor(
        "get_license", json.dumps({"license_key": license_key})
    )
    assert arguments == {"license_key": "***"}
    if expected is None:
        assert result == {
            "ok": False,
            "error": {
                "code": "not_found",
                "message": "Лицензия в области обращения не найдена.",
            },
        }
    else:
        assert result["ok"] is True
        assert result["data"]["valid"] is expected
        assert result["data"]["license"]["customer_id"] == scope["customer"]
        assert result["data"]["license"]["product_id"] == scope["product"]


def test_history_and_customer_tools_return_only_scoped_safe_fields(
    migrated_engine: Engine, scope: dict[str, int]
) -> None:
    """История исключает текущее, чужое и другое по продукту; email не выдаётся."""
    executor = executor_for(migrated_engine, scope)
    _, history = executor(
        "get_ticket_history",
        json.dumps({"customer_id": scope["customer"], "limit": 5}),
    )
    assert [item["id"] for item in history["data"]["tickets"]] == [scope["previous"]]
    assert "description" not in history["data"]["tickets"][0]
    _, customer = executor(
        "get_customer", json.dumps({"customer_id": scope["customer"]})
    )
    assert customer["data"]["customer"] == {
        "id": scope["customer"],
        "name": "Клиент A",
        "company": "Альфа",
    }
    _, foreign = executor(
        "get_ticket", json.dumps({"ticket_id": scope["other_customer_ticket"]})
    )
    assert foreign["error"]["code"] == "out_of_scope"


def test_changed_ticket_owner_is_reported_as_stale_scope(
    migrated_engine: Engine, scope: dict[str, int]
) -> None:
    """Если обращение передали другому клиенту, старая область больше не действует."""
    executor = executor_for(migrated_engine, scope)
    with Session(migrated_engine) as session, session.begin():
        session.get(Ticket, scope["ticket"]).customer_id = scope["other_customer"]
    _, result = executor("get_ticket", json.dumps({"ticket_id": scope["ticket"]}))
    assert result["error"]["code"] == "stale_scope"


def test_tool_transaction_is_read_only_at_postgres_level(
    migrated_engine: Engine, scope: dict[str, int]
) -> None:
    """Даже ошибочный код инструмента не может изменить данные: PostgreSQL откажет."""

    class WritingExecutor(SQLToolExecutor):
        """Нарушить правило инструмента, чтобы проверить защиту самой БД."""

        def _customer(self, session: Session) -> dict:
            """Попытаться изменить клиента внутри READ ONLY транзакции."""
            session.execute(update(Customer).values(name="Изменено агентом"))
            return {"ok": True, "data": {}}

    context = executor_for(migrated_engine, scope).context
    executor = WritingExecutor(migrated_engine, context)
    with pytest.raises(ToolDataUnavailable):
        executor("get_customer", json.dumps({"customer_id": scope["customer"]}))
    with Session(migrated_engine) as session:
        assert session.get(Customer, scope["customer"]).name == "Клиент A"


@pytest.mark.parametrize(
    ("attribute", "value"),
    [("provider", "other"), ("steps", -1), ("tool_trace", {"name": "not-array"})],
)
def test_agent_run_constraints_are_enforced_by_postgres(
    migrated_engine: Engine, scope: dict[str, int], attribute: str, value
) -> None:
    """Новые CHECK миграции 0002 работают независимо от Python-проверок."""
    with Session(migrated_engine) as session, session.begin():
        run = AgentRun(ticket_id=scope["ticket"])
        session.add(run)
        session.flush()
        run_id = run.id
    with pytest.raises(IntegrityError), Session(migrated_engine) as session:
        setattr(session.get(AgentRun, run_id), attribute, value)
        session.commit()


def test_upgrade_from_stage02_keeps_existing_runs_and_suggestions(
    migrated_engine: Engine, migration_config: Config
) -> None:
    """Обновление 0001 → 0002 сохраняет строки и задаёт совместимые значения."""
    command.downgrade(migration_config, "0001_stage02_schema")
    new_columns = {"provider", "steps", "tool_trace", "usage", "result"}
    old_columns = {
        column["name"] for column in inspect(migrated_engine).get_columns("agent_runs")
    }
    assert not new_columns & old_columns
    # Модели уже описывают head-схему, поэтому старые строки пишем прямым SQL.
    with migrated_engine.begin() as connection:
        customer_id = connection.execute(
            text(
                "INSERT INTO customers (name, email) "
                "VALUES ('Старый клиент', 'legacy@example.test') RETURNING id"
            )
        ).scalar_one()
        product_id = connection.execute(
            text(
                "INSERT INTO products (name, code) "
                "VALUES ('Старый продукт', 'LEGACY') RETURNING id"
            )
        ).scalar_one()
        ticket_id = connection.execute(
            text(
                "INSERT INTO tickets (customer_id, product_id, subject, description) "
                "VALUES (:customer, :product, 'Старое обращение', 'Текст') RETURNING id"
            ),
            {"customer": customer_id, "product": product_id},
        ).scalar_one()
        run_id = connection.execute(
            text(
                "INSERT INTO agent_runs (ticket_id, status, model) "
                "VALUES (:ticket, 'completed', 'legacy-model') RETURNING id"
            ),
            {"ticket": ticket_id},
        ).scalar_one()
        suggestion_id = connection.execute(
            text(
                "INSERT INTO agent_suggestions (agent_run_id, content) "
                "VALUES (:run, 'Старое предложение') RETURNING id"
            ),
            {"run": run_id},
        ).scalar_one()

    command.upgrade(migration_config, "head")
    command.check(migration_config)
    with Session(migrated_engine) as session:
        run = session.get(AgentRun, run_id)
        assert (run.status, run.model) == ("completed", "legacy-model")
        assert (run.provider, run.steps, run.tool_trace) == ("mock", 0, [])
        assert run.usage is None
        assert run.result is None
        suggestion = session.get(AgentSuggestion, suggestion_id)
        assert suggestion.content == "Старое предложение"
        assert suggestion.status == "pending"

    # Откат удаляет только новые поля; сами записи этапа 02 остаются.
    command.downgrade(migration_config, "0001_stage02_schema")
    old_columns = {
        column["name"] for column in inspect(migrated_engine).get_columns("agent_runs")
    }
    assert not new_columns & old_columns
    with migrated_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM agent_runs")).scalar() == 1
    command.upgrade(migration_config, "head")
