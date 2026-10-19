"""Серверные показатели и история UI на настоящей отдельной PostgreSQL *_test."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.dependencies import get_api_session
from app.main import app
from app.models import AgentRun, AgentSuggestion, Ticket, TicketMessage, User

pytestmark = pytest.mark.integration


@pytest.fixture
def client(migrated_engine, staff_headers):
    """Применить миграции и дать каждому запросу собственную тестовую сессию."""

    def dependency():
        """Закрыть сессию запроса с откатом незавершённой транзакции."""
        with Session(migrated_engine) as session:
            yield session

    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_api_session] = dependency
    try:
        with TestClient(app, headers=staff_headers) as value:
            yield value
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def seed_ticket(client, suffix="one"):
    """Создать связи публичным API и вернуть одно обращение."""
    customer = client.post(
        "/api/customers", json={"name": suffix, "email": f"{suffix}@example.com"}
    )
    product = client.post("/api/products", json={"name": suffix, "code": suffix})
    assert customer.status_code == product.status_code == 201
    result = client.post(
        "/api/tickets",
        json={
            "customer_id": customer.json()["id"],
            "product_id": product.json()["id"],
            "subject": suffix,
            "description": "Ошибка запуска",
            "priority": "high",
        },
    )
    assert result.status_code == 201
    return result.json()


def test_empty_dashboard_reports_missing_measurements_as_null(client):
    """Не показывать выдуманные среднее время и долю одобрения для пустой базы."""
    response = client.get("/api/dashboard")
    assert response.status_code == 200
    metrics = response.json()
    assert metrics.pop("average_analysis_seconds") is None
    assert metrics.pop("approval_rate") is None
    assert all(value == 0 for value in metrics.values())
    assert client.get("/api/agent-runs").json() == []


def test_status_edit_and_messages_remain_scoped_and_persist(client, migrated_engine):
    """Правка меняет только статус; сообщения читаются лишь у своего обращения."""
    ticket = seed_ticket(client)
    other = seed_ticket(client, "other")
    response = client.patch(f"/api/tickets/{ticket['id']}", json={"status": "resolved"})
    assert response.status_code == 200
    assert response.json()["status"] == "resolved"
    assert response.json()["description"] == ticket["description"]
    with Session(migrated_engine) as session, session.begin():
        assert session.get(Ticket, ticket["id"]).status == "resolved"
        session.add_all(
            [
                TicketMessage(
                    ticket_id=ticket["id"],
                    author_type="customer",
                    content="Моё сообщение",
                ),
                TicketMessage(
                    ticket_id=other["id"],
                    author_type="customer",
                    content="Чужое сообщение",
                ),
                TicketMessage(
                    ticket_id=ticket["id"], author_type="customer", content="Ещё одно"
                ),
            ]
        )
    path = f"/api/tickets/{ticket['id']}/messages"
    assert [row["content"] for row in client.get(path).json()] == [
        "Моё сообщение",
        "Ещё одно",
    ]
    assert client.get(path + "?offset=1&limit=1").json()[0]["content"] == "Ещё одно"
    assert client.get("/api/tickets/9999/messages").status_code == 404
    assert (
        client.patch("/api/tickets/9999", json={"status": "closed"}).status_code == 404
    )


def test_dashboard_aggregates_and_run_history_are_not_page_counts(
    client, migrated_engine
):
    """Distinct тикеты, только completed-время, решения без pending в знаменателе."""
    ticket = seed_ticket(client)
    other = seed_ticket(client, "other")
    start = datetime(2026, 10, 18, tzinfo=UTC)
    with Session(migrated_engine) as session, session.begin():
        session.get(Ticket, other["id"]).status = "closed"
        session.get(Ticket, other["id"]).priority = "critical"
        reviewer = User(name="Оператор", email="reviewer@example.com")
        session.add(reviewer)
        session.flush()
        runs = [
            AgentRun(
                ticket_id=ticket["id"],
                status="completed",
                provider="mock",
                started_at=start,
                finished_at=start + timedelta(seconds=s),
            )
            for s in (10, 20, 15)
        ]
        runs.append(
            AgentRun(
                ticket_id=other["id"],
                status="failed",
                provider="mock",
                started_at=start,
                finished_at=start + timedelta(seconds=100),
            )
        )
        session.add_all(runs)
        session.flush()
        run_ids = [run.id for run in runs]
        session.add_all(
            [
                AgentSuggestion(
                    agent_run_id=runs[0].id,
                    suggestion_type="reply",
                    content="Ответ",
                    status="approved",
                    reviewed_by=reviewer.id,
                    reviewed_at=start,
                ),
                AgentSuggestion(
                    agent_run_id=runs[1].id,
                    suggestion_type="reply",
                    content="Ответ",
                    status="rejected",
                    reviewed_by=reviewer.id,
                    reviewed_at=start,
                ),
                AgentSuggestion(
                    agent_run_id=runs[2].id,
                    suggestion_type="reply",
                    content="Черновик",
                    status="pending",
                ),
            ]
        )
    metrics = client.get("/api/dashboard").json()
    assert metrics == {
        "tickets_total": 2,
        "open_tickets": 1,
        "high_priority_tickets": 1,
        "analyzed_tickets": 1,
        "completed_runs": 3,
        "failed_runs": 1,
        "pending_suggestions": 1,
        "approved_suggestions": 1,
        "rejected_suggestions": 1,
        "average_analysis_seconds": 15.0,
        "approval_rate": 0.5,
    }
    history = client.get(f"/api/agent-runs?ticket_id={ticket['id']}").json()
    assert [row["id"] for row in history] == list(reversed(run_ids[:3]))
    assert [row["suggestion"]["status"] for row in history] == [
        "pending",
        "rejected",
        "approved",
    ]
    assert client.get("/api/agent-runs?offset=1&limit=1").json()[0]["id"] == run_ids[2]
    assert client.get("/api/agent-runs?ticket_id=99999").json() == []
