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


def valid_responses() -> dict[str, tuple[int, bytes]]:
    """Собрать минимальные корректные HTTP-ответы публичного контракта API."""
    return {
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
                    "paths": {"/health": {"get": {}}},
                }
            ).encode(),
        ),
    }


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


def run_smoke(url: str, timeout: str = "2") -> subprocess.CompletedProcess:
    """Вызвать CLI отдельным процессом с ограничением времени самого теста."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--base-url", url, "--timeout", timeout],
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
