"""Контракты панели: неверные фильтры и поля отсекаются до SQL."""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import DATABASE_ERROR, get_api_session
from app.main import app


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/api/agent-runs?limit=101", None),
        ("GET", "/api/agent-runs?offset=-1", None),
        ("GET", "/api/agent-runs?ticket_id=0", None),
        ("GET", "/api/tickets/0/messages", None),
        ("GET", "/api/tickets/1/messages?limit=0", None),
        ("PATCH", "/api/tickets/0", {"status": "closed"}),
        ("PATCH", "/api/tickets/1", {"status": "bogus"}),
        ("PATCH", "/api/tickets/1", {"status": "closed", "customer_id": 99}),
        ("PATCH", "/api/tickets/1", {}),
    ],
)
def test_operator_validation_before_sql(method, path, body):
    """Не разрешать UI обходить пагинацию, формат id и список изменяемых полей."""
    session = Mock()

    def dependency():
        """Выдать наблюдаемую сессию без подключения к БД."""
        yield session

    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_api_session] = dependency
    try:
        with TestClient(app) as client:
            assert client.request(method, path, json=body).status_code == 422
        assert session.mock_calls == []
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def test_dashboard_database_failure_is_safe():
    """Не выдавать адрес БД и SQL через показатели панели при сбое агрегатов."""
    session = Mock()
    session.execute.side_effect = SQLAlchemyError("postgresql://secret:password@db")

    def dependency():
        """Подставить отказ сервера без настоящего PostgreSQL."""
        yield session

    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_api_session] = dependency
    try:
        with TestClient(app) as client:
            response = client.get("/api/dashboard")
        assert response.status_code == 503
        assert response.json() == {"detail": DATABASE_ERROR}
        assert "secret" not in response.text
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
