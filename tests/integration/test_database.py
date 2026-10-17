"""Проверить миграции, сохранение и ограничения на настоящем PostgreSQL."""

from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.main as api
from app.models import (
    AgentRun,
    AgentSuggestion,
    Customer,
    KnowledgeDocument,
    License,
    Product,
    Ticket,
    TicketMessage,
    User,
)

# Маркер позволяет отдельно запускать unit и реальные проверки PostgreSQL.
pytestmark = pytest.mark.integration
EXPECTED_TABLES = {
    "users",
    "customers",
    "products",
    "licenses",
    "tickets",
    "ticket_messages",
    "knowledge_documents",
    "agent_runs",
    "agent_suggestions",
}


@pytest.fixture
def records(migrated_engine: Engine) -> dict[str, int]:
    """Сохранить связанный пример всех девяти сущностей с явным commit."""
    with Session(migrated_engine) as session:
        user = User(name="Тестовый оператор", email="operator@example.test")
        customer = Customer(name="Тестовый клиент", email="client@example.test")
        product = Product(name="Тестовый продукт", code="TEST-PRODUCT")
        session.add_all([user, customer, product])
        session.flush()
        license_record = License(
            customer_id=customer.id, product_id=product.id, license_key="TEST-KEY"
        )
        ticket = Ticket(
            customer_id=customer.id,
            product_id=product.id,
            subject="Проверка БД",
            description="Тестовое обращение",
        )
        document = KnowledgeDocument(
            product_id=product.id,
            document_key="KB-TEST",
            title="Тестовая статья",
            source_path="knowledge/test.md",
        )
        session.add_all([license_record, ticket, document])
        session.flush()
        message = TicketMessage(
            ticket_id=ticket.id,
            user_id=user.id,
            author_type="operator",
            content="Тестовый ответ",
        )
        run = AgentRun(ticket_id=ticket.id)
        session.add_all([message, run])
        session.flush()
        suggestion = AgentSuggestion(
            agent_run_id=run.id, content="Тестовое предложение"
        )
        session.add(suggestion)
        session.flush()
        ids = {
            "user": user.id,
            "customer": customer.id,
            "product": product.id,
            "license": license_record.id,
            "ticket": ticket.id,
            "message": message.id,
            "document": document.id,
            "run": run.id,
            "suggestion": suggestion.id,
        }
        session.commit()
    return ids


def test_migrations_round_trip_and_no_model_drift(
    migrated_engine: Engine,
    migration_config: Config,
) -> None:
    """Доказать upgrade, отсутствие дрейфа моделей, downgrade и повторный upgrade."""
    assert set(inspect(migrated_engine).get_table_names()) == EXPECTED_TABLES | {
        "alembic_version"
    }
    command.check(migration_config)
    command.downgrade(migration_config, "base")
    assert set(inspect(migrated_engine).get_table_names()) <= {"alembic_version"}
    command.upgrade(migration_config, "head")
    assert set(inspect(migrated_engine).get_table_names()) == EXPECTED_TABLES | {
        "alembic_version"
    }
    command.check(migration_config)


def test_committed_data_is_visible_in_new_session(
    migrated_engine: Engine,
    records: dict[str, int],
) -> None:
    """Новая сессия читает commit предыдущей и серверные значения по умолчанию."""
    with Session(migrated_engine) as session:
        assert session.get(Customer, records["customer"]).email == "client@example.test"
        assert session.get(User, records["user"]).role == "operator"
        assert session.get(License, records["license"]).status == "active"
        assert session.get(Ticket, records["ticket"]).priority == "medium"
        assert (
            session.get(TicketMessage, records["message"]).content == "Тестовый ответ"
        )
        assert (
            session.get(KnowledgeDocument, records["document"]).document_key
            == "KB-TEST"
        )
        assert session.get(AgentRun, records["run"]).status == "pending"
        assert session.get(AgentSuggestion, records["suggestion"]).status == "pending"
        assert session.get(Product, records["product"]).code == "TEST-PRODUCT"


def test_uncommitted_data_rolls_back_on_session_close(migrated_engine: Engine) -> None:
    """flush отправляет INSERT, но без commit строка не переживает закрытие сессии."""
    with Session(migrated_engine) as session:
        session.add(Customer(name="Не сохранять", email="rollback@example.test"))
        session.flush()
        assert session.scalar(select(func.count()).select_from(Customer)) == 1
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(Customer)) == 0


def test_unique_email_rejected(
    migrated_engine: Engine,
    records: dict[str, int],
) -> None:
    """Ограничение PostgreSQL запрещает повторный email даже без API-валидации."""
    with pytest.raises(IntegrityError), Session(migrated_engine) as session:
        session.add(Customer(name="Дубликат", email="client@example.test"))
        session.commit()


def test_orphan_license_rejected(
    migrated_engine: Engine,
    records: dict[str, int],
) -> None:
    """Нельзя выдать лицензию несуществующему клиенту: работает внешний ключ."""
    with pytest.raises(IntegrityError), Session(migrated_engine) as session:
        session.add(
            License(
                customer_id=999999999,
                product_id=records["product"],
                license_key="ORPHAN",
            )
        )
        session.commit()


def test_customer_with_history_cannot_be_deleted(
    migrated_engine: Engine,
    records: dict[str, int],
) -> None:
    """RESTRICT защищает клиента, на которого ссылаются лицензия и обращение."""
    with pytest.raises(IntegrityError), Session(migrated_engine) as session:
        session.execute(delete(Customer).where(Customer.id == records["customer"]))
        session.commit()


@pytest.mark.parametrize(
    ("model", "record_key", "attribute", "value"),
    [
        (User, "user", "role", "unknown"),
        (License, "license", "status", "unknown"),
        (Ticket, "ticket", "status", "unknown"),
        (Ticket, "ticket", "priority", "urgent"),
        (TicketMessage, "message", "author_type", "unknown"),
        (AgentRun, "run", "status", "unknown"),
        (AgentSuggestion, "suggestion", "status", "unknown"),
        (AgentSuggestion, "suggestion", "suggestion_type", "unknown"),
    ],
)
def test_invalid_state_rejected(
    migrated_engine: Engine,
    records: dict[str, int],
    model,
    record_key: str,
    attribute: str,
    value: str,
) -> None:
    """Каждое перечисляемое состояние защищено CHECK на уровне PostgreSQL."""
    with pytest.raises(IntegrityError), Session(migrated_engine) as session:
        row = session.get(model, records[record_key])
        setattr(row, attribute, value)
        session.commit()


@pytest.mark.parametrize(
    ("model", "record_key", "attribute"),
    [(License, "license", "expires_at"), (AgentRun, "run", "finished_at")],
)
def test_end_cannot_precede_start(
    migrated_engine: Engine,
    records: dict[str, int],
    model,
    record_key: str,
    attribute: str,
) -> None:
    """Срок лицензии и завершение запуска не могут быть раньше их начала."""
    with pytest.raises(IntegrityError), Session(migrated_engine) as session:
        row = session.get(model, records[record_key])
        setattr(row, attribute, datetime(2000, 1, 1, tzinfo=UTC))
        session.commit()


@pytest.mark.parametrize(
    ("author_type", "has_user"),
    [("operator", False), ("customer", True), ("agent", True)],
)
def test_message_author_and_operator_must_agree(
    migrated_engine: Engine,
    records: dict[str, int],
    author_type: str,
    has_user: bool,
) -> None:
    """Оператор обязан иметь user_id, клиент и агент не могут выдавать себя за него."""
    with pytest.raises(IntegrityError), Session(migrated_engine) as session:
        row = session.get(TicketMessage, records["message"])
        row.author_type = author_type
        row.user_id = records["user"] if has_user else None
        session.commit()


@pytest.mark.parametrize(
    ("status", "has_reviewer", "has_time"),
    [
        ("approved", False, False),
        ("rejected", True, False),
        ("approved", False, True),
        ("pending", True, True),
    ],
)
def test_suggestion_review_must_be_complete(
    migrated_engine: Engine,
    records: dict[str, int],
    status: str,
    has_reviewer: bool,
    has_time: bool,
) -> None:
    """Решение требует рецензента и времени, а ожидание не допускает их заполнения."""
    with pytest.raises(IntegrityError), Session(migrated_engine) as session:
        row = session.get(AgentSuggestion, records["suggestion"])
        row.status = status
        row.reviewed_by = records["user"] if has_reviewer else None
        row.reviewed_at = datetime.now(UTC) if has_time else None
        session.commit()


def test_valid_review_can_be_saved(
    migrated_engine: Engine,
    records: dict[str, int],
) -> None:
    """Полное подтверждение сохраняется и читается другой транзакцией."""
    with Session(migrated_engine) as session:
        row = session.get(AgentSuggestion, records["suggestion"])
        row.status = "approved"
        row.reviewed_by = records["user"]
        row.reviewed_at = datetime.now(UTC)
        session.commit()
    with Session(migrated_engine) as session:
        assert session.get(AgentSuggestion, records["suggestion"]).status == "approved"


def test_ticket_delete_cascades_to_owned_rows(
    migrated_engine: Engine,
    records: dict[str, int],
) -> None:
    """Удаление обращения удаляет сообщения, запуски и их предложения."""
    with Session(migrated_engine) as session:
        session.execute(delete(Ticket).where(Ticket.id == records["ticket"]))
        session.commit()
    with Session(migrated_engine) as session:
        assert session.get(TicketMessage, records["message"]) is None
        assert session.get(AgentRun, records["run"]) is None
        assert session.get(AgentSuggestion, records["suggestion"]) is None
        assert session.get(Customer, records["customer"]) is not None


def test_product_delete_keeps_knowledge_document(migrated_engine: Engine) -> None:
    """SET NULL сохраняет статью после удаления незадействованного продукта."""
    with Session(migrated_engine) as session:
        product = Product(name="Удаляемый продукт", code="REMOVE-ME")
        session.add(product)
        session.flush()
        document = KnowledgeDocument(
            product_id=product.id,
            document_key="KB-KEEP",
            title="Общая статья",
            source_path="knowledge/keep.md",
        )
        session.add(document)
        session.flush()
        document_id = document.id
        session.delete(product)
        session.commit()
    with Session(migrated_engine) as session:
        assert session.get(KnowledgeDocument, document_id).product_id is None


def test_real_database_is_ready(
    migrated_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP /ready проверяет реальные SQL и текущую версию миграции."""

    def sessions():
        """Подставить только тестовый пул, сохранив production-проверку ошибок."""
        with Session(migrated_engine) as session:
            yield session

    monkeypatch.setattr(api, "get_session", sessions)
    with TestClient(api.app) as client:
        response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}
