import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import utils


class GeneratedFileCleanupTests(unittest.TestCase):
    now = 1_800_000_000
    seconds_per_day = 86_400

    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.workspace = Path(workspace.name).resolve()
        self.generated = self.workspace / "generated"
        self.generated.mkdir()
        cwd_patch = patch("utils.Path.cwd", return_value=self.workspace)
        cwd_patch.start()
        self.addCleanup(cwd_patch.stop)
        time_patch = patch("utils.time", return_value=self.now)
        time_patch.start()
        self.addCleanup(time_patch.stop)

    def make_file(self, path, age_days=0):
        path.write_bytes(b"test")
        timestamp = self.now - age_days * self.seconds_per_day
        os.utime(path, (timestamp, timestamp))
        return path

    def make_symlink(self, link, target, *, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (NotImplementedError, OSError) as error:
            if directory and getattr(error, "winerror", None) == 1314:
                # Directory junctions do not require the Windows symlink privilege.
                from _winapi import CreateJunction

                CreateJunction(str(target), str(link))
                return
            self.skipTest(f"Symbolic links are unavailable: {error}")

    def test_removes_relative_and_absolute_docx_paths(self):
        first = self.make_file(self.generated / "first.docx")
        second = self.make_file(self.generated / "second.DOCX")

        with self.assertLogs("utils", level="INFO") as logs:
            utils.cleanup_generated_file(Path("generated") / first.name)
            utils.cleanup_generated_file(second)

        for path in (first, second):
            self.assertFalse(path.exists())
            self.assertTrue(any(path.name in message for message in logs.output))

    def test_rejects_outside_paths_other_extensions_and_directories(self):
        outside = self.make_file(self.workspace / "outside.docx")
        other_type = self.make_file(self.generated / "notes.txt")
        docx_directory = self.generated / "folder.docx"
        docx_directory.mkdir()

        for path in (outside, Path("generated/../outside.docx"), other_type, docx_directory):
            with self.subTest(path=path), self.assertLogs("utils", level="ERROR"):
                with self.assertRaises(ValueError):
                    utils.cleanup_generated_file(path)

        for path in (outside, other_type, docx_directory):
            self.assertTrue(path.exists())

    def test_missing_file_or_directory_is_a_no_op(self):
        missing = self.generated / "absent.docx"
        utils.cleanup_generated_file(missing)
        self.generated.rmdir()
        utils.cleanup_generated_file(missing)
        utils.cleanup_old_files()
        self.assertFalse(self.generated.exists())

    def test_bulk_cleanup_uses_modified_time_and_removes_only_old_docx(self):
        old = self.make_file(self.generated / "old.DOCX", age_days=8)
        recent = self.make_file(self.generated / "recent.docx", age_days=6)
        boundary = self.make_file(self.generated / "boundary.docx", age_days=7)
        other_type = self.make_file(self.generated / "old.txt", age_days=8)
        outside = self.make_file(self.workspace / "outside.docx", age_days=8)
        docx_directory = self.generated / "folder.docx"
        docx_directory.mkdir()

        with self.assertLogs("utils", level="INFO") as logs:
            utils.cleanup_old_files()

        self.assertFalse(old.exists())
        self.assertEqual(len(logs.output), 1)
        self.assertIn(old.name, logs.output[0])
        for path in (recent, boundary, other_type, outside, docx_directory):
            self.assertTrue(path.exists())

    def test_bulk_cleanup_accepts_a_custom_age(self):
        old = self.make_file(self.generated / "old.docx", age_days=3)
        recent = self.make_file(self.generated / "recent.docx", age_days=1)

        utils.cleanup_old_files(max_age_days=2)

        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_bulk_cleanup_leaves_subfolders_for_explicit_cleanup(self):
        nested = self.generated / "nested"
        nested.mkdir()
        old = self.make_file(nested / "old.docx", age_days=10)

        utils.cleanup_old_files()
        self.assertTrue(old.exists())

        utils.cleanup_generated_file(old)
        self.assertFalse(old.exists())

    def test_cleanup_does_not_follow_a_file_link_outside_generated(self):
        outside = self.make_file(self.workspace / "outside.docx", age_days=10)
        link = self.generated / "linked.docx"
        self.make_symlink(link, outside)

        with self.assertLogs("utils", level="ERROR"), self.assertRaises(ValueError):
            utils.cleanup_generated_file(link)
        utils.cleanup_old_files()

        self.assertTrue(outside.is_file())
        self.assertTrue(link.is_symlink())

    def test_single_file_cleanup_rejects_a_path_through_a_directory_link(self):
        outside = self.workspace / "outside"
        outside.mkdir()
        protected = self.make_file(outside / "protected.docx")
        link = self.generated / "linked"
        self.make_symlink(link, outside, directory=True)

        with self.assertLogs("utils", level="ERROR"), self.assertRaises(ValueError):
            utils.cleanup_generated_file(link / protected.name)

        self.assertTrue(protected.is_file())

    def test_cleanup_rejects_a_generated_directory_link(self):
        self.generated.rmdir()
        outside = self.workspace / "outside"
        outside.mkdir()
        protected = self.make_file(outside / "protected.docx", age_days=10)
        self.make_symlink(self.generated, outside, directory=True)

        with self.assertLogs("utils", level="ERROR"), self.assertRaises(ValueError):
            utils.cleanup_generated_file(self.generated / protected.name)
        with self.assertLogs("utils", level="ERROR"), self.assertRaises(ValueError):
            utils.cleanup_old_files()

        self.assertTrue(protected.is_file())

    def test_cleanup_logs_and_propagates_unlink_failures(self):
        path = self.make_file(self.generated / "locked.docx", age_days=10)
        for cleanup, args in (
            (utils.cleanup_generated_file, (path,)),
            (utils.cleanup_old_files, ()),
        ):
            with self.subTest(cleanup=cleanup.__name__):
                with patch("utils.Path.unlink", side_effect=PermissionError("file is locked")) as unlink:
                    with self.assertLogs("utils", level="ERROR") as logs:
                        with self.assertRaises(PermissionError):
                            cleanup(*args)

                unlink.assert_called_once_with(missing_ok=True)
                self.assertIn(path.name, logs.output[0])
                self.assertIn("file is locked", logs.output[0])
                self.assertTrue(path.exists())
