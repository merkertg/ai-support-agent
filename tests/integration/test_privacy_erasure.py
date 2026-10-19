"""Обезличивание на настоящей PostgreSQL: клиент по запросу и срок хранения."""

import json
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text, update
from sqlalchemy.orm import Session

import app.privacy as privacy_cli
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
)
from app.schemas.resources import TicketCreate
from app.services import privacy, resources
from app.services.errors import ResourceInvalid
from app.workflow.checkpoint import open_saver

pytestmark = pytest.mark.integration
# Строки, которые после обезличивания не должны встречаться нигде в БД.
PERSONAL = {
    "name": "Анна Персональная",
    "email": "anna.personal@example.com",
    "company": "ООО Личные Данные",
    "subject": "Сервер Анны не запускается",
    "description": "После обновления сервер Анны в офисе на Садовой падает.",
    "final": "Анна, перезапустите службу на Садовой.",
    "comment": "Отклонено: Анна просила звонок.",
}
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[None]:
    """Mock-провайдер и база знаний без адреса Qdrant, как в тестах review."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    for name in ("OPENAI_API_KEY", "AGENT_MAX_STEPS", "QDRANT_URL"):
        monkeypatch.delenv(name, raising=False)
    for cache in (get_llm_settings, get_agent_settings, get_knowledge_settings):
        cache.cache_clear()
    yield
    for cache in (get_llm_settings, get_agent_settings, get_knowledge_settings):
        cache.cache_clear()


@pytest.fixture
def client(
    migrated_engine: Engine, test_database_url: str, staff_headers: dict[str, str]
) -> Iterator[TestClient]:
    """HTTP-клиент администратора: сессии и checkpointer — тестовая БД *_test."""
    pool, saver = open_saver(test_database_url)

    def session_override() -> Iterator[Session]:
        """Открыть отдельную сессию тестовой БД на запрос."""
        with Session(migrated_engine) as session:
            yield session

    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_api_session] = session_override
    app.dependency_overrides[get_checkpointer_factory] = lambda: lambda: saver
    try:
        with TestClient(app, headers=staff_headers) as http:
            yield http
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        pool.close()


def created(response) -> dict:
    """Проверить 201 и вернуть JSON созданной записи."""
    assert response.status_code == 201, response.text
    return response.json()


def make_customer(client: TestClient, email: str = PERSONAL["email"]) -> dict:
    """Клиент с именем, email и компанией."""
    return created(
        client.post(
            "/api/customers",
            json={
                "name": PERSONAL["name"],
                "email": email,
                "company": PERSONAL["company"],
            },
        )
    )


def make_product(client: TestClient) -> dict:
    """Продукт, по которому mock-агент находит статьи."""
    return created(
        client.post(
            "/api/products", json={"name": "TechFlow Server", "code": "techflow-server"}
        )
    )


def make_ticket(client: TestClient, customer: dict, product: dict) -> dict:
    """Обращение с персональными подробностями в теме и описании."""
    return created(
        client.post(
            "/api/tickets",
            json={
                "customer_id": customer["id"],
                "product_id": product["id"],
                "subject": PERSONAL["subject"],
                "description": PERSONAL["description"],
            },
        )
    )


def analyze(client: TestClient, ticket: dict) -> dict:
    """Запустить анализ и вернуть запуск с pending-предложением."""
    return created(client.post(f"/api/tickets/{ticket['id']}/analyze", json={}))


def review(client: TestClient, run: dict, body: dict) -> dict:
    """Принять решение по предложению запуска."""
    response = client.post(
        f"/api/suggestions/{run['suggestion']['id']}/review", json=body
    )
    assert response.status_code == 200, response.text
    return response.json()


def tables_containing(engine: Engine, needle: str) -> list[str]:
    """Найти таблицы public, в строках которых есть подстрока.

    Строка таблицы сравнивается в текстовом виде; bytea (blob checkpoint)
    в нём выглядит как hex, поэтому ищется и hex-представление подстроки.
    """
    needle_hex = needle.encode("utf-8").hex()
    with engine.connect() as connection:
        tables = connection.scalars(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        ).all()
        return [
            table
            for table in sorted(tables)
            if connection.scalar(
                text(
                    f'SELECT count(*) FROM "{table}" AS row '
                    "WHERE strpos(row::text, :needle) > 0 "
                    "OR strpos(row::text, :needle_hex) > 0"
                ),
                {"needle": needle, "needle_hex": needle_hex},
            )
        ]


def checkpoint_rows(engine: Engine, threads: list[str]) -> int:
    """Сколько строк checkpoint осталось у потоков."""
    with engine.connect() as connection:
        return sum(
            connection.scalar(
                text(f"SELECT count(*) FROM {table} WHERE thread_id = ANY(:threads)"),
                {"threads": threads},
            )
            for table in CHECKPOINT_TABLES
        )


def test_erasure_removes_personal_data_from_every_table(
    client: TestClient, migrated_engine: Engine
) -> None:
    """Имя, email, тексты, решения и checkpoint исчезают; учёт и итоги остаются."""
    customer = make_customer(client)
    product = make_product(client)
    licensed = created(
        client.post(
            "/api/licenses",
            json={
                "customer_id": customer["id"],
                "product_id": product["id"],
                "license_key": "LIC-PRIVACY-1",
            },
        )
    )
    approved_ticket = make_ticket(client, customer, product)
    approved_run = analyze(client, approved_ticket)
    review(
        client,
        approved_run,
        {"decision": "approve", "final_content": PERSONAL["final"]},
    )
    rejected_ticket = make_ticket(client, customer, product)
    rejected_run = analyze(client, rejected_ticket)
    review(client, rejected_run, {"decision": "reject", "comment": PERSONAL["comment"]})
    untouched = make_ticket(client, customer, product)
    threads = [approved_run["workflow_thread_id"], rejected_run["workflow_thread_id"]]
    assert checkpoint_rows(migrated_engine, threads) > 0
    for value in PERSONAL.values():
        assert tables_containing(migrated_engine, value), value
    # Решение с текстом хранится и в checkpoint LangGraph (blob, т. е. bytea).
    for value in (PERSONAL["final"], PERSONAL["comment"]):
        assert set(tables_containing(migrated_engine, value)) & set(
            CHECKPOINT_TABLES
        ), value

    path = f"/api/customers/{customer['id']}/erase"
    wrong = client.post(path, json={"confirm_email": "other@example.com"})
    assert wrong.status_code == 422
    assert wrong.json() == {"detail": privacy.CONFIRMATION_MISMATCH}
    assert client.get(f"/api/customers/{customer['id']}").json()["erased_at"] is None

    response = client.post(path, json={"confirm_email": PERSONAL["email"].upper()})
    assert response.status_code == 200, response.text
    report = response.json()
    assert report == report | {
        "customer_id": customer["id"],
        "tickets": 3,
        "messages": 1,
        "agent_runs": 2,
        "suggestions": 2,
        "workflow_threads": 2,
        "licenses_kept": 1,
    }
    for value in PERSONAL.values():
        assert tables_containing(migrated_engine, value) == [], value
    assert checkpoint_rows(migrated_engine, threads) == 0

    erased = client.get(f"/api/customers/{customer['id']}").json()
    assert erased["name"] == privacy.ERASED_CUSTOMER_NAME
    assert erased["email"] == privacy.erased_email(customer["id"])
    assert erased["company"] is None and erased["erased_at"] is not None
    assert (
        client.get(f"/api/licenses/{licensed['id']}").json()["customer_id"]
        == (customer["id"])
    )
    for ticket in (approved_ticket, rejected_ticket, untouched):
        stored = client.get(f"/api/tickets/{ticket['id']}").json()
        assert stored["subject"] == privacy.ANONYMIZED_SUBJECT
        assert stored["description"] == privacy.ERASED_TEXT
        assert stored["status"] == "closed"
        assert stored["anonymized_reason"] == "customer_request"
    messages = client.get(f"/api/tickets/{approved_ticket['id']}/messages").json()
    assert [message["content"] for message in messages] == [privacy.ERASED_TEXT]
    run = client.get(f"/api/agent-runs/{approved_run['id']}").json()
    assert run["result"]["draft_reply"] == privacy.ERASED_TEXT
    assert run["result"]["sources"] == approved_run["result"]["sources"]
    assert run["suggestion"]["status"] == "approved"
    assert {item["name"] for item in run["tool_trace"]} == {
        item["name"] for item in approved_run["tool_trace"]
    }
    assert all(item["arguments"] == {} for item in run["tool_trace"])
    # Поток восстанавливается по фактам БД: id и исход без текстов решения.
    for source, outcome in ((approved_run, "delivered"), (rejected_run, "rejected")):
        workflow = client.get(f"/api/agent-runs/{source['id']}/workflow")
        assert workflow.status_code == 200, workflow.text
        assert workflow.json()["state"] == outcome
    assert tables_containing(migrated_engine, PERSONAL["final"]) == []
    assert client.get("/api/dashboard").json()["tickets_total"] == 3


def test_erased_customer_and_tickets_reject_further_work(
    client: TestClient, migrated_engine: Engine
) -> None:
    """Повтор, новые обращения и лицензии, смена статуса и анализ дают 409."""
    customer = make_customer(client)
    product = make_product(client)
    ticket = make_ticket(client, customer, product)
    path = f"/api/customers/{customer['id']}/erase"
    assert client.post(path, json={"confirm_email": PERSONAL["email"]}).status_code == (
        200
    )
    again = client.post(path, json={"confirm_email": PERSONAL["email"]})
    assert again.status_code == 409
    assert again.json() == {"detail": privacy.ALREADY_ERASED}
    new_ticket = client.post(
        "/api/tickets",
        json={
            "customer_id": customer["id"],
            "product_id": product["id"],
            "subject": "Новое",
            "description": "Новое обращение",
        },
    )
    assert new_ticket.status_code == 409
    assert new_ticket.json() == {"detail": resources.CUSTOMER_ERASED}
    license_response = client.post(
        "/api/licenses",
        json={
            "customer_id": customer["id"],
            "product_id": product["id"],
            "license_key": "LIC-AFTER-ERASURE",
        },
    )
    assert license_response.status_code == 409
    reopened = client.patch(f"/api/tickets/{ticket['id']}", json={"status": "open"})
    assert reopened.status_code == 409
    analysis = client.post(f"/api/tickets/{ticket['id']}/analyze", json={})
    assert analysis.status_code == 409
    missing = client.post(
        "/api/customers/999999/erase", json={"confirm_email": "a@example.com"}
    )
    assert missing.status_code == 404


def test_erasure_waits_for_pending_decision_and_changes_nothing(
    client: TestClient, migrated_engine: Engine
) -> None:
    """Pending-предложение блокирует обезличивание целиком до решения."""
    customer = make_customer(client)
    product = make_product(client)
    ticket = make_ticket(client, customer, product)
    run = analyze(client, ticket)
    path = f"/api/customers/{customer['id']}/erase"
    blocked = client.post(path, json={"confirm_email": PERSONAL["email"]})
    assert blocked.status_code == 409
    detail = blocked.json()["detail"]
    assert f"обращения {ticket['id']}" in detail
    assert f"предложение {run['suggestion']['id']}" in detail
    assert (
        client.get(f"/api/customers/{customer['id']}").json()["name"]
        == (PERSONAL["name"])
    )
    assert (
        client.get(f"/api/tickets/{ticket['id']}").json()["subject"]
        == (PERSONAL["subject"])
    )
    review(client, run, {"decision": "reject", "comment": PERSONAL["comment"]})
    assert client.post(path, json={"confirm_email": PERSONAL["email"]}).status_code == (
        200
    )


def staff_headers_for(client: TestClient, role: str) -> dict[str, str]:
    """Создать сотрудника с ролью и выдать ему токен через API администратора."""
    user = created(
        client.post(
            "/api/users",
            json={"name": role, "email": f"{role}@example.com", "role": role},
        )
    )
    issued = created(
        client.post(f"/api/users/{user['id']}/tokens", json={"name": "tests"})
    )
    return {"Authorization": f"Bearer {issued['token']}"}


def test_erasure_requires_admin_role(
    client: TestClient, migrated_engine: Engine
) -> None:
    """Оператор и рецензент получают 403; клиент не меняется."""
    customer = make_customer(client)
    for role in ("operator", "reviewer"):
        response = client.post(
            f"/api/customers/{customer['id']}/erase",
            json={"confirm_email": PERSONAL["email"]},
            headers=staff_headers_for(client, role),
        )
        assert response.status_code == 403
    assert client.get(f"/api/customers/{customer['id']}").json()["erased_at"] is None


def test_ticket_created_during_erasure_is_erased_too(
    client: TestClient, migrated_engine: Engine
) -> None:
    """Вставка обращения держит FOR SHARE клиента: обезличивание ждёт и видит её."""
    customer = make_customer(client)
    product = make_product(client)
    outcome: dict = {}
    with Session(migrated_engine) as creating:
        # Первая половина create_ticket: блокировка клиента и ещё не commit.
        resources._require_references(creating, customer["id"], product["id"])
        payload = TicketCreate(
            customer_id=customer["id"],
            product_id=product["id"],
            subject=PERSONAL["subject"],
            description=PERSONAL["description"],
        )
        creating.add(Ticket(**payload.model_dump(), status="open"))
        creating.flush()

        def erase() -> None:
            """Обезличить в отдельной сессии, как второй HTTP-запрос."""
            with Session(migrated_engine) as session:
                outcome["report"] = privacy.erase_customer(
                    session, customer["id"], PERSONAL["email"]
                )

        worker = threading.Thread(target=erase)
        worker.start()
        time.sleep(0.5)
        assert worker.is_alive(), "обезличивание не ждало блокировки клиента"
        creating.commit()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert outcome["report"].tickets == 1
    assert tables_containing(migrated_engine, PERSONAL["description"]) == []


def closed_ticket(
    client: TestClient,
    engine: Engine,
    customer: dict,
    product: dict,
    *,
    status: str = "closed",
    age_days: int = 40,
) -> dict:
    """Обращение в нужном статусе с updated_at в прошлом."""
    ticket = make_ticket(client, customer, product)
    if status != "open":
        patched = client.patch(f"/api/tickets/{ticket['id']}", json={"status": status})
        assert patched.status_code == 200, patched.text
    with Session(engine) as session:
        session.execute(
            update(Ticket)
            .where(Ticket.id == ticket["id"])
            .values(updated_at=datetime.now(UTC) - timedelta(days=age_days))
        )
        session.commit()
    return ticket


def test_retention_plans_then_anonymizes_only_old_closed_tickets(
    client: TestClient, migrated_engine: Engine
) -> None:
    """План ничего не меняет; apply берёт старые закрытые и пропускает pending."""
    customer = make_customer(client)
    product = make_product(client)
    old = closed_ticket(client, migrated_engine, customer, product)
    resolved = closed_ticket(
        client, migrated_engine, customer, product, status="resolved"
    )
    recent = closed_ticket(client, migrated_engine, customer, product, age_days=5)
    still_open = closed_ticket(
        client, migrated_engine, customer, product, status="open"
    )
    waiting = make_ticket(client, customer, product)
    pending_run = analyze(client, waiting)
    client.patch(f"/api/tickets/{waiting['id']}", json={"status": "closed"})
    with Session(migrated_engine) as session:
        session.execute(
            update(Ticket)
            .where(Ticket.id == waiting["id"])
            .values(updated_at=datetime.now(UTC) - timedelta(days=60))
        )
        session.commit()

    with Session(migrated_engine) as session:
        plan = privacy.apply_retention(session, 30)
    assert plan.applied is False
    assert plan.ticket_ids == [old["id"], resolved["id"]]
    assert [skip.ticket_id for skip in plan.skipped] == [waiting["id"]]
    assert f"предложение {pending_run['suggestion']['id']}" in plan.skipped[0].reason
    assert plan.tickets == 2
    assert tables_containing(migrated_engine, PERSONAL["subject"])
    with Session(migrated_engine) as session:
        unchanged = session.get(Ticket, old["id"])
        assert unchanged.anonymized_at is None

    with Session(migrated_engine) as session:
        applied = privacy.apply_retention(session, 30, apply=True)
    assert applied.applied is True
    assert applied.ticket_ids == [old["id"], resolved["id"]]
    with Session(migrated_engine) as session:
        tickets = {
            ticket.id: ticket
            for ticket in session.scalars(select(Ticket).order_by(Ticket.id))
        }
        assert tickets[old["id"]].anonymized_reason == "retention"
        assert tickets[resolved["id"]].status == "resolved"
        for kept in (recent, still_open, waiting):
            assert tickets[kept["id"]].anonymized_at is None
            assert tickets[kept["id"]].subject == PERSONAL["subject"]
        # Срок хранения не трогает самого клиента: только явный запрос.
        stored = session.get(Customer, customer["id"])
        assert stored.name == PERSONAL["name"] and stored.erased_at is None

    with Session(migrated_engine) as session:
        repeat = privacy.apply_retention(session, 30, apply=True)
    assert repeat.ticket_ids == []
    with Session(migrated_engine) as session, pytest.raises(ResourceInvalid):
        privacy.apply_retention(session, 7)


def test_retention_scrubs_runs_and_suggestions_of_old_tickets(
    client: TestClient, migrated_engine: Engine
) -> None:
    """Запуск, решение и сообщение старого обращения тоже обезличиваются."""
    customer = make_customer(client)
    product = make_product(client)
    ticket = make_ticket(client, customer, product)
    run = analyze(client, ticket)
    review(client, run, {"decision": "approve", "final_content": PERSONAL["final"]})
    client.patch(f"/api/tickets/{ticket['id']}", json={"status": "closed"})
    with Session(migrated_engine) as session:
        session.execute(
            update(Ticket)
            .where(Ticket.id == ticket["id"])
            .values(updated_at=datetime.now(UTC) - timedelta(days=400))
        )
        session.commit()
    with Session(migrated_engine) as session:
        report = privacy.apply_retention(session, 365, apply=True)
    assert (report.tickets, report.messages, report.agent_runs) == (1, 1, 1)
    assert (report.suggestions, report.workflow_threads) == (1, 1)
    for value in ("subject", "description", "final"):
        assert tables_containing(migrated_engine, PERSONAL[value]) == [], value
    with Session(migrated_engine) as session:
        suggestion = session.scalar(select(AgentSuggestion))
        assert suggestion.final_content == privacy.ERASED_TEXT
        message = session.scalar(select(TicketMessage))
        assert message.content == privacy.ERASED_TEXT
        stored_run = session.get(AgentRun, run["id"])
        assert stored_run.result["category"] == run["result"]["category"]


@pytest.fixture
def cli(migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch):
    """Запускать python -m app.privacy в процессе теста против тестовой БД."""
    monkeypatch.setattr(privacy_cli, "get_engine", lambda: migrated_engine)
    monkeypatch.setattr(privacy_cli, "dispose_engine", lambda: None)
    return privacy_cli.main


def test_cli_erases_and_plans_retention_with_json_reports(
    cli, capsys, client: TestClient, migrated_engine: Engine
) -> None:
    """erase и retention печатают JSON без персональных данных."""
    customer = make_customer(client)
    product = make_product(client)
    closed_ticket(client, migrated_engine, customer, product)
    assert cli(["retention", "--older-than-days", "30"]) == 0
    captured = capsys.readouterr()
    plan = json.loads(captured.out)
    assert plan["applied"] is False and plan["tickets"] == 1
    assert "--apply" in captured.err
    assert (
        cli(
            [
                "erase",
                "--customer-id",
                str(customer["id"]),
                "--confirm-email",
                PERSONAL["email"],
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert json.loads(output)["tickets"] == 1
    assert PERSONAL["email"] not in output and PERSONAL["name"] not in output
    assert (
        cli(
            [
                "erase",
                "--customer-id",
                str(customer["id"]),
                "--confirm-email",
                PERSONAL["email"],
            ]
        )
        == 1
    )
    assert privacy.ALREADY_ERASED in capsys.readouterr().err
