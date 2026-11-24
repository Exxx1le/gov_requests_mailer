import argparse
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from email_sender import send_email
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


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Сформировать и отправить подборку правовых актов.")
    parser.add_argument("--start-date", type=_parse_date, metavar="YYYY-MM-DD")
    parser.add_argument("--end-date", type=_parse_date, metavar="YYYY-MM-DD")
    args = parser.parse_args(argv)

    start_date, end_date = args.start_date, args.end_date
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

    start_label, end_label = (
        f"{day.day:02d}.{day.month:02d}.{day.year:04d}"
        for day in (start_date, end_date)
    )
    logger.info("Подготовка подборки за %s — %s", start_label, end_label)
    try:
        cleanup_old_files(max_age_days=7)
    except Exception as error:
        logger.error("Не удалось очистить старые документы (%s)", type(error).__name__)
        return 1

    try:
        file_path = process_documents(start_date, end_date)
    except Exception as error:
        logger.error("Не удалось сформировать подборку документов (%s)", type(error).__name__)
        return 1

    subject = f"Подборка нормативных правовых актов за {start_label} — {end_label}"
    body = (
        "Коллеги, добрый день!\n\n"
        "Направляем еженедельную подборку нормативных правовых актов, "
        f"опубликованных за период с {start_label} по {end_label}.\n\n"
        "Подборка документов представлена во вложении.\n\n"
        "Письмо сформировано автоматически. Отвечать на него не требуется."
    )
    try:
        send_email(subject, body, file_path)
    except Exception as error:
        logger.error("Ошибка отправки письма (%s). DOCX сохранён.", type(error).__name__)
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


if __name__ == "__main__":
    raise SystemExit(main())
