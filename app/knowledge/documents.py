"""Чтение Markdown-статей: метаданные в заголовке и разбиение на фрагменты.

Файл начинается блоком между строками `---` с полями `ключ: значение`.
Текст делится по заголовкам второго уровня `## `, длинный раздел — по абзацам.
Отдельный фрагмент получает собственный вектор и может быть найден поиском.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from app.knowledge.errors import KnowledgeSourceError
from app.schemas.knowledge import DocumentKey
from app.schemas.resources import ProductCode

MAX_DOCUMENT_BYTES = 200_000
MAX_CHUNK_CHARACTERS = 1200
GENERAL_SCOPE = "general"
Title = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)
]
ProductVersion = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
]


class DocumentMetadata(BaseModel):
    """Проверить заголовок статьи; неизвестное поле считается опечаткой автора."""

    model_config = ConfigDict(extra="forbid")
    document_key: DocumentKey
    title: Title
    # Нет кода продукта — статья общая и находится для любого обращения.
    product_code: ProductCode | None = None
    product_version: ProductVersion | None = None


@dataclass(frozen=True)
class Chunk:
    """Фрагмент раздела, который индексируется и цитируется отдельно."""

    index: int
    section: str
    text: str


@dataclass(frozen=True)
class SourceDocument:
    """Проверенная статья: метаданные, путь, контрольная сумма и фрагменты."""

    metadata: DocumentMetadata
    source_path: str
    checksum: str
    chunks: tuple[Chunk, ...]

    @property
    def scope(self) -> str:
        """Вернуть код продукта статьи или признак общей статьи для фильтра Qdrant."""
        return self.metadata.product_code or GENERAL_SCOPE

    def embedding_text(self, chunk: Chunk) -> str:
        """Добавить к тексту название и раздел: они тоже помогают поиску."""
        return f"{self.metadata.title}\n{chunk.section}\n{chunk.text}"


def parse_front_matter(text: str, path: Path) -> tuple[DocumentMetadata, str]:
    """Разобрать блок `---` без YAML-библиотеки и вернуть метаданные и тело."""
    if not text.startswith("---\n"):
        raise KnowledgeSourceError(f"{path.name}: файл должен начинаться с ---")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise KnowledgeSourceError(f"{path.name}: не найден конец заголовка ---")
    fields: dict[str, str] = {}
    for number, line in enumerate(text[4:end].splitlines(), start=2):
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        key = key.strip()
        if not separator or not key:
            raise KnowledgeSourceError(
                f"{path.name}:{number}: ожидалась строка вида ключ: значение"
            )
        if key in fields:
            raise KnowledgeSourceError(f"{path.name}: поле {key} указано дважды")
        fields[key] = value.strip()
    try:
        metadata = DocumentMetadata.model_validate(fields)
    except ValidationError as error:
        names = sorted({".".join(map(str, item["loc"])) for item in error.errors()})
        raise KnowledgeSourceError(
            f"{path.name}: неверные поля заголовка: {', '.join(names)}"
        ) from None
    return metadata, text[end + len("\n---\n") :]


def split_long_paragraph(paragraph: str) -> list[str]:
    """Разделить слишком длинный абзац по пробелам, не разрывая слова."""
    parts: list[str] = []
    current = ""
    for word in paragraph.split():
        candidate = f"{current} {word}".strip()
        if len(candidate) > MAX_CHUNK_CHARACTERS and current:
            parts.append(current)
            current = word
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def split_sections(title: str, body: str) -> list[Chunk]:
    """Собрать фрагменты по разделам `## ` и абзацам не длиннее предела."""
    sections: list[tuple[str, list[str]]] = [(title, [])]
    for line in body.splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip() or title, []))
        else:
            sections[-1][1].append(line)
    chunks: list[Chunk] = []
    for section, lines in sections:
        pieces: list[str] = []
        for paragraph in re.split(r"\n\s*\n", "\n".join(lines)):
            paragraph = paragraph.strip()
            if len(paragraph) > MAX_CHUNK_CHARACTERS:
                pieces.extend(split_long_paragraph(paragraph))
            elif paragraph:
                pieces.append(paragraph)
        # Соседние абзацы раздела объединяются, пока фрагмент не превысит предел.
        current: list[str] = []
        for piece in pieces:
            if current and len("\n\n".join([*current, piece])) > MAX_CHUNK_CHARACTERS:
                chunks.append(Chunk(len(chunks), section, "\n\n".join(current)))
                current = []
            current.append(piece)
        if current:
            chunks.append(Chunk(len(chunks), section, "\n\n".join(current)))
    return chunks


def load_document(path: Path, root: Path) -> SourceDocument:
    """Прочитать один файл UTF-8, проверить заголовок и разбить текст."""
    data = path.read_bytes()
    if len(data) > MAX_DOCUMENT_BYTES:
        raise KnowledgeSourceError(f"{path.name}: файл больше 200 000 байт")
    try:
        text = data.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError:
        raise KnowledgeSourceError(f"{path.name}: файл не в кодировке UTF-8") from None
    metadata, body = parse_front_matter(text, path)
    chunks = split_sections(metadata.title, body)
    if not chunks:
        raise KnowledgeSourceError(f"{path.name}: статья не содержит текста")
    return SourceDocument(
        metadata=metadata,
        source_path=path.relative_to(root).as_posix(),
        # Контрольная сумма исходных байтов показывает, изменился ли файл.
        checksum=hashlib.sha256(data).hexdigest(),
        chunks=tuple(chunks),
    )


def load_documents(directory: Path) -> list[SourceDocument]:
    """Прочитать все `*.md` каталога и запретить повторяющиеся ключи статей."""
    if not directory.is_dir():
        raise KnowledgeSourceError(f"Каталог базы знаний не найден: {directory}")
    # Путь сохраняется относительно родителя: knowledge/<файл>.md в любой среде.
    root = directory.parent
    documents = [load_document(path, root) for path in sorted(directory.glob("*.md"))]
    keys: dict[str, str] = {}
    for document in documents:
        key = document.metadata.document_key
        if key in keys:
            raise KnowledgeSourceError(
                f"Ключ {key} повторяется в {keys[key]} и {document.source_path}"
            )
        keys[key] = document.source_path
    return sorted(documents, key=lambda item: item.metadata.document_key)
