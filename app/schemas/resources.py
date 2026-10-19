"""Схемы этапа 03: принимаем только разрешённые поля и проверенные значения."""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    model_validator,
)

# PostgreSQL BIGINT ограничен сверху: неверный id отсекаем до SQL-запроса.
MAX_ID = 2**63 - 1
PositiveId = Annotated[int, Field(gt=0, le=MAX_ID)]
# JSON должен содержать целое число: true, 1.0 и строка "1" не являются id.
StrictPositiveId = Annotated[int, Field(gt=0, le=MAX_ID, strict=True)]
Name = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


def product_code_not_reserved(value: str) -> str:
    """Не смешивать код продукта со служебной областью общих статей Qdrant."""
    if value == "general":
        raise ValueError("Код general зарезервирован для общих статей базы знаний")
    return value


ProductCode = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
    AfterValidator(product_code_not_reserved),
]
LicenseKey = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]
Subject = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)
]
Description = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000)
]
LicenseStatus = Literal["active", "expired", "revoked"]
TicketPriority = Literal["low", "medium", "high", "critical"]
TicketStatus = Literal["open", "in_progress", "waiting_customer", "resolved", "closed"]
# Почему удалены тексты обращения: запрос клиента или истёкший срок хранения.
AnonymizationReason = Literal["customer_request", "retention"]
# Закрытое обращение не анализируется и не получает доставку ответа.
CLOSED_TICKET_STATUSES = frozenset({"resolved", "closed"})
UserRole = Literal["operator", "reviewer", "admin"]
# Лестница прав: каждая следующая роль умеет всё, что предыдущая.
# operator работает с обращениями, reviewer ещё и решает по предложениям агента,
# admin также управляет сотрудниками и их токенами.
ROLE_LEVELS: dict[str, int] = {"operator": 1, "reviewer": 2, "admin": 3}


class CreateSchema(BaseModel):
    """Запретить неожиданные поля и убрать внешние пробелы из строк запроса."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReadSchema(BaseModel):
    """Читать только объявленные поля из ORM-объекта без выдачи внутренностей БД."""

    model_config = ConfigDict(from_attributes=True)


class CustomerCreate(CreateSchema):
    """Данные нового клиента; идентификатор и дату создаёт PostgreSQL."""

    name: Name
    email: Annotated[EmailStr, Field(max_length=320)]
    company: Name | None = None


class CustomerRead(ReadSchema):
    """Публичные поля сохранённого клиента."""

    id: int
    name: str
    email: str
    company: str | None
    created_at: datetime
    # Не NULL: клиент обезличен, имя и email — заглушки, а не его данные.
    erased_at: datetime | None = None


class ProductCreate(CreateSchema):
    """Данные продукта с уникальным кодом и необязательным описанием."""

    name: Name
    code: ProductCode
    description: Description | None = None


class ProductRead(ReadSchema):
    """Публичные поля сохранённого продукта."""

    id: int
    name: str
    code: str
    description: str | None
    created_at: datetime


class LicenseCreate(CreateSchema):
    """Проверить ссылки, статус и согласованность дат новой лицензии."""

    customer_id: StrictPositiveId
    product_id: StrictPositiveId
    license_key: LicenseKey
    status: LicenseStatus = "active"
    # Фабрика выполняется для каждого запроса; время импорта не сохраняется.
    issued_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_dates(self) -> Self:
        """Отклонить истечение раньше выдачи ещё до обращения к PostgreSQL."""
        if self.expires_at is not None and self.expires_at < self.issued_at:
            raise ValueError("expires_at не может быть раньше issued_at")
        return self


class LicenseRead(ReadSchema):
    """Публичные поля лицензии с датами, содержащими часовой пояс."""

    id: int
    customer_id: int
    product_id: int
    license_key: str
    status: LicenseStatus
    issued_at: datetime
    expires_at: datetime | None


class TicketCreate(CreateSchema):
    """Данные обращения; начальный статус open задаёт сервис, а не клиент."""

    customer_id: StrictPositiveId
    product_id: StrictPositiveId
    subject: Subject
    description: Description
    priority: TicketPriority = "medium"


class TicketRead(ReadSchema):
    """Публичные поля обращения и времена его создания и изменения."""

    id: int
    customer_id: int
    product_id: int
    subject: str
    description: str
    priority: TicketPriority
    status: TicketStatus
    created_at: datetime
    updated_at: datetime
    # Не NULL: тексты удалены по запросу клиента или по сроку хранения.
    anonymized_at: datetime | None = None
    anonymized_reason: AnonymizationReason | None = None


class UserCreate(CreateSchema):
    """Сотрудник поддержки; доступ к API даёт отдельно выданный токен."""

    name: Name
    email: Annotated[EmailStr, Field(max_length=320)]
    role: UserRole = "operator"


class UserRead(ReadSchema):
    """Публичные поля сотрудника; решения принимают роли reviewer и admin."""

    id: int
    name: str
    email: str
    role: UserRole
    is_active: bool
    created_at: datetime


class ErrorRead(ReadSchema):
    """Безопасная форма ожидаемой ошибки 401, 403, 404, 409, 429 или 503."""

    detail: str
