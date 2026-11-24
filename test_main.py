import smtplib
import unittest
from contextlib import redirect_stderr
from datetime import date, datetime, timezone
from io import StringIO
from unittest.mock import Mock, call, patch
from zoneinfo import ZoneInfoNotFoundError

import requests

import main as mailer


class MailerMainTests(unittest.TestCase):
    start = date(2026, 11, 16)
    end = date(2026, 11, 22)
    period = ["--start-date", start.isoformat(), "--end-date", end.isoformat()]
    document = "generated/Документы за неделю.docx"

    def setUp(self):
        self.steps = Mock()
        for name in (
            "cleanup_old_files",
            "process_documents",
            "send_email",
            "cleanup_generated_file",
        ):
            patcher = patch.object(mailer, name)
            operation = patcher.start()
            self.addCleanup(patcher.stop)
            self.steps.attach_mock(operation, name)
        self.steps.process_documents.return_value = self.document

    def test_uses_previous_complete_week_at_moscow_midnight(self):
        cases = (
            (datetime(2026, 11, 22, 20, 59, tzinfo=timezone.utc), date(2026, 11, 9), date(2026, 11, 15)),
            (datetime(2026, 11, 22, 21, 0, tzinfo=timezone.utc), self.start, self.end),
        )
        for instant, start, end in cases:
            with self.subTest(instant=instant), patch.object(mailer, "datetime") as clock:
                clock.now.side_effect = instant.astimezone
                with self.assertLogs("main", level="INFO"):
                    self.assertEqual(mailer.main([]), 0)

                self.assertEqual(clock.now.call_args[0][0].key, "Europe/Moscow")
                self.steps.process_documents.assert_called_once_with(start, end)
                self.steps.reset_mock()

    def test_missing_timezone_data_stops_before_cleanup(self):
        with patch.object(mailer, "ZoneInfo", side_effect=ZoneInfoNotFoundError):
            with self.assertLogs("main", level="ERROR") as logs:
                self.assertEqual(mailer.main([]), 1)

        self.assertIn("tzdata", logs.output[0])
        self.assertEqual(self.steps.mock_calls, [])

    def test_custom_period_sends_and_then_deletes_attachment(self):
        period = ["--start-date", "2026-11-19", "--end-date", "2026-11-19"]
        day = date(2026, 11, 19)
        subject = "Подборка нормативных правовых актов за 19.11.2026 — 19.11.2026"
        body = (
            "Коллеги, добрый день!\n\n"
            "Направляем еженедельную подборку нормативных правовых актов, "
            "опубликованных за период с 19.11.2026 по 19.11.2026.\n\n"
            "Подборка документов представлена во вложении.\n\n"
            "Письмо сформировано автоматически. Отвечать на него не требуется."
        )

        with self.assertLogs("main", level="INFO"):
            self.assertEqual(mailer.main(period), 0)

        self.assertEqual(self.steps.mock_calls, [
            call.cleanup_old_files(max_age_days=7),
            call.process_documents(day, day),
            call.send_email(subject, body, self.document),
            call.cleanup_generated_file(self.document),
        ])

    def test_invalid_periods_exit_before_any_operations(self):
        cases = (
            ["--start-date", "2026-11-19"],
            ["--end-date", "2026-11-19"],
            ["--start-date", "2026-11-31", "--end-date", "2026-12-01"],
            ["--start-date", "20261119", "--end-date", "2026-11-22"],
            ["--start-date", "2026-11-22", "--end-date", "2026-11-19"],
        )
        for args in cases:
            with self.subTest(args=args), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    mailer.main(args)
                self.assertEqual(raised.exception.code, 2)

        self.assertEqual(self.steps.mock_calls, [])

    def test_old_file_cleanup_failure_stops_run(self):
        self.steps.cleanup_old_files.side_effect = PermissionError("locked directory")

        with self.assertLogs("main", level="ERROR") as logs:
            self.assertEqual(mailer.main(self.period), 1)

        self.assertIn("Не удалось очистить старые документы", logs.output[0])
        self.assertEqual(self.steps.mock_calls, [call.cleanup_old_files(max_age_days=7)])

    def test_api_failure_prevents_email_and_attachment_cleanup(self):
        self.steps.process_documents.side_effect = requests.exceptions.Timeout("API timeout")

        with self.assertLogs("main", level="ERROR") as logs:
            self.assertEqual(mailer.main(self.period), 1)

        self.assertIn("Не удалось сформировать подборку", logs.output[0])
        self.assertEqual(self.steps.mock_calls, [
            call.cleanup_old_files(max_age_days=7),
            call.process_documents(self.start, self.end),
        ])

    def test_smtp_errors_keep_attachment_and_redact_exception_text(self):
        secret = "test-only-secret"
        cases = (
            smtplib.SMTPAuthenticationError(535, secret.encode()),
            smtplib.SMTPRecipientsRefused({"test@example.com": (550, secret.encode())}),
        )
        for error in cases:
            with self.subTest(error=type(error).__name__):
                self.steps.send_email.side_effect = error
                with self.assertLogs("main", level="ERROR") as logs:
                    self.assertEqual(mailer.main(self.period), 1)

                self.assertIn("DOCX сохранён", logs.output[0])
                self.assertNotIn(secret, "\n".join(logs.output))
                self.steps.send_email.assert_called_once()
                self.steps.cleanup_generated_file.assert_not_called()
                self.steps.reset_mock()

    def test_attachment_cleanup_failure_is_reported_without_resending(self):
        self.steps.cleanup_generated_file.side_effect = PermissionError("locked document")

        with self.assertLogs("main", level="ERROR") as logs:
            self.assertEqual(mailer.main(self.period), 1)

        self.assertIn("SMTP-сервер принял письмо", logs.output[0])
        self.assertIn("удалить вложение не удалось", logs.output[0])
        self.steps.send_email.assert_called_once()
        self.steps.cleanup_generated_file.assert_called_once_with(self.document)
