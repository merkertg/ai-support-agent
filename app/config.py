"""Настройки приложения из переменных окружения и локального файла .env."""

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    """Прочитать конфигурацию один раз: адрес PostgreSQL и лимит изменений.

    Переменная окружения DATABASE_URL имеет приоритет над файлом .env.
    Отсутствующий адрес допустим: /health продолжает работать, а /ready
    сообщает, что приложение ещё не готово обслуживать операции с БД.
    """

    # Не выводим значение поля в repr и не включаем исходный ввод в текст
    # ошибки Pydantic: адрес подключения может содержать пароль.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )
    database_url: str | None = Field(default=None, repr=False)
    # Сколько изменений (POST/PATCH/DELETE) один сотрудник делает за минуту.
    write_rate_limit_per_minute: int = Field(default=60, ge=1, le=10_000)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str | None) -> str | None:
        """Разрешить только Psycopg 3 и явно указанное имя PostgreSQL-базы."""
        if value is None:
            return None
        try:
            url = make_url(value)
        except ArgumentError:
            raise ValueError(
                "DATABASE_URL: нужен корректный адрес PostgreSQL"
            ) from None
        if url.drivername != "postgresql+psycopg":
            raise ValueError("DATABASE_URL: требуется драйвер postgresql+psycopg")
        if not url.database or not url.database.strip():
            raise ValueError("DATABASE_URL: укажите конкретное имя базы данных")
        return value


@lru_cache
def get_settings() -> Settings:
    """Кешировать проверенные настройки до завершения процесса приложения."""
    return Settings()
