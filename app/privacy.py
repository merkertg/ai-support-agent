"""Обезличить клиента или применить срок хранения обращений из командной строки.

Запуск: python -m app.privacy …
(в Compose: docker compose exec api python -m app.privacy …).

- erase --customer-id N --confirm-email E — то же, что POST /api/customers/N/erase:
  нужно, например, чтобы повторить обезличивание после восстановления копии,
  сделанной раньше него;
- retention --older-than-days D [--apply] [--limit N] — закрытые обращения,
  не менявшиеся D дней. Без --apply выводится только план, данные не меняются.

Обе команды печатают JSON-отчёт без персональных данных: только id и счётчики.
Код возврата: 0 — успех, 1 — понятная ошибка, 2 — неверные аргументы.
"""

import argparse
import json
import sys

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.console import use_utf8_output
from app.db.session import DatabaseNotConfigured, dispose_engine, get_engine
from app.services import privacy
from app.services.errors import ResourceError

DATABASE_HINT = "проверь DATABASE_URL, доступность PostgreSQL и alembic upgrade head"


def positive_int(value: str) -> int:
    """Принять только целое число больше нуля."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("нужно целое число") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("нужно число больше 0")
    return number


def print_json(report) -> None:
    """Вывести отчёт Pydantic одним JSON-объектом с кириллицей без экранирования."""
    print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))


def erase(arguments: argparse.Namespace) -> int:
    """Обезличить клиента после проверки введённого email."""
    with Session(get_engine()) as session:
        report = privacy.erase_customer(
            session, arguments.customer_id, arguments.confirm_email
        )
    print_json(report)
    return 0


def retention(arguments: argparse.Namespace) -> int:
    """Показать план или обезличить закрытые обращения старше срока."""
    with Session(get_engine()) as session:
        report = privacy.apply_retention(
            session,
            arguments.older_than_days,
            apply=arguments.apply,
            limit=arguments.limit,
        )
    print_json(report)
    if not arguments.apply:
        print(
            "Это план: данные не изменены. Для обезличивания добавь --apply.",
            file=sys.stderr,
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Описать подкоманды erase и retention."""
    parser = argparse.ArgumentParser(
        prog="python -m app.privacy", description=__doc__.splitlines()[0]
    )
    commands = parser.add_subparsers(dest="command", required=True)
    erase_parser = commands.add_parser("erase", help="обезличить клиента по запросу")
    erase_parser.add_argument("--customer-id", type=positive_int, required=True)
    erase_parser.add_argument(
        "--confirm-email", required=True, help="текущий email клиента"
    )
    erase_parser.set_defaults(handler=erase)
    retention_parser = commands.add_parser(
        "retention", help="обезличить закрытые обращения старше срока"
    )
    retention_parser.add_argument(
        "--older-than-days",
        type=positive_int,
        required=True,
        help=f"срок хранения в днях, не меньше {privacy.MIN_RETENTION_DAYS}",
    )
    retention_parser.add_argument(
        "--apply", action="store_true", help="выполнить, а не только показать план"
    )
    retention_parser.add_argument(
        "--limit",
        type=positive_int,
        default=500,
        help=f"сколько обращений за запуск, до {privacy.MAX_RETENTION_BATCH}",
    )
    retention_parser.set_defaults(handler=retention)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Выполнить подкоманду и вернуть 0 при успехе, 1 при понятной ошибке."""
    use_utf8_output()
    arguments = build_parser().parse_args(argv)
    try:
        return arguments.handler(arguments)
    except ResourceError as error:
        print(f"Обезличивание: {error.detail}", file=sys.stderr)
    except (DatabaseNotConfigured, SQLAlchemyError):
        print(f"Обезличивание: {DATABASE_HINT}", file=sys.stderr)
    finally:
        dispose_engine()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
