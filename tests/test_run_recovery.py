"""Порог, SQL-защита и фоновый цикл восстановления без доступа к рабочей БД."""

import asyncio
import json
import logging
import threading
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.agent.config import AgentSettings
from app.agent.contracts import AgentProgress
from app.db.session import DatabaseNotConfigured
from app.llm.errors import LLMTimeoutError
from app.models import AgentRun
from app.schemas.drafts import AgentDraftContent
from app.services import run_recovery as recovery
from app.services.agents import AgentRunFailure, finish_run
from app.services.errors import ResourceNotFound


def test_recovery_window_covers_maximum_timeout_even_after_config_change() -> None:
    """Уменьшение текущего лимита не завершает чужой допустимый 120s-запуск."""
    upper_bounds = [
        item.le
        for item in AgentSettings.model_fields["agent_timeout_seconds"].metadata
        if hasattr(item, "le")
    ]
    assert upper_bounds == [recovery.MAX_AGENT_TIMEOUT_SECONDS]
    assert recovery.STALE_RUNNING_SECONDS == 180
    assert recovery.RECOVERY_INTERVAL_SECONDS == 30


def test_recovery_update_is_conditional_scoped_and_never_creates_suggestions() -> None:
    """Один UPDATE с PostgreSQL-временем защищает completed и чужие тикеты."""
    session = Mock(spec=Session)
    session.execute.return_value = [SimpleNamespace(id=7, ticket_id=3)]
    assert recovery.recover_stale_runs(session, ticket_id=3) == [(7, 3)]
    statement = session.execute.call_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert sql.startswith("UPDATE agent_runs SET")
    assert "agent_runs.status =" in sql
    assert "agent_runs.started_at <= now() -" in sql
    assert "agent_runs.ticket_id =" in sql
    assert "RETURNING agent_runs.id, agent_runs.ticket_id" in sql
    assert "running" in compiled.params.values()
    assert "failed" in compiled.params.values()
    assert timedelta(seconds=180) in compiled.params.values()
    assert recovery.RECOVERY_ERROR in compiled.params.values()
    assert "INSERT" not in sql
    session.add.assert_not_called()
    session.commit.assert_not_called()
    assert [str(call.args[0]) for call in session.execute.call_args_list[:2]] == [
        "SET LOCAL statement_timeout = '3s'",
        "SET LOCAL lock_timeout = '1s'",
    ]


@pytest.mark.parametrize("status", ["completed", "failed", "pending"])
@pytest.mark.parametrize("has_error", [False, True])
def test_late_worker_cannot_replace_a_terminal_run_or_create_pending(
    status: str, has_error: bool
) -> None:
    """Оба исхода позднего worker сохраняют победивший конечный статус."""
    session = MagicMock(spec=Session)
    original = AgentRun(id=7, ticket_id=3, status=status, steps=1)
    session.get.return_value = original
    result = AgentDraftContent(
        summary="Проверено", category="other", draft_reply="Ответ", sources=[]
    )
    with pytest.raises(AgentRunFailure) as failure:
        finish_run(
            session,
            7,
            AgentProgress(steps=5),
            result=result,
            error=LLMTimeoutError() if has_error else None,
        )
    assert failure.value.status_code == 409
    assert failure.value.run_id == 7
    session.get.assert_called_once_with(
        AgentRun, 7, with_for_update=True, populate_existing=True
    )
    assert original.status == status and original.steps == 1
    session.add.assert_not_called()


def test_finishing_a_deleted_run_keeps_the_existing_404() -> None:
    """Блокировка не превращает удалённый запуск в успешный или новый."""
    session = MagicMock(spec=Session)
    session.get.return_value = None
    with pytest.raises(ResourceNotFound):
        finish_run(session, 7, AgentProgress(), error=LLMTimeoutError())
    session.add.assert_not_called()


def test_recovery_commits_before_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Успешное событие отражает уже сохранённое изменение, не попытку UPDATE."""
    session = MagicMock(spec=Session)
    monkeypatch.setattr(recovery, "get_engine", Mock(return_value="engine"))
    session_factory = MagicMock(return_value=session)
    session.__enter__.return_value = session
    monkeypatch.setattr(recovery, "Session", session_factory)
    recover = Mock(return_value=[(7, 3)])
    monkeypatch.setattr(recovery, "recover_stale_runs", recover)

    def verify_commit(recovered) -> None:
        """Проверить закрытие транзакции до публикации успешного события."""
        session.begin.return_value.__exit__.assert_called_once()
        assert recovered == [(7, 3)]

    monkeypatch.setattr(recovery, "log_recovered_runs", verify_commit)
    recovery.recover_once()
    session_factory.assert_called_once_with("engine")
    recover.assert_called_once_with(session)


@pytest.mark.parametrize(
    "error",
    [DatabaseNotConfigured("LIC-SECRET"), SQLAlchemyError("SQL LIC-SECRET password")],
)
def test_recovery_failure_logs_safe_fields_and_can_retry(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """Недоступная БД не убивает монитор и не раскрывает текст драйвера."""
    monkeypatch.setattr(recovery, "get_engine", Mock(side_effect=error))
    log = Mock()
    monkeypatch.setattr(recovery, "log_event", log)
    recovery.recover_once()
    recovery.recover_once()
    assert log.call_count == 2
    assert log.call_args.args[1:3] == (
        logging.WARNING,
        "agent_run_recovery_failed",
    )
    assert "LIC-SECRET" not in json.dumps(log.call_args.kwargs)
    assert "password" not in json.dumps(log.call_args.kwargs)


def test_recovery_event_contains_only_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Предметные данные, ключи и содержимое ответа не входят в recovery-журнал."""
    log = Mock()
    monkeypatch.setattr(recovery, "log_event", log)
    recovery.log_recovered_runs([(7, 3)])
    assert log.call_args.args[2] == "agent_run_recovered"
    assert log.call_args.kwargs == {"run_id": 7, "ticket_id": 3, "status": "failed"}


def test_stopped_loop_never_opens_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Остановка до первого тика обходится без SQL и без ожидания 30 секунд."""
    recover = Mock()
    monkeypatch.setattr(recovery, "recover_once", recover)

    async def scenario() -> None:
        """Выполнить цикл с уже установленным stop-событием."""
        stop = asyncio.Event()
        stop.set()
        await recovery.recovery_loop(stop, interval=30)

    asyncio.run(scenario())
    recover.assert_not_called()


def test_loop_runs_in_worker_and_stops_without_an_extra_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Периодическая проверка не блокирует event loop и уважает shutdown."""

    async def scenario() -> None:
        """Сигнал остановки из worker возвращается в поток event loop безопасно."""
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        def one_pass() -> None:
            """Остановить цикл после одной имитации SQL-проверки."""
            loop.call_soon_threadsafe(stop.set)

        recover = Mock(side_effect=one_pass)
        monkeypatch.setattr(recovery, "recover_once", recover)
        await asyncio.wait_for(recovery.recovery_loop(stop, interval=0.001), timeout=1)
        recover.assert_called_once_with()

    asyncio.run(scenario())


@pytest.mark.parametrize("enabled", [False, True])
def test_monitor_does_not_query_database_on_start_and_cleans_up_task(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    """Контекст стартует/закрывается сразу, а отключённый не создаёт задачу."""
    monkeypatch.setenv("RUN_RECOVERY_ENABLED", str(enabled).lower())
    recover = Mock()
    monkeypatch.setattr(recovery, "recover_once", recover)

    async def scenario() -> None:
        """Дать задаче начать ожидание и проверить отсутствие оставшихся задач."""
        async with recovery.recovery_monitor():
            await asyncio.sleep(0)
            tasks = [
                task
                for task in asyncio.all_tasks()
                if task.get_name() == "agent-run-recovery"
            ]
            assert len(tasks) == int(enabled)
        assert all(task.done() for task in tasks)

    asyncio.run(scenario())
    recover.assert_not_called()


def test_shutdown_waits_for_inflight_sql_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пул можно закрыть лишь после выхода уже выполняющейся SQL-проверки."""
    monkeypatch.setenv("RUN_RECOVERY_ENABLED", "true")
    monkeypatch.setattr(recovery, "RECOVERY_INTERVAL_SECONDS", 0.001)
    release = threading.Event()
    finished = threading.Event()

    async def scenario() -> None:
        """Освободить worker чуть позже выхода из контекста и дождаться его."""
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def work() -> None:
            """Имитировать короткий SQL, который уже начался во время shutdown."""
            loop.call_soon_threadsafe(started.set)
            assert release.wait(timeout=1)
            finished.set()

        monkeypatch.setattr(recovery, "recover_once", work)
        async with recovery.recovery_monitor():
            await asyncio.wait_for(started.wait(), timeout=1)
            loop.call_later(0.01, release.set)
        assert finished.is_set()

    asyncio.run(scenario())
