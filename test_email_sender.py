import json
import os
import smtplib
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv

import email_sender


class SendEmailTests(unittest.TestCase):
    settings = {
        "EMAIL_USER": "sender@example.com",
        "EMAIL_PASSWORD": "test-only-password",
    }

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.recipients_file = Path(directory.name) / "recipients.json"
        self.recipients_file.write_text(json.dumps(["one@example.com", "two@example.com"]), encoding="utf-8")
        for patcher in (
            patch.dict(os.environ, self.settings, clear=True),
            patch("recipients.RECIPIENTS_FILE", self.recipients_file),
            patch("email_sender.load_dotenv"),
            patch("email_sender.ssl.create_default_context"),
            patch("email_sender.smtplib.SMTP_SSL"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.connect = email_sender.smtplib.SMTP_SSL
        self.server = self.connect.return_value.__enter__.return_value
        self.server.send_message.return_value = {}

    def test_sends_plain_text_over_tls_with_default_server_and_timeout(self):
        with self.assertLogs("email_sender", level="INFO") as logs:
            email_sender.send_email("Подборка", "Список документов")

        email_sender.load_dotenv.assert_called_once_with(interpolate=False)
        email_sender.ssl.create_default_context.assert_called_once_with()
        self.connect.assert_called_once_with(
            "smtp.yandex.ru", 465, timeout=30,
            context=email_sender.ssl.create_default_context.return_value,
        )
        self.server.login.assert_called_once_with("sender@example.com", self.settings["EMAIL_PASSWORD"])
        self.server.send_message.assert_called_once()
        message = self.server.send_message.call_args[0][0]
        self.assertEqual(message["From"].addresses[0].addr_spec, "sender@example.com")
        self.assertEqual(
            [address.addr_spec for address in message["To"].addresses],
            ["one@example.com", "two@example.com"],
        )
        self.assertEqual(message["Subject"], "Подборка")
        self.assertEqual(message.get_content_type(), "text/plain")
        self.assertEqual(message.get_content(), "Список документов\n")
        self.assertEqual(logs.records[0].levelname, "INFO")

    def test_loads_dotenv_and_preserves_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / ".env"
            config_path.write_text(
                "EMAIL_HOST=smtp.example.com\nEMAIL_PORT=2465\n"
                "EMAIL_USER=file-sender@example.com\nRECIPIENTS=from-file@example.com\n"
                "EMAIL_PASSWORD='literal${test}'\n",
                encoding="utf-8",
            )
            email_sender.load_dotenv.side_effect = (
                lambda **kwargs: load_dotenv(config_path, **kwargs)
            )
            with patch.dict(os.environ, {"EMAIL_USER": "environment@example.com"}, clear=True):
                email_sender.send_email("Subject", "Body")

        self.assertEqual(self.connect.call_args[0], ("smtp.example.com", 2465))
        self.server.login.assert_called_once_with("environment@example.com", "literal${test}")
        message = self.server.send_message.call_args[0][0]
        self.assertEqual(
            [address.addr_spec for address in message["To"].addresses],
            ["one@example.com", "two@example.com"],
        )

    def test_sends_docx_with_a_cyrillic_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Документы за неделю.docx"
            content = b"test DOCX attachment"
            path.write_bytes(content)
            email_sender.send_email("Подборка", "Файл во вложении", path)

        message = self.server.send_message.call_args[0][0]
        decoded = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
        self.assertEqual(decoded.get_body(preferencelist=("plain",)).get_content(), "Файл во вложении\n")
        attachment, = decoded.iter_attachments()
        self.assertEqual(
            attachment.get_content_type(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertEqual(attachment.get_filename(), path.name)
        self.assertEqual(attachment.get_payload(decode=True), content)

    def test_rejects_missing_or_invalid_settings_before_connecting(self):
        cases = {
            "EMAIL_HOST": ("", "https://smtp.example.com", "-invalid.example.com"),
            "EMAIL_PORT": ("", "abc", "4_65", "0", "65536"),
            "EMAIL_USER": ("", "sender", "invalid@@example.com"),
            "EMAIL_PASSWORD": ("", "  "),
        }
        for setting, values in cases.items():
            for value in values:
                with self.subTest(setting=setting, value=value), patch.dict(os.environ, {setting: value}):
                    with self.assertLogs("email_sender", level="ERROR"):
                        with self.assertRaisesRegex(ValueError, setting):
                            email_sender.send_email("Subject", "Body")
        self.connect.assert_not_called()

    def test_missing_or_empty_json_prevents_sending_even_with_legacy_recipients(self):
        self.recipients_file.unlink()
        with patch.dict(os.environ, {"RECIPIENTS": "legacy@example.com"}):
            for exists in (False, True):
                with self.subTest(exists=exists):
                    if exists:
                        self.recipients_file.write_text("[]", encoding="utf-8")
                    with self.assertLogs("email_sender", level="ERROR") as logs:
                        with self.assertRaisesRegex(ValueError, "Список получателей пуст"):
                            email_sender.send_email("Subject", "Body")
                    self.assertIn("Список получателей пуст", logs.output[0])
        self.connect.assert_not_called()

    def test_invalid_json_prevents_sending(self):
        self.recipients_file.write_text("{invalid", encoding="utf-8")

        with self.assertLogs("email_sender", level="ERROR"), self.assertRaisesRegex(ValueError, "повреждён"):
            email_sender.send_email("Subject", "Body")

        self.connect.assert_not_called()

    def test_rejects_an_invalid_or_missing_attachment_before_connecting(self):
        with tempfile.TemporaryDirectory() as directory:
            other_type = Path(directory) / "notes.txt"
            other_type.write_text("plain text", encoding="utf-8")
            with self.assertLogs("email_sender", level="ERROR"), self.assertRaises(ValueError):
                email_sender.send_email("Subject", "Body", other_type)

            missing = Path(directory) / "absent.docx"
            with self.assertLogs("email_sender", level="ERROR"), self.assertRaises(FileNotFoundError):
                email_sender.send_email("Subject", "Body", missing)
        self.connect.assert_not_called()

    def test_propagates_authentication_errors_without_logging_credentials(self):
        password = self.settings["EMAIL_PASSWORD"]
        error = smtplib.SMTPAuthenticationError(535, f"Rejected {password}".encode())
        self.server.login.side_effect = error

        with self.assertLogs("email_sender", level="INFO") as logs:
            with self.assertRaises(smtplib.SMTPAuthenticationError) as raised:
                email_sender.send_email("Subject", "Body")

        self.assertIs(raised.exception, error)
        self.assertEqual([record.levelname for record in logs.records], ["ERROR"])
        self.assertNotIn(password, "\n".join(logs.output))
        self.server.send_message.assert_not_called()

    def test_raises_when_only_some_recipients_are_refused(self):
        refused = {"two@example.com": (550, b"Recipient refused")}
        self.server.send_message.return_value = refused

        with self.assertLogs("email_sender", level="INFO") as logs:
            with self.assertRaises(smtplib.SMTPRecipientsRefused) as raised:
                email_sender.send_email("Subject", "Body")

        self.assertEqual(raised.exception.recipients, refused)
        self.assertFalse(email_sender.is_definite_send_failure(raised.exception))
        self.assertEqual([record.levelname for record in logs.records], ["ERROR"])
        self.assertIn("1 из 2", logs.output[0])

    def test_connection_timeout_is_a_definite_failure(self):
        error = TimeoutError("SMTP timed out")
        self.connect.side_effect = error

        with self.assertLogs("email_sender", level="ERROR"):
            with self.assertRaises(email_sender.SMTPBeforeSendError) as raised:
                email_sender.send_email("Subject", "Body")

        self.assertIs(raised.exception.__cause__, error)
        self.assertTrue(email_sender.is_definite_send_failure(raised.exception))

    def test_timeout_during_submission_is_uncertain(self):
        error = TimeoutError("SMTP timed out during submission")
        self.server.send_message.side_effect = error

        with self.assertLogs("email_sender", level="ERROR"):
            with self.assertRaises(email_sender.SMTPDeliveryUncertain) as raised:
                email_sender.send_email("Subject", "Body")

        self.assertIs(raised.exception.__cause__, error)
        self.assertFalse(email_sender.is_definite_send_failure(raised.exception))

    def test_all_recipients_refused_is_a_definite_failure(self):
        error = smtplib.SMTPRecipientsRefused({"one@example.com": (550, b"Refused")})
        self.server.send_message.side_effect = error

        with self.assertLogs("email_sender", level="ERROR"):
            with self.assertRaises(smtplib.SMTPRecipientsRefused) as raised:
                email_sender.send_email("Subject", "Body")

        self.assertIs(raised.exception, error)
        self.assertTrue(email_sender.is_definite_send_failure(raised.exception))

    def test_quit_failure_does_not_change_confirmed_acceptance(self):
        self.connect.return_value.__exit__.side_effect = smtplib.SMTPResponseException(500, b"QUIT failed")

        with self.assertLogs("email_sender", level="INFO") as logs:
            email_sender.send_email("Subject", "Body")

        self.assertEqual([record.levelname for record in logs.records], ["WARNING", "INFO"])
