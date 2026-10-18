"""Добавляет реестр индексации статей базы знаний без новых таблиц.

Revision ID: 0003_knowledge_index
Revises: 0002_agent_runtime

Существующие строки knowledge_documents получают chunk_count=0 и пустые
сведения об индексации: такие статьи будут проиндексированы командой ingest.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_knowledge_index"
down_revision: str | Sequence[str] | None = "0002_agent_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавить код продукта статьи и сведения о её записи в Qdrant."""
    op.add_column(
        "knowledge_documents", sa.Column("product_code", sa.String(64), nullable=True)
    )
    op.add_column(
        "knowledge_documents",
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "knowledge_documents",
        sa.Column("embedding_signature", sa.String(300), nullable=True),
    )
    op.add_column(
        "knowledge_documents",
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_knowledge_documents_chunk_count"),
        "knowledge_documents",
        "chunk_count >= 0",
    )


def downgrade() -> None:
    """Удалить поля этапа 06; векторы в Qdrant этим откатом не удаляются."""
    op.drop_constraint(
        op.f("ck_knowledge_documents_chunk_count"), "knowledge_documents", type_="check"
    )
    for name in ("indexed_at", "embedding_signature", "chunk_count", "product_code"):
        op.drop_column("knowledge_documents", name)
