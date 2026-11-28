"""Φάση 5: πραγματική δοκιμή του Client σε «καθαρά» Windows (docs/phase5.md §7.1).

Δοκιμή όπως στο σχολείο: πραγματικό ``setup.exe`` στην κανονική θέση
(``%LOCALAPPDATA%\\sch-intercom``, κανονικό όνομα στο HKCU\\Run), εγγραφή με κωδικό,
επανεγκατάσταση με ``/ROOM=`` και ``/PC=``, αυτόματη ενημέρωση μέσω LAN από πραγματικό
Manager, rollback, επανεκκίνηση των Windows, απεγκατάσταση. Τα πακέτα δοκιμής είναι
υπογεγραμμένα με το **πραγματικό** κλειδί έκδοσης και έχουν εκδόσεις 0.6.x, που δεν
περνούν ποτέ τις πραγματικές 1.x (απόφαση χρήστη 28/11/2026).

Ρόλοι:

* ``director`` (Linux): πραγματικός Manager σε δικό του προσωρινό φάκελο (τα δεδομένα του
  κανονικού Director δεν αγγίζονται), με τις κανονικές θύρες, και κανάλι δοκιμής TCP 50569
  για τον βοηθό των Windows: εγκρίσεις, εισαγωγή πακέτων, ανακοίνωση, αναμονή γραμμών.
* ``client`` (Windows, από τον φάκελο ``field-test`` που φτιάχνει το
  ``tools/field_test_build.py``): οδηγεί όλα τα βήματα και τυπώνει PASS/FAIL/SKIP.
  Στο βήμα της επανεκκίνησης ορίζει HKCU\\RunOnce και η δοκιμή συνεχίζει μόνη της μετά τη
  σύνδεση στα Windows.
* ``selfcheck`` (Linux, μόνο για έλεγχο του script): ίδια ροή σε loopback, με
  προσομοίωση του εγκαταστάτη και κλειδί δοκιμής· τα βήματα των Windows γίνονται SKIP.

ΜΟΝΟ ΓΙΑ ΔΟΚΙΜΕΣ: το κανάλι 50569 είναι χωρίς κρυπτογράφηση και δέχεται μόνο τις λίγες
εντολές της δοκιμής· τα πακέτα επαληθεύονται (υπογραφή) από τον Manager στην εισαγωγή.

    Linux (Director):  tools/field_test.sh director
    Windows (Client):  field-test.cmd <IP του Director>     (μέσα στον φάκελο field-test)
    Έλεγχος script:    tools/field_test.sh selfcheck
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from client.install_paths import DEFAULT_RUN_NAME, Layout, atomic_write_json  # noqa: E402
from shared import package as pkg  # noqa: E402
from shared.version import APP_VERSION, parse_version  # noqa: E402

CHANNEL_PORT = 50569
FIELD_PROTOCOL = 1
BASE = "0.6.0"
GOOD = "0.6.1"
CRASHING = "0.6.2"
HANGING = "0.6.3"
FINAL = "0.6.4"
UPDATES = (GOOD, CRASHING, HANGING, FINAL)
ROOM1, PC1 = "C2627-Δοκ1", "Πεδίο1"
ROOM2, PC2 = "C2627-Δοκ2", "Πεδίο2"
JOINED = ") συνδέθηκε"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUNONCE_KEY = r"Software\Microsoft\Windows\CurrentVersion\RunOnce"
RUNONCE_NAME = "SchIntercomFieldTest"
STATE_FILE = "field-test-state.json"
MAX_LINE = 1 << 20
PACKAGE_NAME = re.compile(r"^sch-intercom-client-\d{1,4}\.\d{1,4}\.\d{1,4}\.sicu$")
SETUP_TIMEOUT = 900          # το antivirus μπορεί να κρατήσει το setup.exe αρκετά λεπτά

CRASH_MAIN = "import sys\nprint('δοκιμαστική χαλασμένη έκδοση')\nsys.exit(7)\n"
HANG_MAIN = ("import time\nprint('δοκιμαστική έκδοση που δεν συνδέεται', flush=True)\n"
             "while True:\n    time.sleep(1)\n")
VARIANTS = {GOOD: None, CRASHING: CRASH_MAIN, HANGING: HANG_MAIN, FINAL: None}


def package_file(version: str) -> str:
    return f"sch-intercom-client-{version}.sicu"


def setup_file(version: str) -> str:
    return f"sch-intercom-client-{version}-setup.exe"


def make_variant(base_tree: Path, dest: Path, version: str, main_code: str | None) -> Path:
    """Αντίγραφο του δέντρου έκδοσης με άλλο VERSION (και χαλασμένο client/main.py)."""
    parse_version(version)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(base_tree, dest)
    (dest / "VERSION").write_text(version + "\n", encoding="ascii")
    if main_code is not None:
        (dest / "app" / "client" / "main.py").write_text(main_code, encoding="utf-8")
    return dest


def write_base_manifest(tree: Path, out: Path) -> None:
    """Λίστα αρχείων της έκδοσης του setup.exe (χωρίς υπογραφή· μόνο για τον έλεγχο αρχείων)."""
    manifest = pkg.build_manifest(tree, (tree / "VERSION").read_text(encoding="ascii").strip())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")


def check_files(folder: Path, manifest: dict) -> tuple[int, int, list[str]]:
    """(σωστά, σύνολο, προβλήματα): κάθε αρχείο του manifest υπάρχει με ίδιο SHA-256.

    Αρχείο που λείπει ή άλλαξε μετά την εγκατάσταση = πιθανή καραντίνα από antivirus.
    """
    good, problems = 0, []
    for item in manifest["files"]:
        path = folder / item["path"]
        if not path.is_file():
            problems.append(f"λείπει {item['path']}")
        elif path.stat().st_size != item["size"] or pkg._sha256_file(path) != item["sha256"]:
            problems.append(f"άλλαξε {item['path']}")
        else:
            good += 1
    return good, len(manifest["files"]), problems


# --------------------------------------------------------------------- κανάλι δοκιμής
def send_line(sock: socket.socket, data: dict) -> None:
    sock.sendall((json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8"))


def read_line(rfile) -> dict:
    line = rfile.readline(MAX_LINE)
    if not line.endswith(b"\n"):
        raise ConnectionError("Το κανάλι δοκιμής έκλεισε")
    data = json.loads(line.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Μη έγκυρο μήνυμα")
    return data


def sha256_file(path: Path) -> str:
    return pkg._sha256_file(Path(path))


def local_addresses() -> list[str]:
    found = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))          # καμία αποστολή· μόνο επιλογή διαδρομής
            found.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        done = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5)
        for address in done.stdout.split():
            if "." in address and address not in found and not address.startswith("127."):
                found.append(address)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        for address in socket.gethostbyname_ex(socket.gethostname())[2]:
            if address not in found and not address.startswith("127."):
                found.append(address)
    except OSError:
        pass
    return found


# ============================================================ Director (Linux)
class FieldDirector:
    """Πραγματικός Manager + κανάλι δοκιμής για τον βοηθό των Windows."""

    def __init__(self, work: Path, *, trusted_key: str | None = None,
                 bind: str = "0.0.0.0", port: int = CHANNEL_PORT, show: bool = True):
        self.work = Path(work)
        self.trusted_key = trusted_key
        self.bind = bind
        self.port = port
        self.show = show
        self.cert = self.work / "tls" / "director-tls.crt"
        self.key = self.work / "tls" / "director-tls.key"
        self.registry = self.work / "clients-registry.json"
        self.packages = self.work / "packages"
        self.manager = None
        self.finished = threading.Event()
        self.report = ""
        self._lock = threading.Lock()

    # -- Manager ------------------------------------------------------------------
    def start_manager(self) -> None:
        from tools.mtls_live_test import Process, child_env
        self.packages.mkdir(parents=True, exist_ok=True)
        self.cert.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, "-m", "director.tls_identity", "--cert", str(self.cert),
                        "--key", str(self.key)], cwd=ROOT, env=child_env(), check=True,
                       capture_output=True, timeout=60)
        args = ["-m", "director.main", "--registry-file", str(self.registry),
                "--tls-cert", str(self.cert), "--tls-key", str(self.key),
                "--director-id-file", str(self.work / "director.id"), "--test-tone", "silent"]
        if self.trusted_key:
            args += ["--update-trusted-key", self.trusted_key]
        self.manager = Process(args, label="Manager", show=self.show)
        if self.manager.wait_for("[Ενημερώσεις] Ενεργή TLS θύρα", timeout=60) is None:
            raise RuntimeError("Ο Manager δεν ξεκίνησε (μήπως τρέχει ήδη ο κανονικός Director;)")
        self.manager.send("none")

    def stop(self) -> None:
        if self.manager is not None:
            self.manager.stop(quit_command="quit")
            self.manager = None

    def updates_cli(self, *args: str) -> tuple[int, str]:
        from tools.mtls_live_test import child_env
        command = [sys.executable, "-m", "director.updates", "--registry-file", str(self.registry)]
        if self.trusted_key:
            command += ["--trusted-key", self.trusted_key]
        done = subprocess.run([*command, *args], cwd=ROOT, env=child_env(), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=300)
        output = (done.stdout + done.stderr).strip()
        for line in output.splitlines():
            print(f"    [updates {args[0]}] {line}", flush=True)
        return done.returncode, output

    def wait(self, *texts: str, since: int = 0, timeout: float = 20) -> str | None:
        return self.manager.wait_for(*texts, since=since, timeout=max(0.0, min(timeout, 1200)))

    def command(self, line: str, expect: str, timeout: float = 15) -> str | None:
        mark = self.manager.mark()
        self.manager.send(line)
        return self.wait(expect, since=mark, timeout=timeout)

    # -- εντολές του καναλιού -------------------------------------------------------
    def op_hello(self, req: dict) -> dict:
        return {"ok": True, "protocol": FIELD_PROTOCOL, "app": APP_VERSION,
                "cert": self.cert.read_text(encoding="utf-8")}

    def op_mark(self, req: dict) -> dict:
        return {"ok": True, "mark": self.manager.mark()}

    def op_wait(self, req: dict) -> dict:
        line = self.wait(*[str(t) for t in req["texts"]], since=int(req.get("since", 0)),
                         timeout=float(req.get("timeout", 20)))
        return {"ok": line is not None, "line": line}

    def op_approve_enrollment(self, req: dict) -> dict:
        room, name, code = str(req["room"]), str(req["name"]), str(req["code"])
        line = self.wait("Νέο αίτημα", f"| {room} | {name}", since=int(req.get("since", 0)),
                         timeout=float(req.get("timeout", 60)))
        if line is None:
            return {"ok": False, "error": f"δεν ήρθε αίτημα εγγραφής {room} · {name}"}
        short = line.split("Νέο αίτημα", 1)[1].split("|")[0].strip()
        done = self.command(f"approve {short} {code}", "Εγκρίθηκε:")
        return {"ok": done is not None, "line": done,
                "client_id": done.rsplit("|", 1)[-1].strip() if done else ""}

    def op_approve_change(self, req: dict) -> dict:
        text = f"{req['old_room']} · {req['old_name']} → {req['room']} · {req['name']}"
        line = self.wait("[Αλλαγές] Αίτημα", text, since=int(req.get("since", 0)),
                         timeout=float(req.get("timeout", 60)))
        if line is None:
            return {"ok": False, "error": f"δεν ήρθε αίτημα αλλαγής {text}"}
        done = self.command(f"approve-change {str(req['client_id'])[:8]}", "Εγκρίθηκε η αλλαγή")
        return {"ok": done is not None, "line": done}

    def op_change_seen(self, req: dict) -> dict:
        line = self.wait("[Αλλαγές] Αίτημα", since=int(req.get("since", 0)),
                         timeout=float(req.get("timeout", 20)))
        return {"ok": True, "seen": line is not None, "line": line}

    def op_joined(self, req: dict) -> dict:
        texts = [str(req["client_id"]), JOINED]
        if req.get("room"):
            texts.append(f"({req['room']}){JOINED[1:]}")
        line = self.wait(*texts, since=int(req.get("since", 0)),
                         timeout=float(req.get("timeout", 60)))
        return {"ok": line is not None, "line": line}

    def op_announce(self, req: dict) -> dict:
        self.manager.send("all" if req.get("on") else "none")
        return {"ok": True}

    def op_has_package(self, req: dict) -> dict:
        path = self.packages / str(req["name"])
        ok = PACKAGE_NAME.fullmatch(str(req["name"])) is not None and path.is_file() and \
            sha256_file(path) == req.get("sha256")
        return {"ok": True, "present": ok}

    def op_import(self, req: dict) -> dict:
        name = str(req["name"])
        if PACKAGE_NAME.fullmatch(name) is None or not (self.packages / name).is_file():
            return {"ok": False, "error": f"δεν υπάρχει το πακέτο {name}"}
        code, output = self.updates_cli("import", str(self.packages / name))
        return {"ok": code == 0, "code": code, "output": output[-2000:]}

    def op_start_all(self, req: dict) -> dict:
        code, output = self.updates_cli("start", "--all")
        return {"ok": code == 0, "output": output[-2000:]}

    def op_update_status(self, req: dict) -> dict:
        try:
            data = json.loads((self.work / "updates" / "updates-status.json")
                              .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return {"ok": True, "status": data.get("clients", {}).get(str(req["client_id"]), {})}

    def op_finish(self, req: dict) -> dict:
        self.report = str(req.get("report", ""))
        self.finished.set()
        return {"ok": True}

    def receive_upload(self, req: dict, rfile) -> dict:
        name, size, digest = str(req["name"]), int(req["size"]), str(req["sha256"])
        if PACKAGE_NAME.fullmatch(name) is None or not 0 < size <= pkg.MAX_PACKAGE_BYTES:
            raise ValueError(f"Μη αποδεκτό αρχείο {name} ({size} bytes)")
        partial = self.packages / (name + ".part")
        hasher = hashlib.sha256()
        left = size
        with open(partial, "wb") as handle:
            while left:
                chunk = rfile.read(min(left, 1 << 20))
                if not chunk:
                    raise ConnectionError("Η μεταφορά διακόπηκε")
                handle.write(chunk)
                hasher.update(chunk)
                left -= len(chunk)
        if hasher.hexdigest() != digest:
            partial.unlink(missing_ok=True)
            return {"ok": False, "error": "λάθος SHA-256"}
        os.replace(partial, self.packages / name)
        print(f"[field-test] Παραλήφθηκε {name} ({size / 1_000_000:.1f} MB)", flush=True)
        return {"ok": True}

    def handle(self, connection: socket.socket, address) -> None:
        print(f"[field-test] Σύνδεση βοηθού από {address[0]}", flush=True)
        rfile = connection.makefile("rb")
        try:
            while not self.finished.is_set():
                try:
                    req = read_line(rfile)
                except (ConnectionError, OSError, ValueError):
                    break
                op = str(req.get("op", ""))
                with self._lock:
                    try:
                        if op == "upload":
                            reply = self.receive_upload(req, rfile)
                        else:
                            function = getattr(self, f"op_{op}", None)
                            reply = function(req) if function else \
                                {"ok": False, "error": f"άγνωστη εντολή {op}"}
                    except (KeyError, TypeError, ValueError, OSError) as exc:
                        reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                try:
                    send_line(connection, reply)
                except OSError:
                    break
        finally:
            rfile.close()
            connection.close()
            print(f"[field-test] Ο βοηθός από {address[0]} αποσυνδέθηκε", flush=True)

    def serve(self, ready: threading.Event | None = None) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.bind, self.port))
        server.listen(4)
        server.settimeout(0.5)
        if ready is not None:
            ready.set()
        try:
            while not self.finished.is_set():
                try:
                    connection, address = server.accept()
                except socket.timeout:
                    continue
                connection.settimeout(None)
                threading.Thread(target=self.handle, args=(connection, address),
                                 daemon=True).start()
        finally:
            server.close()


def run_director(args) -> int:
    keep = args.work is not None
    work = Path(args.work) if keep else Path(tempfile.mkdtemp(prefix="sch-field-test-"))
    service = FieldDirector(work, trusted_key=args.trusted_key, bind=args.bind,
                            port=args.port, show=not args.quiet)
    try:
        service.start_manager()
        addresses = ", ".join(local_addresses()) or "(δεν βρέθηκε)"
        print("\n" + "=" * 70, flush=True)
        print(f"Director δοκιμής έτοιμος. Φάκελος δεδομένων δοκιμής: {work}", flush=True)
        print(f"IP αυτού του υπολογιστή: {addresses}", flush=True)
        print("Στα Windows, μέσα στον φάκελο field-test:  field-test.cmd <IP>", flush=True)
        print("Αφήστε αυτό το παράθυρο ανοιχτό ως το τέλος (και στην επανεκκίνηση των Windows).",
              flush=True)
        print("=" * 70 + "\n", flush=True)
        try:
            service.serve()
        except KeyboardInterrupt:
            print("\n[field-test] Διακοπή από τον χρήστη.", flush=True)
        if service.report:
            print("\n" + service.report, flush=True)
    finally:
        service.stop()
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
    return 0 if not service.report or ", 0 FAIL" in service.report else 1


# ============================================================ βοηθός Client
class Channel:
    def __init__(self, host: str, port: int = CHANNEL_PORT):
        self.host, self.port = host, port
        self.sock: socket.socket | None = None
        self.rfile = None

    def connect(self, timeout: float = 15) -> None:
        self.close()
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.settimeout(1500)
        self.rfile = self.sock.makefile("rb")

    def connect_retry(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while True:
            try:
                self.connect()
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(3)

    def close(self) -> None:
        for item in (self.rfile, self.sock):
            try:
                if item is not None:
                    item.close()
            except OSError:
                pass
        self.sock = self.rfile = None

    def call(self, op: str, **fields) -> dict:
        for attempt in range(2):
            try:
                if self.sock is None:
                    self.connect()
                send_line(self.sock, {"op": op, **fields})
                return read_line(self.rfile)
            except (OSError, ConnectionError, ValueError):
                self.close()
                if attempt:
                    raise
        raise ConnectionError("unreachable")

    def upload(self, path: Path, report=print) -> dict:
        path = Path(path)
        size = path.stat().st_size
        digest = sha256_file(path)
        if self.call("has_package", name=path.name, sha256=digest).get("present"):
            return {"ok": True, "skipped": True}
        if self.sock is None:
            self.connect()
        send_line(self.sock, {"op": "upload", "name": path.name, "size": size, "sha256": digest})
        sent, shown = 0, -1
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                self.sock.sendall(chunk)
                sent += len(chunk)
                percent = sent * 100 // size
                if percent // 25 != shown:
                    shown = percent // 25
                    report(f"    αποστολή {path.name}: {percent}%")
        return read_line(self.rfile)


def pid_alive(pid) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        code = ctypes.c_ulong()
        try:
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        finally:
            kernel32.CloseHandle(handle)
        return code.value == 259
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


class WindowsMachine:
    """Τα Windows όπως είναι: κανονικοί φάκελοι, κανονικό όνομα Run, πραγματικός εγκαταστάτης."""

    sim = False
    trial_seconds = 90

    def __init__(self, bundle: Path):
        self.bundle = Path(bundle)
        self.layout = Layout()
        self.runs = 0

    # -- εγκατάσταση -----------------------------------------------------------------
    def install(self, params: list[str]) -> tuple[int | None, float]:
        self.runs += 1
        logs = self.bundle / "logs"
        logs.mkdir(exist_ok=True)
        setup = self.bundle / "setup" / setup_file(BASE)
        started = time.monotonic()
        try:
            done = subprocess.run([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                                   f"/LOG={logs / f'setup-{self.runs}.log'}", *params],
                                  capture_output=True, timeout=SETUP_TIMEOUT)
            code = done.returncode
        except subprocess.TimeoutExpired:
            code = None
        return code, time.monotonic() - started

    def uninstall(self, *, purge: bool) -> bool:
        program = self.layout.home / "unins000.exe"
        if not program.exists():
            return False
        subprocess.run([str(program), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                        *(["/PURGE"] if purge else [])], capture_output=True, timeout=600)
        # Ο uninstaller του Inno Setup τρέχει από αντίγραφο στο TEMP: αναμονή να τελειώσει.
        return wait(lambda: not self.layout.home.exists(), 180)

    def cli(self, *args: str, timeout: float = 90) -> int:
        done = subprocess.run(["cmd", "/c", str(self.layout.cli_file), *args],
                              capture_output=True, timeout=timeout)
        return done.returncode

    def reboot_now(self) -> None:
        subprocess.run(["shutdown", "/r", "/t", "20", "/c",
                        "School Intercom: δοκιμή επανεκκίνησης (ακύρωση: shutdown /a)"],
                       capture_output=True)

    def after_logon(self) -> None:
        """Μετά την πραγματική επανεκκίνηση ο supervisor ξεκινά από το HKCU\\Run."""

    # -- μητρώο Windows ------------------------------------------------------------
    @staticmethod
    def _value(root, key: str, name: str):
        import winreg
        try:
            with winreg.OpenKey(root, key) as handle:
                return winreg.QueryValueEx(handle, name)[0]
        except FileNotFoundError:
            return None

    def run_value(self):
        import winreg
        return self._value(winreg.HKEY_CURRENT_USER, RUN_KEY, DEFAULT_RUN_NAME)

    def display_version(self):
        import winreg
        from client.supervisor import UNINSTALL_KEY
        return self._value(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, "DisplayVersion")

    def uninstall_entry(self) -> bool:
        import winreg
        from client.supervisor import UNINSTALL_KEY
        try:
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY).Close()
            return True
        except FileNotFoundError:
            return False

    def runonce(self):
        import winreg
        return self._value(winreg.HKEY_CURRENT_USER, RUNONCE_KEY, RUNONCE_NAME)

    def set_runonce(self, command: str | None) -> None:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUNONCE_KEY, 0,
                            winreg.KEY_SET_VALUE) as handle:
            if command is None:
                try:
                    winreg.DeleteValue(handle, RUNONCE_NAME)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(handle, RUNONCE_NAME, 0, winreg.REG_SZ, command)

    # -- πληροφορίες -----------------------------------------------------------------
    def windows_info(self) -> str:
        import winreg
        key = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        build = str(self._value(winreg.HKEY_LOCAL_MACHINE, key, "CurrentBuild") or "?")
        edition = str(self._value(winreg.HKEY_LOCAL_MACHINE, key, "EditionID") or "")
        shown = str(self._value(winreg.HKEY_LOCAL_MACHINE, key, "DisplayVersion") or "")
        name = "Windows 11" if build.isdigit() and int(build) >= 22000 else "Windows 10"
        return f"{name} {edition} {shown} (build {build})".replace("  ", " ")

    @staticmethod
    def powershell(command: str, timeout: float = 90) -> str:
        try:
            done = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return done.stdout.strip()

    def antivirus(self) -> list[str]:
        text = self.powershell(
            "Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct | "
            "ForEach-Object { $_.displayName + '|' + $_.productState }")
        found = []
        for line in text.splitlines():
            name, _, state = line.strip().rpartition("|")
            if not name:
                continue
            try:
                active = (int(state) >> 12) & 0xF == 1
            except ValueError:
                active = None
            found.append(f"{name} ({'ενεργό' if active else 'ανενεργό' if active is False else '?'})")
        return found

    def firewall_rules(self) -> str:
        text = self.powershell(
            "Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue | "
            "Where-Object { $_.Program -like '*\\sch-intercom\\*' } | ForEach-Object { "
            "$r = $_ | Get-NetFirewallRule; $r.Direction.ToString() + ' ' + $r.Action.ToString() "
            "+ ' ' + $r.Profile.ToString() }", timeout=120)
        rules = sorted({line.strip() for line in text.splitlines() if line.strip()})
        return ", ".join(rules) if rules else "κανένας"

    @staticmethod
    def process_path(pid) -> str:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid or 0))
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value
            return ""
        finally:
            kernel32.CloseHandle(handle)

    @staticmethod
    def uptime() -> float:
        import ctypes
        function = ctypes.windll.kernel32.GetTickCount64
        function.restype = ctypes.c_ulonglong
        return function() / 1000.0


class SimMachine:
    """Μόνο για τον έλεγχο του script στο Linux: προσομοίωση του εγκαταστάτη (ίδια βήματα με
    το installer/sch-intercom-client.iss) σε προσωρινούς φακέλους, κλειδί δοκιμής."""

    sim = True
    trial_seconds = 20

    def __init__(self, bundle: Path, root: Path):
        self.bundle = Path(bundle)
        self.layout = Layout(Path(root) / "home", Path(root) / "data")
        self.public = (self.bundle / "test-key.txt").read_text(encoding="ascii").strip()
        self.supervisor: subprocess.Popen | None = None

    def _stop(self) -> None:
        from client import ctl
        ctl.stop(self.layout)
        if self.supervisor is not None:
            try:
                self.supervisor.wait(20)
            except subprocess.TimeoutExpired:
                self._kill()
        self.supervisor = None

    def _kill(self) -> None:
        if self.supervisor is not None:
            try:
                os.killpg(self.supervisor.pid, signal.SIGKILL)
            except OSError:
                pass
            self.supervisor.wait(10)
            self.supervisor = None

    def start_supervisor(self) -> None:
        version = self.layout.current_version()
        command = self.layout.module_command(
            "client.supervisor", "--home", str(self.layout.home), "--data-dir",
            str(self.layout.data), "--trial-seconds", str(self.trial_seconds), "--",
            "--update-trusted-key", self.public, "--update-retry-seconds", "3", version=version)
        env = self.layout.child_env(version)
        env["SCH_INTERCOM_NO_TRAY"] = "1"
        self.supervisor = subprocess.Popen(command, cwd=self.layout.home, env=env,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, start_new_session=True)

    def install(self, params: list[str]) -> tuple[int | None, float]:
        from client import ctl
        started = time.monotonic()
        options = {}
        for item in params:
            key, _, value = item.lstrip("/").partition("=")
            options[key.upper()] = value
        self._stop()
        shutil.copytree(self.bundle / "trees" / BASE, self.layout.version_dir(BASE),
                        dirs_exist_ok=True)
        new = not (self.layout.data / "client.json").exists()
        host = options.get("MANAGERHOST")
        cert = self.bundle / "setup" / "manager-ca.pem"
        ctl.finish_install(self.layout, BASE, manager_cert=cert if cert.exists() else None,
                           director_host=(host or "") if (new or host) else None,
                           run_name=DEFAULT_RUN_NAME)
        room, name = options.get("ROOM", ""), options.get("PC", "")
        if new and "NOENROLL" not in options:
            ctl.enroll(self.layout, room, name, pause=False)
        elif not new and room and name:
            ctl.request_change(self.layout, room, name)
        if "NOSTART" not in options:
            self.start_supervisor()
        return 0, time.monotonic() - started

    def uninstall(self, *, purge: bool) -> bool:
        from client import ctl
        if not self.layout.home.exists():
            return False
        self._stop()
        if purge:
            ctl.purge(self.layout)
        shutil.rmtree(self.layout.home)
        return True

    def cli(self, *args: str, timeout: float = 90) -> int:
        from client import ctl
        return ctl.main(["--home", str(self.layout.home), "--data-dir", str(self.layout.data),
                         *args])

    def reboot_now(self) -> None:
        """Κλείσιμο υπολογιστή: τερματίζονται supervisor και Client χωρίς εντολή stop."""
        self._kill()

    def after_logon(self) -> None:
        self.start_supervisor()

    def run_value(self):
        return None

    def display_version(self):
        return None

    def uninstall_entry(self) -> bool:
        return False

    def runonce(self):
        return None

    def set_runonce(self, command: str | None) -> None:
        pass

    def windows_info(self) -> str:
        return f"προσομοίωση σε {platform.system()} {platform.release()}"

    def antivirus(self) -> list[str]:
        return []

    def firewall_rules(self) -> str:
        return "—"

    @staticmethod
    def process_path(pid) -> str:
        return ""

    @staticmethod
    def uptime() -> float:
        return time.monotonic()


def wait(condition, timeout: float, step: float = 0.5):
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(step)


class Log:
    """Log του supervisor/Client: αναζήτηση γραμμών μετά από ένα σημάδι."""

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
        return wait(lambda: self.find(*texts, since=since), timeout, 0.5)


class Tee:
    def __init__(self, stream, path: Path):
        self.stream = stream
        self.file = open(path, "a", encoding="utf-8")

    def write(self, text):
        self.stream.write(text)
        self.file.write(text)
        return len(text)

    def flush(self):
        self.stream.flush()
        self.file.flush()


STEPS = [
    ("Έλεγχος υπολογιστή, σύνδεση με τον Director δοκιμής, αποστολή πακέτων", "s01"),
    (f"Αθόρυβη εγκατάσταση {BASE} με /ROOM= /PC= /MANAGERHOST= (κανονική θέση, χωρίς admin)",
     "s02"),
    ("Εγγραφή με κωδικό και έγκριση: ο Client συνδέεται", "s03"),
    ("Εντοπισμός του Director χωρίς IP (UDP), μετά πάλι με IP", "s04"),
    ("Επανεγκατάσταση με νέα /ROOM= /PC=: αίτημα αλλαγής χωρίς κωδικό, ίδιο UUID", "s05"),
    ("Επανεγκατάσταση με τα ίδια ονόματα: κανένα αίτημα, ίδιο UUID", "s06"),
    (f"Νέο πακέτο {GOOD} κατά την ανακοίνωση: καμία εναλλαγή όσο παίζει", "s07"),
    (f"Εναλλαγή ≥ 10 s μετά την ανακοίνωση, commit {GOOD}, μόνο τα αρχεία που άλλαξαν", "s08"),
    (f"Run, .cmd, «Εφαρμογές» και αρχεία της {GOOD} (έλεγχος καραντίνας)", "s09"),
    (f"Επανεκκίνηση Windows: ο Client ξεκινά μόνος από το Run με την {GOOD}", "s10"),
    (f"Η {CRASHING} πέφτει: rollback στην {GOOD}, ο Manager βλέπει failed", "s11"),
    (f"Η {HANGING} δεν συνδέεται σε 90 s: rollback στην {GOOD}", "s12"),
    ("Οι αποτυχημένες εκδόσεις δεν ξανακατεβαίνουν", "s13"),
    (f"Η {FINAL} εγκαθίσταται: μένουν μόνο {FINAL} και {GOOD}, σωστά αρχεία", "s14"),
    ("--restart από το sch-intercom-client.cmd", "s15"),
    ("Απεγκατάσταση: φεύγει το πρόγραμμα, μένει η ταυτότητα", "s16"),
    (f"Επανεγκατάσταση του παλιού setup {BASE}: ίδια ταυτότητα, ενημερώνεται μόνο του σε {FINAL}",
     "s17"),
    ("«Πλήρης αφαίρεση»: δεν μένει τίποτα (επαναφορά του .school-intercom αν υπήρχε)", "s18"),
]
TOTAL = len(STEPS)
REBOOT_AFTER = 9           # μετά το s09 γίνεται η επανεκκίνηση
ABORT_IF_FAILED = ("s01", "s02", "s03")


class FieldTest:
    def __init__(self, machine, channel: Channel, *, state_file: Path, host: str,
                 trusted_keys=None, auto_reboot: bool = True):
        self.m = machine
        self.ch = channel
        self.state_file = state_file
        self.host = host
        self.trusted_keys = trusted_keys
        self.auto_reboot = auto_reboot
        self.layout = machine.layout
        self.sup_log = Log(self.layout.logs / "supervisor.log")
        self.client_log = Log(self.layout.logs / "client.log")
        self.state: dict = {"phase": "start", "results": [], "timings": [], "host": host}
        self.manifests: dict[str, dict] = {}

    # -- κατάσταση (επιβιώνει της επανεκκίνησης) -------------------------------------
    def save(self) -> None:
        atomic_write_json(self.state_file, self.state)

    def load(self) -> bool:
        try:
            self.state = json.loads(self.state_file.read_text(encoding="utf-8"))
            return True
        except (OSError, ValueError):
            return False

    @property
    def cid(self) -> str:
        return self.state.get("client_id", "")

    def timing(self, label: str, seconds: float) -> None:
        self.state["timings"].append([label, round(seconds, 1)])

    def result(self, index: int, state: str, detail: str) -> None:
        title = STEPS[index][0]
        self.state["results"].append([index + 1, title, state, detail])
        self.save()
        print(f"\n{state} {index + 1:02d}/{TOTAL}: {title}" + (f" — {detail}" if detail else ""),
              flush=True)

    def step(self, index: int) -> str:
        title, name = STEPS[index]
        print(f"\n--- {index + 1:02d}/{TOTAL}: {title}", flush=True)
        try:
            answer = getattr(self, name)()
            if isinstance(answer, tuple) and len(answer) == 3:
                state, detail = answer[0], answer[2]
            else:
                ok, detail = answer if isinstance(answer, tuple) else (bool(answer), "")
                state = "PASS" if ok else "FAIL"
        except Exception as exc:  # noqa: BLE001
            state, detail = "FAIL", f"{type(exc).__name__}: {exc}"
        self.result(index, state, detail)
        return state

    # -- βοηθητικά -------------------------------------------------------------------
    def manifest(self, version: str) -> dict:
        if version not in self.manifests:
            if version == BASE:
                path = self.m.bundle / "manifests" / f"{BASE}.json"
                self.manifests[version] = json.loads(path.read_text(encoding="utf-8"))
            else:
                self.manifests[version] = pkg.read_manifest(
                    self.m.bundle / "packages" / package_file(version),
                    trusted_keys=self.trusted_keys)
        return self.manifests[version]

    def files_ok(self, version: str) -> tuple[bool, str]:
        good, total, problems = check_files(self.layout.version_dir(version), self.manifest(version))
        text = f"αρχεία {version}: {good}/{total} σωστά"
        if problems:
            text += " (" + ", ".join(problems[:3]) + ")"
        return not problems, text

    def mark(self) -> int:
        return int(self.ch.call("mark")["mark"])

    def joined(self, since: int, timeout: float = 90, room: str | None = None) -> str | None:
        reply = self.ch.call("joined", client_id=self.cid, since=since, timeout=timeout,
                             room=room or "")
        return reply.get("line")

    def supervisor_status(self) -> dict:
        from client.supervisor import send_command
        try:
            return send_command(self.layout, "status")
        except (OSError, TimeoutError):
            return {}

    def current(self) -> dict:
        try:
            return self.layout.read_current() or {}
        except ValueError:
            return {}

    def read_cid(self) -> str:
        try:
            return json.loads((self.layout.data / "client.json").read_text(encoding="utf-8"))[
                "client_id"]
        except (OSError, ValueError, KeyError):
            return ""

    def manager_update(self) -> dict:
        return self.ch.call("update_status", client_id=self.cid).get("status", {})

    def import_package(self, version: str) -> tuple[bool, str]:
        reply = self.ch.call("import", name=package_file(version))
        return bool(reply.get("ok")), reply.get("output") or reply.get("error", "")

    def folders(self) -> list[str]:
        try:
            return sorted(p.name for p in self.layout.versions.iterdir() if p.is_dir())
        except OSError:
            return []

    def run_points_to(self, version: str) -> tuple[bool | None, str]:
        if self.m.sim:
            return None, "HKCU\\Run: SKIP"
        value = self.m.run_value() or ""
        ok = f"\\versions\\{version}\\python\\pythonw.exe" in value and "client.supervisor" in value
        return ok, f"Run → {'versions' + chr(92) + version if ok else value or 'καμία τιμή'}"

    def processes_gone(self, info: dict) -> bool:
        return not pid_alive(info.get("client_pid")) and not pid_alive(info.get("supervisor_pid"))

    # ---------------------------------------------------------------------- βήματα
    def s01(self):
        notes = [self.m.windows_info()]
        av = self.m.antivirus()
        self.state["antivirus"] = av
        notes.append("antivirus: " + (", ".join(av) if av else "κανένα" if not self.m.sim else "—"))
        hello = self.ch.call("hello")
        if hello.get("protocol") != FIELD_PROTOCOL:
            return False, f"άλλη έκδοση του field_test στον Director ({hello.get('protocol')})"
        existing = self.layout.home.exists() or bool(self.m.run_value()) or self.m.uninstall_entry()
        if existing:
            return False, (f"υπάρχει ήδη εγκατάσταση Client ({self.layout.home})· αφαιρέστε την "
                           "πρώτα ή τρέξτε field-test.cmd -Cleanup αν έμεινε από αυτή τη δοκιμή")
        data = self.layout.data
        if data.exists():
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = data.with_name(f"{data.name}.before-field-test-{stamp}")
            os.replace(data, backup)
            self.state["backup"] = str(backup)
            notes.append(f"το {data.name} μετακινήθηκε στο {backup.name}")
        self.save()
        cert = self.m.bundle / "setup" / "manager-ca.pem"
        cert.write_text(hello["cert"], encoding="utf-8")
        started = time.monotonic()
        sizes = 0
        for version in UPDATES:
            path = self.m.bundle / "packages" / package_file(version)
            self.manifest(version)                       # υπογραφή: πριν από την αποστολή
            reply = self.ch.upload(path)
            if not reply.get("ok"):
                return False, f"αποστολή {path.name}: {reply.get('error')}"
            sizes += path.stat().st_size
        notes.append(f"4 πακέτα στον Director ({sizes / 1_000_000:.0f} MB, "
                     f"{time.monotonic() - started:.0f} s)")
        return True, " · ".join(notes)

    def s02(self):
        self.state["mark_install"] = self.mark()
        code, seconds = self.m.install([f"/ROOM={ROOM1}", f"/PC={PC1}", f"/MANAGERHOST={self.host}"])
        self.timing(f"setup.exe {BASE} (νέος υπολογιστής)", seconds)
        if code is None:
            return False, f"ο εγκαταστάτης δεν τελείωσε σε {SETUP_TIMEOUT} s (κόλλησε σε παράθυρο;)"
        self.state["client_id"] = self.read_cid()
        try:
            pending = json.loads((self.layout.data / "enrollment-pending.json")
                                 .read_text(encoding="utf-8"))
            self.state["code"] = str(pending["code"])
        except (OSError, ValueError, KeyError):
            self.state["code"] = ""
        self.save()
        files, files_text = self.files_ok(BASE)
        run, run_text = self.run_points_to(BASE)
        ok = code == 0 and bool(self.cid) and bool(self.state["code"]) and files and run is not False
        return ok, (f"κωδικός εξόδου {code}, {seconds:.0f} s, κωδικός εγγραφής "
                    f"{'υπάρχει' if self.state['code'] else 'ΛΕΙΠΕΙ'}, {files_text}, {run_text}")

    def s03(self):
        reply = self.ch.call("approve_enrollment", room=ROOM1, name=PC1, code=self.state["code"],
                             since=self.state["mark_install"], timeout=60)
        if not reply.get("ok"):
            return False, reply.get("error") or "η έγκριση απέτυχε"
        approved_at = time.monotonic()
        mark = self.state["mark_install"]
        line = self.joined(mark, timeout=120, room=ROOM1)
        connect = time.monotonic() - approved_at
        version = wait(lambda: self.supervisor_status().get("version"), 20)
        ok = line is not None and reply.get("client_id") == self.cid and version == BASE
        return ok, (f"εγκρίθηκε {ROOM1} · {PC1}, σύνδεση σε {connect:.0f} s, "
                    f"Client v{version}, UUID {self.cid[:8]}…")

    def s04(self):
        if self.m.sim:
            return "SKIP", None, "SKIP στην προσομοίωση (μόνο σε πραγματικό δίκτυο)"
        mark = self.mark()
        self.m.cli("--set-director-host", "auto")
        found = self.joined(mark, timeout=60) is not None
        rules = self.m.firewall_rules()
        mark = self.mark()
        self.m.cli("--set-director-host", self.host)
        back = self.joined(mark, timeout=90) is not None
        hint = "" if found else (" · αν το VM είναι σε NAT το broadcast δεν φτάνει (βάλτε "
                                 "bridged), αλλιώς δείτε αν εμφανίστηκε ερώτηση Firewall")
        return found and back, (f"χωρίς IP: {'συνδέθηκε' if found else 'ΔΕΝ συνδέθηκε σε 60 s'}, "
                                f"κανόνες Firewall για τον Client: {rules}, με IP ξανά: {back}{hint}")

    def s05(self):
        mark = self.mark()
        code, seconds = self.m.install([f"/ROOM={ROOM2}", f"/PC={PC2}"])
        self.timing(f"setup.exe {BASE} (επανεγκατάσταση, νέα ονόματα)", seconds)
        reply = self.ch.call("approve_change", client_id=self.cid, old_room=ROOM1, old_name=PC1,
                             room=ROOM2, name=PC2, since=mark, timeout=180)
        line = self.joined(mark, timeout=120)
        same = self.read_cid() == self.cid
        ok = code == 0 and bool(reply.get("ok")) and line is not None and same
        return ok, (f"κωδικός εξόδου {code}, {seconds:.0f} s, αίτημα αλλαγής "
                    f"{'εγκρίθηκε' if reply.get('ok') else reply.get('error')}, ίδιο UUID {same}")

    def s06(self):
        mark = self.mark()
        code, seconds = self.m.install([f"/ROOM={ROOM2}", f"/PC={PC2}"])
        self.timing(f"setup.exe {BASE} (επανεγκατάσταση, ίδια ονόματα)", seconds)
        line = self.joined(mark, timeout=120, room=ROOM2)
        seen = self.ch.call("change_seen", since=mark, timeout=20).get("seen")
        same = self.read_cid() == self.cid
        ok = code == 0 and line is not None and not seen and same
        return ok, (f"κωδικός εξόδου {code}, {seconds:.0f} s, συνδέθηκε ως {ROOM2}: "
                    f"{line is not None}, αίτημα αλλαγής: {'ΝΑΙ' if seen else 'κανένα'}, ίδιο UUID {same}")

    def s07(self):
        self.sup_mark = self.sup_log.mark()
        self.client_mark = self.client_log.mark()
        self.state["mark_update"] = self.mark()
        self.ch.call("announce", on=True)
        time.sleep(2)
        self.import_started = time.monotonic()
        ok, output = self.import_package(GOOD)
        seen = self.client_log.wait(f"Λήψη έκδοσης {GOOD}", since=self.client_mark, timeout=120) \
            or self.client_log.wait("Παύση λήψης", since=self.client_mark, timeout=1)
        time.sleep(20)
        early = self.sup_log.find(f"Δοκιμή έκδοσης {GOOD}", since=self.sup_mark)
        paused = self.client_log.find("Παύση λήψης", since=self.client_mark) is not None
        waiting = self.client_log.find("περιμένει το τέλος της ανακοίνωσης",
                                       since=self.client_mark) is not None
        self.ch.call("announce", on=False)
        self.announce_end = time.monotonic()
        return ok and seen is not None and early is None, \
            (f"εισαγωγή {'OK' if ok else output[-120:]}, ο Client είδε την {GOOD}: "
             f"{seen is not None}, εναλλαγή όσο παίζει: {'ΝΑΙ' if early else 'όχι'} "
             f"(παύση λήψης {paused}, αναμονή εναλλαγής {waiting})")

    def s08(self):
        trial = self.sup_log.wait(f"Δοκιμή έκδοσης {GOOD}", since=self.sup_mark, timeout=120)
        after = time.monotonic() - self.announce_end
        installed = self.sup_log.wait(f"Η έκδοση {GOOD} εγκαταστάθηκε", since=self.sup_mark,
                                      timeout=self.m.trial_seconds + 60)
        self.timing(f"ενημέρωση {GOOD}: εισαγωγή → εγκαταστάθηκε",
                    time.monotonic() - self.import_started)
        state = self.current()
        same = self.read_cid() == self.cid
        line = self.client_log.find(f"Λήψη έκδοσης {GOOD}:", since=self.client_mark) or ""
        try:
            words = line.split(f"Λήψη έκδοσης {GOOD}:", 1)[1].split()
            needed, count = int(words[0]), int(words[2])
        except (IndexError, ValueError):
            needed, count = -1, -1
        manager = wait(lambda: (self.manager_update().get("state") == "installed"
                                and self.manager_update().get("app") == GOOD), 60)
        ok = (trial is not None and after >= 9 and installed is not None
              and state.get("current") == GOOD and state.get("previous") == BASE and same
              and needed == 1 and bool(manager))
        return ok, (f"εναλλαγή {after:.0f} s μετά το τέλος της ανακοίνωσης, current "
                    f"{state.get('current')}, previous {state.get('previous')}, κατέβηκαν "
                    f"{needed} από {count} αρχεία, ίδιο UUID {same}, Manager: "
                    f"{self.manager_update().get('state')} {self.manager_update().get('app')}")

    def s09(self):
        files, files_text = self.files_ok(GOOD)
        base, base_text = self.files_ok(BASE)
        cmd = self.layout.cli_file.read_text(encoding="ascii", errors="replace")
        cmd_ok = f"versions\\{GOOD}\\python\\python.exe" in cmd
        run, run_text = self.run_points_to(GOOD)
        shown = self.m.display_version()
        shown_ok = self.m.sim or shown == GOOD
        ok = files and base and cmd_ok and run is not False and shown_ok
        return ok, (f"{files_text}, {base_text}, .cmd {cmd_ok}, {run_text}, «Εφαρμογές»: "
                    f"{shown if not self.m.sim else 'SKIP'}")

    def prepare_reboot(self, script: Path | None) -> None:
        self.state["phase"] = "rebooting"
        self.state["mark_reboot"] = self.mark()
        self.state["reboot_at"] = time.time()
        self.save()
        if not self.m.sim and script is not None:
            self.m.set_runonce(f'cmd.exe /c ""{script}" -Resume"')

    def s10(self):
        info = wait(self.supervisor_status, 240, 2) or {}
        line = self.joined(int(self.state.get("mark_reboot", 0)), timeout=240)
        version = info.get("version")
        if self.m.sim:
            where_ok, where = True, "διαδρομή supervisor: SKIP"
        else:
            path = self.m.process_path(info.get("supervisor_pid"))
            where_ok = f"\\versions\\{GOOD}\\python\\pythonw.exe".lower() in path.lower()
            where = f"supervisor από {path or '?'}"
        uptime = self.m.uptime()
        runonce_gone = not self.m.runonce()
        ok = bool(info) and line is not None and version == GOOD and where_ok and runonce_gone
        return ok, (f"Client v{version}, {where}, συνδέθηκε: {line is not None}, "
                    f"Windows σε λειτουργία {uptime / 60:.1f} λεπτά, supervisor "
                    f"{info.get('uptime')} s, RunOnce καθαρό {runonce_gone}")

    def trial_fails(self, version: str, reason: str, timeout: float):
        mark = self.sup_log.mark()
        joined_mark = self.mark()
        started = time.monotonic()
        ok, output = self.import_package(version)
        trial = self.sup_log.wait(f"Δοκιμή έκδοσης {version}", since=mark, timeout=120)
        rollback = self.sup_log.wait(f"Rollback από την {version}", since=mark, timeout=timeout)
        self.timing(f"{version}: εισαγωγή → rollback", time.monotonic() - started)
        back = self.joined(joined_mark, timeout=120) is not None
        failed = wait(lambda: self.manager_update().get("state") == "failed"
                      and self.manager_update().get("available") == version, 60)
        state = self.current()
        gone = not self.layout.version_dir(version).exists()
        good = ok and trial is not None and rollback is not None and reason in (rollback or "") \
            and state.get("current") == GOOD and version in state.get("failed", []) and back \
            and bool(failed) and gone
        return good, (f"rollback «{(rollback or '—').split(': ', 1)[-1]}», current "
                      f"{state.get('current')}, Manager: failed «"
                      f"{self.manager_update().get('detail', '')[:50]}», ξανά συνδεδεμένος {back}, "
                      f"φάκελος {version} σβήστηκε {gone}")

    def s11(self):
        return self.trial_fails(CRASHING, "τερματίστηκε", 120)

    def s12(self):
        return self.trial_fails(HANGING, f"{self.m.trial_seconds} s", self.m.trial_seconds + 120)

    def s13(self):
        time.sleep(3)
        mark = self.client_log.mark()
        self.ch.call("start_all")
        time.sleep(10)
        again = self.client_log.find("Λήψη έκδοσης 0.6.", since=mark)
        failed = self.current().get("failed", [])
        return again is None and failed == [CRASHING, HANGING], \
            f"failed {failed}, νέα λήψη: {again or 'καμία'}"

    def s14(self):
        mark = self.sup_log.mark()
        started = time.monotonic()
        ok, output = self.import_package(FINAL)
        installed = self.sup_log.wait(f"Η έκδοση {FINAL} εγκαταστάθηκε", since=mark,
                                      timeout=self.m.trial_seconds + 180)
        self.timing(f"ενημέρωση {FINAL}: εισαγωγή → εγκαταστάθηκε", time.monotonic() - started)
        manager = wait(lambda: self.manager_update().get("state") == "installed"
                       and self.manager_update().get("app") == FINAL, 60)
        folders = self.folders()
        files, files_text = self.files_ok(FINAL)
        shown = self.m.display_version()
        good = ok and installed is not None and bool(manager) and folders == [GOOD, FINAL] and \
            files and (self.m.sim or shown == FINAL)
        return good, (f"εκδόσεις στον δίσκο: {', '.join(folders)}, {files_text}, Manager: "
                      f"{self.manager_update().get('state')} {self.manager_update().get('app')}, "
                      f"«Εφαρμογές»: {shown if not self.m.sim else 'SKIP'}")

    def s15(self):
        before = self.supervisor_status().get("client_pid")
        mark = self.mark()
        code = self.m.cli("--restart")
        info = wait(lambda: (self.supervisor_status().get("client_pid") not in (None, before)
                             and self.supervisor_status()), 30) or {}
        line = self.joined(mark, timeout=90)
        return code == 0 and bool(info) and line is not None and info.get("version") == FINAL, \
            f"κωδικός {code}, νέος Client v{info.get('version')}, συνδέθηκε {line is not None}"

    def s16(self):
        info = self.supervisor_status()
        removed = self.m.uninstall(purge=False)
        gone = wait(lambda: self.processes_gone(info), 30)
        run_gone = not self.m.run_value()
        entry_gone = not self.m.uninstall_entry()
        kept = self.read_cid() == self.cid
        return removed and gone and run_gone and entry_gone and kept, \
            (f"φάκελος προγράμματος σβήστηκε {removed}, διεργασίες έκλεισαν {bool(gone)}, "
             f"HKCU\\Run {run_gone if not self.m.sim else 'SKIP'}, «Εφαρμογές» "
             f"{entry_gone if not self.m.sim else 'SKIP'}, ταυτότητα έμεινε {kept}")

    def s17(self):
        mark = self.mark()
        code, seconds = self.m.install([])
        self.timing(f"setup.exe {BASE} (επανεγκατάσταση μετά από απεγκατάσταση)", seconds)
        same = self.read_cid() == self.cid
        no_code = not (self.layout.data / "enrollment-pending.json").exists() or \
            self.state.get("code") == json.loads((self.layout.data / "enrollment-pending.json")
                                                 .read_text(encoding="utf-8")).get("code")
        line = self.joined(mark, timeout=120)
        self.sup_log = Log(self.layout.logs / "supervisor.log")
        self.client_log = Log(self.layout.logs / "client.log")
        installed = self.sup_log.wait(f"Η έκδοση {FINAL} εγκαταστάθηκε",
                                      timeout=self.m.trial_seconds + 240)
        line2 = self.client_log.find(f"Λήψη έκδοσης {FINAL}:") or ""
        try:
            needed = int(line2.split(f"Λήψη έκδοσης {FINAL}:", 1)[1].split()[0])
        except (IndexError, ValueError):
            needed = -1
        version = wait(lambda: self.supervisor_status().get("version") == FINAL, 30)
        ok = code == 0 and same and no_code and line is not None and installed is not None and \
            bool(version) and needed == 1
        return ok, (f"κωδικός εξόδου {code}, {seconds:.0f} s, ίδιο UUID {same}, συνδέθηκε χωρίς "
                    f"νέα έγκριση {line is not None}, αυτόματα σε {FINAL}: {installed is not None} "
                    f"(κατέβηκαν {needed} αρχεία)")

    def s18(self):
        info = self.supervisor_status()
        removed = self.m.uninstall(purge=True)
        gone = wait(lambda: self.processes_gone(info), 30)
        data_gone = not self.layout.data.exists()
        run_gone = not self.m.run_value()
        restored = self.restore_backup()
        return removed and bool(gone) and data_gone and run_gone, \
            (f"πρόγραμμα σβήστηκε {removed}, ταυτότητα σβήστηκε {data_gone}, διεργασίες έκλεισαν "
             f"{bool(gone)}, HKCU\\Run {run_gone if not self.m.sim else 'SKIP'}"
             + (f", {restored}" if restored else ""))

    def restore_backup(self) -> str:
        backup = self.state.get("backup")
        if not backup or not Path(backup).exists() or self.layout.data.exists():
            return ""
        os.replace(backup, self.layout.data)
        self.state["backup"] = None
        self.save()
        return f"το {self.layout.data.name} επαναφέρθηκε"

    # ------------------------------------------------------------------ εκτέλεση
    def cleanup(self) -> None:
        """Μετά από διακοπή/αποτυχία: όσα έμειναν από αυτή τη δοκιμή."""
        try:
            if self.layout.home.exists():
                self.m.uninstall(purge=True)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"    απεγκατάσταση: {exc}", flush=True)
        if self.state.get("client_id") and self.layout.data.exists() and \
                self.read_cid() == self.state["client_id"]:
            shutil.rmtree(self.layout.data, ignore_errors=True)
        self.m.set_runonce(None)
        restored = self.restore_backup()
        if restored:
            print(f"    {restored}", flush=True)

    def summary(self) -> str:
        rows = self.state["results"]
        counts = {key: sum(1 for row in rows if row[2] == key) for key in ("PASS", "SKIP", "FAIL")}
        lines = [f"{state} {index:02d}: {title}" + (f" — {detail}" if detail else "")
                 for index, title, state, detail in rows]
        lines.append("")
        lines.append("Χρόνοι:")
        lines += [f"  {label}: {seconds:.0f} s" for label, seconds in self.state["timings"]]
        if self.state.get("antivirus") is not None:
            lines.append("Antivirus: " + (", ".join(self.state["antivirus"]) or "κανένα"))
        lines.append(f"ΣΥΝΟΛΟ: {counts['PASS']}/{TOTAL} PASS, {counts['SKIP']} SKIP, "
                     f"{counts['FAIL']} FAIL")
        return "\n".join(lines)

    def run_steps(self, indexes) -> bool:
        for index in indexes:
            state = self.step(index)
            if state == "FAIL" and STEPS[index][1] in ABORT_IF_FAILED:
                for later in range(index + 1, TOTAL):
                    self.result(later, "FAIL", "δεν έγινε το προηγούμενο βήμα")
                return False
        return True

    def finish(self) -> int:
        self.state["phase"] = "done"
        self.save()
        text = self.summary()
        print("\n" + text, flush=True)
        try:
            self.ch.call("finish", report=text)
        except (OSError, ConnectionError, ValueError):
            print("(ο Director δοκιμής δεν πήρε το αποτέλεσμα)", flush=True)
        return 0 if not any(row[2] == "FAIL" for row in self.state["results"]) else 1

    def run(self, *, resume: bool, script: Path | None) -> int:
        if resume:
            if not self.load() or self.state.get("phase") != "rebooting":
                print("Δεν υπάρχει δοκιμή σε επανεκκίνηση· ξεκινήστε κανονικά.", flush=True)
                return 2
            self.host = self.ch.host = self.state["host"]
            print("Συνέχεια μετά την επανεκκίνηση...", flush=True)
            self.ch.connect_retry(300)
            self.m.after_logon()
            return self.part_two()
        self.save()
        try:
            if not self.run_steps(range(REBOOT_AFTER)):
                self.cleanup()
                return self.finish()
        except BaseException:
            self.cleanup()
            raise
        self.prepare_reboot(script)
        if self.m.sim:
            self.m.reboot_now()
            time.sleep(2)
            self.m.after_logon()
            return self.part_two()
        print("\n" + "=" * 70, flush=True)
        print("Επανεκκίνηση των Windows σε 20 s (ακύρωση: shutdown /a).", flush=True)
        print("Μετά τη σύνδεση στα Windows η δοκιμή συνεχίζει μόνη της σε νέο παράθυρο.",
              flush=True)
        print("Αν δεν συνεχίσει: field-test.cmd -Resume", flush=True)
        print("=" * 70, flush=True)
        if self.auto_reboot:
            self.m.reboot_now()
        return 0

    def part_two(self) -> int:
        self.state["phase"] = "part2"
        self.save()
        try:
            self.run_steps(range(REBOOT_AFTER, TOTAL))
        finally:
            if self.layout.home.exists() or self.state.get("backup"):
                self.cleanup()
        return self.finish()


def run_client(args) -> int:
    bundle = Path(args.bundle).resolve()
    logs = bundle / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = logs / f"field-test-{stamp}.txt"
    sys.stdout = Tee(sys.stdout, log_path)
    print(f"(η έξοδος γράφεται και στο {log_path})", flush=True)
    state_file = bundle / STATE_FILE
    if args.sim_root:
        machine = SimMachine(bundle, Path(args.sim_root))
        trusted = [machine.public]
    else:
        if os.name != "nt":
            print("Ο ρόλος client τρέχει μόνο στα Windows (στο Linux: selfcheck).", flush=True)
            return 2
        machine = WindowsMachine(bundle)
        trusted = None
    host = args.director_host or ""
    if not host and not args.resume:
        try:
            saved = json.loads(state_file.read_text(encoding="utf-8")).get("host", "")
        except (OSError, ValueError):
            saved = ""
        host = input(f"IP του Director δοκιμής{f' [{saved}]' if saved else ''}: ").strip() or saved
    channel = Channel(host or "127.0.0.1", args.port)
    test = FieldTest(machine, channel, state_file=state_file, host=host, trusted_keys=trusted,
                     auto_reboot=not args.no_reboot)
    if args.cleanup:
        test.load()
        test.cleanup()
        state_file.unlink(missing_ok=True)
        print("Καθαρισμός έγινε.", flush=True)
        return 0
    if not args.resume:
        try:
            channel.connect()
        except OSError as exc:
            print(f"FAIL: δεν βρέθηκε ο Director δοκιμής στο {host}:{args.port} ({exc}). Τρέχει "
                  "το tools/field_test.sh director στο Linux;", flush=True)
            return 1
    script = bundle / "field-test.cmd"
    return test.run(resume=args.resume, script=script if script.exists() else None)


# ============================================================ selfcheck (Linux)
def build_sim_bundle(bundle: Path) -> str:
    """Bundle χωρίς embeddable Python/setup.exe: δέντρα με τον κώδικα, κλειδί δοκιμής."""
    from tools import build_release
    trees = bundle / "trees"
    base = trees / BASE
    for relative in build_release.code_files():
        target = base / "app" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    (base / "VERSION").write_text(BASE + "\n", encoding="ascii")
    write_base_manifest(base, bundle / "manifests" / f"{BASE}.json")
    key = pkg.generate_key()
    public = pkg.public_hex(key)
    (bundle / "test-key.txt").write_text(public + "\n", encoding="ascii")
    (bundle / "packages").mkdir(parents=True, exist_ok=True)
    (bundle / "setup").mkdir(parents=True, exist_ok=True)
    for version, code in VARIANTS.items():
        tree = make_variant(base, trees / version, version, code)
        pkg.write_package(tree, bundle / "packages" / package_file(version), key, version)
    return public


def run_selfcheck(args) -> int:
    with tempfile.TemporaryDirectory(prefix="sch-field-selfcheck-") as temp:
        temp = Path(temp)
        bundle = temp / "field-test"
        public = build_sim_bundle(bundle)
        os.environ["SCH_INTERCOM_NO_TRAY"] = "1"
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        director = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "director", "--work",
             str(temp / "director"), "--bind", "127.0.0.1", "--trusted-key", public,
             *(["--quiet"] if args.quiet else [])], cwd=ROOT, env=env)
        try:
            channel = Channel("127.0.0.1")
            channel.connect_retry(60)
            if channel.call("hello").get("protocol") != FIELD_PROTOCOL:
                print("FAIL: ο Director δοκιμής δεν απαντά σωστά", flush=True)
                return 1
            machine = SimMachine(bundle, temp / "pc")
            test = FieldTest(machine, channel, state_file=bundle / STATE_FILE,
                             host="127.0.0.1", trusted_keys=[public])
            return test.run(resume=False, script=None)
        finally:
            try:
                director.wait(30)
            except subprocess.TimeoutExpired:
                director.terminate()
                director.wait(10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Δοκιμή Client σε καθαρά Windows (Φάση 5)")
    roles = parser.add_subparsers(dest="role", required=True)
    director = roles.add_parser("director", help="Linux: Manager δοκιμής + κανάλι")
    director.add_argument("--bind", default="0.0.0.0")
    director.add_argument("--port", type=int, default=CHANNEL_PORT)
    director.add_argument("--work", default=None, help=argparse.SUPPRESS)
    director.add_argument("--trusted-key", default=None, help=argparse.SUPPRESS)
    director.add_argument("--quiet", action="store_true", help="Χωρίς την έξοδο του Manager")
    client = roles.add_parser("client", help="Windows: ο βοηθός που οδηγεί τη δοκιμή")
    client.add_argument("--bundle", default=str(ROOT.parent))
    client.add_argument("--director-host", default="")
    client.add_argument("--port", type=int, default=CHANNEL_PORT)
    client.add_argument("--resume", action="store_true", help="Συνέχεια μετά την επανεκκίνηση")
    client.add_argument("--cleanup", action="store_true",
                        help="Αφαίρεση όσων έμειναν από διακοπείσα δοκιμή")
    client.add_argument("--no-reboot", action="store_true",
                        help="Χωρίς αυτόματη επανεκκίνηση (την κάνετε εσείς)")
    client.add_argument("--sim-root", default=None, help=argparse.SUPPRESS)
    selfcheck = roles.add_parser("selfcheck", help="Linux: έλεγχος του script σε loopback")
    selfcheck.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    if args.role == "director":
        return run_director(args)
    if args.role == "client":
        return run_client(args)
    return run_selfcheck(args)


if __name__ == "__main__":
    sys.exit(main())
