"""Создать и прочитать вымышленные данные для проверки сохранения в PostgreSQL.

Запускать явно: python -m app.db.demo seed или python -m app.db.demo show.
Миграции и запуск API не добавляют демонстрационные записи автоматически.
"""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.console import use_utf8_output
from app.db.session import DatabaseNotConfigured, dispose_engine, get_engine
from app.models import Customer, License, Product

DEMO_EMAIL = "support-demo@example.test"
DEMO_PRODUCT = "techflow-server"
DEMO_LICENSE = "LIC-1054"


class DemoDataError(RuntimeError):
    """Объяснить отсутствие или конфликт демонстрационных записей."""


def seed_demo() -> None:
    """Добавить клиента, продукт и лицензию одной явной транзакцией.

    Повторный запуск ищет записи по уникальным ключам и не создаёт копии.
    Существующие значения не перезаписываются. При конфликте принадлежности
    лицензии вся незавершённая транзакция откатывается.
    """
    with Session(get_engine()) as session, session.begin():
        customer = session.scalar(select(Customer).where(Customer.email == DEMO_EMAIL))
        if customer is None:
            customer = Customer(
                name="Demo Customer", email=DEMO_EMAIL, company="TechFlow Demo"
            )
            session.add(customer)
            # flush получает созданный PostgreSQL ID, но ещё не делает commit.
            session.flush()
        product = session.scalar(select(Product).where(Product.code == DEMO_PRODUCT))
        if product is None:
            product = Product(
                code=DEMO_PRODUCT,
                name="TechFlow Server",
                description="Fictional product for local database verification",
            )
            session.add(product)
            session.flush()
        license_record = session.scalar(
            select(License).where(License.license_key == DEMO_LICENSE)
        )
        if license_record is None:
            session.add(
                License(
                    customer_id=customer.id,
                    product_id=product.id,
                    license_key=DEMO_LICENSE,
                    status="active",
                    expires_at=datetime.now(UTC) + timedelta(days=365),
                )
            )
        elif (
            license_record.customer_id != customer.id
            or license_record.product_id != product.id
        ):
            raise DemoDataError(
                "LIC-1054 уже принадлежит другим записям; существующие данные сохранены"
            )
        # session.begin() делает commit только после успешного выхода из блока.


def show_demo() -> dict[str, dict]:
    """Прочитать уже сохранённые записи в новой сессии и вернуть снимок.

    Стабильный JSON можно сравнить до и после перезапуска PostgreSQL.
    Команда show сама ничего не добавляет и не изменяет.
    """
    with Session(get_engine()) as session:
        customer = session.scalar(select(Customer).where(Customer.email == DEMO_EMAIL))
        product = session.scalar(select(Product).where(Product.code == DEMO_PRODUCT))
        license_record = session.scalar(
            select(License).where(License.license_key == DEMO_LICENSE)
        )
        if customer is None or product is None or license_record is None:
            raise DemoDataError("Демонстрационных данных нет: сначала выполни seed")
        if (
            license_record.customer_id != customer.id
            or license_record.product_id != product.id
        ):
            raise DemoDataError("Демонстрационная лицензия связана с другими записями")
        return {
            "customer": {
                "id": customer.id,
                "name": customer.name,
                "email": customer.email,
            },
            "product": {"id": product.id, "name": product.name, "code": product.code},
            "license": {
                "id": license_record.id,
                "license_key": license_record.license_key,
                "status": license_record.status,
                "issued_at": license_record.issued_at.isoformat(),
                "expires_at": (
                    license_record.expires_at.isoformat()
                    if license_record.expires_at is not None
                    else None
                ),
            },
        }


def main() -> int:
    """Выбрать seed/show, вывести только JSON или безопасное сообщение ошибки."""
    use_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("seed", "show"))
    args = parser.parse_args()
    try:
        if args.command == "seed":
            seed_demo()
        snapshot = show_demo()
    except DemoDataError as error:
        print(f"Демо: {error}", file=sys.stderr)
        return 1
    except (DatabaseNotConfigured, ValidationError, SQLAlchemyError):
        print(
            "Демо: проверь DATABASE_URL, доступность PostgreSQL и alembic upgrade head",
            file=sys.stderr,
        )
        return 1
    finally:
        dispose_engine()
    print(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
