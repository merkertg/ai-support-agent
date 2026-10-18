"""Проверить HTTP-контракт агента без PostgreSQL, внешней модели и API-ключа."""

from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

import app.main as application
from app.api.agents import get_agent_factory
from app.api.dependencies import DATABASE_ERROR, get_api_session
from app.llm.errors import LLMConfigurationError, LLMTimeoutError
from app.models import AgentRun, AgentSuggestion, Ticket
from app.schemas.agents import AgentRunRead
from app.services import agents
from app.services.agents import AgentRunFailure


@pytest.fixture
def agent_dependencies() -> Iterator[tuple[Mock, Mock]]:
    """Выдать наблюдаемые сессию и фабрику без соединений и SDK."""
    session = Mock(spec=Session)
    factory = Mock()

    def session_override() -> Iterator[Session]:
        """Вернуть SQL-заглушку текущему HTTP-запросу."""
        yield session

    def factory_override():
        """Выдать фабрику, не вызывая её до проверки существования обращения."""
        return factory

    previous = application.app.dependency_overrides.copy()
    application.app.dependency_overrides[get_api_session] = session_override
    application.app.dependency_overrides[get_agent_factory] = factory_override
    try:
        yield session, factory
    finally:
        application.app.dependency_overrides.clear()
        application.app.dependency_overrides.update(previous)


def run_response() -> AgentRunRead:
    """Собрать полный ожидаемый DTO с обязательной проверкой черновика человеком."""
    return AgentRunRead(
        id=12,
        ticket_id=7,
        status="completed",
        provider="mock",
        model="mock-agent-v1",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        finished_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        error_message=None,
        steps=2,
        tool_trace=[],
        usage=None,
        result={
            "summary": "Ошибка",
            "category": "other",
            "draft_reply": "Уточните версию.",
        },
        suggestion={"id": 2, "content": "Уточните версию.", "status": "pending"},
    )


@pytest.mark.parametrize(
    "body", [None, {}, {"license_key": None}, {"license_key": " LIC-42 "}]
)
def test_analyze_accepts_only_optional_license_hint(
    agent_dependencies: tuple[Mock, Mock], monkeypatch: pytest.MonkeyPatch, body
) -> None:
    """Пустой body допустим, а пробелы ключа нормализуются до вызова сервиса."""
    session, factory = agent_dependencies
    analyze = Mock(return_value=run_response())
    monkeypatch.setattr(agents, "analyze_ticket", analyze)
    with TestClient(application.app) as client:
        response = (
            client.post("/api/tickets/7/analyze")
            if body is None
            else client.post("/api/tickets/7/analyze", json=body)
        )
    assert response.status_code == 201, response.text
    assert response.json()["suggestion"]["requires_human_review"] is True
    assert analyze.call_args.args[:2] == (session, 7)
    assert analyze.call_args.args[2].license_key == (
        "LIC-42" if body and body.get("license_key") else None
    )
    assert analyze.call_args.args[3] is factory
    factory.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        {"status": "closed"},
        {"provider": "openai"},
        {"description": "Подменённый текст"},
        {"license_key": " "},
        {"license_key": "x" * 256},
        {"license_key": 123},
        [],
        "text",
    ],
)
def test_analyze_rejects_invalid_body_before_service(
    agent_dependencies: tuple[Mock, Mock], monkeypatch: pytest.MonkeyPatch, body
) -> None:
    """Команды изменения и неверные ключи дают 422 без SQL-записи и модели."""
    session, factory = agent_dependencies
    analyze = Mock()
    monkeypatch.setattr(agents, "analyze_ticket", analyze)
    with TestClient(application.app) as client:
        response = client.post("/api/tickets/7/analyze", json=body)
    assert response.status_code == 422, response.text
    analyze.assert_not_called()
    factory.assert_not_called()
    session.add.assert_not_called()


@pytest.mark.parametrize("path", ["/api/tickets/{}/analyze", "/api/agent-runs/{}"])
@pytest.mark.parametrize("resource_id", ["0", "-1", "wrong", str(2**63)])
def test_agent_routes_validate_resource_id_before_sql(
    agent_dependencies: tuple[Mock, Mock], path: str, resource_id: str
) -> None:
    """Оба маршрута ограничивают id диапазоном PostgreSQL BIGINT до чтения БД."""
    session, factory = agent_dependencies
    with TestClient(application.app) as client:
        url = path.format(resource_id)
        response = client.post(url) if url.endswith("analyze") else client.get(url)
    assert response.status_code == 422, response.text
    session.get.assert_not_called()
    factory.assert_not_called()


def test_missing_ticket_has_priority_over_provider_configuration(
    agent_dependencies: tuple[Mock, Mock],
) -> None:
    """Неизвестное обращение даёт 404 до фабрики и до записи AgentRun."""
    session, factory = agent_dependencies
    session.get.return_value = None
    factory.side_effect = LLMConfigurationError()
    with TestClient(application.app) as client:
        response = client.post("/api/tickets/999/analyze")
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "Обращение не найдено"}
    factory.assert_not_called()
    session.add.assert_not_called()
    session.commit.assert_not_called()


def test_bad_provider_configuration_does_not_create_running_record(
    agent_dependencies: tuple[Mock, Mock],
) -> None:
    """Настройка проверяется после освобождения соединения, до первой записи."""
    session, factory = agent_dependencies
    session.get.return_value = Ticket(id=7, customer_id=1, product_id=2)

    def unavailable_provider():
        """Убедиться, что конфигурационная ошибка случилась после конца чтения."""
        session.rollback.assert_called_once_with()
        raise LLMConfigurationError()

    factory.side_effect = unavailable_provider
    with TestClient(application.app) as client:
        response = client.post("/api/tickets/7/analyze")
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": LLMConfigurationError.detail}
    session.add.assert_not_called()
    session.commit.assert_not_called()


def test_runtime_error_returns_persisted_run_id(
    agent_dependencies: tuple[Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Тайм-аут можно найти отдельным GET по безопасному run_id из ошибки."""
    analyze = Mock(side_effect=AgentRunFailure.of(LLMTimeoutError(), 12))
    monkeypatch.setattr(agents, "analyze_ticket", analyze)
    with TestClient(application.app) as client:
        response = client.post("/api/tickets/7/analyze")
    assert response.status_code == 504, response.text
    assert response.json() == {"detail": LLMTimeoutError.detail, "run_id": 12}


def test_database_failure_does_not_expose_sql_or_key(
    agent_dependencies: tuple[Mock, Mock],
) -> None:
    """Неудачное чтение получает общий 503, а фабрика модели не вызывается."""
    session, factory = agent_dependencies
    session.get.side_effect = SQLAlchemyError("SELECT secret_key FROM private")
    with TestClient(application.app) as client:
        response = client.post("/api/tickets/7/analyze")
    assert response.status_code == 503
    assert response.json() == {"detail": DATABASE_ERROR}
    assert "secret" not in response.text
    factory.assert_not_called()


def test_get_unknown_run_returns_404(agent_dependencies: tuple[Mock, Mock]) -> None:
    """Чтение неизвестного запуска не создаёт запись и не вызывает провайдера."""
    session, factory = agent_dependencies
    session.get.return_value = None
    with TestClient(application.app) as client:
        response = client.get("/api/agent-runs/900")
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "Запуск агента не найден"}
    factory.assert_not_called()
    session.add.assert_not_called()


def test_get_run_copies_persisted_dto_and_finishes_read(
    agent_dependencies: tuple[Mock, Mock],
) -> None:
    """GET отдаёт сохранённые JSON-поля и предложение без повторной генерации."""
    session, factory = agent_dependencies
    expected = run_response()
    data = expected.model_dump(exclude={"suggestion"})
    session.get.return_value = AgentRun(**data)
    session.scalar.return_value = AgentSuggestion(
        id=2, agent_run_id=12, content="Уточните версию.", status="pending"
    )
    with TestClient(application.app) as client:
        response = client.get("/api/agent-runs/12")
    assert response.status_code == 200, response.text
    assert response.json() == expected.model_dump(mode="json")
    session.rollback.assert_called_once_with()
    factory.assert_not_called()
    session.add.assert_not_called()


def test_agent_openapi_documents_optional_input_and_failure_run_id() -> None:
    """Swagger объясняет необязательный body, 201 и способ чтения неудачного запуска."""
    with TestClient(application.app) as client:
        schema = client.get("/openapi.json").json()
    post = schema["paths"]["/api/tickets/{resource_id}/analyze"]["post"]
    assert not post["requestBody"].get("required", False)
    assert set(post["responses"]) == {"201", "404", "413", "422", "502", "503", "504"}
    assert (
        schema["components"]["schemas"]["AnalyzeRequest"]["additionalProperties"]
        is False
    )
    assert set(schema["components"]["schemas"]["AgentErrorRead"]["required"]) == {
        "detail",
        "run_id",
    }
    assert (
        schema["components"]["schemas"]["SuggestionRead"]["properties"][
            "requires_human_review"
        ]["const"]
        is True
    )
