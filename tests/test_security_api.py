"""Аутентификация, роли и лимит изменений API без PostgreSQL.

В этом модуле подмена сотрудника из tests/conftest.py отключена: запросы
проходят настоящие зависимости app/api/security.py. Сессия БД — заглушка,
поэтому любой преждевременный SQL-запрос сразу уронил бы тест.
"""

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.main as api
from app.api.dependencies import get_api_session
from app.api.rate_limit import RateLimiter, write_limiter
from app.api.security import NOT_AUTHENTICATED
from app.config import get_settings
from app.observability import LOGGER_NAME, JSONFormatter
from app.services import auth, drafts, resources
from app.services.auth import AuthenticationFailed, Principal

TOKEN = auth.TOKEN_PREFIX + "A" * 43
OPEN_PATHS = {"/health", "/ready", "/docs", "/openapi.json", "/redoc"}


@pytest.fixture(autouse=True)
def staff_principal() -> None:
    """Отключить подмену аутентификации из tests/conftest.py для этого модуля."""
    return None


@pytest.fixture
def session() -> Iterator[Mock]:
    """Подставить сессию-заглушку: SQL выполняют только явно подменённые сервисы."""
    value = Mock(spec=Session)

    def override() -> Iterator[Session]:
        """Выдать заглушку запросу."""
        yield value

    previous = api.app.dependency_overrides.copy()
    api.app.dependency_overrides[get_api_session] = override
    try:
        yield value
    finally:
        api.app.dependency_overrides.clear()
        api.app.dependency_overrides.update(previous)


@pytest.fixture
def client(session: Mock) -> Iterator[TestClient]:
    """HTTP-клиент приложения с настоящими зависимостями безопасности."""
    with TestClient(api.app) as value:
        yield value


def staff(role: str = "admin", user_id: int = 5) -> Principal:
    """Сотрудник с заданной ролью, каким его возвращает auth.authenticate."""
    return Principal(
        user_id=user_id,
        name="Сотрудник",
        email=f"user{user_id}@example.com",
        role=role,
        token_id=9,
        token_name="tests",
        token_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )


def sign_in(monkeypatch: pytest.MonkeyPatch, *principals: Principal) -> None:
    """Принимать токены вида asa_…: первый символ после префикса выбирает сотрудника."""
    by_token = {
        auth.TOKEN_PREFIX + str(index) * 43: principal
        for index, principal in enumerate(principals)
    }

    def authenticate(session: Session, token: str) -> Principal:
        """Найти сотрудника заглушки без SQL."""
        if token not in by_token:
            raise AuthenticationFailed("unknown")
        return by_token[token]

    monkeypatch.setattr(auth, "authenticate", authenticate)


def bearer(index: int = 0) -> dict[str, str]:
    """Заголовок токена сотрудника с номером index из sign_in."""
    return {"Authorization": f"Bearer {auth.TOKEN_PREFIX}{str(index) * 43}"}


def api_operations() -> list[tuple[str, str]]:
    """Перечислить все операции /api/* из OpenAPI с подставленными id."""
    schema = api.app.openapi()
    operations = []
    for path, methods in schema["paths"].items():
        if not path.startswith("/api/"):
            continue
        concrete = path.replace("{resource_id}", "1")
        operations.extend((method.upper(), concrete) for method in methods)
    return operations


@pytest.fixture
def app_logs() -> Iterator[list[dict]]:
    """Собрать JSON-строки журнала «app» в список словарей."""
    records: list[dict] = []

    class Collect(logging.Handler):
        """Сохранить отформатированную строку как разобранный JSON."""

        def emit(self, record: logging.LogRecord) -> None:
            """Разобрать JSON-строку журнала."""
            records.append(json.loads(self.format(record)))

    logger = logging.getLogger(LOGGER_NAME)
    handler = Collect()
    handler.setFormatter(JSONFormatter())
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def test_every_api_operation_requires_token_before_sql(client, session) -> None:
    """Любая операция /api/* без токена получает 401 и не трогает БД."""
    operations = api_operations()
    assert len(operations) >= 25
    for method, path in operations:
        response = client.request(method, path, json={})
        assert response.status_code == 401, (method, path, response.text)
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert response.json() == {"detail": NOT_AUTHENTICATED}
    assert session.method_calls == []


@pytest.mark.parametrize(
    "header",
    [
        "Bearer",
        "Basic dXNlcjpwYXNz",
        "Bearer not-a-staff-token",
        f"Bearer {TOKEN}x",
        f"Token {TOKEN}",
    ],
)
def test_malformed_credentials_are_rejected_without_sql(client, session, header):
    """Чужая схема или неверный формат токена отсекаются до запроса к БД."""
    response = client.get("/api/customers", headers={"Authorization": header})
    assert response.status_code == 401
    assert session.method_calls == []


def test_open_routes_do_not_require_token(client) -> None:
    """Проверки живости и документация доступны без токена."""
    assert client.get("/health").status_code == 200
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert schema["components"]["securitySchemes"]["StaffToken"] == {
        "type": "http",
        "scheme": "bearer",
        "description": "API-токен сотрудника, выданный администратором (asa_…).",
    }
    for path, methods in schema["paths"].items():
        for operation in methods.values():
            protected = operation.get("security") == [{"StaffToken": []}]
            assert protected is path.startswith("/api/"), path
            if protected:
                assert "401" in operation["responses"], path
    assert (
        set(schema["paths"])
        - {path for path in schema["paths"] if path.startswith("/api/")}
        <= OPEN_PATHS
    )


def test_unknown_token_is_logged_without_secret(client, monkeypatch, app_logs) -> None:
    """Причина отказа видна в журнале, а сам токен — нигде."""
    sign_in(monkeypatch, staff())
    response = client.get(
        "/api/customers", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert response.status_code == 401
    [failure] = [item for item in app_logs if item["event"] == "auth_failed"]
    assert (failure["reason"], failure["path"]) == ("unknown", "/api/customers")
    assert TOKEN not in json.dumps(app_logs)


def test_authenticated_request_log_has_user_id(client, monkeypatch, app_logs) -> None:
    """Строка журнала запроса называет сотрудника, но не его токен."""
    sign_in(monkeypatch, staff(user_id=12))
    monkeypatch.setattr(resources, "list_customers", lambda *args, **kwargs: [])
    response = client.get("/api/customers", headers=bearer())
    assert response.status_code == 200, response.text
    [line] = [item for item in app_logs if item["event"] == "request"]
    assert (line["user_id"], line["status"]) == (12, 200)
    assert "asa_" not in json.dumps(app_logs)


def test_current_user_reports_role_and_token(client, monkeypatch) -> None:
    """/api/auth/me показывает сотрудника токена без секрета."""
    sign_in(monkeypatch, staff("reviewer", 3))
    response = client.get("/api/auth/me", headers=bearer())
    assert response.status_code == 200
    assert response.json() == {
        "id": 3,
        "name": "Сотрудник",
        "email": "user3@example.com",
        "role": "reviewer",
        "token_id": 9,
        "token_name": "tests",
        "token_expires_at": "2099-01-01T00:00:00Z",
    }


@pytest.mark.parametrize(
    ("role", "method", "path", "body"),
    [
        ("operator", "POST", "/api/suggestions/1/review", {"decision": "approve"}),
        ("operator", "POST", "/api/users", {"name": "Новый", "email": "n@x.com"}),
        ("reviewer", "POST", "/api/users", {"name": "Новый", "email": "n@x.com"}),
        ("reviewer", "POST", "/api/users/1/tokens", {"name": "t"}),
        ("operator", "GET", "/api/users/1/tokens", None),
        ("reviewer", "DELETE", "/api/tokens/1", None),
    ],
)
def test_roles_below_required_get_403(
    client, session, monkeypatch, role, method, path, body
) -> None:
    """Недостаточная роль получает 403 до бизнес-логики и SQL."""
    sign_in(monkeypatch, staff(role))
    response = client.request(method, path, json=body, headers=bearer())
    assert response.status_code == 403, response.text
    assert "Недостаточно прав" in response.json()["detail"]
    assert session.method_calls == []


def test_forbidden_attempts_do_not_spend_write_limit(client, monkeypatch) -> None:
    """Отказ по роли не расходует лимит: следующая разрешённая запись проходит."""
    monkeypatch.setenv("WRITE_RATE_LIMIT_PER_MINUTE", "1")
    get_settings.cache_clear()
    sign_in(monkeypatch, staff("operator"))
    monkeypatch.setattr(
        resources,
        "create_customer",
        lambda session, payload: SimpleNamespace(
            id=1,
            name=payload.name,
            email=payload.email,
            company=None,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )
    try:
        for _ in range(3):
            denied = client.post(
                "/api/users", json={"name": "x", "email": "x@x.com"}, headers=bearer()
            )
            assert denied.status_code == 403
        created = client.post(
            "/api/customers",
            json={"name": "Клиент", "email": "c@example.com"},
            headers=bearer(),
        )
        assert created.status_code == 201, created.text
    finally:
        get_settings.cache_clear()


def test_write_limit_returns_429_per_staff_member(client, monkeypatch) -> None:
    """Лимит считается на сотрудника; чтение и другой сотрудник не блокируются."""
    monkeypatch.setenv("WRITE_RATE_LIMIT_PER_MINUTE", "2")
    get_settings.cache_clear()
    sign_in(monkeypatch, staff("operator", 1), staff("operator", 2))
    draft = Mock(side_effect=drafts.ResourceNotFound("Обращение не найдено"))
    monkeypatch.setattr(drafts, "create_draft", draft)
    monkeypatch.setattr(resources, "list_tickets", lambda *args, **kwargs: [])
    try:
        statuses = [
            client.post("/api/tickets/1/draft", headers=bearer(0)).status_code
            for _ in range(3)
        ]
        assert statuses == [404, 404, 429]
        limited = client.post("/api/tickets/1/draft", headers=bearer(0))
        assert limited.status_code == 429
        assert 1 <= int(limited.headers["Retry-After"]) <= 30
        assert "Слишком много изменений" in limited.json()["detail"]
        assert client.get("/api/tickets", headers=bearer(0)).status_code == 200
        assert client.post("/api/tickets/1/draft", headers=bearer(1)).status_code == 404
        assert draft.call_count == 3
    finally:
        get_settings.cache_clear()


def test_invalid_rate_limit_setting_is_rejected(monkeypatch) -> None:
    """Нулевой или огромный лимит не принимается настройками."""
    for value in ("0", "10001", "many"):
        monkeypatch.setenv("WRITE_RATE_LIMIT_PER_MINUTE", value)
        with pytest.raises(ValueError):
            type(get_settings())(_env_file=None)


class Clock:
    """Управляемые часы для проверки пополнения ведра."""

    def __init__(self) -> None:
        """Начать с нуля секунд."""
        self.now = 0.0

    def __call__(self) -> float:
        """Вернуть текущее время теста."""
        return self.now


def test_rate_limiter_allows_burst_then_refills() -> None:
    """Ведро на 3 изменения в минуту: серия из трёх, затем одно раз в 20 с."""
    clock = Clock()
    limiter = RateLimiter(clock)
    assert [limiter.acquire("staff", 3) for _ in range(3)] == [None, None, None]
    assert limiter.acquire("staff", 3) == 20
    clock.now = 5
    assert limiter.acquire("staff", 3) == 15
    clock.now = 20
    assert limiter.acquire("staff", 3) is None
    assert limiter.acquire("staff", 3) == 20
    assert limiter.acquire("other", 3) is None
    clock.now = 10_000
    # Простой не копит разрешения сверх ёмкости ведра.
    assert [limiter.acquire("staff", 3) for _ in range(4)][-1] == 20
    limiter.reset()
    assert limiter.acquire("staff", 3) is None


def test_rate_limiter_wait_is_at_least_one_second() -> None:
    """Высокий лимит не превращает ожидание в ноль секунд."""
    limiter = RateLimiter(Clock())
    for _ in range(600):
        assert limiter.acquire("staff", 600) is None
    assert limiter.acquire("staff", 600) == 1


def test_shared_limiter_is_reset_between_tests() -> None:
    """Общий ограничитель процесса пуст в начале каждого теста."""
    assert write_limiter.acquire("tests", 1) is None
    assert write_limiter.acquire("tests", 1) is not None


def test_generated_tokens_are_random_and_well_formed() -> None:
    """Токен — префикс и 43 символа base64url; хранится только SHA-256."""
    tokens = {auth.generate_token() for _ in range(100)}
    assert len(tokens) == 100
    assert all(auth.is_well_formed(token) for token in tokens)
    token = tokens.pop()
    assert len(auth.hash_token(token)) == 64
    assert auth.hash_token(token) != auth.hash_token(token[:-1] + "x")
    assert not auth.is_well_formed(token + "\n")


def token_row(**changes) -> tuple:
    """Строка SELECT authenticate: токен, сотрудник и признак «не истёк»."""
    record = SimpleNamespace(
        id=4,
        name="ноутбук",
        revoked_at=None,
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    user = SimpleNamespace(
        id=8, name="Анна", email="anna@example.com", role="reviewer", is_active=True
    )
    current = changes.pop("current", True)
    for key, value in changes.items():
        setattr(user if hasattr(user, key) else record, key, value)
    return record, user, current


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (None, "unknown"),
        (token_row(revoked_at=datetime(2026, 1, 1, tzinfo=UTC)), "revoked"),
        (token_row(current=False), "expired"),
        (token_row(is_active=False), "inactive"),
    ],
)
def test_authenticate_rejects_and_closes_transaction(row, reason) -> None:
    """Каждый отказ называет причину и завершает транзакцию чтения."""
    session = Mock(spec=Session)
    session.execute.return_value.first.return_value = row
    with pytest.raises(AuthenticationFailed) as raised:
        auth.authenticate(session, TOKEN)
    assert raised.value.reason == reason
    session.rollback.assert_called_once_with()


def test_authenticate_returns_detached_principal() -> None:
    """Успех возвращает данные сотрудника и тоже закрывает транзакцию."""
    session = Mock(spec=Session)
    session.execute.return_value.first.return_value = token_row()
    principal = auth.authenticate(session, TOKEN)
    assert principal == Principal(
        user_id=8,
        name="Анна",
        email="anna@example.com",
        role="reviewer",
        token_id=4,
        token_name="ноутбук",
        token_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    session.rollback.assert_called_once_with()


def test_authenticate_rejects_malformed_token_without_sql() -> None:
    """Строка неверного формата не превращается в SQL-запрос."""
    session = Mock(spec=Session)
    with pytest.raises(AuthenticationFailed, match="malformed"):
        auth.authenticate(session, "asa_short")
    session.execute.assert_not_called()
