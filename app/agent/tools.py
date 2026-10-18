"""Разрешённые инструменты агента: только чтение данных текущего обращения.

Имя функции и JSON от модели никогда не превращаются в Python-код или SQL.
Реестр задаёт пять функций, строгие аргументы и серверную область доступа:
четыре читают PostgreSQL, search_knowledge ищет статьи базы знаний в Qdrant.
"""

import json
from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.agent.contracts import AgentContext
from app.knowledge.documents import GENERAL_SCOPE
from app.knowledge.errors import KnowledgeError
from app.knowledge.index import IndexFactory
from app.llm.errors import LLMError
from app.models import Customer, License, Product, Ticket
from app.schemas.knowledge import SearchQuery
from app.schemas.resources import StrictPositiveId

MAX_ARGUMENT_CHARACTERS = 4096
MAX_TOOL_RESULT_CHARACTERS = 16_000
# Фрагмент статьи для модели короче полного: пять источников помещаются в лимит.
MAX_SOURCE_CHARACTERS = 800


class ToolArguments(BaseModel):
    """Отклонить опечатки, лишние поля и неявное приведение типов аргументов."""

    model_config = ConfigDict(extra="forbid", strict=True)


class TicketArguments(ToolArguments):
    """Принять только положительный целый ID текущего обращения."""

    ticket_id: StrictPositiveId


class CustomerArguments(ToolArguments):
    """Принять ID клиента, который затем сверяется с серверной областью."""

    customer_id: StrictPositiveId


class LicenseArguments(ToolArguments):
    """Принять непустой ключ лицензии; поиск остаётся в рамках клиента/продукта."""

    license_key: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
    ]


class HistoryArguments(CustomerArguments):
    """Ограничить количество предыдущих обращений пятью записями."""

    limit: int = Field(ge=1, le=5, strict=True)


class SearchArguments(ToolArguments):
    """Принять текст поиска; продукт для фильтра сервер берёт из обращения."""

    query: SearchQuery
    limit: int = Field(ge=1, le=5, strict=True)


ARGUMENT_SCHEMAS = {
    "get_ticket": TicketArguments,
    "get_customer": CustomerArguments,
    "get_license": LicenseArguments,
    "get_ticket_history": HistoryArguments,
    "search_knowledge": SearchArguments,
}
TOOL_DESCRIPTIONS = {
    "get_ticket": "Прочитать текущее обращение и его продукт по ticket_id.",
    "get_customer": "Прочитать имя и компанию клиента текущего обращения без email.",
    "get_license": "Проверить указанный ключ только для клиента и продукта обращения.",
    "get_ticket_history": "Прочитать до пяти прежних обращений клиента по продукту.",
    "search_knowledge": (
        "Найти до пяти статей базы знаний для продукта обращения и общих статей. "
        "В sources черновика можно указывать только document_key из результата."
    ),
}
# В strict-схемах все параметры обязательны, additionalProperties запрещены.
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "name": name,
        "description": TOOL_DESCRIPTIONS[name],
        "parameters": schema.model_json_schema(),
        "strict": True,
    }
    for name, schema in ARGUMENT_SCHEMAS.items()
]


class ToolDataUnavailable(LLMError):
    """Прервать анализ безопасной ошибкой при недоступности источника данных."""

    status_code = 503
    detail = "Данные инструмента временно недоступны."


def tool_error(code: str, message: str) -> dict:
    """Вернуть модели предсказуемую ошибку без исходного исключения или SQL."""
    return {"ok": False, "error": {"code": code, "message": message}}


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    """Запретить неоднозначный JSON с повторяющимися ключами даже во вложениях."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class SQLToolExecutor:
    """Открывать отдельную короткую read-only транзакцию для каждого инструмента.

    AgentContext создаёт сервер из уже найденного обращения. Модель не может
    заменить область доступа произвольным customer_id или product_id.
    """

    def __init__(
        self,
        engine: Engine,
        context: AgentContext,
        *,
        checked_at: datetime | None = None,
        knowledge: IndexFactory | None = None,
    ) -> None:
        """Зафиксировать доверенную область и момент проверки срока лицензии.

        knowledge открывает индекс базы знаний только для search_knowledge;
        без фабрики поиск возвращает модели ошибку инструмента.
        """
        self.engine = engine
        self.context = context
        self.checked_at = checked_at or datetime.now(UTC)
        self.knowledge = knowledge

    def __call__(self, name: str, arguments: str) -> tuple[dict, dict]:
        """Проверить имя/JSON/типы/область прежде запроса к PostgreSQL.

        Ошибка аргументов — результат инструмента, позволяющий модели
        исправить вызов. Ошибка БД прерывает запуск, поскольку данные
        нельзя считать проверенными. Оба случая не раскрывают SQL и URL.
        """
        schema = ARGUMENT_SCHEMAS.get(name)
        if schema is None:
            return {}, tool_error("unknown_tool", "Такой инструмент не разрешён.")
        try:
            if (
                not isinstance(arguments, str)
                or len(arguments) > MAX_ARGUMENT_CHARACTERS
            ):
                raise ValueError("arguments length")
            payload = json.loads(arguments, object_pairs_hook=unique_object)
            validated = schema.model_validate(payload)
        except (ValueError, TypeError, RecursionError, ValidationError):
            return {}, tool_error("invalid_arguments", "Аргументы инструмента неверны.")
        safe_arguments = validated.model_dump()
        # В журнале не сохраняем сырой ключ; для SQL используется исходное значение.
        trace_arguments = dict(safe_arguments)
        if "license_key" in trace_arguments:
            trace_arguments["license_key"] = "***"
        requested_ticket = safe_arguments.get("ticket_id", self.context.ticket_id)
        requested_customer = safe_arguments.get("customer_id", self.context.customer_id)
        if (
            requested_ticket != self.context.ticket_id
            or requested_customer != self.context.customer_id
        ):
            return trace_arguments, tool_error(
                "out_of_scope", "Доступ вне области обращения запрещён."
            )
        try:
            with Session(self.engine) as session:
                # Запрет записи действует на уровне PostgreSQL, а не только Python.
                session.execute(text("SET TRANSACTION READ ONLY"))
                session.execute(text("SET LOCAL statement_timeout = '3s'"))
                session.execute(text("SET LOCAL lock_timeout = '3s'"))
                ticket = session.get(Ticket, self.context.ticket_id)
                if ticket is None:
                    return trace_arguments, tool_error(
                        "not_found", "Обращение не найдено."
                    )
                if (
                    ticket.customer_id != self.context.customer_id
                    or ticket.product_id != self.context.product_id
                ):
                    return trace_arguments, tool_error(
                        "stale_scope", "Область обращения изменилась."
                    )
                # Диспетчер закрыт: нет getattr, eval или имён таблиц от модели.
                if name == "search_knowledge":
                    product = session.get(Product, self.context.product_id)
                    if product is None:
                        return trace_arguments, tool_error(
                            "not_found", "Продукт обращения не найден."
                        )
                    # Код копируем: поиск в Qdrant идёт уже после закрытия сессии.
                    product_code = product.code
                    result = None
                elif name == "get_ticket":
                    result = self._ticket(session, ticket)
                elif name == "get_customer":
                    result = self._customer(session)
                elif name == "get_license":
                    result = self._license(session, safe_arguments["license_key"])
                else:
                    result = self._history(session, safe_arguments["limit"])
        except SQLAlchemyError:
            raise ToolDataUnavailable() from None
        if name == "search_knowledge":
            # SQL-соединение уже возвращено в пул до запроса эмбеддингов и Qdrant.
            result = self._search(
                safe_arguments["query"], product_code, safe_arguments["limit"]
            )
        # Результат уже состоит из обычных JSON-значений; ORM остаётся в сессии.
        if len(json.dumps(result, ensure_ascii=False)) > MAX_TOOL_RESULT_CHARACTERS:
            return trace_arguments, tool_error(
                "result_too_large", "Результат инструмента слишком большой."
            )
        return trace_arguments, result

    def _search(self, query: str, product_code: str, limit: int) -> dict:
        """Найти статьи продукта обращения и общие; сбой поиска — ошибка инструмента.

        Недоступная база знаний не прерывает анализ: модель узнаёт об ошибке
        и обязана ответить без ссылок на источники.
        """
        if self.knowledge is None:
            return tool_error("knowledge_unavailable", "База знаний не настроена.")
        try:
            with self.knowledge() as index:
                hits = index.search(
                    query, [product_code, GENERAL_SCOPE], limit, per_document=True
                )
                signature = index.embedder.signature
        except KnowledgeError:
            return tool_error(
                "knowledge_unavailable", "База знаний временно недоступна."
            )
        return {
            "ok": True,
            "data": {
                "documents": [
                    {
                        "document_key": hit.document_key,
                        "title": hit.title,
                        "section": hit.section,
                        "text": hit.text[:MAX_SOURCE_CHARACTERS],
                        "score": hit.score,
                        "source_path": hit.source_path,
                    }
                    for hit in hits
                ],
                "scope": [product_code, GENERAL_SCOPE],
                "embedding_signature": signature,
            },
        }

    def _ticket(self, session: Session, ticket: Ticket) -> dict:
        """Выдать текст текущего обращения и ограниченные сведения о продукте."""
        product = session.get(Product, self.context.product_id)
        if product is None:
            return tool_error("not_found", "Продукт обращения не найден.")
        return {
            "ok": True,
            "data": {
                "ticket": {
                    "id": ticket.id,
                    "customer_id": ticket.customer_id,
                    "product_id": ticket.product_id,
                    "subject": ticket.subject,
                    "description": ticket.description,
                    "priority": ticket.priority,
                    "status": ticket.status,
                    "product": {
                        "id": product.id,
                        "name": product.name,
                        "code": product.code,
                    },
                }
            },
        }

    def _customer(self, session: Session) -> dict:
        """Прочитать только имя и компанию текущего клиента, исключив email."""
        customer = session.get(Customer, self.context.customer_id)
        if customer is None:
            return tool_error("not_found", "Клиент обращения не найден.")
        return {
            "ok": True,
            "data": {
                "customer": {
                    "id": customer.id,
                    "name": customer.name,
                    "company": customer.company,
                }
            },
        }

    def _license(self, session: Session, license_key: str) -> dict:
        """Проверить ключ только внутри текущих клиента/продукта и вычислить срок."""
        record = session.scalar(
            select(License).where(
                License.license_key == license_key,
                License.customer_id == self.context.customer_id,
                License.product_id == self.context.product_id,
            )
        )
        if record is None:
            # Не сообщаем, существует ли этот ключ у другого клиента/продукта.
            return tool_error("not_found", "Лицензия в области обращения не найдена.")
        valid = (
            record.status == "active"
            and record.issued_at <= self.checked_at
            and (record.expires_at is None or record.expires_at > self.checked_at)
        )
        return {
            "ok": True,
            "data": {
                "license": {
                    "id": record.id,
                    "status": record.status,
                    "customer_id": record.customer_id,
                    "product_id": record.product_id,
                    "issued_at": record.issued_at.isoformat(),
                    "expires_at": record.expires_at.isoformat()
                    if record.expires_at
                    else None,
                },
                "valid": valid,
                "checked_at": self.checked_at.isoformat(),
            },
        }

    def _history(self, session: Session, limit: int) -> dict:
        """Прочитать прежние обращения по продукту без полного текста описаний."""
        records = session.scalars(
            select(Ticket)
            .where(
                Ticket.customer_id == self.context.customer_id,
                Ticket.product_id == self.context.product_id,
                Ticket.id != self.context.ticket_id,
            )
            .order_by(Ticket.id.desc())
            .limit(limit)
        )
        return {
            "ok": True,
            "data": {
                "tickets": [
                    {
                        "id": row.id,
                        "subject": row.subject,
                        "status": row.status,
                        "priority": row.priority,
                        "created_at": row.created_at.isoformat(),
                    }
                    for row in records
                ],
                "limit": limit,
            },
        }
