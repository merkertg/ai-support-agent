"""Проверить разбор Markdown-статей и деление на фрагменты без Qdrant и сети."""

from pathlib import Path

import pytest

from app.knowledge.config import DEFAULT_KNOWLEDGE_DIR
from app.knowledge.documents import (
    GENERAL_SCOPE,
    MAX_CHUNK_CHARACTERS,
    load_document,
    load_documents,
)
from app.knowledge.errors import KnowledgeSourceError

HEADER = "---\ndocument_key: KB-TEST\ntitle: Проверка\n---\n"


def write(directory: Path, name: str, text: str) -> Path:
    """Создать статью во временном каталоге теста."""
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def test_repository_knowledge_base_is_valid_and_has_unique_keys() -> None:
    """Все статьи репозитория проходят проверку заголовка и делятся на фрагменты."""
    documents = load_documents(DEFAULT_KNOWLEDGE_DIR)
    keys = [document.metadata.document_key for document in documents]
    assert len(keys) == len(set(keys)) >= 7
    assert keys == sorted(keys)
    for document in documents:
        assert document.source_path.startswith("knowledge/")
        assert len(document.checksum) == 64
        assert [chunk.index for chunk in document.chunks] == list(
            range(len(document.chunks))
        )
        assert all(len(chunk.text) <= MAX_CHUNK_CHARACTERS for chunk in document.chunks)
        # Каждая статья помечена как вымышленная учебная.
        assert "Вымышленная" in document.chunks[0].text
    scopes = {document.scope for document in documents}
    assert {"techflow-server", "techflow-desktop", GENERAL_SCOPE} <= scopes


def test_sections_become_chunks_with_section_names(tmp_path: Path) -> None:
    """Текст до первого `##` относится к названию статьи, далее — к разделам."""
    path = write(
        tmp_path,
        "a.md",
        HEADER
        + "Вступление.\n\n## Причины\n\nПорт занят.\n\n"
        + "## Решение\n\nОсвободите.",
    )
    document = load_document(path, tmp_path)
    assert [(chunk.section, chunk.text) for chunk in document.chunks] == [
        ("Проверка", "Вступление."),
        ("Причины", "Порт занят."),
        ("Решение", "Освободите."),
    ]
    assert document.scope == GENERAL_SCOPE
    assert document.source_path == "a.md"
    assert document.embedding_text(document.chunks[1]) == (
        "Проверка\nПричины\nПорт занят."
    )


def test_long_section_is_split_without_breaking_words(tmp_path: Path) -> None:
    """Длинный раздел делится по абзацам и словам, не превышая предел."""
    paragraph = " ".join(["слово"] * 400)
    path = write(tmp_path, "a.md", HEADER + f"## Раздел\n\n{paragraph}\n\nКонец.")
    chunks = load_document(path, tmp_path).chunks
    assert len(chunks) >= 2
    assert all(len(chunk.text) <= MAX_CHUNK_CHARACTERS for chunk in chunks)
    joined = " ".join(chunk.text.replace("\n\n", " ") for chunk in chunks)
    assert joined.split() == paragraph.split() + ["Конец."]


def test_checksum_changes_only_with_file_content(tmp_path: Path) -> None:
    """Контрольная сумма зависит от байтов файла, а не от времени чтения."""
    path = write(tmp_path, "a.md", HEADER + "Текст.")
    first = load_document(path, tmp_path).checksum
    assert load_document(path, tmp_path).checksum == first
    path.write_text(HEADER + "Другой текст.", encoding="utf-8")
    assert load_document(path, tmp_path).checksum != first


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("document_key: KB-A\n", "должен начинаться с ---"),
        ("---\ndocument_key: KB-A\ntitle: A\n", "не найден конец"),
        ("---\nбез двоеточия\n---\nТекст", "ключ: значение"),
        ("---\ndocument_key: KB-A\ndocument_key: KB-B\n---\nТекст", "указано дважды"),
        ("---\ndocument_key: kb-lower\ntitle: A\n---\nТекст", "document_key"),
        ("---\ntitle: A\n---\nТекст", "document_key"),
        ("---\ndocument_key: KB-A\ntitle: A\nauthor: X\n---\nТекст", "author"),
        ("---\ndocument_key: KB-A\ntitle: A\n---\n\n## Пусто\n\n", "не содержит"),
    ],
)
def test_invalid_articles_are_rejected_with_reason(
    tmp_path: Path, text: str, message: str
) -> None:
    """Ошибка автора статьи называет файл и причину, а не печатает весь текст."""
    path = write(tmp_path, "bad.md", text)
    with pytest.raises(KnowledgeSourceError, match=message) as error:
        load_document(path, tmp_path)
    assert "bad.md" in str(error.value)


def test_non_utf8_and_oversized_files_are_rejected(tmp_path: Path) -> None:
    """Статья должна быть UTF-8 и не больше установленного размера."""
    (tmp_path / "latin.md").write_bytes(HEADER.encode() + "Текст".encode("cp1251"))
    with pytest.raises(KnowledgeSourceError, match="UTF-8"):
        load_document(tmp_path / "latin.md", tmp_path)
    write(tmp_path, "big.md", HEADER + "я" * 120_000)
    with pytest.raises(KnowledgeSourceError, match="200 000"):
        load_document(tmp_path / "big.md", tmp_path)


def test_duplicate_keys_and_missing_directory_are_rejected(tmp_path: Path) -> None:
    """Один ключ — одна статья; несуществующий каталог — понятная ошибка."""
    write(tmp_path, "a.md", HEADER + "Первая.")
    write(tmp_path, "b.md", HEADER + "Вторая.")
    with pytest.raises(KnowledgeSourceError, match="повторяется"):
        load_documents(tmp_path)
    with pytest.raises(KnowledgeSourceError, match="не найден"):
        load_documents(tmp_path / "missing")
