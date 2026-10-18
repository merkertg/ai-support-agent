"""Эмбеддинги: локальный демонстрационный вектор и официальный OpenAI SDK.

Эмбеддинг — список чисел фиксированной длины. Близкие по смыслу (или, в mock,
по словам) тексты дают векторы с большим косинусным сходством; Qdrant
сравнивает вектор запроса с векторами фрагментов статей.
"""

import hashlib
import math
import re
from collections.abc import Callable
from typing import Literal, Protocol

import openai
from pydantic import ValidationError

from app.knowledge.config import KnowledgeSettings
from app.knowledge.errors import (
    KnowledgeConfigurationError,
    KnowledgeInvalidResponseError,
    KnowledgeTimeoutError,
    KnowledgeUnavailableError,
)
from app.llm.config import LLMSettings, get_llm_settings

MOCK_EMBEDDING_MODEL = "mock-hash-v1"
# Усечение до пяти букв грубо объединяет формы слова: «ошибка» и «ошибке».
STEM_LENGTH = 5
MAX_BATCH = 64
WORD = re.compile(r"[0-9a-zа-я]+")
STOP_WORDS = frozenset(
    {
        "без",
        "для",
        "его",
        "если",
        "есть",
        "еще",
        "или",
        "как",
        "нет",
        "под",
        "после",
        "при",
        "так",
        "уже",
        "что",
        "чтобы",
        "это",
        "the",
        "and",
    }
)


class Embedder(Protocol):
    """Общий интерфейс: индексация и поиск используют один и тот же провайдер."""

    provider: Literal["mock", "openai"]
    model: str
    dimensions: int

    @property
    def signature(self) -> str:
        """Однозначно назвать пространство векторов: провайдер, модель, размер."""
        ...

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Вернуть по одному вектору на каждый текст в исходном порядке."""
        ...


def stems(text: str) -> list[str]:
    """Выделить основы слов: нижний регистр, ё → е, без коротких и служебных."""
    words = WORD.findall(text.lower().replace("ё", "е"))
    return [word[:STEM_LENGTH] for word in words if len(word) >= 3]


def is_meaningful(text: str) -> bool:
    """Проверить, что в запросе есть хотя бы одно значимое слово."""
    return any(stem not in STOP_WORDS for stem in stems(text))


class MockEmbedder:
    """Детерминированный лексический вектор без сети и платных запросов.

    Каждая основа слова через SHA-256 попадает в одну из координат со знаком
    +1 или −1, затем вектор нормируется. Похожими считаются тексты с общими
    словами; синонимы и смысл без общих слов mock не распознаёт.
    """

    provider: Literal["mock"] = "mock"
    model = MOCK_EMBEDDING_MODEL

    def __init__(self, dimensions: int) -> None:
        """Зафиксировать размерность, общую для индексации и поиска."""
        self.dimensions = dimensions

    @property
    def signature(self) -> str:
        """Назвать пространство векторов для payload Qdrant и реестра PostgreSQL."""
        return f"{self.provider}:{self.model}:{self.dimensions}"

    def vector(self, text: str) -> list[float]:
        """Построить нормированный вектор; текст без слов даёт нулевой вектор."""
        values = [0.0] * self.dimensions
        for stem in stems(text):
            if stem in STOP_WORDS:
                continue
            digest = hashlib.sha256(stem.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            values[index] += 1.0 if digest[4] % 2 == 0 else -1.0
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values] if norm else values

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Вернуть векторы всех текстов без внешних запросов."""
        return [self.vector(text) for text in texts]


class OpenAIEmbedder:
    """Получать эмбеддинги из OpenAI одной попыткой на пакет без повторов SDK."""

    provider: Literal["openai"] = "openai"

    def __init__(
        self,
        settings: KnowledgeSettings,
        llm_settings: LLMSettings,
        *,
        sdk_factory: Callable[..., openai.OpenAI] = openai.OpenAI,
    ) -> None:
        """Проверить ключ и явную модель; конструктор не обращается в сеть."""
        if not llm_settings.openai_api_key or not settings.openai_embedding_model:
            raise KnowledgeConfigurationError()
        self.model = settings.openai_embedding_model
        self.dimensions = settings.embedding_dimensions
        self._api_key = llm_settings.openai_api_key
        self._timeout = llm_settings.llm_timeout_seconds
        self._sdk_factory = sdk_factory

    @property
    def signature(self) -> str:
        """Назвать пространство векторов выбранной модели и размерности."""
        return f"{self.provider}:{self.model}:{self.dimensions}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Отправить тексты пакетами и проверить порядок, размер и числа ответа."""
        vectors: list[list[float]] = []
        try:
            with self._sdk_factory(
                api_key=self._api_key.get_secret_value(),
                base_url="https://api.openai.com/v1",
                timeout=self._timeout,
                max_retries=0,
            ) as sdk:
                for start in range(0, len(texts), MAX_BATCH):
                    batch = texts[start : start + MAX_BATCH]
                    response = sdk.embeddings.create(
                        model=self.model,
                        input=batch,
                        dimensions=self.dimensions,
                        encoding_format="float",
                    )
                    items = sorted(response.data, key=lambda item: item.index)
                    if [item.index for item in items] != list(range(len(batch))):
                        raise KnowledgeInvalidResponseError()
                    for item in items:
                        vector = [float(value) for value in item.embedding]
                        if len(vector) != self.dimensions or not all(
                            math.isfinite(value) for value in vector
                        ):
                            raise KnowledgeInvalidResponseError()
                        vectors.append(vector)
        except openai.APITimeoutError:
            raise KnowledgeTimeoutError() from None
        except (
            openai.AuthenticationError,
            openai.PermissionDeniedError,
            # Неизвестная модель или неподдерживаемая размерность — ошибка настройки.
            openai.BadRequestError,
            openai.NotFoundError,
        ):
            raise KnowledgeConfigurationError() from None
        except (
            openai.APIResponseValidationError,
            ValidationError,
            AttributeError,
            TypeError,
            ValueError,
        ):
            raise KnowledgeInvalidResponseError() from None
        except openai.APIError:
            raise KnowledgeUnavailableError() from None
        return vectors


def get_embedder(settings: KnowledgeSettings) -> Embedder:
    """Выбрать mock по умолчанию; OpenAI требует ключ и явную модель."""
    if settings.embedding_provider == "mock":
        return MockEmbedder(settings.embedding_dimensions)
    try:
        llm_settings = get_llm_settings()
    except ValidationError:
        raise KnowledgeConfigurationError() from None
    return OpenAIEmbedder(settings, llm_settings)
