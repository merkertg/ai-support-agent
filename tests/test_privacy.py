"""Обезличивание без БД: очистка трассы и итога, контракт API и командная строка."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.privacy as privacy_cli
from app.api.dependencies import get_api_session
from app.api.security import get_principal
from app.db.session import DatabaseNotConfigured
from app.main import app
from app.schemas.privacy import ErasureReport, RetentionReport, RetentionSkip
from app.services import privacy
from app.services.auth import Principal
from app.services.errors import ResourceConflict, ResourceInvalid

REPORT = ErasureReport(
    customer_id=5,
    erased_at=datetime(2026, 10, 19, tzinfo=UTC),
    tickets=2,
    messages=1,
    agent_runs=1,
    suggestions=1,
    workflow_threads=1,
    licenses_kept=1,
)


def test_trace_keeps_only_call_names() -> None:
    """Аргументы и ответы инструментов заменяются; чужие элементы отбрасываются."""
    trace = [
        {
            "call_id": "call-1",
            "name": "get_customer",
            "arguments": {"customer_id": 5},
            "result": {"name": "Анна", "email": "anna@example.com"},
        },
        "мусор",
        {"name": None},
    ]
    assert privacy.scrub_trace(trace) == [
        {
            "call_id": "call-1",
            "name": "get_customer",
            "arguments": {},
            "result": {"erased": True},
        },
        {"call_id": "", "name": "", "arguments": {}, "result": {"erased": True}},
    ]
    assert privacy.scrub_trace(None) == []
    assert privacy.scrub_trace({"name": "x"}) == []


def test_result_keeps_category_and_article_keys_only() -> None:
    """Текст итога удаляется; неизвестная категория становится other."""
    result = {
        "summary": "Анна не может запустить сервер",
        "category": "technical_issue",
        "draft_reply": "Анна, перезапустите службу",
        "sources": ["KB-SERVER-START", 7],
    }
    assert privacy.scrub_result(result) == {
        "summary": privacy.ERASED_TEXT,
        "category": "technical_issue",
        "draft_reply": privacy.ERASED_TEXT,
        "sources": ["KB-SERVER-START"],
    }
    legacy = privacy.scrub_result({"category": "unknown", "sources": "KB"})
    assert legacy["category"] == "other" and legacy["sources"] == []
    assert privacy.scrub_result(None) is None


def test_erased_email_is_unique_and_undeliverable() -> None:
    """Заглушка зависит от id и использует зарезервированный домен .invalid."""
    assert privacy.erased_email(5) == "erased-5@erased.invalid"
    assert privacy.erased_email(5) != privacy.erased_email(6)


@pytest.mark.parametrize(
    "days,limit,message",
    [(7, 500, "не меньше 30"), (30, 0, "от 1 до 1000"), (30, 1001, "от 1 до 1000")],
)
def test_retention_rejects_unsafe_settings_before_sql(days, limit, message) -> None:
    """Слишком короткий срок или пачка вне границ отклоняются без запроса к БД."""
    session = Mock(spec=Session)
    with pytest.raises(ResourceInvalid, match=message):
        privacy.apply_retention(session, days, limit=limit)
    assert session.mock_calls == []


@pytest.fixture
def api_session() -> Iterator[Mock]:
    """Подставить наблюдаемую сессию в маршруты API."""
    session = Mock(spec=Session)

    def dependency() -> Iterator[Mock]:
        """Выдать заглушку без подключения к БД."""
        yield session

    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_api_session] = dependency
    try:
        yield session
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def test_erase_route_returns_report(monkeypatch, api_session: Mock) -> None:
    """Администратор получает отчёт; сервис получает id и введённый email."""
    calls = []

    def erase(session, customer_id, confirm_email):
        """Записать аргументы вызова сервиса."""
        calls.append((customer_id, confirm_email))
        return REPORT

    monkeypatch.setattr(privacy, "erase_customer", erase)
    with TestClient(app) as client:
        response = client.post(
            "/api/customers/5/erase", json={"confirm_email": "anna@example.com"}
        )
    assert response.status_code == 200, response.text
    assert response.json() == REPORT.model_dump(mode="json")
    assert calls == [(5, "anna@example.com")]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"confirm_email": "не адрес"},
        {"confirm_email": "anna@example.com", "force": True},
    ],
)
def test_erase_route_validates_body_before_service(
    monkeypatch, api_session: Mock, body
) -> None:
    """Без корректного email и с лишними полями сервис не вызывается."""
    service = Mock()
    monkeypatch.setattr(privacy, "erase_customer", service)
    with TestClient(app) as client:
        response = client.post("/api/customers/5/erase", json=body)
    assert response.status_code == 422
    service.assert_not_called()


@pytest.mark.parametrize("role", ["operator", "reviewer"])
def test_erase_route_requires_admin(monkeypatch, api_session: Mock, role) -> None:
    """Роль ниже admin получает 403 до сервиса."""
    service = Mock()
    monkeypatch.setattr(privacy, "erase_customer", service)
    app.dependency_overrides[get_principal] = lambda: Principal(
        user_id=2,
        name="Сотрудник",
        email="staff@example.com",
        role=role,
        token_id=2,
        token_name="tests",
        token_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/customers/5/erase", json={"confirm_email": "anna@example.com"}
        )
    assert response.status_code == 403
    service.assert_not_called()


def test_erase_route_maps_service_errors(monkeypatch, api_session: Mock) -> None:
    """409 и 422 сервиса возвращаются единым JSON без данных клиента."""
    for error in (
        ResourceConflict(privacy.ALREADY_ERASED),
        ResourceInvalid(privacy.CONFIRMATION_MISMATCH),
    ):
        monkeypatch.setattr(privacy, "erase_customer", Mock(side_effect=error))
        with TestClient(app) as client:
            response = client.post(
                "/api/customers/5/erase", json={"confirm_email": "anna@example.com"}
            )
        assert response.status_code == error.status_code
        assert response.json() == {"detail": error.detail}


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch):
    """Запускать CLI без настоящего движка БД."""
    monkeypatch.setattr(privacy_cli, "get_engine", lambda: Mock())
    monkeypatch.setattr(privacy_cli, "dispose_engine", lambda: None)
    return privacy_cli.main


def test_cli_erase_prints_json_report(cli, monkeypatch, capsys) -> None:
    """erase печатает отчёт JSON и возвращает 0."""
    monkeypatch.setattr(privacy, "erase_customer", Mock(return_value=REPORT))
    code = cli(["erase", "--customer-id", "5", "--confirm-email", "a@example.com"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["customer_id"] == 5


def test_cli_retention_plan_explains_apply(cli, monkeypatch, capsys) -> None:
    """Пробный план печатает отчёт и подсказку про --apply в stderr."""
    report = RetentionReport(
        applied=False,
        older_than_days=365,
        cutoff=datetime(2025, 10, 19, tzinfo=UTC),
        ticket_ids=[3],
        skipped=[RetentionSkip(ticket_id=4, reason="выполняется анализ (запуск 9)")],
        tickets=1,
    )
    service = Mock(return_value=report)
    monkeypatch.setattr(privacy, "apply_retention", service)
    assert cli(["retention", "--older-than-days", "365"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["skipped"][0]["ticket_id"] == 4
    assert "--apply" in captured.err
    assert service.call_args.kwargs == {"apply": False, "limit": 500}
    report.applied = True
    assert cli(["retention", "--older-than-days", "365", "--apply"]) == 0
    assert "--apply" not in capsys.readouterr().err
    assert service.call_args.kwargs == {"apply": True, "limit": 500}


@pytest.mark.parametrize(
    "error,message",
    [
        (ResourceConflict(privacy.ALREADY_ERASED), privacy.ALREADY_ERASED),
        (DatabaseNotConfigured("DATABASE_URL не задан"), "DATABASE_URL"),
    ],
)
def test_cli_reports_understood_errors(cli, monkeypatch, capsys, error, message):
    """Понятные ошибки дают код 1 и сообщение в stderr без трассировки."""
    monkeypatch.setattr(privacy, "erase_customer", Mock(side_effect=error))
    code = cli(["erase", "--customer-id", "5", "--confirm-email", "a@example.com"])
    assert code == 1
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["erase", "--customer-id", "0", "--confirm-email", "a@example.com"],
        ["erase", "--customer-id", "x", "--confirm-email", "a@example.com"],
        ["retention"],
        ["retention", "--older-than-days", "30", "--limit", "-1"],
    ],
)
def test_cli_rejects_invalid_arguments(cli, argv) -> None:
    """Неверные аргументы завершают команду кодом 2 до обращения к БД."""
    with pytest.raises(SystemExit) as stopped:
        cli(argv)
    assert stopped.value.code == 2
