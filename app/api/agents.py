"""HTTP-анализ обращения: сохранённый запуск и предложение без отправки ответа."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from app.agent.provider import get_agent_provider
from app.api.dependencies import ApiSession
from app.api.knowledge import get_knowledge_factory
from app.api.reviews import get_checkpointer_factory
from app.api.routes import ResourceId
from app.api.security import (
    AUTH_RESPONSES,
    WRITE_RESPONSES,
    get_principal,
    operator_writer,
)
from app.knowledge.index import IndexFactory
from app.schemas.agents import AgentErrorRead, AgentRunRead, AnalyzeRequest
from app.schemas.resources import ErrorRead
from app.services import agents
from app.services.agents import AgentRunFailure, ProviderFactory
from app.workflow import service as workflow
from app.workflow.service import CheckpointerFactory

router = APIRouter(
    prefix="/api",
    tags=["Agent"],
    dependencies=[Depends(get_principal)],
    responses=AUTH_RESPONSES,
)


def get_agent_factory() -> ProviderFactory:
    """Выдать ленивую фабрику для проверки обращения раньше настройки провайдера."""
    return get_agent_provider


async def agent_error_handler(request: Request, error: AgentRunFailure) -> JSONResponse:
    """Вернуть id сохранённой ошибки, не раскрывая SDK, SQL и конфигурацию."""
    return JSONResponse(
        status_code=error.status_code,
        content={"detail": error.detail, "run_id": error.run_id},
    )


@router.post(
    "/tickets/{resource_id}/analyze",
    status_code=status.HTTP_201_CREATED,
    response_model=AgentRunRead,
    dependencies=[Depends(operator_writer)],
    responses=WRITE_RESPONSES
    | {
        404: {"model": ErrorRead, "description": "Обращение не найдено"},
        413: {"model": AgentErrorRead, "description": "Контекст превышает лимит"},
        502: {"model": AgentErrorRead, "description": "Непригодный ответ агента"},
        503: {"model": ErrorRead | AgentErrorRead, "description": "БД/LLM недоступны"},
        504: {"model": AgentErrorRead, "description": "Истёк тайм-аут агента"},
    },
)
def analyze_ticket(
    resource_id: ResourceId,
    session: ApiSession,
    provider_factory: Annotated[ProviderFactory, Depends(get_agent_factory)],
    knowledge_factory: Annotated[IndexFactory, Depends(get_knowledge_factory)],
    checkpointer_factory: Annotated[
        CheckpointerFactory, Depends(get_checkpointer_factory)
    ],
    payload: AnalyzeRequest | None = None,
) -> AgentRunRead:
    """Запустить инструменты чтения и сохранить черновик для проверки человеком.

    Тело необязательно: {} и отсутствие body означают анализ без подсказки
    ключа лицензии. Другие поля, включая команды изменения статуса, запрещены.
    Синхронный маршрут работает в потоке и не блокирует async-цикл FastAPI.
    """
    # С этапа 07 анализ — первый узел графа LangGraph: после него граф
    # останавливается и ждёт решения сотрудника (POST /api/suggestions/.../review).
    return workflow.start_analysis(
        session,
        resource_id,
        payload or AnalyzeRequest(),
        provider_factory,
        knowledge_factory,
        checkpointer_factory,
    )


@router.get(
    "/agent-runs/{resource_id}",
    response_model=AgentRunRead,
    responses={
        404: {"model": ErrorRead, "description": "Запуск агента не найден"},
        503: {"model": ErrorRead, "description": "База данных временно недоступна"},
    },
)
def get_agent_run(resource_id: ResourceId, session: ApiSession) -> AgentRunRead:
    """Прочитать сохранённый результат или ошибку без повторного вызова модели."""
    return agents.read_run(session, resource_id)
