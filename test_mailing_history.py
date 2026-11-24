import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from time import monotonic, sleep
from unittest.mock import patch

import mailing_history as history


class MailingHistoryTests(unittest.TestCase):
    start = date(2026, 11, 16)
    end = date(2026, 11, 22)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "mailing_history.json"
        history_patch = patch.object(history, "HISTORY_FILE", self.path)
        history_patch.start()
        self.addCleanup(history_patch.stop)

    def test_missing_file_and_round_trip_with_only_required_fields(self):
        self.assertEqual(history.load_history(), [])
        attempt = history.new_attempt(self.start, self.end)
        with history.mailing_lock():
            history.save_history([attempt])

        self.assertEqual(history.load_history(), [attempt])
        self.assertEqual(set(attempt), {"start_date", "end_date", "timestamp", "status"})
        self.assertIsNotNone(datetime.fromisoformat(attempt["timestamp"]).utcoffset())
        self.assertEqual(attempt["status"], "pending")

    def test_failed_attempt_does_not_override_an_earlier_blocking_status(self):
        attempt = history.new_attempt(self.start, self.end)
        failed = {**attempt, "status": "failed"}
        unrelated = history.new_attempt(self.start, self.start)

        for status in ("pending", "sent", "uncertain"):
            with self.subTest(status=status):
                attempts = [{**attempt, "status": status}, failed, unrelated]
                self.assertEqual(history.last_blocking_status(attempts, self.start, self.end), status)
        self.assertIsNone(history.last_blocking_status([failed], self.start, self.end))

    def test_rejects_damaged_or_invalid_history_without_writing(self):
        attempt = history.new_attempt(self.start, self.end)
        for original in (
            b"invalid json",
            b"{}",
            json.dumps([{**attempt, "status": "unknown"}]).encode(),
            json.dumps([{**attempt, "end_date": "2026-11-15"}]).encode(),
            json.dumps([{**attempt, "timestamp": "2026-11-22T12:00:00"}]).encode(),
            json.dumps([{**attempt, "unexpected": "value"}]).encode(),
        ):
            with self.subTest(original=original):
                self.path.write_bytes(original)
                with self.assertRaises(history.HistoryError):
                    history.load_history()
                self.assertEqual(self.path.read_bytes(), original)

    def test_failed_atomic_replace_preserves_original(self):
        attempt = history.new_attempt(self.start, self.end)
        with history.mailing_lock():
            history.save_history([attempt])
            original = self.path.read_bytes()
            with patch("mailing_history.os.replace", side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    history.save_history([{**attempt, "status": "sent"}])

        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob(".mailing_history.json.*.tmp")), [])

    def test_two_processes_running_main_send_only_once(self):
        worker = """
import sys
import time
from pathlib import Path
from unittest.mock import patch

import main
import mailing_history

mailing_history.HISTORY_FILE = Path(sys.argv[1])
marker = mailing_history.HISTORY_FILE.with_name('submitted.txt')
contender = mailing_history.HISTORY_FILE.with_name('contender-ready')

def send(*args):
    assert mailing_history.load_history()[-1]['status'] == 'pending'
    with marker.open('a', encoding='utf-8') as output:
        output.write('submitted\\n')
    deadline = time.monotonic() + 15
    while not contender.exists():
        assert time.monotonic() < deadline, 'Second process did not start'
        time.sleep(0.02)
    time.sleep(0.3)

with patch('main.load_recipients', return_value=['test@example.com']), patch('main.cleanup_old_files'), patch('main.process_documents', return_value='generated/test.docx'), patch('main.send_email', side_effect=send), patch('main.cleanup_generated_file'):
    if sys.argv[2] == 'contender':
        contender.touch()
    raise SystemExit(main.main(['--start-date', '2026-11-16', '--end-date', '2026-11-22']))
"""

        def run(role):
            return subprocess.run(
                [sys.executable, "-B", "-X", "utf8", "-c", worker, str(self.path), role],
                cwd=Path(history.__file__).resolve().parent,
                capture_output=True, encoding="utf-8", timeout=30,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(run, "first")
            deadline = monotonic() + 10
            marker = self.path.with_name("submitted.txt")
            while not marker.exists() and not first.done() and monotonic() < deadline:
                sleep(0.02)
            self.assertTrue(marker.exists(), "The first process did not reach SMTP.")
            second = executor.submit(run, "contender")
            for result in (first.result(), second.result()):
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.path.with_name("submitted.txt").read_text(encoding="utf-8").splitlines(), [
            "submitted",
        ])
        self.assertEqual([attempt["status"] for attempt in history.load_history()], ["sent"])
