"""Преобразование прикладных исключений в единый JSON-формат API."""

import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from psycopg import Error as PsycopgError
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import DATABASE_ERROR
from app.observability import database_fields, get_logger, log_event
from app.services.errors import ResourceError

logger = get_logger("errors")


async def resource_error_handler(
    request: Request, error: ResourceError
) -> JSONResponse:
    """Вернуть ожидаемую ошибку ресурса без подробностей внутреннего исключения."""
    # Текст detail составлен приложением и не содержит данных запроса.
    log_event(
        logger,
        logging.INFO,
        "resource_error",
        status=error.status_code,
        error=type(error).__name__,
        detail=error.detail,
        path=request.url.path,
    )
    return JSONResponse(status_code=error.status_code, content={"detail": error.detail})


async def database_error_handler(
    request: Request, error: SQLAlchemyError | PsycopgError
) -> JSONResponse:
    """Скрыть SQL/адреса из ошибок ORM и прямого драйвера checkpointer."""
    # В журнал — класс и SQLSTATE: str(error) содержит SQL, параметры и адрес БД.
    log_event(
        logger,
        logging.ERROR,
        "database_error",
        path=request.url.path,
        **database_fields(error),
    )
    return JSONResponse(status_code=503, content={"detail": DATABASE_ERROR})
