"""Проверить запущенное API через настоящий HTTP без сторонних библиотек."""

import argparse
import json
import math
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

# Ограничиваем объём ответа, чтобы ошибка сервиса не заполнила память CI.
MAX_RESPONSE_BYTES = 1024 * 1024
EXPECTED_HEALTH = {"status": "ok", "service": "ai-support-agent"}
EXPECTED_READINESS = {"status": "ok", "database": "ok"}
API_RESOURCES = ("customers", "products", "licenses", "tickets")


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


def fetch(url: str, deadline: float) -> bytes:
    """Получить HTTP 200 до дедлайна; сетевые и HTTP ошибки различаются."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SmokeTestError(f"Истёк общий тайм-аут перед запросом {url}")
    try:
        # Каждая сетевая попытка ограничена оставшимся временем и двумя секундами.
        request = Request(url, headers={"User-Agent": "ai-support-agent-smoke-test"})
        with build_opener(NoRedirects()).open(
            request, timeout=min(2.0, remaining)
        ) as response:
            if response.status != 200:
                raise SmokeTestError(
                    f"{url}: ожидался HTTP 200, получен {response.status}"
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
            f"{url}: ожидался HTTP 200, получен HTTP {error.code}"
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


def run_checks(
    url: str, timeout: float, check_database: bool = False, check_api: bool = False
) -> None:
    """Проверить HTTP-контракт и при явном флаге готовность БД за один срок."""
    # Предметное API требует БД, поэтому --check-api включает и /ready.
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
    args = parser.parse_args()
    try:
        run_checks(
            args.base_url,
            args.timeout,
            check_database=args.check_database,
            check_api=args.check_api,
        )
    except SmokeTestError as error:
        print(f"Smoke test провален: {error}", file=sys.stderr)
        return 1
    print("Smoke test пройден.")
    return 0


if __name__ == "__main__":
    # Нулевой код разрешает следующий этап CI; ненулевой останавливает цепочку.
    raise SystemExit(main())
