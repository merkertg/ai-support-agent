"""Добавляет наблюдаемый прогресс запуска агента без изменения девяти таблиц.

Revision ID: 0002_agent_runtime
Revises: 0001_stage02_schema

Старые записи получают provider=mock, steps=0 и пустую трассу. Первая
миграция остаётся неизменной, а upgrade сохраняет все существующие записи.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_agent_runtime"
down_revision: str | Sequence[str] | None = "0001_stage02_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавить поля результата и ограничения, совместимые со старыми запусками."""
    op.add_column(
        "agent_runs",
        sa.Column("provider", sa.String(20), nullable=False, server_default="mock"),
    )
    op.add_column(
        "agent_runs",
        sa.Column("steps", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "agent_runs",
        sa.Column(
            "tool_trace",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column("agent_runs", sa.Column("usage", postgresql.JSONB(none_as_null=True)))
    op.add_column(
        "agent_runs", sa.Column("result", postgresql.JSONB(none_as_null=True))
    )
    op.create_check_constraint(
        op.f("ck_agent_runs_provider"), "agent_runs", "provider IN ('mock', 'openai')"
    )
    op.create_check_constraint(op.f("ck_agent_runs_steps"), "agent_runs", "steps >= 0")
    op.create_check_constraint(
        op.f("ck_agent_runs_tool_trace"),
        "agent_runs",
        "jsonb_typeof(tool_trace) = 'array'",
    )


def downgrade() -> None:
    """Удалить поля этапа 05; их содержимое теряется при явном откате миграции."""
    for name in ("tool_trace", "steps", "provider"):
        op.drop_constraint(op.f(f"ck_agent_runs_{name}"), "agent_runs", type_="check")
    for name in ("result", "usage", "tool_trace", "steps", "provider"):
        op.drop_column("agent_runs", name)
