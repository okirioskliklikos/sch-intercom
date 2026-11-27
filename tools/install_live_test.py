"""Αυτόματη πραγματική δοκιμή του εγκαταστάτη Client (Φάση 5A, docs/phase5.md §7).

Μόνο Windows, με Inno Setup 7 (ή 6) εγκατεστημένο. Φτιάχνει δύο setup.exe (η έκδοση του
repository και μία «νεότερη» +1 από τον ίδιο κώδικα) και τα εγκαθιστά αθόρυβα σε
προσωρινούς φακέλους, με δικό της όνομα στο HKCU\\...\\Run (SchIntercomInstallTest):
η πραγματική ταυτότητα (%USERPROFILE%\\.school-intercom) δεν αγγίζεται. Ο Client
δοκιμής δεν βρίσκει Manager (127.0.0.1 χωρίς Manager δοκιμής) και απλώς ξαναδοκιμάζει·
δεν χρειάζεται εγγραφή, μικρόφωνο ή ηχεία. Στο τέλος όλα σβήνονται.

    powershell -ExecutionPolicy Bypass -File tools\\install_live_test.ps1
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.version import APP_VERSION, parse_version  # noqa: E402
from tools import build_release  # noqa: E402

TOTAL = 15
RUN_NAME = "SchIntercomInstallTest"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
UNINSTALL_KEY = (r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
                 r"\{6B0E7C55-2F3D-4C41-9E0B-5C1D2A7E9F10}_is1")


def next_version(version: str) -> str:
    major, minor, patch = parse_version(version)
    return f"{major}.{minor}.{patch + 1}"


def test_certificate(path: Path) -> None:
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sch-install-test")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=30))
            .sign(key, hashes.SHA256()))
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259                                # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def kill(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)


def run_value() -> str | None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, RUN_NAME)[0]
    except FileNotFoundError:
        return None


def delete_run_value() -> None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_NAME)
    except FileNotFoundError:
        pass


def real_install_present() -> bool:
    import winreg
    try:
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY).Close()
        return True
    except FileNotFoundError:
        return False


def wait_for(predicate, timeout: float, step: float = 0.5):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(step)


class LiveTest:
    def __init__(self, base: Path, dist: Path):
        self.base = base
        self.dist = dist
        self.home = base / "app"
        self.data = base / "data"
        self.cert = base / "manager-ca.pem"
        self.results: list[tuple[str, bool, str]] = []
        self.setups: dict[str, Path] = {}
        self.uuid: str | None = None
        self.v1 = APP_VERSION
        self.v2 = next_version(APP_VERSION)

    # -------------------------------------------------------------- αναφορά
    def result(self, name: str, ok: bool, detail: str = "", *, skip: bool = False) -> None:
        self.results.append((name, ok or skip, detail))
        word = "SKIP" if skip else ("PASS" if ok else "FAIL")
        text = f"{word} {len(self.results):02d}/{TOTAL}: {name}"
        print(f"\n=== {text}" + (f" — {detail}" if detail else "") + "\n", flush=True)

    def step(self, name: str, action) -> None:
        try:
            outcome = action()
            ok, detail = outcome if isinstance(outcome, tuple) else (bool(outcome), "")
        except Exception as exc:  # η δοκιμή αναφέρει, δεν κρύβει
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        self.result(name, ok, detail)

    # -------------------------------------------------------------- βοηθητικά
    def cli(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run(["cmd", "/c", str(self.home / "sch-intercom-client.cmd"), *args],
                              capture_output=True, timeout=timeout, encoding="utf-8",
                              errors="replace")

    def status(self) -> dict | None:
        from client.install_paths import Layout
        from client.supervisor import send_command
        try:
            return send_command(Layout(self.home), "status")
        except (OSError, TimeoutError):
            return None

    def client_pid(self) -> int | None:
        return (self.status() or {}).get("client_pid")

    def install(self, version: str, *extra: str) -> subprocess.CompletedProcess:
        command = [str(self.setups[version]), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                   f"/DIR={self.home}", f"/DATADIR={self.data}", f"/RUNNAME={RUN_NAME}",
                   f"/MANAGERCERT={self.cert}", "/MANAGERHOST=127.0.0.1", "/NOENROLL",
                   f"/LOG={self.base / ('setup-' + version + '.log')}", *extra]
        return subprocess.run(command, capture_output=True, timeout=300)

    def uninstall(self, *extra: str) -> bool:
        uninstaller = self.home / "unins000.exe"
        if not uninstaller.exists():
            return False
        subprocess.run([str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", *extra],
                       capture_output=True, timeout=300)
        # Ο uninstaller τρέχει αντίγραφο του εαυτού του από το TEMP· περιμένουμε το αποτέλεσμα.
        return bool(wait_for(lambda: not self.home.exists() and run_value() is None, 90))

    def read_uuid(self) -> str | None:
        try:
            return json.loads((self.data / "client.json").read_text(encoding="utf-8"))["client_id"]
        except (OSError, ValueError, KeyError):
            return None

    def running_version(self) -> str | None:
        return (wait_for(self.status, 30) or {}).get("version")

    # -------------------------------------------------------------- βήματα
    def s01_build(self):
        iscc = build_release.find_iscc()
        if iscc is None:
            raise RuntimeError("Δεν βρέθηκε το Inno Setup 7 ή 6 (ISCC.exe)· εγκαταστήστε το από "
                               "https://jrsoftware.org/isdl.php")
        for version in (self.v1, self.v2):
            tree = build_release.build_tree(self.dist, version,
                                            python_version=build_release.platform.python_version())
            subprocess.run(build_release.iscc_command(iscc, tree, version, self.dist), check=True,
                           capture_output=True)
            self.setups[version] = self.dist / f"sch-intercom-client-{version}-setup.exe"
        sizes = ", ".join(f"{v}: {p.stat().st_size / 1e6:.0f} MB" for v, p in self.setups.items())
        return all(p.exists() for p in self.setups.values()), sizes

    def s02_install(self):
        result = self.install(self.v1)
        state = json.loads((self.home / "current.json").read_text(encoding="utf-8"))
        settings = json.loads((self.data / "client-settings.json").read_text(encoding="utf-8"))
        checks = {
            "κωδικός 0": result.returncode == 0,
            "current": state["current"] == self.v1,
            "pythonw": (self.home / "versions" / self.v1 / "python" / "pythonw.exe").exists(),
            ".cmd": (self.home / "sch-intercom-client.cmd").exists(),
            "πιστοποιητικό": (self.data / "manager-ca.pem").read_bytes() == self.cert.read_bytes(),
            "Manager": settings.get("director_host") == "127.0.0.1",
        }
        return all(checks.values()), ", ".join(k for k, ok in checks.items() if not ok) or "όλα"

    def s03_run_key(self):
        value = run_value() or ""
        expected = f'"{self.home}\\versions\\{self.v1}\\python\\pythonw.exe" -m client.supervisor'
        return value.startswith(expected) and f'--home "{self.home}"' in value, value

    def s04_running(self):
        info = wait_for(self.status, 30)
        pid = wait_for(self.client_pid, 15)
        self.uuid = wait_for(self.read_uuid, 30)
        ok = bool(info) and pid_alive(pid) and self.uuid is not None
        result = self.cli("--status")
        return ok and result.returncode == 0, f"{result.stdout.strip()} | UUID {self.uuid}"

    def s05_single_instance(self):
        python = self.home / "versions" / self.v1 / "python" / "python.exe"
        before = self.status() or {}
        result = subprocess.run([str(python), "-m", "client.supervisor", "--home", str(self.home)],
                                capture_output=True, timeout=30)
        after = self.status() or {}
        same = before.get("supervisor_pid") == after.get("supervisor_pid")
        return result.returncode == 3 and same, f"κωδικός {result.returncode}"

    def s06_restart(self):
        before = self.client_pid()
        result = self.cli("--restart")
        after = wait_for(lambda: (p := self.client_pid()) not in (None, before) and p, 20)
        return result.returncode == 0 and bool(after) and not pid_alive(before), \
            f"PID {before} → {after}"

    def s07_client_crash(self):
        before = self.client_pid()
        kill(before)
        after = wait_for(lambda: (p := self.client_pid()) not in (None, before) and p, 20)
        return bool(after), f"PID {before} τερματίστηκε → νέος {after}"

    def s08_supervisor_killed(self):
        info = self.status() or {}
        kill(info["supervisor_pid"])
        gone = wait_for(lambda: not pid_alive(info["client_pid"]), 10)
        return bool(gone), f"supervisor {info['supervisor_pid']} και Client {info['client_pid']}"

    def s09_start(self):
        result = self.cli("--start")
        wait_for(self.status, 20)
        pid = wait_for(self.client_pid, 15)
        return result.returncode == 0 and pid_alive(pid), result.stdout.strip()

    def s10_set_host(self):
        before = self.client_pid()
        result = self.cli("--set-director-host", "127.0.0.2")
        settings = json.loads((self.data / "client-settings.json").read_text(encoding="utf-8"))
        after = wait_for(lambda: (p := self.client_pid()) not in (None, before) and p, 20)
        return (result.returncode == 0 and settings["director_host"] == "127.0.0.2"
                and bool(after)), result.stdout.strip()

    def s11_stop(self):
        info = self.status() or {}
        result = self.cli("--stop")
        gone = wait_for(lambda: not pid_alive(info.get("client_pid"))
                        and not pid_alive(info.get("supervisor_pid")), 15)
        status = self.cli("--status")
        return result.returncode == 0 and bool(gone) and status.returncode == 3, \
            status.stdout.strip()

    def s12_reinstall(self):
        result = self.install(self.v1)
        version = self.running_version()
        return (result.returncode == 0 and self.read_uuid() == self.uuid
                and version == self.v1), f"UUID {self.read_uuid()}, έκδοση {version}"

    def s13_upgrade(self):
        result = self.install(self.v2)
        state = json.loads((self.home / "current.json").read_text(encoding="utf-8"))
        version = self.running_version()
        value = run_value() or ""
        ok = (result.returncode == 0 and state["current"] == self.v2
              and state["previous"] == self.v1 and version == self.v2
              and f"\\versions\\{self.v2}\\" in value and self.read_uuid() == self.uuid)
        return ok, f"current {state['current']}, previous {state['previous']}, εκτελείται {version}"

    def s14_uninstall(self):
        info = self.status() or {}
        removed = self.uninstall()
        alive = pid_alive(info.get("client_pid")) or pid_alive(info.get("supervisor_pid"))
        kept = self.read_uuid() == self.uuid
        return removed and not alive and kept, \
            f"φάκελος προγράμματος {'σβήστηκε' if removed else 'έμεινε'}, ταυτότητα " \
            f"{'έμεινε' if kept else 'χάθηκε'}"

    def s15_purge(self):
        result = self.install(self.v1, "/NOSTART")
        same = self.read_uuid() == self.uuid
        removed = self.uninstall("/PURGE")
        return result.returncode == 0 and same and removed and not self.data.exists(), \
            f"ίδια ταυτότητα στην επανεγκατάσταση: {same}, δεδομένα σβήστηκαν: {not self.data.exists()}"

    def cleanup(self) -> None:
        try:
            if (self.home / "sch-intercom-client.cmd").exists():
                self.cli("--stop", timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
        if (self.home / "unins000.exe").exists():
            try:
                self.uninstall("/PURGE")
            except (OSError, subprocess.SubprocessError):
                pass
        delete_run_value()

    def run(self) -> int:
        steps = [
            ("Build δύο εκδόσεων και setup.exe (Inno Setup)", self.s01_build),
            ("Αθόρυβη εγκατάσταση ανά χρήστη, χωρίς admin", self.s02_install),
            ("Αυτόματη εκκίνηση στο HKCU\\Run με pythonw", self.s03_run_key),
            ("Ο Client τρέχει μετά την εγκατάσταση, νέα ταυτότητα", self.s04_running),
            ("Ένα μόνο αντίγραφο supervisor", self.s05_single_instance),
            ("--restart: νέος Client", self.s06_restart),
            ("Ο Client τερματίζεται → ξαναξεκινά μόνος", self.s07_client_crash),
            ("Ο supervisor τερματίζεται → κλείνει και ο Client", self.s08_supervisor_killed),
            ("--start", self.s09_start),
            ("--set-director-host: αποθήκευση και επανεκκίνηση", self.s10_set_host),
            ("--stop", self.s11_stop),
            ("Επανεγκατάσταση ίδιας έκδοσης: ίδια ταυτότητα", self.s12_reinstall),
            (f"Εγκατάσταση νεότερης ({self.v2}) πάνω από την {self.v1}", self.s13_upgrade),
            ("Απεγκατάσταση: φεύγει το πρόγραμμα, μένει η ταυτότητα", self.s14_uninstall),
            ("Επανεγκατάσταση με την ίδια ταυτότητα, μετά «Πλήρης αφαίρεση»", self.s15_purge),
        ]
        try:
            for index, (name, action) in enumerate(steps):
                self.step(name, action)
                if index == 0 and not self.results[-1][1]:
                    for later, _ in steps[1:]:
                        self.result(later, False, "χωρίς setup.exe")
                    break
        finally:
            self.cleanup()
        passed = sum(1 for _, ok, _ in self.results if ok)
        print(f"\nΣΥΝΟΛΟ: {passed}/{TOTAL} PASS", flush=True)
        return 0 if passed == TOTAL else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Πραγματική δοκιμή εγκαταστάτη Client (Windows)")
    parser.add_argument("--dist", type=Path, default=ROOT / "dist" / "install-test")
    parser.add_argument("--keep", action="store_true", help="Κράτα τον προσωρινό φάκελο")
    args = parser.parse_args(argv)
    if os.name != "nt":
        print("Η δοκιμή του εγκαταστάτη τρέχει μόνο στα Windows.", flush=True)
        return 2
    if real_install_present():
        print("Υπάρχει ήδη πραγματική εγκατάσταση του School Intercom Client σε αυτόν τον "
              "χρήστη· η δοκιμή δεν τρέχει για να μην την πειράξει.", flush=True)
        return 2
    base = Path(tempfile.mkdtemp(prefix="sch-install-test-"))
    try:
        test_certificate(base / "manager-ca.pem")
        return LiveTest(base, args.dist).run()
    finally:
        if not args.keep:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
