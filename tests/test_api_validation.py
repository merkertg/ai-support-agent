"""Проверить HTTP-валидацию до SQL без подключения к PostgreSQL."""

from collections.abc import Iterator
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.dependencies import get_api_session
from app.main import app

VALID_PAYLOADS = {
    "customers": {"name": "Клиент", "email": "client@example.com"},
    "products": {"name": "Продукт", "code": "PRODUCT"},
    "licenses": {"customer_id": 1, "product_id": 1, "license_key": "KEY"},
    "tickets": {
        "customer_id": 1,
        "product_id": 1,
        "subject": "Ошибка",
        "description": "Подробности ошибки",
    },
}


@pytest.fixture
def validation_client() -> Iterator[TestClient]:
    """Подменить только источник сессии: невалидный запрос не должен вызывать SQL."""

    def unused_session() -> Iterator[Session]:
        """Выдать объект без SQL-методов, чтобы преждевременный запрос уронил тест."""
        yield cast(Session, object())

    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_api_session] = unused_session
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)


@pytest.mark.parametrize(
    ("resource", "changes"),
    [
        ("customers", {"name": ""}),
        ("customers", {"name": "   "}),
        ("customers", {"name": "x" * 201}),
        ("customers", {"email": "not-an-email"}),
        ("customers", {"company": "x" * 201}),
        ("customers", {"unexpected": "запрещено"}),
        ("products", {"name": " "}),
        ("products", {"code": " "}),
        ("products", {"code": "x" * 65}),
        ("products", {"description": "x" * 10001}),
        ("products", {"unexpected": "запрещено"}),
        ("licenses", {"customer_id": 0}),
        ("licenses", {"product_id": -1}),
        ("licenses", {"customer_id": 2**63}),
        ("licenses", {"license_key": "  "}),
        ("licenses", {"license_key": "x" * 256}),
        ("licenses", {"status": "unknown"}),
        ("licenses", {"issued_at": None}),
        ("licenses", {"issued_at": "2026-01-01T00:00:00"}),
        ("licenses", {"expires_at": "2027-01-01T00:00:00"}),
        (
            "licenses",
            {
                "issued_at": "2027-01-01T00:00:00+00:00",
                "expires_at": "2026-01-01T00:00:00+00:00",
            },
        ),
        ("licenses", {"unexpected": "запрещено"}),
        ("tickets", {"customer_id": 0}),
        ("tickets", {"product_id": 2**63}),
        ("tickets", {"subject": " "}),
        ("tickets", {"subject": "x" * 301}),
        ("tickets", {"description": " "}),
        ("tickets", {"description": "x" * 10001}),
        ("tickets", {"priority": "urgent"}),
        # Создание всегда начинает обращение в open: клиент не задаёт status.
        ("tickets", {"status": "closed"}),
        ("tickets", {"unexpected": "запрещено"}),
    ],
)
def test_invalid_create_request_is_rejected_before_database_access(
    validation_client: TestClient, resource: str, changes: dict
) -> None:
    """Ошибочные поля получают HTTP 422 даже при отсутствии настоящей сессии."""
    response = validation_client.post(
        f"/api/{resource}", json=VALID_PAYLOADS[resource] | changes
    )
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], list)


@pytest.mark.parametrize("resource", ["customers", "products", "licenses", "tickets"])
@pytest.mark.parametrize("value", ["0", "-1", str(2**63), "not-a-number"])
def test_invalid_path_id_is_rejected_without_sql(
    validation_client: TestClient, resource: str, value: str
) -> None:
    """ID в URL ограничен положительным диапазоном PostgreSQL BIGINT."""
    response = validation_client.get(f"/api/{resource}/{value}")
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("resource", ["customers", "products", "licenses", "tickets"])
@pytest.mark.parametrize(
    "params",
    [
        {"offset": -1},
        {"offset": 2**63},
        {"limit": 0},
        {"limit": 101},
        {"limit": "wrong"},
    ],
)
def test_invalid_pagination_is_rejected_without_sql(
    validation_client: TestClient, resource: str, params: dict
) -> None:
    """Отрицательный offset и limit вне 1..100 не доходят до запроса данных."""
    response = validation_client.get(f"/api/{resource}", params=params)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    ("resource", "params"),
    [
        ("licenses", {"customer_id": 0}),
        ("licenses", {"product_id": 2**63}),
        ("tickets", {"customer_id": -1}),
        ("tickets", {"product_id": "wrong"}),
        ("tickets", {"status": "unknown"}),
    ],
)
def test_invalid_list_filter_is_rejected_without_sql(
    validation_client: TestClient, resource: str, params: dict
) -> None:
    """Фильтры соблюдают тот же диапазон ID и допустимые состояния обращения."""
    response = validation_client.get(f"/api/{resource}", params=params)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("failure", ["missing_configuration", "sql_failure"])
def test_database_failure_returns_safe_service_unavailable(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Ошибка настройки или подключения даёт 503 без исходного пароля и SQL."""
    import app.api.dependencies as dependencies
    from app.db.session import DatabaseNotConfigured

    secret = "postgresql+psycopg://user:private-password@private-host/database"
    error = (
        DatabaseNotConfigured(secret)
        if failure == "missing_configuration"
        else SQLAlchemyError(f"SELECT secret FROM private_table; {secret}")
    )

    def failed_session() -> Iterator[Session]:
        """Воспроизвести ошибку открытия сессии до выполнения обработчика API."""
        raise error

    monkeypatch.setattr(dependencies, "get_session", failed_session)
    with TestClient(app) as client:
        response = client.get("/api/customers")
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": dependencies.DATABASE_ERROR}
    assert "private-password" not in response.text
    assert "SELECT" not in response.text


def test_query_failure_after_yield_is_safe_and_closes_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQL-ошибка внутри маршрута возвращается в yield-зависимость и очищает её."""
    from unittest.mock import Mock

    import app.api.dependencies as dependencies

    session = Mock(spec=Session)
    session.get.side_effect = SQLAlchemyError("private query and password")
    closed = []

    def failing_query_session() -> Iterator[Session]:
        """Отметить закрытие источника сессии после SQL-ошибки внутри маршрута."""
        try:
            yield session
        finally:
            closed.append(True)

    monkeypatch.setattr(dependencies, "get_session", failing_query_session)
    with TestClient(app) as client:
        response = client.get("/api/customers/1")
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": dependencies.DATABASE_ERROR}
    assert closed == [True]


@pytest.mark.parametrize("resource", ["licenses", "tickets"])
@pytest.mark.parametrize("field", ["customer_id", "product_id"])
@pytest.mark.parametrize("value", [True, 1.0, 1.5, "1"])
def test_json_reference_id_requires_integer_without_coercion(
    validation_client: TestClient, resource: str, field: str, value: object
) -> None:
    """JSON-ссылки не принимают bool, float или строку, похожую на целый ID."""
    response = validation_client.post(
        f"/api/{resource}", json=VALID_PAYLOADS[resource] | {field: value}
    )
    assert response.status_code == 422, response.text
