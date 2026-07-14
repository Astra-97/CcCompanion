import contextlib
import io
import os
from pathlib import Path
import tempfile
import time
import unittest

from scripts.cleanup_chat_attachments import (
    CleanupError,
    JOURNAL_PREFIX,
    NANOSECONDS_PER_DAY,
    QUARANTINE_PREFIX,
    cleanup,
    main,
)


class SimulatedCrash(BaseException):
    pass


class AttachmentCleanupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "attachments"
        self.root.mkdir()
        self.base_now = time.time_ns()
        self.future_now = self.base_now + 40 * NANOSECONDS_PER_DAY

    def tearDown(self):
        self.temp.cleanup()

    def make_file(self, name: str, content: bytes = b"data") -> Path:
        path = self.root / name
        path.write_bytes(content)
        return path

    def journals(self) -> list[Path]:
        return [path for path in self.root.iterdir() if path.name.startswith(JOURNAL_PREFIX)]

    def quarantines(self) -> list[Path]:
        return [path for path in self.root.iterdir() if path.name.startswith(QUARANTINE_PREFIX)]

    def test_dry_run_reports_but_keeps_old_regular_file(self):
        old = self.make_file("old.jpg", b"old")

        result = cleanup(self.root, now_ns=self.future_now)

        self.assertEqual(result.candidates, 1)
        self.assertEqual(result.removed, 0)
        self.assertTrue(old.exists())

    def test_apply_removes_old_but_keeps_recent_file(self):
        old = self.make_file("old.webp", b"old")
        recent = self.make_file("recent.png", b"recent")
        os.utime(recent, ns=(self.future_now, self.future_now))

        result = cleanup(self.root, apply=True, now_ns=self.future_now)

        self.assertEqual(result.removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_file_newer_than_cutoff_is_kept(self):
        recent = self.make_file("recent.png")
        now = time.time_ns()

        result = cleanup(self.root, apply=True, now_ns=now)

        self.assertEqual(result.candidates, 0)
        self.assertTrue(recent.exists())

    def test_symlink_and_subdirectory_are_never_removed_or_followed(self):
        outside = Path(self.temp.name) / "portrait.jpg"
        outside.write_bytes(b"portrait")
        symlink = self.root / "link.jpg"
        symlink.symlink_to(outside)
        nested = self.root / "nested"
        nested.mkdir()
        nested_file = nested / "old.jpg"
        nested_file.write_bytes(b"nested")

        result = cleanup(self.root, apply=True, now_ns=self.future_now)

        self.assertEqual(result.removed, 0)
        self.assertEqual(result.skipped_non_regular, 2)
        self.assertTrue(symlink.is_symlink())
        self.assertEqual(outside.read_bytes(), b"portrait")
        self.assertTrue(nested_file.exists())

    def test_delete_count_is_bounded(self):
        for number in range(3):
            self.make_file(f"{number}.bin")

        result = cleanup(
            self.root, apply=True, max_files=2, now_ns=self.future_now
        )

        self.assertEqual(result.candidates, 3)
        self.assertEqual(result.removed, 2)
        self.assertTrue(result.delete_limit_reached)
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_scan_count_is_bounded(self):
        for number in range(3):
            self.make_file(f"{number}.bin")

        result = cleanup(self.root, max_scan=2, now_ns=self.future_now)

        self.assertEqual(result.scanned, 2)
        self.assertTrue(result.scan_limit_reached)

    def test_replacement_before_quarantine_is_restored_not_deleted(self):
        original = self.make_file("message.jpg", b"original")
        saved_original = Path(self.temp.name) / "saved-original"

        def replace(_root_fd: int, name: str) -> None:
            (self.root / name).rename(saved_original)
            (self.root / name).write_bytes(b"replacement")

        result = cleanup(
            self.root,
            apply=True,
            now_ns=self.future_now,
            _before_quarantine=replace,
        )

        self.assertEqual(result.removed, 0)
        self.assertEqual(result.skipped_changed, 1)
        self.assertEqual(original.read_bytes(), b"replacement")
        self.assertEqual(saved_original.read_bytes(), b"original")
        self.assertFalse(any(p.name.startswith(QUARANTINE_PREFIX) for p in self.root.iterdir()))
        self.assertEqual(self.journals(), [])

    def test_restore_never_overwrites_newer_name_and_quarantine_is_retained(self):
        original = self.make_file("message.jpg", b"original")
        saved_original = Path(self.temp.name) / "saved-original"

        def replace(_root_fd: int, name: str) -> None:
            (self.root / name).rename(saved_original)
            (self.root / name).write_bytes(b"captured-replacement")

        def occupy_before_restore(_root_fd: int, name: str, _quarantine: str) -> None:
            (self.root / name).write_bytes(b"newer-name")

        result = cleanup(
            self.root,
            apply=True,
            now_ns=self.future_now,
            _before_quarantine=replace,
            _before_restore=occupy_before_restore,
        )

        quarantines = self.quarantines()
        self.assertEqual(result.removed, 0)
        self.assertEqual(result.journal_retained, 1)
        self.assertEqual(result.errors, 1)
        self.assertEqual(original.read_bytes(), b"newer-name")
        self.assertEqual(saved_original.read_bytes(), b"original")
        self.assertEqual(len(quarantines), 1)
        self.assertEqual(len(self.journals()), 1)
        self.assertEqual(quarantines[0].read_bytes(), b"captured-replacement")

        second = cleanup(self.root, apply=True, now_ns=self.future_now)
        self.assertGreaterEqual(second.journal_retained, 1)
        self.assertTrue(quarantines[0].exists())

    def test_crash_after_journal_before_rename_recovers_without_stranding(self):
        original = self.make_file("message.jpg", b"original")

        def crash(*_args) -> None:
            raise SimulatedCrash()

        with self.assertRaises(SimulatedCrash):
            cleanup(
                self.root,
                apply=True,
                now_ns=self.future_now,
                _after_journal=crash,
            )
        self.assertTrue(original.exists())
        self.assertEqual(len(self.journals()), 1)
        self.assertEqual(self.journals()[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.quarantines(), [])

        recovered = cleanup(self.root, apply=True, now_ns=self.future_now)
        self.assertEqual(recovered.recovery_cleared, 1)
        self.assertEqual(recovered.removed, 0)
        self.assertTrue(original.exists())
        self.assertEqual(self.journals(), [])

    def test_crash_after_rename_of_old_candidate_finishes_delete(self):
        original = self.make_file("message.jpg", b"original")

        def crash(*_args) -> None:
            raise SimulatedCrash()

        with self.assertRaises(SimulatedCrash):
            cleanup(
                self.root,
                apply=True,
                now_ns=self.future_now,
                _after_rename=crash,
            )
        self.assertFalse(original.exists())
        self.assertEqual(len(self.journals()), 1)
        self.assertEqual(len(self.quarantines()), 1)

        recovered = cleanup(self.root, apply=True, now_ns=self.future_now)
        self.assertEqual(recovered.recovery_removed, 1)
        self.assertFalse(original.exists())
        self.assertEqual(self.journals(), [])
        self.assertEqual(self.quarantines(), [])

    def test_crash_after_rename_capturing_replacement_restores_replacement(self):
        original = self.make_file("message.jpg", b"original")
        saved_original = Path(self.temp.name) / "saved-original"

        def replace(_root_fd: int, name: str) -> None:
            (self.root / name).rename(saved_original)
            (self.root / name).write_bytes(b"replacement")

        def crash(*_args) -> None:
            raise SimulatedCrash()

        with self.assertRaises(SimulatedCrash):
            cleanup(
                self.root,
                apply=True,
                now_ns=self.future_now,
                _before_quarantine=replace,
                _after_rename=crash,
            )
        self.assertFalse(original.exists())

        recovered = cleanup(self.root, apply=True, now_ns=self.future_now)
        self.assertEqual(recovered.recovery_restored, 1)
        self.assertEqual(original.read_bytes(), b"replacement")
        self.assertEqual(saved_original.read_bytes(), b"original")
        self.assertEqual(self.journals(), [])
        self.assertEqual(self.quarantines(), [])

    def test_crash_after_delete_before_marker_cleanup_clears_marker(self):
        original = self.make_file("message.jpg", b"original")

        def crash(*_args) -> None:
            raise SimulatedCrash()

        with self.assertRaises(SimulatedCrash):
            cleanup(
                self.root,
                apply=True,
                now_ns=self.future_now,
                _after_delete=crash,
            )
        self.assertFalse(original.exists())
        self.assertEqual(len(self.journals()), 1)
        self.assertEqual(self.quarantines(), [])

        recovered = cleanup(self.root, apply=True, now_ns=self.future_now)
        self.assertEqual(recovered.recovery_cleared, 1)
        self.assertEqual(self.journals(), [])

    def test_corrupt_and_symlink_journals_are_retained_without_following(self):
        corrupt = self.root / f"{JOURNAL_PREFIX}{'a' * 32}.json"
        corrupt.write_text("not-json")
        corrupt.chmod(0o600)
        outside = Path(self.temp.name) / "outside"
        outside.write_text("outside")
        symlink = self.root / f"{JOURNAL_PREFIX}{'b' * 32}.json"
        symlink.symlink_to(outside)

        result = cleanup(self.root, apply=True, now_ns=self.future_now)

        self.assertEqual(result.journal_retained, 2)
        self.assertEqual(result.errors, 2)
        self.assertTrue(corrupt.exists())
        self.assertTrue(symlink.is_symlink())
        self.assertEqual(outside.read_text(), "outside")

    def test_crash_recovery_never_overwrites_occupied_restore_destination(self):
        original = self.make_file("message.jpg", b"original")
        saved_original = Path(self.temp.name) / "saved-original"

        def replace(_root_fd: int, name: str) -> None:
            (self.root / name).rename(saved_original)
            (self.root / name).write_bytes(b"captured-replacement")

        def crash(*_args) -> None:
            raise SimulatedCrash()

        with self.assertRaises(SimulatedCrash):
            cleanup(
                self.root,
                apply=True,
                now_ns=self.future_now,
                _before_quarantine=replace,
                _after_rename=crash,
            )
        original.write_bytes(b"newer-name")

        recovered = cleanup(self.root, apply=True, now_ns=self.future_now)

        self.assertEqual(recovered.journal_retained, 1)
        self.assertEqual(recovered.errors, 1)
        self.assertEqual(original.read_bytes(), b"newer-name")
        self.assertEqual(saved_original.read_bytes(), b"original")
        self.assertEqual(len(self.journals()), 1)
        self.assertEqual(len(self.quarantines()), 1)
        self.assertEqual(self.quarantines()[0].read_bytes(), b"captured-replacement")

    def test_symlink_root_is_rejected(self):
        root_link = Path(self.temp.name) / "attachment-link"
        root_link.symlink_to(self.root, target_is_directory=True)

        with self.assertRaises(CleanupError):
            cleanup(root_link, now_ns=self.future_now)

    def test_cli_summary_does_not_log_attachment_name_or_root(self):
        sensitive_name = "private-photo.jpg"
        self.make_file(sensitive_name)
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            code = main([
                "--root", str(self.root),
                "--older-than-days", "30",
            ])

        self.assertEqual(code, 0)
        self.assertNotIn(sensitive_name, output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())
        self.assertIn("mode=dry-run", output.getvalue())


if __name__ == "__main__":
    unittest.main()
