"""Проверить HTTP-клиент рабочего стола без настоящих сетевых запросов."""

import json

import httpx
import pytest

from operator_ui.client import (
    ANALYZE_CONFLICT,
    REVIEW_CONFLICT,
    APIClient,
    APIError,
    configured_api_url,
    positive_id,
)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1", 2**63])
def test_reject_invalid_ids(value):
    """Строка, bool и выход за BIGINT не могут стать частью адреса запроса."""
    with pytest.raises(APIError):
        positive_id(value)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:secret@example.org",
        "http://host/api",
        "http://host?key=secret",
        "http://host#fragment",
        "http://host:bad",
        "http://[broken",
    ],
)
def test_invalid_environment_url_is_safe(monkeypatch, url):
    """Неверная серверная настройка получает понятную ошибку без утечки адреса."""
    monkeypatch.setenv("API_URL", url)
    with pytest.raises(APIError) as raised:
        configured_api_url()
    assert url not in str(raised.value)


def test_environment_and_default_url(monkeypatch):
    """Локальная конфигурация работает без env, Docker может задать внутренний DNS."""
    monkeypatch.delenv("API_URL", raising=False)
    assert configured_api_url() == "http://127.0.0.1:8001"
    monkeypatch.setenv("API_URL", "http://api:8000/")
    assert configured_api_url() == "http://api:8000"


def test_requests_are_bounded_and_explicit():
    """Проверить маршруты, фильтры, тела и фиксированную страницу в реальном HTTPX."""
    captured = []

    def handler(request):
        """Запомнить HTTP-запрос и выдать подходящую форму JSON."""
        captured.append(request)
        listed = request.url.path in {"/api/tickets", "/api/tickets/4/messages"}
        return httpx.Response(200, json=[] if listed else {"id": 1})

    with APIClient("http://api", transport=httpx.MockTransport(handler)) as client:
        client.list_records("tickets", offset=20, customer_id=2, status=None, limit=999)
        client.get_record("tickets", 4)
        client.analyze(4, "hint")
        client.review(8, {"decision": "reject", "reviewer_id": 1, "comment": "Причина"})
        client.messages(4, offset=20)
        client.search("ошибка запуска", "server", 3)
        client.update_ticket_status(4, "in_progress")
        client.dashboard()
        client.status("llm")
    assert len(captured) == 9
    assert dict(captured[0].url.params) == {
        "offset": "20",
        "limit": "20",
        "customer_id": "2",
    }
    assert json.loads(captured[2].content) == {"license_key": "hint"}
    assert captured[6].method == "PATCH"
    assert json.loads(captured[6].content) == {"status": "in_progress"}
    assert captured[7].url.path == "/api/dashboard"
    assert captured[4].url.params["limit"] == "20"


@pytest.mark.parametrize(
    "operation",
    [
        lambda client: client.list_records("../../private"),
        lambda client: client.get_record("private", 1),
        lambda client: client.create_record("agent-runs", {}),
        lambda client: client.status("private"),
        lambda client: client.messages(1, offset=-1),
        lambda client: client.list_records("tickets", offset=True),
    ],
)
def test_no_network_for_invalid_local_arguments(operation):
    """Маршруты ограничены явным списком ещё до сетевого запроса."""

    def handler(request):
        """Падать, если локально отклонённый запрос всё же ушёл в сеть."""
        raise AssertionError("Запрос не должен исполняться")

    with APIClient("http://api", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(APIError):
            operation(client)


@pytest.mark.parametrize("method", ["analyze", "update_ticket_status"])
def test_timeout_never_retries_mutation(method):
    """После потери ответа клиент не повторяет действие, которое могло сохраниться."""
    calls = []

    def handler(request):
        """Сымитировать потерянный ответ сервера после приёма запроса."""
        calls.append(request)
        raise httpx.ReadTimeout("secret.internal:5432?key=private")

    with APIClient("http://api", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(APIError, match="могло сохраниться") as raised:
            getattr(client, method)(1, None if method == "analyze" else "closed")
    assert len(calls) == 1
    assert "secret" not in str(raised.value)


@pytest.mark.parametrize("code", [302, 404, 409, 422, 500, 502, 503, 504])
def test_error_hides_response_body_and_retains_run_id(code):
    """Не выводить HTML и traceback; сохранённый run_id остаётся доступен оператору."""
    calls = []

    def handler(request):
        """Вернуть небезопасное тело, которое нельзя переносить в страницу."""
        calls.append(request)
        return httpx.Response(
            code,
            json={"detail": "secret <script>db-password</script>", "run_id": 7},
            headers={"Location": "http://other/private"},
        )

    with APIClient("http://api", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(APIError) as raised:
            client.analyze(1, None)
    assert len(calls) == 1
    assert raised.value.status_code == code
    assert raised.value.run_id == 7
    assert "secret" not in str(raised.value)
    assert "script" not in str(raised.value)


@pytest.mark.parametrize(
    "body", [b"<html>private traceback</html>", b"null", b"42", b"[]"]
)
def test_unexpected_success_body_is_safe(body):
    """Ответ успешного статуса тоже обязан иметь ожидаемую JSON-форму."""
    with APIClient(
        "http://api",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=body)
        ),
    ) as client:
        with pytest.raises(APIError, match="неожиданном формате"):
            client.status("llm")


def test_network_error_does_not_expose_url():
    """Ошибка соединения не показывает технический адрес или данные окружения."""

    def handler(request):
        """Отклонить соединение с внутренним сервисом."""
        raise httpx.ConnectError("http://private-host:8000?secret=key")

    with APIClient("http://api", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(APIError, match="Нет соединения") as raised:
            client.status("llm")
    assert "private-host" not in str(raised.value)


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (lambda client: client.analyze(1, None), ANALYZE_CONFLICT),
        (
            lambda client: client.review(1, {"decision": "reject", "reviewer_id": 1}),
            REVIEW_CONFLICT,
        ),
        (lambda client: client.create_record("customers", {}), "Запись уже существует"),
    ],
)
def test_conflict_hint_depends_on_the_action(operation, expected):
    """409 анализа и решения объясняет своё состояние, а не только дубликат."""

    def handler(request):
        """Вернуть конфликт с текстом, который в интерфейс не переносится."""
        return httpx.Response(409, json={"detail": "internal detail"})

    with APIClient("http://api", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(APIError) as raised:
            operation(client)
    assert raised.value.status_code == 409
    assert str(raised.value).startswith(expected)
    assert "internal" not in str(raised.value)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("0123456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef"),
        ("<script>x</script>", None),
    ],
)
def test_error_keeps_only_safe_request_id(header, expected):
    """Код запроса из заголовка API показывается, только если он безопасен."""

    def handler(request):
        """Вернуть ошибку сервера с id запроса в заголовке."""
        return httpx.Response(503, json={}, headers={"X-Request-ID": header})

    with APIClient("http://api", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(APIError) as raised:
            client.dashboard()
    assert raised.value.request_id == expected
