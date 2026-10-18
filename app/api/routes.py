"""Тонкие HTTP-маршруты: проверка параметров, вызов сервиса, схема ответа."""

from typing import Annotated

from fastapi import APIRouter, Path, Query, status

from app.api.dependencies import ApiSession
from app.models import Customer, License, Product, Ticket
from app.schemas.resources import (
    MAX_ID,
    CustomerCreate,
    CustomerRead,
    ErrorRead,
    LicenseCreate,
    LicenseKey,
    LicenseRead,
    PositiveId,
    ProductCreate,
    ProductRead,
    TicketCreate,
    TicketRead,
    TicketStatus,
)
from app.services import resources

# Описываем ожидаемые ошибки в Swagger той же схемой, что возвращают handlers.
router = APIRouter(
    prefix="/api",
    responses={
        404: {
            "model": ErrorRead,
            "description": "Ресурс или указанная ссылка не найдены",
        },
        409: {"model": ErrorRead, "description": "Конфликт уникального значения"},
        503: {"model": ErrorRead, "description": "База данных временно недоступна"},
    },
)

# Общие ограничения вынесены в типы: каждый список имеет одинаковые границы.
Offset = Annotated[int, Query(ge=0, le=MAX_ID)]
Limit = Annotated[int, Query(ge=1, le=100)]
ResourceId = Annotated[PositiveId, Path()]
FilterId = Annotated[PositiveId | None, Query()]


@router.post(
    "/customers",
    response_model=CustomerRead,
    status_code=status.HTTP_201_CREATED,
    tags=["Customers"],
)
def create_customer(payload: CustomerCreate, session: ApiSession) -> Customer:
    """Создать клиента и вернуть только публичные поля сохранённой записи."""
    return resources.create_customer(session, payload)


@router.get("/customers", response_model=list[CustomerRead], tags=["Customers"])
def list_customers(
    session: ApiSession, offset: Offset = 0, limit: Limit = 20
) -> list[Customer]:
    """Прочитать страницу клиентов без загрузки всей таблицы в память."""
    return resources.list_customers(session, offset=offset, limit=limit)


@router.get("/customers/{resource_id}", response_model=CustomerRead, tags=["Customers"])
def get_customer(resource_id: ResourceId, session: ApiSession) -> Customer:
    """Прочитать существующего клиента по положительному id."""
    return resources.get_customer(session, resource_id)


@router.post(
    "/products",
    response_model=ProductRead,
    status_code=status.HTTP_201_CREATED,
    tags=["Products"],
)
def create_product(payload: ProductCreate, session: ApiSession) -> Product:
    """Создать продукт с уникальным кодом и необязательным описанием."""
    return resources.create_product(session, payload)


@router.get("/products", response_model=list[ProductRead], tags=["Products"])
def list_products(
    session: ApiSession, offset: Offset = 0, limit: Limit = 20
) -> list[Product]:
    """Прочитать страницу продуктов в порядке возрастания id."""
    return resources.list_products(session, offset=offset, limit=limit)


@router.get("/products/{resource_id}", response_model=ProductRead, tags=["Products"])
def get_product(resource_id: ResourceId, session: ApiSession) -> Product:
    """Прочитать один продукт или получить безопасную ошибку 404."""
    return resources.get_product(session, resource_id)


@router.post(
    "/licenses",
    response_model=LicenseRead,
    status_code=status.HTTP_201_CREATED,
    tags=["Licenses"],
)
def create_license(payload: LicenseCreate, session: ApiSession) -> License:
    """Создать лицензию после проверки ссылок, статуса и временных границ."""
    return resources.create_license(session, payload)


@router.get("/licenses", response_model=list[LicenseRead], tags=["Licenses"])
def list_licenses(
    session: ApiSession,
    offset: Offset = 0,
    limit: Limit = 20,
    customer_id: FilterId = None,
    product_id: FilterId = None,
    license_key: Annotated[LicenseKey | None, Query()] = None,
) -> list[License]:
    """Прочитать лицензии с точными фильтрами, объединёнными условием И."""
    return resources.list_licenses(
        session,
        offset=offset,
        limit=limit,
        customer_id=customer_id,
        product_id=product_id,
        license_key=license_key,
    )


@router.get("/licenses/{resource_id}", response_model=LicenseRead, tags=["Licenses"])
def get_license(resource_id: ResourceId, session: ApiSession) -> License:
    """Прочитать лицензию по id, не путая его со строковым license_key."""
    return resources.get_license(session, resource_id)


@router.post(
    "/tickets",
    response_model=TicketRead,
    status_code=status.HTTP_201_CREATED,
    tags=["Tickets"],
)
def create_ticket(payload: TicketCreate, session: ApiSession) -> Ticket:
    """Создать обращение с начальным статусом open и выбранным приоритетом."""
    return resources.create_ticket(session, payload)


@router.get("/tickets", response_model=list[TicketRead], tags=["Tickets"])
def list_tickets(
    session: ApiSession,
    offset: Offset = 0,
    limit: Limit = 20,
    customer_id: FilterId = None,
    product_id: FilterId = None,
    status: Annotated[TicketStatus | None, Query()] = None,
) -> list[Ticket]:
    """Прочитать страницу обращений по клиенту, продукту и текущему статусу."""
    return resources.list_tickets(
        session,
        offset=offset,
        limit=limit,
        customer_id=customer_id,
        product_id=product_id,
        status=status,
    )


@router.get("/tickets/{resource_id}", response_model=TicketRead, tags=["Tickets"])
def get_ticket(resource_id: ResourceId, session: ApiSession) -> Ticket:
    """Прочитать одно обращение без изменений его состояния."""
    return resources.get_ticket(session, resource_id)
