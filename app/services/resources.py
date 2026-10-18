"""Чтение и создание клиентов, продуктов, лицензий, обращений и сотрудников.

Маршруты передают сюда проверенные схемы и сессию запроса. Сервис явно
фиксирует запись и переводит только известные ограничения PostgreSQL
в прикладную ошибку; остальные сбои остаются ошибками доступности БД.
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import Customer, License, Product, Ticket, User
from app.schemas.resources import (
    CustomerCreate,
    LicenseCreate,
    ProductCreate,
    TicketCreate,
    TicketStatus,
    UserCreate,
)
from app.services.errors import ResourceConflict, ResourceNotFound


def _persist[Entity: (Customer, Product, License, Ticket, User)](
    session: Session, entity: Entity
) -> Entity:
    """Сохранить одну запись и загрузить созданные БД id и времена.

    Откат обязателен и при ошибке refresh: после неуспешного SQL нельзя
    отдавать вызывающему коду сессию с незавершённой ошибочной транзакцией.
    Если commit уже прошёл, rollback не отменит подтверждённую запись.
    """
    try:
        session.add(entity)
        session.commit()
        session.refresh(entity)
    except IntegrityError as error:
        session.rollback()
        # Код PostgreSQL проверяем без разбора текста, где могут быть секреты.
        sqlstate = getattr(error.orig, "sqlstate", None)
        if sqlstate == "23505":
            raise ResourceConflict(
                "Запись с таким уникальным значением уже существует"
            ) from None
        if sqlstate == "23503":
            # Ссылка могла исчезнуть между проверкой и INSERT в другом запросе.
            raise ResourceNotFound("Указанный клиент или продукт не найден") from None
        raise
    except SQLAlchemyError:
        session.rollback()
        raise
    return entity


def _get[Entity: (Customer, Product, License, Ticket, User)](
    session: Session, model: type[Entity], resource_id: int, message: str
) -> Entity:
    """Найти запись по первичному ключу или вернуть понятную ошибку ресурса."""
    entity = session.get(model, resource_id)
    if entity is None:
        raise ResourceNotFound(message)
    return entity


def _require_references(session: Session, customer_id: int, product_id: int) -> None:
    """Проверить внешние ключи до вставки и назвать отсутствующую сущность."""
    get_customer(session, customer_id)
    get_product(session, product_id)


def create_customer(session: Session, payload: CustomerCreate) -> Customer:
    """Создать клиента, не меняя уже существующую запись с таким email."""
    return _persist(session, Customer(**payload.model_dump()))


def list_customers(
    session: Session, *, offset: int = 0, limit: int = 20
) -> list[Customer]:
    """Прочитать ограниченную страницу клиентов в устойчивом порядке id."""
    statement = select(Customer).order_by(Customer.id).offset(offset).limit(limit)
    return list(session.scalars(statement))


def get_customer(session: Session, resource_id: int) -> Customer:
    """Вернуть одного клиента или ошибку отсутствующего ресурса."""
    return _get(session, Customer, resource_id, "Клиент не найден")


def create_product(session: Session, payload: ProductCreate) -> Product:
    """Создать продукт; повторяющийся код приводит к конфликту, а не замене."""
    return _persist(session, Product(**payload.model_dump()))


def list_products(
    session: Session, *, offset: int = 0, limit: int = 20
) -> list[Product]:
    """Прочитать страницу продуктов, отсортированную по id по возрастанию."""
    statement = select(Product).order_by(Product.id).offset(offset).limit(limit)
    return list(session.scalars(statement))


def get_product(session: Session, resource_id: int) -> Product:
    """Вернуть один продукт или ошибку отсутствующего ресурса."""
    return _get(session, Product, resource_id, "Продукт не найден")


def create_license(session: Session, payload: LicenseCreate) -> License:
    """Проверить клиента и продукт и сохранить лицензию одной транзакцией."""
    _require_references(session, payload.customer_id, payload.product_id)
    return _persist(session, License(**payload.model_dump()))


def list_licenses(
    session: Session,
    *,
    offset: int = 0,
    limit: int = 20,
    customer_id: int | None = None,
    product_id: int | None = None,
    license_key: str | None = None,
) -> list[License]:
    """Прочитать страницу лицензий по совместно применённым точным фильтрам."""
    statement = select(License)
    if customer_id is not None:
        statement = statement.where(License.customer_id == customer_id)
    if product_id is not None:
        statement = statement.where(License.product_id == product_id)
    if license_key is not None:
        statement = statement.where(License.license_key == license_key)
    # Даже при пагинации результат повторяем при неизменном наборе данных.
    statement = statement.order_by(License.id).offset(offset).limit(limit)
    return list(session.scalars(statement))


def get_license(session: Session, resource_id: int) -> License:
    """Вернуть одну лицензию по её внутреннему id."""
    return _get(session, License, resource_id, "Лицензия не найдена")


def create_ticket(session: Session, payload: TicketCreate) -> Ticket:
    """Создать обращение с серверным начальным статусом open."""
    _require_references(session, payload.customer_id, payload.product_id)
    # Клиент не может управлять процессом обработки через скрытое поле status.
    return _persist(session, Ticket(**payload.model_dump(), status="open"))


def list_tickets(
    session: Session,
    *,
    offset: int = 0,
    limit: int = 20,
    customer_id: int | None = None,
    product_id: int | None = None,
    status: TicketStatus | None = None,
) -> list[Ticket]:
    """Прочитать страницу обращений по клиенту, продукту и текущему статусу."""
    statement = select(Ticket)
    if customer_id is not None:
        statement = statement.where(Ticket.customer_id == customer_id)
    if product_id is not None:
        statement = statement.where(Ticket.product_id == product_id)
    if status is not None:
        statement = statement.where(Ticket.status == status)
    statement = statement.order_by(Ticket.id).offset(offset).limit(limit)
    return list(session.scalars(statement))


def get_ticket(session: Session, resource_id: int) -> Ticket:
    """Вернуть одно обращение или ошибку отсутствующего ресурса."""
    return _get(session, Ticket, resource_id, "Обращение не найдено")


def create_user(session: Session, payload: UserCreate) -> User:
    """Создать сотрудника; повторяющийся email приводит к конфликту 409."""
    return _persist(session, User(**payload.model_dump()))


def list_users(session: Session, *, offset: int = 0, limit: int = 20) -> list[User]:
    """Прочитать страницу сотрудников в порядке id."""
    statement = select(User).order_by(User.id).offset(offset).limit(limit)
    return list(session.scalars(statement))


def get_user(session: Session, resource_id: int) -> User:
    """Вернуть сотрудника или ошибку отсутствующего ресурса."""
    return _get(session, User, resource_id, "Сотрудник не найден")
