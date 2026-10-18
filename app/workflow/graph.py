"""Граф LangGraph: analyze → human_review (ожидание) → deliver или reject.

Цикл инструментов агента остаётся проверенным AgentRuntime этапов 05–06.
LangGraph управляет процессом вокруг него: сохраняет состояние после каждого
узла в checkpointer и останавливается в human_review через interrupt(),
пока сотрудник не примет решение. Продолжение — Command(resume=решение).
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt

REVIEW_NODE = "human_review"
Outcome = Literal["awaiting_review", "delivered", "rejected", "failed"]


class ReviewState(TypedDict, total=False):
    """Состояние в checkpoint: только id и итоги, без текста и ключа лицензии."""

    ticket_id: int
    thread_id: str
    run_id: int
    suggestion_id: int
    outcome: Outcome
    error: dict
    review: dict
    applied_review_id: str
    message_id: int | None
    ticket_status: str


@dataclass(frozen=True)
class WorkflowContext:
    """Зависимости узлов на время одного вызова; в checkpoint не сохраняются.

    analyze(ticket_id, thread_id) запускает агента и возвращает обновление
    состояния; apply_review(suggestion_id, review) сохраняет решение в БД.
    """

    analyze: Callable[[int, str], dict]
    apply_review: Callable[[int, dict], dict]


def analyze(state: ReviewState, runtime: Runtime[WorkflowContext]) -> dict:
    """Запустить агента: успешный анализ ведёт к ожиданию решения сотрудника."""
    return runtime.context.analyze(state["ticket_id"], state["thread_id"])


def after_analysis(state: ReviewState) -> str:
    """Неудачный анализ завершает процесс: проверять нечего."""
    return REVIEW_NODE if state.get("outcome") == "awaiting_review" else END


def human_review(state: ReviewState) -> dict:
    """Остановить граф до решения сотрудника; при resume вернуть это решение.

    До interrupt() нет побочных эффектов: при продолжении LangGraph выполняет
    узел заново, и interrupt() сразу возвращает переданное решение.
    """
    review = interrupt(
        {"run_id": state["run_id"], "suggestion_id": state["suggestion_id"]}
    )
    return {"review": review}


def after_review(state: ReviewState) -> str:
    """Направить одобрение на доставку, отклонение — на закрытие предложения."""
    return "deliver" if state["review"]["decision"] == "approve" else "reject"


def deliver(state: ReviewState, runtime: Runtime[WorkflowContext]) -> dict:
    """Сохранить одобрение и учебно доставить ответ сообщением обращения."""
    result = runtime.context.apply_review(state["suggestion_id"], state["review"])
    return {
        "outcome": "delivered",
        "applied_review_id": state["review"]["review_id"],
        "message_id": result["message_id"],
        "ticket_status": result["ticket_status"],
    }


def reject(state: ReviewState, runtime: Runtime[WorkflowContext]) -> dict:
    """Сохранить отклонение с причиной; обращение и сообщения не меняются."""
    result = runtime.context.apply_review(state["suggestion_id"], state["review"])
    return {
        "outcome": "rejected",
        "applied_review_id": state["review"]["review_id"],
        "message_id": None,
        "ticket_status": result["ticket_status"],
    }


def build_graph(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    """Собрать и скомпилировать граф с переданным хранилищем состояния."""
    graph = StateGraph(ReviewState, context_schema=WorkflowContext)
    graph.add_node("analyze", analyze)
    graph.add_node(REVIEW_NODE, human_review)
    graph.add_node("deliver", deliver)
    graph.add_node("reject", reject)
    graph.add_edge(START, "analyze")
    graph.add_conditional_edges("analyze", after_analysis, [REVIEW_NODE, END])
    graph.add_conditional_edges(REVIEW_NODE, after_review, ["deliver", "reject"])
    graph.add_edge("deliver", END)
    graph.add_edge("reject", END)
    return graph.compile(checkpointer=checkpointer, name="support_review")
