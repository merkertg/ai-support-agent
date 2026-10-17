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


def run_checks(url: str, timeout: float) -> None:
    """Дождаться запуска и проверить health, Swagger UI и OpenAPI за один срок."""
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
    print("OK /openapi.json: HTTP 200, title и операция GET /health")


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
    args = parser.parse_args()
    try:
        run_checks(args.base_url, args.timeout)
    except SmokeTestError as error:
        print(f"Smoke test провален: {error}", file=sys.stderr)
        return 1
    print("Smoke test пройден.")
    return 0


if __name__ == "__main__":
    # Нулевой код разрешает следующий этап CI; ненулевой останавливает цепочку.
    raise SystemExit(main())
