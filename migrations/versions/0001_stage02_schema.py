"""Создаёт девять таблиц фундамента поддержки.

Revision ID: 0001_stage02_schema
Revises: None

Схема зафиксирована в этой миграции. Изменение классов моделей не меняет
содержимое уже выпущенной миграции: для новых изменений создают следующую.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_stage02_schema"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Создаёт таблицы от независимых справочников к зависимым записям."""
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column(
            "role", sa.String(length=20), server_default="operator", nullable=False
        ),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('operator', 'admin')", name=op.f("ck_users_role")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_table(
        "customers",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("company", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_customers")),
        sa.UniqueConstraint("email", name=op.f("uq_customers_email")),
    )
    op.create_table(
        "products",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
        sa.UniqueConstraint("code", name=op.f("uq_products_code")),
    )
    op.create_table(
        "licenses",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("customer_id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("license_key", sa.String(length=255), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default="active", nullable=False
        ),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'expired', 'revoked')",
            name=op.f("ck_licenses_status"),
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at >= issued_at",
            name=op.f("ck_licenses_dates"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_licenses_customer_id_customers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_licenses_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_licenses")),
        sa.UniqueConstraint("license_key", name=op.f("uq_licenses_license_key")),
    )
    op.create_index(op.f("ix_licenses_customer_id"), "licenses", ["customer_id"])
    op.create_index(op.f("ix_licenses_product_id"), "licenses", ["product_id"])
    op.create_table(
        "tickets",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("customer_id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("subject", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "priority", sa.String(length=20), server_default="medium", nullable=False
        ),
        sa.Column(
            "status", sa.String(length=30), server_default="open", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "priority IN ('low', 'medium', 'high', 'critical')",
            name=op.f("ck_tickets_priority"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'in_progress', 'waiting_customer', "
            "'resolved', 'closed')",
            name=op.f("ck_tickets_status"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_tickets_customer_id_customers"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_tickets_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tickets")),
    )
    op.create_index(op.f("ix_tickets_customer_id"), "tickets", ["customer_id"])
    op.create_index(op.f("ix_tickets_product_id"), "tickets", ["product_id"])
    op.create_table(
        "ticket_messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("ticket_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("author_type", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "author_type IN ('customer', 'operator', 'agent')",
            name=op.f("ck_ticket_messages_author_type"),
        ),
        sa.CheckConstraint(
            "(author_type = 'operator' AND user_id IS NOT NULL) OR "
            "(author_type IN ('customer', 'agent') AND user_id IS NULL)",
            name=op.f("ck_ticket_messages_author_user"),
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tickets.id"],
            name=op.f("fk_ticket_messages_ticket_id_tickets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_ticket_messages_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_messages")),
    )
    op.create_index(
        op.f("ix_ticket_messages_ticket_id"), "ticket_messages", ["ticket_id"]
    )
    op.create_index(op.f("ix_ticket_messages_user_id"), "ticket_messages", ["user_id"])
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("document_key", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("source_path", sa.String(length=1024), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=True),
        sa.Column("product_version", sa.String(length=64), nullable=True),
        sa.Column("checksum", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_knowledge_documents_product_id_products"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_documents")),
        sa.UniqueConstraint(
            "document_key", name=op.f("uq_knowledge_documents_document_key")
        ),
        sa.UniqueConstraint(
            "source_path", name=op.f("uq_knowledge_documents_source_path")
        ),
    )
    op.create_index(
        op.f("ix_knowledge_documents_product_id"), "knowledge_documents", ["product_id"]
    )
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("ticket_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default="pending", nullable=False
        ),
        sa.Column("model", sa.String(length=200), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name=op.f("ck_agent_runs_status"),
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name=op.f("ck_agent_runs_dates"),
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tickets.id"],
            name=op.f("fk_agent_runs_ticket_id_tickets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_runs")),
    )
    op.create_index(op.f("ix_agent_runs_ticket_id"), "agent_runs", ["ticket_id"])
    op.create_table(
        "agent_suggestions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("agent_run_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "suggestion_type",
            sa.String(length=20),
            server_default="reply",
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default="pending", nullable=False
        ),
        sa.Column("reviewed_by", sa.BigInteger(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "suggestion_type IN ('reply', 'action')",
            name=op.f("ck_agent_suggestions_type"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name=op.f("ck_agent_suggestions_status"),
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND reviewed_by IS NULL AND reviewed_at IS NULL) "
            "OR (status IN ('approved', 'rejected') AND reviewed_by IS NOT NULL "
            "AND reviewed_at IS NOT NULL)",
            name=op.f("ck_agent_suggestions_review"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_run_id"],
            ["agent_runs.id"],
            name=op.f("fk_agent_suggestions_agent_run_id_agent_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"],
            ["users.id"],
            name=op.f("fk_agent_suggestions_reviewed_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_suggestions")),
    )
    op.create_index(
        op.f("ix_agent_suggestions_agent_run_id"), "agent_suggestions", ["agent_run_id"]
    )
    op.create_index(
        op.f("ix_agent_suggestions_reviewed_by"), "agent_suggestions", ["reviewed_by"]
    )


def downgrade() -> None:
    """Удаляет таблицы в обратном порядке; их данные будут потеряны."""
    op.drop_index(op.f("ix_agent_suggestions_reviewed_by"), "agent_suggestions")
    op.drop_index(op.f("ix_agent_suggestions_agent_run_id"), "agent_suggestions")
    op.drop_table("agent_suggestions")
    op.drop_index(op.f("ix_agent_runs_ticket_id"), "agent_runs")
    op.drop_table("agent_runs")
    op.drop_index(op.f("ix_knowledge_documents_product_id"), "knowledge_documents")
    op.drop_table("knowledge_documents")
    op.drop_index(op.f("ix_ticket_messages_user_id"), "ticket_messages")
    op.drop_index(op.f("ix_ticket_messages_ticket_id"), "ticket_messages")
    op.drop_table("ticket_messages")
    op.drop_index(op.f("ix_tickets_product_id"), "tickets")
    op.drop_index(op.f("ix_tickets_customer_id"), "tickets")
    op.drop_table("tickets")
    op.drop_index(op.f("ix_licenses_product_id"), "licenses")
    op.drop_index(op.f("ix_licenses_customer_id"), "licenses")
    op.drop_table("licenses")
    op.drop_table("products")
    op.drop_table("customers")
    op.drop_table("users")
