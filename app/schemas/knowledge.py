"""Запросы и ответы поиска по базе знаний и общий формат ключа статьи."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.resources import CreateSchema, ProductCode

# Ключ статьи цитируется в ответах агента, поэтому формат строгий: KB-СЛОВО-СЛОВО.
DocumentKey = Annotated[
    str, StringConstraints(pattern=r"^KB-[A-Z0-9]+(?:-[A-Z0-9]+)*$", max_length=64)
]
SearchQuery = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=3, max_length=300)
]
IndexState = Literal[
    "ready", "missing", "incompatible", "unavailable", "not_configured"
]


class KnowledgeSearchRequest(CreateSchema):
    """Принять текст запроса, необязательный код продукта и число результатов."""

    query: SearchQuery
    # С кодом продукта ищем статьи этого продукта и общие; без кода — все статьи.
    product_code: ProductCode | None = None
    limit: int = Field(default=5, ge=1, le=10, strict=True)


class KnowledgeHitRead(BaseModel):
    """Найденный фрагмент статьи с источником и косинусным сходством."""

    model_config = ConfigDict(extra="forbid")
    document_key: str
    title: str
    section: str
    text: str
    score: float
    source_path: str
    scope: str


class KnowledgeSearchResponse(BaseModel):
    """Вернуть результаты и пространство векторов, в котором выполнен поиск."""

    model_config = ConfigDict(extra="forbid")
    query: str
    embedding_signature: str
    results: list[KnowledgeHitRead]


class KnowledgeStatus(BaseModel):
    """Показать настройки и состояние индекса без ключей и сетевых адресов."""

    model_config = ConfigDict(extra="forbid")
    configured: bool
    embedding_provider: Literal["mock", "openai"]
    embedding_model: str | None
    dimensions: int
    collection: str
    index: IndexState
    points: int | None
    documents: int
