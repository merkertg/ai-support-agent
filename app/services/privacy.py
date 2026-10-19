"""Обезличивание персональных данных: по запросу клиента и по сроку хранения.

Где в БД лежат персональные данные и что с ними происходит:

- customers: имя, email и компания заменяются заглушками, ставится erased_at;
- tickets: тема и описание заменяются, обращение закрывается и получает
  anonymized_at и причину; статус, приоритет, продукт и даты остаются для
  статистики панели;
- ticket_messages: текст каждого сообщения, включая доставленный ответ;
- agent_runs: аргументы и результаты инструментов (там были тема, описание,
  имя и email) и текст итога; категория, источники и счётчики остаются;
- agent_suggestions: черновик, одобренный текст и комментарий решения;
- checkpoint_* LangGraph: в состоянии потока хранится решение с текстом,
  поэтому потоки удаляются целиком. GET /workflow позже восстановит итог
  потока по фактам БД (id и исход), уже без текстов.

Лицензии остаются у обезличенного клиента: это учёт продукта, а ключ
лицензии без имени и email не указывает на человека. Qdrant хранит только
статьи базы знаний, журнал API — id и пути запросов без текстов обращений.
Резервные копии, сделанные до обезличивания, по-прежнему содержат данные.
"""

import logging
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import get_args

from sqlalchemy import case, func, select, text, update
from sqlalchemy.orm import Session

from app.models import (
    AgentRun,
    AgentSuggestion,
    Customer,
    License,
    Ticket,
    TicketMessage,
)
from app.observability import get_logger, log_event
from app.schemas.drafts import DraftCategory
from app.schemas.privacy import (
    AnonymizationCounts,
    ErasureReport,
    RetentionReport,
    RetentionSkip,
)
from app.schemas.resources import CLOSED_TICKET_STATUSES, AnonymizationReason
from app.services.errors import ResourceConflict, ResourceInvalid, ResourceNotFound
from app.services.run_recovery import (
    log_recovered_runs,
    recover_stale_runs,
    stale_cutoff,
)

ERASED_TEXT = "[удалено]"
ERASED_CUSTOMER_NAME = "Клиент удалён"
ANONYMIZED_SUBJECT = "Обращение обезличено"
ALREADY_ERASED = "Клиент уже обезличен"
CONFIRMATION_MISMATCH = "Email подтверждения не совпадает с email клиента"
# Минимальный срок защищает от опечатки вроде --older-than-days 3.
MIN_RETENTION_DAYS = 30
MAX_RETENTION_BATCH = 1000
CATEGORIES = frozenset(get_args(DraftCategory))
# Таблицы PostgresSaver (миграция 0004): writes и blobs ссылаются на поток.
DELETE_CHECKPOINTS = tuple(
    text(f"DELETE FROM {table} WHERE thread_id = ANY(:threads)")
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints")
)
logger = get_logger("privacy")


def erased_email(customer_id: int) -> str:
    """Уникальная заглушка email: домен .invalid зарезервирован и не доставляется."""
    return f"erased-{customer_id}@erased.invalid"


def scrub_trace(trace: object) -> list[dict]:
    """Оставить в трассе только имена вызовов: аргументы и ответы содержали текст."""
    if not isinstance(trace, list):
        return []
    return [
        {
            "call_id": str(item.get("call_id") or ""),
            "name": str(item.get("name") or ""),
            "arguments": {},
            "result": {"erased": True},
        }
        for item in trace
        if isinstance(item, dict)
    ]


def scrub_result(result: object) -> dict | None:
    """Удалить текст итога, сохранив категорию и ключи статей базы знаний."""
    if not isinstance(result, dict):
        return None
    category = result.get("category")
    sources = result.get("sources")
    return {
        "summary": ERASED_TEXT,
        "category": category if category in CATEGORIES else "other",
        "draft_reply": ERASED_TEXT,
        "sources": [key for key in sources if isinstance(key, str)]
        if isinstance(sources, list)
        else [],
    }


def ticket_blockers(session: Session, ticket_ids: Sequence[int]) -> dict[int, str]:
    """Найти обращения с незавершённой работой, которую нельзя обезличить.

    Активный анализ ещё запишет итог с текстом, а pending-предложение ждёт
    решения сотрудника. Брошенные running старше порога восстановления не
    считаются: вызывающая сторона завершает их через recover_stale_runs.
    """
    if not ticket_ids:
        return {}
    blockers: dict[int, str] = {}
    pending = session.execute(
        select(AgentSuggestion.id, AgentRun.ticket_id)
        .join(AgentRun, AgentRun.id == AgentSuggestion.agent_run_id)
        .where(AgentRun.ticket_id.in_(ticket_ids), AgentSuggestion.status == "pending")
        .order_by(AgentSuggestion.id)
    )
    for suggestion_id, ticket_id in pending:
        blockers.setdefault(
            ticket_id,
            f"предложение {suggestion_id} ждёт решения: одобрите или отклоните его",
        )
    running = session.execute(
        select(AgentRun.id, AgentRun.ticket_id)
        .where(
            AgentRun.ticket_id.in_(ticket_ids),
            AgentRun.status.in_(("pending", "running")),
            AgentRun.started_at > stale_cutoff(),
        )
        .order_by(AgentRun.id)
    )
    for run_id, ticket_id in running:
        blockers.setdefault(ticket_id, f"выполняется анализ (запуск {run_id})")
    return blockers


def count_ticket_data(session: Session, ticket_ids: Sequence[int]) -> dict:
    """Посчитать, сколько записей затронет обезличивание: для пробного плана."""
    if not ticket_ids:
        return AnonymizationCounts().model_dump()
    messages = session.scalar(
        select(func.count(TicketMessage.id)).where(
            TicketMessage.ticket_id.in_(ticket_ids)
        )
    )
    runs, threads = session.execute(
        select(func.count(AgentRun.id), func.count(AgentRun.workflow_thread_id)).where(
            AgentRun.ticket_id.in_(ticket_ids)
        )
    ).one()
    suggestions = session.scalar(
        select(func.count(AgentSuggestion.id))
        .join(AgentRun, AgentRun.id == AgentSuggestion.agent_run_id)
        .where(AgentRun.ticket_id.in_(ticket_ids))
    )
    return AnonymizationCounts(
        tickets=len(ticket_ids),
        messages=messages,
        agent_runs=runs,
        suggestions=suggestions,
        workflow_threads=threads,
    ).model_dump()


def anonymize_tickets(
    session: Session,
    tickets: Iterable[Ticket],
    reason: AnonymizationReason,
    now: datetime,
) -> AnonymizationCounts:
    """Удалить тексты обращений и всего, что из них получено, в текущей транзакции.

    Вызывающая сторона заблокировала строки обращений (FOR UPDATE) и проверила
    ticket_blockers: новый анализ не начнётся, пока транзакция не завершится,
    а после неё увидит закрытое обезличенное обращение.
    """
    targets = [ticket for ticket in tickets if ticket.anonymized_at is None]
    ticket_ids = [ticket.id for ticket in targets]
    if not ticket_ids:
        return AnonymizationCounts()
    messages = session.execute(
        update(TicketMessage)
        .where(TicketMessage.ticket_id.in_(ticket_ids))
        .values(content=ERASED_TEXT)
    ).rowcount
    runs = list(
        session.scalars(
            select(AgentRun)
            .where(AgentRun.ticket_id.in_(ticket_ids))
            .order_by(AgentRun.id)
            .with_for_update()
        )
    )
    for run in runs:
        run.tool_trace = scrub_trace(run.tool_trace)
        run.result = scrub_result(run.result)
    run_ids = [run.id for run in runs]
    suggestions = 0
    if run_ids:
        suggestions = session.execute(
            update(AgentSuggestion)
            .where(AgentSuggestion.agent_run_id.in_(run_ids))
            .values(
                content=ERASED_TEXT,
                final_content=case(
                    (AgentSuggestion.final_content.is_(None), None),
                    else_=ERASED_TEXT,
                ),
                review_comment=case(
                    (AgentSuggestion.review_comment.is_(None), None),
                    else_=ERASED_TEXT,
                ),
            )
        ).rowcount
    threads = sorted({run.workflow_thread_id for run in runs if run.workflow_thread_id})
    if threads:
        for statement in DELETE_CHECKPOINTS:
            session.execute(statement, {"threads": threads})
    for ticket in targets:
        ticket.subject = ANONYMIZED_SUBJECT
        ticket.description = ERASED_TEXT
        if ticket.status not in CLOSED_TICKET_STATUSES:
            ticket.status = "closed"
        ticket.anonymized_at = now
        ticket.anonymized_reason = reason
    session.flush()
    return AnonymizationCounts(
        tickets=len(ticket_ids),
        messages=messages,
        agent_runs=len(runs),
        suggestions=suggestions,
        workflow_threads=len(threads),
    )


def erase_customer(
    session: Session, customer_id: int, confirm_email: str
) -> ErasureReport:
    """Обезличить клиента и все его обращения одной транзакцией.

    Клиент и его обращения блокируются FOR UPDATE: создание обращения или
    лицензии берёт FOR SHARE клиента, а анализ — FOR UPDATE обращения, поэтому
    параллельный запрос либо завершится до обезличивания и будет обезличен,
    либо после него увидит отметку и получит 409.
    """
    recovered: list[tuple[int, int]] = []
    with session.begin():
        customer = session.scalar(
            select(Customer)
            .where(Customer.id == customer_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if customer is None:
            raise ResourceNotFound("Клиент не найден")
        if customer.erased_at is not None:
            raise ResourceConflict(ALREADY_ERASED)
        if confirm_email.strip().casefold() != customer.email.casefold():
            raise ResourceInvalid(CONFIRMATION_MISMATCH)
        tickets = list(
            session.scalars(
                select(Ticket)
                .where(Ticket.customer_id == customer_id)
                .order_by(Ticket.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        for ticket in tickets:
            recovered += recover_stale_runs(session, ticket_id=ticket.id)
        blockers = ticket_blockers(session, [ticket.id for ticket in tickets])
        if blockers:
            ticket_id, reason = min(blockers.items())
            raise ResourceConflict(
                f"Нельзя обезличить клиента: у обращения {ticket_id} {reason}"
            )
        now = datetime.now(UTC)
        counts = anonymize_tickets(session, tickets, "customer_request", now)
        customer.name = ERASED_CUSTOMER_NAME
        customer.email = erased_email(customer.id)
        customer.company = None
        customer.erased_at = now
        licenses = session.scalar(
            select(func.count(License.id)).where(License.customer_id == customer_id)
        )
    log_recovered_runs(recovered)
    report = ErasureReport(
        **counts.model_dump(),
        customer_id=customer_id,
        erased_at=now,
        licenses_kept=licenses,
    )
    log_event(
        logger,
        logging.INFO,
        "customer_erased",
        customer_id=customer_id,
        **counts.model_dump(),
    )
    return report


def apply_retention(
    session: Session,
    older_than_days: int,
    *,
    apply: bool = False,
    limit: int = 500,
    now: datetime | None = None,
) -> RetentionReport:
    """Обезличить закрытые обращения, не менявшиеся дольше срока хранения.

    По умолчанию это только план: ничего не меняется и не блокируется.
    С apply=True строки берутся FOR UPDATE SKIP LOCKED: обращение, с которым
    сейчас работают, будет обработано следующим запуском. Клиенты не меняются:
    их данные удаляет только явный запрос (erase_customer).
    """
    if older_than_days < MIN_RETENTION_DAYS:
        raise ResourceInvalid(
            f"Срок хранения должен быть не меньше {MIN_RETENTION_DAYS} дней"
        )
    if not 1 <= limit <= MAX_RETENTION_BATCH:
        raise ResourceInvalid(f"Размер пачки должен быть от 1 до {MAX_RETENTION_BATCH}")
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=older_than_days)
    statement = (
        select(Ticket)
        .where(
            Ticket.status.in_(sorted(CLOSED_TICKET_STATUSES)),
            Ticket.anonymized_at.is_(None),
            Ticket.updated_at < cutoff,
        )
        .order_by(Ticket.id)
        .limit(limit)
    )
    if apply:
        statement = statement.with_for_update(skip_locked=True)
    recovered: list[tuple[int, int]] = []
    try:
        tickets = list(
            session.scalars(statement.execution_options(populate_existing=True))
        )
        if apply:
            for ticket in tickets:
                recovered += recover_stale_runs(session, ticket_id=ticket.id)
        blockers = ticket_blockers(session, [ticket.id for ticket in tickets])
        eligible = [ticket for ticket in tickets if ticket.id not in blockers]
        eligible_ids = [ticket.id for ticket in eligible]
        if apply:
            counts = anonymize_tickets(session, eligible, "retention", moment)
            data = counts.model_dump()
            session.commit()
        else:
            data = count_ticket_data(session, eligible_ids)
            # План ничего не меняет: чтение завершается без commit.
            session.rollback()
    except Exception:
        session.rollback()
        raise
    log_recovered_runs(recovered)
    report = RetentionReport(
        **data,
        applied=apply,
        older_than_days=older_than_days,
        cutoff=cutoff,
        ticket_ids=eligible_ids,
        skipped=[
            RetentionSkip(ticket_id=ticket_id, reason=reason)
            for ticket_id, reason in sorted(blockers.items())
        ],
    )
    if apply:
        log_event(
            logger,
            logging.INFO,
            "tickets_retention_applied",
            older_than_days=older_than_days,
            skipped=len(report.skipped),
            **data,
        )
    return report
