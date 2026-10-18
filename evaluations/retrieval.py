"""Размеченные вопросы и измерение ранжирования существующего поискового индекса."""

import hashlib
import json
import warnings
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from qdrant_client import QdrantClient

from app.knowledge.documents import SourceDocument, load_documents
from app.knowledge.embeddings import MockEmbedder
from app.knowledge.index import KnowledgeIndex

DEFAULT_DATASET = Path(__file__).with_name("retrieval_cases.jsonl")
DIMENSIONS = 256
MIN_SCORE = 0.2
CUTOFF = 5


class RetrievalCase(BaseModel):
    """Проверить разметку вопроса, продукта и ожидаемых статей до запуска поиска."""

    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1)
    product_code: str = Field(min_length=1)
    query: str = Field(min_length=1)
    expected_articles: list[str]
    kind: Literal["positive", "no_hit"]
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def coherent_label(self) -> "RetrievalCase":
        """Не допустить пустую положительную разметку или дубли источников."""
        if bool(self.expected_articles) != (self.kind == "positive"):
            raise ValueError("positive требует источники, no_hit — пустой список")
        if len(set(self.expected_articles)) != len(self.expected_articles):
            raise ValueError("повтор источника в разметке")
        return self


def load_cases(path: Path, documents: list[SourceDocument]) -> list[RetrievalCase]:
    """Прочитать JSONL и проверить уникальные ID, существование и область статей."""
    cases = [
        RetrievalCase.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError("набор должен содержать вопросы с уникальными ID")
    if not any(case.kind == "positive" for case in cases) or not any(
        case.kind == "no_hit" for case in cases
    ):
        raise ValueError("нужны положительные вопросы и вопросы без ответа")
    by_key = {document.metadata.document_key: document for document in documents}
    for case in cases:
        for key in case.expected_articles:
            if key not in by_key:
                raise ValueError(f"{case.id}: неизвестная статья {key}")
            if by_key[key].scope not in {case.product_code, "general"}:
                raise ValueError(f"{case.id}: статья {key} вне области продукта")
    return cases


@contextmanager
def local_index(documents: list[SourceDocument]):
    """Построить настоящий Qdrant-индекс в памяти; соединения всегда закрываются."""
    with KnowledgeIndex(
        QdrantClient(location=":memory:"),
        "offline_evaluation",
        MockEmbedder(DIMENSIONS),
        min_score=MIN_SCORE,
    ) as index:
        # Локальный Qdrant не применяет индексы payload; сервер их применяет.
        # Подавляем только этот известный сигнал и только при создании коллекции.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="Payload indexes have no effect in the local Qdrant.*",
                category=UserWarning,
            )
            index.ensure_collection()
        for document in documents:
            index.replace_document(document)
        yield index


def ranking_metrics(expected: list[str], ranked: list[str], k: int) -> dict:
    """Посчитать Recall@k и reciprocal rank; безответные вопросы не усредняются."""
    selected = ranked[:k]
    recall = len(set(expected) & set(selected)) / len(expected) if expected else None
    first_rank = next(
        (position for position, key in enumerate(selected, 1) if key in expected),
        None,
    )
    return {
        "recall": recall,
        "reciprocal_rank": 1 / first_rank if first_rank is not None else 0.0,
    }


def evaluate_retrieval(index: KnowledgeIndex, cases: list[RetrievalCase]) -> dict:
    """Измерить поиск по статьям, сохранив все промахи и нарушения области."""
    details = []
    for case in cases:
        hits = index.search(
            case.query, [case.product_code, "general"], CUTOFF, per_document=True
        )
        ranked = [hit.document_key for hit in hits]
        details.append(
            {
                **case.model_dump(),
                "ranked_articles": ranked,
                "scores": [hit.score for hit in hits],
                "scopes": [hit.scope for hit in hits],
                "at_5": ranking_metrics(case.expected_articles, ranked, CUTOFF),
                "at_3": ranking_metrics(case.expected_articles, ranked, 3),
                "scope_violations": sum(
                    hit.scope not in {case.product_code, "general"} for hit in hits
                ),
                "no_hit_correct": not hits if case.kind == "no_hit" else None,
            }
        )
    positive = [item for item in details if item["kind"] == "positive"]
    negative = [item for item in details if item["kind"] == "no_hit"]
    return {
        "case_count": len(details),
        "positive_count": len(positive),
        "negative_count": len(negative),
        "recall_at_5": sum(item["at_5"]["recall"] for item in positive) / len(positive),
        "mrr_at_5": sum(item["at_5"]["reciprocal_rank"] for item in positive)
        / len(positive),
        "recall_at_3": sum(item["at_3"]["recall"] for item in positive) / len(positive),
        "mrr_at_3": sum(item["at_3"]["reciprocal_rank"] for item in positive)
        / len(positive),
        "no_hit_accuracy": sum(item["no_hit_correct"] for item in negative)
        / len(negative),
        "scope_violations": sum(item["scope_violations"] for item in details),
        "cases": details,
    }


def corpus_manifest(directory: Path) -> tuple[list[SourceDocument], list[dict]]:
    """Зафиксировать SHA256 исходных статей, чтобы отчёты можно было сравнивать."""
    documents = load_documents(directory)
    manifest = [
        {
            "document_key": item.metadata.document_key,
            "source_path": item.source_path,
            "sha256": item.checksum,
            "scope": item.scope,
            "chunks": len(item.chunks),
        }
        for item in documents
    ]
    return documents, manifest


def dataset_sha256(path: Path) -> str:
    """Вычислить контрольную сумму исходных байтов разметки без нормализации."""
    return hashlib.sha256(path.read_bytes()).hexdigest()
