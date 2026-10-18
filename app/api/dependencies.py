"""Общая сессия HTTP-запроса с безопасным ответом при сбое PostgreSQL."""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import DatabaseNotConfigured, get_session

DATABASE_ERROR = "База данных временно недоступна"


def get_api_session() -> Iterator[Session]:
    """Выдать сессию и заменить конфигурационные и SQL-ошибки на HTTP 503.

    get_session сначала откатывает исключение и закрывает соединение.
    Только после этого внешний обработчик формирует безопасное сообщение,
    не раскрывая URL, пароль, SQL или параметры пользовательского запроса.
    """
    try:
        yield from get_session()
    except (DatabaseNotConfigured, ValidationError, SQLAlchemyError):
        raise HTTPException(status_code=503, detail=DATABASE_ERROR) from None


ApiSession = Annotated[Session, Depends(get_api_session)]
