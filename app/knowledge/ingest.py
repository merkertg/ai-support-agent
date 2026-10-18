"""Проиндексировать статьи базы знаний: Markdown → фрагменты → Qdrant + реестр.

Запускать явно: python -m app.knowledge.ingest. Повторный запуск без
изменений ничего не переписывает. Изменённая статья индексируется заново,
удалённая из каталога — удаляется из Qdrant и из реестра PostgreSQL.
"""

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import Engine, delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.console import use_utf8_output
from app.db.session import DatabaseNotConfigured, dispose_engine, get_engine
from app.knowledge.config import get_knowledge_settings
from app.knowledge.documents import SourceDocument, load_documents
from app.knowledge.errors import KnowledgeError, KnowledgeSourceError
from app.knowledge.index import KnowledgeIndex, open_index
from app.models import KnowledgeDocument, Product


@dataclass
class IngestReport:
    """Итог индексации, который печатается как JSON и проверяется тестами."""

    collection: str
    embedding_signature: str
    documents: int = 0
    chunks: int = 0
    indexed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def is_current(
    row: KnowledgeDocument | None, document: SourceDocument, index: KnowledgeIndex
) -> bool:
    """Проверить реестр и сам Qdrant: файл, модель и число точек совпадают.

    Подсчёт точек нужен на случай, когда том Qdrant удалили, а реестр остался.
    """
    return (
        row is not None
        and row.checksum == document.checksum
        and row.embedding_signature == index.embedder.signature
        and row.chunk_count == len(document.chunks)
        and index.count(document.metadata.document_key) == len(document.chunks)
    )


def save_registry(
    engine: Engine,
    document: SourceDocument,
    product_id: int | None,
    *,
    signature: str | None,
    indexed: bool,
) -> None:
    """Создать или обновить строку реестра одной статьи отдельной транзакцией."""
    key = document.metadata.document_key
    with Session(engine) as session, session.begin():
        row = session.scalar(
            select(KnowledgeDocument).where(KnowledgeDocument.document_key == key)
        )
        if row is None:
            row = KnowledgeDocument(document_key=key)
            session.add(row)
        row.title = document.metadata.title
        row.source_path = document.source_path
        row.product_code = document.metadata.product_code
        row.product_id = product_id
        row.product_version = document.metadata.product_version
        if indexed:
            row.checksum = document.checksum
            row.chunk_count = len(document.chunks)
            row.embedding_signature = signature
            row.indexed_at = datetime.now(UTC)


def ingest(
    engine: Engine, index: KnowledgeIndex, documents: list[SourceDocument]
) -> IngestReport:
    """Привести Qdrant и реестр PostgreSQL в соответствие с каталогом статей.

    Сначала пишем векторы, затем коммитим реестр. Если commit не удался,
    следующий запуск увидит старую контрольную сумму и повторит индексацию.
    Во время запросов к эмбеддингам и Qdrant SQL-соединение не удерживается.
    """
    index.ensure_collection()
    report = IngestReport(
        collection=index.collection,
        embedding_signature=index.embedder.signature,
        documents=len(documents),
        chunks=sum(len(document.chunks) for document in documents),
    )
    with Session(engine) as session:
        rows = {
            row.document_key: row
            for row in session.scalars(select(KnowledgeDocument)).all()
        }
        products = dict(session.execute(select(Product.code, Product.id)).all())
        # Объекты нужны только для сравнения: отсоединяем их до сетевых запросов.
        session.expunge_all()
        session.rollback()
    current_keys = {document.metadata.document_key for document in documents}
    for document in documents:
        key = document.metadata.document_key
        row = rows.get(key)
        product_id = products.get(document.metadata.product_code or "")
        if is_current(row, document, index):
            report.unchanged.append(key)
            # Векторы актуальны; обновляем только метаданные, например связь
            # с продуктом, появившимся в каталоге после прошлой индексации.
            if (
                row.product_id != product_id
                or row.title != document.metadata.title
                or row.source_path != document.source_path
            ):
                save_registry(
                    engine, document, product_id, signature=None, indexed=False
                )
            continue
        index.replace_document(document)
        save_registry(
            engine,
            document,
            product_id,
            signature=index.embedder.signature,
            indexed=True,
        )
        report.indexed.append(key)
    # Удаляем статьи, которых нет в каталоге: и из реестра, и из самого Qdrant.
    stale_keys = (set(rows) | index.document_keys()) - current_keys
    for key in sorted(stale_keys):
        index.remove_document(key)
        with Session(engine) as session, session.begin():
            session.execute(
                delete(KnowledgeDocument).where(KnowledgeDocument.document_key == key)
            )
        report.removed.append(key)
    return report


def main(argv: list[str] | None = None) -> int:
    """Прочитать каталог, выполнить индексацию и вывести JSON или ошибку."""
    use_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="каталог статей *.md (по умолчанию KNOWLEDGE_DIR или knowledge/)",
    )
    args = parser.parse_args(argv)
    try:
        settings = get_knowledge_settings()
        documents = load_documents(args.dir or settings.knowledge_dir)
        with open_index(settings) as index:
            report = ingest(get_engine(), index, documents)
    except KnowledgeSourceError as error:
        print(f"Ошибка статьи базы знаний: {error}", file=sys.stderr)
        return 1
    except KnowledgeError as error:
        print(error.detail, file=sys.stderr)
        return 1
    except (DatabaseNotConfigured, ValidationError, SQLAlchemyError):
        # Не печатаем URL, SQL и параметры: там могут быть пароль и данные.
        print("База данных недоступна или не настроена.", file=sys.stderr)
        return 1
    finally:
        dispose_engine()
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
