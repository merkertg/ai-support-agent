"""Независимые ограничения цикла, общие для mock и реального провайдера."""

from functools import lru_cache

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agent.errors import AgentConfigurationError


class AgentSettings(BaseSettings):
    """Ограничить стоимость и длительность без неявных платных значений."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )
    agent_max_steps: int = Field(default=6, ge=1, le=8)
    agent_max_tool_calls: int = Field(default=12, ge=1, le=20)
    agent_timeout_seconds: float = Field(default=60, ge=1, le=120, allow_inf_nan=False)


@lru_cache
def get_agent_settings() -> AgentSettings:
    """Прочитать лимиты один раз и не раскрыть значения ошибочного .env."""
    try:
        return AgentSettings()
    except ValidationError:
        raise AgentConfigurationError() from None
