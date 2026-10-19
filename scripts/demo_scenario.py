"""Подготовить повторяемый учебный сценарий через API, используя только mock.

По умолчанию оставляет один черновик на проверку человеку. Опциональный
--verify-review проверяет одобрение и отклонение на двух отдельных обращениях.
Повторный последовательный запуск переиспользует записи; POST не повторяется
автоматически даже при сетевой ошибке. Скрипт не отправляет внешние письма.

Запросы идут от имени сотрудника, чей токен лежит в переменной окружения
API_TOKEN: для подготовки и анализа достаточно роли operator, для
--verify-review нужна роль reviewer или admin. Токен никогда не печатается.
"""

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

PAGE_SIZE = 100
MAX_PAGES = 100
MAX_RESPONSE_BYTES = 1024 * 1024
CUSTOMER_EMAIL = "stage10-customer@example.com"
TOKEN_ENV = "API_TOKEN"
REVIEW_ROLES = {"reviewer", "admin"}
STAFF_ROLES = {"operator"} | REVIEW_ROLES
PRODUCT_CODE = "techflow-server"
# Это вымышленный учебный ключ. Он не включается в stdout, stderr и URL.
LICENSE_KEY = "DEMO-STAGE10-TECHFLOW-LOCAL-ONLY"
SUBJECT = "[Демо 10] Сервер не запускается после обновления"
DESCRIPTION = (
    "Вымышленное учебное обращение: TechFlow Server 5.x не запускается после "
    "обновления. Нужна проверка лицензии и инструкции по диагностике. "
    "Рабочие данные клиента отсутствуют."
)
APPROVED_TEXT = (
    "Учебный ответ проверен оператором. Проверьте журналы запуска сервера "
    "и наличие резервной копии; передайте службе поддержки текст ошибки."
)


class DemoError(RuntimeError):
    """Остановить сценарий с понятной причиной без вывода тела ответа и ключей."""


class NoRedirects(HTTPRedirectHandler):
    """Не передавать данные демонстрации другому адресу через HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Отклонить автоматический переход по ответу 3xx."""
        return None


def base_url(value: str) -> str:
    """Принять корневой HTTP(S) URL без credentials, пути и параметров."""
    try:
        parts = urlsplit(value)
        valid = parts.hostname is not None and (parts.port is None or parts.port > 0)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Некорректный URL API") from error
    if (
        not valid
        or parts.scheme not in {"http", "https"}
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in value)
    ):
        raise argparse.ArgumentTypeError(
            "Нужен корневой http(s) URL без credentials, пути и параметров"
        )
    return value.rstrip("/")


def timeout_seconds(value: str) -> float:
    """Ограничить общий тайм-аут конечным числом от 1 до 600 секунд."""
    try:
        timeout = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Тайм-аут должен быть числом") from error
    if not math.isfinite(timeout) or not 1 <= timeout <= 600:
        raise argparse.ArgumentTypeError("Тайм-аут должен быть от 1 до 600 секунд")
    return timeout


def record_id(record: dict) -> int:
    """Потребовать настоящий положительный целый id без bool и строк."""
    value = record.get("id")
    if type(value) is not int or value <= 0:
        raise DemoError("API вернул запись без положительного целого id")
    return value


@dataclass
class DemoClient:
    """Выполнять ограниченные HTTP-запросы без повторов и секретов в логе."""

    url: str
    deadline: float
    token: str

    def request(
        self,
        path: str,
        *,
        payload: dict | None = None,
        expected_status: int = 200,
    ) -> dict | list:
        """Прочитать JSON до общего дедлайна; тело ошибки никогда не печатать."""
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise DemoError("Истёк общий тайм-аут сценария")
        headers = {
            "User-Agent": "ai-support-agent-demo-stage10",
            "Authorization": f"Bearer {self.token}",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            self.url + path,
            data=data,
            headers=headers,
            method="POST" if payload is not None else "GET",
        )
        try:
            with build_opener(NoRedirects()).open(
                request, timeout=min(remaining, 30.0)
            ) as response:
                if response.status != expected_status:
                    raise DemoError("API вернул неожиданный HTTP-статус")
                chunks = []
                size = 0
                while True:
                    if time.monotonic() >= self.deadline:
                        raise DemoError("Истёк общий тайм-аут сценария")
                    chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise DemoError("Ответ API превышает размер 1 МиБ")
                    chunks.append(chunk)
                body = b"".join(chunks)
        except HTTPError as error:
            # Не включаем URL, тело HTTPError или JSON запроса в диагностику.
            hints = {
                401: f"API не принял токен: проверь {TOKEN_ENV} или получи новый",
                403: "роль сотрудника токена не допускает это действие",
                429: "превышен лимит изменений в минуту; повтори позже",
            }
            hint = hints.get(
                error.code, "проверь API и повтори сценарий после исправления"
            )
            raise DemoError(f"HTTP {error.code}: {hint}") from error
        except (URLError, OSError) as error:
            raise DemoError(
                "API недоступно; POST не повторялся. Проверь стенд и запусти заново"
            ) from error
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DemoError("API вернул некорректный JSON") from error
        if not isinstance(value, (dict, list)):
            raise DemoError("API вернул JSON неожиданного типа")
        return value

    def object(self, path: str, **kwargs) -> dict:
        """Проверить, что ответ операции является JSON-объектом."""
        result = self.request(path, **kwargs)
        if not isinstance(result, dict):
            raise DemoError("Ожидался JSON-объект API")
        return result

    def records(self, path: str, **filters) -> list[dict]:
        """Прочитать не более 10 000 записей постранично, не теряя вторую страницу."""
        records = []
        for page in range(MAX_PAGES):
            query = urlencode(
                filters | {"offset": page * PAGE_SIZE, "limit": PAGE_SIZE}
            )
            batch = self.request(f"{path}?{query}")
            if not isinstance(batch, list) or len(batch) > PAGE_SIZE:
                raise DemoError("API вернул некорректную страницу списка")
            for record in batch:
                if not isinstance(record, dict):
                    raise DemoError("Список API содержит значение вместо записи")
                record_id(record)
            records.extend(batch)
            if len(batch) < PAGE_SIZE:
                return records
        raise DemoError("Демо ограничено 10 000 записями списка; уточни учебный стенд")

    def ensure(self, resource: str, identity: dict, payload: dict, **filters) -> dict:
        """Найти стабильную запись или создать её ровно одним POST без перезаписи."""
        matches = [
            record
            for record in self.records(f"/api/{resource}", **filters)
            if all(record.get(key) == value for key, value in identity.items())
        ]
        if len(matches) > 1:
            raise DemoError("Найдено несколько демозаписей; требуется ручная проверка")
        if matches:
            return matches[0]
        created = self.object(f"/api/{resource}", payload=payload, expected_status=201)
        record_id(created)
        if not all(created.get(key) == value for key, value in identity.items()):
            raise DemoError("Созданная запись API не совпадает с учебным сценарием")
        return created


def require_mock(client: DemoClient, *, check_review: bool = False) -> dict:
    """До первого POST проверить БД, сотрудника токена, mock LLM и индекс знаний.

    Возвращает сотрудника из /api/auth/me: он станет автором решений.
    """
    ready = client.object("/ready")
    if ready != {"status": "ok", "database": "ok"}:
        raise DemoError("База данных не готова или требуется миграция")
    staff = client.object("/api/auth/me")
    if staff.get("role") not in STAFF_ROLES:
        raise DemoError("API вернул неизвестную роль сотрудника токена")
    record_id(staff)
    if check_review and staff["role"] not in REVIEW_ROLES:
        raise DemoError("Для --verify-review нужен токен роли reviewer или admin")
    llm = client.object("/api/llm/status")
    knowledge = client.object("/api/knowledge/status")
    if llm.get("provider") != "mock" or llm.get("configured") is not True:
        raise DemoError("Демонстрация разрешена только при LLM_PROVIDER=mock")
    if (
        knowledge.get("embedding_provider") != "mock"
        or knowledge.get("configured") is not True
        or knowledge.get("index") != "ready"
        or type(knowledge.get("documents")) is not int
        or knowledge["documents"] < 1
    ):
        raise DemoError("Нужен готовый индекс знаний с EMBEDDING_PROVIDER=mock")
    return staff


def ensure_fixtures(client: DemoClient, staff: dict) -> dict:
    """Подготовить отдельные вымышленные записи, не изменяя найденные данные.

    Отдельного учебного сотрудника скрипт не создаёт: решения принимает
    сотрудник токена, а создавать сотрудников может только администратор.
    """
    customer = client.ensure(
        "customers",
        {"email": CUSTOMER_EMAIL},
        {
            "name": "Учебный клиент этапа 10",
            "email": CUSTOMER_EMAIL,
            "company": "Вымышленная компания Demo",
        },
    )
    product = client.ensure(
        "products",
        {"code": PRODUCT_CODE},
        {
            "name": "TechFlow Server",
            "code": PRODUCT_CODE,
            "description": "Вымышленный продукт для учебной демонстрации",
        },
    )
    license_record = client.ensure(
        "licenses",
        {"license_key": LICENSE_KEY},
        {
            "customer_id": customer["id"],
            "product_id": product["id"],
            "license_key": LICENSE_KEY,
            "status": "active",
            "issued_at": "2020-01-01T00:00:00Z",
            "expires_at": None,
        },
        customer_id=customer["id"],
        product_id=product["id"],
    )
    if (
        license_record.get("customer_id") != customer["id"]
        or license_record.get("product_id") != product["id"]
        or license_record.get("status") != "active"
        or license_record.get("expires_at") is not None
    ):
        raise DemoError("Учебная лицензия изменена; существующие данные сохранены")
    return {
        "customer_id": customer["id"],
        "product_id": product["id"],
        "staff_id": staff["id"],
        "license_id": license_record["id"],
    }


def ensure_ticket(client: DemoClient, fixtures: dict, subject: str) -> dict:
    """Найти обращение по отдельному клиенту, продукту и точной учебной теме."""
    ticket = client.ensure(
        "tickets",
        {"subject": subject},
        {
            "customer_id": fixtures["customer_id"],
            "product_id": fixtures["product_id"],
            "subject": subject,
            "description": DESCRIPTION,
            "priority": "medium",
        },
        customer_id=fixtures["customer_id"],
        product_id=fixtures["product_id"],
    )
    if (
        ticket.get("customer_id") != fixtures["customer_id"]
        or ticket.get("product_id") != fixtures["product_id"]
        or ticket.get("description") != DESCRIPTION
    ):
        raise DemoError("Учебное обращение изменено; автоматическая замена запрещена")
    return ticket


def ensure_analysis(client: DemoClient, ticket: dict) -> tuple[dict, dict]:
    """Переиспользовать запуск; после failed/running не создавать скрытый повтор."""
    runs = client.records("/api/agent-runs", ticket_id=ticket["id"])
    if len(runs) > 1:
        raise DemoError("У учебного обращения несколько запусков: проверь его вручную")
    run = (
        runs[0]
        if runs
        else client.object(
            f"/api/tickets/{ticket['id']}/analyze",
            payload={"license_key": LICENSE_KEY},
            expected_status=201,
        )
    )
    record_id(run)
    suggestion = run.get("suggestion")
    if (
        run.get("ticket_id") != ticket["id"]
        or run.get("provider") != "mock"
        or run.get("status") != "completed"
        or not isinstance(suggestion, dict)
        or suggestion.get("status") not in {"pending", "approved", "rejected"}
    ):
        raise DemoError("Анализ не завершён; новый запуск автоматически не создан")
    record_id(suggestion)
    trace = run.get("tool_trace")
    expected_tools = {
        "get_ticket",
        "get_customer",
        "get_ticket_history",
        "get_license",
        "search_knowledge",
    }
    if (
        not isinstance(trace, list)
        or len(trace) != len(expected_tools)
        or any(not isinstance(item, dict) for item in trace)
        or {item.get("name") for item in trace} != expected_tools
        or any(item.get("result", {}).get("ok") is not True for item in trace)
    ):
        raise DemoError(
            "Демонстрация не получила пять успешных результатов инструментов"
        )
    license_call = next(item for item in trace if item["name"] == "get_license")
    if (
        license_call.get("arguments") != {"license_key": "***"}
        or license_call["result"].get("data", {}).get("valid") is not True
    ):
        raise DemoError(
            "Лицензия демонстрации не подтверждена или ключ не скрыт в трассе"
        )
    workflow = client.object(f"/api/agent-runs/{run['id']}/workflow")
    if (
        workflow.get("run_id") != run["id"]
        or workflow.get("suggestion_id") != suggestion["id"]
    ):
        raise DemoError("Состояние процесса не соответствует учебному запуску")
    return run, workflow


def verify_review(client: DemoClient, fixtures: dict, decision: str) -> dict:
    """Проверить одно решение на отдельном обращении без повторной доставки."""
    ticket = ensure_ticket(client, fixtures, f"{SUBJECT} — {decision}")
    run, _ = ensure_analysis(client, ticket)
    suggestion_id = run["suggestion"]["id"]
    suggestion = client.object(f"/api/suggestions/{suggestion_id}")
    expected_status = "approved" if decision == "approve" else "rejected"
    if suggestion.get("status") == "pending":
        # Автора решения API берёт из токена: в теле его нет.
        payload = {"decision": decision}
        if decision == "approve":
            payload["final_content"] = APPROVED_TEXT
            payload["comment"] = "Явная учебная проверка --verify-review"
        else:
            payload["comment"] = "Учебное отклонение: оператор запросил уточнение"
        client.object(f"/api/suggestions/{suggestion_id}/review", payload=payload)
        suggestion = client.object(f"/api/suggestions/{suggestion_id}")
    if (
        suggestion.get("status") != expected_status
        or suggestion.get("reviewed_by") != fixtures["staff_id"]
    ):
        raise DemoError("Предложение рассмотрено с другим решением или сотрудником")
    messages = client.records(f"/api/tickets/{ticket['id']}/messages")
    current_ticket = client.object(f"/api/tickets/{ticket['id']}")
    workflow = client.object(f"/api/agent-runs/{run['id']}/workflow")
    if decision == "approve":
        if (
            len(messages) != 1
            or messages[0].get("id") != suggestion.get("delivered_message_id")
            or messages[0].get("author_type") != "operator"
            or messages[0].get("user_id") != fixtures["staff_id"]
            or messages[0].get("content") != APPROVED_TEXT
            or current_ticket.get("status") != "waiting_customer"
            or workflow.get("state") != "delivered"
        ):
            raise DemoError("Доставка не соответствует ожидаемому одному сообщению")
    elif (
        messages
        or current_ticket.get("status") != "open"
        or workflow.get("state") != "rejected"
    ):
        raise DemoError("Отклонение изменило обращение или создало сообщение")
    return {
        "ticket_id": ticket["id"],
        "run_id": run["id"],
        "suggestion_id": suggestion_id,
        "status": expected_status,
        "messages": len(messages),
        "workflow_state": workflow["state"],
    }


def run_demo(client: DemoClient, *, check_review: bool = False) -> dict:
    """Оставить основной черновик человеку и опционально проверить два других пути."""
    staff = require_mock(client, check_review=check_review)
    fixtures = ensure_fixtures(client, staff)
    ticket = ensure_ticket(client, fixtures, SUBJECT)
    run, workflow = ensure_analysis(client, ticket)
    result = fixtures | {
        "ticket_id": ticket["id"],
        "run_id": run["id"],
        "suggestion_id": run["suggestion"]["id"],
        "suggestion_status": run["suggestion"]["status"],
        "workflow_state": workflow.get("state"),
    }
    if check_review:
        result["review_checks"] = {
            decision: verify_review(client, fixtures, decision)
            for decision in ("approve", "reject")
        }
    return result


def use_utf8_output() -> None:
    """Печатать UTF-8 и при перенаправлении вывода в Windows, где по умолчанию cp1252.

    Консоль Windows и так получает Unicode; меняется только поток в файл или pipe.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Разобрать аргументы и вывести только безопасный JSON идентификаторов."""
    use_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", type=base_url, default="http://127.0.0.1:8001")
    parser.add_argument("--timeout", type=timeout_seconds, default=180.0)
    parser.add_argument(
        "--verify-review",
        action="store_true",
        help="Явно проверить approve/reject на двух отдельных учебных обращениях",
    )
    args = parser.parse_args(argv)
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        print(
            f"Демо: задай токен сотрудника в переменной {TOKEN_ENV} "
            "(python -m app.tokens issue …)",
            file=sys.stderr,
        )
        return 1
    client = DemoClient(args.url, time.monotonic() + args.timeout, token)
    try:
        result = run_demo(client, check_review=args.verify_review)
    except DemoError as error:
        print(f"Демо: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
