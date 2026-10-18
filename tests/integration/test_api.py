"""Проверить публичные API-сценарии на отдельной PostgreSQL-базе *_test."""

from collections.abc import Iterator
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_api_session
from app.main import app
from app.models import Customer, License, Product, Ticket

pytestmark = pytest.mark.integration


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    """Выдать каждому HTTP-запросу отдельную сессию из проверенной тестовой БД."""

    def session_override() -> Iterator[Session]:
        """Закрыть сессию запроса и откатить незавершённую транзакцию при ошибке."""
        with Session(migrated_engine) as session:
            try:
                yield session
            except Exception:
                session.rollback()
                raise

    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_api_session] = session_override
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        # Не оставляем тестовую сессию другим проверкам /health и /ready.
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)


def create(client: TestClient, resource: str, payload: dict) -> dict:
    """Создать запись штатным API и вернуть ответ только после HTTP 201."""
    response = client.post(f"/api/{resource}", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def references(client: TestClient, suffix: str = "one") -> tuple[dict, dict]:
    """Создать клиента и продукт, на которые API разрешает сослаться."""
    customer = create(
        client,
        "customers",
        {"name": f"Клиент {suffix}", "email": f"client-{suffix}@example.com"},
    )
    product = create(
        client,
        "products",
        {"name": f"Продукт {suffix}", "code": f"PRODUCT-{suffix}"},
    )
    return customer, product


def test_complete_creation_and_reading_persists_between_sessions(
    client: TestClient, migrated_engine: Engine
) -> None:
    """Создать связанную историю через HTTP и прочитать commit в новой SQL-сессии."""
    customer = create(
        client,
        "customers",
        {
            "name": "  ООО Пример  ",
            "email": "support@example.com",
            "company": "  Компания  ",
        },
    )
    product = create(
        client,
        "products",
        {"name": "TechFlow", "code": "TF-SERVER", "description": "Сервер"},
    )
    license_record = create(
        client,
        "licenses",
        {
            "customer_id": customer["id"],
            "product_id": product["id"],
            "license_key": "LIC-1054",
            "issued_at": "2026-01-01T00:00:00+00:00",
            "expires_at": "2027-01-01T00:00:00+00:00",
        },
    )
    ticket = create(
        client,
        "tickets",
        {
            "customer_id": customer["id"],
            "product_id": product["id"],
            "subject": "Ошибка активации",
            "description": "Ключ LIC-1054 не активируется",
            "priority": "high",
        },
    )
    assert customer["name"] == "ООО Пример"
    assert customer["company"] == "Компания"
    assert license_record["status"] == "active"
    assert ticket["status"] == "open"
    assert ticket["priority"] == "high"

    # Каждый запрос использует новую Session: успех GET доказывает commit POST.
    for resource, record in (
        ("customers", customer),
        ("products", product),
        ("licenses", license_record),
        ("tickets", ticket),
    ):
        response = client.get(f"/api/{resource}/{record['id']}")
        assert response.status_code == 200, response.text
        assert response.json() == record
        response = client.get(f"/api/{resource}")
        assert response.status_code == 200, response.text
        assert response.json() == [record]

    with Session(migrated_engine) as session:
        assert session.get(Customer, customer["id"]).company == "Компания"
        assert session.get(Product, product["id"]).code == "TF-SERVER"
        assert session.get(License, license_record["id"]).license_key == "LIC-1054"
        assert session.get(Ticket, ticket["id"]).subject == "Ошибка активации"
    for record, field in (
        (customer, "created_at"),
        (product, "created_at"),
        (license_record, "issued_at"),
        (ticket, "created_at"),
        (ticket, "updated_at"),
    ):
        assert datetime.fromisoformat(record[field]).utcoffset() is not None


def test_omitted_optional_fields_and_server_defaults(client: TestClient) -> None:
    """Минимальный запрос получает nullable-поля, активную лицензию и open/medium."""
    customer, product = references(client)
    license_record = create(
        client,
        "licenses",
        {
            "customer_id": customer["id"],
            "product_id": product["id"],
            "license_key": "DEFAULTS",
        },
    )
    ticket = create(
        client,
        "tickets",
        {
            "customer_id": customer["id"],
            "product_id": product["id"],
            "subject": "Минимальное обращение",
            "description": "Описание",
        },
    )
    assert customer["company"] is None
    assert product["description"] is None
    assert license_record["status"] == "active"
    assert license_record["expires_at"] is None
    assert datetime.fromisoformat(license_record["issued_at"]).utcoffset() is not None
    assert ticket["status"] == "open"
    assert ticket["priority"] == "medium"


@pytest.mark.parametrize("resource", ["customers", "products", "licenses"])
def test_duplicate_returns_conflict_and_next_request_still_commits(
    client: TestClient, resource: str
) -> None:
    """Дубликат получает 409, не создаёт строку и не ломает следующий commit."""
    customer, product = references(client)
    payloads = {
        "customers": {"name": "Дубликат", "email": customer["email"]},
        "products": {"name": "Дубликат", "code": product["code"]},
        "licenses": {
            "customer_id": customer["id"],
            "product_id": product["id"],
            "license_key": "SAME-KEY",
        },
    }
    payload = payloads[resource]
    if resource == "licenses":
        create(client, resource, payload)
    before = client.get(f"/api/{resource}").json()
    response = client.post(f"/api/{resource}", json=payload)
    assert response.status_code == 409, response.text
    assert isinstance(response.json().get("detail"), str)
    assert "INSERT INTO" not in response.text
    assert "psycopg" not in response.text
    assert client.get(f"/api/{resource}").json() == before

    unique_field = {
        "customers": "email",
        "products": "code",
        "licenses": "license_key",
    }[resource]
    payload[unique_field] = (
        "new-client@example.com" if resource == "customers" else "NEW-UNIQUE"
    )
    created = create(client, resource, payload)
    assert created[unique_field] == payload[unique_field]
    assert len(client.get(f"/api/{resource}").json()) == len(before) + 1


@pytest.mark.parametrize("resource", ["customers", "products", "licenses", "tickets"])
def test_unknown_resource_returns_not_found(client: TestClient, resource: str) -> None:
    """Корректный, но отсутствующий ID возвращает HTTP 404 для всех сущностей."""
    response = client.get(f"/api/{resource}/999999999")
    assert response.status_code == 404, response.text
    assert isinstance(response.json().get("detail"), str)


@pytest.mark.parametrize(
    "resource, missing",
    [(r, f) for r in ("licenses", "tickets") for f in ("customer_id", "product_id")],
)
def test_missing_reference_has_no_partial_write(
    client: TestClient, migrated_engine: Engine, resource: str, missing: str
) -> None:
    """Несуществующий клиент/продукт даёт 404, не оставляя лицензию или обращение."""
    customer, product = references(client)
    valid = {"customer_id": customer["id"], "product_id": product["id"]}
    valid.update(
        {"license_key": "FK-CHECK"}
        if resource == "licenses"
        else {"subject": "Проверка связи", "description": "Описание"}
    )
    invalid = valid | {missing: 999999999}
    response = client.post(f"/api/{resource}", json=invalid)
    assert response.status_code == 404, response.text
    model = License if resource == "licenses" else Ticket
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(model)) == 0
    assert create(client, resource, valid)[missing] == valid[missing]


def test_license_filters_are_exact_and_combine(client: TestClient) -> None:
    """Списки лицензий отбирают клиента, продукт и точное значение ключа вместе."""
    first_customer, first_product = references(client, "first")
    second_customer, second_product = references(client, "second")
    records = [
        create(
            client,
            "licenses",
            {
                "customer_id": customer["id"],
                "product_id": product["id"],
                "license_key": key,
            },
        )
        for customer, product, key in (
            (first_customer, first_product, "LIC-A"),
            (first_customer, second_product, "LIC-AB"),
            (second_customer, first_product, "LIC-B"),
        )
    ]
    for params, expected in (
        ({"customer_id": first_customer["id"]}, records[:2]),
        ({"product_id": first_product["id"]}, [records[0], records[2]]),
        ({"license_key": "LIC-A"}, [records[0]]),
        (
            {
                "customer_id": first_customer["id"],
                "product_id": second_product["id"],
                "license_key": "LIC-AB",
            },
            [records[1]],
        ),
        ({"license_key": "LIC"}, []),
        ({"customer_id": 999999999}, []),
    ):
        response = client.get("/api/licenses", params=params)
        assert response.status_code == 200, response.text
        assert response.json() == expected


def test_ticket_filters_combine_status_customer_and_product(
    client: TestClient, migrated_engine: Engine
) -> None:
    """Фильтр статуса читает сохранённые данные и объединяется с внешними ключами."""
    first_customer, first_product = references(client, "first")
    second_customer, second_product = references(client, "second")
    records = [
        create(
            client,
            "tickets",
            {
                "customer_id": customer["id"],
                "product_id": product["id"],
                "subject": f"Обращение {index}",
                "description": "Описание",
            },
        )
        for index, (customer, product) in enumerate(
            (
                (first_customer, first_product),
                (first_customer, second_product),
                (second_customer, first_product),
            )
        )
    ]
    # HTTP-изменение статуса ещё не реализовано: готовим существующую историю SQL.
    with Session(migrated_engine) as session:
        session.get(Ticket, records[0]["id"]).status = "resolved"
        session.commit()
    records[0] = client.get(f"/api/tickets/{records[0]['id']}").json()
    for params, expected in (
        ({"customer_id": first_customer["id"]}, records[:2]),
        ({"product_id": first_product["id"]}, [records[0], records[2]]),
        ({"status": "open"}, records[1:]),
        ({"status": "resolved"}, [records[0]]),
        (
            {
                "customer_id": first_customer["id"],
                "product_id": second_product["id"],
                "status": "open",
            },
            [records[1]],
        ),
        ({"customer_id": second_customer["id"], "status": "resolved"}, []),
    ):
        response = client.get("/api/tickets", params=params)
        assert response.status_code == 200, response.text
        assert response.json() == expected


@pytest.mark.parametrize("resource", ["customers", "products", "licenses", "tickets"])
def test_pagination_has_stable_order_and_empty_tail(
    client: TestClient, resource: str
) -> None:
    """offset/limit выбирают устойчивую страницу по ID и пустой хвост списка."""
    customer, product = references(client)
    for index in range(3):
        payload = {
            "customers": {
                "name": f"Клиент {index}",
                "email": f"pagination-{index}@example.com",
            },
            "products": {"name": f"Продукт {index}", "code": f"PAGE-{index}"},
            "licenses": {
                "customer_id": customer["id"],
                "product_id": product["id"],
                "license_key": f"PAGE-{index}",
            },
            "tickets": {
                "customer_id": customer["id"],
                "product_id": product["id"],
                "subject": f"Обращение {index}",
                "description": "Описание",
            },
        }[resource]
        create(client, resource, payload)
    records = client.get(f"/api/{resource}").json()
    assert [record["id"] for record in records] == sorted(
        record["id"] for record in records
    )
    response = client.get(f"/api/{resource}", params={"offset": 1, "limit": 1})
    assert response.status_code == 200, response.text
    assert response.json() == records[1:2]
    response = client.get(f"/api/{resource}", params={"offset": 100})
    assert response.status_code == 200, response.text
    assert response.json() == []


def test_default_page_size_is_twenty(client: TestClient) -> None:
    """Без параметров выдача ограничена двадцатью; limit=100 раскрывает остаток."""
    for index in range(22):
        create(
            client,
            "customers",
            {"name": f"Клиент {index}", "email": f"page-{index}@example.com"},
        )
    first_page = client.get("/api/customers").json()
    all_records = client.get("/api/customers", params={"limit": 100}).json()
    assert len(first_page) == 20
    assert len(all_records) == 22
    assert first_page == all_records[:20]
