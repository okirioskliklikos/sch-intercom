"""UDP beacon v2 και παρακολούθηση άλλων Managers (χωρίς πιστοποίηση)."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import socket
import threading
import time
import uuid

DISCOVERY_PORT = 50556
ANNOUNCE_INTERVAL = 1.0
DISCOVERY_WINDOW = 2.5
CONFIRM_WINDOW = ANNOUNCE_INTERVAL + 0.2
MAGIC = b"SID2:"
MAX_BEACON_BYTES = 512
BEACON_EXPIRY_SECONDS = 5.0


def director_id(path: Path) -> str:
    """Σταθερό UUID Director, ώστε να αναγνωρίζεται μετά από επανεκκίνηση."""
    path = Path(path).expanduser()
    if path.exists():
        value = path.read_text(encoding="ascii").strip()
        if str(uuid.UUID(value)) != value:
            raise ValueError("Μη έγκυρο αποθηκευμένο Director ID")
        return value
    value = str(uuid.uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(value + "\n", encoding="ascii")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return value


@dataclass(frozen=True)
class BeaconInfo:
    id: str
    port: int
    school: str
    inst: str
    rv: int
    host: str


@dataclass(frozen=True)
class SeenManager:
    info: BeaconInfo
    ip: str
    last_seen: float


def _uuid(text: str) -> bool:
    try:
        return isinstance(text, str) and str(uuid.UUID(text)) == text
    except (ValueError, AttributeError, TypeError):
        return False


def encode_beacon(identifier: str, tcp_port: int, *,
                  school: str = "00000000-0000-0000-0000-000000000000",
                  inst: str | None = None, rv: int = 1,
                  host: str | None = None) -> bytes:
    """Σύνθεση κάθε beacon με την τρέχουσα έκδοση (όχι αποθηκευμένη στην έναρξη)."""
    if inst is None:
        inst = str(uuid.uuid4())
    if host is None:
        host = socket.gethostname()[:63]
    values = {"id": identifier, "port": tcp_port, "school": school,
              "inst": inst, "rv": rv, "host": host}
    if (not _uuid(identifier) or not _uuid(school) or not _uuid(inst) or
            type(tcp_port) is not int or not 1 <= tcp_port <= 65535 or
            type(rv) is not int or rv < 1 or
            not isinstance(host, str) or not 1 <= len(host) <= 63 or
            any(ord(char) < 32 or ord(char) == 127 for char in host)):
        raise ValueError("Μη έγκυρα στοιχεία beacon")
    packet = MAGIC + json.dumps(values, ensure_ascii=True,
                                separators=(",", ":")).encode("ascii")
    if len(packet) > MAX_BEACON_BYTES:
        raise ValueError("Υπερβολικά μεγάλο beacon")
    return packet


def decode_beacon_info(packet: bytes) -> BeaconInfo:
    if (not isinstance(packet, bytes) or not packet.startswith(MAGIC) or
            len(packet) > MAX_BEACON_BYTES):
        raise ValueError("Άγνωστο UDP beacon")
    try:
        payload = json.loads(packet[len(MAGIC):].decode("ascii"))
        if not isinstance(payload, dict) or set(payload) != {
                "id", "port", "school", "inst", "rv", "host"}:
            raise ValueError("Μη έγκυρα πεδία beacon")
        return BeaconInfo(**_validated(payload))
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError,
            AttributeError) as exc:
        raise ValueError("Μη έγκυρο UDP beacon") from exc


def _validated(payload: dict) -> dict:
    # Reuse precisely the same field/size validation as the sender.
    packet = encode_beacon(payload["id"], payload["port"],
                           school=payload["school"], inst=payload["inst"],
                           rv=payload["rv"], host=payload["host"])
    if len(packet) > MAX_BEACON_BYTES:
        raise ValueError("Oversize beacon")
    return payload


def decode_beacon(packet: bytes) -> tuple[str, int]:
    item = decode_beacon_info(packet)
    return item.id, item.port


def announce(stop, identifier: str, tcp_port: int, *,
             udp_port: int = DISCOVERY_PORT, interval: float = ANNOUNCE_INTERVAL,
             report=print, school: str | None = None, inst: str | None = None,
             registry_version=None, host: str | None = None,
             extra_ports: tuple[int, ...] = ()) -> None:
    """Broadcast και προαιρετικά loopback test ports, κάθε φορά με live rv."""
    if inst is None:
        inst = str(uuid.uuid4())
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while not stop.is_set():
            try:
                version = registry_version() if callable(registry_version) else (
                    registry_version if registry_version is not None else 1)
                packet = encode_beacon(identifier, tcp_port, school=school or
                                       "00000000-0000-0000-0000-000000000000",
                                       inst=inst, rv=version, host=host)
                sender.sendto(packet, ("255.255.255.255", udp_port))
                for target in extra_ports:
                    sender.sendto(packet, ("127.0.0.1", target))
            except (OSError, ValueError) as exc:
                report(f"[Discovery] Αποτυχία broadcast: {exc}")
            stop.wait(interval)


class BeaconWatcher:
    """Παρακολουθεί αναξιόπιστα beacons μόνο για προειδοποιήσεις σύγκρουσης."""

    def __init__(self, school: str, inst: str, *, port: int = DISCOVERY_PORT,
                 clock=time.monotonic):
        if not _uuid(school) or not _uuid(inst):
            raise ValueError("Invalid Manager identity")
        self.school, self.inst, self.port = school, inst, port
        self.clock = clock
        self.started_at: float | None = None
        self._seen: dict[str, SeenManager] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def receive(self, packet: bytes, ip: str, *, now: float | None = None) -> None:
        try:
            info = decode_beacon_info(packet)
        except ValueError:
            return
        if info.school != self.school or info.inst == self.inst:
            return
        instant = self.clock() if now is None else now
        with self._lock:
            self._seen[info.inst] = SeenManager(info, ip, instant)

    def others(self, *, now: float | None = None) -> list[SeenManager]:
        instant = self.clock() if now is None else now
        with self._lock:
            self._seen = {
                inst: found for inst, found in self._seen.items()
                if instant - found.last_seen < BEACON_EXPIRY_SECONDS
            }
            return list(self._seen.values())

    def newer(self, version: int) -> list[SeenManager]:
        return [item for item in self.others() if item.info.rv > version]

    def run(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            listener.bind(("", self.port))
            listener.settimeout(0.2)
            self.started_at = self.clock()
            while not self._stop.is_set():
                try:
                    packet, sender = listener.recvfrom(MAX_BEACON_BYTES + 1)
                except socket.timeout:
                    continue
                except OSError:
                    break
                self.receive(packet, sender[0])

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, daemon=True,
                                        name="manager-beacon-watcher")
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

def discover(*, udp_port: int = DISCOVERY_PORT, timeout: float = DISCOVERY_WINDOW,
             preferred_id: str | None = None,
             confirm: float = CONFIRM_WINDOW) -> tuple[str, int, str] | None:
    """Επιστρέφει IP πηγής, TCP port και UUID ή None.

    Δεν εμπιστεύεται IP μέσα στο beacon. Με πολλούς Directors και χωρίς
    προτιμώμενο ID δεν επιλέγει αυθαίρετα έναν. Με προτιμώμενο ID επιστρέφει
    μόλις περάσει το confirm (αντί για όλο το timeout), αν έχει βρεθεί.
    Αν το ίδιο ID έρθει από δύο διευθύνσεις (IP ή TCP port), δεν επιλέγει καμία.
    """
    candidates: dict[str, set[tuple[str, int]]] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        listener.bind(("", udp_port))
        start = time.monotonic()
        deadline = start + timeout
        early = start + min(confirm, timeout) if preferred_id is not None else deadline
        while True:
            now = time.monotonic()
            if candidates and now >= early:
                break
            remaining = (early if candidates else deadline) - now
            if remaining <= 0:
                break
            listener.settimeout(remaining)
            try:
                packet, sender = listener.recvfrom(MAX_BEACON_BYTES + 1)
            except socket.timeout:
                break
            try:
                identifier, tcp_port = decode_beacon(packet)
            except ValueError:
                continue
            if preferred_id is not None and identifier != preferred_id:
                continue
            candidates.setdefault(identifier, set()).add((sender[0], tcp_port))
    if len(candidates) > 1:
        raise ValueError("Βρέθηκαν πολλοί Directors: ορίστε χειροκίνητα --host")
    if not candidates:
        return None
    identifier, addresses = next(iter(candidates.items()))
    if len(addresses) > 1:
        found = ", ".join(f"{host}:{port}" for host, port in sorted(addresses))
        raise ValueError(f"Το ID Director εμφανίστηκε από πολλές διευθύνσεις ({found})· "
                         "πιθανή πλαστοπροσωπία, δεν γίνεται σύνδεση")
    host, tcp_port = next(iter(addresses))
    return host, tcp_port, identifier
