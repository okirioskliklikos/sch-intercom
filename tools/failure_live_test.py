"""4.3B: 16 πραγματικοί έλεγχοι σεναρίων σφαλμάτων (docs/phase4-3.md §4.2), μόνο loopback.

Τρέχουν τα πραγματικά ``director.main`` και ``client.main`` ως διεργασίες, σε
προσωρινό φάκελο και θύρες 517xx: η κανονική εγκατάσταση δεν αγγίζεται. Ο Manager
στέλνει χαμηλό τόνο δοκιμής αντί για μικρόφωνο (κρυφό ``--test-tone``).

* Ένα μικρό TCP proxy μέσα στο script «παγώνει» ή κόβει τη σύνδεση ενός Client
  (χαμένο δίκτυο χωρίς πραγματικό καλώδιο).
* Ψεύτικα beacons και ψεύτικος Manager με δικό του πιστοποιητικό στέλνονται από εδώ.
* Οι 70 συνδέσεις του ορίου γίνονται από το script με πραγματικό mTLS.
* Στα Windows (pycaw) ελέγχεται και η ένταση: ο Board1 τρέχει με
  ``--announcement-volume 60``. Αλλού οι έλεγχοι έντασης γράφουν SKIP.

    Windows:  powershell -ExecutionPolicy Bypass -File tools\\failure_live_test.ps1
    Linux:    tools/failure_live_test.sh
"""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import time
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from director.backup import BackupLocked, manager_lock  # noqa: E402
from director.tls_identity import create_director_identity  # noqa: E402
from shared.client_registry import SignedClientRegistry  # noqa: E402
from shared.control import heartbeat_sender  # noqa: E402
from shared.device_identity import create_device_identity  # noqa: E402
from shared.discovery import director_id, encode_beacon  # noqa: E402
from shared.identity import load_identity, receive_welcome, send_hello  # noqa: E402
from shared.transport_security import (DEFAULT_SERVER_NAME, client_context,  # noqa: E402
                                      server_context)
from tools.mtls_live_test import JOINED, Process  # noqa: E402
from tools.manager_role_live_test import TestMachine  # noqa: E402

TOTAL = 16
HOST = "127.0.0.1"
PORT, BROADCAST_PORT, ENROLL_PORT, WATCH_PORT = 51705, 51706, 51709, 51707
DISCOVERY_PORT = 51708        # beacons του Manager προς τον Board2 (loopback)
FAKE_DISCOVERY_PORT = 51718   # μόνο ψεύτικα/χειροκίνητα beacons, για τον Board4
PROXY_PORT = 51721
FAKE_TCP_PORT, FAKE_TLS_PORT = 51730, 51731
ROOM1, ROOM2, LOAD_ROOM = "C2627-Γ1", "C2627-Γ2", "C2627-Δ9"
MAX_CLIENTS = 70
ORIGINAL_VOLUME, ANNOUNCEMENT_VOLUME = 0.25, 60
LOST = ("Η σύνδεση διακόπηκε", "Δεν ήρθε έγκυρο μήνυμα")
LEFT = "αποσυνδέθηκε"


# ------------------------------------------------------------------ ένταση Windows
class Volume:
    """Κύρια ένταση Windows μέσω pycaw· None όπου δεν υπάρχει (Linux, χωρίς pycaw)."""

    def __init__(self):
        self.master = None
        self.reason = "όχι Windows"
        self.saved = None
        if sys.platform == "win32":
            try:
                from shared.windows_audio import PycawMaster
                self.master = PycawMaster()
                self.saved = self.master.get_volume()
            except Exception as exc:  # noqa: BLE001
                self.reason = f"χωρίς pycaw/συσκευή ({exc})"

    @property
    def available(self) -> bool:
        return self.master is not None

    def get(self) -> float | None:
        return None if self.master is None else round(self.master.get_volume(), 2)

    def set(self, value: float) -> None:
        if self.master is not None:
            self.master.set_volume(value)

    def wait(self, value: float, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if abs((self.get() or 0) - value) < 0.02:
                return True
            time.sleep(0.1)
        return abs((self.get() or 0) - value) < 0.02

    def restore_user_volume(self) -> None:
        if self.master is not None and self.saved is not None:
            self.master.set_volume(self.saved)


# ------------------------------------------------------------------------- proxy
class FreezableProxy:
    """TCP proxy: κανονικά προωθεί· «παγωμένο» δεν προωθεί τίποτα και δεν κλείνει."""

    def __init__(self, port: int, upstream: int):
        self.upstream = upstream
        self.frozen = threading.Event()
        self.closed = threading.Event()
        self.pairs: list[tuple[socket.socket, socket.socket]] = []
        self.lock = threading.Lock()
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
            if self.frozen.is_set():
                with self.lock:          # όπως χαμένο δίκτυο: καμία απάντηση
                    self.pairs.append((client, client))
                continue
            try:
                upstream = socket.create_connection((HOST, self.upstream), timeout=3)
            except OSError:
                client.close()
                continue
            with self.lock:
                self.pairs.append((client, upstream))
            for source, target in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pump, args=(source, target), daemon=True).start()

    def _pump(self, source, target):
        source.settimeout(0.2)
        while not self.closed.is_set():
            if self.frozen.is_set():
                time.sleep(0.05)         # τα δεδομένα μένουν στα buffers
                continue
            try:
                data = source.recv(65536)
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
        for item in (source, target):
            try:
                item.close()
            except OSError:
                pass

    def freeze(self):
        self.frozen.set()

    def thaw(self):
        """Το «καλώδιο» ξαναμπαίνει: οι παλιές συνδέσεις είναι ήδη νεκρές και στις δύο άκρες."""
        with self.lock:
            pairs, self.pairs = self.pairs, []
        for pair in pairs:
            for item in pair:
                try:
                    item.close()
                except OSError:
                    pass
        self.frozen.clear()

    def close(self):
        self.closed.set()
        self.thaw()
        self.server.close()


# ------------------------------------------------------------- ψεύτικος Manager
class FakeBeacons:
    """Ψεύτικα beacons προς μία θύρα discovery: άκυρα πακέτα και άλλο Director ID."""

    def __init__(self, port: int):
        self.port = port
        self.packets: list[bytes] = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None

    def send(self, packets: list[bytes]):
        with self.lock:
            self.packets = packets
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def _run(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            while not self.stop.wait(0.3):
                with self.lock:
                    packets = list(self.packets)
                for packet in packets:
                    try:
                        sender.sendto(packet, (HOST, self.port))
                    except OSError:
                        pass

    def close(self):
        self.stop.set()


class FakeManager:
    """Listener ψεύτικου Manager· μετρά συνδέσεις, επιτυχή TLS και bytes μετά το TLS."""

    def __init__(self, port: int, tls: ssl.SSLContext | None):
        self.tls = tls
        self.connections = self.handshakes = self.app_bytes = 0
        self.closed = threading.Event()
        self.server = socket.create_server((HOST, port))
        self.server.settimeout(0.2)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.closed.is_set():
            try:
                connection, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection):
        connection.settimeout(5)
        try:
            if self.tls is not None:
                connection = self.tls.wrap_socket(connection, server_side=True)
                self.handshakes += 1
                data = connection.recv(4096)
                self.app_bytes += len(data)
        except (OSError, ssl.SSLError):
            pass
        finally:
            connection.close()

    def close(self):
        self.closed.set()
        self.server.close()


# ------------------------------------------------------------------- συσκευές
class Board:
    """Πραγματικός Client (διεργασία), ήδη εγκεκριμένος στο μητρώο του Manager."""

    def __init__(self, base: Path, name: str, room: str, *, port: int | None = PORT,
                 discovery: int | None = None, volume: bool = False):
        self.name, self.room = name, room
        self.dir = base / f"client-{name}"
        self.port, self.discovery, self.volume = port, discovery, volume
        self.client_id = ""
        self.process: Process | None = None

    def approve(self, registry: SignedClientRegistry) -> None:
        self.client_id, _ = load_identity(self.dir / "client.json", room_override=self.room)
        cert, _, _ = create_device_identity(self.dir / "credentials", self.client_id)
        registry.approve(self.client_id, self.room, cert.read_bytes(), name=self.name)

    def args(self, trust: Path) -> list[str]:
        args = ["-m", "client.main", "--tls-ca", str(trust),
                "--identity-file", str(self.dir / "client.json"),
                "--client-credentials-dir", str(self.dir / "credentials"),
                "--client-hints-file", str(self.dir / "client-hints.json"),
                "--registry-seen-file", str(self.dir / "registry-seen.json"),
                "--volume-recovery-file", str(self.recovery),
                "--room", self.room, "--enrollment-port", str(ENROLL_PORT)]
        if self.discovery is not None:
            args += ["--discovery-port", str(self.discovery), "--port", str(PORT)]
        else:
            args += ["--host", HOST, "--port", str(self.port)]
        if self.volume:
            args += ["--announcement-volume", str(ANNOUNCEMENT_VOLUME)]
        return args

    @property
    def recovery(self) -> Path:
        return self.dir / "volume-recovery.json"

    def start(self, trust: Path) -> Process:
        self.stop()
        self.process = Process(self.args(trust), label=f"Client {self.name}")
        self.process.wait_for("Client ID:", timeout=30)
        return self.process

    def kill(self) -> None:
        if self.process is not None:
            self.process.proc.kill()      # SIGKILL / TerminateProcess: crash
            self.process.proc.wait(10)
            self.process = None

    def stop(self) -> None:
        if self.process is not None:
            self.process.stop()
            self.process = None


class LoadClient:
    """Μία από τις συνδέσεις του ορίου: mTLS, HELLO, heartbeat και άδειασμα ροής."""

    def __init__(self, base: Path, index: int, manager_cert: Path,
                 registry: SignedClientRegistry):
        self.client_id = str(uuid.uuid4())
        cert, key, password = create_device_identity(base / f"load-{index:02d}",
                                                     self.client_id)
        registry.approve(self.client_id, LOAD_ROOM, cert.read_bytes(), name=f"L{index:02d}")
        self.context = client_context(manager_cert, certfile=cert, keyfile=key,
                                      password=password)
        self.connection: ssl.SSLSocket | None = None
        self.stop = threading.Event()
        self.ended = threading.Event()

    def connect(self) -> None:
        self.stop = threading.Event()
        self.ended = threading.Event()
        plain = socket.create_connection((HOST, PORT), timeout=5)
        connection = self.context.wrap_socket(plain, server_hostname=DEFAULT_SERVER_NAME)
        connection.settimeout(5)
        send_hello(connection, self.client_id, LOAD_ROOM)
        receive_welcome(connection)
        self.connection = connection
        threading.Thread(target=heartbeat_sender, args=(connection, self.stop),
                         daemon=True).start()
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self):
        while not self.stop.is_set():
            try:
                if not self.connection.recv(65536):
                    break
            except socket.timeout:
                continue
            except OSError:
                break
        self.ended.set()

    def close(self) -> None:
        self.stop.set()
        if self.connection is not None:
            try:
                self.connection.close()
            except OSError:
                pass
            self.connection = None


class FailureMachine(TestMachine):
    def start(self) -> Process:
        self.director = Process([
            "-m", "director.main", *self.paths(), "--host", HOST,
            "--port", str(self.port), "--discovery-port", str(self.discovery),
            "--enrollment-port", str(self.enrollment),
            "--director-id-file", str(self.dir / "director.id"),
            "--watch-port", str(self.watch), "--extra-beacon-port", str(self.extra),
            "--start-check-seconds", "2", "--test-tone"],
            label="Manager", show=True)
        if self.director.wait_for("[Manager] Εξυπηρέτηση:", timeout=30) is None:
            raise RuntimeError("Δεν εκκίνησε ο Manager")
        if self.director.wait_for("Πηγή δοκιμής ενεργή", timeout=10) is None:
            raise RuntimeError("Ο Manager δεν ξεκίνησε τον τόνο δοκιμής")
        return self.director

    def kill(self) -> None:
        if self.director is not None:
            self.director.proc.kill()
            self.director.proc.wait(10)
            self.director = None


# --------------------------------------------------------------------- δοκιμή
class LiveTest:
    def __init__(self, root: Path):
        self.root = root
        self.m = FailureMachine(root, "manager", PORT, BROADCAST_PORT, ENROLL_PORT,
                                WATCH_PORT, DISCOVERY_PORT)
        self.b1 = Board(root, "Board1", ROOM1, volume=True)
        self.b2 = Board(root, "Board2", ROOM1, discovery=DISCOVERY_PORT)
        self.b3 = Board(root, "Board3", ROOM2, port=PROXY_PORT)
        self.b4 = Board(root, "Board4", ROOM2, discovery=FAKE_DISCOVERY_PORT)
        self.boards = [self.b1, self.b2, self.b3]
        self.volume = Volume()
        self.proxy: FreezableProxy | None = None
        self.fake_beacons = FakeBeacons(DISCOVERY_PORT)
        self.fake_tcp: FakeManager | None = None
        self.load: list[LoadClient] = []
        self.results: list[tuple[str, str, str]] = []
        self.volume_skips = 0

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

    def volume_check(self, value: float, timeout: float) -> tuple[bool, str]:
        """Έλεγχος έντασης Windows ή SKIP (δεν μετρά ως αποτυχία)."""
        if not self.volume.available:
            self.volume_skips += 1
            return True, f"ένταση: SKIP ({self.volume.reason})"
        ok = self.volume.wait(value, timeout)
        return ok, f"ένταση {self.volume.get():.2f} (αναμενόταν {value:.2f})"

    @property
    def director(self) -> Process:
        if self.m.director is None:
            raise RuntimeError("Ο Manager δεν τρέχει")
        return self.m.director

    def send(self, command: str) -> None:
        self.director.send(command)

    def joined(self, board: Board, since: int, timeout: float) -> bool:
        return self.director.wait_for(board.client_id, JOINED, since=since,
                                      timeout=timeout) is not None

    def report(self, board: Board, since: int, timeout: float = 7.5) -> tuple[str, int]:
        """Τελευταία αναφορά (ανά 5 s) του Client μετά το since: κατάσταση και blocks."""
        line = board.process.wait_for("[Client] κατάσταση:", since=since, timeout=timeout)
        if line is None:
            return "—", 0
        mode = "ανακοίνωση" if "κατάσταση: ανακοίνωση" in line else "αναμονή"
        return mode, int(re.search(r"blocks (\d+)", line).group(1))

    def hears(self, board: Board, since: int, timeout: float = 7.5) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = board.process.wait_for("κατάσταση: ανακοίνωση | blocks", since=since,
                                          timeout=max(0.1, deadline - time.monotonic()))
            if line is None:
                return False
            if int(re.search(r"blocks (\d+)", line).group(1)) > 0:
                return True
            since = board.process.lines.index(line, since) + 1
        return False

    def lost_after(self, board: Board, since: int, timeout: float) -> float | None:
        started = time.monotonic()
        for _ in range(int(timeout * 20)):
            if any(board.process.seen(text, since=since) for text in LOST):
                return time.monotonic() - started
            time.sleep(0.05)
        return None

    def no_loss(self, boards, since: int) -> bool:
        return not any(board.process.seen(text, since=since)
                       for board in boards for text in LOST)

    # ------------------------------------------------------------------ βήματα
    def s01(self):
        if not self.m.create_identity():
            return False, "δεν δημιουργήθηκε ταυτότητα Manager"
        registry = SignedClientRegistry(self.m.registry, self.m.cert, self.m.key)
        for board in (*self.boards, self.b4):
            board.approve(registry)
        # Οι συνδέσεις του ορίου (βήμα 14): έως 70 μαζί με τους Boards, + 1 επιπλέον.
        for index in range(MAX_CLIENTS):
            self.load.append(LoadClient(self.root, index, self.m.cert, registry))
        del registry
        self.volume.set(ORIGINAL_VOLUME)
        self.m.start()
        self.proxy = FreezableProxy(PROXY_PORT, PORT)
        mark = self.director.mark()
        for board in self.boards:
            board.start(self.m.cert)
        ok = all(self.joined(board, mark, 30) for board in self.boards)
        return ok, f"{len(self.load)} ταυτότητες ορίου έτοιμες"

    def s02(self):
        marks = [board.process.mark() for board in self.boards]
        self.send("all")
        heard = [self.hears(board, mark) for board, mark in zip(self.boards, marks)]
        vol_ok, vol = self.volume_check(ANNOUNCEMENT_VOLUME / 100, 5)
        self.send("none")
        restored, _ = self.volume_check(ORIGINAL_VOLUME, 6)
        return all(heard) and vol_ok and restored, f"ήχος {heard}, {vol}"

    def s03(self):
        marks = [board.process.mark() for board in self.boards]
        self.send(f"room {ROOM1}")
        heard = [self.hears(self.b1, marks[0]), self.hears(self.b2, marks[1])]
        time.sleep(1)
        lines = [line for line in self.b3.process.lines[marks[2]:] if "κατάσταση:" in line]
        self.send("none")
        silent = bool(lines) and all("κατάσταση: αναμονή" in line for line in lines)
        return all(heard) and silent, f"Γ1 {heard}, Γ2 αναφορές {len(lines)} σε αναμονή {silent}"

    def s04(self):
        mark_c, mark_d = self.b3.process.mark(), self.director.mark()
        started = time.monotonic()
        self.proxy.freeze()
        client = self.lost_after(self.b3, mark_c, 6)
        manager = self.director.wait_for(self.b3.client_id, LEFT, since=mark_d, timeout=10)
        manager_s = time.monotonic() - started
        ok = client is not None and client <= 3.5 and manager is not None and manager_s <= 7.5
        return ok, f"Client σε {client if client is None else round(client, 1)} s, " \
                   f"Manager σε {round(manager_s, 1) if manager else '—'} s"

    def s05(self):
        mark = self.director.mark()
        started = time.monotonic()
        self.proxy.thaw()
        ok = self.joined(self.b3, mark, 12)
        return ok, f"επανασύνδεση σε {round(time.monotonic() - started, 1)} s"

    def s06(self):
        # Ψεύτικα beacons στη θύρα discovery του Board2 σε όλη την επανεκκίνηση (βλ. 11).
        self.fake_tcp = FakeManager(FAKE_TCP_PORT, None)
        self.fake_beacons.send([
            b"not a beacon", b"\x00" * 600,
            encode_beacon(str(uuid.uuid4()), FAKE_TCP_PORT, inst=str(uuid.uuid4()))])
        marks = [board.process.mark() for board in self.boards]
        self.send("all")
        if not self.hears(self.b1, marks[0]):
            return False, "η ανακοίνωση δεν ξεκίνησε"
        vol_up, _ = self.volume_check(ANNOUNCEMENT_VOLUME / 100, 3)
        marks = [board.process.mark() for board in self.boards]
        self.m.kill()
        times = [self.lost_after(board, mark, 6) for board, mark in zip(self.boards, marks)]
        vol_ok, vol = self.volume_check(ORIGINAL_VOLUME, 4)
        ok = all(t is not None and t <= 3.5 for t in times) and vol_up and vol_ok
        return ok, f"απώλεια σε {[None if t is None else round(t, 1) for t in times]} s, {vol}"

    def s07(self):
        started = time.monotonic()
        self.m.start()                    # χωρίς παρέμβαση: lock ελεύθερο, εξυπηρέτηση ενεργή
        mark = 0
        joined = [self.joined(board, mark, 20) for board in self.boards]
        rejoin = round(time.monotonic() - started, 1)
        marks = [board.process.mark() for board in self.boards]
        self.send("all")
        heard = [self.hears(board, m) for board, m in zip(self.boards, marks)]
        self.send("none")
        serving = self.director.seen("[Manager] Εξυπηρέτηση: ενεργή")
        ok = serving and all(joined) and all(heard) and rejoin <= 20
        return ok, f"όλοι πίσω σε {rejoin} s, ήχος {heard}"

    def s08(self):
        time.sleep(2)                    # η προηγούμενη επαναφορά έχει ολοκληρωθεί
        marks = [board.process.mark() for board in self.boards]
        mark_d = self.director.mark()
        for _ in range(20):
            self.send("all")
            time.sleep(0.2)
            self.send("none")
            time.sleep(0.3)
        vol_ok, vol = self.volume_check(ORIGINAL_VOLUME, 5)
        time.sleep(1)
        stayed = all(self.no_loss([b], m) for b, m in zip(self.boards, marks))
        left = self.director.seen(LEFT, since=mark_d)
        return stayed and not left and vol_ok, f"καμία αποσύνδεση {stayed and not left}, {vol}"

    def s09(self):
        marks = [board.process.mark() for board in self.boards]
        self.send("all")
        if not self.hears(self.b1, marks[0]):
            return False, "η ανακοίνωση δεν ξεκίνησε"
        marks = [board.process.mark() for board in self.boards]
        mark_d = self.director.mark()
        started = time.monotonic()
        self.send(f"revoke {self.b2.client_id}")
        cut = self.director.wait_for(self.b2.client_id, LEFT, since=mark_d, timeout=3)
        cut_s = time.monotonic() - started
        client = self.lost_after(self.b2, marks[1], 3)
        time.sleep(3)
        others = self.no_loss([self.b1], marks[0]) and self.no_loss([self.b3], marks[2])
        others_left = any(self.director.seen(b.client_id, LEFT, since=mark_d)
                          for b in (self.b1, self.b3))
        self.send("none")
        ok = cut is not None and cut_s <= 1.5 and client is not None and others \
            and not others_left
        return ok, f"κόπηκε σε {round(cut_s, 2)} s, οι άλλοι συνέχισαν {others and not others_left}"

    def s10(self):
        mark_c, mark_d = self.b2.process.mark(), self.director.mark()
        time.sleep(14)
        attempts = sum("Σύνδεση στον Director" in line
                       for line in self.b2.process.lines[mark_c:])
        rejected = sum("Μη αποδεκτή δήλωση" in line or "Μη εγκεκριμένος" in line
                       for line in self.director.lines[mark_d:])
        joined = self.director.seen(self.b2.client_id, JOINED, since=mark_d)
        self.b2.stop()
        mark = self.b1.process.mark()
        self.send("all")
        alive = self.hears(self.b1, mark)
        self.send("none")
        ok = attempts >= 2 and not joined and alive and rejected <= attempts + 1
        return ok, (f"{attempts} προσπάθειες σε 14 s, {rejected} γραμμές απόρριψης στον "
                    f"Manager, εξυπηρέτηση {alive}")

    def s11(self):
        # Έλεγχος όσων έγιναν στα 6–7 με ψεύτικα beacons ενεργά.
        rejoined = self.director.seen(self.b2.client_id, JOINED)
        fake = self.fake_tcp.connections
        self.fake_beacons.close()
        return rejoined and fake == 0, (f"Board2 ξανά στον πραγματικό {rejoined}, "
                                        f"συνδέσεις στον ψεύτικο {fake}")

    def s12(self):
        fake_dir = self.root / "fake-manager"
        fake_dir.mkdir()
        cert, key = fake_dir / "fake.crt", fake_dir / "fake.key"
        create_director_identity(cert, key)
        context = server_context(cert, key)
        fake = FakeManager(FAKE_TLS_PORT, context)
        beacons = FakeBeacons(FAKE_DISCOVERY_PORT)
        try:
            beacons.send([encode_beacon(str(uuid.uuid4()), FAKE_TLS_PORT,
                                        inst=str(uuid.uuid4()))])
            self.b4.start(self.m.cert)
            deadline = time.monotonic() + 20
            while fake.connections == 0 and time.monotonic() < deadline:
                time.sleep(0.1)
            time.sleep(1)
            mark = self.director.mark()
            real = director_id(self.m.dir / "director.id")
            beacons.send([encode_beacon(real, PORT, inst=str(uuid.uuid4()))])
            joined = self.joined(self.b4, mark, 25)
            ok = fake.connections >= 1 and fake.handshakes == 0 and fake.app_bytes == 0 \
                and joined
            return ok, (f"ψεύτικος: {fake.connections} συνδέσεις, {fake.handshakes} TLS, "
                        f"{fake.app_bytes} bytes· μετά στον πραγματικό {joined}")
        finally:
            beacons.close()
            fake.close()
            self.b4.stop()

    def s13(self):
        silent = []
        mark = self.director.mark()
        try:
            for _ in range(25):
                silent.append(socket.create_connection((HOST, PORT), timeout=3))
            self.b3.stop()
            started = time.monotonic()
            self.b3.start(self.m.cert)
            joined = self.joined(self.b3, mark, 20)
            took = round(time.monotonic() - started, 1)
        finally:
            for item in silent:
                item.close()
        return joined, f"25 σιωπηλές συνδέσεις, ο Board3 μπήκε σε {took} s"

    def s14(self):
        mark = self.director.mark()
        running = sum(board.process is not None for board in self.boards)
        room = MAX_CLIENTS - running      # ο Board2 είναι ανακλημένος και σταματημένος
        connected = []
        for client in self.load[:room]:
            client.connect()
            connected.append(client)
        full = self.director.wait_for(f"σύνολο {MAX_CLIENTS}", since=mark, timeout=15)
        extra = self.load[room]
        extra.connect()                   # WELCOME και κλείσιμο (§5.2)
        refused = extra.ended.wait(3)
        limit = self.director.seen("Όριο Clients", since=mark)
        extra.close()
        mark = self.director.mark()
        connected[0].close()
        self.director.wait_for(connected[0].client_id, LEFT, since=mark, timeout=10)
        extra.connect()
        joined = self.director.wait_for(extra.client_id, JOINED, since=mark, timeout=10)
        stays = not extra.ended.wait(3)
        for client in [*connected, extra]:
            client.close()
        ok = full is not None and refused and limit and joined is not None and stays
        return ok, (f"{MAX_CLIENTS} συνδεδεμένοι {full is not None}, ο επιπλέον "
                    f"απορρίφθηκε {refused} και μπήκε μετά {joined is not None and stays}")

    def s15(self):
        time.sleep(2)
        mark = self.b1.process.mark()
        self.send("all")
        if not self.hears(self.b1, mark):
            return False, "η ανακοίνωση δεν ξεκίνησε"
        vol_up, _ = self.volume_check(ANNOUNCEMENT_VOLUME / 100, 3)
        self.b1.kill()                    # crash στη μέση ανακοίνωσης
        crashed_vol = self.volume.get()
        journal = self.b1.recovery.exists()
        self.send("none")
        mark = self.director.mark()
        self.b1.start(self.m.cert)
        joined = self.joined(self.b1, mark, 20)
        vol_ok, vol = self.volume_check(ORIGINAL_VOLUME, 10)
        if self.volume.available:
            journal_ok = journal and not self.b1.recovery.exists()
            detail = f"ένταση μετά το crash {crashed_vol}, μετά την εκκίνηση: {vol}"
        else:
            journal_ok = True
            detail = vol
        ok = joined and vol_up and vol_ok and journal_ok
        return ok, f"ξανά συνδεδεμένος {joined}, {detail}"

    def s16(self):
        self.b1.stop()
        self.b3.stop()
        process = self.director
        process.send("quit")
        try:
            code = process.proc.wait(15)
        except Exception:  # noqa: BLE001
            code = None
        self.m.director = None
        try:
            with manager_lock(self.m.dir):
                free = True
        except BackupLocked:
            free = False
        leftovers = [p.name for p in self.m.dir.glob("*.tmp")] + \
            [p.name for p in self.m.dir.glob("restore-journal*")]
        return code == 0 and free and not leftovers, \
            f"έξοδος {code}, lock ελεύθερο {free}, υπόλοιπα {leftovers}"

    # ------------------------------------------------------------------ εκτέλεση
    def close(self):
        for board in (*self.boards, self.b4):
            board.stop()
        for client in self.load:
            client.close()
        self.fake_beacons.close()
        if self.fake_tcp is not None:
            self.fake_tcp.close()
        if self.proxy is not None:
            self.proxy.close()
        if self.m.director is not None:
            self.m.stop()
        self.volume.restore_user_volume()

    def run(self) -> int:
        actions = [
            ("Manager και 3 Clients (2 στη Γ1, 1 στη Γ2) συνδέονται", self.s01),
            ("Ανακοίνωση σε όλους: λαμβάνουν και οι 3", self.s02),
            ("Ανακοίνωση μόνο στη Γ1: η Γ2 δεν λαμβάνει", self.s03),
            ("Πάγωμα δικτύου Client: απώλεια ≤3,5 s, offline στον Manager ≤7,5 s", self.s04),
            ("Ξεπάγωμα: ο Client επανασυνδέεται", self.s05),
            ("Kill του Manager στη μέση ανακοίνωσης: όλοι το καταλαβαίνουν ≤3,5 s", self.s06),
            ("Επανεκκίνηση Manager χωρίς παρέμβαση: όλοι πίσω, ήχος", self.s07),
            ("20 γρήγορες ανακοινώσεις: καμία αποσύνδεση, ένταση όπως πριν", self.s08),
            ("Ανάκληση Client στη μέση ανακοίνωσης: κόβεται, οι άλλοι συνεχίζουν", self.s09),
            ("Ο ανακλημένος ξαναπροσπαθεί και απορρίπτεται, εξυπηρέτηση κανονική", self.s10),
            ("Ψεύτικα beacons στην επανεκκίνηση: ο Client γύρισε στον πραγματικό", self.s11),
            ("Ψεύτικος Manager με άλλο πιστοποιητικό: κανένα HELLO, μετά στον πραγματικό",
             self.s12),
            ("25 σιωπηλές συνδέσεις TCP: πραγματικός Client συνδέεται", self.s13),
            ("Όριο 70: ο επιπλέον απορρίπτεται και μπαίνει όταν φύγει ένας", self.s14),
            ("Kill του Client στη μέση ανακοίνωσης και επανεκκίνηση", self.s15),
            ("Κανονικό κλείσιμο Manager: lock ελεύθερο, κανένα υπόλοιπο", self.s16),
        ]
        try:
            for name, action in actions:
                self.step(name, action)
        finally:
            self.close()
        passed = sum(state == "PASS" for _, state, _ in self.results)
        print("\n" + "\n".join(
            f"{state} {i:02d}: {name}" + (f" — {detail}" if detail else "")
            for i, (name, state, detail) in enumerate(self.results, 1)), flush=True)
        skips = (f" · έλεγχοι έντασης Windows: {self.volume_skips} SKIP "
                 f"({self.volume.reason})" if self.volume_skips else "")
        print(f"ΣΥΝΟΛΟ: {passed}/{TOTAL} PASS{skips}", flush=True)
        return 0 if passed == TOTAL else 1


def main():
    root = Path(tempfile.mkdtemp(prefix="sch-failure-test-"))
    print(f"Προσωρινή δοκιμή μόνο σε loopback: {root}", flush=True)
    try:
        return LiveTest(root).run()
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
