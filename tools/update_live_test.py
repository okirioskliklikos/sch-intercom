"""Φάση 5B2a: πραγματική δοκιμή διανομής ενημερώσεων (docs/phase5.md §5.2–5.4, §7).

Τρέχουν τα πραγματικά ``director.main`` και ``client.main`` ως διεργασίες, μόνο σε
loopback, σε προσωρινό φάκελο και θύρες 518xx· η κανονική εγκατάσταση δεν αγγίζεται.
Τα πακέτα υπογράφονται με κλειδί δοκιμής (κρυφές επιλογές ``--update-trusted-key``)·
το πραγματικό κλειδί του συντηρητή δεν χρησιμοποιείται. Οι Clients είναι
«εγκατεστημένοι» 1.0.0 (current.json σε προσωρινό SCH_INTERCOM_HOME).

* Ένα TCP proxy με όριο ταχύτητας μπαίνει μπροστά στη θύρα ενημερώσεων του Board2,
  ώστε η λήψη να κρατά λίγα δευτερόλεπτα: παύση στην ανακοίνωση, κόψιμο, συνέχιση.
* Οι 6 συνδέσεις για το όριο των 5 λήψεων γίνονται από το script με πραγματικό mTLS.
* Η εγκατάσταση της νέας έκδοσης (εναλλαγή, rollback) είναι το 5B2b.

    Windows:  powershell -ExecutionPolicy Bypass -File tools\\update_live_test.ps1
    Linux:    tools/update_live_test.sh
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import package as pkg  # noqa: E402
from shared.client_registry import SignedClientRegistry  # noqa: E402
from shared.device_identity import create_device_identity  # noqa: E402
from shared.identity import load_identity  # noqa: E402
from shared.transport_security import DEFAULT_SERVER_NAME, client_context  # noqa: E402
from shared.update_protocol import check_message, read_message, send_message  # noqa: E402
from client.install_paths import DATA_ENV, HOME_ENV, Layout  # noqa: E402
from tools.mtls_live_test import JOINED, ROOT, Process, child_env  # noqa: E402

TOTAL = 15
HOST = "127.0.0.1"
PORT = 51855
DISCOVERY_PORT = 51856
WATCH_PORT = 51857
EXTRA_BEACON_PORT = 51867
ENROLL_PORT = 51859
UPDATE_PORT = 51860
PROXY_PORT = 51861
ROOM = "C2627-Γ1"
INSTALLED = "1.0.0"
BIG_MB = 12
RATE = 1_500_000            # bytes/s μέσω του proxy του Board2 (~8 s για 12 MB)
RETRY_SECONDS = 3            # συνέχιση μετά από κομμένη λήψη (κανονικά 30 s)


class ThrottleProxy:
    """TCP proxy με όριο ταχύτητας προς τον Client· ``cut()`` κόβει τις συνδέσεις."""

    def __init__(self, port: int, upstream: int, rate: int):
        self.upstream, self.rate = upstream, rate
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.pairs: list[tuple[socket.socket, socket.socket]] = []
        self.server = socket.create_server((HOST, port))
        self.server.settimeout(0.2)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.closed.is_set():
            try:
                client, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                upstream = socket.create_connection((HOST, self.upstream), timeout=3)
            except OSError:
                client.close()
                continue
            with self.lock:
                self.pairs.append((client, upstream))
            threading.Thread(target=self._pump, args=(client, upstream, None),
                             daemon=True).start()
            threading.Thread(target=self._pump, args=(upstream, client, self.rate),
                             daemon=True).start()

    def _pump(self, source, target, rate):
        source.settimeout(0.2)
        while not self.closed.is_set():
            try:
                data = source.recv(16384)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            try:
                target.sendall(data)
            except OSError:
                break
            if rate:
                time.sleep(len(data) / rate)
        for item in (source, target):
            try:
                item.close()
            except OSError:
                pass

    def cut(self) -> int:
        with self.lock:
            pairs, self.pairs = self.pairs, []
        for pair in pairs:
            for item in pair:
                try:
                    item.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                item.close()
        return len(pairs)

    def close(self):
        self.closed.set()
        self.cut()
        self.server.close()


class Board:
    """Πραγματικός Client, εγκεκριμένος και «εγκατεστημένος» στην έκδοση 1.0.0."""

    def __init__(self, base: Path, name: str, *, update_port: int = UPDATE_PORT):
        self.name = name
        self.dir = base / f"client-{name}"
        self.layout = Layout(self.dir / "home", self.dir / "data")
        self.update_port = update_port
        self.client_id = ""
        self.process: Process | None = None

    def approve(self, registry: SignedClientRegistry) -> None:
        self.client_id, _ = load_identity(self.dir / "client.json", room_override=ROOM)
        cert, _, _ = create_device_identity(self.dir / "credentials", self.client_id)
        registry.approve(self.client_id, ROOM, cert.read_bytes(), name=self.name)
        self.layout.set_installed(INSTALLED)

    def start(self, trust: Path, public: str) -> Process:
        self.stop()
        args = ["-m", "client.main", "--tls-ca", str(trust),
                "--identity-file", str(self.dir / "client.json"),
                "--client-credentials-dir", str(self.dir / "credentials"),
                "--client-hints-file", str(self.dir / "client-hints.json"),
                "--registry-seen-file", str(self.dir / "registry-seen.json"),
                "--volume-recovery-file", str(self.dir / "volume-recovery.json"),
                "--room", ROOM, "--host", HOST, "--port", str(PORT),
                "--enrollment-port", str(ENROLL_PORT),
                "--update-port", str(self.update_port), "--update-trusted-key", public,
                "--update-retry-seconds", str(RETRY_SECONDS)]
        saved = {key: os.environ.get(key) for key in (HOME_ENV, DATA_ENV)}
        os.environ[HOME_ENV] = str(self.layout.home)
        os.environ[DATA_ENV] = str(self.layout.data)
        try:
            self.process = Process(args, label=f"Client {self.name}")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.process.wait_for("Client ID:", timeout=30)
        return self.process

    def ready(self, version: str) -> bool:
        return (self.layout.version_dir(version) / "VERSION").is_file()

    def stop(self) -> None:
        if self.process is not None:
            self.process.stop()
            self.process = None


class Raw:
    """Σύνδεση ενημερώσεων από το script (όριο 5 λήψεων), με πραγματικό mTLS."""

    def __init__(self, base: Path, index: int, cert_path: Path, registry):
        self.client_id = str(uuid.uuid4())
        cert, key, password = create_device_identity(base / f"raw-{index}", self.client_id)
        registry.approve(self.client_id, ROOM, cert.read_bytes(), name=f"R{index}")
        self.context = client_context(cert_path, certfile=cert, keyfile=key, password=password)
        self.stream = None

    def get(self, version: str) -> str:
        """CHECK + GET του πρώτου κομματιού· η σύνδεση μένει ανοιχτή (κρατά θέση)."""
        plain = socket.create_connection((HOST, UPDATE_PORT), timeout=5)
        self.stream = self.context.wrap_socket(plain, server_hostname=DEFAULT_SERVER_NAME)
        self.stream.settimeout(10)
        send_message(self.stream, check_message(self.client_id, INSTALLED, updater=1,
                                                protocol=7))
        offer = read_message(self.stream)
        if offer.get("type") != "offer" or offer.get("version") != version:
            return f"offer {offer}"
        send_message(self.stream, {"type": "get", "version": version, "offset": 0})
        reply = read_message(self.stream)
        if reply.get("type") == "data":
            remaining = reply["size"]
            while remaining:
                remaining -= len(self.stream.recv(min(remaining, 65536)))
        return reply.get("type")

    def close(self):
        if self.stream is not None:
            try:
                self.stream.close()
            except OSError:
                pass
            self.stream = None


class LiveTest:
    def __init__(self, root: Path):
        self.root = root
        self.dir = root / "manager"
        self.dir.mkdir(parents=True)
        self.cert = self.dir / "tls" / "director-tls.crt"
        self.key = self.dir / "tls" / "director-tls.key"
        self.registry = self.dir / "clients-registry.json"
        self.status_file = self.dir / "updates" / "updates-status.json"
        self.signing = pkg.generate_key()
        self.public = pkg.public_hex(self.signing)
        self.director: Process | None = None
        self.b1 = Board(root, "Board1")
        self.b2 = Board(root, "Board2", update_port=PROXY_PORT)
        self.b3 = Board(root, "Board3")
        self.boards = [self.b1, self.b2, self.b3]
        self.raw: list[Raw] = []
        self.proxy: ThrottleProxy | None = None
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

    def package(self, version: str, *, mb: float = 0.2, key=None) -> Path:
        tree = self.root / "trees" / f"{version}-{uuid.uuid4().hex[:6]}"
        (tree / "app" / "client").mkdir(parents=True)
        (tree / "app" / "client" / "main.py").write_text(f"# {version}\n", encoding="utf-8")
        (tree / "app" / "blob.bin").write_bytes(os.urandom(int(mb * 1_000_000)))
        (tree / "VERSION").write_text(version + "\n", encoding="ascii")
        out = self.root / "packages" / f"{version}-{uuid.uuid4().hex[:6]}.sicu"
        out.parent.mkdir(exist_ok=True)
        pkg.write_package(tree, out, key or self.signing, version)
        return out

    def updates(self, *args: str) -> tuple[int, str]:
        done = subprocess.run(
            [sys.executable, "-m", "director.updates", "--registry-file", str(self.registry),
             "--trusted-key", self.public, *args],
            cwd=ROOT, env=child_env(), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120)
        output = (done.stdout + done.stderr).strip()
        for line in output.splitlines():
            print(f"    [updates {args[0]}] {line}", flush=True)
        return done.returncode, output

    def status(self, board: Board) -> dict:
        try:
            data = json.loads(self.status_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data.get("clients", {}).get(board.client_id, {})

    def wait(self, condition, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.1)
        return condition()

    def start_manager(self) -> Process:
        self.director = Process([
            "-m", "director.main", "--registry-file", str(self.registry),
            "--tls-cert", str(self.cert), "--tls-key", str(self.key), "--host", HOST,
            "--port", str(PORT), "--discovery-port", str(DISCOVERY_PORT),
            "--enrollment-port", str(ENROLL_PORT), "--update-port", str(UPDATE_PORT),
            "--update-trusted-key", self.public,
            "--director-id-file", str(self.dir / "director.id"),
            "--watch-port", str(WATCH_PORT), "--extra-beacon-port", str(EXTRA_BEACON_PORT),
            "--start-check-seconds", "1", "--test-tone", "silent"], label="Manager")
        if self.director.wait_for("[Ενημερώσεις] Ενεργή TLS θύρα", timeout=30) is None:
            raise RuntimeError("Δεν άνοιξε η θύρα ενημερώσεων του Manager")
        # Ο τόνος δοκιμής ξεκινά προς όλους· ανακοίνωση μόνο όταν τη ζητά βήμα.
        self.director.send("none")
        return self.director

    def joined(self, board: Board, since: int, timeout: float = 30) -> bool:
        return self.director.wait_for(board.client_id, JOINED, since=since,
                                      timeout=timeout) is not None

    def port_open(self, port: int) -> bool:
        try:
            socket.create_connection((HOST, port), timeout=1).close()
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------ βήματα
    def s01(self):
        self.cert.parent.mkdir(parents=True, exist_ok=True)
        done = subprocess.run([sys.executable, "-m", "director.tls_identity",
                               "--cert", str(self.cert), "--key", str(self.key)],
                              cwd=ROOT, env=child_env(), capture_output=True, timeout=60)
        if done.returncode != 0:
            return False, "δεν δημιουργήθηκε ταυτότητα Manager"
        registry = SignedClientRegistry(self.registry, self.cert, self.key)
        for board in self.boards:
            board.approve(registry)
        self.raw = [Raw(self.root, index, self.cert, registry) for index in range(6)]
        del registry
        self.big = self.package("1.0.1", mb=BIG_MB)
        code, out = self.updates("import", str(self.big))
        return code == 0 and "1.0.1" in out, out.splitlines()[-1] if out else ""

    def s02(self):
        wrong = self.package("1.0.2", key=pkg.generate_key())
        tampered = self.root / "packages" / "tampered.sicu"
        source = self.package("1.0.2")
        import zipfile
        with zipfile.ZipFile(source) as old, zipfile.ZipFile(tampered, "w") as new:
            for info in old.infolist():
                data = old.read(info)
                if info.filename == "files/app/client/main.py":
                    data = data[:-1] + bytes([data[-1] ^ 1])
                new.writestr(info.filename, data)
        older = self.package("1.0.0")
        results = [self.updates("import", str(path)) for path in (wrong, tampered, older)]
        expected = ("Άκυρη υπογραφή", "Αλλοιωμένο αρχείο", "δεν είναι νεότερη")
        ok = all(code == 2 and text in out for (code, out), text in zip(results, expected))
        return ok, " | ".join(out.splitlines()[-1] for _, out in results)

    def s03(self):
        self.start_manager()
        self.proxy = ThrottleProxy(PROXY_PORT, UPDATE_PORT, RATE)
        mark = self.director.mark()
        self.b1.start(self.cert, self.public)
        self.b2.start(self.cert, self.public)
        joined = self.joined(self.b1, mark) and self.joined(self.b2, mark)
        ready = self.wait(lambda: self.b1.ready("1.0.1"), 30)
        state = self.status(self.b1)
        return joined and ready and state.get("state") == "ready", \
            f"Board1: {state.get('state')} {state.get('available')}"

    def s04(self):
        """Board2 κατεβάζει αργά· «all» = ανακοίνωση: παύση, «none»: συνέχεια."""
        part = self.b2.layout.home / "staging" / "1.0.1.part"
        started = self.wait(lambda: part.is_file() and part.stat().st_size > 1_000_000, 20)
        mark = self.b2.process.mark()
        self.director.send("all")
        paused = self.b2.process.wait_for("Παύση λήψης", since=mark, timeout=10) is not None
        waiting = self.wait(lambda: self.status(self.b2).get("state") == "waiting", 5)
        size = part.stat().st_size if part.exists() else -1
        time.sleep(2)
        still = (part.stat().st_size if part.exists() else -2) == size
        self.director.send("none")
        resumed = self.b2.process.wait_for("Συνέχιση λήψης 1.0.1", since=mark,
                                           timeout=20) is not None
        return started and paused and waiting and still and resumed, \
            f"παύση {paused}, Manager «waiting» {waiting}, σταθερό .part {still}, " \
            f"συνέχεια {resumed}"

    def s05(self):
        """Κόψιμο της σύνδεσης ενημερώσεων στη μέση· συνέχιση από offset, μετά έτοιμη."""
        part = self.b2.layout.home / "staging" / "1.0.1.part"
        mark = self.b2.process.mark()
        self.wait(lambda: part.is_file() and part.stat().st_size > 4_000_000, 20)
        before = part.stat().st_size if part.exists() else 0
        cut = self.proxy.cut()
        broken = self.b2.process.wait_for("Η λήψη διακόπηκε", since=mark, timeout=15)
        line = self.b2.process.wait_for("Συνέχιση λήψης 1.0.1 από", since=mark, timeout=30)
        ready = self.wait(lambda: self.b2.ready("1.0.1"), 60)
        percent = line.rsplit(" ", 1)[-1] if line else "—"
        return cut > 0 and broken is not None and line is not None and ready, \
            f"κόπηκε στα {before / 1e6:.1f} MB, συνέχεια από {percent}, έτοιμη {ready}"

    def s06(self):
        """Ο Board3 ήταν κλειστός: παίρνει την έκδοση όταν ανοίξει."""
        mark = self.director.mark()
        self.b3.start(self.cert, self.public)
        return self.joined(self.b3, mark) and self.wait(lambda: self.b3.ready("1.0.1"), 30)

    def s07(self):
        """Επανασύνδεση: νέο CHECK μετά από επανεκκίνηση του Manager."""
        before = self.status(self.b1).get("seen_at", 0)
        time.sleep(1.1)
        self.director.stop(quit_command="quit")
        self.start_manager()
        back = all(self.joined(board, 0, timeout=40) for board in self.boards)
        checked = self.wait(lambda: self.status(self.b1).get("seen_at", 0) > before, 20)
        return back and checked, \
            f"seen_at {before} → {self.status(self.b1).get('seen_at')}"

    def s08(self):
        """Νέο πακέτο ενώ τρέχουν: ο Manager ζητά CHECK, χωρίς επανασύνδεση."""
        mark = self.b1.process.mark()
        code, _ = self.updates("import", str(self.package("1.0.2")))
        pushed = self.director.wait_for("Νέο πακέτο 1.0.2", timeout=10) is not None
        ready = all(self.wait(lambda b=b: b.ready("1.0.2"), 30) for b in self.boards)
        no_reconnect = not self.b1.process.seen("Η σύνδεση διακόπηκε", since=mark)
        return code == 0 and pushed and ready and no_reconnect, \
            f"push {pushed}, όλοι 1.0.2 {ready}, χωρίς επανασύνδεση {no_reconnect}"

    def s09(self):
        """auto off: τίποτα· «Έναρξη τώρα» μόνο για τον Board1."""
        code_off, _ = self.updates("auto", "off")
        code_imp, _ = self.updates("import", str(self.package("1.0.3")))
        time.sleep(4)   # ο Manager το ξαναδιαβάζει κάθε 2 s
        quiet = not any(b.ready("1.0.3") for b in self.boards)
        code_start, _ = self.updates("start", "--client", self.b1.client_id)
        started = self.director.wait_for("Έναρξη τώρα", timeout=10) is not None
        b1 = self.wait(lambda: self.b1.ready("1.0.3"), 30)
        time.sleep(2)
        others = not self.b3.ready("1.0.3") and not self.b2.ready("1.0.3")
        state = self.status(self.b3).get("state")
        return (code_off, code_imp, code_start) == (0, 0, 0) and quiet and started and b1 \
            and others, f"χωρίς λήψη πριν {quiet}, Board1 {b1}, οι άλλοι όχι {others} ({state})"

    def s10(self):
        """«Έναρξη τώρα» για όλους, μετά ξανά auto on."""
        code, _ = self.updates("start", "--all")
        ready = all(self.wait(lambda b=b: b.ready("1.0.3"), 30) for b in self.boards)
        code_on, _ = self.updates("auto", "on")
        return code == 0 and code_on == 0 and ready

    def s11(self):
        """Όριο 5 ταυτόχρονων λήψεων: ο 6ος παίρνει BUSY, μπαίνει όταν φύγει ένας."""
        # Οι Boards έχουν ήδη την 1.0.3· οι 6 συνδέσεις του script είναι «1.0.0».
        answers = [raw.get("1.0.3") for raw in self.raw[:5]]
        sixth = self.raw[5].get("1.0.3")
        self.raw[5].close()
        self.raw[0].close()
        time.sleep(0.5)
        again = self.raw[5].get("1.0.3")
        for raw in self.raw:
            raw.close()
        return answers == ["data"] * 5 and sixth == "busy" and again == "data", \
            f"5 λήψεις {answers.count('data')}/5, 6η: {sixth}, μετά: {again}"

    def s12(self):
        """Αλλοιωμένο πακέτο από τον Manager (με σωστό hash): ο Client το απορρίπτει."""
        forged = self.package("1.0.4", key=pkg.generate_key())
        legit = self.package("1.0.4")
        self.updates("auto", "off")          # ώστε κανείς να μην πάρει το γνήσιο πρώτα
        code, _ = self.updates("import", str(legit))
        folder = self.dir / "updates"
        config = json.loads((folder / "updates-config.json").read_text(encoding="utf-8"))
        target = folder / config["current"]["file"]
        # «Κακόβουλος» Manager: άλλο περιεχόμενο, ενημερωμένο hash/μέγεθος.
        shutil.copyfile(forged, target)
        import hashlib
        config["current"]["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        config["current"]["size"] = target.stat().st_size
        (folder / "updates-config.json").write_text(json.dumps(config), encoding="utf-8")
        self.updates("start", "--all")
        rejected = self.wait(lambda: self.status(self.b1).get("state") == "failed", 30)
        detail = self.status(self.b1).get("detail", "")
        failed = "1.0.4" in self.b1.layout.read_current()["failed"]
        self.updates("auto", "on")
        return code == 0 and rejected and "Άκυρη υπογραφή" in detail and failed and \
            not self.b1.ready("1.0.4"), f"Manager βλέπει: failed «{detail[:60]}»"

    def s13(self):
        """Μη εγκεκριμένος υπολογιστής δεν περνά το mTLS της θύρας ενημερώσεων."""
        client_id = str(uuid.uuid4())
        cert, key, password = create_device_identity(self.root / "stranger", client_id)
        context = client_context(self.cert, certfile=cert, keyfile=key, password=password)
        try:
            plain = socket.create_connection((HOST, UPDATE_PORT), timeout=5)
            with context.wrap_socket(plain, server_hostname=DEFAULT_SERVER_NAME) as stream:
                stream.settimeout(5)
                send_message(stream, check_message(client_id, INSTALLED, updater=1,
                                                   protocol=7))
                reply = read_message(stream)
        except (OSError, EOFError) as exc:
            return True, f"απορρίφθηκε: {type(exc).__name__}"
        return False, f"απάντηση {reply}"

    def s14(self):
        """Εφεδρικός: η θύρα ενημερώσεων κλείνει· Κύριος: ξανανοίγει."""
        mark = self.director.mark()
        self.director.send("role backup confirm")
        closed = self.director.wait_for("Η θύρα ενημερώσεων έκλεισε", since=mark,
                                        timeout=10) is not None
        refused = self.wait(lambda: not self.port_open(UPDATE_PORT), 5)
        mark = self.director.mark()
        self.director.send("role primary confirm")
        opened = self.director.wait_for("[Ενημερώσεις] Ενεργή TLS θύρα", since=mark,
                                        timeout=10) is not None
        return closed and refused and opened and self.port_open(UPDATE_PORT), \
            f"Εφεδρικός: κλειστή {refused}, Κύριος: ανοιχτή {opened}"

    def s15(self):
        """Κανονικό κλείσιμο: καμία μισή λήψη, η εγκατεστημένη έκδοση δεν άλλαξε."""
        for board in self.boards:
            board.stop()
        self.director.stop(quit_command="quit")
        self.director = None
        staging = [p.name for b in self.boards
                   for p in (b.layout.home / "staging").glob("*") if p.suffix == ".part"]
        installed = {b.layout.current_version() for b in self.boards}
        status = json.loads(self.status_file.read_text(encoding="utf-8"))
        return not staging and installed == {INSTALLED} and len(status["clients"]) >= 3, \
            f"εγκατεστημένη {installed}, μισά αρχεία {staging or 'κανένα'}"

    def close(self):
        for board in self.boards:
            board.stop()
        for raw in self.raw:
            raw.close()
        if self.director is not None:
            self.director.stop(quit_command="quit")
        if self.proxy is not None:
            self.proxy.close()

    def run(self) -> int:
        actions = [
            ("Ταυτότητα Manager, 3 εγκεκριμένοι Clients 1.0.0, εισαγωγή πακέτου 1.0.1 "
             f"({BIG_MB} MB)", self.s01),
            ("Εισαγωγή: λάθος κλειδί, αλλοιωμένο αρχείο και παλαιότερη έκδοση απορρίπτονται",
             self.s02),
            ("CHECK στη σύνδεση: ο Board1 κατεβάζει, επαληθεύει και αναφέρει «ready»", self.s03),
            ("Ανακοίνωση στη μέση της λήψης του Board2: παύση, συνέχεια μετά", self.s04),
            ("Κομμένη λήψη (proxy): συνέχιση από το offset, έτοιμη", self.s05),
            ("Client που ήταν κλειστός παίρνει την έκδοση όταν ανοίξει", self.s06),
            ("Επανεκκίνηση Manager: CHECK στην επανασύνδεση", self.s07),
            ("Νέο πακέτο ενώ τρέχουν: CHECK αμέσως, χωρίς επανασύνδεση", self.s08),
            ("Αυτόματες off: καμία λήψη· «Έναρξη τώρα» μόνο για έναν", self.s09),
            ("«Έναρξη τώρα» για όλους", self.s10),
            ("Όριο 5 ταυτόχρονων λήψεων: ο 6ος περιμένει (BUSY)", self.s11),
            ("Πακέτο αλλοιωμένο από τον Manager: ο Client το απορρίπτει και το αναφέρει",
             self.s12),
            ("Μη εγκεκριμένος υπολογιστής: καμία απάντηση στη θύρα ενημερώσεων", self.s13),
            ("Εφεδρικός δεν διανέμει· ως Κύριος ξανά ναι", self.s14),
            ("Κλείσιμο: καμία μισή λήψη, εγκατεστημένη έκδοση αμετάβλητη (εναλλαγή στο 5B2b)",
             self.s15),
        ]
        try:
            for index, (name, action) in enumerate(actions):
                self.step(name, action)
                if index < 3 and self.results[-1][1] != "PASS":
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


def main():
    root = Path(tempfile.mkdtemp(prefix="sch-update-test-"))
    print(f"Προσωρινή δοκιμή μόνο σε loopback: {root}", flush=True)
    try:
        return LiveTest(root).run()
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
