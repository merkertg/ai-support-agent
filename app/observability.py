"""Журнал API: одна JSON-строка на событие и id запроса для поиска по логам.

В журнал попадают классы ошибок, коды, идентификаторы и id сотрудника, но не
тела запросов, заголовки (включая Authorization с токеном), query string, SQL,
тексты ответов SDK и ключи: строка запроса может содержать license_key,
а сообщение драйвера — параметры SQL и адрес базы.
"""

import json
import logging
import os
import re
import sys
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

LOGGER_NAME = "app"
REQUEST_ID_HEADER = "X-Request-ID"
# Чужой id принимается, только если его можно безопасно вставить в журнал.
VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{8,64}")
# Успешные проверки Docker идут каждые несколько секунд и не несут информации.
QUIET_PATHS = frozenset({"/health", "/ready"})
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
INTERNAL_ERROR = "Внутренняя ошибка сервера. Сообщите администратору код запроса."
# Ошибки этих библиотек описывают ответ внешнего сервиса, а не наш код.
UPSTREAM_MODULES = ("openai", "httpx", "httpx2", "qdrant_client")

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


class JSONFormatter(logging.Formatter):
    """Записать событие одной строкой JSON с id текущего HTTP-запроса."""

    def format(self, record: logging.LogRecord) -> str:
        """Собрать время, уровень, событие, поля и traceback без сторонних данных."""
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None) or request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        payload.update(getattr(record, "fields", None) or {})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def get_logger(name: str) -> logging.Logger:
    """Вернуть дочерний журнал приложения: он наследует настройку «app»."""
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    exc_info: bool = False,
    **fields,
) -> None:
    """Записать событие с полями; None-поля пропускаются ради коротких строк."""
    values = {key: value for key, value in fields.items() if value is not None}
    logger.log(level, event, exc_info=exc_info, extra={"fields": values})


def configure_logging(level: str | None = None) -> None:
    """Направить журнал приложения в stdout в JSON; повторный вызов безопасен.

    Уровень берётся из LOG_LEVEL (DEBUG, INFO, WARNING, ERROR), иначе INFO.
    Журнал «app» не передаёт записи корневому: формат не смешивается с Uvicorn.
    """
    logger = logging.getLogger(LOGGER_NAME)
    value = (level or os.environ.get("LOG_LEVEL") or "INFO").strip().upper()
    logger.setLevel(value if value in LEVELS else "INFO")
    if not any(getattr(handler, "app_json", False) for handler in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JSONFormatter())
        handler.app_json = True
        logger.addHandler(handler)
    logger.propagate = False


def upstream_fields(error: BaseException) -> dict:
    """Найти в цепочке исключений ошибку внешнего SDK и вернуть её безопасные поля.

    Безопасные ошибки создаются через `raise ... from None`: исходная ошибка
    остаётся в __context__. Берём только класс, HTTP-код и id запроса провайдера,
    но не текст: в нём бывают фрагменты исходного запроса.
    """
    current: BaseException | None = error
    for _ in range(6):
        if current is None:
            break
        module = type(current).__module__.split(".")[0]
        if module in UPSTREAM_MODULES:
            return {
                "upstream_error": type(current).__name__,
                "upstream_status": getattr(current, "status_code", None),
                "upstream_request_id": getattr(current, "request_id", None),
            }
        current = current.__context__
    return {}


def database_fields(error: BaseException) -> dict:
    """Описать ошибку БД классом и SQLSTATE, без SQL, параметров и адреса."""
    driver = getattr(error, "orig", None) or error
    return {
        "error": type(error).__name__,
        "driver_error": type(driver).__name__ if driver is not error else None,
        "sqlstate": getattr(driver, "sqlstate", None),
    }


def incoming_request_id(headers: Headers) -> str:
    """Принять корректный id от прокси или клиента, иначе создать новый."""
    value = headers.get(REQUEST_ID_HEADER, "")
    return value if VALID_REQUEST_ID.fullmatch(value) else uuid4().hex


class RequestContextMiddleware:
    """Присвоить запросу id, записать итог и перехватить непредвиденную ошибку.

    Middleware работает на уровне ASGI: перехваченные обработчиками ошибки
    приходят сюда уже ответами, а необработанные превращаются в JSON 500
    с тем же id в заголовке. Путь пишется без query string.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Обернуть приложение и подготовить журнал запросов."""
        self.app = app
        self.logger = get_logger("http")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Обработать HTTP-запрос; остальные типы ASGI передать без изменений."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = incoming_request_id(Headers(scope=scope))
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status = 500
        response_started = False

        async def send_with_id(message: Message) -> None:
            """Добавить id в заголовки ответа и запомнить код состояния."""
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            # Traceback нужен для исправления кода; тело запроса в него не входит.
            log_event(
                self.logger,
                logging.ERROR,
                "unhandled_error",
                exc_info=True,
                method=scope["method"],
                path=scope["path"],
            )
            if response_started:
                raise
            status = 500
            response = JSONResponse(
                {"detail": INTERNAL_ERROR},
                status_code=500,
                headers={REQUEST_ID_HEADER: request_id},
            )
            await response(scope, receive, send)
        finally:
            if not (scope["path"] in QUIET_PATHS and status < 400):
                # Зависимость аутентификации кладёт id сотрудника в request.state.
                state = scope.get("state")
                log_event(
                    self.logger,
                    logging.WARNING if status >= 500 else logging.INFO,
                    "request",
                    method=scope["method"],
                    path=scope["path"],
                    status=status,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                    user_id=state.get("user_id") if isinstance(state, dict) else None,
                )
            request_id_var.reset(token)
