"""Проверить безопасное чтение конфигурации без настоящего PostgreSQL."""

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings

VALID_URL = "postgresql+psycopg://support:private-password@localhost/ai_support"


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch):
    """Не зависеть от настроек разработчика и не сохранять кеш между тестами."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_database_url_is_optional() -> None:
    """Без БД разрешён запуск API с проверкой самого процесса /health."""
    settings = Settings(_env_file=None)
    assert settings.database_url is None


def test_database_url_accepts_psycopg_and_hides_password() -> None:
    """Корректный адрес доступен коду, но отсутствует в repr настроек."""
    settings = Settings(database_url=VALID_URL, _env_file=None)
    assert settings.database_url == VALID_URL
    assert "private-password" not in repr(settings)
    assert "postgresql" not in repr(settings)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a URL private-password",
        "sqlite:///private-password.db",
        "postgresql://support:private-password@localhost/ai_support",
        "postgresql+asyncpg://support:private-password@localhost/ai_support",
        "postgresql+psycopg://support:private-password@localhost",
        "postgresql+psycopg://support:private-password@localhost/",
    ],
)
def test_database_url_rejects_invalid_driver_or_missing_database(url: str) -> None:
    """Не допустить другой драйвер и незаметное подключение к базе по умолчанию."""
    with pytest.raises(ValidationError) as raised:
        Settings(database_url=url, _env_file=None)
    # Pydantic должен скрывать исходный ввод в человекочитаемом исключении.
    assert "private-password" not in str(raised.value)


def test_environment_overrides_dotenv(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CI и Docker могут заменить локальный .env своей переменной окружения."""
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgresql+psycopg://local@localhost/local_db\n")
    monkeypatch.setenv("DATABASE_URL", VALID_URL)
    settings = Settings(_env_file=env_file)
    assert settings.database_url == VALID_URL


def test_settings_read_dotenv_and_ignore_other_project_variables(tmp_path) -> None:
    """Файл .env читается в UTF-8, а посторонние переменные не ломают старт."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATABASE_URL={VALID_URL}\nPOSTGRES_USER=support\n")
    assert Settings(_env_file=env_file).database_url == VALID_URL


def test_settings_are_cached(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Изменение окружения не создаёт разные настройки внутри одного процесса."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", VALID_URL)
    first = get_settings()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://other@localhost/other_db")
    assert get_settings() is first
    assert get_settings().database_url == VALID_URL
