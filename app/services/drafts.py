"""Подготовка черновика из сохранённого обращения без изменения данных БД."""

from collections.abc import Callable

from sqlalchemy.orm import Session

from app.llm.client import DraftClient, get_draft_client
from app.models import Product, Ticket
from app.schemas.drafts import DraftContext, DraftResponse
from app.services.errors import ResourceNotFound

# Фабрика вызывается только после чтения обращения: неверный id даёт 404
# даже тогда, когда оператор ещё не настроил внешний LLM-провайдер.
ClientFactory = Callable[[], DraftClient]


def create_draft(
    session: Session,
    resource_id: int,
    client_factory: ClientFactory = get_draft_client,
) -> DraftResponse:
    """Скопировать разрешённый контекст, освободить БД и получить черновик.

    В модель передаются только текст обращения, приоритет и название/код
    продукта. Клиента и лицензию здесь не читаем. Пользовательский текст
    остаётся данными для LLM: сервис не выполняет содержащиеся в нём команды.
    """
    ticket = session.get(Ticket, resource_id)
    if ticket is None:
        raise ResourceNotFound("Обращение не найдено")
    product = session.get(Product, ticket.product_id)
    if product is None:
        # В нормальной схеме это запрещает FK; проверка защищает старые данные.
        raise ResourceNotFound("Продукт обращения не найден")
    context = DraftContext(
        ticket_id=ticket.id,
        subject=ticket.subject,
        description=ticket.description,
        priority=ticket.priority,
        product_name=product.name,
        product_code=product.code,
    )
    # SELECT уже открыл транзакцию SQLAlchemy. Копия контекста не зависит
    # от ORM, поэтому rollback завершает чтение и возвращает соединение в пул
    # до медленного HTTP-вызова. Здесь нет неподтверждённых пользовательских
    # изменений: сессия принадлежит только текущему HTTP-запросу черновика.
    session.rollback()
    result = client_factory().generate(context)
    return DraftResponse(ticket_id=resource_id, **result.model_dump())
