"""Проверить повторяемость демонстрации и запрет платных вызовов до первого POST."""

import argparse
import io
import json
import time
from copy import deepcopy
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts import demo_scenario as demo


class FakeClient(demo.DemoClient):
    """Смоделировать HTTP-контракт API и подсчитать каждое изменение данных."""

    def __init__(self):
        """Создать отдельную память сценария без PostgreSQL, Qdrant и сети."""
        super().__init__("http://127.0.0.1:8001", time.monotonic() + 60)
        self.calls = []
        self.tables = {
            name: []
            for name in ("customers", "products", "users", "licenses", "tickets")
        }
        self.runs = []
        self.suggestions = {}
        self.workflows = {}
        self.messages = []
        self.llm_provider = "mock"
        self.embedding_provider = "mock"
        self.index = "ready"
        self.next_id = 1

    def new_id(self) -> int:
        """Получить отдельный положительный идентификатор очередной записи."""
        value = self.next_id
        self.next_id += 1
        return value

    def request(self, path: str, *, payload=None, expected_status=200):
        """Ответить на маршруты сценария и сохранить только явные POST."""
        self.calls.append((path, deepcopy(payload)))
        split = urlsplit(path)
        endpoint = split.path
        query = {key: values[0] for key, values in parse_qs(split.query).items()}
        if endpoint == "/ready":
            return {"status": "ok", "database": "ok"}
        if endpoint == "/api/llm/status":
            return {"provider": self.llm_provider, "configured": True}
        if endpoint == "/api/knowledge/status":
            return {
                "embedding_provider": self.embedding_provider,
                "configured": True,
                "index": self.index,
                "documents": 7,
            }
        parts = endpoint.strip("/").split("/")
        resource = parts[1]
        if resource in self.tables and len(parts) == 2:
            if payload is not None:
                record = {"id": self.new_id(), **payload}
                if resource == "users":
                    record["is_active"] = True
                if resource == "tickets":
                    record["status"] = "open"
                self.tables[resource].append(record)
                return deepcopy(record)
            return self.page(self.tables[resource], query)
        if resource == "agent-runs" and len(parts) == 2:
            return self.page(self.runs, query)
        if resource == "tickets" and len(parts) == 3:
            return deepcopy(self.ticket(int(parts[2])))
        if resource == "tickets" and len(parts) == 4:
            ticket_id = int(parts[2])
            if parts[3] == "analyze":
                assert payload == {"license_key": demo.LICENSE_KEY}
                return self.analyze(ticket_id)
            return self.page(self.messages, query | {"ticket_id": str(ticket_id)})
        if resource == "agent-runs" and parts[3] == "workflow":
            return deepcopy(self.workflows[int(parts[2])])
        if resource == "suggestions":
            suggestion_id = int(parts[2])
            if payload is not None:
                return self.review(suggestion_id, payload)
            return deepcopy(self.suggestions[suggestion_id])
        raise AssertionError(f"Неожиданный маршрут: {endpoint}")

    def page(self, records: list[dict], query: dict) -> list[dict]:
        """Применить точные фильтры и постраничную выдачу API."""
        filtered = [
            record
            for record in records
            if all(
                str(record.get(key)) == value
                for key, value in query.items()
                if key not in {"offset", "limit"}
            )
        ]
        offset = int(query.get("offset", 0))
        limit = int(query.get("limit", 20))
        return deepcopy(filtered[offset : offset + limit])

    def ticket(self, ticket_id: int) -> dict:
        """Найти сохранённый тикет, чтобы решение изменяло состояние сервера."""
        return next(
            record for record in self.tables["tickets"] if record["id"] == ticket_id
        )

    def analyze(self, ticket_id: int) -> dict:
        """Создать завершённый mock-запуск и ровно одно ожидающее предложение."""
        run_id, suggestion_id = self.new_id(), self.new_id()
        suggestion = {
            "id": suggestion_id,
            "status": "pending",
            "reviewed_by": None,
            "content": "Демонстрационный черновик",
            "ticket_id": ticket_id,
            "delivered_message_id": None,
        }
        trace = [
            {"name": name, "arguments": {}, "result": {"ok": True, "data": {}}}
            for name in (
                "get_ticket",
                "get_customer",
                "get_ticket_history",
                "get_license",
                "search_knowledge",
            )
        ]
        trace[3]["arguments"] = {"license_key": "***"}
        trace[3]["result"]["data"] = {"valid": True}
        run = {
            "id": run_id,
            "ticket_id": ticket_id,
            "provider": "mock",
            "status": "completed",
            "suggestion": suggestion,
            "tool_trace": trace,
        }
        self.runs.append(run)
        self.suggestions[suggestion_id] = suggestion
        self.workflows[run_id] = {
            "run_id": run_id,
            "suggestion_id": suggestion_id,
            "state": "awaiting_review",
        }
        return deepcopy(run)

    def review(self, suggestion_id: int, payload: dict) -> dict:
        """Доставить учебное сообщение при approve и ничего не создать при reject."""
        suggestion = self.suggestions[suggestion_id]
        assert suggestion["status"] == "pending", "Повтор решения запрещён"
        decision = payload["decision"]
        suggestion["status"] = "approved" if decision == "approve" else "rejected"
        suggestion["reviewed_by"] = payload["reviewer_id"]
        run = next(run for run in self.runs if run["suggestion"]["id"] == suggestion_id)
        state = "delivered" if decision == "approve" else "rejected"
        self.workflows[run["id"]]["state"] = state
        if decision == "approve":
            message = {
                "id": self.new_id(),
                "ticket_id": suggestion["ticket_id"],
                "author_type": "operator",
                "user_id": payload["reviewer_id"],
                "content": payload["final_content"],
            }
            self.messages.append(message)
            suggestion["delivered_message_id"] = message["id"]
            self.ticket(suggestion["ticket_id"])["status"] = "waiting_customer"
        return {"suggestion": deepcopy(suggestion)}


def writes(client: FakeClient) -> list:
    """Выбрать только запросы, которые действительно изменяют учебные данные."""
    return [call for call in client.calls if call[1] is not None]


def test_default_demo_is_idempotent_and_leaves_pending():
    """Два запуска сохраняют те же id и не создают второй тикет, запуск или письмо."""
    client = FakeClient()
    first = demo.run_demo(client)
    count = len(writes(client))
    second = demo.run_demo(client)
    assert first == second
    assert count == len(writes(client)) == 6
    assert first["suggestion_status"] == "pending"
    assert first["workflow_state"] == "awaiting_review"
    assert len(client.tables["tickets"]) == len(client.runs) == 1
    assert client.messages == []
    assert demo.LICENSE_KEY not in json.dumps(first)


def test_review_paths_are_separate_and_idempotent():
    """Approve и reject используют разные тикеты; исходный черновик ждёт человека."""
    client = FakeClient()
    first = demo.run_demo(client, check_review=True)
    count = len(writes(client))
    second = demo.run_demo(client, check_review=True)
    assert first == second
    assert len(writes(client)) == count == 12
    assert first["suggestion_status"] == "pending"
    assert first["review_checks"]["approve"]["messages"] == 1
    assert first["review_checks"]["reject"]["messages"] == 0
    assert len(client.messages) == 1
    assert len(client.tables["tickets"]) == len(client.runs) == 3


@pytest.mark.parametrize(
    "setting,value",
    [
        ("llm_provider", "openai"),
        ("embedding_provider", "openai"),
        ("index", "missing"),
        ("index", "incompatible"),
    ],
)
def test_unready_or_real_provider_prevents_all_posts(setting, value):
    """Платный провайдер и отсутствующий индекс отсекаются до создания клиента."""
    client = FakeClient()
    setattr(client, setting, value)
    with pytest.raises(demo.DemoError):
        demo.run_demo(client, check_review=True)
    assert not writes(client)


def test_failed_analysis_is_not_restarted():
    """Сохранённый failed не маскируется автоматическим повтором анализа."""
    client = FakeClient()
    demo.run_demo(client)
    client.runs[0]["status"] = "failed"
    count = len(writes(client))
    with pytest.raises(demo.DemoError, match="не завершён"):
        demo.run_demo(client)
    assert len(writes(client)) == count


def test_changed_ticket_is_not_overwritten():
    """Изменённое человеком содержание не заменяется учебным текстом."""
    client = FakeClient()
    demo.run_demo(client)
    client.tables["tickets"][0]["description"] = "Исправленный текст"
    count = len(writes(client))
    with pytest.raises(demo.DemoError, match="изменено"):
        demo.run_demo(client)
    assert len(writes(client)) == count


def test_records_find_fixture_on_second_page_without_post():
    """Демо ищет данные за первой сотней записей, прежде чем создавать новые."""
    client = FakeClient()
    client.tables["customers"] = [
        {"id": index, "email": f"other-{index}@example.com"} for index in range(1, 101)
    ] + [{"id": 101, "email": demo.CUSTOMER_EMAIL}]
    record = client.ensure("customers", {"email": demo.CUSTOMER_EMAIL}, {})
    assert record["id"] == 101
    assert not writes(client)
    assert any("offset=100" in call[0] for call in client.calls)


def test_unrelated_data_is_preserved():
    """Учебный seed не удаляет и не изменяет другую запись клиента."""
    client = FakeClient()
    other = {"id": 500, "email": "real@example.com", "name": "Существующий клиент"}
    client.tables["customers"].append(deepcopy(other))
    demo.run_demo(client, check_review=True)
    assert client.tables["customers"][0] == other


@pytest.mark.parametrize(
    "url",
    [
        "ftp://localhost",
        "http://user:secret@localhost",
        "http://localhost/path",
        "http://localhost?key=secret",
        "http://localhost#fragment",
        "http://localhost:99999",
        "http:///",
        "http://local host",
    ],
)
def test_url_rejects_credentials_and_non_root_addresses(url):
    """Сценарий не принимает секреты и произвольные пути в адресе API."""
    with pytest.raises(argparse.ArgumentTypeError):
        demo.base_url(url)


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-1", "601", "one"])
def test_timeout_has_finite_bounds(value):
    """Неконечный или слишком большой тайм-аут не отключает ограничения CLI."""
    with pytest.raises(argparse.ArgumentTypeError):
        demo.timeout_seconds(value)


def test_post_network_error_is_not_retried(monkeypatch):
    """Потеря ответа POST не вызывает второй запрос и не раскрывает значение ключа."""
    calls = []

    class BrokenOpener:
        """Оборвать сеть после единственного запроса, не имитируя успех."""

        def open(self, request, timeout):
            """Посчитать попытку и вернуть внутреннюю ошибку с секретом."""
            calls.append(request)
            raise URLError(f"internal {demo.LICENSE_KEY}")

    monkeypatch.setattr(demo, "build_opener", lambda *args: BrokenOpener())
    client = demo.DemoClient("http://localhost", time.monotonic() + 60)
    with pytest.raises(demo.DemoError) as error:
        client.object("/api/licenses", payload={"license_key": demo.LICENSE_KEY})
    assert len(calls) == 1
    assert demo.LICENSE_KEY not in str(error.value)


def test_http_error_does_not_expose_response_or_credentials(monkeypatch):
    """Диагностика ограничена HTTP-кодом; тело сервера и URL не печатаются."""

    class BrokenOpener:
        """Вернуть ошибку с чувствительным телом для проверки фильтрации."""

        def open(self, request, timeout):
            """Не передать тело ошибки пользователю через исключение DemoError."""
            raise HTTPError(
                "http://secret-host",
                409,
                "secret",
                {},
                io.BytesIO(demo.LICENSE_KEY.encode()),
            )

    monkeypatch.setattr(demo, "build_opener", lambda *args: BrokenOpener())
    client = demo.DemoClient("http://localhost", time.monotonic() + 60)
    with pytest.raises(demo.DemoError) as error:
        client.object("/api/licenses", payload={"license_key": demo.LICENSE_KEY})
    assert "409" in str(error.value)
    assert "secret" not in str(error.value)
    assert demo.LICENSE_KEY not in str(error.value)


def test_cli_failure_is_safe_and_returns_nonzero(monkeypatch, capsys):
    """При ошибке CLI возвращает 1 и печатает только понятную строку stderr."""

    def fail(*args, **kwargs):
        """Имитировать заранее проверенную ошибку сценария."""
        raise demo.DemoError("Индекс не готов")

    monkeypatch.setattr(demo, "run_demo", fail)
    assert demo.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Демо: Индекс не готов\n"
