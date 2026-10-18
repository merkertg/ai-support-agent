"""Граф LangGraph без PostgreSQL: остановка, продолжение и ветви решения."""

import importlib.util
import re
from pathlib import Path
from unittest.mock import Mock

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.base import BasePostgresSaver
from langgraph.types import Command
from sqlalchemy.orm import Session

from app.llm.errors import LLMTimeoutError
from app.schemas.agents import AnalyzeRequest
from app.services import agents
from app.services.agents import AgentRunFailure
from app.services.errors import ResourceNotFound
from app.workflow import service as workflow
from app.workflow.checkpoint import conninfo
from app.workflow.graph import REVIEW_NODE, WorkflowContext, build_graph

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG = {"configurable": {"thread_id": "thread-1"}}


class Recorder:
    """Подменить узлы графа и записать их вызовы."""

    def __init__(self, analysis: dict | None = None) -> None:
        """Задать результат анализа: по умолчанию успешный запуск 7 / предложение 3."""
        self.analysis = analysis or {
            "outcome": "awaiting_review",
            "run_id": 7,
            "suggestion_id": 3,
        }
        self.analyzed: list[tuple[int, str]] = []
        self.reviews: list[tuple[int, dict]] = []

    def analyze(self, ticket_id: int, thread_id: str) -> dict:
        """Вернуть заранее заданный итог анализа."""
        self.analyzed.append((ticket_id, thread_id))
        return self.analysis

    def apply_review(self, suggestion_id: int, review: dict) -> dict:
        """Записать решение вместо транзакции PostgreSQL."""
        self.reviews.append((suggestion_id, review))
        message = 11 if review["decision"] == "approve" else None
        status = "waiting_customer" if message else "open"
        return {"message_id": message, "ticket_status": status}

    def context(self) -> WorkflowContext:
        """Собрать контекст выполнения графа из подменённых функций."""
        return WorkflowContext(analyze=self.analyze, apply_review=self.apply_review)


def start(recorder: Recorder, saver: InMemorySaver) -> dict:
    """Запустить граф до первой остановки."""
    return build_graph(saver).invoke(
        {"ticket_id": 5, "thread_id": "thread-1"}, CONFIG, context=recorder.context()
    )


def review(decision: str = "approve", review_id: str = "r-1") -> dict:
    """Сформировать решение так, как его передаёт сервис."""
    return {"decision": decision, "reviewer_id": 2, "review_id": review_id}


def test_graph_pauses_at_human_review_after_successful_analysis() -> None:
    """После анализа граф ждёт решения и сохраняет только id в состоянии."""
    saver, recorder = InMemorySaver(), Recorder()
    result = start(recorder, saver)
    assert recorder.analyzed == [(5, "thread-1")]
    assert result["__interrupt__"][0].value == {"run_id": 7, "suggestion_id": 3}
    snapshot = build_graph(saver).get_state(CONFIG)
    assert snapshot.next == (REVIEW_NODE,)
    assert set(snapshot.values) == {
        "ticket_id",
        "thread_id",
        "outcome",
        "run_id",
        "suggestion_id",
    }
    assert recorder.reviews == []


@pytest.mark.parametrize(
    ("decision", "outcome", "message_id"),
    [("approve", "delivered", 11), ("reject", "rejected", None)],
)
def test_resume_routes_decision_and_finishes(decision, outcome, message_id) -> None:
    """Решение ведёт в deliver или reject; граф завершается без новых остановок."""
    saver, recorder = InMemorySaver(), Recorder()
    start(recorder, saver)
    # Новый объект графа с тем же хранилищем — как после перезапуска API.
    state = build_graph(saver).invoke(
        Command(resume=review(decision)), CONFIG, context=recorder.context()
    )
    assert state["outcome"] == outcome
    assert state["message_id"] == message_id
    assert state["applied_review_id"] == "r-1"
    assert recorder.reviews == [(3, review(decision))]
    assert recorder.analyzed == [(5, "thread-1")]
    assert build_graph(saver).get_state(CONFIG).next == ()


def test_failed_analysis_ends_without_waiting_for_review() -> None:
    """Неудачный анализ не создаёт ожидания: проверять нечего."""
    saver = InMemorySaver()
    recorder = Recorder({"outcome": "failed", "run_id": 7, "error": {}})
    result = start(recorder, saver)
    assert "__interrupt__" not in result
    assert result["outcome"] == "failed"
    assert build_graph(saver).get_state(CONFIG).next == ()


def test_resuming_finished_thread_does_not_apply_twice() -> None:
    """Повторный resume завершённого потока не вызывает сохранение решения."""
    saver, recorder = InMemorySaver(), Recorder()
    start(recorder, saver)
    graph = build_graph(saver)
    graph.invoke(Command(resume=review()), CONFIG, context=recorder.context())
    state = graph.invoke(
        Command(resume=review("reject", "r-2")), CONFIG, context=recorder.context()
    )
    assert len(recorder.reviews) == 1
    # Сервис сравнивает applied_review_id и отвечает 409 второму решению.
    assert state["applied_review_id"] == "r-1"


def test_start_analysis_keeps_license_hint_out_of_checkpoint(monkeypatch) -> None:
    """Ключ лицензии доходит до агента, но не сохраняется в состоянии графа."""
    saver = InMemorySaver()
    run = Mock(id=7, suggestion=Mock(id=3))
    analyze = Mock(return_value=run)
    monkeypatch.setattr(agents, "analyze_ticket", analyze)
    session = Mock(spec=Session)
    result = workflow.start_analysis(
        session,
        5,
        AnalyzeRequest(license_key="LIC-SECRET"),
        Mock(),
        None,
        lambda: saver,
    )
    assert result is run
    assert analyze.call_args.args[2].license_key == "LIC-SECRET"
    thread_id = analyze.call_args.kwargs["workflow_thread_id"]
    snapshot = build_graph(saver).get_state({"configurable": {"thread_id": thread_id}})
    assert snapshot.next == (REVIEW_NODE,)
    assert "LIC-SECRET" not in repr(list(saver.storage.items()))


def test_error_before_run_deletes_empty_thread(monkeypatch) -> None:
    """Ошибка до создания запуска (нет обращения) не оставляет поток в checkpoint."""
    saver = InMemorySaver()
    monkeypatch.setattr(
        agents,
        "analyze_ticket",
        Mock(side_effect=ResourceNotFound("Обращение не найдено")),
    )
    with pytest.raises(ResourceNotFound):
        workflow.start_analysis(
            Mock(spec=Session), 5, AnalyzeRequest(), Mock(), None, lambda: saver
        )
    assert not saver.storage


def test_failed_run_keeps_thread_and_raises_with_run_id(monkeypatch) -> None:
    """Сохранённый неудачный запуск возвращает run_id; поток помечен failed."""
    saver = InMemorySaver()
    monkeypatch.setattr(
        agents,
        "analyze_ticket",
        Mock(side_effect=AgentRunFailure.of(LLMTimeoutError(), 12)),
    )
    with pytest.raises(AgentRunFailure) as error:
        workflow.start_analysis(
            Mock(spec=Session), 5, AnalyzeRequest(), Mock(), None, lambda: saver
        )
    assert (error.value.status_code, error.value.run_id) == (504, 12)
    assert error.value.detail == LLMTimeoutError.detail
    assert saver.storage


def migration_module():
    """Загрузить файл миграции 0004 как модуль, не выполняя её."""
    path = PROJECT_ROOT / "migrations/versions/0004_review_workflow.py"
    spec = importlib.util.spec_from_file_location("migration_0004", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def normalize(sql: str) -> str:
    """Сравнивать SQL без пробелов и без CONCURRENTLY (миграция идёт в транзакции)."""
    return " ".join(sql.replace("CONCURRENTLY ", "").split())


def test_frozen_checkpoint_ddl_matches_installed_library() -> None:
    """Обновление langgraph-checkpoint-postgres с новой схемой не пройдёт незаметно."""
    frozen = migration_module().CHECKPOINT_DDL
    library = BasePostgresSaver.MIGRATIONS
    assert [normalize(item) for item in frozen] == [normalize(item) for item in library]
    assert not any(re.search(r"\bCONCURRENTLY\b", item) for item in frozen)


def test_conninfo_converts_sqlalchemy_url_for_psycopg() -> None:
    """Пул checkpointer получает адрес libpq без драйвера SQLAlchemy."""
    assert (
        conninfo("postgresql+psycopg://user:p%40ss@db:5432/app")
        == "postgresql://user:p%40ss@db:5432/app"
    )
