"""Проверить границу между JSON модели и разрешёнными Python-инструментами."""

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from sqlalchemy import Engine

import app.agent.tools as tools
from app.agent.contracts import AgentContext


@pytest.fixture
def executor(monkeypatch: pytest.MonkeyPatch) -> tools.SQLToolExecutor:
    """Запретить любой SQL: неверные аргументы должны остановиться раньше."""
    monkeypatch.setattr(
        tools, "Session", Mock(side_effect=AssertionError("SQL forbidden"))
    )
    return tools.SQLToolExecutor(
        Mock(spec=Engine),
        AgentContext(1, 2, 3),
        checked_at=datetime(2026, 10, 18, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    "name", ["delete_customer", "__import__", "get_engine", "send_email"]
)
def test_unknown_tool_cannot_resolve_python_name(executor, name: str) -> None:
    """Произвольное имя не открывает сессию и не вызывает Python-функцию."""
    arguments, result = executor(name, '{"id":2}')
    assert arguments == {}
    assert result["error"]["code"] == "unknown_tool"


@pytest.mark.parametrize(
    "name,payload",
    [
        ("get_ticket", "not-json"),
        ("get_ticket", "[]"),
        ("get_ticket", '{"ticket_id":true}'),
        ("get_ticket", '{"ticket_id":1.0}'),
        ("get_ticket", '{"ticket_id":"1"}'),
        ("get_ticket", '{"ticket_id":0}'),
        ("get_ticket", '{"ticket_id":9223372036854775808}'),
        ("get_ticket", '{"ticket_id":1,"ticket_id":2}'),
        ("get_customer", '{"customer_id":2,"sql":"DROP TABLE customers"}'),
        ("get_license", '{"license_key":" "}'),
        ("get_license", '{"license_key":null}'),
        ("get_license", '{"license_key":"' + "x" * 256 + '"}'),
        ("get_license", "x" * 4097),
        ("get_ticket_history", '{"customer_id":2,"limit":6}'),
        ("get_ticket_history", '{"customer_id":2,"limit":0}'),
        ("get_ticket_history", '{"customer_id":2,"limit":true}'),
        ("get_ticket_history", '{"customer_id":2}'),
        ("search_knowledge", '{"query":"ok","limit":3}'),
        ("search_knowledge", '{"query":"сервер","limit":6}'),
        ("search_knowledge", '{"query":"сервер"}'),
        ("search_knowledge", '{"query":"' + "я" * 301 + '","limit":3}'),
        ("search_knowledge", '{"query":"сервер","limit":3,"product_code":"x"}'),
    ],
)
def test_invalid_arguments_never_reach_database(
    executor, name: str, payload: str
) -> None:
    """Отсечь типы, дубликаты JSON, лишние поля и превышение лимитов до SQL."""
    arguments, result = executor(name, payload)
    assert arguments == {}
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize(
    "name,payload",
    [
        ("get_ticket", '{"ticket_id":99}'),
        ("get_customer", '{"customer_id":99}'),
        ("get_ticket_history", '{"customer_id":99,"limit":5}'),
    ],
)
def test_other_customer_or_ticket_is_denied_before_sql(executor, name, payload) -> None:
    """Модель не расширяет доверенную область даже корректным положительным ID."""
    _, result = executor(name, payload)
    assert result["error"]["code"] == "out_of_scope"


def test_all_tool_schemas_require_declared_fields_and_forbid_extras() -> None:
    """Реестр пригоден для strict function calling без скрытых необязательных полей."""
    assert {item["name"] for item in tools.TOOL_DEFINITIONS} == {
        "get_ticket",
        "get_customer",
        "get_license",
        "get_ticket_history",
        "search_knowledge",
    }
    for item in tools.TOOL_DEFINITIONS:
        assert item["type"] == "function"
        assert item["strict"] is True
        schema = item["parameters"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
