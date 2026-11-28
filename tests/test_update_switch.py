"""Φάση 5B2b: εναλλαγή έκδοσης από τον supervisor, δοκιμή 90 s, commit και rollback."""
from __future__ import annotations

import json
import os
from pathlib import Path
import unittest
from unittest import mock

from client.ctl import finish_install
from client.supervisor import (RESULT_FILE, TRIAL_ENV, VERSION_ENV, Supervisor,
                               run_command)
from client.updater import AnnouncementState, Updater
from tests.test_client_install import CERT, Clock, FakeChild, TempTest


def make_version(layout, version: str) -> Path:
    folder = layout.version_dir(version)
    (folder / "app").mkdir(parents=True)
    (folder / "VERSION").write_text(version + "\n", encoding="ascii")
    return folder


class SwitchTests(TempTest):
    def setUp(self):
        super().setUp()
        cert = self.root / "m.pem"
        cert.write_text(CERT)
        finish_install(self.layout, "1.0.0", manager_cert=cert, director_host=None,
                       run_name="TestRun")
        make_version(self.layout, "1.0.0")
        self.clock = Clock()
        self.spawned = []
        self.run_values = []
        self.display_versions = []

        def spawn(command, env, cwd):
            child = FakeChild()
            self.spawned.append((command, env, child))
            return child

        self.sup = Supervisor(self.layout, spawn=spawn, clock=self.clock, trial_seconds=90,
                              registry=lambda name, value: self.run_values.append((name, value)),
                              uninstall=self.display_versions.append)
        self.addCleanup(self.sup.close)
        self.sup.step()

    def state(self):
        return self.layout.read_current()

    def result(self):
        return json.loads((self.home / RESULT_FILE).read_text(encoding="utf-8"))

    def start_trial(self, version="1.0.1"):
        make_version(self.layout, version)
        self.assertEqual(self.sup.handle("switch", {"version": version}), {"ok": True})
        self.sup.step()
        self.assertEqual(self.state()["trial"], version)
        _, env, child = self.spawned[-1]
        self.assertEqual((env[TRIAL_ENV], env[VERSION_ENV]), (version, version))
        self.assertIsNotNone(self.spawned[-2][2].returncode)    # ο παλιός σταμάτησε
        return child

    def test_switch_refused_when_not_ready_old_or_failed(self):
        self.assertIn("δεν είναι έτοιμη", self.sup.handle("switch", {"version": "1.0.1"})["error"])
        self.assertIn("δεν είναι νεότερη", self.sup.handle("switch", {"version": "1.0.0"})["error"])
        self.assertFalse(self.sup.handle("switch", {"version": "x"})["ok"])
        self.assertFalse(self.sup.handle("trial-ok", {"version": "1.0.1"})["ok"])
        _, env, _ = self.spawned[0]
        self.assertNotIn(TRIAL_ENV, env)
        self.assertEqual(env[VERSION_ENV], "1.0.0")

    def test_trial_ok_commits_and_rewrites_launchers(self):
        make_version(self.layout, "0.9.0")       # παλιά έκδοση που πρέπει να σβηστεί
        child = self.start_trial()
        self.assertFalse(self.sup.handle("switch", {"version": "1.0.1"})["ok"])   # ήδη σε δοκιμή
        self.assertEqual(self.sup.handle("trial-ok", {"version": "1.0.1"}), {"ok": True})
        self.sup.step()
        state = self.state()
        self.assertEqual((state["current"], state["previous"], state["trial"]),
                         ("1.0.1", "1.0.0", None))
        self.assertIsNone(child.returncode)       # η νέα συνεχίζει, χωρίς επανεκκίνηση
        self.assertIn("versions\\1.0.1\\python\\python.exe", self.layout.cli_file.read_text())
        self.assertEqual(self.run_values, [("TestRun", run_command(self.layout, "1.0.1"))])
        self.assertIn(str(self.layout.version_dir("1.0.1") / "python" / "pythonw.exe"),
                      self.run_values[0][1])
        self.assertEqual(self.display_versions, ["1.0.1"])       # «Εφαρμογές» των Windows
        self.assertFalse(self.layout.version_dir("0.9.0").exists())
        self.assertTrue(self.layout.version_dir("1.0.0").exists())    # previous για rollback
        self.assertEqual(self.layout.install_info()["installed_version"], "1.0.1")
        self.assertEqual(self.result()["ok"], True)
        # Επόμενο crash: ξεκινά πάλι η 1.0.1.
        child.returncode = 1
        self.sup.step()
        self.clock.now = self.sup.next_start
        self.sup.step()
        self.assertEqual(self.spawned[-1][1][VERSION_ENV], "1.0.1")

    def test_crash_during_trial_rolls_back(self):
        child = self.start_trial()
        child.returncode = 1
        self.sup.step()
        state = self.state()
        self.assertEqual((state["current"], state["trial"], state["failed"]),
                         ("1.0.0", None, ["1.0.1"]))
        self.assertFalse(self.layout.version_dir("1.0.1").exists())
        self.assertEqual(self.spawned[-1][1][VERSION_ENV], "1.0.0")
        self.assertNotIn(TRIAL_ENV, self.spawned[-1][1])
        self.assertIn("τερματίστηκε", self.result()["reason"])
        self.assertEqual((self.run_values, self.display_versions), ([], []))
        self.assertIn("απέτυχε ήδη", self.sup.handle("switch", {"version": "1.0.1"})["error"])

    def test_no_connection_within_90_s_rolls_back(self):
        child = self.start_trial()
        self.clock.now += 89
        self.sup.step()
        self.assertEqual(self.state()["trial"], "1.0.1")
        self.clock.now += 1
        self.sup.step()
        self.assertIsNotNone(child.returncode)
        self.assertEqual(self.state()["failed"], ["1.0.1"])
        self.assertIn("90 s", self.result()["reason"])
        # Αργοπορημένο «ΟΚ» μετά το rollback δεν αλλάζει τίποτα.
        self.assertFalse(self.sup.handle("trial-ok", {"version": "1.0.1"})["ok"])

    def test_power_loss_during_trial_rolls_back_at_start(self):
        self.start_trial()
        fresh = Supervisor(self.layout, spawn=lambda *a: FakeChild(), clock=self.clock)
        self.addCleanup(fresh.close)
        fresh.recover_trial()
        state = self.state()
        self.assertEqual((state["current"], state["trial"], state["failed"]),
                         ("1.0.0", None, ["1.0.1"]))
        self.assertIn("διακόπηκε", self.result()["reason"])


class InstallerKeyTests(unittest.TestCase):
    def test_uninstall_key_matches_installer_app_id(self):
        from client.supervisor import APP_ID, UNINSTALL_KEY
        iss = (Path(__file__).resolve().parents[1] / "installer" /
               "sch-intercom-client.iss").read_text(encoding="utf-8-sig")
        self.assertIn("AppId={" + APP_ID + "\n", iss)
        self.assertTrue(UNINSTALL_KEY.endswith("\\" + APP_ID + "_is1"))


class UpdaterSwitchTests(TempTest):
    def setUp(self):
        super().setUp()
        self.layout.set_installed("1.0.0")
        self.clock = Clock()
        self.announcement = AnnouncementState(clock=self.clock)
        self.sent = []
        self.updater = Updater(self.layout, "id", None, announcement=self.announcement,
                               log=lambda line: None, pause_poll=0.001,
                               send_command=lambda layout, command, **f:
                               self.sent.append((command, f)) or {"ok": True})

    def test_switch_waits_ten_seconds_after_announcement(self):
        self.announcement.set(True)
        ticks = iter(range(100))

        def sleep(_):
            step = next(ticks)
            if step == 3:
                self.announcement.set(False)        # τέλος ανακοίνωσης
            self.clock.now += 1
        with mock.patch("client.updater.time.sleep", side_effect=sleep):
            self.assertTrue(self.updater.request_switch("1.0.1"))
        self.assertEqual(self.sent, [("switch", {"version": "1.0.1"})])
        self.assertGreaterEqual(self.announcement.quiet_for(), 10)
        self.assertEqual(next(ticks), 13)           # τέλος στο 4ο βήμα + 10 s ησυχία

    def test_no_supervisor_is_not_fatal(self):
        def refuse(*args, **kwargs):
            raise ConnectionRefusedError("όχι")
        self.updater.send_command = refuse
        self.assertFalse(self.updater.request_switch("1.0.1"))

    def test_running_version_from_supervisor_env(self):
        state = self.layout.read_current()
        with mock.patch.dict(os.environ, {VERSION_ENV: "1.0.1"}):
            self.assertEqual(self.updater.installed_version(state), "1.0.1")
        with mock.patch.dict(os.environ, {VERSION_ENV: "bad"}):
            self.assertEqual(self.updater.installed_version(state), "1.0.0")

    def test_child_env_uses_version_app_without_embedded_python(self):
        folder = make_version(self.layout, "1.0.1")
        env = self.layout.child_env("1.0.1")
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep)[0], str(folder / "app"))


if __name__ == "__main__":
    unittest.main()


class TrashTests(unittest.TestCase):
    def test_undeletable_version_goes_to_trash_and_is_purged_later(self):
        import tempfile
        from client import supervisor as module
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            folder = home / "versions" / "1.0.2"
            (folder / "python").mkdir(parents=True)
            (folder / "python" / "python313.dll").write_bytes(b"in use")
            with mock.patch("shutil.rmtree"):          # όπως .dll σε χρήση στα Windows
                self.assertTrue(module.remove_tree(folder, timeout=0.1))
            self.assertFalse(folder.exists())
            trash = home / module.TRASH_DIR
            self.assertEqual(len(list(trash.iterdir())), 1)
            module.purge_trash(home)
            self.assertFalse(trash.exists())
