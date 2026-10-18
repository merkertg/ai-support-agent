"""Журнал API: id запроса, JSON-строки и отсутствие секретов, SQL и query string."""

import asyncio
import io
import json
import logging
from collections.abc import Iterator

import httpx2
import openai
import pytest
from fastapi.testclient import TestClient
from psycopg import errors as psycopg_errors
from sqlalchemy.exc import OperationalError
from starlette.requests import Request

import app.main as api
from app.api.dependencies import get_api_session
from app.api.drafts import llm_error_handler
from app.api.errors import database_error_handler
from app.db.session import DatabaseNotConfigured
from app.llm.errors import LLMTimeoutError, LLMUnavailableError
from app.observability import (
    INTERNAL_ERROR,
    LOGGER_NAME,
    JSONFormatter,
    configure_logging,
    upstream_fields,
)

SECRET = "LIC-SECRET-0042"


@pytest.fixture
def app_logs() -> Iterator[list[dict]]:
    """Собрать JSON-строки журнала «app» в список словарей."""
    records: list[dict] = []

    class Collect(logging.Handler):
        """Сохранить отформатированную строку как разобранный JSON."""

        def emit(self, record: logging.LogRecord) -> None:
            """Проверить, что каждая строка — корректный JSON."""
            records.append(json.loads(self.format(record)))

    logger = logging.getLogger(LOGGER_NAME)
    handler = Collect()
    handler.setFormatter(JSONFormatter())
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def events(records: list[dict], name: str) -> list[dict]:
    """Выбрать записи одного события."""
    return [item for item in records if item["event"] == name]


def fake_request(path: str = "/api/licenses") -> Request:
    """Минимальный HTTP-запрос для прямого вызова обработчика ошибок."""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": f"license_key={SECRET}".encode(),
        }
    )


def test_request_id_is_generated_returned_and_quiet_health_not_logged(app_logs):
    """Каждый ответ получает id; успешный /health не засоряет журнал."""
    with TestClient(api.app) as client:
        response = client.get("/health")
    request_id = response.headers["X-Request-ID"]
    assert len(request_id) == 32 and int(request_id, 16) >= 0
    assert events(app_logs, "request") == []
    assert events(app_logs, "api_started")


@pytest.mark.parametrize(
    ("incoming", "kept"),
    [("proxy-req.0001", True), ("bad id\n<script>", False), ("short", False)],
)
def test_incoming_request_id_is_kept_only_when_safe(incoming, kept):
    """Id прокси сохраняется, небезопасный заменяется новым."""
    with TestClient(api.app) as client:
        response = client.get("/health", headers={"X-Request-ID": incoming})
    assert (response.headers["X-Request-ID"] == incoming) is kept


def test_request_log_omits_query_string(app_logs):
    """license_key из строки запроса не попадает в журнал."""
    with TestClient(api.app) as client:
        response = client.get(f"/api/no-such-route?license_key={SECRET}")
    assert response.status_code == 404
    [line] = events(app_logs, "request")
    assert line["path"] == "/api/no-such-route"
    assert line["status"] == 404
    assert line["request_id"] == response.headers["X-Request-ID"]
    assert SECRET not in json.dumps(app_logs)


def test_threadpool_log_carries_request_id(app_logs, monkeypatch):
    """Синхронный обработчик в рабочем потоке пишет тот же id, что и ответ."""

    def unavailable():
        """Сымитировать ненастроенную БД в зависимости /ready."""
        raise DatabaseNotConfigured()
        yield

    monkeypatch.setattr(api, "get_session", unavailable)
    with TestClient(api.app) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    [line] = events(app_logs, "readiness_failed")
    assert line["error"] == "DatabaseNotConfigured"
    assert line["request_id"] == response.headers["X-Request-ID"]
    [request] = events(app_logs, "request")
    assert request["status"] == 503 and request["level"] == "WARNING"


def test_database_error_logs_class_and_sqlstate_without_sql_or_address(app_logs):
    """В журнал не попадают SQL, параметры и адрес базы из текста драйвера."""
    error = OperationalError(
        "SELECT * FROM licenses WHERE license_key = %(key)s",
        {"key": SECRET},
        psycopg_errors.CannotConnectNow("connection to db-secret-host failed"),
    )
    response = asyncio.run(database_error_handler(fake_request(), error))
    assert response.status_code == 503
    [line] = events(app_logs, "database_error")
    assert line["error"] == "OperationalError"
    assert line["driver_error"] == "CannotConnectNow"
    assert line["sqlstate"] == psycopg_errors.CannotConnectNow.sqlstate
    text = json.dumps(app_logs, ensure_ascii=False)
    assert SECRET not in text and "SELECT" not in text and "db-secret" not in text


def test_psycopg_error_from_checkpointer_is_logged_with_sqlstate(app_logs):
    """Прямая ошибка psycopg тоже описывается классом и кодом."""
    error = psycopg_errors.UniqueViolation(f"duplicate key ({SECRET})")
    asyncio.run(database_error_handler(fake_request(), error))
    [line] = events(app_logs, "database_error")
    assert line["error"] == "UniqueViolation" and line["sqlstate"] == "23505"
    assert SECRET not in json.dumps(app_logs)


def test_llm_error_logs_upstream_class_but_not_its_text(app_logs):
    """Класс ошибки SDK и её id сохраняются; текст ответа провайдера — нет."""

    class ProviderStatusError(Exception):
        """Ошибка из модуля SDK с кодом, id и опасным текстом."""

        status_code = 429
        request_id = "req_upstream_1"

    ProviderStatusError.__module__ = "openai._exceptions"
    try:
        try:
            raise ProviderStatusError(f"quota exceeded for prompt {SECRET}")
        except ProviderStatusError:
            raise LLMUnavailableError() from None
    except LLMUnavailableError as error:
        response = asyncio.run(llm_error_handler(fake_request("/api/x"), error))
    assert response.status_code == 503
    [line] = events(app_logs, "llm_error")
    assert line["upstream_error"] == "ProviderStatusError"
    assert line["upstream_status"] == 429
    assert line["upstream_request_id"] == "req_upstream_1"
    assert SECRET not in json.dumps(app_logs)


def test_upstream_fields_find_real_sdk_timeout() -> None:
    """Настоящий тайм-аут SDK OpenAI распознаётся через __context__."""
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    try:
        try:
            raise openai.APITimeoutError(request=request)
        except openai.APITimeoutError:
            raise LLMTimeoutError() from None
    except LLMTimeoutError as error:
        fields = upstream_fields(error)
    assert fields["upstream_error"] == "APITimeoutError"
    assert upstream_fields(ValueError("local")) == {}


def test_unhandled_error_returns_safe_json_and_logs_traceback(app_logs):
    """Непредвиденная ошибка: JSON 500 с id, traceback только в журнале."""

    def broken_session():
        """Сломать зависимость маршрута непредвиденным исключением."""
        raise RuntimeError("unexpected state")

    previous = api.app.dependency_overrides.copy()
    api.app.dependency_overrides[get_api_session] = broken_session
    try:
        with TestClient(api.app) as client:
            response = client.get("/api/customers")
    finally:
        api.app.dependency_overrides.clear()
        api.app.dependency_overrides.update(previous)
    assert response.status_code == 500
    assert response.json() == {"detail": INTERNAL_ERROR}
    assert "unexpected state" not in response.text
    [line] = events(app_logs, "unhandled_error")
    assert line["request_id"] == response.headers["X-Request-ID"]
    assert "RuntimeError: unexpected state" in line["exception"]
    [request] = events(app_logs, "request")
    assert request["status"] == 500 and request["level"] == "WARNING"


def test_configure_logging_is_idempotent_and_validates_level(monkeypatch):
    """Повторная настройка не дублирует вывод; неизвестный уровень — INFO."""
    logger = logging.getLogger(LOGGER_NAME)
    saved = (list(logger.handlers), logger.level, logger.propagate)
    try:
        logger.handlers.clear()
        monkeypatch.setenv("LOG_LEVEL", "verbose")
        configure_logging()
        configure_logging()
        json_handlers = [h for h in logger.handlers if getattr(h, "app_json", False)]
        assert len(json_handlers) == 1
        assert logger.level == logging.INFO and logger.propagate is False
        configure_logging("debug")
        assert logger.level == logging.DEBUG
        stream = io.StringIO()
        json_handlers[0].setStream(stream)
        logger.warning("checked", extra={"fields": {"run_id": 7}})
        line = json.loads(stream.getvalue())
        assert line["event"] == "checked" and line["run_id"] == 7
        assert line["level"] == "WARNING" and line["ts"].endswith("+00:00")
    finally:
        logger.handlers[:] = saved[0]
        logger.setLevel(saved[1])
        logger.propagate = saved[2]
