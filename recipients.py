import argparse
import json
import logging
import os
import re
import sys
from contextlib import contextmanager
from email.errors import HeaderParseError
from email.headerregistry import Address
from pathlib import Path
from tempfile import NamedTemporaryFile

from dotenv import dotenv_values


PROJECT_DIR = Path(__file__).resolve().parent
RECIPIENTS_FILE = PROJECT_DIR / "recipients.json"


class RecipientsError(ValueError):
    """An invalid recipient list or command. Messages may be shown to the user."""


def _validate_address(value):
    try:
        address = Address(addr_spec=value)
    except (ValueError, HeaderParseError):
        raise RecipientsError("Некорректный адрес электронной почты.") from None
    if (
        not address.username
        or address.addr_spec != value
        or not all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in address.domain.split(".")
        )
    ):
        raise RecipientsError("Некорректный адрес электронной почты.")
    return value


def load_recipients():
    """Read the validated recipient list. An absent file means an empty list."""
    try:
        with RECIPIENTS_FILE.open(encoding="utf-8") as source:
            data = json.load(source)
    except FileNotFoundError:
        if not os.path.lexists(RECIPIENTS_FILE):
            return []
        raise
    except (ValueError, RecursionError):
        raise RecipientsError("Файл recipients.json повреждён или содержит неверный JSON.") from None

    if not isinstance(data, list) or not all(isinstance(value, str) for value in data):
        raise RecipientsError("Файл recipients.json должен содержать список адресов.")
    try:
        addresses = [_validate_address(value) for value in data]
    except RecipientsError:
        raise RecipientsError("Файл recipients.json содержит некорректный адрес.") from None
    if len({value.casefold() for value in addresses}) != len(addresses):
        raise RecipientsError("Файл recipients.json содержит повторяющиеся адреса.")
    return addresses


@contextmanager
def _lock_recipients():
    # Lock a stable sidecar rather than the JSON file whose inode os.replace changes.
    with RECIPIENTS_FILE.with_suffix(".json.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def _save_recipients(addresses):
    temporary_path = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=RECIPIENTS_FILE.parent,
            prefix=".recipients.json.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(addresses, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, RECIPIENTS_FILE)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _report_dotenv_warning(record):
    print("Предупреждение: в .env есть некорректная строка.", file=sys.stderr)
    return False


def _migrate():
    if os.path.lexists(RECIPIENTS_FILE):
        raise RecipientsError("Файл recipients.json уже существует. Перенос отменён.")

    value = os.environ.get("RECIPIENTS")
    if value is None:
        dotenv_logger = logging.getLogger("dotenv.main")
        dotenv_logger.addFilter(_report_dotenv_warning)
        try:
            value = dotenv_values(PROJECT_DIR / ".env", interpolate=False).get("RECIPIENTS")
        except UnicodeError:
            raise RecipientsError("Не удалось прочитать .env в кодировке UTF-8.") from None
        finally:
            dotenv_logger.removeFilter(_report_dotenv_warning)
    if not value or not value.strip():
        raise RecipientsError("В окружении и .env нет адресов для переноса из RECIPIENTS.")

    addresses = {}
    for entry in value.split(","):
        address = _validate_address(entry.strip())
        addresses.setdefault(address.casefold(), address)
    _save_recipients(list(addresses.values()))
    return f"Перенесено адресов: {len(addresses)}."


def _change_recipients(command, email):
    with _lock_recipients():
        if command == "migrate":
            return _migrate()

        address = _validate_address(email.strip())
        addresses = load_recipients()
        by_address = {value.casefold(): value for value in addresses}
        key = address.casefold()
        if command == "add":
            if key in by_address:
                raise RecipientsError("Этот получатель уже есть в списке.")
            _save_recipients([*addresses, address])
            return "Получатель добавлен."

        if key not in by_address:
            raise RecipientsError("Получатель не найден в списке.")
        addresses.remove(by_address[key])
        _save_recipients(addresses)
        return "Получатель удалён."


class _RussianHelpFormatter(argparse.HelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        super().add_usage(usage, actions, groups, prefix="Использование: ")


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, add_help=False, formatter_class=_RussianHelpFormatter, **kwargs)
        self.add_argument_group("Параметры").add_argument(
            "-h", "--help", action="help", help="Показать эту справку.",
        )

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, "Ошибка: неверная команда или аргументы. Используйте --help.\n")


def main(argv=None):
    parser = _Parser(prog="recipients.py", description="Управление получателями рассылки.")
    commands = parser.add_subparsers(dest="command", required=True, title="Команды", metavar="КОМАНДА")
    commands.add_parser("list", help="Показать список получателей.")
    commands.add_parser("count", help="Показать число получателей.")
    commands.add_parser("migrate", help="Однократно перенести адреса из RECIPIENTS.")
    for command, description in (("add", "Добавить получателя."), ("remove", "Удалить получателя.")):
        subparser = commands.add_parser(command, help=description, description=description)
        subparser.add_argument_group("Аргументы").add_argument("email", metavar="EMAIL", help="Адрес.")
    args = parser.parse_args(argv)

    try:
        if args.command == "list":
            print("\n".join(load_recipients()) or "Список получателей пуст.")
        elif args.command == "count":
            print(f"Получателей: {len(load_recipients())}.")
        else:
            print(_change_recipients(args.command, getattr(args, "email", None)))
    except RecipientsError as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("Ошибка: не удалось прочитать или обновить локальный список получателей.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
