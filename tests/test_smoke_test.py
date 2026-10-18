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


KNOWLEDGE_STATUS = {
    "configured": True,
    "embedding_provider": "mock",
    "embedding_model": "mock-hash-v1",
    "dimensions": 256,
    "collection": "support_knowledge",
    "index": "ready",
    "points": 27,
    "documents": 7,
}


def hit(key: str, scope: str) -> dict:
    """Описать найденный фрагмент так, как его возвращает API поиска."""
    return {
        "document_key": key,
        "title": "Статья",
        "section": "Раздел",
        "text": "Текст",
        "score": 0.5,
        "source_path": f"knowledge/{key.lower()}.md",
        "scope": scope,
    }


def knowledge_search(body: dict) -> tuple[int, bytes]:
    """Ответить на поиск по телу запроса: фильтр продукта меняет выдачу."""
    if body.get("product_code") == "techflow-server":
        results = [hit("KB-SERVER-START", "techflow-server")]
    else:
        results = [hit("KB-SUPPORT-DATA", "general")]
    return 200, json.dumps(
        {
            "query": body.get("query"),
            "embedding_signature": "mock:mock-hash-v1:256",
            "results": results,
        }
    ).encode()


def valid_responses(
    check_database: bool = False,
    check_api: bool = False,
    check_llm: bool = False,
    check_agent: bool = False,
    check_knowledge: bool = False,
    check_review: bool = False,
    review_body: dict | None = None,
    repeat_status: int = 409,
) -> dict:
    """Собрать контракт этапа 01 либо расширенный контракт готовности БД."""
    check_agent = check_agent or check_review
    check_llm = check_llm or check_agent
    check_knowledge = check_knowledge or check_agent
    check_api = check_api or check_llm or check_knowledge
    check_database = check_database or check_api
    paths = {"/health": {"get": {}}}
    if check_api:
        for resource in ("customers", "products", "licenses", "tickets"):
            paths[f"/api/{resource}"] = {"get": {}, "post": {}}
            paths[f"/api/{resource}/{{id}}"] = {"get": {}}
    if check_llm:
        paths["/api/llm/status"] = {"get": {}}
        paths["/api/tickets/{resource_id}/draft"] = {"post": {}}
    if check_knowledge:
        paths["/api/knowledge/status"] = {"get": {}}
        paths["/api/knowledge/search"] = {"post": {}}
    if check_agent:
        paths["/api/tickets/{resource_id}/analyze"] = {"post": {}}
        paths["/api/agent-runs/{resource_id}"] = {"get": {}}
    if check_review:
        paths.update(REVIEW_PATHS)
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
    if check_knowledge:
        responses["/api/knowledge/status"] = (
            200,
            json.dumps(KNOWLEDGE_STATUS).encode(),
        )
        responses["/api/knowledge/search"] = knowledge_search
    if check_agent:
        run = json.dumps(agent_run()).encode()
        responses.update(
            {
                "/api/licenses": (201, b'{"id":14}'),
                "/api/tickets/13/analyze": (201, run),
                "/api/agent-runs/21": (200, run),
            }
        )
    if check_review:
        add_review_responses(responses, review_body, repeat_status)
    return responses


REVIEW_TEXT = "Smoke: ответ проверен и одобрен оператором."
REVIEW_PATHS = {
    "/api/users": {"get": {}, "post": {}},
    "/api/agent-runs/{resource_id}/workflow": {"get": {}},
    "/api/suggestions/{resource_id}/review": {"post": {}},
}
WAITING = {
    "thread_id": "thread-21",
    "state": "awaiting_review",
    "next": ["human_review"],
    "run_id": 21,
    "suggestion_id": 31,
}


def review_result(reviewer_id: int) -> dict:
    """Ответ API на одобрение: предложение, сообщение, статус и итог графа."""
    return {
        "suggestion": {
            "id": 31,
            "agent_run_id": 21,
            "ticket_id": 13,
            "status": "approved",
            "reviewed_by": reviewer_id,
            "final_content": REVIEW_TEXT,
            "delivered_message_id": 51,
        },
        "message": {
            "id": 51,
            "ticket_id": 13,
            "user_id": reviewer_id,
            "author_type": "operator",
            "content": REVIEW_TEXT,
        },
        "ticket_status": "waiting_customer",
        "workflow": WAITING | {"state": "delivered", "next": []},
    }


def add_review_responses(
    responses: dict, result: dict | None = None, repeat_status: int = 409
) -> None:
    """Первое решение одобряет, повторное получает 409; затем обращение ждёт клиента."""
    state = {"reviewed": False}
    open_ticket = responses.get("/api/tickets/13", (200, b'{"id":13,"status":"open"}'))

    def ticket(body: dict) -> tuple[int, bytes]:
        """Показать статус обращения до и после доставки ответа."""
        if state["reviewed"]:
            return 200, b'{"id":13,"status":"waiting_customer"}'
        return open_ticket

    def review(body: dict) -> tuple[int, bytes]:
        """Применить решение один раз, как advisory lock и FOR UPDATE в API."""
        value = result if result is not None else review_result(body["reviewer_id"])
        if state["reviewed"] and repeat_status == 409:
            # Smoke проверяет только код 409; текст ошибки здесь не важен.
            return 409, b'{"detail":"conflict"}'
        state["reviewed"] = True
        return 200, json.dumps(value).encode()

    responses.update(
        {
            "/api/users": (201, b'{"id":41}'),
            "/api/agent-runs/21/workflow": (200, json.dumps(WAITING).encode()),
            "/api/suggestions/31/review": review,
            "/api/tickets/13": ticket,
        }
    )


def agent_run() -> dict:
    """Собрать сохранённый mock-запуск с четырьмя успешными инструментами."""
    reply = "Демонстрационный агент. Реальная модель не вызывалась."

    def call(name: str, arguments: dict, data: dict) -> dict:
        """Описать один вызов так, как его возвращает API."""
        return {
            "call_id": f"mock-{name}",
            "name": name,
            "arguments": arguments,
            "result": {"ok": True, "data": data},
        }

    return {
        "id": 21,
        "ticket_id": 13,
        "status": "completed",
        "provider": "mock",
        "model": "mock-agent-v1",
        "started_at": "2026-10-18T00:00:00Z",
        "finished_at": "2026-10-18T00:00:01Z",
        "error_message": None,
        "steps": 3,
        "tool_trace": [
            call("get_ticket", {"ticket_id": 13}, {"ticket": {"id": 13}}),
            call("get_customer", {"customer_id": 11}, {"customer": {"id": 11}}),
            call(
                "get_ticket_history",
                {"customer_id": 11, "limit": 5},
                {"tickets": [], "limit": 5},
            ),
            call("get_license", {"license_key": "***"}, {"valid": True}),
            call(
                "search_knowledge",
                {"query": "Учебная проверка", "limit": 3},
                {"documents": [hit("KB-SUPPORT-DATA", "general")]},
            ),
        ],
        "usage": None,
        "result": {
            "summary": "Обращение №13",
            "category": "license_question",
            "draft_reply": reply,
            "sources": ["KB-SUPPORT-DATA"],
        },
        "suggestion": {
            "id": 31,
            "content": reply,
            "status": "pending",
            "requires_human_review": True,
        },
        "workflow_thread_id": "thread-21",
    }


@contextmanager
def http_server(responses: dict[str, tuple[int, bytes]]):
    """Запустить изолированный HTTP-сервер и гарантированно закрыть его."""
    requests = Counter()

    class Handler(BaseHTTPRequestHandler):
        """Выдать ответы сценария и посчитать реальные HTTP-запросы."""

        def respond(self, payload: dict | None = None) -> None:
            """Выдать ответ сценария; функция-ответ получает JSON тела POST."""
            requests[self.path] += 1
            value = responses.get(self.path, (404, b"Not found"))
            status, body = value(payload or {}) if callable(value) else value
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            """Обработать GET так же, как внешний сервис для smoke CLI."""
            self.respond()

        def do_POST(self) -> None:
            """Принять тело POST и выдать отдельный ответ сценария без реальной БД."""
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.respond(json.loads(raw) if raw else None)

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
    check_agent: bool = False,
    check_knowledge: bool = False,
    check_review: bool = False,
    review_run: int | None = None,
) -> subprocess.CompletedProcess:
    """Вызвать CLI отдельным процессом с ограничением времени самого теста."""
    command = [sys.executable, str(SCRIPT), "--base-url", url, "--timeout", timeout]
    if check_database:
        command.append("--check-database")
    if check_api:
        command.append("--check-api")
    if check_llm:
        command.append("--check-llm")
    if check_agent:
        command.append("--check-agent")
    if check_knowledge:
        command.append("--check-knowledge")
    if check_review:
        command.append("--check-review")
    if review_run is not None:
        command.extend(["--review-run", str(review_run)])
    # Дочерний CLI печатает UTF-8 на любой ОС; Windows по умолчанию читал бы cp1252.
    return subprocess.run(
        command,
        capture_output=True,
        encoding="utf-8",
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


def test_cli_checks_saved_agent_run_and_unchanged_ticket() -> None:
    """--check-agent включает mock-черновик, затем проверяет POST и GET запуска."""
    responses = valid_responses(check_agent=True)
    with http_server(responses) as (url, requests):
        result = run_smoke(url, check_agent=True)
    assert result.returncode == 0, result.stderr
    # Обращение читается дважды: после черновика и после анализа агентом;
    # поиск — дважды: для продукта и для проверки фильтра другого продукта.
    expected = Counter({endpoint: 1 for endpoint in responses})
    expected["/api/tickets/13"] = 2
    expected["/api/knowledge/search"] = 2
    assert requests == expected
    assert "пять инструментов" in result.stdout
    assert "Smoke test пройден" in result.stdout


def test_cli_refuses_agent_mode_before_any_post_for_real_provider() -> None:
    """Агент не запускается и записи не создаются, если провайдер не mock."""
    responses = valid_responses(check_agent=True)
    responses["/api/llm/status"] = (
        200,
        b'{"provider":"openai","model":"gpt","configured":true}',
    )
    with http_server(responses) as (url, requests):
        result = run_smoke(url, check_agent=True)
    assert result.returncode == 1
    assert "POST не выполнялись" in result.stderr
    assert requests["/api/licenses"] == 0
    assert requests["/api/tickets/13/analyze"] == 0
    # Проверка режима LLM идёт раньше любого POST, включая поиск.
    assert requests["/api/knowledge/search"] == 0


@pytest.mark.parametrize("missing", ["analyze", "agent-runs"])
def test_cli_requires_agent_operations_before_writing_license(missing: str) -> None:
    """Без маршрутов агента в OpenAPI лицензия и запуск не создаются."""
    responses = valid_responses(check_agent=True)
    schema = json.loads(responses["/openapi.json"][1])
    schema["paths"] = {
        path: operations
        for path, operations in schema["paths"].items()
        if missing not in path
    }
    responses["/openapi.json"] = (200, json.dumps(schema).encode())
    with http_server(responses) as (url, requests):
        result = run_smoke(url, check_agent=True)
    assert result.returncode == 1
    assert "отсутствует операция" in result.stderr
    assert requests["/api/licenses"] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"status": "failed"},
        {"provider": "openai"},
        {"model": "unknown"},
        {"steps": 2},
        {"error_message": "Ошибка"},
        {"result": None},
        {"suggestion": None},
        {"suggestion": {"id": 31, "content": "x", "status": "approved"}},
        "drop_license_call",
        "drop_search_call",
        "unknown_source",
        "no_sources",
        "raw_license_key",
        "license_invalid",
        "tool_error",
        "missing_marker",
    ],
)
def test_cli_rejects_wrong_agent_run(change) -> None:
    """Отклонить провал, утечку ключа, ложную проверку и отсутствие pending."""
    run = agent_run()
    if isinstance(change, dict):
        run |= change
    elif change == "drop_license_call":
        run["tool_trace"].pop(3)
    elif change == "drop_search_call":
        run["tool_trace"].pop()
    elif change == "unknown_source":
        run["result"]["sources"] = ["KB-PASSWORD-RESET"]
    elif change == "no_sources":
        run["result"]["sources"] = []
    elif change == "raw_license_key":
        run["tool_trace"][3]["arguments"] = {"license_key": "SMOKE-LEAKED"}
    elif change == "license_invalid":
        run["tool_trace"][3]["result"]["data"]["valid"] = False
    elif change == "tool_error":
        run["tool_trace"][1]["result"] = {"ok": False, "error": {"code": "x"}}
    else:
        run["result"]["draft_reply"] = "Ответ без пометки"
        run["suggestion"]["content"] = "Ответ без пометки"
    responses = valid_responses(check_agent=True)
    responses["/api/tickets/13/analyze"] = (201, json.dumps(run).encode())
    with http_server(responses) as (url, requests):
        result = run_smoke(url, check_agent=True)
    assert result.returncode == 1
    assert "неверный контракт сохранённого запуска" in result.stderr
    assert requests["/api/agent-runs/21"] == 0


def test_cli_rejects_saved_run_that_differs_from_post() -> None:
    """GET обязан вернуть ровно тот запуск, который сохранил POST."""
    responses = valid_responses(check_agent=True)
    stored = agent_run() | {"steps": 4}
    responses["/api/agent-runs/21"] = (200, json.dumps(stored).encode())
    with http_server(responses) as (url, _):
        result = run_smoke(url, check_agent=True)
    assert result.returncode == 1
    assert "сохранённый запуск отличается" in result.stderr


def test_cli_checks_knowledge_index_and_product_filter() -> None:
    """--check-knowledge только читает: статус, поиск и изоляция продуктов."""
    responses = valid_responses(check_knowledge=True)
    with http_server(responses) as (url, requests):
        result = run_smoke(url, check_knowledge=True)
    assert result.returncode == 0, result.stderr
    assert requests["/api/knowledge/status"] == 1
    assert requests["/api/knowledge/search"] == 2
    assert requests["/api/customers"] == 0
    assert "KB-SERVER-START" in result.stdout


@pytest.mark.parametrize(
    "changes",
    [
        {"embedding_provider": "openai"},
        {"configured": False},
    ],
)
def test_cli_refuses_knowledge_search_without_mock_embeddings(changes: dict) -> None:
    """Поиск с платными или ненастроенными эмбеддингами не выполняется."""
    responses = valid_responses(check_knowledge=True)
    responses["/api/knowledge/status"] = (
        200,
        json.dumps(KNOWLEDGE_STATUS | changes).encode(),
    )
    with http_server(responses) as (url, requests):
        result = run_smoke(url, check_knowledge=True)
    assert result.returncode == 1
    assert "поиск не выполнялся" in result.stderr
    assert requests["/api/knowledge/search"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"index": "missing", "points": None},
        {"documents": 0},
        {"points": 3},
    ],
)
def test_cli_requires_ready_index(changes: dict) -> None:
    """Пустой или неполный индекс просит выполнить индексацию."""
    responses = valid_responses(check_knowledge=True)
    responses["/api/knowledge/status"] = (
        200,
        json.dumps(KNOWLEDGE_STATUS | changes).encode(),
    )
    with http_server(responses) as (url, requests):
        result = run_smoke(url, check_knowledge=True)
    assert result.returncode == 1
    assert "app.knowledge.ingest" in result.stderr
    assert requests["/api/knowledge/search"] == 0


def test_cli_rejects_wrong_top_article() -> None:
    """Известный запрос должен находить известную статью первой."""
    responses = valid_responses(check_knowledge=True)
    responses["/api/knowledge/search"] = lambda body: knowledge_search({})
    with http_server(responses) as (url, _):
        result = run_smoke(url, check_knowledge=True)
    assert result.returncode == 1
    assert "не найдена первой" in result.stderr


def test_cli_rejects_other_product_leak() -> None:
    """Статья сервера в выдаче для другого продукта блокирует выпуск."""
    responses = valid_responses(check_knowledge=True)
    responses["/api/knowledge/search"] = lambda body: knowledge_search(
        {"product_code": "techflow-server"}
    )
    with http_server(responses) as (url, _):
        result = run_smoke(url, check_knowledge=True)
    assert result.returncode == 1
    assert "другого продукта" in result.stderr


def test_cli_checks_review_delivery_and_conflict() -> None:
    """--check-review одобряет предложение, проверяет доставку и 409 на повтор."""
    responses = valid_responses(check_review=True)
    with http_server(responses) as (url, requests):
        result = run_smoke(url, check_review=True)
    assert result.returncode == 0, result.stderr
    assert requests["/api/suggestions/31/review"] == 2
    assert requests["/api/agent-runs/21/workflow"] == 1
    assert requests["/api/users"] == 1
    # Обращение: после черновика, после анализа и после доставки ответа.
    assert requests["/api/tickets/13"] == 3
    assert "повторное решение получает 409" in result.stdout


def test_cli_refuses_review_when_graph_is_not_waiting() -> None:
    """Если граф не ждёт решения, сотрудник и решение не создаются."""
    responses = valid_responses(check_review=True)
    responses["/api/agent-runs/21/workflow"] = (
        200,
        json.dumps(WAITING | {"state": "delivered", "next": []}).encode(),
    )
    with http_server(responses) as (url, requests):
        result = run_smoke(url, check_review=True)
    assert result.returncode == 1
    assert "не ожидает решения" in result.stderr
    assert requests["/api/users"] == 0
    assert requests["/api/suggestions/31/review"] == 0


@pytest.mark.parametrize(
    "path,value",
    [
        (("suggestion", "status"), "rejected"),
        (("suggestion", "final_content"), "Другой текст"),
        (("message", "author_type"), "agent"),
        (("message", "user_id"), 99),
        (("ticket_status",), "open"),
        (("workflow", "state"), "awaiting_review"),
    ],
)
def test_cli_rejects_wrong_review_result(path: tuple, value) -> None:
    """Неверное решение, автор, текст, статус или итог графа блокируют выпуск."""
    result_body = review_result(41)
    target = result_body
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    responses = valid_responses(check_review=True, review_body=result_body)
    with http_server(responses) as (url, _):
        result = run_smoke(url, check_review=True)
    assert result.returncode == 1
    assert "неверный итог одобрения" in result.stderr


def test_cli_requires_conflict_for_second_decision() -> None:
    """Если повторное решение тоже принято, двойная доставка не обнаружится."""
    responses = valid_responses(check_review=True, repeat_status=200)
    with http_server(responses) as (url, _):
        result = run_smoke(url, check_review=True)
    assert result.returncode == 1
    assert "повторное решение не отклонено" in result.stderr


def test_cli_review_run_continues_existing_workflow() -> None:
    """--review-run рассматривает уже сохранённый запуск без нового анализа."""
    responses = valid_responses(check_api=True)
    responses["/api/agent-runs/21"] = (200, json.dumps(agent_run()).encode())
    schema = json.loads(responses["/openapi.json"][1])
    schema["paths"].update(REVIEW_PATHS)
    responses["/openapi.json"] = (200, json.dumps(schema).encode())
    add_review_responses(responses)
    with http_server(responses) as (url, requests):
        result = run_smoke(url, review_run=21)
    assert result.returncode == 0, result.stderr
    assert requests["/api/tickets/13/analyze"] == 0
    assert requests["/api/suggestions/31/review"] == 2


@pytest.mark.parametrize("value", ["0", "-3", "abc"])
def test_cli_rejects_invalid_review_run_id(value: str) -> None:
    """Неверный id запуска — ошибка аргументов с кодом 2."""
    command = [sys.executable, str(SCRIPT), "--review-run", value]
    result = subprocess.run(command, capture_output=True, encoding="utf-8", timeout=5)
    assert result.returncode == 2
