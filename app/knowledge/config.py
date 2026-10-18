"""Настройки базы знаний независимо от БД, LLM-черновика и лимитов агента."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.knowledge.errors import KnowledgeConfigurationError

# Каталог статей лежит рядом с app/: в репозитории и в Docker-образе (/app).
DEFAULT_KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge"
CollectionName = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$", strip_whitespace=True)
]


class KnowledgeSettings(BaseSettings):
    """Прочитать адрес Qdrant, коллекцию, провайдера эмбеддингов и порог поиска.

    Без QDRANT_URL база знаний считается ненастроенной: поиск отвечает 503,
    а агент получает ошибку инструмента и продолжает работу без источников.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )
    qdrant_url: str | None = None
    qdrant_collection: CollectionName = "support_knowledge"
    qdrant_timeout_seconds: int = Field(default=5, ge=1, le=30)
    embedding_provider: Literal["mock", "openai"] = "mock"
    # Модель OpenAI выбирается явно: платной модели по умолчанию нет.
    openai_embedding_model: str | None = Field(default=None, max_length=200)
    embedding_dimensions: int = Field(default=256, ge=64, le=3072)
    # Фрагменты с меньшим косинусным сходством не считаются найденными.
    knowledge_min_score: float = Field(default=0.2, ge=0, le=1, allow_inf_nan=False)
    knowledge_dir: Path = DEFAULT_KNOWLEDGE_DIR

    @field_validator("qdrant_url", "openai_embedding_model", mode="before")
    @classmethod
    def empty_as_missing(cls, value: object) -> object:
        """Считать пустую строку из .env отсутствующим значением."""
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("qdrant_url")
    @classmethod
    def http_url(cls, value: str | None) -> str | None:
        """Принять только http(s)-адрес без пути: так ошибка видна до запроса."""
        if value is not None and not value.startswith(("http://", "https://")):
            raise ValueError("QDRANT_URL должен начинаться с http:// или https://")
        return value.rstrip("/") if value is not None else None


@lru_cache
def get_knowledge_settings() -> KnowledgeSettings:
    """Кешировать настройки и не показывать значения ошибочного .env."""
    try:
        return KnowledgeSettings()
    except ValidationError:
        raise KnowledgeConfigurationError() from None
