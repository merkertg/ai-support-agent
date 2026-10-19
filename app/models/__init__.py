"""Публичные модели проекта; импорт регистрирует все таблицы в Base.metadata."""

from app.models.entities import (
    AgentRun,
    AgentSuggestion,
    ApiToken,
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
    "ApiToken",
    "Customer",
    "KnowledgeDocument",
    "License",
    "Product",
    "Ticket",
    "TicketMessage",
    "User",
]
