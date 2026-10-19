"""Токены сотрудников на настоящей PostgreSQL: выдача, отзыв, сроки, CLI, миграция."""

import hashlib
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.tokens as tokens_cli
from app.api.dependencies import get_api_session
from app.config import get_settings
from app.main import app
from app.models import ApiToken, User
from app.services import auth

pytestmark = pytest.mark.integration


@pytest.fixture
def client(
    migrated_engine: Engine, staff_headers: dict[str, str]
) -> Iterator[TestClient]:
    """HTTP-клиент администратора: каждая сессия запроса — к тестовой БД."""

    def session_override() -> Iterator[Session]:
        """Открыть отдельную сессию тестовой БД на запрос."""
        with Session(migrated_engine) as session:
            yield session

    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_api_session] = session_override
    try:
        with TestClient(app, headers=staff_headers) as http:
            yield http
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def bearer(token: str) -> dict[str, str]:
    """Заголовок запроса от имени сотрудника с этим токеном."""
    return {"Authorization": f"Bearer {token}"}


def create_user(client: TestClient, email: str, role: str = "operator") -> dict:
    """Создать сотрудника через API администратора."""
    response = client.post(
        "/api/users", json={"name": "Сотрудник", "email": email, "role": role}
    )
    assert response.status_code == 201, response.text
    return response.json()


def issue(client: TestClient, user_id: int, **body) -> dict:
    """Выдать токен сотруднику через API администратора."""
    response = client.post(
        f"/api/users/{user_id}/tokens", json={"name": "ноутбук"} | body
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_token_lifecycle_and_only_hash_is_stored(
    client: TestClient, migrated_engine: Engine
) -> None:
    """Выдача, чтение «кто я», список без секретов, отзыв и повторный отзыв."""
    user = create_user(client, "anna@example.com", "reviewer")
    issued = issue(client, user["id"], expires_in_days=7)
    secret = issued["token"]
    assert auth.is_well_formed(secret)
    assert issued["token_prefix"] == secret[:12]
    assert issued["user_id"] == user["id"] and issued["revoked_at"] is None
    me = client.get("/api/auth/me", headers=bearer(secret))
    assert me.status_code == 200, me.text
    assert (me.json()["id"], me.json()["role"]) == (user["id"], "reviewer")
    assert me.json()["token_id"] == issued["id"]
    listed = client.get(f"/api/users/{user['id']}/tokens").json()
    assert [item["id"] for item in listed] == [issued["id"]]
    assert "token" not in listed[0] and "token_hash" not in listed[0]
    with migrated_engine.connect() as connection:
        row = connection.execute(
            select(ApiToken).where(ApiToken.id == issued["id"])
        ).one()
        days = connection.execute(
            text("SELECT expires_at - created_at FROM api_tokens WHERE id = :id"),
            {"id": issued["id"]},
        ).scalar()
        dump = connection.execute(
            text("SELECT string_agg(t::text, '') FROM api_tokens t")
        ).scalar()
    assert row.token_hash == hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in dump
    assert days.days == 7
    revoked = client.delete(f"/api/tokens/{issued['id']}")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked_at"] is not None
    again = client.delete(f"/api/tokens/{issued['id']}")
    assert again.json()["revoked_at"] == revoked.json()["revoked_at"]
    rejected = client.get("/api/auth/me", headers=bearer(secret))
    assert rejected.status_code == 401
    assert rejected.headers["WWW-Authenticate"] == "Bearer"


def test_expired_unknown_and_inactive_tokens_are_rejected(
    client: TestClient, migrated_engine: Engine
) -> None:
    """Истёкший, чужой и токен неактивного сотрудника получают одинаковый 401."""
    user = create_user(client, "boris@example.com")
    expired = issue(client, user["id"])
    active = issue(client, user["id"], name="второй")
    with Session(migrated_engine) as session, session.begin():
        session.execute(
            update(ApiToken)
            .where(ApiToken.id == expired["id"])
            .values(
                created_at=text("now() - interval '10 days'"),
                expires_at=text("now() - interval '1 second'"),
            )
        )
    assert client.get("/api/auth/me", headers=bearer(expired["token"])).status_code == (
        401
    )
    assert client.get("/api/auth/me", headers=bearer(active["token"])).status_code == (
        200
    )
    unknown = client.get("/api/auth/me", headers=bearer(auth.generate_token()))
    assert unknown.status_code == 401
    with Session(migrated_engine) as session, session.begin():
        session.get(User, user["id"]).is_active = False
    inactive = client.get("/api/auth/me", headers=bearer(active["token"]))
    assert inactive.status_code == 401
    assert inactive.json() == unknown.json()
    refused = client.post(f"/api/users/{user['id']}/tokens", json={"name": "ещё"})
    assert refused.status_code == 409


def test_token_management_errors(client: TestClient) -> None:
    """Неизвестный сотрудник или токен — 404; неверный срок — 422."""
    assert client.post("/api/users/999/tokens", json={"name": "x"}).status_code == 404
    assert client.get("/api/users/999/tokens").status_code == 404
    assert client.delete("/api/tokens/999").status_code == 404
    user = create_user(client, "vera@example.com")
    for body in (
        {},
        {"name": " "},
        {"name": "x", "expires_in_days": 0},
        {"name": "x", "expires_in_days": 366},
        {"name": "x", "expires_in_days": "30"},
        {"name": "x", "token": "asa_chosen"},
    ):
        response = client.post(f"/api/users/{user['id']}/tokens", json=body)
        assert response.status_code == 422, body


def test_role_change_in_database_applies_to_next_request(
    client: TestClient, migrated_engine: Engine
) -> None:
    """Роль читается из БД на каждый запрос: понижение действует сразу."""
    user = create_user(client, "gleb@example.com", "admin")
    secret = issue(client, user["id"])["token"]
    created = client.post(
        "/api/users",
        json={"name": "Новый", "email": "new@example.com"},
        headers=bearer(secret),
    )
    assert created.status_code == 201
    with Session(migrated_engine) as session, session.begin():
        session.get(User, user["id"]).role = "operator"
    denied = client.post(
        "/api/users",
        json={"name": "Ещё", "email": "more@example.com"},
        headers=bearer(secret),
    )
    assert denied.status_code == 403


def test_write_limit_applies_to_real_staff(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Лимит изменений считает запросы сотрудника; чтение остаётся доступным."""
    monkeypatch.setenv("WRITE_RATE_LIMIT_PER_MINUTE", "3")
    get_settings.cache_clear()
    try:
        statuses = [
            client.post(
                "/api/customers",
                json={"name": "Клиент", "email": f"c{index}@example.com"},
            ).status_code
            for index in range(4)
        ]
        assert statuses == [201, 201, 201, 429]
        assert client.get("/api/customers").status_code == 200
        assert len(client.get("/api/customers").json()) == 3
    finally:
        get_settings.cache_clear()


def test_database_constraints_protect_tokens(migrated_engine: Engine) -> None:
    """Хеш уникален, срок позже выдачи, роль только из списка."""
    with Session(migrated_engine) as session:
        user = User(name="Дина", email="dina@example.com", role="reviewer")
        session.add(user)
        session.commit()
        user_id = user.id
    statements = [
        ApiToken(
            user_id=user_id,
            name="a",
            token_hash="0" * 64,
            token_prefix="asa_",
            expires_at=text("now() + interval '1 day'"),
        ),
        ApiToken(
            user_id=user_id,
            name="b",
            token_hash="0" * 64,
            token_prefix="asa_",
            expires_at=text("now() + interval '1 day'"),
        ),
    ]
    with Session(migrated_engine) as session:
        session.add(statements[0])
        session.commit()
        session.add(statements[1])
        with pytest.raises(IntegrityError):
            session.commit()
    with Session(migrated_engine) as session:
        session.add(
            ApiToken(
                user_id=user_id,
                name="c",
                token_hash="1" * 64,
                token_prefix="asa_",
                expires_at=text("now() - interval '1 day'"),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
    with Session(migrated_engine) as session:
        session.add(User(name="Ева", email="eva@example.com", role="superuser"))
        with pytest.raises(IntegrityError):
            session.commit()
    with Session(migrated_engine) as session, session.begin():
        session.delete(session.get(User, user_id))
    with migrated_engine.connect() as connection:
        remaining = connection.execute(text("SELECT count(*) FROM api_tokens"))
        assert remaining.scalar() == 0


def test_upgrade_keeps_staff_and_downgrade_demotes_reviewers(
    migrated_engine: Engine, migration_config: Config
) -> None:
    """0004 → 0005 сохраняет сотрудников; откат превращает reviewer в operator."""
    command.downgrade(migration_config, "0004_review_workflow")
    assert "api_tokens" not in inspect(migrated_engine).get_table_names()
    with migrated_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (name, email, role) VALUES "
                "('Админ', 'old-admin@example.com', 'admin'), "
                "('Оператор', 'old-operator@example.com', 'operator')"
            )
        )
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    text(
                        "INSERT INTO users (name, email, role) "
                        "VALUES ('Р', 'r@example.com', 'reviewer')"
                    )
                )
    command.upgrade(migration_config, "head")
    with migrated_engine.begin() as connection:
        roles = dict(connection.execute(text("SELECT email, role FROM users")).all())
        assert roles == {
            "old-admin@example.com": "admin",
            "old-operator@example.com": "operator",
        }
        connection.execute(
            text(
                "UPDATE users SET role = 'reviewer' "
                "WHERE email = 'old-operator@example.com'"
            )
        )
    command.downgrade(migration_config, "0004_review_workflow")
    with migrated_engine.connect() as connection:
        roles = dict(connection.execute(text("SELECT email, role FROM users")).all())
    assert roles["old-operator@example.com"] == "operator"
    assert "api_tokens" not in inspect(migrated_engine).get_table_names()
    command.upgrade(migration_config, "head")


@pytest.fixture
def cli(migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch):
    """Запускать python -m app.tokens в процессе теста против тестовой БД."""
    monkeypatch.setattr(tokens_cli, "get_engine", lambda: migrated_engine)
    monkeypatch.setattr(tokens_cli, "dispose_engine", lambda: None)
    return tokens_cli.main


def test_cli_bootstraps_admin_and_prints_only_token(
    cli, capsys, migrated_engine: Engine, client: TestClient
) -> None:
    """issue создаёт первого администратора; stdout — только сам токен."""
    assert (
        cli(
            [
                "issue",
                "--email",
                "root@example.com",
                "--name",
                "Первый администратор",
                "--role",
                "admin",
                "--label",
                "bootstrap",
                "--days",
                "2",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    secret = captured.out.strip()
    assert auth.is_well_formed(secret) and captured.out == secret + "\n"
    assert secret not in captured.err
    assert "Создан сотрудник" in captured.err and "admin" in captured.err
    me = client.get("/api/auth/me", headers=bearer(secret)).json()
    assert (me["email"], me["role"], me["token_name"]) == (
        "root@example.com",
        "admin",
        "bootstrap",
    )
    # Повторная выдача существующему сотруднику не требует имени и роли.
    assert cli(["issue", "--email", "root@example.com", "--label", "второй"]) == 0
    second = capsys.readouterr().out.strip()
    assert second != secret and auth.is_well_formed(second)
    assert cli(["list", "--email", "root@example.com"]) == 0
    listed = capsys.readouterr().out
    assert '"bootstrap"' in listed and '"второй"' in listed
    assert secret not in listed and second not in listed
    token_id = me["token_id"]
    assert cli(["revoke", "--token-id", str(token_id)]) == 0
    assert '"revoked_at": "' in capsys.readouterr().out
    assert client.get("/api/auth/me", headers=bearer(secret)).status_code == 401


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["issue", "--email", "ghost@example.com"], "укажи --name"),
        (
            ["issue", "--email", "staff-admin@example.com", "--role", "operator"],
            "не меняет роль",
        ),
        (["issue", "--email", "x@example.com", "--name", " "], "неверное значение"),
        (["issue", "--email", "not-an-email", "--name", "X"], "неверное значение"),
        (["issue", "--email", "a@example.com", "--days", "400"], "неверное значение"),
        (["list", "--email", "ghost@example.com"], "не найден"),
        (["revoke", "--token-id", "999"], "Токен не найден"),
    ],
)
def test_cli_explains_refusals(
    cli, capsys, staff_headers: dict, arguments: list[str], message: str
) -> None:
    """Понятная причина в stderr и код 1; ничего не печатается в stdout."""
    assert cli(arguments) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err


def test_cli_refuses_inactive_staff(cli, capsys, migrated_engine: Engine) -> None:
    """Неактивному сотруднику токен не выдаётся."""
    with Session(migrated_engine) as session, session.begin():
        session.add(User(name="Ушёл", email="gone@example.com", is_active=False))
    assert cli(["issue", "--email", "gone@example.com"]) == 1
    assert "неактивен" in capsys.readouterr().err


def test_cli_reports_unconfigured_database(monkeypatch, capsys) -> None:
    """Без DATABASE_URL команда подсказывает настройку и не печатает адрес."""
    from app.db.session import DatabaseNotConfigured

    def missing():
        """Сымитировать отсутствующий DATABASE_URL."""
        raise DatabaseNotConfigured("DATABASE_URL не задан")

    monkeypatch.setattr(tokens_cli, "get_engine", missing)
    assert tokens_cli.main(["list", "--email", "a@example.com"]) == 1
    assert "DATABASE_URL" in capsys.readouterr().err


def test_cli_normalizes_email_domain_for_repeat_issue_and_list(
    cli, capsys, migrated_engine: Engine
) -> None:
    """Регистр домена и внешние пробелы не создают нового сотрудника при выдаче."""
    assert cli(["issue", "--email", "Staff@EXAMPLE.COM", "--name", "Сотрудник"]) == 0
    first = capsys.readouterr().out.strip()
    assert auth.is_well_formed(first)
    assert cli(["issue", "--email", " Staff@ExAmPlE.CoM "]) == 0
    second = capsys.readouterr().out.strip()
    assert auth.is_well_formed(second) and first != second
    assert cli(["list", "--email", " Staff@EXAMPLE.COM "]) == 0
    listed = capsys.readouterr().out
    assert first not in listed and second not in listed
    with Session(migrated_engine) as session:
        employees = session.scalars(
            select(User).where(User.email == "Staff@example.com")
        )
        [user] = list(employees)
        issued = session.scalars(select(ApiToken).where(ApiToken.user_id == user.id))
        assert len(list(issued)) == 2
