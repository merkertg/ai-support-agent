"""Проверить запущенное API через настоящий HTTP без сторонних библиотек."""

import argparse
import json
import math
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

# Ограничиваем объём ответа, чтобы ошибка сервиса не заполнила память CI.
MAX_RESPONSE_BYTES = 1024 * 1024
EXPECTED_HEALTH = {"status": "ok", "service": "ai-support-agent"}
EXPECTED_READINESS = {"status": "ok", "database": "ok"}
API_RESOURCES = ("customers", "products", "licenses", "tickets")
EXPECTED_MOCK_LLM_STATUS = {
    "provider": "mock",
    "model": "mock-draft-v1",
    "configured": True,
}
# Порядок вызовов учебного mock-агента при переданном ключе лицензии.
EXPECTED_AGENT_TOOLS = [
    "get_ticket",
    "get_customer",
    "get_ticket_history",
    "get_license",
]
DRAFT_CATEGORIES = {"technical_issue", "license_question", "how_to", "other"}
MOCK_NOTE = "Реальная модель не вызывалась."


class SmokeTestError(Exception):
    """Сообщить, какая проверка работающего API завершилась неудачно."""


class ServiceUnavailable(SmokeTestError):
    """Отличить сетевую недоступность при запуске от неверного ответа API."""


class NoRedirects(HTTPRedirectHandler):
    """Проверять исходный маршрут: перенаправление не считается HTTP 200."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Запретить автоматический переход urllib по ответам 3xx."""
        return None


def positive_timeout(value: str) -> float:
    """Принять только конечное положительное число секунд для дедлайна."""
    try:
        timeout = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("тайм-аут должен быть числом") from error
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("тайм-аут должен быть конечным числом > 0")
    return timeout


def base_url(value: str) -> str:
    """Проверить адрес корня API и убрать завершающий слеш."""
    try:
        parts = urlsplit(value)
        # Чтение port также проверяет корректность номера порта в URL.
        valid = parts.hostname is not None and (parts.port is None or parts.port > 0)
    except ValueError as error:
        raise argparse.ArgumentTypeError("некорректный URL API") from error
    if (
        not valid
        or parts.scheme not in {"http", "https"}
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise argparse.ArgumentTypeError(
            "нужен корневой http(s) URL без пароля, пути и параметров"
        )
    return value.rstrip("/")


def fetch(
    url: str,
    deadline: float,
    *,
    method: str = "GET",
    payload: dict | None = None,
    expected_status: int = 200,
) -> bytes:
    """Выполнить HTTP-запрос до дедлайна без скрытых повторов POST."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SmokeTestError(f"Истёк общий тайм-аут перед запросом {url}")
    try:
        # Каждая сетевая попытка ограничена оставшимся временем и двумя секундами.
        headers = {"User-Agent": "ai-support-agent-smoke-test"}
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, method=method, headers=headers)
        with build_opener(NoRedirects()).open(
            request, timeout=min(2.0, remaining)
        ) as response:
            if response.status != expected_status:
                raise SmokeTestError(
                    f"{url}: ожидался HTTP {expected_status}, получен {response.status}"
                )
            # read1 возвращает очередную порцию: медленный поток байтов не
            # может бесконечно продлевать чтение одним вызовом read().
            chunks = []
            size = 0
            while True:
                if time.monotonic() >= deadline:
                    raise SmokeTestError(f"Истёк общий тайм-аут при запросе {url}")
                chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise SmokeTestError(f"{url}: ответ превышает размер 1 МиБ")
            body = b"".join(chunks)
    except HTTPError as error:
        # HTTPError является наследником URLError: обрабатываем его первым.
        raise SmokeTestError(
            f"{url}: ожидался HTTP {expected_status}, получен HTTP {error.code}"
        ) from error
    except (URLError, OSError) as error:
        raise ServiceUnavailable(f"{url}: сетевая ошибка: {error}") from error
    if time.monotonic() > deadline:
        raise SmokeTestError(f"Истёк общий тайм-аут при запросе {url}")
    return body


def json_object(body: bytes, endpoint: str) -> dict:
    """Разобрать UTF-8 JSON и потребовать объект верхнего уровня."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SmokeTestError(f"{endpoint}: ответ содержит невалидный JSON") from error
    if not isinstance(payload, dict):
        raise SmokeTestError(f"{endpoint}: ожидался JSON-объект")
    return payload


def check_business_api(url: str, paths: dict, deadline: float) -> None:
    """Проверить 12 операций в OpenAPI и четыре списка по настоящему HTTP.

    Smoke ничего не записывает в БД: создание и ошибки транзакций проверяют
    интеграционные тесты. Здесь проверяем маршруты собранного контейнера.
    """
    for resource in API_RESOURCES:
        collection = f"/api/{resource}"
        operations = paths.get(collection)
        for method in ("get", "post"):
            if not isinstance(operations, dict) or not isinstance(
                operations.get(method), dict
            ):
                raise SmokeTestError(
                    f"/openapi.json: отсутствует операция {method.upper()} {collection}"
                )
        # Имя параметра ({customer_id}, {id} и т. п.) не меняет контракт URL.
        item_operations = [
            operation
            for path, operation in paths.items()
            if path.startswith(collection + "/{")
            and path.endswith("}")
            and path.count("/") == 3
        ]
        if not any(
            isinstance(operation, dict) and isinstance(operation.get("get"), dict)
            for operation in item_operations
        ):
            raise SmokeTestError(
                f"/openapi.json: отсутствует операция GET {collection}/{{id}}"
            )
        endpoint = f"{collection}?limit=1"
        try:
            records = json.loads(fetch(f"{url}{endpoint}", deadline).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SmokeTestError(
                f"{endpoint}: ответ содержит невалидный JSON"
            ) from error
        if not isinstance(records, list) or len(records) > 1:
            raise SmokeTestError(f"{endpoint}: ожидался JSON-массив длиной не более 1")
        # bool в Python является int, но не может быть идентификатором записи.
        if any(
            not isinstance(record, dict)
            or type(record.get("id")) is not int
            or record["id"] <= 0
            for record in records
        ):
            raise SmokeTestError(f"{endpoint}: ожидались объекты с положительным id")
        print(f"OK {endpoint}: HTTP 200, список и операции GET/POST описаны")


def create_smoke_record(
    url: str, resource: str, payload: dict, deadline: float
) -> dict:
    """Явно создать учебную запись только в режимах --check-llm и --check-agent.

    Случайный суффикс задаёт вызывающая функция: повтор запуска не заменяет
    существующие данные. При сетевой ошибке POST никогда не повторяется.
    """
    endpoint = f"/api/{resource}"
    record = json_object(
        fetch(
            f"{url}{endpoint}",
            deadline,
            method="POST",
            payload=payload,
            expected_status=201,
        ),
        endpoint,
    )
    if type(record.get("id")) is not int or record["id"] <= 0:
        raise SmokeTestError(f"{endpoint}: создание не вернуло положительный id")
    return record


def has_operation(paths: dict, prefix: str, suffix: str, method: str) -> bool:
    """Найти операцию с параметром пути независимо от имени параметра."""
    return any(
        path.startswith(prefix)
        and path.endswith(suffix)
        and isinstance(operations, dict)
        and isinstance(operations.get(method), dict)
        for path, operations in paths.items()
    )


def check_mock_llm(url: str, paths: dict, deadline: float) -> dict[str, dict]:
    """Проверить mock-черновик; сначала запретить платный режим, затем писать.

    Флаг создаёт три вымышленные записи (клиент, продукт, обращение). Сам
    маршрут черновика не должен их менять; это проверяем отдельным GET.
    В CI записи и том принадлежат одноразовому Compose-проекту.
    """
    status_path = paths.get("/api/llm/status")
    if not isinstance(status_path, dict) or not isinstance(
        status_path.get("get"), dict
    ):
        raise SmokeTestError("/openapi.json: отсутствует операция GET /api/llm/status")
    if not has_operation(paths, "/api/tickets/{", "}/draft", "post"):
        raise SmokeTestError(
            "/openapi.json: отсутствует операция POST /api/tickets/{id}/draft"
        )
    llm_status = json_object(
        fetch(f"{url}/api/llm/status", deadline), "/api/llm/status"
    )
    if llm_status != EXPECTED_MOCK_LLM_STATUS:
        # Проверка выполняется ДО любого POST: реальный OpenAI не вызывается.
        raise SmokeTestError(
            "--check-llm разрешён только для настроенного mock; POST не выполнялись"
        )
    print("OK /api/llm/status: mock настроен; платный провайдер исключён")
    suffix = uuid4().hex[:12]
    customer = create_smoke_record(
        url,
        "customers",
        {
            "name": "Smoke customer",
            "email": f"smoke-{suffix}@example.com",
        },
        deadline,
    )
    product = create_smoke_record(
        url,
        "products",
        {
            "name": "Smoke product",
            "code": f"smoke-{suffix}",
        },
        deadline,
    )
    ticket = create_smoke_record(
        url,
        "tickets",
        {
            "customer_id": customer["id"],
            "product_id": product["id"],
            "subject": "Учебная проверка черновика",
            "description": "Нужны подробности ошибки",
        },
        deadline,
    )
    endpoint = f"/api/tickets/{ticket['id']}/draft"
    draft = json_object(fetch(f"{url}{endpoint}", deadline, method="POST"), endpoint)
    content = draft.get("content")
    if (
        type(draft.get("ticket_id")) is not int
        or draft["ticket_id"] != ticket["id"]
        or draft.get("provider") != "mock"
        or draft.get("model") != "mock-draft-v1"
        or draft.get("requires_human_review") is not True
        or draft.get("usage") is not None
        or not isinstance(content, dict)
        or set(content) != {"summary", "category", "draft_reply"}
        or not isinstance(content.get("summary"), str)
        or not content["summary"].strip()
        or content.get("category") not in DRAFT_CATEGORIES
        or not isinstance(content.get("draft_reply"), str)
        or MOCK_NOTE not in content["draft_reply"]
    ):
        raise SmokeTestError(
            f"{endpoint}: неверный контракт демонстрационного черновика"
        )
    unchanged = json_object(
        fetch(f"{url}/api/tickets/{ticket['id']}", deadline), "/api/tickets/{id}"
    )
    if unchanged != ticket:
        raise SmokeTestError("Черновик изменил сохранённое обращение")
    print(f"OK {endpoint}: демонстрационный черновик, требуется проверка человеком")
    print("OK обращение не изменилось; созданы только три явные учебные записи")
    return {"customer": customer, "product": product, "ticket": ticket}


def valid_agent_run(run: dict, ticket_id: int) -> bool:
    """Проверить сохранённый mock-запуск: четыре инструмента и pending-черновик."""
    trace = run.get("tool_trace")
    result = run.get("result")
    suggestion = run.get("suggestion")
    if (
        type(run.get("id")) is not int
        or run["id"] <= 0
        or run.get("ticket_id") != ticket_id
        or run.get("status") != "completed"
        or run.get("provider") != "mock"
        or run.get("model") != "mock-agent-v1"
        or run.get("error_message") is not None
        or run.get("steps") != 3
        or not isinstance(trace, list)
        or not all(isinstance(item, dict) for item in trace)
        or [item.get("name") for item in trace] != EXPECTED_AGENT_TOOLS
        or not all(
            isinstance(item.get("result"), dict) and item["result"].get("ok") is True
            for item in trace
        )
        or not isinstance(result, dict)
        or set(result) != {"summary", "category", "draft_reply"}
        or result.get("category") not in DRAFT_CATEGORIES
        or not isinstance(result.get("draft_reply"), str)
        or MOCK_NOTE not in result["draft_reply"]
        or not isinstance(suggestion, dict)
        or type(suggestion.get("id")) is not int
        or suggestion.get("status") != "pending"
        or suggestion.get("requires_human_review") is not True
        or suggestion.get("content") != result["draft_reply"]
    ):
        return False
    # Ключ в журнале маскируется, а результат проверки берётся из инструмента.
    license_call = trace[3]
    data = license_call["result"].get("data")
    return license_call.get("arguments") == {"license_key": "***"} and (
        isinstance(data, dict) and data.get("valid") is True
    )


def check_mock_agent(
    url: str, paths: dict, deadline: float, records: dict[str, dict]
) -> None:
    """Проверить сохранённый запуск агента на учебных записях --check-llm.

    Вызывается только после подтверждения mock в check_mock_llm. Создаёт
    лицензию, запуск и pending-предложение; обращение при этом не меняется.
    """
    if not has_operation(paths, "/api/tickets/{", "}/analyze", "post"):
        raise SmokeTestError(
            "/openapi.json: отсутствует операция POST /api/tickets/{id}/analyze"
        )
    if not has_operation(paths, "/api/agent-runs/{", "}", "get"):
        raise SmokeTestError(
            "/openapi.json: отсутствует операция GET /api/agent-runs/{id}"
        )
    customer, product, ticket = (
        records["customer"],
        records["product"],
        records["ticket"],
    )
    license_key = f"SMOKE-{uuid4().hex[:12].upper()}"
    create_smoke_record(
        url,
        "licenses",
        {
            "customer_id": customer["id"],
            "product_id": product["id"],
            "license_key": license_key,
        },
        deadline,
    )
    endpoint = f"/api/tickets/{ticket['id']}/analyze"
    body = fetch(
        f"{url}{endpoint}",
        deadline,
        method="POST",
        payload={"license_key": license_key},
        expected_status=201,
    )
    run = json_object(body, endpoint)
    if license_key.encode() in body or not valid_agent_run(run, ticket["id"]):
        raise SmokeTestError(f"{endpoint}: неверный контракт сохранённого запуска")
    print(f"OK {endpoint}: HTTP 201, четыре инструмента, черновик ожидает проверки")
    stored_endpoint = f"/api/agent-runs/{run['id']}"
    stored = json_object(fetch(f"{url}{stored_endpoint}", deadline), stored_endpoint)
    if stored != run:
        raise SmokeTestError(f"{stored_endpoint}: сохранённый запуск отличается")
    print(f"OK {stored_endpoint}: GET читает тот же запуск без нового вызова модели")
    unchanged = json_object(
        fetch(f"{url}/api/tickets/{ticket['id']}", deadline), "/api/tickets/{id}"
    )
    if unchanged != ticket:
        raise SmokeTestError("Агент изменил сохранённое обращение")
    print("OK обращение не изменилось; добавлены лицензия, запуск и предложение")


def run_checks(
    url: str,
    timeout: float,
    check_database: bool = False,
    check_api: bool = False,
    check_llm: bool = False,
    check_agent: bool = False,
) -> None:
    """Проверить HTTP-контракт и при явном флаге готовность БД за один срок."""
    # Агент использует учебные записи и проверку mock из --check-llm.
    check_llm = check_llm or check_agent
    # Предметное API требует БД, поэтому --check-api включает и /ready.
    check_api = check_api or check_llm
    check_database = check_database or check_api
    deadline = time.monotonic() + timeout
    while True:
        try:
            health_body = fetch(f"{url}/health", deadline)
        except ServiceUnavailable as error:
            # Повторяем только сетевые ошибки запуска. Неверный HTTP или JSON
            # означает сломанный контракт и завершает проверку немедленно.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SmokeTestError(
                    f"За {timeout:g} с API не запустился. Последняя ошибка: {error}"
                ) from error
            time.sleep(min(0.25, remaining))
            continue
        break

    health = json_object(health_body, "/health")
    if health != EXPECTED_HEALTH:
        raise SmokeTestError(
            f"/health: ожидался {EXPECTED_HEALTH!r}, получен {health!r}"
        )
    print("OK /health: HTTP 200 и ожидаемый JSON")

    if check_database:
        # Compose уже дождался готовности. HTTP 503 здесь — ошибка контракта,
        # а не повод повторять запрос до случайного успешного ответа.
        readiness = json_object(fetch(f"{url}/ready", deadline), "/ready")
        if readiness != EXPECTED_READINESS:
            raise SmokeTestError(
                f"/ready: ожидался {EXPECTED_READINESS!r}, получен {readiness!r}"
            )
        print("OK /ready: HTTP 200, соединение с БД и миграции готовы")

    docs = fetch(f"{url}/docs", deadline)
    if b"SwaggerUIBundle" not in docs:
        raise SmokeTestError("/docs: в ответе отсутствует SwaggerUIBundle")
    print("OK /docs: HTTP 200 и Swagger UI")

    schema = json_object(fetch(f"{url}/openapi.json", deadline), "/openapi.json")
    info = schema.get("info")
    if not isinstance(info, dict) or info.get("title") != "AI Support Agent":
        raise SmokeTestError("/openapi.json: неверный или отсутствующий info.title")
    paths = schema.get("paths")
    health_path = paths.get("/health") if isinstance(paths, dict) else None
    if not isinstance(health_path, dict) or not isinstance(
        health_path.get("get"), dict
    ):
        raise SmokeTestError("/openapi.json: отсутствует операция GET /health")
    if check_database:
        ready_path = paths.get("/ready")
        if not isinstance(ready_path, dict) or not isinstance(
            ready_path.get("get"), dict
        ):
            raise SmokeTestError("/openapi.json: отсутствует операция GET /ready")
        print("OK /openapi.json: HTTP 200, title и операции GET /health, GET /ready")
    else:
        print("OK /openapi.json: HTTP 200, title и операция GET /health")

    if check_api:
        check_business_api(url, paths, deadline)
    if check_llm:
        records = check_mock_llm(url, paths, deadline)
        if check_agent:
            check_mock_agent(url, paths, deadline, records)


def main() -> int:
    """Прочитать CLI, вывести понятный результат и вернуть код для CI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        type=base_url,
        default="http://127.0.0.1:8000",
        help="адрес корня работающего API (по умолчанию http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--timeout",
        type=positive_timeout,
        default=30.0,
        help="общий срок ожидания и проверок в секундах (по умолчанию 30)",
    )
    parser.add_argument(
        "--check-database",
        action="store_true",
        help="также проверить /ready и его описание в OpenAPI (этап 02)",
    )
    parser.add_argument(
        "--check-api",
        action="store_true",
        help="проверить готовность БД и API клиентов, продуктов, лицензий, обращений",
    )
    parser.add_argument(
        "--check-llm",
        action="store_true",
        help=(
            "только mock: проверить черновик и создать 3 учебные записи; "
            "включает --check-api"
        ),
    )
    parser.add_argument(
        "--check-agent",
        action="store_true",
        help=(
            "только mock: дополнительно создать лицензию и сохранённый запуск "
            "агента; включает --check-llm"
        ),
    )
    args = parser.parse_args()
    try:
        run_checks(
            args.base_url,
            args.timeout,
            check_database=args.check_database,
            check_api=args.check_api,
            check_llm=args.check_llm,
            check_agent=args.check_agent,
        )
    except SmokeTestError as error:
        print(f"Smoke test провален: {error}", file=sys.stderr)
        return 1
    print("Smoke test пройден.")
    return 0


if __name__ == "__main__":
    # Нулевой код разрешает следующий этап CI; ненулевой останавливает цепочку.
    raise SystemExit(main())
