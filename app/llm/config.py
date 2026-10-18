"""Независимые настройки LLM: ошибка ключа не ломает здоровье API и PostgreSQL."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseSettings):
    """Прочитать LLM-переменные, скрыть ключ и ограничить стоимость запроса."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )
    llm_provider: Literal["mock", "openai"] = "mock"
    openai_api_key: SecretStr | None = Field(default=None, repr=False)
    # Название реальной модели выбирает пользователь явно: платного default нет.
    openai_model: str | None = Field(default=None, max_length=200)
    llm_timeout_seconds: float = Field(default=30, ge=1, le=120, allow_inf_nan=False)
    llm_max_output_tokens: int = Field(default=1200, ge=128, le=4096)

    @field_validator("openai_api_key", mode="before")
    @classmethod
    def normalize_key(cls, value: object) -> object:
        """Считать пустой ключ отсутствующим и убрать внешние пробелы."""
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("openai_model", mode="before")
    @classmethod
    def normalize_model(cls, value: object) -> object:
        """Считать пустое название модели отсутствующим."""
        if isinstance(value, str):
            return value.strip() or None
        return value


@lru_cache
def get_llm_settings() -> LLMSettings:
    """Кешировать только LLM-настройки независимо от соединения с БД."""
    return LLMSettings()
