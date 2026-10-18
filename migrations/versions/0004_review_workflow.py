"""Добавляет решение сотрудника по предложению и таблицы checkpointer LangGraph.

Revision ID: 0004_review_workflow
Revises: 0003_knowledge_index

Таблицы checkpoint_* — замороженная копия DDL langgraph-checkpoint-postgres
3.1.2 (BasePostgresSaver.MIGRATIONS, версии 0–9). Библиотека сама создаёт их
методом setup(), но схему проекта меняет только Alembic. Индексы создаются
без CONCURRENTLY: миграция выполняется в транзакции, а таблицы ещё пусты.
Записанные версии делают последующий setup() пустой операцией. Тест сверяет
эту копию с DDL установленной версии библиотеки.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_review_workflow"
down_revision: str | Sequence[str] | None = "0003_knowledge_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECKPOINT_DDL = (
    "CREATE TABLE IF NOT EXISTS checkpoint_migrations (\n    v INTEGER PRIMARY KEY\n);",
    """CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    type TEXT,
    checkpoint JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);""",
    """CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    version TEXT NOT NULL,
    type TEXT NOT NULL,
    blob BYTEA,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);""",
    """CREATE TABLE IF NOT EXISTS checkpoint_writes (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    channel TEXT NOT NULL,
    type TEXT,
    blob BYTEA NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);""",
    "ALTER TABLE checkpoint_blobs ALTER COLUMN blob DROP not null;",
    "SELECT 1;",
    "CREATE INDEX IF NOT EXISTS checkpoints_thread_id_idx ON checkpoints(thread_id);",
    "CREATE INDEX IF NOT EXISTS checkpoint_blobs_thread_id_idx "
    "ON checkpoint_blobs(thread_id);",
    "CREATE INDEX IF NOT EXISTS checkpoint_writes_thread_id_idx "
    "ON checkpoint_writes(thread_id);",
    "ALTER TABLE checkpoint_writes ADD COLUMN IF NOT EXISTS "
    "task_path TEXT NOT NULL DEFAULT '';",
)
CHECKPOINT_TABLES = (
    "checkpoint_writes",
    "checkpoint_blobs",
    "checkpoints",
    "checkpoint_migrations",
)


def upgrade() -> None:
    """Добавить поля проверки, ссылку на доставленное сообщение и checkpoint_*."""
    op.add_column(
        "agent_runs", sa.Column("workflow_thread_id", sa.String(64), nullable=True)
    )
    op.create_unique_constraint(
        op.f("uq_agent_runs_workflow_thread_id"), "agent_runs", ["workflow_thread_id"]
    )
    op.add_column("agent_suggestions", sa.Column("final_content", sa.Text()))
    op.add_column("agent_suggestions", sa.Column("review_comment", sa.Text()))
    op.add_column(
        "agent_suggestions", sa.Column("delivered_message_id", sa.BigInteger())
    )
    op.create_foreign_key(
        op.f("fk_agent_suggestions_delivered_message_id_ticket_messages"),
        "agent_suggestions",
        "ticket_messages",
        ["delivered_message_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        op.f("uq_agent_suggestions_delivered_message_id"),
        "agent_suggestions",
        ["delivered_message_id"],
    )
    op.create_check_constraint(
        op.f("ck_agent_suggestions_final_content"),
        "agent_suggestions",
        "final_content IS NULL OR status = 'approved'",
    )
    op.create_check_constraint(
        op.f("ck_agent_suggestions_delivery"),
        "agent_suggestions",
        "delivered_message_id IS NULL OR status = 'approved'",
    )
    for statement in CHECKPOINT_DDL:
        op.execute(statement)
    # Версии 0–9 отмечены применёнными, как это сделал бы PostgresSaver.setup().
    op.execute(
        "INSERT INTO checkpoint_migrations (v) "
        f"SELECT generate_series(0, {len(CHECKPOINT_DDL) - 1}) "
        "ON CONFLICT (v) DO NOTHING"
    )


def downgrade() -> None:
    """Удалить поля этапа 07 и состояния графов; решения в них теряются."""
    for table in CHECKPOINT_TABLES:
        op.drop_table(table)
    op.drop_constraint(
        op.f("ck_agent_suggestions_delivery"), "agent_suggestions", type_="check"
    )
    op.drop_constraint(
        op.f("ck_agent_suggestions_final_content"), "agent_suggestions", type_="check"
    )
    op.drop_constraint(
        op.f("uq_agent_suggestions_delivered_message_id"),
        "agent_suggestions",
        type_="unique",
    )
    op.drop_constraint(
        op.f("fk_agent_suggestions_delivered_message_id_ticket_messages"),
        "agent_suggestions",
        type_="foreignkey",
    )
    for name in ("delivered_message_id", "review_comment", "final_content"):
        op.drop_column("agent_suggestions", name)
    op.drop_constraint(
        op.f("uq_agent_runs_workflow_thread_id"), "agent_runs", type_="unique"
    )
    op.drop_column("agent_runs", "workflow_thread_id")
