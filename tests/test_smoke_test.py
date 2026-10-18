"""Проверить CLI smoke-теста на настоящем локальном HTTP-сервере."""

import json
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke_test.py"


def valid_responses(
    check_database: bool = False, check_api: bool = False, check_llm: bool = False
) -> dict[str, tuple[int, bytes]]:
    """Собрать контракт этапа 01 либо расширенный контракт готовности БД."""
    check_api = check_api or check_llm
    check_database = check_database or check_api
    paths = {"/health": {"get": {}}}
    if check_api:
        for resource in ("customers", "products", "licenses", "tickets"):
            paths[f"/api/{resource}"] = {"get": {}, "post": {}}
            paths[f"/api/{resource}/{{id}}"] = {"get": {}}
    if check_llm:
        paths["/api/llm/status"] = {"get": {}}
        paths["/api/tickets/{resource_id}/draft"] = {"post": {}}
    if check_database:
        paths["/ready"] = {"get": {}}
    responses = {
        "/health": (
            200,
            json.dumps({"status": "ok", "service": "ai-support-agent"}).encode(),
        ),
        "/docs": (200, b"<html>SwaggerUIBundle({})</html>"),
        "/openapi.json": (
            200,
            json.dumps(
                {
                    "info": {"title": "AI Support Agent"},
                    "paths": paths,
                }
            ).encode(),
        ),
    }
    if check_database:
        responses["/ready"] = (200, b'{"status": "ok", "database": "ok"}')
    if check_api:
        for resource in ("customers", "products", "licenses", "tickets"):
            responses[f"/api/{resource}?limit=1"] = (200, b"[]")
    if check_llm:
        responses.update(
            {
                "/api/llm/status": (
                    200,
                    b'{"provider":"mock","model":"mock-draft-v1","configured":true}',
                ),
                "/api/customers": (201, b'{"id":11}'),
                "/api/products": (201, b'{"id":12}'),
                "/api/tickets": (201, b'{"id":13,"status":"open"}'),
                "/api/tickets/13": (200, b'{"id":13,"status":"open"}'),
                "/api/tickets/13/draft": (
                    200,
                    json.dumps(
                        {
                            "ticket_id": 13,
                            "provider": "mock",
                            "model": "mock-draft-v1",
                            "requires_human_review": True,
                            "usage": None,
                            "content": {
                                "summary": "Учебный разбор",
                                "category": "other",
                                "draft_reply": (
                                    "Демонстрационный черновик. "
                                    "Реальная модель не вызывалась."
                                ),
                            },
                        }
                    ).encode(),
                ),
            }
        )
    return responses


@contextmanager
def http_server(responses: dict[str, tuple[int, bytes]]):
    """Запустить изолированный HTTP-сервер и гарантированно закрыть его."""
    requests = Counter()

    class Handler(BaseHTTPRequestHandler):
        """Выдать ответы сценария и посчитать реальные HTTP-запросы."""

        def do_GET(self) -> None:
            """Обработать GET так же, как внешний сервис для smoke CLI."""
            requests[self.path] += 1
            status, body = responses.get(self.path, (404, b"Not found"))
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            """Принять тело POST и выдать отдельный ответ сценария без реальной БД."""
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.do_GET()

        def log_message(self, format: str, *args) -> None:
            """Не засорять вывод Pytest служебными логами фиктивного сервера."""

    # Порт 0 поручает ОС выбрать свободный порт и устраняет конфликты тестов.
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def run_smoke(
    url: str,
    timeout: str = "2",
    check_database: bool = False,
    check_api: bool = False,
    check_llm: bool = False,
) -> subprocess.CompletedProcess:
    """Вызвать CLI отдельным процессом с ограничением времени самого теста."""
    command = [sys.executable, str(SCRIPT), "--base-url", url, "--timeout", timeout]
    if check_database:
        command.append("--check-database")
    if check_api:
        command.append("--check-api")
    if check_llm:
        command.append("--check-llm")
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )


def test_cli_accepts_working_api() -> None:
    """Успех требует доступности всех трёх маршрутов через настоящий HTTP."""
    with http_server(valid_responses()) as (url, requests):
        result = run_smoke(url)
    assert result.returncode == 0, result.stderr
    assert requests == Counter({"/health": 1, "/docs": 1, "/openapi.json": 1})
    assert "Smoke test пройден" in result.stdout


@pytest.mark.parametrize(
    ("endpoint", "response", "message"),
    [
        ("/health", (503, b"starting"), "HTTP 503"),
        ("/health", (200, b"broken json"), "невалидный JSON"),
        ("/health", (200, b"[]"), "JSON-объект"),
        ("/health", (200, b'{"status": "ok"}'), "ожидался"),
        ("/docs", (404, b"Not found"), "HTTP 404"),
        ("/docs", (200, b"plain HTML"), "SwaggerUIBundle"),
        (
            "/openapi.json",
            (200, b'{"info": {"title": "Another API"}, "paths": {}}'),
            "info.title",
        ),
        (
            "/openapi.json",
            (200, b'{"info": {"title": "AI Support Agent"}, "paths": {}}'),
            "GET /health",
        ),
    ],
)
def test_cli_rejects_bad_responses_without_retry(
    endpoint: str, response: tuple[int, bytes], message: str
) -> None:
    """Ошибка контракта немедленно блокирует CI и не скрывается повторами."""
    responses = valid_responses()
    responses[endpoint] = response
    with http_server(responses) as (url, requests):
        result = run_smoke(url)
    assert result.returncode == 1
    assert message in result.stderr
    assert requests[endpoint] == 1
    if endpoint == "/health":
        assert requests["/docs"] == 0


def test_cli_times_out_when_service_is_unavailable() -> None:
    """Недоступный порт ограничен заданным дедлайном, а не бесконечным retry."""
    # Резервируем порт без listen(): он свободен от чужих сервисов, но недоступен.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{reserved.getsockname()[1]}"
        started = time.monotonic()
        result = run_smoke(url, timeout="0.2")
        elapsed = time.monotonic() - started
    assert result.returncode == 1
    assert "тайм-аут" in result.stderr or "не запустился" in result.stderr
    assert elapsed < 2


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf", "invalid"])
def test_cli_rejects_invalid_timeout(timeout: str) -> None:
    """Неправильный тайм-аут должен завершаться ошибкой аргументов до сети."""
    result = run_smoke("http://127.0.0.1:1", timeout=timeout)
    assert result.returncode == 2
    assert "тайм-аут" in result.stderr


def test_cli_accepts_database_readiness() -> None:
    """Флаг готовности проверяет четыре реальных маршрута и GET /ready в схеме."""
    with http_server(valid_responses(check_database=True)) as (url, requests):
        result = run_smoke(url, check_database=True)
    assert result.returncode == 0, result.stderr
    assert requests == Counter(
        {"/health": 1, "/ready": 1, "/docs": 1, "/openapi.json": 1}
    )
    assert "OK /ready" in result.stdout
    assert "GET /ready" in result.stdout


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ((503, b'{"status": "unavailable"}'), "HTTP 503"),
        ((200, b"broken json"), "невалидный JSON"),
        ((200, b"[]"), "JSON-объект"),
        ((200, b'{"status": "ok"}'), "ожидался"),
        ((200, b'{"status": "ok", "database": "unavailable"}'), "ожидался"),
    ],
)
def test_cli_rejects_bad_database_readiness_without_retry(
    response: tuple[int, bytes], message: str
) -> None:
    """HTTP 503 и неверные данные /ready немедленно блокируют выпуск образа."""
    responses = valid_responses(check_database=True)
    responses["/ready"] = response
    with http_server(responses) as (url, requests):
        result = run_smoke(url, check_database=True)
    assert result.returncode == 1
    assert message in result.stderr
    assert requests == Counter({"/health": 1, "/ready": 1})


@pytest.mark.parametrize("ready_path", [None, {}, {"post": {}}, {"get": []}])
def test_cli_requires_readiness_operation_in_openapi(ready_path: object) -> None:
    """Одного ответа /ready мало: OpenAPI должен содержать именно GET операцию."""
    responses = valid_responses(check_database=True)
    schema = json.loads(responses["/openapi.json"][1])
    if ready_path is None:
        schema["paths"].pop("/ready")
    else:
        schema["paths"]["/ready"] = ready_path
    responses["/openapi.json"] = (200, json.dumps(schema).encode())
    with http_server(responses) as (url, requests):
        result = run_smoke(url, check_database=True)
    assert result.returncode == 1
    assert "GET /ready" in result.stderr
    assert requests["/ready"] == 1
    assert requests["/openapi.json"] == 1


def test_cli_accepts_business_api_and_requires_readiness() -> None:
    """Флаг API включает /ready, OpenAPI и четыре списка без POST-запросов."""
    responses = valid_responses(check_api=True)
    responses["/api/customers?limit=1"] = (200, b'[{"id": 7, "name": "Customer"}]')
    with http_server(responses) as (url, requests):
        result = run_smoke(url, check_api=True)
    assert result.returncode == 0, result.stderr
    assert requests == Counter({endpoint: 1 for endpoint in responses})
    assert "OK /ready" in result.stdout
    assert "OK /api/tickets?limit=1" in result.stdout


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"not-json", "невалидный JSON"),
        (b"{}", "JSON-массив"),
        (b'[{"id": 1}, {"id": 2}]', "JSON-массив"),
        (b'["ticket"]', "положительным id"),
        (b'[{"id": true}]', "положительным id"),
        (b'[{"id": 0}]', "положительным id"),
    ],
)
def test_cli_rejects_invalid_business_list(body: bytes, message: str) -> None:
    """Рабочие /health и /ready не скрывают ошибку предметного API."""
    responses = valid_responses(check_api=True)
    responses["/api/licenses?limit=1"] = (200, body)
    with http_server(responses) as (url, requests):
        result = run_smoke(url, check_api=True)
    assert result.returncode == 1
    assert message in result.stderr
    assert requests["/api/licenses?limit=1"] == 1
    assert requests["/api/tickets?limit=1"] == 0


@pytest.mark.parametrize("method", ["get", "post", "item"])
def test_cli_rejects_missing_business_operation(method: str) -> None:
    """Потеря любой из трёх операций ресурса блокирует публикацию образа."""
    responses = valid_responses(check_api=True)
    schema = json.loads(responses["/openapi.json"][1])
    path = "/api/products/{id}" if method == "item" else "/api/products"
    del schema["paths"][path]["get" if method == "item" else method]
    responses["/openapi.json"] = (200, json.dumps(schema).encode())
    with http_server(responses) as (url, requests):
        result = run_smoke(url, check_api=True)
    assert result.returncode == 1
    assert "отсутствует операция" in result.stderr
    assert requests["/api/products?limit=1"] == 0


def test_cli_rejects_business_http_error_without_retry() -> None:
    """Ошибка SQL/HTTP в списке немедленно завершает проверку контейнера."""
    responses = valid_responses(check_api=True)
    responses["/api/tickets?limit=1"] = (503, b'{"detail": "database unavailable"}')
    with http_server(responses) as (url, requests):
        result = run_smoke(url, check_api=True)
    assert result.returncode == 1
    assert "HTTP 503" in result.stderr
    assert requests["/api/tickets?limit=1"] == 1


def test_cli_checks_mock_draft_and_unchanged_ticket() -> None:
    """Явный флаг проверяет mock и полную цепочку HTTP без платного провайдера."""
    responses = valid_responses(check_llm=True)
    with http_server(responses) as (url, requests):
        result = run_smoke(url, check_llm=True)
    assert result.returncode == 0, result.stderr
    assert requests == Counter({endpoint: 1 for endpoint in responses})
    assert sum(requests.values()) == 14
    assert "обращение не изменилось" in result.stdout


@pytest.mark.parametrize(
    "provider,configured", [("openai", True), ("mock", False), ("unknown", True)]
)
def test_cli_refuses_llm_mode_before_any_post(provider: str, configured: bool) -> None:
    """Реальный/неизвестный провайдер не вызывает ни платный запрос, ни записи."""
    responses = valid_responses(check_llm=True)
    responses["/api/llm/status"] = (
        200,
        json.dumps(
            {
                "provider": provider,
                "model": "mock-draft-v1",
                "configured": configured,
            }
        ).encode(),
    )
    with http_server(responses) as (url, requests):
        result = run_smoke(url, check_llm=True)
    assert result.returncode == 1
    assert "POST не выполнялись" in result.stderr
    assert requests["/api/customers"] == 0
    assert requests["/api/tickets/13/draft"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"ticket_id": 99},
        {"provider": "openai"},
        {"model": "unknown"},
        {"requires_human_review": False},
        {"content": None},
        {
            "content": {
                "summary": "Учебный",
                "category": "other",
                "draft_reply": "Ответ без пометки",
            }
        },
    ],
)
def test_cli_rejects_wrong_mock_draft(changes: dict) -> None:
    """Отклонить сломанный контракт черновика или исчезнувшую демо-пометку."""
    responses = valid_responses(check_llm=True)
    draft = json.loads(responses["/api/tickets/13/draft"][1])
    responses["/api/tickets/13/draft"] = (200, json.dumps(draft | changes).encode())
    with http_server(responses) as (url, requests):
        result = run_smoke(url, check_llm=True)
    assert result.returncode == 1
    assert "неверный контракт" in result.stderr
    assert requests["/api/tickets/13/draft"] == 1


def test_cli_rejects_ticket_mutation_by_draft() -> None:
    """Изменение статуса обращения при генерации предпросмотра блокирует выпуск."""
    responses = valid_responses(check_llm=True)
    responses["/api/tickets/13"] = (200, b'{"id":13,"status":"closed"}')
    with http_server(responses) as (url, requests):
        result = run_smoke(url, check_llm=True)
    assert result.returncode == 1
    assert "изменил сохранённое обращение" in result.stderr
