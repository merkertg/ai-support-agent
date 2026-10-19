"""Решение сотрудника на настоящей PostgreSQL: LangGraph PostgresSaver и доставка."""

import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import Engine, func, select, text, update
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
from app.observability import INTERNAL_ERROR
from app.schemas.reviews import ReviewRequest
from app.services import agents, reviews
from app.services.errors import ResourceForbidden, ResourceNotFound
from app.workflow import service as workflow
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
def client(
    migrated_engine: Engine, savers: Savers, staff_headers: dict[str, str]
) -> Iterator[TestClient]:
    """HTTP-клиент: сессии и checkpointer указывают на тестовую БД *_test."""

    def session_override() -> Iterator[Session]:
        """Открыть отдельную сессию тестовой БД на запрос."""
        with Session(migrated_engine) as session:
            yield session

    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_api_session] = session_override
    app.dependency_overrides[get_checkpointer_factory] = lambda: lambda: savers.saver
    try:
        with TestClient(app, headers=staff_headers) as http:
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


def bearer(token: str) -> dict[str, str]:
    """Заголовок запроса от имени сотрудника с этим токеном."""
    return {"Authorization": f"Bearer {token}"}


def reviewer(
    client: TestClient, email: str = "reviewer@example.com", role: str = "reviewer"
) -> dict:
    """Создать сотрудника и выдать ему токен через API администратора.

    В словаре сотрудника добавлен ключ headers: решение отправляется с этим
    заголовком, и API записывает автором именно этого сотрудника.
    """
    response = client.post(
        "/api/users", json={"name": "Рецензент", "email": email, "role": role}
    )
    assert response.status_code == 201, response.text
    user = response.json()
    issued = client.post(f"/api/users/{user['id']}/tokens", json={"name": "tests"})
    assert issued.status_code == 201, issued.text
    return user | {"headers": bearer(issued.json()["token"])}


def analyze(client: TestClient, ticket: dict, body: dict | None = None) -> dict:
    """Запустить граф анализа и вернуть сохранённый запуск."""
    response = client.post(f"/api/tickets/{ticket['id']}/analyze", json=body or {})
    assert response.status_code == 201, response.text
    return response.json()


def set_ticket_status(client: TestClient, ticket: dict, status: str) -> None:
    """Сменить статус обращения так же, как это делает сотрудник в интерфейсе."""
    response = client.patch(f"/api/tickets/{ticket['id']}", json={"status": status})
    assert response.status_code == 200, response.text


def message_count(engine: Engine) -> int:
    """Посчитать все сообщения обращений в тестовой БД."""
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(TicketMessage))


def test_users_api_creates_lists_and_reads(client: TestClient) -> None:
    """Сотрудник создаётся с ролью operator по умолчанию; повтор email — 409."""
    response = client.post(
        "/api/users", json={"name": "Оператор", "email": "operator@example.com"}
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["role"] == "operator" and created["is_active"] is True
    assert client.get(f"/api/users/{created['id']}").json() == created
    # Первый сотрудник — администратор, чей токен использует клиент теста.
    listed = client.get("/api/users").json()
    assert [user["role"] for user in listed] == ["admin", "operator"]
    assert listed[1] == created
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
            "final_content": "Здравствуйте! Проверьте, свободен ли порт 8443.",
            "comment": "Сократил черновик",
        },
        headers=user["headers"],
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
        json={"decision": "reject", "comment": "Неточно"},
        headers=user["headers"],
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


def test_unauthorized_reviewers_keep_workflow_waiting(
    client: TestClient, ticket: dict, migrated_engine: Engine
) -> None:
    """Без токена, с ролью operator или у неактивного сотрудника решения нет.

    Ни один отказ не расходует ожидание графа: после него рецензент
    по-прежнему может принять решение.
    """
    run = analyze(client, ticket)
    path = f"/api/suggestions/{run['suggestion']['id']}/review"
    anonymous = client.post(
        path, json={"decision": "approve"}, headers={"Authorization": ""}
    )
    assert anonymous.status_code == 401
    assert anonymous.headers["WWW-Authenticate"] == "Bearer"
    operator = reviewer(client, "operator@example.com", role="operator")
    forbidden = client.post(
        path, json={"decision": "approve"}, headers=operator["headers"]
    )
    assert forbidden.status_code == 403, forbidden.text
    inactive = reviewer(client, "inactive@example.com")
    with Session(migrated_engine) as session, session.begin():
        session.get(User, inactive["id"]).is_active = False
    blocked = client.post(
        path, json={"decision": "approve"}, headers=inactive["headers"]
    )
    assert blocked.status_code == 401
    spoofed = client.post(
        path,
        json={"decision": "approve", "reviewer_id": inactive["id"]},
        headers=operator["headers"],
    )
    assert spoofed.status_code == 403
    workflow = client.get(f"/api/agent-runs/{run['id']}/workflow").json()
    assert workflow["state"] == "awaiting_review"
    assert message_count(migrated_engine) == 0
    active = reviewer(client)
    # Правильный токен не позволяет подставить другого автора в теле запроса.
    impostor = client.post(
        path,
        json={"decision": "approve", "reviewer_id": operator["id"]},
        headers=active["headers"],
    )
    assert impostor.status_code == 422
    approved = client.post(
        path, json={"decision": "approve"}, headers=active["headers"]
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["suggestion"]["reviewed_by"] == active["id"]
    assert approved.json()["message"]["user_id"] == active["id"]


def test_service_rechecks_reviewer_in_database(
    client: TestClient, ticket: dict, savers: Savers, migrated_engine: Engine
) -> None:
    """Сервис под блокировкой перечитывает сотрудника: роль и существование."""
    run = analyze(client, ticket)
    operator = reviewer(client, "operator@example.com", role="operator")
    request = ReviewRequest(decision="approve")
    with Session(migrated_engine) as session:
        with pytest.raises(ResourceForbidden):
            workflow.review_suggestion(
                session,
                run["suggestion"]["id"],
                request,
                lambda: savers.saver,
                operator["id"],
            )
        with pytest.raises(ResourceNotFound):
            workflow.review_suggestion(
                session, run["suggestion"]["id"], request, lambda: savers.saver, 999
            )
    state = client.get(f"/api/agent-runs/{run['id']}/workflow").json()
    assert state["state"] == "awaiting_review"


def test_second_decision_and_unknown_suggestion_are_refused(
    client: TestClient, ticket: dict
) -> None:
    """Повторное решение — 409; неизвестное предложение — 404."""
    run = analyze(client, ticket)
    user = reviewer(client)
    path = f"/api/suggestions/{run['suggestion']['id']}/review"
    first = client.post(path, json={"decision": "approve"}, headers=user["headers"])
    assert first.status_code == 200
    second = client.post(
        path,
        json={"decision": "reject", "comment": "Нет"},
        headers=user["headers"],
    )
    assert second.status_code == 409
    assert second.json() == {"detail": "Предложение уже рассмотрено"}
    unknown = client.post(
        "/api/suggestions/9999/review",
        json={"decision": "approve"},
        headers=user["headers"],
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
        json={"decision": "approve"},
        headers=user["headers"],
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
                path, json={"decision": "approve"}, headers=user["headers"]
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
        json={"decision": "approve"},
        headers=user["headers"],
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


def test_missing_checkpoint_is_restored_before_review(
    client: TestClient, ticket: dict, savers: Savers, migrated_engine: Engine
) -> None:
    """Потеря checkpoint не теряет pending: GET восстанавливает ожидание решения."""
    run = analyze(client, ticket)
    savers.saver.delete_thread(run["workflow_thread_id"])
    response = client.get(f"/api/agent-runs/{run['id']}/workflow")
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "awaiting_review"
    user = reviewer(client)
    approved = client.post(
        f"/api/suggestions/{run['suggestion']['id']}/review",
        json={"decision": "approve"},
        headers=user["headers"],
    )
    assert approved.status_code == 200, approved.text
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(AgentRun)) == 1
        assert session.scalar(select(func.count()).select_from(TicketMessage)) == 1


def fail_saver_when_armed(monkeypatch: pytest.MonkeyPatch, savers: Savers) -> dict:
    """Подменить все записи saver: флаг включается после бизнес-commit."""
    state = {"failed": False}
    original_put, original_writes = savers.saver.put, savers.saver.put_writes

    def put(*args, **kwargs):
        """Заменить только запись checkpoint после включения тестового отказа."""
        if state["failed"]:
            raise OSError("simulated checkpoint outage")
        return original_put(*args, **kwargs)

    def put_writes(*args, **kwargs):
        """Отклонить pending writes: сохранённый результат не скроет отказ."""
        if state["failed"]:
            raise OSError("simulated checkpoint outage")
        return original_writes(*args, **kwargs)

    monkeypatch.setattr(savers.saver, "put", put)
    monkeypatch.setattr(savers.saver, "put_writes", put_writes)
    return state


def test_checkpoint_outage_after_analysis_preserves_durable_run(
    client: TestClient,
    ticket: dict,
    savers: Savers,
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Отказ checkpoint после commit анализа восстанавливается без второго агента."""
    fault = fail_saver_when_armed(monkeypatch, savers)
    original_analyze = agents.analyze_ticket

    def committed_analysis(*args, **kwargs):
        """Завершить настоящий анализ до отказа сохранения состояния графа."""
        result = original_analyze(*args, **kwargs)
        fault["failed"] = True
        return result

    monkeypatch.setattr(agents, "analyze_ticket", committed_analysis)
    # Непредвиденный отказ хранилища: безопасный JSON 500 с id запроса.
    outage = client.post(f"/api/tickets/{ticket['id']}/analyze")
    assert outage.status_code == 500, outage.text
    assert outage.json() == {"detail": INTERNAL_ERROR}
    assert outage.headers["X-Request-ID"]
    fault["failed"] = False
    with Session(migrated_engine) as session:
        run = session.scalar(select(AgentRun))
        run_id, thread_id = run.id, run.workflow_thread_id
        assert run.status == "completed"
        assert session.scalar(select(func.count()).select_from(AgentRun)) == 1
    assert savers.saver.get_tuple({"configurable": {"thread_id": thread_id}})
    state = client.get(f"/api/agent-runs/{run_id}/workflow")
    assert state.status_code == 200, state.text
    assert state.json()["state"] == "awaiting_review"
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(AgentRun)) == 1
        assert session.scalar(select(func.count()).select_from(AgentSuggestion)) == 1
        assert session.scalar(select(func.count()).select_from(TicketMessage)) == 0


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_checkpoint_outage_after_review_does_not_repeat_delivery(
    client: TestClient,
    ticket: dict,
    savers: Savers,
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
) -> None:
    """После commit решения GET восстанавливает итог без второго сообщения."""
    from app.services import reviews

    run, user = analyze(client, ticket), reviewer(client)
    fault = fail_saver_when_armed(monkeypatch, savers)
    original_apply = reviews.apply_review

    def committed_review(*args, **kwargs):
        """Сохранить решение в реальной БД, затем выключить записи checkpointer."""
        result = original_apply(*args, **kwargs)
        fault["failed"] = True
        return result

    monkeypatch.setattr(reviews, "apply_review", committed_review)
    path = f"/api/suggestions/{run['suggestion']['id']}/review"
    body = {"decision": decision, "comment": "Проверено"}
    outage = client.post(path, json=body, headers=user["headers"])
    assert outage.status_code == 500, outage.text
    assert outage.json() == {"detail": INTERNAL_ERROR}
    fault["failed"] = False
    state = client.get(f"/api/agent-runs/{run['id']}/workflow")
    assert state.status_code == 200, state.text
    assert state.json()["state"] == (
        "delivered" if decision == "approve" else "rejected"
    )
    assert state.json()["next"] == []
    assert client.post(path, json=body, headers=user["headers"]).status_code == 409
    with Session(migrated_engine) as session:
        expected = 1 if decision == "approve" else 0
        assert (
            session.scalar(select(func.count()).select_from(TicketMessage)) == expected
        )
        assert session.get(AgentSuggestion, run["suggestion"]["id"]).status == (
            "approved" if decision == "approve" else "rejected"
        )


def test_review_recovers_pending_checkpoint_without_prior_get(
    client: TestClient, ticket: dict, savers: Savers, migrated_engine: Engine
) -> None:
    """POST review сам восстанавливает пропавшее ожидание, не требуя отдельного GET."""
    run, user = analyze(client, ticket), reviewer(client)
    savers.saver.delete_thread(run["workflow_thread_id"])
    response = client.post(
        f"/api/suggestions/{run['suggestion']['id']}/review",
        json={"decision": "approve"},
        headers=user["headers"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["workflow"]["state"] == "delivered"
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(TicketMessage)) == 1


def test_pending_after_rolled_back_review_can_receive_new_decision(
    client: TestClient,
    ticket: dict,
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ошибка БД до commit не расходует ожидание: новый POST может отклонить ответ."""
    from psycopg import OperationalError

    from app.services import reviews

    run, user = analyze(client, ticket), reviewer(client)
    original_apply = reviews.apply_review

    def failed_transaction(*args, **kwargs):
        """Имитировать недоступность БД до сохранения решения сотрудника."""
        raise OperationalError("simulated database outage")

    monkeypatch.setattr(reviews, "apply_review", failed_transaction)
    path = f"/api/suggestions/{run['suggestion']['id']}/review"
    failed = client.post(path, json={"decision": "approve"}, headers=user["headers"])
    assert failed.status_code == 503, failed.text
    monkeypatch.setattr(reviews, "apply_review", original_apply)
    retried = client.post(
        path,
        json={"decision": "reject", "comment": "Исправить"},
        headers=user["headers"],
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["workflow"]["state"] == "rejected"
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(TicketMessage)) == 0
        assert (
            session.get(AgentSuggestion, run["suggestion"]["id"]).status == "rejected"
        )


def test_closed_ticket_analysis_is_refused_without_checkpoint(
    client: TestClient, ticket: dict, migrated_engine: Engine
) -> None:
    """Отказ происходит в узле analyze до записи: поток графа удаляется."""
    set_ticket_status(client, ticket, "closed")
    response = client.post(f"/api/tickets/{ticket['id']}/analyze", json={})
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": agents.TICKET_CLOSED}
    with migrated_engine.connect() as connection:
        assert (
            connection.execute(text("SELECT count(*) FROM checkpoints")).scalar() == 0
        )
        assert connection.execute(text("SELECT count(*) FROM agent_runs")).scalar() == 0


@pytest.mark.parametrize("status", ["resolved", "closed"])
def test_closed_ticket_suggestion_can_only_be_rejected(
    client: TestClient, ticket: dict, migrated_engine: Engine, status: str
) -> None:
    """Закрытое обращение не получает ответ; ожидание графа при этом сохраняется."""
    run = analyze(client, ticket)
    user = reviewer(client)
    suggestion_id = run["suggestion"]["id"]
    set_ticket_status(client, ticket, status)
    approve = client.post(
        f"/api/suggestions/{suggestion_id}/review",
        json={"decision": "approve"},
        headers=user["headers"],
    )
    assert approve.status_code == 409, approve.text
    assert approve.json() == {"detail": reviews.TICKET_CLOSED}
    workflow = client.get(f"/api/agent-runs/{run['id']}/workflow").json()
    assert workflow["state"] == "awaiting_review"
    reject = client.post(
        f"/api/suggestions/{suggestion_id}/review",
        json={"decision": "reject", "comment": "Закрыто"},
        headers=user["headers"],
    )
    assert reject.status_code == 200, reject.text
    result = reject.json()
    assert result["suggestion"]["status"] == "rejected"
    assert result["ticket_status"] == status
    assert result["message"] is None
    assert result["workflow"]["state"] == "rejected"
    assert message_count(migrated_engine) == 0


def test_ticket_closed_after_precheck_is_not_delivered(
    client: TestClient,
    ticket: dict,
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Гонка: статус сменился после предварительной проверки, решает блокировка.

    Отказ случается уже в узле deliver; следующий запрос восстанавливает
    ожидание графа из фактов БД, и предложение можно отклонить.
    """
    run = analyze(client, ticket)
    user = reviewer(client)
    suggestion_id = run["suggestion"]["id"]
    original = reviews.check_reviewable

    def close_after_check(*args, **kwargs):
        """Пропустить проверку открытым обращением и сразу закрыть его."""
        result = original(*args, **kwargs)
        with Session(migrated_engine) as other, other.begin():
            other.execute(
                update(Ticket).where(Ticket.id == ticket["id"]).values(status="closed")
            )
        return result

    monkeypatch.setattr(reviews, "check_reviewable", close_after_check)
    approve = client.post(
        f"/api/suggestions/{suggestion_id}/review",
        json={"decision": "approve"},
        headers=user["headers"],
    )
    assert approve.status_code == 409, approve.text
    assert approve.json() == {"detail": reviews.TICKET_CLOSED}
    monkeypatch.setattr(reviews, "check_reviewable", original)
    assert message_count(migrated_engine) == 0
    workflow = client.get(f"/api/agent-runs/{run['id']}/workflow").json()
    assert workflow["state"] == "awaiting_review"
    reject = client.post(
        f"/api/suggestions/{suggestion_id}/review",
        json={"decision": "reject", "comment": "Закрыто"},
        headers=user["headers"],
    )
    assert reject.status_code == 200, reject.text
    assert reject.json()["workflow"]["state"] == "rejected"
    assert message_count(migrated_engine) == 0


def test_new_analysis_is_allowed_after_decision(
    client: TestClient, ticket: dict
) -> None:
    """После решения по предложению обращение можно проанализировать снова."""
    run = analyze(client, ticket)
    user = reviewer(client)
    blocked = client.post(f"/api/tickets/{ticket['id']}/analyze", json={})
    assert blocked.status_code == 409, blocked.text
    rejected = client.post(
        f"/api/suggestions/{run['suggestion']['id']}/review",
        json={"decision": "reject", "comment": "Неточно"},
        headers=user["headers"],
    )
    assert rejected.status_code == 200, rejected.text
    second = analyze(client, ticket)
    assert second["id"] != run["id"]
    queue = client.get("/api/suggestions", params={"status": "pending"}).json()
    assert [item["id"] for item in queue] == [second["suggestion"]["id"]]


@pytest.mark.parametrize(
    ("change", "failure"),
    [
        ({"role": "operator"}, ResourceForbidden),
        ({"is_active": False}, ResourceNotFound),
    ],
)
def test_delivery_rechecks_staff_after_waiting_for_concurrent_change(
    client: TestClient,
    ticket: dict,
    migrated_engine: Engine,
    change: dict,
    failure: type[Exception],
) -> None:
    """Понижение роли/отключение в другой транзакции запрещает позднюю доставку.

    Сессия доставки уже кеширует прежние права. Изменение сотрудника удерживает
    строку, поэтому FOR SHARE реально ждёт PostgreSQL, затем перечитывает
    завершённое изменение вместо доверия старому объекту из identity map.
    """
    run = analyze(client, ticket)
    user = reviewer(client, "concurrent-role@example.com")
    ready = threading.Event()
    proceed = threading.Event()
    backend_pid: list[int] = []

    def deliver() -> None:
        """Кешировать рецензента и попытаться доставить после начала его изменения."""
        # Закрепляем соединение, чтобы pg_stat_activity наблюдал именно тот PID,
        # который впоследствии будет выполнять SELECT ... FOR SHARE.
        with migrated_engine.connect() as connection:
            with Session(bind=connection, expire_on_commit=False) as session:
                cached = session.get(User, user["id"])
                assert cached.role == "reviewer" and cached.is_active
                backend_pid.append(session.scalar(select(func.pg_backend_pid())))
                session.commit()
                ready.set()
                assert proceed.wait(timeout=5)
                with pytest.raises(failure):
                    reviews.apply_review(
                        session,
                        run["suggestion"]["id"],
                        {"decision": "approve", "reviewer_id": user["id"]},
                    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(deliver)
        assert ready.wait(timeout=5)
        with migrated_engine.begin() as writer:
            writer.execute(update(User).where(User.id == user["id"]).values(**change))
            proceed.set()
            blocked = False
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with migrated_engine.connect() as observer:
                    blocked = bool(
                        observer.exec_driver_sql(
                            "SELECT wait_event_type = 'Lock' FROM pg_stat_activity "
                            "WHERE pid = %s",
                            (backend_pid[0],),
                        ).scalar()
                    )
                if blocked:
                    break
                time.sleep(0.01)
            assert blocked, "Доставка не дождалась блокировки сотрудника в PostgreSQL"
        future.result(timeout=5)
    with Session(migrated_engine) as session:
        suggestion = session.get(AgentSuggestion, run["suggestion"]["id"])
        assert suggestion.status == "pending"
        assert suggestion.reviewed_by is None
        assert session.scalar(select(func.count()).select_from(TicketMessage)) == 0
        assert session.get(Ticket, ticket["id"]).status == ticket["status"]
