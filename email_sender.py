import logging
import os
import re
import smtplib
import ssl
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

from recipients import RecipientsError, load_recipients


logger = logging.getLogger(__name__)

_SMTP_REJECTIONS = (
    smtplib.SMTPAuthenticationError,
    smtplib.SMTPConnectError,
    smtplib.SMTPHeloError,
    smtplib.SMTPNotSupportedError,
    smtplib.SMTPRecipientsRefused,
    smtplib.SMTPSenderRefused,
)


class SMTPBeforeSendError(smtplib.SMTPException):
    """Sending stopped before the message could be submitted."""


class SMTPDeliveryUncertain(smtplib.SMTPException):
    """The server may have accepted the message."""


class SMTPPartialDelivery(smtplib.SMTPRecipientsRefused, SMTPDeliveryUncertain):
    """At least one recipient was refused and the message was accepted for others."""


def _is_smtp_rejection(error):
    if isinstance(error, SMTPDeliveryUncertain):
        return False
    if isinstance(error, smtplib.SMTPDataError):
        return 400 <= error.smtp_code < 600
    return isinstance(error, _SMTP_REJECTIONS)


def is_definite_send_failure(error):
    return isinstance(error, (SMTPBeforeSendError, ValueError, FileNotFoundError, PermissionError)) or (
        _is_smtp_rejection(error)
    )


def _parse_address(value, setting):
    try:
        address = Address(addr_spec=value)
        if address.username and address.domain:
            return address
    except (ValueError, HeaderParseError):
        pass
    raise ValueError(f"{setting}: ожидается корректный адрес электронной почты")


def _load_email_settings():
    load_dotenv(interpolate=False)
    host = os.getenv("EMAIL_HOST", "smtp.yandex.ru").strip()
    labels = host.removesuffix(".").split(".")
    if len(host) > 254 or any(
        not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
        for label in labels
    ):
        raise ValueError("EMAIL_HOST: ожидается корректное имя SMTP-сервера")

    port_value = os.getenv("EMAIL_PORT", "465").strip()
    if not re.fullmatch(r"[0-9]{1,5}", port_value) or not 1 <= int(port_value) <= 65535:
        raise ValueError("EMAIL_PORT: ожидается целое число от 1 до 65535")

    user = _parse_address(os.getenv("EMAIL_USER", "").strip(), "EMAIL_USER")
    password = os.getenv("EMAIL_PASSWORD", "")
    if not password.strip():
        raise ValueError("EMAIL_PASSWORD: требуется пароль SMTP")

    recipients = load_recipients()
    if not recipients:
        raise RecipientsError("Список получателей пуст. Добавьте адрес через recipients.py add EMAIL.")
    return host, int(port_value), user, password, recipients


def _send_message(smtp, message):
    try:
        refused = smtp.send_message(message)
    except Exception as error:
        if _is_smtp_rejection(error):
            raise
        raise SMTPDeliveryUncertain("Результат отправки неизвестен.") from error
    if refused:
        raise SMTPPartialDelivery(refused)


def _submit_message(host, port, user, password, message):
    submission_finished = False
    delivery_error = None
    try:
        with smtplib.SMTP_SSL(
            host, port, timeout=30, context=ssl.create_default_context()
        ) as smtp:
            smtp.login(user.addr_spec, password)
            try:
                _send_message(smtp, message)
            except Exception as error:
                delivery_error = error
            submission_finished = True
    except Exception as error:
        if not submission_finished:
            if isinstance(error, _SMTP_REJECTIONS):
                raise
            raise SMTPBeforeSendError("Не удалось начать отправку по SMTP.") from error
        logger.warning("Ошибка закрытия SMTP-соединения (%s)", type(error).__name__)

    if delivery_error is not None:
        raise delivery_error


def send_email(subject, body, attachment_path=None):
    """Send a plain-text email with an optional DOCX. Raise if any recipient is refused."""
    try:
        host, port, user, password, recipients = _load_email_settings()
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = user
        message["To"] = ", ".join(recipients)
        message.set_content(body)

        if attachment_path is not None:
            attachment = Path(attachment_path)
            if attachment.suffix.lower() != ".docx":
                raise ValueError("Вложение должно быть файлом .docx")
            message.add_attachment(
                attachment.read_bytes(),
                maintype="application",
                subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
                filename=attachment.name,
            )
    except RecipientsError as error:
        logger.error("Не удалось подготовить письмо: %s", error)
        raise
    except Exception as error:
        logger.error("Не удалось подготовить письмо (%s)", type(error).__name__)
        if is_definite_send_failure(error):
            raise
        raise SMTPBeforeSendError("Не удалось подготовить письмо.") from error

    try:
        _submit_message(host, port, user, password, message)
    except smtplib.SMTPRecipientsRefused as error:
        logger.error(
            "SMTP-сервер отклонил получателей: %d из %d",
            len(error.recipients), len(recipients),
        )
        raise
    except OSError as error:
        logger.error("Не удалось отправить письмо (%s)", type(error).__name__)
        raise

    logger.info("SMTP-сервер принял письмо для всех получателей: %d", len(recipients))
