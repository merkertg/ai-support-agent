"""Выдать, отозвать и показать API-токены сотрудников из командной строки.

Команда нужна прежде всего для первого администратора: без токена API не
принимает запросы, поэтому первый токен выдаётся с доступом к серверу.
Запуск: python -m app.tokens issue --email E --name N --role admin
(в Compose: docker compose exec api python -m app.tokens issue …).

stdout команды issue содержит только токен — его удобно сохранить в переменную
окружения. Пояснения выводятся в stderr. Повторно токен не показывается:
в БД хранится лишь его SHA-256. Команды list и revoke выводят JSON без секретов.
"""

import argparse
import json
import sys

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.console import use_utf8_output
from app.db.session import DatabaseNotConfigured, dispose_engine, get_engine
from app.models import User
from app.schemas.auth import DEFAULT_TOKEN_DAYS, MAX_TOKEN_DAYS, TokenCreate
from app.schemas.resources import UserCreate
from app.services import auth
from app.services.errors import ResourceError

ROLES = ("operator", "reviewer", "admin")
DATABASE_HINT = "проверь DATABASE_URL, доступность PostgreSQL и alembic upgrade head"
EMAIL_ADDRESS = TypeAdapter(EmailStr)


class TokenCommandError(RuntimeError):
    """Понятная причина отказа команды без SQL, адреса БД и секретов."""


def validation_message(error: ValidationError) -> str:
    """Назвать поле и правило, не повторяя введённое значение."""
    first = error.errors(include_input=False)[0]
    field = ".".join(str(part) for part in first["loc"]) or "параметры"
    return f"неверное значение {field}: {first['msg']}"


def find_user(session: Session, email: str) -> User | None:
    """Найти email с той же нормализацией, что и в схеме создания сотрудника."""
    try:
        normalized = EMAIL_ADDRESS.validate_python(email.strip())
    except ValidationError:
        # Не повторяем адрес в ошибке: аргумент может содержать секрет по ошибке.
        raise TokenCommandError(
            "неверное значение email: нужен корректный адрес"
        ) from None
    return session.scalar(select(User).where(User.email == normalized))


def ensure_user(
    session: Session, email: str, name: str | None, role: str | None
) -> tuple[User, bool]:
    """Вернуть сотрудника по email или создать его, если указано имя.

    Роль существующего сотрудника команда не меняет: тихое повышение прав
    из-за опечатки в --role опаснее явной ошибки.
    """
    user = find_user(session, email)
    if user is not None:
        if role is not None and user.role != role:
            raise TokenCommandError(
                f"у сотрудника роль {user.role}; issue не меняет роль существующего"
            )
        return user, False
    if not name:
        raise TokenCommandError(
            "сотрудника с таким email нет: укажи --name (и --role), чтобы создать"
        )
    payload = UserCreate(name=name, email=email, role=role or "operator")
    user = User(**payload.model_dump())
    try:
        session.add(user)
        session.commit()
    except IntegrityError:
        session.rollback()
        raise TokenCommandError(
            "сотрудник с этим email создан параллельно: повтори команду"
        ) from None
    session.refresh(user)
    return user, True


def issue(arguments: argparse.Namespace) -> int:
    """Выдать токен; при необходимости сначала создать сотрудника."""
    try:
        payload = TokenCreate(name=arguments.label, expires_in_days=arguments.days)
        if arguments.name is not None:
            UserCreate(
                name=arguments.name,
                email=arguments.email,
                role=arguments.role or "operator",
            )
    except ValidationError as error:
        raise TokenCommandError(validation_message(error)) from None
    with Session(get_engine()) as session:
        user, created = ensure_user(
            session, arguments.email, arguments.name, arguments.role
        )
        if created:
            print(f"Создан сотрудник №{user.id} с ролью {user.role}.", file=sys.stderr)
        role = user.role
        issued = auth.issue_token(session, user.id, payload)
    print(issued.token)
    print(
        f"Токен №{issued.id} «{issued.name}» ({issued.token_prefix}…) выдан "
        f"сотруднику №{issued.user_id} ({role}) до {issued.expires_at.isoformat()}. "
        "Сохрани его сейчас: повторно он не показывается.",
        file=sys.stderr,
    )
    return 0


def list_user_tokens(arguments: argparse.Namespace) -> int:
    """Показать токены сотрудника по email без секретов и хешей."""
    with Session(get_engine()) as session:
        user = find_user(session, arguments.email)
        if user is None:
            raise TokenCommandError("сотрудник с таким email не найден")
        tokens = auth.list_tokens(session, user.id, limit=100)
    print(
        json.dumps(
            [token.model_dump(mode="json") for token in tokens],
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def revoke(arguments: argparse.Namespace) -> int:
    """Отозвать токен по номеру; повторный отзыв безопасен."""
    with Session(get_engine()) as session:
        token = auth.revoke_token(session, arguments.token_id)
    print(json.dumps(token.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


def positive_int(value: str) -> int:
    """Принять положительное целое число из аргумента командной строки."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("нужно целое число") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("нужно число больше 0")
    return number


def build_parser() -> argparse.ArgumentParser:
    """Описать три подкоманды: issue, list и revoke."""
    parser = argparse.ArgumentParser(
        prog="python -m app.tokens", description=__doc__.splitlines()[0]
    )
    commands = parser.add_subparsers(dest="command", required=True)
    issue_parser = commands.add_parser("issue", help="выдать токен сотруднику")
    issue_parser.add_argument("--email", required=True)
    issue_parser.add_argument("--name", help="имя; нужно, если сотрудника ещё нет")
    issue_parser.add_argument("--role", choices=ROLES)
    issue_parser.add_argument("--label", default="cli", help="подпись токена")
    issue_parser.add_argument(
        "--days",
        type=positive_int,
        default=DEFAULT_TOKEN_DAYS,
        help=f"срок действия в днях, от 1 до {MAX_TOKEN_DAYS}",
    )
    issue_parser.set_defaults(handler=issue)
    list_parser = commands.add_parser("list", help="показать токены сотрудника")
    list_parser.add_argument("--email", required=True)
    list_parser.set_defaults(handler=list_user_tokens)
    revoke_parser = commands.add_parser("revoke", help="отозвать токен по номеру")
    revoke_parser.add_argument("--token-id", type=positive_int, required=True)
    revoke_parser.set_defaults(handler=revoke)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Выполнить подкоманду и вернуть 0 при успехе, 1 при понятной ошибке."""
    use_utf8_output()
    arguments = build_parser().parse_args(argv)
    try:
        return arguments.handler(arguments)
    except TokenCommandError as error:
        print(f"Токены: {error}", file=sys.stderr)
    except ResourceError as error:
        print(f"Токены: {error.detail}", file=sys.stderr)
    except (DatabaseNotConfigured, ValidationError, SQLAlchemyError):
        print(f"Токены: {DATABASE_HINT}", file=sys.stderr)
    finally:
        dispose_engine()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
