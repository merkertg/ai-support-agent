"""PostgreSQL: восстановление брошенных запусков, граница времени и гонки."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.agent.config import AgentSettings
from app.agent.contracts import AgentProgress
from app.agent.provider import MockAgentProvider
from app.models import (
    AgentRun,
    AgentSuggestion,
    Customer,
    Product,
    Ticket,
    TicketMessage,
)
from app.schemas.agents import AnalyzeRequest
from app.schemas.drafts import AgentDraftContent
from app.services import agents
from app.services import run_recovery as recovery
from app.services.errors import ResourceConflict

pytestmark = pytest.mark.integration


@pytest.fixture
def recovery_scope(migrated_engine: Engine) -> tuple[int, int]:
    """Создать два обращения только в проверенной базе TEST_DATABASE_URL *_test."""
    with Session(migrated_engine) as session, session.begin():
        customer = Customer(name="Recovery", email="recovery@example.test")
        product = Product(name="Recovery", code="recovery-product")
        session.add_all([customer, product])
        session.flush()
        tickets = [
            Ticket(
                customer_id=customer.id,
                product_id=product.id,
                subject=f"Обращение {number}",
                description="Проверка восстановления",
            )
            for number in range(2)
        ]
        session.add_all(tickets)
        session.flush()
        return tickets[0].id, tickets[1].id


def create_run(
    session: Session, ticket_id: int, age: float, *, status: str = "running"
) -> AgentRun:
    """Задать возраст от PostgreSQL now() в той же транзакции, что recovery."""
    run = AgentRun(
        ticket_id=ticket_id,
        status=status,
        provider="mock",
        model="mock-agent-v1",
        started_at=func.now() - timedelta(seconds=age),
        steps=2,
        tool_trace=[{"name": "get_ticket", "result": {"ok": True}}],
    )
    session.add(run)
    session.flush()
    return run


def reply() -> AgentDraftContent:
    """Вернуть валидный локальный черновик без запроса к провайдеру."""
    return AgentDraftContent(
        summary="Восстановление", category="other", draft_reply="Проверено", sources=[]
    )


def count(session: Session, model) -> int:
    """Сосчитать побочные эффекты recovery в настоящей PostgreSQL."""
    return session.scalar(select(func.count()).select_from(model))


def test_recovery_exact_boundary_and_terminal_states_are_preserved(
    migrated_engine: Engine, recovery_scope: tuple[int, int]
) -> None:
    """180s включительно завершается; 179.999s и все конечные статусы остаются."""
    with Session(migrated_engine) as session, session.begin():
        fresh = create_run(session, recovery_scope[0], 179.999)
        boundary = create_run(session, recovery_scope[0], 180)
        older = create_run(session, recovery_scope[0], 181)
        terminal = [
            create_run(session, recovery_scope[0], 3600, status=status)
            for status in ("pending", "completed", "failed")
        ]
        pending = AgentSuggestion(
            agent_run_id=terminal[1].id,
            suggestion_type="reply",
            status="pending",
            content="Существующий черновик",
        )
        session.add(pending)
        session.flush()
        changed = recovery.recover_stale_runs(session)
        assert set(changed) == {
            (boundary.id, recovery_scope[0]),
            (older.id, recovery_scope[0]),
        }
        session.expire_all()
        assert fresh.status == "running"
        for run in (boundary, older):
            assert run.status == "failed"
            assert run.error_message == recovery.RECOVERY_ERROR
            assert run.finished_at >= run.started_at
            assert run.result is None
            assert run.steps == 2 and run.tool_trace[0]["name"] == "get_ticket"
        assert [run.status for run in terminal] == ["pending", "completed", "failed"]
        assert pending.status == "pending"
        assert count(session, AgentSuggestion) == 1
        assert count(session, TicketMessage) == 0


def test_scoped_recovery_is_idempotent_and_releases_connections(
    migrated_engine: Engine, recovery_scope: tuple[int, int]
) -> None:
    """Recovery перед анализом не меняет брошенный запуск соседнего обращения."""
    with Session(migrated_engine) as session, session.begin():
        first = create_run(session, recovery_scope[0], 3600)
        second = create_run(session, recovery_scope[1], 3600)
        first_id, second_id = first.id, second.id
    with Session(migrated_engine) as session, session.begin():
        assert recovery.recover_stale_runs(session, ticket_id=recovery_scope[0]) == [
            (first_id, recovery_scope[0])
        ]
        assert recovery.recover_stale_runs(session, ticket_id=recovery_scope[0]) == []
    with Session(migrated_engine) as session:
        assert session.get(AgentRun, first_id).status == "failed"
        assert session.get(AgentRun, second_id).status == "running"
        assert count(session, AgentSuggestion) == count(session, TicketMessage) == 0
    assert migrated_engine.pool.checkedout() == 0


def test_recovered_worker_cannot_later_create_a_pending_reply(
    migrated_engine: Engine, recovery_scope: tuple[int, int]
) -> None:
    """Поздний результат старого процесса не оживляет failed и не отправляется."""
    with Session(migrated_engine) as session, session.begin():
        run_id = create_run(session, recovery_scope[0], 3600).id
        recovery.recover_stale_runs(session)
    with Session(migrated_engine) as session:
        with pytest.raises(agents.AgentRunFailure) as failure:
            agents.finish_run(session, run_id, AgentProgress(steps=5), result=reply())
        assert failure.value.status_code == 409
    with Session(migrated_engine) as session:
        run = session.get(AgentRun, run_id)
        assert run.status == "failed" and run.steps == 2
        assert run.error_message == recovery.RECOVERY_ERROR
        assert count(session, AgentSuggestion) == count(session, TicketMessage) == 0


def test_completed_result_wins_over_later_recovery(
    migrated_engine: Engine, recovery_scope: tuple[int, int]
) -> None:
    """Успешное атомарное завершение сохраняет pending даже при старом started_at."""
    with Session(migrated_engine) as session, session.begin():
        run_id = create_run(session, recovery_scope[0], 3600).id
    with Session(migrated_engine) as session:
        agents.finish_run(session, run_id, AgentProgress(steps=3), result=reply())
    with Session(migrated_engine) as session, session.begin():
        assert recovery.recover_stale_runs(session) == []
        run = session.get(AgentRun, run_id)
        assert run.status == "completed" and run.result["draft_reply"] == "Проверено"
        assert count(session, AgentSuggestion) == 1
        assert count(session, TicketMessage) == 0


def test_update_waiting_for_a_finishing_worker_rechecks_running_status(
    migrated_engine: Engine, recovery_scope: tuple[int, int]
) -> None:
    """PostgreSQL повторяет WHERE после снятия блокировки и сохраняет completed."""
    with Session(migrated_engine) as session, session.begin():
        run_id = create_run(session, recovery_scope[0], 3600).id
    pid_ready = threading.Event()
    backend_pid: list[int] = []

    def recover_in_parallel() -> list[tuple[int, int]]:
        """Условный UPDATE начинает ждать строки, которую завершает другой worker."""
        with Session(migrated_engine) as session, session.begin():
            backend_pid.append(session.scalar(select(func.pg_backend_pid())))
            pid_ready.set()
            return recovery.recover_stale_runs(session)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(migrated_engine) as winner, winner.begin():
            run = winner.get(AgentRun, run_id, with_for_update=True)
            run.status = "completed"
            run.finished_at = func.now()
            run.result = reply().model_dump()
            winner.flush()
            future = pool.submit(recover_in_parallel)
            assert pid_ready.wait(timeout=2)
            # Проверяем реальное ожидание PostgreSQL, а не задержку планировщика.
            deadline = time.monotonic() + 0.8
            blocked = False
            while time.monotonic() < deadline:
                with migrated_engine.connect() as observer:
                    blocked = bool(
                        observer.exec_driver_sql(
                            "SELECT wait_event_type = 'Lock' FROM pg_stat_activity "
                            "WHERE pid = %s",
                            (backend_pid[0],),
                        ).scalar()
                    )
                if blocked:
                    break
                time.sleep(0.01)
            assert blocked, "Параллельный UPDATE не дошёл до ожидания блокировки"
        assert future.result(timeout=3) == []
    with Session(migrated_engine) as session:
        assert session.get(AgentRun, run_id).status == "completed"


def test_reduced_timeout_does_not_unlock_a_still_live_run(
    migrated_engine: Engine,
    recovery_scope: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """После смены 120s на 1s возраст 150s ещё не даёт повторно вызвать модель."""
    monkeypatch.setattr(
        agents,
        "get_agent_settings",
        lambda: AgentSettings(agent_timeout_seconds=1, _env_file=None),
    )
    with Session(migrated_engine) as session, session.begin():
        create_run(session, recovery_scope[0], 150)
    provider = Mock()
    with Session(migrated_engine) as session:
        with pytest.raises(ResourceConflict, match="уже выполняется"):
            agents.analyze_ticket(
                session, recovery_scope[0], AnalyzeRequest(), provider, None
            )
    provider.assert_not_called()


def test_new_analysis_finalizes_old_run_and_creates_only_one_new_pending(
    migrated_engine: Engine,
    recovery_scope: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Повтор анализа закрывает брошенный running в транзакции нового запуска."""
    monkeypatch.setattr(
        agents, "get_agent_settings", lambda: AgentSettings(_env_file=None)
    )
    with Session(migrated_engine) as session, session.begin():
        old_id = create_run(session, recovery_scope[0], 3600).id
    with Session(migrated_engine) as session:
        result = agents.analyze_ticket(
            session,
            recovery_scope[0],
            AnalyzeRequest(),
            MockAgentProvider,
            None,
        )
        assert result.status == "completed" and result.suggestion.status == "pending"
    with Session(migrated_engine) as session:
        assert session.get(AgentRun, old_id).status == "failed"
        assert count(session, AgentRun) == 2
        assert count(session, AgentSuggestion) == 1
        assert count(session, TicketMessage) == 0


def test_background_pass_uses_configured_engine_without_waiting_for_new_analysis(
    migrated_engine: Engine,
    recovery_scope: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Таймер может закрыть брошенный запуск, к которому никто больше не обращался."""
    with Session(migrated_engine) as session, session.begin():
        run_id = create_run(session, recovery_scope[0], 3600).id
    monkeypatch.setattr(recovery, "get_engine", lambda: migrated_engine)
    recovery.recover_once()
    recovery.recover_once()
    with Session(migrated_engine) as session:
        assert session.get(AgentRun, run_id).status == "failed"
        assert count(session, AgentSuggestion) == count(session, TicketMessage) == 0
    assert migrated_engine.pool.checkedout() == 0
