"""Решение сотрудника на настоящей PostgreSQL: LangGraph PostgresSaver и доставка."""

import threading
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent.config import get_agent_settings
from app.api.dependencies import get_api_session
from app.api.reviews import get_checkpointer_factory
from app.knowledge.config import get_knowledge_settings
from app.llm.config import get_llm_settings
from app.main import app
from app.models import (
    AgentRun,
    AgentSuggestion,
    Customer,
    Ticket,
    TicketMessage,
    User,
)
from app.workflow.checkpoint import open_saver

pytestmark = pytest.mark.integration
LICENSE_KEY = "LIC-REVIEW-SECRET"


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[None]:
    """Mock-провайдер, стандартные лимиты и база знаний без адреса Qdrant."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    for name in ("OPENAI_API_KEY", "AGENT_MAX_STEPS", "QDRANT_URL"):
        monkeypatch.delenv(name, raising=False)
    for cache in (get_llm_settings, get_agent_settings, get_knowledge_settings):
        cache.cache_clear()
    yield
    for cache in (get_llm_settings, get_agent_settings, get_knowledge_settings):
        cache.cache_clear()


class Savers:
    """Открывать PostgresSaver тестовой БД; replace() имитирует перезапуск API."""

    def __init__(self, url: str) -> None:
        """Создать первый пул и checkpointer."""
        self.url = url
        self.pool, self.saver = open_saver(url)

    def replace(self) -> None:
        """Закрыть пул и открыть новый: состояние остаётся только в PostgreSQL."""
        self.pool.close()
        self.pool, self.saver = open_saver(self.url)

    def close(self) -> None:
        """Освободить соединения пула после теста."""
        self.pool.close()


@pytest.fixture
def savers(test_database_url: str, migrated_engine: Engine) -> Iterator[Savers]:
    """Checkpointer на тестовой БД после применения всех миграций."""
    value = Savers(test_database_url)
    try:
        yield value
    finally:
        value.close()


@pytest.fixture
def client(migrated_engine: Engine, savers: Savers) -> Iterator[TestClient]:
    """HTTP-клиент: сессии и checkpointer указывают на тестовую БД *_test."""

    def session_override() -> Iterator[Session]:
        """Открыть отдельную сессию тестовой БД на запрос."""
        with Session(migrated_engine) as session:
            yield session

    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_api_session] = session_override
    app.dependency_overrides[get_checkpointer_factory] = lambda: lambda: savers.saver
    try:
        with TestClient(app) as http:
            yield http
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.fixture
def ticket(client: TestClient) -> dict:
    """Создать клиента, продукт и обращение через API."""
    customer = client.post(
        "/api/customers", json={"name": "Клиент", "email": "review@example.com"}
    ).json()
    product = client.post(
        "/api/products", json={"name": "TechFlow Server", "code": "techflow-server"}
    ).json()
    return client.post(
        "/api/tickets",
        json={
            "customer_id": customer["id"],
            "product_id": product["id"],
            "subject": "Сервер не запускается",
            "description": "После обновления служба останавливается.",
        },
    ).json()


def reviewer(client: TestClient, email: str = "operator@example.com") -> dict:
    """Создать сотрудника через API."""
    response = client.post("/api/users", json={"name": "Оператор", "email": email})
    assert response.status_code == 201, response.text
    return response.json()


def analyze(client: TestClient, ticket: dict, body: dict | None = None) -> dict:
    """Запустить граф анализа и вернуть сохранённый запуск."""
    response = client.post(f"/api/tickets/{ticket['id']}/analyze", json=body or {})
    assert response.status_code == 201, response.text
    return response.json()


def test_users_api_creates_lists_and_reads(client: TestClient) -> None:
    """Сотрудник создаётся с ролью operator; повтор email — 409."""
    created = reviewer(client)
    assert created["role"] == "operator" and created["is_active"] is True
    assert client.get(f"/api/users/{created['id']}").json() == created
    assert client.get("/api/users").json() == [created]
    duplicate = client.post(
        "/api/users", json={"name": "Другой", "email": "operator@example.com"}
    )
    assert duplicate.status_code == 409
    assert client.get("/api/users/999").status_code == 404


def test_analysis_waits_for_review_in_postgres_checkpoint(
    client: TestClient, ticket: dict, migrated_engine: Engine
) -> None:
    """После анализа граф остановлен на human_review, checkpoint в PostgreSQL."""
    run = analyze(client, ticket)
    thread_id = run["workflow_thread_id"]
    assert thread_id
    state = client.get(f"/api/agent-runs/{run['id']}/workflow").json()
    assert state == {
        "thread_id": thread_id,
        "state": "awaiting_review",
        "next": ["human_review"],
        "run_id": run["id"],
        "suggestion_id": run["suggestion"]["id"],
    }
    with migrated_engine.connect() as connection:
        stored = connection.execute(
            text("SELECT count(*) FROM checkpoints WHERE thread_id = :thread"),
            {"thread": thread_id},
        ).scalar()
    assert stored > 0
    queue = client.get("/api/suggestions", params={"status": "pending"}).json()
    assert [item["id"] for item in queue] == [run["suggestion"]["id"]]


def test_approval_delivers_edited_reply_and_finishes_workflow(
    client: TestClient, ticket: dict, migrated_engine: Engine
) -> None:
    """Одобрение создаёт сообщение оператора; обращение — waiting_customer."""
    run = analyze(client, ticket)
    user = reviewer(client)
    response = client.post(
        f"/api/suggestions/{run['suggestion']['id']}/review",
        json={
            "decision": "approve",
            "reviewer_id": user["id"],
            "final_content": "Здравствуйте! Проверьте, свободен ли порт 8443.",
            "comment": "Сократил черновик",
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    suggestion = result["suggestion"]
    assert suggestion["status"] == "approved"
    assert suggestion["reviewed_by"] == user["id"]
    assert suggestion["reviewed_at"] is not None
    assert (
        suggestion["final_content"] == "Здравствуйте! Проверьте, свободен ли порт 8443."
    )
    assert suggestion["content"] == run["suggestion"]["content"]
    assert suggestion["review_comment"] == "Сократил черновик"
    message = result["message"]
    assert message["id"] == suggestion["delivered_message_id"]
    assert (message["author_type"], message["user_id"]) == ("operator", user["id"])
    assert message["content"] == suggestion["final_content"]
    assert result["ticket_status"] == "waiting_customer"
    assert result["workflow"]["state"] == "delivered"
    assert result["workflow"]["next"] == []
    assert client.get(f"/api/tickets/{ticket['id']}").json()["status"] == (
        "waiting_customer"
    )
    stored_run = client.get(f"/api/agent-runs/{run['id']}").json()
    assert stored_run["suggestion"]["status"] == "approved"
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(TicketMessage)) == 1


def test_rejection_keeps_ticket_and_creates_no_message(
    client: TestClient, ticket: dict, migrated_engine: Engine
) -> None:
    """Отклонение сохраняет причину; сообщений нет, статус обращения прежний."""
    run = analyze(client, ticket)
    user = reviewer(client)
    response = client.post(
        f"/api/suggestions/{run['suggestion']['id']}/review",
        json={"decision": "reject", "reviewer_id": user["id"], "comment": "Неточно"},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["suggestion"]["status"] == "rejected"
    assert result["suggestion"]["review_comment"] == "Неточно"
    assert result["suggestion"]["final_content"] is None
    assert result["message"] is None
    assert result["ticket_status"] == "open"
    assert result["workflow"]["state"] == "rejected"
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(TicketMessage)) == 0


def test_invalid_reviewer_keeps_workflow_waiting(
    client: TestClient, ticket: dict, migrated_engine: Engine
) -> None:
    """Неизвестный или неактивный сотрудник не расходует ожидание графа."""
    run = analyze(client, ticket)
    suggestion_id = run["suggestion"]["id"]
    missing = client.post(
        f"/api/suggestions/{suggestion_id}/review",
        json={"decision": "approve", "reviewer_id": 999},
    )
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Сотрудник не найден"}
    inactive = reviewer(client, "inactive@example.com")
    with Session(migrated_engine) as session, session.begin():
        session.get(User, inactive["id"]).is_active = False
    blocked = client.post(
        f"/api/suggestions/{suggestion_id}/review",
        json={"decision": "approve", "reviewer_id": inactive["id"]},
    )
    assert blocked.status_code == 409
    workflow = client.get(f"/api/agent-runs/{run['id']}/workflow").json()
    assert workflow["state"] == "awaiting_review"
    active = reviewer(client)
    approved = client.post(
        f"/api/suggestions/{suggestion_id}/review",
        json={"decision": "approve", "reviewer_id": active["id"]},
    )
    assert approved.status_code == 200, approved.text


def test_second_decision_and_unknown_suggestion_are_refused(
    client: TestClient, ticket: dict
) -> None:
    """Повторное решение — 409; неизвестное предложение — 404."""
    run = analyze(client, ticket)
    user = reviewer(client)
    path = f"/api/suggestions/{run['suggestion']['id']}/review"
    first = client.post(path, json={"decision": "approve", "reviewer_id": user["id"]})
    assert first.status_code == 200
    second = client.post(
        path, json={"decision": "reject", "reviewer_id": user["id"], "comment": "Нет"}
    )
    assert second.status_code == 409
    assert second.json() == {"detail": "Предложение уже рассмотрено"}
    unknown = client.post(
        "/api/suggestions/9999/review",
        json={"decision": "approve", "reviewer_id": user["id"]},
    )
    assert unknown.status_code == 404


def test_review_resumes_after_checkpointer_restart(
    client: TestClient, ticket: dict, savers: Savers
) -> None:
    """Новый пул и checkpointer продолжают граф: состояние только в PostgreSQL."""
    run = analyze(client, ticket)
    savers.replace()
    user = reviewer(client)
    response = client.post(
        f"/api/suggestions/{run['suggestion']['id']}/review",
        json={"decision": "approve", "reviewer_id": user["id"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["workflow"]["state"] == "delivered"


def test_parallel_decisions_deliver_exactly_once(
    client: TestClient, ticket: dict, migrated_engine: Engine
) -> None:
    """Два одновременных решения: одно применяется, второе получает 409."""
    run = analyze(client, ticket)
    users = [reviewer(client, f"parallel{index}@example.com") for index in range(2)]
    path = f"/api/suggestions/{run['suggestion']['id']}/review"
    statuses: list[int] = []
    barrier = threading.Barrier(2)

    def decide(user: dict) -> None:
        """Отправить решение одновременно со вторым потоком."""
        barrier.wait()
        statuses.append(
            client.post(
                path, json={"decision": "approve", "reviewer_id": user["id"]}
            ).status_code
        )

    threads = [threading.Thread(target=decide, args=(user,)) for user in users]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(statuses) == [200, 409]
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(TicketMessage)) == 1


def test_failed_analysis_has_failed_workflow_and_no_review(
    client: TestClient, ticket: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Остановка по лимиту: 502 с run_id, граф failed, предложения нет."""
    monkeypatch.setenv("AGENT_MAX_STEPS", "1")
    get_agent_settings.cache_clear()
    response = client.post(f"/api/tickets/{ticket['id']}/analyze")
    assert response.status_code == 502, response.text
    run_id = response.json()["run_id"]
    workflow = client.get(f"/api/agent-runs/{run_id}/workflow").json()
    assert (workflow["state"], workflow["next"]) == ("failed", [])
    assert client.get("/api/suggestions").json() == []


def test_license_hint_is_not_stored_in_checkpoints(
    client: TestClient, ticket: dict, migrated_engine: Engine
) -> None:
    """Подсказка ключа лицензии не попадает ни в одну таблицу checkpoint_*."""
    analyze(client, ticket, {"license_key": LICENSE_KEY})
    with migrated_engine.connect() as connection:
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            dump = connection.execute(
                text(f"SELECT coalesce(string_agg(t::text, ''), '') FROM {table} t")
            ).scalar()
            assert LICENSE_KEY not in dump
            assert LICENSE_KEY.encode().hex() not in dump


def test_legacy_suggestion_without_thread_is_reviewed_directly(
    client: TestClient, ticket: dict, migrated_engine: Engine
) -> None:
    """Предложение до этапа 07 решается без графа; состояния процесса у него нет."""
    with Session(migrated_engine) as session, session.begin():
        run = AgentRun(ticket_id=ticket["id"], status="completed")
        session.add(run)
        session.flush()
        suggestion = AgentSuggestion(agent_run_id=run.id, content="Старый черновик")
        session.add(suggestion)
        session.flush()
        run_id, suggestion_id = run.id, suggestion.id
    workflow = client.get(f"/api/agent-runs/{run_id}/workflow")
    assert workflow.status_code == 404
    user = reviewer(client)
    response = client.post(
        f"/api/suggestions/{suggestion_id}/review",
        json={"decision": "approve", "reviewer_id": user["id"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["workflow"] is None
    assert response.json()["message"]["content"] == "Старый черновик"


def test_saver_setup_after_migration_is_a_no_op(
    savers: Savers, migrated_engine: Engine
) -> None:
    """Миграция записала все версии: setup() библиотеки ничего не меняет."""
    with migrated_engine.connect() as connection:
        before = connection.execute(text("SELECT count(*) FROM checkpoint_migrations"))
        before = before.scalar()
    PostgresSaver(savers.pool).setup()
    with migrated_engine.connect() as connection:
        after = connection.execute(text("SELECT count(*) FROM checkpoint_migrations"))
        assert after.scalar() == before == len(PostgresSaver.MIGRATIONS)


@pytest.mark.parametrize(
    ("status", "values"),
    [
        ("pending", {"final_content": "Текст"}),
        ("pending", {"delivered_message_id": "message"}),
    ],
)
def test_review_fields_are_checked_by_postgres(
    migrated_engine: Engine, ticket: dict, status: str, values: dict
) -> None:
    """Итоговый текст и доставленное сообщение бывают только у одобренного."""
    with Session(migrated_engine) as session, session.begin():
        run = AgentRun(ticket_id=ticket["id"], status="completed")
        session.add(run)
        session.flush()
        message = TicketMessage(
            ticket_id=ticket["id"], author_type="customer", content="Вопрос"
        )
        suggestion = AgentSuggestion(agent_run_id=run.id, content="Черновик")
        session.add_all([message, suggestion])
        session.flush()
        suggestion_id, message_id = suggestion.id, message.id
    if values.get("delivered_message_id") == "message":
        values = {"delivered_message_id": message_id}
    with pytest.raises(IntegrityError), Session(migrated_engine) as session:
        row = session.get(AgentSuggestion, suggestion_id)
        row.status = status
        for name, value in values.items():
            setattr(row, name, value)
        session.commit()


def test_upgrade_from_stage06_keeps_runs_and_suggestions(
    migrated_engine: Engine, migration_config: Config
) -> None:
    """Обновление 0003 → 0004 сохраняет строки; новые поля пустые."""
    command.downgrade(migration_config, "0003_knowledge_index")
    with migrated_engine.begin() as connection:
        customer = connection.execute(
            text(
                "INSERT INTO customers (name, email) "
                "VALUES ('Старый', 'old@example.com') RETURNING id"
            )
        ).scalar_one()
        product = connection.execute(
            text("INSERT INTO products (name, code) VALUES ('P', 'old') RETURNING id")
        ).scalar_one()
        ticket = connection.execute(
            text(
                "INSERT INTO tickets (customer_id, product_id, subject, description) "
                "VALUES (:c, :p, 'Тема', 'Текст') RETURNING id"
            ),
            {"c": customer, "p": product},
        ).scalar_one()
        run = connection.execute(
            text(
                "INSERT INTO agent_runs (ticket_id, status) "
                "VALUES (:t, 'completed') RETURNING id"
            ),
            {"t": ticket},
        ).scalar_one()
        suggestion = connection.execute(
            text(
                "INSERT INTO agent_suggestions (agent_run_id, content) "
                "VALUES (:r, 'Старое предложение') RETURNING id"
            ),
            {"r": run},
        ).scalar_one()
    command.upgrade(migration_config, "head")
    command.check(migration_config)
    with Session(migrated_engine) as session:
        assert session.get(AgentRun, run).workflow_thread_id is None
        row = session.get(AgentSuggestion, suggestion)
        assert (row.status, row.content) == ("pending", "Старое предложение")
        assert (row.final_content, row.delivered_message_id) == (None, None)
        assert session.get(Ticket, ticket) is not None
        assert session.get(Customer, customer) is not None
