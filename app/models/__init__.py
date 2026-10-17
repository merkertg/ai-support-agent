"""Публичные модели этапа 02; импорт регистрирует все таблицы в Base.metadata."""

from app.models.entities import (
    AgentRun,
    AgentSuggestion,
    Customer,
    KnowledgeDocument,
    License,
    Product,
    Ticket,
    TicketMessage,
    User,
)

__all__ = [
    "AgentRun",
    "AgentSuggestion",
    "Customer",
    "KnowledgeDocument",
    "License",
    "Product",
    "Ticket",
    "TicketMessage",
    "User",
]
