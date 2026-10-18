"""Общие настройки тестов: состояние LangGraph никогда не пишется в рабочую БД."""

from collections.abc import Iterator

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import app.main as application
from app.api.reviews import get_checkpointer_factory


@pytest.fixture(autouse=True)
def memory_checkpointer() -> Iterator[InMemorySaver]:
    """Подставить checkpointer в памяти процесса для каждого теста.

    Иначе POST /analyze взял бы DATABASE_URL из локального .env и записал бы
    состояние графа в development-БД. Интеграционные тесты, которым нужен
    PostgresSaver, заменяют эту зависимость явно на тестовую БД *_test.
    """
    saver = InMemorySaver()
    previous = application.app.dependency_overrides.get(get_checkpointer_factory)
    application.app.dependency_overrides[get_checkpointer_factory] = lambda: (
        lambda: saver
    )
    try:
        yield saver
    finally:
        if previous is None:
            application.app.dependency_overrides.pop(get_checkpointer_factory, None)
        else:
            application.app.dependency_overrides[get_checkpointer_factory] = previous
