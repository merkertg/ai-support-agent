"""Безопасные ошибки базы знаний: без URL, ключей, текстов запросов и ответов SDK.

Классы наследуют LLMError, чтобы общий обработчик API возвращал только
постоянный статус и текст. Исходная ошибка Qdrant или OpenAI наружу не выдаётся.
"""

from app.llm.errors import LLMError


class KnowledgeError(LLMError):
    """Общий публичный текст для ожидаемой ошибки поиска или индексации."""

    status_code = 503
    detail = "База знаний временно недоступна."


class KnowledgeConfigurationError(KnowledgeError):
    """Сообщить о неполных или неверных настройках Qdrant и эмбеддингов."""

    detail = "Проверьте настройки базы знаний: Qdrant, коллекция и эмбеддинги."


class KnowledgeUnavailableError(KnowledgeError):
    """Сообщить о недоступности Qdrant или сервиса эмбеддингов."""

    detail = "База знаний временно недоступна. Повторите запрос позже."


class KnowledgeIndexMismatchError(KnowledgeError):
    """Отклонить коллекцию с другой размерностью векторов."""

    detail = (
        "Индекс базы знаний создан с другой размерностью эмбеддингов. "
        "Переиндексируйте документы в новую коллекцию."
    )


class KnowledgeIndexMissingError(KnowledgeError):
    """Сообщить, что коллекция ещё не создана командой индексации."""

    detail = (
        "Индекс базы знаний ещё не создан. Выполните python -m app.knowledge.ingest."
    )


class KnowledgeTimeoutError(KnowledgeError):
    """Сообщить, что Qdrant или эмбеддинги не ответили вовремя."""

    status_code = 504
    detail = "База знаний не ответила за установленное время."


class KnowledgeInvalidResponseError(KnowledgeError):
    """Отклонить ответ эмбеддингов неверного размера или формата."""

    status_code = 502
    detail = "Сервис эмбеддингов вернул некорректный ответ."


class KnowledgeSourceError(Exception):
    """Описать ошибку исходного Markdown-файла при индексации.

    Это ошибка автора статьи, а не HTTP-ответ: сообщение содержит путь и
    причину, но не текст файла целиком.
    """
