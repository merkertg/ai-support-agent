"""HTTP-контракт решения сотрудника и API сотрудников без PostgreSQL."""

from collections.abc import Iterator
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.main as application
from app.api.dependencies import get_api_session
from app.workflow import service as workflow


@pytest.fixture
def client_and_session() -> Iterator[tuple[TestClient, Mock]]:
    """Выдать HTTP-клиент с заглушкой сессии: SQL в этих тестах запрещён."""
    session = Mock(spec=Session)

    def session_override() -> Iterator[Session]:
        """Вернуть заглушку текущему запросу."""
        yield session

    previous = application.app.dependency_overrides.copy()
    application.app.dependency_overrides[get_api_session] = session_override
    try:
        with TestClient(application.app) as client:
            yield client, session
    finally:
        application.app.dependency_overrides.clear()
        application.app.dependency_overrides.update(previous)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"decision": "approve"},
        {"decision": "maybe", "reviewer_id": 1},
        {"decision": "approve", "reviewer_id": "1"},
        {"decision": "approve", "reviewer_id": 0},
        {"decision": "reject", "reviewer_id": 1},
        {"decision": "reject", "reviewer_id": 1, "comment": "  "},
        {"decision": "reject", "reviewer_id": 1, "comment": "x" * 2001},
        {
            "decision": "reject",
            "reviewer_id": 1,
            "comment": "Неверно",
            "final_content": "Текст",
        },
        {"decision": "approve", "reviewer_id": 1, "final_content": ""},
        {"decision": "approve", "reviewer_id": 1, "status": "approved"},
    ],
)
def test_invalid_review_is_rejected_before_service(
    client_and_session, monkeypatch: pytest.MonkeyPatch, body: dict
) -> None:
    """Неверное решение — 422 без SQL и без продолжения графа."""
    client, session = client_and_session
    review = Mock()
    monkeypatch.setattr(workflow, "review_suggestion", review)
    response = client.post("/api/suggestions/3/review", json=body)
    assert response.status_code == 422, response.text
    review.assert_not_called()
    session.execute.assert_not_called()


def test_valid_review_reaches_workflow_service(
    client_and_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Корректное тело передаётся сервису с нормализованным текстом."""
    client, session = client_and_session
    review = Mock(
        side_effect=workflow.ResourceNotFound("Предложение агента не найдено")
    )
    monkeypatch.setattr(workflow, "review_suggestion", review)
    response = client.post(
        "/api/suggestions/3/review",
        json={"decision": "approve", "reviewer_id": 2, "final_content": "  Ответ  "},
    )
    assert response.status_code == 404
    session_arg, suggestion_id, request, _ = review.call_args.args
    assert (session_arg, suggestion_id) == (session, 3)
    assert request.final_content == "Ответ"


@pytest.mark.parametrize(
    "body",
    [
        {"name": "Оператор"},
        {"name": "Оператор", "email": "not-an-email"},
        {"name": "Оператор", "email": "op@example.com", "role": "superuser"},
        {"name": " ", "email": "op@example.com"},
        {"name": "Оператор", "email": "op@example.com", "is_active": False},
    ],
)
def test_invalid_user_is_rejected(client_and_session, body: dict) -> None:
    """Сотрудник требует имя, email и роль operator или admin; лишнее запрещено."""
    client, session = client_and_session
    response = client.post("/api/users", json=body)
    assert response.status_code == 422, response.text
    session.add.assert_not_called()


@pytest.mark.parametrize(
    "path",
    [
        "/api/suggestions/0/review",
        "/api/suggestions/abc",
        "/api/agent-runs/-1/workflow",
        "/api/suggestions?status=unknown",
    ],
)
def test_invalid_path_or_filter_is_rejected(client_and_session, path: str) -> None:
    """Неверный id или фильтр отклоняется до SQL-запроса."""
    client, session = client_and_session
    method = client.post if path.endswith("/review") else client.get
    kwargs = (
        {"json": {"decision": "approve", "reviewer_id": 1}}
        if method == client.post
        else {}
    )
    response = method(path, **kwargs)
    assert response.status_code == 422, response.text
    session.execute.assert_not_called()


def test_openapi_documents_review_and_workflow() -> None:
    """Swagger описывает решение, очередь, сотрудников и состояние графа."""
    schema = application.app.openapi()
    paths = schema["paths"]
    review = paths["/api/suggestions/{resource_id}/review"]["post"]
    assert {"200", "404", "409", "422"} <= set(review["responses"])
    assert "get" in paths["/api/suggestions"]
    assert "get" in paths["/api/agent-runs/{resource_id}/workflow"]
    assert {"get", "post"} <= set(paths["/api/users"])
    request = schema["components"]["schemas"]["ReviewRequest"]
    assert request["additionalProperties"] is False
    assert set(request["required"]) == {"decision", "reviewer_id"}
