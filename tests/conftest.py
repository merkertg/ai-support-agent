"""Общие настройки тестов: состояние LangGraph никогда не пишется в рабочую БД."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import app.main as application
from app.api.rate_limit import write_limiter
from app.api.reviews import get_checkpointer_factory
from app.api.security import get_principal
from app.services.auth import Principal

# Сотрудник unit-тестов: запросы проходят аутентификацию без SQL-запроса токена.
TEST_PRINCIPAL = Principal(
    user_id=1,
    name="Тестовый администратор",
    email="admin@example.com",
    role="admin",
    token_id=1,
    token_name="tests",
    token_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
)


@pytest.fixture(autouse=True)
def staff_principal() -> Iterator[Principal | None]:
    """Подставить администратора вместо проверки токена в unit-тестах API.

    Unit-тесты подменяют сессию заглушками и проверяют бизнес-контракт, а не
    аутентификацию. Её проверяют test_security_api.py (без подмены) и
    интеграционные тесты: их conftest переопределяет эту фикстуру.
    """
    previous = application.app.dependency_overrides.get(get_principal)
    application.app.dependency_overrides[get_principal] = lambda: TEST_PRINCIPAL
    try:
        yield TEST_PRINCIPAL
    finally:
        if previous is None:
            application.app.dependency_overrides.pop(get_principal, None)
        else:
            application.app.dependency_overrides[get_principal] = previous


@pytest.fixture(autouse=True)
def reset_write_limiter() -> Iterator[None]:
    """Начинать каждый тест с полным лимитом изменений у каждого сотрудника."""
    write_limiter.reset()
    yield
    write_limiter.reset()


@pytest.fixture(autouse=True)
def disable_background_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Не позволить длительному TestClient обратиться к development-БД по таймеру.

    Проверки самого монитора явно включают его и подменяют SQL-проход. PG
    интеграции вызывают recovery с движком из проверенного TEST_DATABASE_URL.
    """
    monkeypatch.setenv("RUN_RECOVERY_ENABLED", "false")


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
