"""Φάση 5A: διάταξη εγκατάστασης, γραμμή εντολών, supervisor και build (χωρίς Windows)."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

from client import ctl
from client import main as client_main
from client.install_paths import (DATA_ENV, Layout, load_settings, save_settings)
from client.single_instance import AlreadyRunning, InstanceLock, is_locked
from client.supervisor import EXIT_ALREADY_RUNNING, Supervisor, send_command
from shared.version import APP_VERSION, parse_version
from tools import build_release as br

ROOT = Path(__file__).resolve().parents[1]
CERT = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


class FakeChild:
    count = 0

    def __init__(self):
        FakeChild.count += 1
        self.pid = 1000 + FakeChild.count
        self.returncode = None
        self.stdout = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    kill = terminate

    def wait(self, timeout=None):
        return self.returncode


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class TempTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.home = self.root / "app"
        self.data = self.root / "data"
        self.layout = Layout(self.home, self.data)

    def tearDown(self):
        self._dir.cleanup()


class VersionAndLayoutTests(TempTest):
    def test_app_version_is_valid(self):
        self.assertEqual(parse_version(APP_VERSION), (1, 0, 0))
        for bad in ("1.0", "01.0.0", "1.0.0-beta", "", None, "1.0.0 "):
            with self.assertRaises(ValueError):
                parse_version(bad)

    def test_set_installed_keeps_previous(self):
        self.assertIsNone(self.layout.read_current())
        self.layout.set_installed("1.0.0")
        self.assertEqual(self.layout.read_current()["previous"], None)
        self.layout.set_installed("1.0.0")                # επανεγκατάσταση: ίδια
        self.assertEqual(self.layout.read_current()["previous"], None)
        state = self.layout.set_installed("1.0.1")
        self.assertEqual((state["current"], state["previous"]), ("1.0.1", "1.0.0"))

    def test_corrupt_current_is_error(self):
        self.home.mkdir()
        self.layout.current_file.write_text("{oops", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.layout.read_current()
        self.layout.current_file.write_text('{"current": "../x"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.layout.read_current()

    def test_data_dir_from_install_json(self):
        layout = Layout(self.home)
        self.home.mkdir()
        (self.home / "install.json").write_text(json.dumps({"data_dir": str(self.data)}),
                                                encoding="utf-8")
        with mock.patch.dict(os.environ, {DATA_ENV: ""}):
            self.assertEqual(layout.data, self.data)

    def test_python_falls_back_to_running_interpreter(self):
        self.assertEqual(self.layout.python("1.0.0"), Path(sys.executable))
        exe = self.home / "versions" / "1.0.0" / "python" / "pythonw.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")
        self.assertEqual(self.layout.python("1.0.0", windowed=True), exe)
        env = self.layout.child_env("1.0.0")
        self.assertEqual(env[DATA_ENV], str(self.data))
        self.assertEqual(env.get("PYTHONPATH"), os.environ.get("PYTHONPATH"))
        self.assertIn(str(ROOT), self.layout.child_env(None)["PYTHONPATH"].split(os.pathsep))

    def test_settings_round_trip_and_main_defaults(self):
        self.assertEqual(load_settings(self.data), {"director_host": None, "device": None})
        save_settings(self.data, director_host="10.0.0.5")
        save_settings(self.data, device=3)
        self.assertEqual(load_settings(self.data), {"director_host": "10.0.0.5", "device": 3})
        with mock.patch.dict(os.environ, {DATA_ENV: str(self.data)}):
            args = client_main.build_parser().parse_args([])
        self.assertEqual(args.identity_file, self.data / "client.json")
        self.assertEqual(args.tls_ca, self.data / "manager-ca.pem")
        client_main.apply_local_settings(args, load_settings(self.data))
        self.assertEqual((args.host, args.device), ("10.0.0.5", 3))
        with mock.patch.dict(os.environ, {DATA_ENV: str(self.data)}):
            args = client_main.build_parser().parse_args(["--host", "10.0.0.9", "--device", "1"])
        client_main.apply_local_settings(args, load_settings(self.data))
        self.assertEqual((args.host, args.device), ("10.0.0.9", 1))   # η γραμμή εντολών νικά


class CtlTests(TempTest):
    def finish(self, **extra):
        cert = self.root / "manager-ca.pem"
        cert.write_text(CERT, encoding="ascii")
        options = dict(manager_cert=cert, director_host="", run_name="TestRun")
        options.update(extra)
        ctl.finish_install(self.layout, "1.0.0", **options)

    def test_finish_install_writes_everything(self):
        self.finish(director_host="192.168.1.10")
        self.assertEqual(self.layout.current_version(), "1.0.0")
        self.assertEqual((self.data / "manager-ca.pem").read_text(encoding="ascii"), CERT)
        self.assertEqual(load_settings(self.data)["director_host"], "192.168.1.10")
        info = json.loads(self.layout.install_file.read_text(encoding="utf-8"))
        self.assertEqual((info["data_dir"], info["run_name"]), (str(self.data), "TestRun"))
        cli = self.layout.cli_file.read_bytes()
        cli.decode("ascii")
        self.assertIn(b'"%~dp0versions\\1.0.0\\python\\python.exe" -m client.ctl --home "%~dp0." %*',
                      cli)
        self.assertIn(b"\r\n", cli)
        self.assertTrue((self.home / "logs").is_dir())
        self.assertEqual(Layout(self.home).data, self.data)    # .cmd: χωρίς --data-dir

    def test_reinstall_without_cert_keeps_existing(self):
        self.finish()
        ctl.finish_install(self.layout, "1.0.0", manager_cert=None, director_host=None,
                           run_name="TestRun")
        self.assertTrue((self.data / "manager-ca.pem").exists())

    def test_missing_or_invalid_cert(self):
        with self.assertRaises(ValueError):
            ctl.finish_install(self.layout, "1.0.0", manager_cert=None, director_host=None,
                               run_name="x")
        bad = self.root / "bad.pem"
        bad.write_text("not a cert", encoding="ascii")
        with self.assertRaises(ValueError):
            ctl.finish_install(self.layout, "1.0.0", manager_cert=bad, director_host=None,
                               run_name="x")

    def test_cli_settings_and_purge(self):
        self.finish()
        base = ["--home", str(self.home), "--data-dir", str(self.data)]
        with mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(ctl.main(base + ["--set-director-host", "10.1.2.3"]), 0)
            self.assertEqual(ctl.main(base + ["--set-device", "2"]), 0)
            self.assertEqual(load_settings(self.data), {"director_host": "10.1.2.3", "device": 2})
            self.assertEqual(ctl.main(base + ["--set-director-host", "auto"]), 0)
            self.assertIsNone(load_settings(self.data)["director_host"])
            self.assertEqual(ctl.main(base + ["--status"]), ctl.EXIT_NOT_RUNNING)
            with self.assertRaises(SystemExit), mock.patch("sys.stderr", new=io.StringIO()):
                ctl.main(base + ["--purge"])                  # χωρίς --yes
            self.assertTrue(self.data.exists())
            self.assertEqual(ctl.main(base + ["--purge", "--yes"]), 0)
        self.assertFalse(self.data.exists())
        self.assertTrue(self.home.exists())

    def test_purge_refuses_home_folder(self):
        layout = Layout(self.home, Path.home())
        with self.assertRaises(ValueError):
            ctl.purge(layout)

    def test_bad_values_rejected(self):
        for value in ("-1", "x"):
            with self.assertRaises(SystemExit), mock.patch("sys.stderr", new=io.StringIO()):
                ctl.build_parser().parse_args(["--set-device", value])
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", new=io.StringIO()):
            ctl.build_parser().parse_args(["--set-director-host", "a b"])


class LockTests(TempTest):
    def test_second_lock_fails_and_release_frees(self):
        path = self.home / "supervisor.lock"
        first = InstanceLock(path).acquire()
        self.assertTrue(is_locked(path))
        with self.assertRaises(AlreadyRunning):
            InstanceLock(path).acquire()
        first.release()
        self.assertFalse(is_locked(path))

    def test_second_supervisor_exits_with_code_3(self):
        with InstanceLock(self.layout.lock_file):
            self.assertEqual(Supervisor(self.layout).run(), EXIT_ALREADY_RUNNING)


class SupervisorStepTests(TempTest):
    def make(self):
        self.clock = Clock()
        self.spawned = []

        def spawn(command, env, cwd):
            child = FakeChild()
            self.spawned.append((command, env, child))
            return child

        self.layout.set_installed("1.0.0")
        return Supervisor(self.layout, spawn=spawn, clock=self.clock)

    def test_starts_and_restarts_with_backoff(self):
        sup = self.make()
        sup.step()
        self.assertEqual(len(self.spawned), 1)
        command, env, child = self.spawned[0]
        self.assertEqual(command[1:], ["-m", "client.main"])
        self.assertEqual(env[DATA_ENV], str(self.data))
        self.assertEqual(sup.status()["version"], "1.0.0")
        delays = []
        for _ in range(5):
            self.spawned[-1][2].returncode = 1           # ο Client έπεσε αμέσως
            sup.step()
            delays.append(sup.next_start - self.clock.now)
            self.clock.now = sup.next_start
            sup.step()
        self.assertEqual(delays, [2, 5, 10, 30, 30])
        self.assertEqual(len(self.spawned), 6)
        self.clock.now += 61                             # σταθερή λειτουργία: από την αρχή
        self.spawned[-1][2].returncode = 1
        sup.step()
        self.assertEqual(sup.next_start - self.clock.now, 2)

    def test_restart_and_stop_commands(self):
        sup = self.make()
        sup.step()
        first = self.spawned[-1][2]
        self.assertEqual(sup.handle("restart"), {"ok": True})
        sup.step()
        self.assertIsNotNone(first.returncode)
        self.assertEqual(len(self.spawned), 2)
        self.assertEqual(sup.status()["client_pid"], self.spawned[-1][2].pid)
        self.assertFalse(sup.handle("nonsense")["ok"])
        sup.handle("stop")
        self.assertTrue(sup._stop.is_set())


class SupervisorControlTests(TempTest):
    """Πραγματικός supervisor με πραγματική διεργασία και κανάλι εντολών."""

    def test_status_restart_stop_over_pipe(self):
        self.layout.set_installed("1.0.0")
        children = []

        def spawn(command, env, cwd):
            child = subprocess.Popen([sys.executable, "-c",
                                      "import time; print('Client ok', flush=True); time.sleep(60)"],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            children.append(child)
            return child

        sup = Supervisor(self.layout, spawn=spawn)
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault("code", sup.run(poll=0.1)))
        thread.start()
        try:
            self.assertTrue(ctl.wait_until(lambda: ctl.status(self.layout) is not None, 10))
            self.assertTrue(ctl.running(self.layout))
            info = ctl.status(self.layout)
            self.assertEqual(info["client_pid"], children[0].pid)
            self.assertIn("έκδοση 1.0.0", ctl.format_status(info))
            self.assertTrue(ctl.restart(self.layout))
            self.assertEqual(len(children), 2)
            self.assertIsNotNone(children[0].poll())
            key = self.layout.key_file.read_bytes()
            with self.assertRaises(PermissionError):
                Path(self.layout.key_file).write_bytes(b"x" * 32)   # λάθος κλειδί
                send_command(self.layout, "status", timeout=2)
            self.layout.key_file.write_bytes(key)
            self.assertTrue(ctl.stop(self.layout))           # εντολή μέσω του καναλιού
            thread.join(5)
            self.assertFalse(thread.is_alive())              # χωρίς κόλλημα στο κλείσιμο
        finally:
            sup.handle("stop")
            thread.join(15)
        self.assertEqual(result.get("code"), 0)
        self.assertIsNotNone(children[-1].poll())
        self.assertFalse(ctl.running(self.layout))
        self.assertFalse(self.layout.key_file.exists())
        log = (self.layout.logs / "client.log").read_text(encoding="utf-8")
        self.assertIn("Client ok", log)


class BuildReleaseTests(unittest.TestCase):
    def test_code_files_are_client_only(self):
        files = {path.as_posix() for path in br.code_files()}
        for needed in ("client/main.py", "client/supervisor.py", "client/ctl.py",
                       "client/install_paths.py", "shared/version.py", "shared/identity.py"):
            self.assertIn(needed, files)
        self.assertFalse(any(name.startswith(("director/", "tools/", "tests/")) for name in files))

    def test_build_tree_layout(self):
        embed = io.BytesIO()
        with zipfile.ZipFile(embed, "w") as archive:
            archive.writestr("python.exe", b"MZ")
            archive.writestr("pythonw.exe", b"MZ")
            archive.writestr("python313._pth", "python313.zip\n.\n#import site\n")
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / "embed.zip").write_bytes(embed.getvalue())
            installed = []
            tree = br.build_tree(folder, "1.2.3", python_version="3.13.1",
                                 embed_zip=folder / "embed.zip",
                                 install=lambda target, v: installed.append(target),
                                 report=lambda line: None)
            self.assertEqual(tree, folder / "release" / "1.2.3")
            self.assertTrue((tree / "python" / "pythonw.exe").exists())
            self.assertIn("..\\app", (tree / "python" / "python313._pth").read_text())
            self.assertTrue((tree / "app" / "client" / "supervisor.py").exists())
            self.assertFalse((tree / "app" / "director").exists())
            self.assertEqual((tree / "VERSION").read_text().strip(), "1.2.3")
            self.assertEqual(installed, [tree / "python" / "Lib" / "site-packages"])
        with self.assertRaises(ValueError):
            br.build_tree(Path("/nonexistent"), "1.2", python_version="3.13.1")

    def test_requirements_include_windows_audio(self):
        for name in ("sounddevice>=0.5,<1", "cryptography>=42", "pycaw",
                     "winrt-Windows.Media.Control"):
            self.assertIn(name, br.REQUIREMENTS)

    def test_iss_script(self):
        data = br.ISS.read_bytes()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))      # Inno Setup: UTF-8 με BOM
        text = data.decode("utf-8-sig")
        for needed in ("PrivilegesRequired=lowest", r"DefaultDirName={localappdata}\sch-intercom",
                       r"Software\Microsoft\Windows\CurrentVersion\Run", "uninsdeletevalue",
                       "-m client.supervisor --home", "--install-finish {#AppVersion}",
                       "Add-MpPreference -ExclusionPath", "MB_DEFBUTTON2", "'--purge --yes'",
                       "{param:ROOM|}", "{param:PC|}", "{param:MANAGERHOST|}",
                       "{param:DATADIR|}", "{param:RUNNAME|SchoolIntercomClient}",
                       r"{src}\manager-ca.pem", "HasSwitch('NOENROLL')", "HasSwitch('NOSTART')",
                       "HasSwitch('DEFENDER')", "HasSwitch('PURGE')"):
            self.assertIn(needed, text)

    def test_iscc_command(self):
        command = br.iscc_command(Path("C:/Inno/ISCC.exe"), Path("dist/release/1.0.0"), "1.0.0",
                                  Path("dist"))
        self.assertIn("/DAppVersion=1.0.0", command)
        self.assertTrue(any(part.startswith("/DSourceDir=") for part in command))
        self.assertIn("/Fsch-intercom-client-1.0.0-setup", command)
        self.assertEqual(command[-1], str(br.ISS))


if __name__ == "__main__":
    unittest.main()
