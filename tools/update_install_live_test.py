"""Φάση 5B2b: πραγματική δοκιμή εναλλαγής έκδοσης και rollback (docs/phase5.md §5.4, §7).

Πραγματικοί Manager, supervisor και Client ως διεργασίες, μόνο σε loopback και σε
προσωρινούς φακέλους. Τα πακέτα υπογράφονται με κλειδί δοκιμής (κρυφές επιλογές)·
το πραγματικό κλειδί του συντηρητή δεν χρησιμοποιείται.

* ``--installer`` (Windows, το .ps1): build 1.0.0 με embeddable Python, πραγματικός
  εγκαταστάτης Inno Setup (/NOSTART, δικό του όνομα στο HKCU\\...\\Run), και πακέτα με
  embeddable Python. Θέλει Inno Setup και internet, όπως το install_live_test.
* Χωρίς ``--installer`` (Linux, το .sh): ίδια διάταξη φακέλων χωρίς εγκαταστάτη· οι
  εκδόσεις έχουν μόνο ``app\\`` και τρέχουν με την Python της δοκιμής. Το HKCU\\Run
  ελέγχεται μόνο στα Windows (αλλού SKIP).

Εκδόσεις: 1.0.1 κανονική, 1.0.2 πέφτει αμέσως, 1.0.3 και 1.0.4 κολλάνε (δεν
συνδέονται), 1.0.5 κανονική.

    Windows:  powershell -ExecutionPolicy Bypass -File tools\\update_install_live_test.ps1
    Linux:    tools/update_install_live_test.sh
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from client.ctl import finish_install  # noqa: E402
from client.install_paths import Layout  # noqa: E402
from client.supervisor import send_command  # noqa: E402
from shared import package as pkg  # noqa: E402
from shared.client_registry import SignedClientRegistry  # noqa: E402
from shared.device_identity import create_device_identity  # noqa: E402
from shared.identity import load_identity  # noqa: E402
from tools import build_release  # noqa: E402
from tools.mtls_live_test import JOINED, Process, child_env  # noqa: E402

TOTAL = 13
HOST = "127.0.0.1"
PORT = 51955
DISCOVERY_PORT = 51956
WATCH_PORT = 51957
EXTRA_BEACON_PORT = 51967
ENROLL_PORT = 51959
UPDATE_PORT = 51960
ROOM = "C2627-Γ1"
RUN_NAME = "SchIntercomUpdateTest"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
TRIAL_SECONDS = 20
BASE = "1.0.0"
CRASH = "import sys\nprint('δοκιμαστική χαλασμένη έκδοση')\nsys.exit(7)\n"
HANG = "import time\nprint('δοκιμαστική έκδοση που δεν συνδέεται', flush=True)\n" \
       "while True:\n    time.sleep(1)\n"


def run_value() -> str | None:
    if os.name != "nt":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, RUN_NAME)[0]
    except FileNotFoundError:
        return None


def set_test_run_value(value: str) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, value)


def delete_run_value() -> None:
    if os.name != "nt":
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_NAME)
    except FileNotFoundError:
        pass


class Log:
    """Αρχείο log του supervisor/Client: αναζήτηση γραμμών μετά από ένα σημάδι."""

    def __init__(self, path: Path):
        self.path = path

    def lines(self) -> list[str]:
        try:
            return self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []

    def mark(self) -> int:
        return len(self.lines())

    def find(self, *texts: str, since: int = 0) -> str | None:
        for line in self.lines()[since:]:
            if all(text in line for text in texts):
                return line
        return None

    def wait(self, *texts: str, since: int = 0, timeout: float = 30) -> str | None:
        deadline = time.monotonic() + timeout
        while True:
            line = self.find(*texts, since=since)
            if line is not None or time.monotonic() >= deadline:
                return line
            time.sleep(0.25)


class LiveTest:
    def __init__(self, root: Path, *, installer: bool, dist: Path):
        self.root = root
        self.installer = installer
        self.dist = dist
        self.mdir = root / "manager"
        self.mdir.mkdir(parents=True)
        self.cert = self.mdir / "tls" / "director-tls.crt"
        self.key = self.mdir / "tls" / "director-tls.key"
        self.registry = self.mdir / "clients-registry.json"
        self.status_file = self.mdir / "updates" / "updates-status.json"
        self.home = root / "app"
        self.data = root / "data"
        self.layout = Layout(self.home, self.data)
        self.sup_log = Log(self.home / "logs" / "supervisor.log")
        self.client_log = Log(self.home / "logs" / "client.log")
        self.signing = pkg.generate_key()
        self.public = pkg.public_hex(self.signing)
        self.director: Process | None = None
        self.supervisor: subprocess.Popen | None = None
        self.packages: dict[str, Path] = {}
        self.client_id = ""
        self._built: Path | None = None
        self.results: list[tuple[str, str, str]] = []

    # -------------------------------------------------------------- βοηθητικά
    def step(self, title, function):
        try:
            answer = function()
            ok, detail = answer if isinstance(answer, tuple) else (bool(answer), "")
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        state = "PASS" if ok else "FAIL"
        self.results.append((title, state, detail))
        print(f"\n{state} {len(self.results):02d}/{TOTAL}: {title}"
              + (f" — {detail}" if detail else ""), flush=True)

    def wait(self, condition, timeout: float):
        deadline = time.monotonic() + timeout
        while True:
            value = condition()
            if value or time.monotonic() >= deadline:
                return value
            time.sleep(0.25)

    def state(self) -> dict:
        return self.layout.read_current() or {}

    def manager_status(self) -> dict:
        try:
            data = json.loads(self.status_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data.get("clients", {}).get(self.client_id, {})

    def updates(self, *args: str) -> tuple[int, str]:
        done = subprocess.run(
            [sys.executable, "-m", "director.updates", "--registry-file", str(self.registry),
             "--trusted-key", self.public, *args],
            cwd=ROOT, env=child_env(), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=180)
        output = (done.stdout + done.stderr).strip()
        for line in output.splitlines():
            print(f"    [updates {args[0]}] {line}", flush=True)
        return done.returncode, output

    def tree(self, version: str, main_code: str | None = None) -> Path:
        if self.installer:
            # Μία λήψη embeddable Python + wheels· οι άλλες εκδόσεις είναι αντίγραφα.
            if self._built is None:
                self._built = build_release.build_tree(
                    self.dist, BASE, python_version=build_release.platform.python_version())
            tree = self.root / "trees" / version
            if version != BASE:
                shutil.copytree(self._built, tree)
                (tree / "VERSION").write_text(version + "\n", encoding="ascii")
            else:
                tree = self._built
        else:
            tree = self.root / "trees" / version
            for relative in build_release.code_files():
                target = tree / "app" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            (tree / "VERSION").write_text(version + "\n", encoding="ascii")
        if main_code is not None:
            (tree / "app" / "client" / "main.py").write_text(main_code, encoding="utf-8")
        return tree

    def make_package(self, version: str, main_code: str | None = None) -> Path:
        out = self.root / "packages" / f"{version}.sicu"
        out.parent.mkdir(exist_ok=True)
        pkg.write_package(self.tree(version, main_code), out, self.signing, version)
        self.packages[version] = out
        return out

    def client_args(self) -> list[str]:
        return ["--host", HOST, "--port", str(PORT), "--enrollment-port", str(ENROLL_PORT),
                "--update-port", str(UPDATE_PORT), "--update-trusted-key", self.public,
                "--update-retry-seconds", "3"]

    def start_supervisor(self) -> None:
        version = self.state().get("current", BASE)
        python = self.layout.python(version)       # embeddable στο --installer
        env = child_env()
        if not (self.layout.version_dir(version) / "python").exists():
            env["PYTHONPATH"] = str(ROOT)
        self.supervisor = subprocess.Popen(
            [str(python), "-m", "client.supervisor", "--home", str(self.home),
             "--data-dir", str(self.data), "--trial-seconds", str(TRIAL_SECONDS),
             "--", *self.client_args()],
            cwd=self.home, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)

    def kill_supervisor(self) -> None:
        if self.supervisor is None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(self.supervisor.pid)],
                           capture_output=True)
        else:
            self.supervisor.send_signal(signal.SIGKILL)
        self.supervisor.wait(10)
        self.supervisor = None

    def stop_supervisor(self) -> None:
        if self.supervisor is None:
            return
        try:
            send_command(self.layout, "stop")
            self.supervisor.wait(20)
        except (OSError, subprocess.TimeoutExpired):
            self.kill_supervisor()
        self.supervisor = None

    def joined(self, since: int, timeout: float = 60) -> bool:
        return self.director.wait_for(self.client_id, JOINED, since=since,
                                      timeout=timeout) is not None

    def client_status(self) -> dict:
        try:
            return send_command(self.layout, "status")
        except (OSError, TimeoutError):
            return {}

    # ------------------------------------------------------------------ βήματα
    def s01(self):
        self.cert.parent.mkdir(parents=True, exist_ok=True)
        done = subprocess.run([sys.executable, "-m", "director.tls_identity",
                               "--cert", str(self.cert), "--key", str(self.key)],
                              cwd=ROOT, env=child_env(), capture_output=True, timeout=60)
        if done.returncode != 0:
            return False, "δεν δημιουργήθηκε ταυτότητα Manager"
        self.client_id, _ = load_identity(self.data / "client.json", room_override=ROOM)
        cert, _, _ = create_device_identity(self.data / "credentials", self.client_id)
        registry = SignedClientRegistry(self.registry, self.cert, self.key)
        registry.approve(self.client_id, ROOM, cert.read_bytes(), name="Board1")
        del registry
        if self.installer:
            iscc = build_release.find_iscc()
            if iscc is None:
                raise RuntimeError("Δεν βρέθηκε το Inno Setup 7 ή 6 (ISCC.exe)")
            tree = self.tree(BASE)
            subprocess.run(build_release.iscc_command(iscc, tree, BASE, self.dist), check=True,
                           capture_output=True)
            setup = self.dist / f"sch-intercom-client-{BASE}-setup.exe"
            result = subprocess.run(
                [str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                 f"/DIR={self.home}", f"/DATADIR={self.data}", f"/RUNNAME={RUN_NAME}",
                 f"/MANAGERCERT={self.cert}", f"/MANAGERHOST={HOST}", "/NOENROLL", "/NOSTART",
                 f"/LOG={self.root / 'setup.log'}"], capture_output=True, timeout=300)
            if result.returncode != 0:
                return False, f"εγκαταστάτης: κωδικός {result.returncode}"
        else:
            tree = self.tree(BASE)
            shutil.copytree(tree, self.layout.version_dir(BASE))
            finish_install(self.layout, BASE, manager_cert=self.cert, director_host=HOST,
                           run_name=RUN_NAME)
            if os.name == "nt":
                from client.supervisor import run_command
                set_test_run_value(run_command(self.layout, BASE))
        for version, code in (("1.0.1", None), ("1.0.2", CRASH), ("1.0.3", HANG),
                              ("1.0.4", HANG), ("1.0.5", None)):
            self.make_package(version, code)
        return self.state().get("current") == BASE, \
            f"εγκατεστημένη {self.state().get('current')}, 5 πακέτα δοκιμής"

    def s02(self):
        self.director = Process([
            "-m", "director.main", "--registry-file", str(self.registry),
            "--tls-cert", str(self.cert), "--tls-key", str(self.key), "--host", HOST,
            "--port", str(PORT), "--discovery-port", str(DISCOVERY_PORT),
            "--enrollment-port", str(ENROLL_PORT), "--update-port", str(UPDATE_PORT),
            "--update-trusted-key", self.public,
            "--director-id-file", str(self.mdir / "director.id"),
            "--watch-port", str(WATCH_PORT), "--extra-beacon-port", str(EXTRA_BEACON_PORT),
            "--start-check-seconds", "1", "--test-tone", "silent"], label="Manager")
        if self.director.wait_for("[Ενημερώσεις] Ενεργή TLS θύρα", timeout=30) is None:
            return False, "δεν άνοιξε η θύρα ενημερώσεων"
        self.director.send("none")
        mark = self.director.mark()
        self.start_supervisor()
        ok = self.joined(mark)
        return ok, f"Client {self.client_id[:8]}… v{self.client_status().get('version')}"

    def s03(self):
        """Ανακοίνωση σε εξέλιξη: η λήψη περιμένει, καμία εναλλαγή."""
        self.sup_mark = self.sup_log.mark()
        self.dir_mark = self.director.mark()
        client_mark = self.client_log.mark()
        self.director.send("all")
        time.sleep(1.5)
        code, _ = self.updates("import", str(self.packages["1.0.1"]))
        paused = self.client_log.wait("Παύση λήψης", since=client_mark, timeout=30)
        time.sleep(3)
        no_trial = self.sup_log.find("Δοκιμή έκδοσης", since=self.sup_mark) is None
        self.director.send("none")
        self.stopped_at = time.monotonic()
        return code == 0 and paused is not None and no_trial, \
            f"παύση λήψης {paused is not None}, χωρίς εναλλαγή όσο παίζει {no_trial}"

    def s04(self):
        """Εναλλαγή μόνο 10 s μετά το τέλος της ανακοίνωσης."""
        time.sleep(max(0.0, self.stopped_at + 8 - time.monotonic()))
        early = self.sup_log.find("Δοκιμή έκδοσης 1.0.1", since=self.sup_mark)
        line = self.sup_log.wait("Δοκιμή έκδοσης 1.0.1", since=self.sup_mark, timeout=40)
        waited = self.client_log.find("περιμένει το τέλος της ανακοίνωσης") is not None
        return early is None and line is not None and waited, \
            f"πριν τα 8 s: {'ναι' if early else 'όχι'}, μετά: {line is not None}"

    def s05(self):
        """Η νέα συνδέεται → commit, ίδια ταυτότητα."""
        installed = self.sup_log.wait("Η έκδοση 1.0.1 εγκαταστάθηκε", since=self.sup_mark,
                                      timeout=TRIAL_SECONDS + 30)
        state = self.state()
        rejoined = self.joined(self.dir_mark, timeout=5)
        same = load_identity(self.data / "client.json")[0] == self.client_id
        version = self.wait(lambda: self.client_status().get("version"), 10)
        return installed is not None and state.get("current") == "1.0.1" and \
            state.get("previous") == BASE and same and rejoined and version == "1.0.1", \
            f"current {state.get('current')}, previous {state.get('previous')}, " \
            f"ίδιο UUID {same}, Client v{version}"

    def s06(self):
        """Run και .cmd δείχνουν στη νέα έκδοση."""
        cmd = self.layout.cli_file.read_text(encoding="ascii")
        cmd_ok = "versions\\1.0.1\\python\\python.exe" in cmd
        if os.name != "nt":
            return cmd_ok, ".cmd σωστό · HKCU\\Run: SKIP (μόνο Windows)"
        value = run_value() or ""
        run_ok = f"\\versions\\1.0.1\\python\\pythonw.exe" in value and \
            f'--home "{self.home}"' in value
        return cmd_ok and run_ok, f"Run: {value}"

    def s07(self):
        """Ο Manager βλέπει «installed» και την έκδοση 1.0.1."""
        ok = self.wait(lambda: self.manager_status().get("state") in ("installed", "current")
                       and self.manager_status().get("app") == "1.0.1", 30)
        status = self.manager_status()
        installed_seen = any("installed 1.0.1" in line for line in self.director.lines)
        return bool(ok) and installed_seen, f"Manager: {status.get('state')} v{status.get('app')}"

    def trial_fails(self, version: str, reason: str, timeout: float) -> tuple[bool, str]:
        mark = self.sup_log.mark()
        joined_mark = self.director.mark()
        code, _ = self.updates("import", str(self.packages[version]))
        trial = self.sup_log.wait(f"Δοκιμή έκδοσης {version}", since=mark, timeout=60)
        rollback = self.sup_log.wait(f"Rollback από την {version}", since=mark,
                                     timeout=timeout)
        state = self.state()
        back = self.joined(joined_mark, timeout=60)
        failed = self.wait(lambda: self.manager_status().get("state") == "failed"
                           and self.manager_status().get("available") == version, 40)
        detail = self.manager_status().get("detail", "")
        ok = code == 0 and trial is not None and rollback is not None and \
            reason in (rollback or "") and state.get("current") == "1.0.1" and \
            version in state.get("failed", []) and back and bool(failed) and \
            not self.layout.version_dir(version).exists()
        return ok, f"rollback «{(rollback or '').split(': ', 1)[-1]}», Manager: failed " \
                   f"«{detail[:50]}», ξανά 1.0.1 {back}"

    def s08(self):
        return self.trial_fails("1.0.2", "τερματίστηκε", 40)

    def s09(self):
        return self.trial_fails("1.0.3", f"{TRIAL_SECONDS} s", TRIAL_SECONDS + 40)

    def s10(self):
        """Τερματισμός (κλείσιμο υπολογιστή) μέσα στη δοκιμή: rollback στην εκκίνηση."""
        mark = self.sup_log.mark()
        code, _ = self.updates("import", str(self.packages["1.0.4"]))
        trial = self.sup_log.wait("Δοκιμή έκδοσης 1.0.4", since=mark, timeout=60)
        time.sleep(2)
        child = self.client_status().get("client_pid")
        self.kill_supervisor()
        time.sleep(2)
        if os.name == "nt":
            from tools.install_live_test import pid_alive
            orphan = pid_alive(child)            # το Job object κλείνει και τον Client
        else:
            orphan = False                       # εκτός Windows δεν υπάρχει Job object:
            if child:                            # «κλείσιμο υπολογιστή» = και ο Client
                try:
                    os.kill(child, signal.SIGKILL)
                except OSError:
                    pass
        in_trial = self.state().get("trial") == "1.0.4"
        joined_mark = self.director.mark()
        self.start_supervisor()
        recovered = self.sup_log.wait("δεν ολοκληρώθηκε", since=mark, timeout=30)
        back = self.joined(joined_mark, timeout=60)
        state = self.state()
        return code == 0 and trial is not None and in_trial and recovered is not None and \
            back and not orphan and state.get("current") == "1.0.1" and \
            "1.0.4" in state.get("failed", []), \
            f"trial στον δίσκο {in_trial}, Client 1.0.4 έκλεισε {not orphan}, rollback στην " \
            f"εκκίνηση {recovered is not None}, ξανά 1.0.1 {back}"

    def s11(self):
        """Οι αποτυχημένες δεν ξαναδοκιμάζονται."""
        time.sleep(3)
        mark = self.client_log.mark()
        self.updates("start", "--all")
        time.sleep(6)
        again = self.client_log.find("Λήψη έκδοσης 1.0.", since=mark)
        failed = self.state().get("failed", [])
        return again is None and failed == ["1.0.2", "1.0.3", "1.0.4"], \
            f"failed {failed}, νέα λήψη: {again or 'καμία'}"

    def s12(self):
        """Νεότερη κανονική 1.0.5: εναλλαγή, παλιές εκδόσεις σβήνονται."""
        mark = self.sup_log.mark()
        code, _ = self.updates("import", str(self.packages["1.0.5"]))
        done = self.sup_log.wait("Η έκδοση 1.0.5 εγκαταστάθηκε", since=mark,
                                 timeout=TRIAL_SECONDS + 90)
        state = self.state()
        folders = sorted(p.name for p in self.layout.versions.iterdir() if p.is_dir())
        # Ο supervisor τρέχει ακόμη από την έκδοση με την οποία ξεκίνησε (μένει ως την
        # επόμενη σύνδεση στα Windows)· με --installer αυτή είναι η 1.0.1.
        allowed = {"1.0.5", "1.0.1", self.running_from()}
        return code == 0 and done is not None and state.get("current") == "1.0.5" and \
            state.get("previous") == "1.0.1" and set(folders) <= allowed, \
            f"εκδόσεις στον δίσκο: {', '.join(folders)}"

    def running_from(self) -> str:
        return BASE if not self.installer else (self.state().get("previous") or BASE)

    def s13(self):
        """--stop, και ο Client κλείνει μαζί του."""
        self.stop_supervisor()
        stopped = self.wait(lambda: not self.client_status(), 10)
        self.director.stop(quit_command="quit")
        self.director = None
        return bool(stopped), "supervisor και Client σταμάτησαν"

    def close(self):
        self.stop_supervisor()
        if self.director is not None:
            self.director.stop(quit_command="quit")
        if self.installer and (self.home / "unins000.exe").exists():
            subprocess.run([str(self.home / "unins000.exe"), "/VERYSILENT",
                            "/SUPPRESSMSGBOXES", "/NORESTART", "/PURGE"],
                           capture_output=True, timeout=300)
            self.wait(lambda: not self.home.exists(), 60)
        delete_run_value()

    def run(self) -> int:
        actions = [
            (f"Εγκατάσταση {BASE}" + (" (Inno Setup)" if self.installer else "")
             + ", εγκεκριμένη ταυτότητα, 5 πακέτα δοκιμής", self.s01),
            ("Manager και supervisor: ο Client 1.0.0 συνδέεται", self.s02),
            ("Νέα έκδοση κατά την ανακοίνωση: η λήψη περιμένει, καμία εναλλαγή", self.s03),
            ("Εναλλαγή μόνο 10 s μετά το τέλος της ανακοίνωσης", self.s04),
            ("Η 1.0.1 συνδέεται: commit, ίδια ταυτότητα", self.s05),
            ("HKCU\\Run και .cmd δείχνουν στην 1.0.1", self.s06),
            ("Ο Manager βλέπει «installed» 1.0.1", self.s07),
            ("Η 1.0.2 πέφτει: rollback στην 1.0.1, ο Manager βλέπει αποτυχία", self.s08),
            (f"Η 1.0.3 δεν συνδέεται σε {TRIAL_SECONDS} s: rollback", self.s09),
            ("Kill του supervisor μέσα στη δοκιμή της 1.0.4: rollback στην εκκίνηση",
             self.s10),
            ("Οι αποτυχημένες εκδόσεις δεν ξανακατεβαίνουν", self.s11),
            ("Η 1.0.5 εγκαθίσταται, οι παλιές εκδόσεις σβήνονται", self.s12),
            ("Τερματισμός: supervisor και Client σταματούν", self.s13),
        ]
        try:
            for index, (name, action) in enumerate(actions):
                self.step(name, action)
                if index < 2 and self.results[-1][1] != "PASS":
                    for later, _ in actions[index + 1:]:
                        self.results.append((later, "FAIL", "δεν έγινε το προηγούμενο βήμα"))
                    break
        finally:
            self.close()
        passed = sum(state == "PASS" for _, state, _ in self.results)
        print("\n" + "\n".join(
            f"{state} {i:02d}: {name}" + (f" — {detail}" if detail else "")
            for i, (name, state, detail) in enumerate(self.results, 1)), flush=True)
        print(f"ΣΥΝΟΛΟ: {passed}/{TOTAL} PASS", flush=True)
        return 0 if passed == TOTAL else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Πραγματική δοκιμή εναλλαγής έκδοσης Client")
    parser.add_argument("--installer", action="store_true",
                        help="Windows: πραγματικός εγκαταστάτης και embeddable Python")
    parser.add_argument("--dist", type=Path, default=ROOT / "dist" / "update-install-test")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)
    if args.installer and os.name != "nt":
        print("Το --installer τρέχει μόνο στα Windows.", flush=True)
        return 2
    if args.installer:
        from tools.install_live_test import real_install_present
        if real_install_present():
            print("Υπάρχει ήδη πραγματική εγκατάσταση του School Intercom Client σε αυτόν τον "
                  "χρήστη· η δοκιμή δεν τρέχει για να μην την πειράξει.", flush=True)
            return 2
    if run_value() is not None:
        delete_run_value()       # υπόλοιπο προηγούμενης δοκιμής (δικό της όνομα)
    root = Path(tempfile.mkdtemp(prefix="sch-update-install-"))
    print(f"Προσωρινή δοκιμή μόνο σε loopback: {root}", flush=True)
    try:
        return LiveTest(root, installer=args.installer, dist=args.dist).run()
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
