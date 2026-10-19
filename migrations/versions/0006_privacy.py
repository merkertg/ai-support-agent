"""Добавляет отметки обезличивания клиентов и обращений.

Revision ID: 0006_privacy
Revises: 0005_api_tokens

customers.erased_at фиксирует, что персональные данные клиента заменены по его
запросу. tickets.anonymized_at и anonymized_reason показывают, когда и почему
удалены тексты обращения: по запросу клиента или по сроку хранения. Сами
тексты миграция не меняет: обезличивание выполняет сервис app.services.privacy.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_privacy"
down_revision: str | Sequence[str] | None = "0005_api_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавить отметки времени и причины; существующие записи остаются NULL."""
    op.add_column(
        "customers",
        sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tickets",
        sa.Column("anonymized_reason", sa.String(length=20), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_tickets_anonymized_reason"),
        "tickets",
        "anonymized_reason IN ('customer_request', 'retention')",
    )
    op.create_check_constraint(
        op.f("ck_tickets_anonymization"),
        "tickets",
        "(anonymized_at IS NULL) = (anonymized_reason IS NULL)",
    )


def downgrade() -> None:
    """Удалить отметки; уже удалённые тексты откат схемы не возвращает."""
    op.drop_constraint(op.f("ck_tickets_anonymization"), "tickets", type_="check")
    op.drop_constraint(op.f("ck_tickets_anonymized_reason"), "tickets", type_="check")
    op.drop_column("tickets", "anonymized_reason")
    op.drop_column("tickets", "anonymized_at")
    op.drop_column("customers", "erased_at")
