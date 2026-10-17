"""Проверить readiness, ленивое соединение и границы сессий без сервера БД."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import app.db.session as database
import app.main as api
from app.config import Settings, get_settings


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Изолировать unit-тесты от .env и очистить единственный пул после теста."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    database.dispose_engine()
    get_settings.cache_clear()
    yield
    database.dispose_engine()
    get_settings.cache_clear()


@pytest.fixture
def ready_session(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Заменить только SQL-сессию: обработка HTTP-ошибок остаётся настоящей."""
    session = MagicMock(spec=Session)
    version_result = MagicMock()
    version_result.scalars.return_value = iter(["test_head"])
    session.execute.side_effect = [MagicMock(), version_result]

    def sessions():
        """Выдать фиктивную сессию через тот же протокол генератора."""
        yield session

    monkeypatch.setattr(api, "get_session", sessions)
    monkeypatch.setattr(api, "migration_heads", lambda: {"test_head"})
    return session


def test_health_does_not_require_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Живой API отвечает /health даже без настроенной и доступной базы."""
    factory = MagicMock(side_effect=AssertionError("не должно быть соединения"))
    monkeypatch.setattr(database, "create_engine", factory)
    with TestClient(api.app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "ai-support-agent"}
    factory.assert_not_called()


def test_ready_requires_database_url() -> None:
    """Отсутствующая настройка даёт управляемый HTTP 503 вместо HTTP 500."""
    with TestClient(api.app) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": api.READINESS_ERROR}


def test_ready_returns_ok_for_current_schema(ready_session: MagicMock) -> None:
    """Готовность требует SQL-соединение и актуальную версию схемы."""
    with TestClient(api.app) as client:
        response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}
    statements = [str(call.args[0]) for call in ready_session.execute.call_args_list]
    assert statements == ["SELECT 1", "SELECT version_num FROM alembic_version"]


@pytest.mark.parametrize("versions", [[], ["old_revision"], ["test_head", "extra"]])
def test_ready_rejects_unmigrated_or_stale_schema(
    ready_session: MagicMock, versions: list[str]
) -> None:
    """Пустая или несовпадающая версия схемы не считается готовой базой."""
    result = MagicMock()
    result.scalars.return_value = iter(versions)
    ready_session.execute.side_effect = [MagicMock(), result]
    with TestClient(api.app) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": api.READINESS_ERROR}


def test_ready_hides_connection_error_and_password(ready_session: MagicMock) -> None:
    """Ответ не раскрывает текст Psycopg, SQL и пароль при отказе PostgreSQL."""
    ready_session.execute.side_effect = OperationalError(
        "SELECT 1", {}, RuntimeError("support:private-password@database")
    )
    with TestClient(api.app) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": api.READINESS_ERROR}
    assert "private-password" not in response.text


def test_ready_hides_invalid_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ошибка валидатора .env тоже превращается в безопасный HTTP 503."""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///private-password.db")
    with TestClient(api.app) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert "private-password" not in response.text


def test_engine_is_lazy_cached_and_has_connection_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Не подключаться при импорте и использовать один ограниченный пул."""
    url = "postgresql+psycopg://support:secret@localhost/ai_support"
    monkeypatch.setenv("DATABASE_URL", url)
    engine = MagicMock()
    factory = MagicMock(return_value=engine)
    monkeypatch.setattr(database, "create_engine", factory)
    assert database.get_engine() is engine
    assert database.get_engine() is engine
    factory.assert_called_once_with(
        url, pool_pre_ping=True, connect_args={"connect_timeout": 3}
    )
    engine.connect.assert_not_called()
    database.dispose_engine()
    engine.dispose.assert_called_once()
    assert database.get_engine.cache_info().currsize == 0


def test_dispose_does_not_create_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Остановка приложения без запросов к БД не создаёт ненужный пул."""
    factory = MagicMock()
    monkeypatch.setattr(database, "create_engine", factory)
    database.dispose_engine()
    factory.assert_not_called()


def test_session_does_not_commit_automatically(monkeypatch: pytest.MonkeyPatch) -> None:
    """Успешный выход закрывает сессию, а commit остаётся явным решением кода."""
    session = MagicMock(spec=Session)
    factory = MagicMock()
    factory.return_value.__enter__.return_value = session
    monkeypatch.setattr(database, "Session", factory)
    monkeypatch.setattr(database, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(database, "create_engine", MagicMock())
    # Адрес только создаёт фиктивный пул; сетевых обращений в этом тесте нет.
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://local@localhost/test")
    sessions = database.get_session()
    assert next(sessions) is session
    with pytest.raises(StopIteration):
        next(sessions)
    session.commit.assert_not_called()
    factory.return_value.__exit__.assert_called_once()


def test_session_rolls_back_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ошибка бизнес-операции вызывает rollback до закрытия сессии."""
    session = MagicMock(spec=Session)
    factory = MagicMock()
    factory.return_value.__enter__.return_value = session
    monkeypatch.setattr(database, "Session", factory)
    monkeypatch.setattr(database, "create_engine", MagicMock())
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://local@localhost/test")
    sessions = database.get_session()
    assert next(sessions) is session
    with pytest.raises(ValueError, match="ошибка операции"):
        sessions.throw(ValueError("ошибка операции"))
    session.rollback.assert_called_once()
    session.commit.assert_not_called()
    factory.return_value.__exit__.assert_called_once()
