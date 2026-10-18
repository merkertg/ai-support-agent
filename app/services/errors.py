"""Предсказуемые ошибки предметной области без SQL и HTTP-зависимостей."""


class ResourceError(Exception):
    """Передать безопасное описание ожидаемой ошибки обработчику API."""

    status_code = 400

    def __init__(self, detail: str) -> None:
        """Сохранить сообщение, не добавляя исключение драйвера или SQL."""
        self.detail = detail
        super().__init__(detail)


class ResourceNotFound(ResourceError):
    """Сообщить, что сущность или указанная клиентом связь не найдена."""

    status_code = 404


class ResourceConflict(ResourceError):
    """Сообщить о конфликте уникального значения с уже сохранённой записью."""

    status_code = 409
