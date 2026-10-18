"""Эмбеддинги без сети: mock-вектор и настоящий OpenAI SDK с подменой транспорта."""

import json
import math
from collections.abc import Callable, Iterator

import httpx2
import openai
import pytest

from app.knowledge.config import KnowledgeSettings, get_knowledge_settings
from app.knowledge.embeddings import (
    MockEmbedder,
    OpenAIEmbedder,
    get_embedder,
    is_meaningful,
    stems,
)
from app.knowledge.errors import (
    KnowledgeConfigurationError,
    KnowledgeInvalidResponseError,
    KnowledgeTimeoutError,
    KnowledgeUnavailableError,
)
from app.llm.config import LLMSettings, get_llm_settings

FAKE_KEY = "test-placeholder-never-a-real-key"
ENV_FIELDS = (
    "LLM_PROVIDER",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "OPENAI_EMBEDDING_MODEL",
    "EMBEDDING_PROVIDER",
    "EMBEDDING_DIMENSIONS",
    "QDRANT_URL",
    "QDRANT_COLLECTION",
    "KNOWLEDGE_MIN_SCORE",
)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path) -> Iterator[None]:
    """Не читать локальный .env и ключ пользователя."""
    for name in ENV_FIELDS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    get_llm_settings.cache_clear()
    get_knowledge_settings.cache_clear()
    yield
    get_llm_settings.cache_clear()
    get_knowledge_settings.cache_clear()


def cosine(left: list[float], right: list[float]) -> float:
    """Посчитать косинусное сходство двух нормированных векторов."""
    return sum(a * b for a, b in zip(left, right, strict=True))


def test_stems_normalize_case_yo_and_word_forms() -> None:
    """Основы слов объединяют формы и регистр, короткие слова отбрасываются."""
    assert stems("Ошибка ОШИБКЕ ошибки") == ["ошибк", "ошибк", "ошибк"]
    assert stems("Её в на") == []
    assert stems("Сервер ёлка") == ["серве", "елка"]
    assert is_meaningful("сервер")
    assert not is_meaningful("и в на для что ещё")


def test_mock_vectors_are_deterministic_normalized_and_lexical() -> None:
    """Одинаковый текст — одинаковый вектор; общие слова повышают сходство."""
    embedder = MockEmbedder(256)
    first, second, unrelated = embedder.embed(
        [
            "Сервер не запускается после обновления",
            "после обновления сервер перестал запускаться",
            "сброс пароля пользователя",
        ]
    )
    assert first == embedder.vector("Сервер не запускается после обновления")
    assert len(first) == 256
    assert math.isclose(math.sqrt(sum(value * value for value in first)), 1.0)
    assert cosine(first, second) > 0.6
    assert cosine(first, second) > cosine(first, unrelated) + 0.4
    assert embedder.signature == "mock:mock-hash-v1:256"


def test_mock_vector_of_text_without_words_is_zero() -> None:
    """Текст без значимых слов не превращается в случайный вектор."""
    assert MockEmbedder(64).vector("!!! и в на") == [0.0] * 64


def settings(**overrides) -> KnowledgeSettings:
    """Настроить OpenAI-эмбеддинги с явной тестовой моделью."""
    values = {
        "embedding_provider": "openai",
        "openai_embedding_model": "explicit-test-embedding",
        "embedding_dimensions": 64,
        "qdrant_url": "http://qdrant.test:6333",
    }
    values.update(overrides)
    return KnowledgeSettings(_env_file=None, **values)


def llm_settings(**overrides) -> LLMSettings:
    """Использовать заведомо ненастоящий ключ и короткий тайм-аут."""
    values = {"openai_api_key": FAKE_KEY, "llm_timeout_seconds": 7}
    values.update(overrides)
    return LLMSettings(_env_file=None, **values)


def embedding_payload(vectors: list[list[float]], order: list[int] | None = None):
    """Собрать ответ /v1/embeddings в официальном формате."""
    indexes = order if order is not None else list(range(len(vectors)))
    return {
        "object": "list",
        "model": "explicit-test-embedding",
        "data": [
            {"object": "embedding", "index": index, "embedding": vectors[index]}
            for index in indexes
        ],
        "usage": {"prompt_tokens": 3, "total_tokens": 3},
    }


def sdk_factory_for(
    handler: Callable, clients: list, options: list
) -> Callable[..., openai.OpenAI]:
    """Подменить только транспорт: сериализация и разбор остаются настоящими."""

    def factory(**kwargs) -> openai.OpenAI:
        """Закрепить локальный обработчик вместо сети."""
        client = httpx2.Client(transport=httpx2.MockTransport(handler))
        clients.append(client)
        options.append(kwargs)
        return openai.OpenAI(http_client=client, **kwargs)

    return factory


def test_openai_embeddings_send_model_dimensions_and_keep_order() -> None:
    """Запрос содержит модель и размерность; ответ сортируется по index."""
    bodies, clients, options = [], [], []
    first, second = [0.1] * 64, [0.2] * 64

    def handler(request) -> httpx2.Response:
        """Вернуть векторы в обратном порядке, как может сделать API."""
        bodies.append(json.loads(request.content))
        return httpx2.Response(
            200, json=embedding_payload([first, second], order=[1, 0])
        )

    embedder = OpenAIEmbedder(
        settings(),
        llm_settings(),
        sdk_factory=sdk_factory_for(handler, clients, options),
    )
    assert embedder.embed(["первый", "второй"]) == [first, second]
    assert bodies == [
        {
            "model": "explicit-test-embedding",
            "input": ["первый", "второй"],
            "dimensions": 64,
            "encoding_format": "float",
        }
    ]
    assert options[0]["max_retries"] == 0
    assert options[0]["timeout"] == 7
    assert options[0]["base_url"] == "https://api.openai.com/v1"
    assert all(client.is_closed for client in clients)
    assert embedder.signature == "openai:explicit-test-embedding:64"


def test_openai_embeddings_are_sent_in_batches_of_64() -> None:
    """Большой список текстов разбивается на несколько запросов."""
    sizes = []

    def handler(request) -> httpx2.Response:
        """Вернуть по вектору на каждый текст пакета."""
        texts = json.loads(request.content)["input"]
        sizes.append(len(texts))
        return httpx2.Response(200, json=embedding_payload([[0.5] * 64] * len(texts)))

    embedder = OpenAIEmbedder(
        settings(), llm_settings(), sdk_factory=sdk_factory_for(handler, [], [])
    )
    assert len(embedder.embed([f"текст {index}" for index in range(130)])) == 130
    assert sizes == [64, 64, 2]


@pytest.mark.parametrize(
    "body",
    [
        embedding_payload([[0.1] * 63]),
        embedding_payload([[0.1] * 64, [0.1] * 64]),
        {"object": "list", "data": [], "model": "m", "usage": {}},
    ],
)
def test_wrong_vector_count_or_size_is_invalid(body) -> None:
    """Неверный размер или количество векторов не попадает в Qdrant."""
    embedder = OpenAIEmbedder(
        settings(),
        llm_settings(),
        sdk_factory=sdk_factory_for(
            lambda request: httpx2.Response(200, json=body), [], []
        ),
    )
    with pytest.raises(KnowledgeInvalidResponseError):
        embedder.embed(["текст"])


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, KnowledgeConfigurationError),
        (401, KnowledgeConfigurationError),
        (404, KnowledgeConfigurationError),
        (429, KnowledgeUnavailableError),
        (500, KnowledgeUnavailableError),
    ],
)
def test_http_errors_are_safe_and_not_retried(status: int, error: type) -> None:
    """Ошибка API становится постоянным сообщением без тела ответа и повторов."""
    calls = []

    def handler(request) -> httpx2.Response:
        """Вернуть ошибку с секретом в теле, которое нельзя показывать."""
        calls.append(request)
        return httpx2.Response(status, json={"error": {"message": FAKE_KEY}})

    embedder = OpenAIEmbedder(
        settings(), llm_settings(), sdk_factory=sdk_factory_for(handler, [], [])
    )
    with pytest.raises(error) as raised:
        embedder.embed(["текст"])
    assert FAKE_KEY not in str(raised.value)
    assert len(calls) == 1


def test_timeout_is_safe() -> None:
    """Тайм-аут транспорта превращается в безопасный 504."""

    def handler(request) -> httpx2.Response:
        """Имитировать истёкший срок ожидания ответа."""
        raise httpx2.ReadTimeout("timeout", request=request)

    embedder = OpenAIEmbedder(
        settings(), llm_settings(), sdk_factory=sdk_factory_for(handler, [], [])
    )
    with pytest.raises(KnowledgeTimeoutError):
        embedder.embed(["текст"])


@pytest.mark.parametrize(
    ("knowledge", "llm"),
    [
        ({"openai_embedding_model": None}, {}),
        ({}, {"openai_api_key": None}),
    ],
)
def test_openai_requires_explicit_model_and_key(knowledge: dict, llm: dict) -> None:
    """Без явной модели или ключа адаптер не создаётся."""
    with pytest.raises(KnowledgeConfigurationError):
        OpenAIEmbedder(settings(**knowledge), llm_settings(**llm))


def test_factory_defaults_to_mock_and_masks_invalid_environment(
    monkeypatch,
) -> None:
    """По умолчанию mock; неверное значение окружения не раскрывается."""
    assert isinstance(get_embedder(KnowledgeSettings(_env_file=None)), MockEmbedder)
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "secret-not-a-number")
    with pytest.raises(KnowledgeConfigurationError) as raised:
        get_knowledge_settings()
    assert "secret-not-a-number" not in str(raised.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("qdrant_url", "ftp://qdrant"),
        ("qdrant_collection", "bad name!"),
        ("embedding_dimensions", 32),
        ("knowledge_min_score", 1.5),
        ("qdrant_timeout_seconds", 0),
    ],
)
def test_settings_reject_unsafe_values(field: str, value) -> None:
    """Адрес, имя коллекции, размерность и порог проверяются до сети."""
    with pytest.raises(ValueError):
        KnowledgeSettings(_env_file=None, **{field: value})


def test_empty_qdrant_url_means_not_configured() -> None:
    """Пустая строка из .env означает отсутствие Qdrant, а не адрес ''."""
    assert KnowledgeSettings(_env_file=None, qdrant_url="  ").qdrant_url is None
