"""Схема поддержки: клиенты, лицензии, обращения и история работы агента.

Эти классы описывают данные. Они не реализуют API, авторизацию, запуск LLM
или поиск: тексты и векторы статей хранит Qdrant, здесь — только реестр.
Реальную схему PostgreSQL изменяет только Alembic.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    """Профиль сотрудника поддержки без пароля и механизма авторизации."""

    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('operator', 'admin')", name="role"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    role: Mapped[str] = mapped_column(String(20), server_default="operator")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Customer(Base):
    """Клиент, которому принадлежат лицензии и обращения в поддержку."""

    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    company: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Product(Base):
    """Продукт с устойчивым уникальным кодом для лицензий и обращений."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    code: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class License(Base):
    """Лицензия клиента на продукт с проверкой статуса и срока действия."""

    __tablename__ = "licenses"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'expired', 'revoked')", name="status"),
        CheckConstraint("expires_at IS NULL OR expires_at >= issued_at", name="dates"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # RESTRICT сохраняет связь: нельзя удалить клиента/продукт с лицензиями.
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )
    license_key: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(20), server_default="active")
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Ticket(Base):
    """Обращение клиента по продукту с приоритетом и текущим статусом."""

    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint(
            "priority IN ('low', 'medium', 'high', 'critical')", name="priority"
        ),
        CheckConstraint(
            "status IN ('open', 'in_progress', 'waiting_customer', "
            "'resolved', 'closed')",
            name="status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )
    subject: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(20), server_default="medium")
    status: Mapped[str] = mapped_column(String(30), server_default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # onupdate обновляет время при UPDATE через SQLAlchemy. Прямой SQL должен
    # явно передавать updated_at: серверный триггер на этом этапе не создаётся.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TicketMessage(Base):
    """Сообщение в обращении; сотрудник указан только для автора operator."""

    __tablename__ = "ticket_messages"
    __table_args__ = (
        CheckConstraint(
            "author_type IN ('customer', 'operator', 'agent')", name="author_type"
        ),
        CheckConstraint(
            "(author_type = 'operator' AND user_id IS NOT NULL) OR "
            "(author_type IN ('customer', 'agent') AND user_id IS NULL)",
            name="author_user",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # Сообщения — часть обращения, поэтому удаляются вместе с ним.
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    author_type: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class KnowledgeDocument(Base):
    """Реестр Markdown-статьи: метаданные и сведения о её записи в Qdrant."""

    __tablename__ = "knowledge_documents"
    __table_args__ = (CheckConstraint("chunk_count >= 0", name="chunk_count"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # Ключ для ссылок из ответов; путь указывает на исходный Markdown-файл.
    document_key: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(300))
    source_path: Mapped[str] = mapped_column(String(1024), unique=True)
    # Область поиска задаёт код продукта из заголовка статьи; NULL — общая статья.
    product_code: Mapped[str | None] = mapped_column(String(64))
    # Справочная связь с каталогом: заполняется, если продукт с кодом уже есть.
    # Удаление продукта сохраняет метаданные и не меняет область поиска.
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), index=True
    )
    product_version: Mapped[str | None] = mapped_column(String(64))
    # SHA-256 исходного файла: изменение текста требует новой индексации.
    checksum: Mapped[str | None] = mapped_column(String(128))
    # Сколько фрагментов и в каком пространстве векторов записано в Qdrant.
    chunk_count: Mapped[int] = mapped_column(Integer, server_default="0")
    embedding_signature: Mapped[str | None] = mapped_column(String(300))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AgentRun(Base):
    """Сохранённый запуск: прогресс инструментов, итог или безопасная ошибка."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')", name="status"
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="dates"
        ),
        CheckConstraint("provider IN ('mock', 'openai')", name="provider"),
        CheckConstraint("steps >= 0", name="steps"),
        CheckConstraint("jsonb_typeof(tool_trace) = 'array'", name="tool_trace"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    provider: Mapped[str] = mapped_column(String(20), server_default="mock")
    model: Mapped[str | None] = mapped_column(String(200))
    steps: Mapped[int] = mapped_column(Integer, server_default="0")
    # JSONB хранит только проверенный и ограниченный runtime протокол.
    # none_as_null сохраняет SQL NULL, а не JSON null, для незавершённого итога.
    tool_trace: Mapped[list[dict]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    usage: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    # Поток LangGraph, в котором предложение ждёт решения сотрудника.
    workflow_thread_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)


class AgentSuggestion(Base):
    """Черновик агента: pending ждёт решения; approved доставлен, rejected — нет."""

    __tablename__ = "agent_suggestions"
    __table_args__ = (
        CheckConstraint("suggestion_type IN ('reply', 'action')", name="type"),
        CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="status"),
        CheckConstraint(
            "(status = 'pending' AND reviewed_by IS NULL AND reviewed_at IS NULL) "
            "OR (status IN ('approved', 'rejected') AND reviewed_by IS NOT NULL "
            "AND reviewed_at IS NOT NULL)",
            name="review",
        ),
        # Итоговый текст и доставленное сообщение бывают только у одобренного.
        CheckConstraint(
            "final_content IS NULL OR status = 'approved'", name="final_content"
        ),
        CheckConstraint(
            "delivered_message_id IS NULL OR status = 'approved'", name="delivery"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # Обращение определяется через agent_runs.ticket_id без дублирования FK.
    agent_run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    suggestion_type: Mapped[str] = mapped_column(String(20), server_default="reply")
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    # Ожидающее предложение не имеет рецензента; принятое/отклонённое имеет.
    reviewed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Текст, который сотрудник одобрил (возможно, отредактировав черновик).
    final_content: Mapped[str | None] = mapped_column(Text)
    review_comment: Mapped[str | None] = mapped_column(Text)
    # Сообщение обращения, созданное учебной доставкой одобренного ответа.
    delivered_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("ticket_messages.id", ondelete="SET NULL"), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
