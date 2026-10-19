"""Добавляет API-токены сотрудников и роль reviewer.

Revision ID: 0005_api_tokens
Revises: 0004_review_workflow

Таблица api_tokens хранит только SHA-256 токена: сам секрет показывается один
раз при выдаче. Роли образуют лестницу operator < reviewer < admin: решение по
предложению агента принимает reviewer или admin. Существующие сотрудники
сохраняют свою роль; токены им выдаёт администратор после обновления.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_api_tokens"
down_revision: str | Sequence[str] | None = "0004_review_workflow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Расширить роли сотрудников и создать таблицу хешей токенов."""
    op.drop_constraint(op.f("ck_users_role"), "users", type_="check")
    op.create_check_constraint(
        op.f("ck_users_role"), "users", "role IN ('operator', 'reviewer', 'admin')"
    )
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_prefix", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_api_tokens_expiry")
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name=op.f("ck_api_tokens_revocation"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_api_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_api_tokens_token_hash")),
    )
    op.create_index(op.f("ix_api_tokens_user_id"), "api_tokens", ["user_id"])


def downgrade() -> None:
    """Удалить токены; reviewer становится operator, чтобы прежний CHECK прошёл.

    Понижение роли — осознанная потеря данных отката: схема 0004 не знает
    reviewer, а выдать такому сотруднику права admin было бы небезопасно.
    """
    op.drop_index(op.f("ix_api_tokens_user_id"), table_name="api_tokens")
    op.drop_table("api_tokens")
    op.drop_constraint(op.f("ck_users_role"), "users", type_="check")
    op.execute("UPDATE users SET role = 'operator' WHERE role = 'reviewer'")
    op.create_check_constraint(
        op.f("ck_users_role"), "users", "role IN ('operator', 'admin')"
    )
