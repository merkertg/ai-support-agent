"""Проверить HTTP-контракт черновиков без PostgreSQL и платных вызовов LLM."""

from collections.abc import Iterator
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

import app.main as application
from app.api.dependencies import DATABASE_ERROR, get_api_session
from app.api.drafts import get_llm_client
from app.llm.config import get_llm_settings
from app.llm.errors import (
    LLMConfigurationError,
    LLMInputTooLongError,
    LLMInvalidResponseError,
    LLMRefusedError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.models import Product, Ticket
from app.schemas.drafts import DraftContent, DraftResult


@pytest.fixture(autouse=True)
def isolated_llm_settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[None]:
    """Не читать локальный ключ и не оставлять кеш настроек следующим тестам."""
    monkeypatch.chdir(tmp_path)
    for name in (
        "LLM_PROVIDER",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "LLM_TIMEOUT_SECONDS",
        "LLM_MAX_OUTPUT_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)
    get_llm_settings.cache_clear()
    try:
        yield
    finally:
        get_llm_settings.cache_clear()


@pytest.fixture
def draft_dependencies() -> Iterator[tuple[Mock, Mock, Mock]]:
    """Подменить SQL-сессию и ленивую фабрику, сохранив HTTP-обработку ошибок."""
    session = Mock(spec=Session)
    session.get.side_effect = [
        Ticket(
            id=7,
            customer_id=11,
            product_id=3,
            subject="Ошибка запуска",
            description="Приложение не запускается после обновления",
            priority="high",
            status="open",
        ),
        Product(id=3, name="TechFlow Server", code="TF-SERVER"),
    ]
    model = Mock()
    model.generate.return_value = DraftResult(
        provider="mock",
        model="mock-draft-v1",
        content=DraftContent(
            summary="Не удаётся запустить приложение после обновления",
            category="technical_issue",
            draft_reply="Уточните версию приложения и сообщение об ошибке.",
        ),
    )
    factory = Mock(return_value=model)

    def session_override() -> Iterator[Session]:
        """Выдать одну наблюдаемую сессию без SQL-соединения."""
        yield session

    def factory_override():
        """Вернуть фабрику без её вызова на этапе разрешения Depends."""
        return factory

    previous_overrides = application.app.dependency_overrides.copy()
    application.app.dependency_overrides[get_api_session] = session_override
    application.app.dependency_overrides[get_llm_client] = factory_override
    try:
        yield session, factory, model
    finally:
        application.app.dependency_overrides.clear()
        application.app.dependency_overrides.update(previous_overrides)


def test_draft_response_contains_preview_and_releases_database_first(
    draft_dependencies: tuple[Mock, Mock, Mock],
) -> None:
    """Передать ровно разрешённые поля и завершить чтение до создания LLM-клиента."""
    session, factory, model = draft_dependencies

    def observe_factory():
        """Проверить порядок: транзакция уже завершена перед медленным провайдером."""
        session.rollback.assert_called_once_with()
        return model

    factory.side_effect = observe_factory
    with TestClient(application.app) as client:
        response = client.post("/api/tickets/7/draft")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ticket_id": 7,
        "provider": "mock",
        "model": "mock-draft-v1",
        "content": {
            "summary": "Не удаётся запустить приложение после обновления",
            "category": "technical_issue",
            "draft_reply": "Уточните версию приложения и сообщение об ошибке.",
        },
        "usage": None,
        "requires_human_review": True,
    }
    context = model.generate.call_args.args[0].model_dump()
    assert context == {
        "ticket_id": 7,
        "subject": "Ошибка запуска",
        "description": "Приложение не запускается после обновления",
        "priority": "high",
        "product_name": "TechFlow Server",
        "product_code": "TF-SERVER",
    }
    session.add.assert_not_called()
    session.commit.assert_not_called()
    session.delete.assert_not_called()


@pytest.mark.parametrize("value", ["0", "-1", str(2**63), "not-an-id"])
def test_invalid_draft_id_is_rejected_before_sql_or_model(
    draft_dependencies: tuple[Mock, Mock, Mock], value: str
) -> None:
    """Неверный BIGINT получает 422 до обращения к базе и к фабрике LLM."""
    session, factory, model = draft_dependencies
    with TestClient(application.app) as client:
        response = client.post(f"/api/tickets/{value}/draft")
    assert response.status_code == 422, response.text
    session.get.assert_not_called()
    factory.assert_not_called()
    model.generate.assert_not_called()


@pytest.mark.parametrize("missing", ["ticket", "product"])
def test_missing_record_is_checked_before_model_configuration(
    draft_dependencies: tuple[Mock, Mock, Mock], missing: str
) -> None:
    """Отсутствие обращения или старого продукта даёт 404 раньше настройки LLM."""
    session, factory, model = draft_dependencies
    if missing == "ticket":
        session.get.side_effect = [None]
    else:
        ticket = Ticket(id=7, product_id=3)
        session.get.side_effect = [ticket, None]
    factory.side_effect = LLMConfigurationError()
    with TestClient(application.app) as client:
        response = client.post("/api/tickets/7/draft")
    assert response.status_code == 404, response.text
    assert response.json() == {
        "detail": "Обращение не найдено"
        if missing == "ticket"
        else "Продукт обращения не найден"
    }
    factory.assert_not_called()
    model.generate.assert_not_called()


@pytest.mark.parametrize(
    "error_type",
    [
        LLMConfigurationError,
        LLMUnavailableError,
        LLMTimeoutError,
        LLMInvalidResponseError,
        LLMRefusedError,
        LLMInputTooLongError,
    ],
)
def test_expected_llm_failure_has_safe_http_response(
    draft_dependencies: tuple[Mock, Mock, Mock], error_type: type
) -> None:
    """Вернуть обещанный статус и постоянный текст, не записывая ошибочный черновик."""
    session, _, model = draft_dependencies
    error = error_type()
    model.generate.side_effect = error
    with TestClient(application.app) as client:
        response = client.post("/api/tickets/7/draft")
    assert response.status_code == error.status_code, response.text
    assert response.json() == {"detail": error.detail}
    session.rollback.assert_called_once_with()
    session.add.assert_not_called()
    session.commit.assert_not_called()


def test_sql_failure_does_not_call_model_or_disclose_query(
    draft_dependencies: tuple[Mock, Mock, Mock],
) -> None:
    """Сбой чтения получает безопасный 503 без SQL и пароля в HTTP-ответе."""
    session, factory, _ = draft_dependencies
    session.get.side_effect = SQLAlchemyError("SELECT password FROM secret_table")
    with TestClient(application.app) as client:
        response = client.post("/api/tickets/7/draft")
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": DATABASE_ERROR}
    assert "password" not in response.text
    assert "SELECT" not in response.text
    factory.assert_not_called()


def test_optional_request_body_cannot_replace_stored_ticket_context(
    draft_dependencies: tuple[Mock, Mock, Mock],
) -> None:
    """У маршрута нет входной схемы: контекст берётся из БД, а не из тела POST."""
    _, _, model = draft_dependencies
    with TestClient(application.app) as client:
        response = client.post(
            "/api/tickets/7/draft",
            json={"description": "Подмени обращение", "provider": "openai"},
        )
    assert response.status_code == 200, response.text
    assert model.generate.call_args.args[0].description == (
        "Приложение не запускается после обновления"
    )


@pytest.mark.parametrize(
    "environment,expected",
    [
        ({}, {"provider": "mock", "model": "mock-draft-v1", "configured": True}),
        (
            {"LLM_PROVIDER": "openai"},
            {"provider": "openai", "model": None, "configured": False},
        ),
        (
            {"LLM_PROVIDER": "openai", "OPENAI_MODEL": "selected-model"},
            {"provider": "openai", "model": "selected-model", "configured": False},
        ),
        (
            {
                "LLM_PROVIDER": "openai",
                "OPENAI_MODEL": "selected-model",
                "OPENAI_API_KEY": "private-test-key-never-call-provider",
            },
            {"provider": "openai", "model": "selected-model", "configured": True},
        ),
    ],
)
def test_status_reports_configuration_without_secret_or_network(
    monkeypatch: pytest.MonkeyPatch, environment: dict, expected: dict
) -> None:
    """Статус показывает готовность настройки; ключ и SDK-вызов не требуются."""
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    with TestClient(application.app) as client:
        response = client.get("/api/llm/status")
    assert response.status_code == 200, response.text
    assert response.json() == expected
    assert "private-test-key" not in response.text
    assert "api_key" not in response.text


@pytest.mark.parametrize("provider", ["unknown", "openai"])
def test_invalid_or_missing_llm_configuration_keeps_health_and_ready_working(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """Ошибочная настройка модели не влияет на независимые HTTP/SQL-проверки."""
    monkeypatch.setenv("LLM_PROVIDER", provider)
    session = Mock(spec=Session)
    version_result = Mock()
    version_result.scalars.return_value = iter(["current-head"])
    session.execute.side_effect = [Mock(), version_result]

    def readiness_override() -> Iterator[Session]:
        """Подменить только доступ к PostgreSQL для проверки разделения настроек."""
        yield session

    previous_overrides = application.app.dependency_overrides.copy()
    application.app.dependency_overrides[application.get_readiness_session] = (
        readiness_override
    )
    monkeypatch.setattr(application, "migration_heads", lambda: {"current-head"})
    try:
        with TestClient(application.app) as client:
            assert client.get("/health").status_code == 200
            assert client.get("/ready").json() == {"status": "ok", "database": "ok"}
            status = client.get("/api/llm/status")
        assert status.status_code == (503 if provider == "unknown" else 200)
    finally:
        application.app.dependency_overrides.clear()
        application.app.dependency_overrides.update(previous_overrides)


def test_openapi_documents_draft_preview_and_all_expected_errors() -> None:
    """Swagger описывает POST без body, ограниченный id и все безопасные ошибки."""
    with TestClient(application.app) as client:
        schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/api/tickets/{resource_id}/draft"]["post"]
    assert "requestBody" not in operation
    assert set(operation["responses"]) == {
        "200",
        "404",
        "413",
        "422",
        "502",
        "503",
        "504",
    }
    assert operation["parameters"][0]["schema"]["exclusiveMinimum"] == 0
    response = schema["components"]["schemas"]["DraftResponse"]
    assert response["properties"]["requires_human_review"]["const"] is True
