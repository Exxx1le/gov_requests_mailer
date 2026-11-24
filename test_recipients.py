import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import recipients


class RecipientsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.project = Path(directory.name)
        self.store = self.project / "recipients.json"
        for patcher in (
            patch.object(recipients, "PROJECT_DIR", self.project),
            patch.object(recipients, "RECIPIENTS_FILE", self.store),
            patch.dict(os.environ, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def cli(self, *args):
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            try:
                status = recipients.main(args)
            except SystemExit as error:
                status = error.code
        return status, output.getvalue(), errors.getvalue()

    def test_missing_store_is_empty_without_creating_it(self):
        self.assertEqual(recipients.load_recipients(), [])
        self.assertEqual(self.cli("list"), (0, "Список получателей пуст.\n", ""))
        self.assertEqual(self.cli("count"), (0, "Получателей: 0.\n", ""))
        self.assertFalse(self.store.exists())

    def test_add_list_count_and_remove(self):
        self.assertEqual(self.cli("add", "One@example.com")[0], 0)
        self.assertEqual(self.cli("add", "two+tag@example.com")[0], 0)
        self.assertEqual(self.cli("list"), (0, "One@example.com\ntwo+tag@example.com\n", ""))
        self.assertEqual(self.cli("count"), (0, "Получателей: 2.\n", ""))
        self.assertEqual(self.cli("remove", "one@EXAMPLE.COM")[0], 0)
        self.assertEqual(json.loads(self.store.read_text(encoding="utf-8")), ["two+tag@example.com"])

    def test_duplicate_invalid_and_missing_addresses_leave_store_unchanged(self):
        self.assertEqual(self.cli("add", "one@example.com")[0], 0)
        original = self.store.read_bytes()
        cases = (
            (("add", "ONE@EXAMPLE.COM"), "уже есть"),
            (("add", "broken"), "Некорректный адрес"),
            (("add", "one@example..com"), "Некорректный адрес"),
            (("remove", "missing@example.com"), "не найден"),
        )
        for args, message in cases:
            with self.subTest(args=args):
                status, _, error = self.cli(*args)
                self.assertEqual(status, 1)
                self.assertIn(message, error)
                self.assertEqual(self.store.read_bytes(), original)

    def test_corrupt_or_invalid_json_is_never_replaced(self):
        for original in (
            b"[", b"\xff", b"{}", b"[42]", b'["invalid"]',
            b'["one@example.com", "ONE@example.com"]',
        ):
            with self.subTest(original=original):
                self.store.write_bytes(original)
                with self.assertRaises(recipients.RecipientsError):
                    recipients.load_recipients()
                self.assertEqual(self.cli("add", "new@example.com")[0], 1)
                self.assertEqual(self.cli("remove", "new@example.com")[0], 1)
                self.assertEqual(self.cli("migrate")[0], 1)
                self.assertEqual(self.store.read_bytes(), original)

    def test_migrates_dotenv_once_without_modifying_it(self):
        dotenv_file = self.project / ".env"
        raw = b"RECIPIENTS=one@example.com, ONE@example.com, two@example.com\n"
        dotenv_file.write_bytes(raw)

        self.assertEqual(self.cli("migrate"), (0, "Перенесено адресов: 2.\n", ""))
        self.assertEqual(recipients.load_recipients(), ["one@example.com", "two@example.com"])
        status, _, error = self.cli("migrate")
        self.assertEqual(status, 1)
        self.assertIn("уже существует", error)
        self.assertEqual(dotenv_file.read_bytes(), raw)

    def test_migration_uses_environment_first_and_keeps_existing_empty_list(self):
        (self.project / ".env").write_text("RECIPIENTS=file@example.com\n", encoding="utf-8")
        with patch.dict(os.environ, {"RECIPIENTS": "env@example.com"}):
            self.store.write_bytes(b"[]")
            self.assertEqual(self.cli("migrate")[0], 1)
            self.assertEqual(self.store.read_bytes(), b"[]")
            self.store.unlink()

            self.assertEqual(self.cli("migrate")[0], 0)
            self.assertEqual(recipients.load_recipients(), ["env@example.com"])

    def test_migration_rejects_missing_or_invalid_legacy_values(self):
        self.assertIn("нет адресов", self.cli("migrate")[2])
        with patch.dict(os.environ, {"RECIPIENTS": "one@example.com,invalid"}):
            self.assertEqual(self.cli("migrate")[0], 1)
        self.assertFalse(self.store.exists())

    def test_migration_reports_dotenv_parse_warnings_in_russian(self):
        (self.project / ".env").write_bytes(b"invalid line\nRECIPIENTS=one@example.com\n")

        status, _, error = self.cli("migrate")

        self.assertEqual(status, 0)
        self.assertIn("Предупреждение:", error)
        self.assertNotIn("invalid line", error)
        self.assertEqual(recipients.load_recipients(), ["one@example.com"])

    def test_replace_failure_preserves_original_and_cleans_temporary_file(self):
        self.assertEqual(self.cli("add", "one@example.com")[0], 0)
        original = self.store.read_bytes()
        with patch("recipients.os.replace", side_effect=PermissionError):
            self.assertEqual(self.cli("add", "two@example.com")[0], 1)
        self.assertEqual(self.store.read_bytes(), original)
        self.assertEqual(list(self.project.glob(".recipients.json.*.tmp")), [])
        self.assertTrue((self.project / "recipients.json.lock").is_file())

    def test_help_and_invalid_arguments_are_in_russian(self):
        status, output, _ = self.cli("--help")
        self.assertEqual(status, 0)
        for fragment in ("Использование:", "Параметры:", "Команды:", "migrate", "add"):
            self.assertIn(fragment, output)
        for args in (("unknown",), ("add",), ("list", "extra")):
            with self.subTest(args=args):
                status, _, error = self.cli(*args)
                self.assertEqual(status, 2)
                self.assertIn("Ошибка:", error)
                self.assertIn("Использование:", error)


class RecipientsProcessTests(unittest.TestCase):
    def test_parallel_commands_share_the_store_beside_the_module(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            script = project / "recipients.py"
            shutil.copyfile(recipients.__file__, script)

            def add(number):
                return subprocess.run(
                    [sys.executable, "-B", "-X", "utf8", str(script), "add", f"user{number}@example.com"],
                    cwd=root, capture_output=True, encoding="utf-8", timeout=30,
                )

            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(add, range(12)))
            for result in results:
                self.assertEqual(result.returncode, 0, result.stderr)
            stored = json.loads((project / "recipients.json").read_text(encoding="utf-8"))
            self.assertCountEqual(stored, [f"user{i}@example.com" for i in range(12)])
            self.assertFalse((root / "recipients.json").exists())
