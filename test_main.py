import smtplib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, call, patch
from zoneinfo import ZoneInfoNotFoundError

import requests

import main as mailer
import mailing_history
from email_sender import SMTPPartialDelivery


class MailerMainTests(unittest.TestCase):
    start = date(2026, 11, 16)
    end = date(2026, 11, 22)
    period = ["--start-date", start.isoformat(), "--end-date", end.isoformat()]
    document = "generated/Документы за неделю.docx"

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.history_path = Path(directory.name) / "mailing_history.json"
        history_patch = patch.object(mailing_history, "HISTORY_FILE", self.history_path)
        history_patch.start()
        self.addCleanup(history_patch.stop)
        self.steps = Mock()
        for name in (
            "load_recipients",
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
        self.steps.load_recipients.return_value = ["test@example.com"]

    def statuses(self):
        return [attempt["status"] for attempt in mailing_history.load_history()]

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

        def verify_pending(*args):
            self.assertEqual(self.statuses(), ["pending"])

        def verify_sent(*args):
            self.assertEqual(self.statuses(), ["sent"])

        self.steps.send_email.side_effect = verify_pending
        self.steps.cleanup_generated_file.side_effect = verify_sent
        with self.assertLogs("main", level="INFO"):
            self.assertEqual(mailer.main(period), 0)

        self.assertEqual(self.steps.mock_calls, [
            call.load_recipients(),
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
        self.assertEqual(self.steps.mock_calls, [
            call.load_recipients(),
            call.cleanup_old_files(max_age_days=7),
        ])
        self.assertEqual(self.statuses(), ["failed"])

    def test_api_failure_prevents_email_and_attachment_cleanup(self):
        self.steps.process_documents.side_effect = requests.exceptions.Timeout("API timeout")

        with self.assertLogs("main", level="ERROR") as logs:
            self.assertEqual(mailer.main(self.period), 1)

        self.assertIn("Не удалось сформировать подборку", logs.output[0])
        self.assertEqual(self.steps.mock_calls, [
            call.load_recipients(),
            call.cleanup_old_files(max_age_days=7),
            call.process_documents(self.start, self.end),
        ])
        self.assertEqual(self.statuses(), ["failed"])

    def test_definite_smtp_rejections_keep_attachment_and_record_failed(self):
        secret = "test-only-secret"
        cases = (
            smtplib.SMTPAuthenticationError(535, secret.encode()),
            smtplib.SMTPRecipientsRefused({"test@example.com": (550, secret.encode())}),
            smtplib.SMTPDataError(554, secret.encode()),
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
        self.assertEqual(self.statuses(), ["failed"] * len(cases))

    def test_uncertain_smtp_errors_block_retry_and_keep_attachment(self):
        secret = "test-only-secret"
        for error in (
            SMTPPartialDelivery({"test@example.com": (550, secret.encode())}),
            TimeoutError(secret),
            smtplib.SMTPServerDisconnected(secret),
        ):
            with self.subTest(error=type(error).__name__):
                self.history_path.unlink(missing_ok=True)
                self.steps.reset_mock()
                self.steps.send_email.side_effect = error
                with self.assertLogs("main", level="WARNING") as logs:
                    self.assertEqual(mailer.main(self.period), 1)
                    self.assertEqual(mailer.main(self.period), 1)

                self.assertEqual(self.statuses(), ["uncertain"])
                self.assertNotIn(secret, "\n".join(logs.output))
                self.assertNotIn("test@example.com", self.history_path.read_text(encoding="utf-8"))
                self.steps.send_email.assert_called_once()
                self.steps.cleanup_generated_file.assert_not_called()
                self.assertIn("вручную", logs.output[-1])

    def test_attachment_cleanup_failure_is_reported_without_resending(self):
        self.steps.cleanup_generated_file.side_effect = PermissionError("locked document")

        with self.assertLogs("main", level="ERROR") as logs:
            self.assertEqual(mailer.main(self.period), 1)

        self.assertIn("SMTP-сервер принял письмо", logs.output[0])
        self.assertIn("удалить вложение не удалось", logs.output[0])
        self.steps.send_email.assert_called_once()
        self.steps.cleanup_generated_file.assert_called_once_with(self.document)
        self.assertEqual(self.statuses(), ["sent"])
        with self.assertLogs("main", level="INFO"):
            self.assertEqual(mailer.main(self.period), 0)
        self.steps.send_email.assert_called_once()

    def test_duplicate_is_skipped_and_force_records_another_attempt(self):
        with self.assertLogs("main", level="INFO"):
            self.assertEqual(mailer.main(self.period), 0)
            self.steps.reset_mock()
            self.assertEqual(mailer.main(self.period), 0)
            self.assertEqual(self.steps.mock_calls, [])
            self.assertEqual(mailer.main([*self.period, "--force"]), 0)

        self.assertEqual(self.statuses(), ["sent", "sent"])
        self.steps.send_email.assert_called_once()

    def test_overlapping_but_different_period_is_not_a_duplicate(self):
        with self.assertLogs("main", level="INFO"):
            self.assertEqual(mailer.main(self.period), 0)
            self.assertEqual(mailer.main(["--start-date", "2026-11-17", *self.period[2:]]), 0)

        self.assertEqual(self.statuses(), ["sent", "sent"])
        self.assertEqual(self.steps.send_email.call_count, 2)

    def test_interrupted_submission_leaves_pending_and_blocks_until_force(self):
        self.steps.send_email.side_effect = SystemExit(77)
        with self.assertLogs("main", level="INFO"):
            with self.assertRaises(SystemExit):
                mailer.main(self.period)

        self.assertEqual(self.statuses(), ["pending"])
        self.steps.reset_mock()
        self.steps.send_email.side_effect = None
        with self.assertLogs("main", level="WARNING") as logs:
            self.assertEqual(mailer.main(self.period), 1)
            self.assertEqual(self.steps.mock_calls, [])
            self.assertIn("вручную", logs.output[0])
            self.assertEqual(mailer.main([*self.period, "--force"]), 0)

        self.assertEqual(self.statuses(), ["pending", "sent"])

    def test_status_write_failure_after_acceptance_keeps_pending_and_attachment(self):
        def fail_sent_save(attempts):
            if attempts[-1]["status"] == "sent":
                raise PermissionError
            mailing_history.save_history(attempts)

        with patch.object(mailer, "save_history", side_effect=fail_sent_save):
            with self.assertLogs("main", level="ERROR") as logs:
                self.assertEqual(mailer.main(self.period), 1)

        self.assertEqual(self.statuses(), ["pending"])
        self.steps.cleanup_generated_file.assert_not_called()
        self.assertIn("DOCX сохранён", logs.output[0])
        with self.assertLogs("main", level="WARNING"):
            self.assertEqual(mailer.main(self.period), 1)
        self.steps.send_email.assert_called_once()

    def test_failed_pending_write_prevents_smtp(self):
        with patch.object(mailer, "save_history", side_effect=PermissionError):
            with self.assertLogs("main", level="ERROR"):
                self.assertEqual(mailer.main(self.period), 1)

        self.assertEqual(self.statuses(), [])
        self.steps.send_email.assert_not_called()
        self.steps.cleanup_generated_file.assert_not_called()

    def test_empty_recipient_list_is_checked_before_generation(self):
        self.steps.load_recipients.return_value = []
        with self.assertLogs("main", level="ERROR") as logs:
            self.assertEqual(mailer.main(self.period), 1)

        self.assertIn("Список получателей пуст", logs.output[0])
        self.assertEqual(self.steps.mock_calls, [call.load_recipients()])
        self.assertEqual(self.statuses(), ["failed"])

    def test_corrupt_history_blocks_force_and_normal_runs(self):
        original = b'{invalid'
        self.history_path.write_bytes(original)
        for args in (self.period, [*self.period, "--force"], ["--history"]):
            with self.subTest(args=args), self.assertLogs("main", level="ERROR") as logs:
                self.assertEqual(mailer.main(args), 1)
            self.assertIn("История повреждена", logs.output[0])
            self.assertEqual(self.history_path.read_bytes(), original)
        self.assertEqual(self.steps.mock_calls, [])

    def test_history_displays_in_russian_without_starting_a_mailing(self):
        with self.assertLogs("main", level="INFO"):
            self.assertEqual(mailer.main(self.period), 0)
        self.steps.reset_mock()
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(mailer.main(["--history"]), 0)

        self.assertIn("2026-11-16 — 2026-11-22", output.getvalue())
        self.assertIn("отправлено", output.getvalue())
        self.assertEqual(self.steps.mock_calls, [])
