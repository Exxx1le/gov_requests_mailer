import argparse
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from email_sender import is_definite_send_failure, send_email
from mailing_history import (
    HistoryError,
    last_blocking_status,
    load_history,
    mailing_lock,
    new_attempt,
    print_history,
    save_history,
)
from recipients import RecipientsError, load_recipients
from utils import cleanup_generated_file, cleanup_old_files, process_documents


logger = logging.getLogger(__name__)


def _parse_date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() == value:
            return parsed
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("ожидается существующая дата в формате YYYY-MM-DD")


def _previous_week():
    today = datetime.now(ZoneInfo("Europe/Moscow")).date()
    this_monday = today - timedelta(days=today.weekday())
    return this_monday - timedelta(days=7), this_monday - timedelta(days=1)


def _run_mailing(attempts, start_date, end_date, force):
    status = last_blocking_status(attempts, start_date, end_date)
    if not force:
        if status == "sent":
            logger.info("Подборка за этот период уже отправлена.")
            return 0
        if status in ("pending", "uncertain"):
            logger.warning(
                "Результат предыдущей отправки неизвестен (%s). "
                "Проверьте доставку вручную перед использованием --force.",
                status,
            )
            return 1
    elif status is not None:
        logger.warning("Выполняется принудительная повторная отправка (--force).")

    attempt = new_attempt(start_date, end_date)
    attempts.append(attempt)
    start_label, end_label = (
        f"{day.day:02d}.{day.month:02d}.{day.year:04d}"
        for day in (start_date, end_date)
    )
    logger.info("Подготовка подборки за %s — %s", start_label, end_label)

    stage = "проверить список получателей"
    try:
        if not load_recipients():
            raise RecipientsError("Список получателей пуст. Добавьте адрес через recipients.py.")
        stage = "очистить старые документы"
        cleanup_old_files(max_age_days=7)
        stage = "сформировать подборку"
        file_path = process_documents(start_date, end_date)
    except Exception as error:
        detail = str(error) if isinstance(error, RecipientsError) else type(error).__name__
        logger.error("Не удалось %s (%s)", stage, detail)
        attempt["status"] = "failed"
        save_history(attempts)
        return 1

    subject = f"Подборка нормативных правовых актов за {start_label} — {end_label}"
    body = (
        "Коллеги, добрый день!\n\n"
        "Направляем еженедельную подборку нормативных правовых актов, "
        f"опубликованных за период с {start_label} по {end_label}.\n\n"
        "Подборка документов представлена во вложении.\n\n"
        "Письмо сформировано автоматически. Отвечать на него не требуется."
    )
    save_history(attempts)  # Persist pending before opening the SMTP connection.
    try:
        send_email(subject, body, file_path)
    except Exception as error:
        attempt["status"] = "failed" if is_definite_send_failure(error) else "uncertain"
        logger.error(
            "Ошибка отправки письма (%s, %s). DOCX сохранён.",
            type(error).__name__, attempt["status"],
        )
        save_history(attempts)
        return 1

    attempt["status"] = "sent"
    try:
        save_history(attempts)
    except OSError:
        logger.error("SMTP-сервер принял письмо, но обновить историю не удалось. DOCX сохранён.")
        return 1

    try:
        cleanup_generated_file(file_path)
    except Exception as error:
        logger.error(
            "SMTP-сервер принял письмо, но удалить вложение не удалось (%s)",
            type(error).__name__,
        )
        return 1

    return 0


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Сформировать и отправить подборку правовых актов.")
    parser.add_argument("--start-date", type=_parse_date, metavar="YYYY-MM-DD")
    parser.add_argument("--end-date", type=_parse_date, metavar="YYYY-MM-DD")
    parser.add_argument("--force", action="store_true", help="Принудительно отправить подборку повторно.")
    parser.add_argument("--history", action="store_true", help="Показать локальную историю рассылок.")
    args = parser.parse_args(argv)

    start_date, end_date = args.start_date, args.end_date
    if not args.history:
        if (start_date is None) != (end_date is None):
            parser.error("--start-date и --end-date необходимо указать вместе")
        if start_date is None:
            try:
                start_date, end_date = _previous_week()
            except ZoneInfoNotFoundError:
                logger.error("Часовой пояс Europe/Moscow недоступен. Установите tzdata.")
                return 1
        elif start_date > end_date:
            parser.error("--start-date должна быть не позже --end-date")

    try:
        with mailing_lock():
            attempts = load_history()
            if args.history:
                print_history(attempts)
                return 0
            return _run_mailing(attempts, start_date, end_date, args.force)
    except HistoryError as error:
        logger.error("%s", error)
    except OSError as error:
        logger.error("Не удалось прочитать или записать историю рассылок (%s)", type(error).__name__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
