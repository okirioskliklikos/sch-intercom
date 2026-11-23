"""Έλεγχοι τοπικού ρόλου Manager χωρίς πραγματικά δεδομένα σχολείου."""
import io
import json
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from director.manager_state import (
    ManagerState, RoleError, acknowledge, change_role, change_serving,
    describe, load, main, require_primary, restored, save,
)


class ManagerStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "manager-state.json"
        self.registry = Path(self.temp.name) / "clients-registry.json"

    def test_missing_defaults_to_primary_serving(self):
        self.assertEqual(load(self.path), ManagerState())
        require_primary(load(self.path))

    def test_unknown_format_fails_closed(self):
        self.path.write_text('{"format":2}', encoding="utf-8")
        with redirect_stderr(io.StringIO()) as output:
            state = load(self.path)
        self.assertEqual((state.role, state.serving), ("backup", False))
        self.assertIn("Μη έγκυρο", output.getvalue())

    def test_corrupt_content_fails_closed(self):
        self.path.write_bytes(b"not-json")
        with redirect_stderr(io.StringIO()):
            self.assertFalse(load(self.path).serving)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(load(self.path).role, "backup")

    def test_atomic_save_and_round_trip(self):
        save(self.path, ManagerState(role="backup", serving=False))
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document["format"], 1)
        self.assertEqual(document["role"], "backup")
        self.assertFalse(document["serving"])
        self.assertTrue(document["changed_at"].endswith("Z"))
        self.assertEqual(load(self.path).role, "backup")
        self.assertEqual(list(self.path.parent.glob("manager-state.json.*")), [])

    def test_restore_and_notice(self):
        restored(self.path)
        current = load(self.path)
        self.assertEqual((current.role, current.serving, current.notice),
                         ("backup", False, "restored_as_backup"))
        with self.assertRaises(RoleError):
            require_primary(current)
        self.assertIn("Εφεδρικός", describe(current))
        acknowledge(self.path)
        self.assertIsNone(load(self.path).notice)
        self.assertEqual(load(self.path).role, "backup")

    def test_change_role_keeps_serving_and_clears_notice(self):
        restored(self.path)
        change_role(self.path, "primary")
        self.assertEqual(load(self.path).role, "primary")
        self.assertFalse(load(self.path).serving)
        self.assertIsNone(load(self.path).notice)
        change_serving(self.path, True)
        change_role(self.path, "backup")
        self.assertTrue(load(self.path).serving)

    def test_offline_status_and_confirmation(self):
        self.assertEqual(main(["status", "--registry-file", str(self.registry)]), 0)
        self.assertEqual(main(["backup", "--registry-file", str(self.registry),
                               "--yes"]), 0)
        self.assertEqual(load(self.path).role, "backup")
        self.assertEqual(main(["serve-off", "--registry-file", str(self.registry),
                               "--yes"]), 0)
        self.assertFalse(load(self.path).serving)

    def test_offline_lock_denies_write_but_allows_status(self):
        from director.backup import manager_lock
        with manager_lock(self.path.parent):
            self.assertEqual(main(["status", "--registry-file", str(self.registry)]), 0)
            self.assertEqual(main(["primary", "--registry-file", str(self.registry),
                                   "--yes"]), 4)

    def test_offline_lock_checked_before_confirmation_prompt(self):
        from unittest import mock
        from director.backup import manager_lock
        with manager_lock(self.path.parent), \
                mock.patch("builtins.input", side_effect=AssertionError("prompt")):
            self.assertEqual(main(["primary", "--registry-file", str(self.registry)]), 4)
        self.assertFalse(self.path.exists())

    def test_restore_journal_denies_write(self):
        (self.path.parent / "restore-journal.json").write_text("{}", encoding="utf-8")
        self.assertEqual(main(["backup", "--registry-file", str(self.registry),
                               "--yes"]), 2)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
