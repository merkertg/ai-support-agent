"""HTTP-предпросмотр LLM-черновика: никакой автоматической отправки клиенту."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.api.dependencies import ApiSession
from app.api.routes import ResourceId
from app.llm.client import get_draft_client, llm_status
from app.llm.errors import LLMError
from app.observability import get_logger, log_event, upstream_fields
from app.schemas.drafts import DraftResponse, LLMStatus
from app.schemas.resources import ErrorRead
from app.services import drafts
from app.services.drafts import ClientFactory

router = APIRouter(prefix="/api", tags=["LLM"])


def get_llm_client() -> ClientFactory:
    """Выдать ленивую фабрику, которую тест может заменить без сети и API-ключа.

    Depends выполняется до тела маршрута. Возвращаем саму функцию фабрики,
    а не готовый клиент, чтобы сначала проверить существование обращения.
    """
    return get_draft_client


async def llm_error_handler(request: Request, error: LLMError) -> JSONResponse:
    """Вернуть безопасную прикладную ошибку без ответа SDK и секретов настройки."""
    log_event(
        get_logger("llm"),
        logging.WARNING,
        "llm_error",
        path=request.url.path,
        status=error.status_code,
        error=type(error).__name__,
        **upstream_fields(error),
    )
    return JSONResponse(status_code=error.status_code, content={"detail": error.detail})


@router.get(
    "/llm/status",
    response_model=LLMStatus,
    responses={503: {"model": ErrorRead, "description": "Некорректная настройка LLM"}},
)
def get_llm_status() -> LLMStatus:
    """Прочитать публичную конфигурацию без SQL, сетевого запроса и выдачи ключа."""
    return llm_status()


@router.post(
    "/tickets/{resource_id}/draft",
    response_model=DraftResponse,
    responses={
        404: {"model": ErrorRead, "description": "Обращение или продукт не найдены"},
        413: {"model": ErrorRead, "description": "Контекст обращения слишком большой"},
        502: {"model": ErrorRead, "description": "Модель вернула непригодный ответ"},
        503: {"model": ErrorRead, "description": "БД или LLM временно недоступны"},
        504: {"model": ErrorRead, "description": "Истёк тайм-аут модели"},
    },
)
def create_ticket_draft(
    resource_id: ResourceId,
    session: ApiSession,
    client_factory: Annotated[ClientFactory, Depends(get_llm_client)],
) -> DraftResponse:
    """Создать проверяемый человеком черновик по id обращения без тела запроса.

    Синхронную функцию FastAPI запускает в рабочем потоке: ожидание SDK
    не блокирует основной асинхронный цикл приложения. Сервис не сохраняет
    черновик, AgentRun или сообщения и не меняет статус обращения.
    """
    return drafts.create_draft(session, resource_id, client_factory)
