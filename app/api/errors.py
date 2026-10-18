"""Преобразование прикладных исключений в единый JSON-формат API."""

from fastapi import Request
from fastapi.responses import JSONResponse
from psycopg import Error as PsycopgError
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import DATABASE_ERROR
from app.services.errors import ResourceError


async def resource_error_handler(
    request: Request, error: ResourceError
) -> JSONResponse:
    """Вернуть ожидаемую ошибку ресурса без подробностей внутреннего исключения."""
    return JSONResponse(status_code=error.status_code, content={"detail": error.detail})


async def database_error_handler(
    request: Request, error: SQLAlchemyError | PsycopgError
) -> JSONResponse:
    """Скрыть SQL/адреса из ошибок ORM и прямого драйвера checkpointer."""
    return JSONResponse(status_code=503, content={"detail": DATABASE_ERROR})
