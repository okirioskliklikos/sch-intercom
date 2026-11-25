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
    senders = BroadcastSenders(udp_port)
    try:
        while not stop.is_set():
            try:
                version = registry_version() if callable(registry_version) else (
                    registry_version if registry_version is not None else 1)
                packet = encode_beacon(identifier, tcp_port, school=school or
                                       "00000000-0000-0000-0000-000000000000",
                                       inst=inst, rv=version, host=host)
                senders.send(packet)
                for target in extra_ports:
                    senders.default.sendto(packet, ("127.0.0.1", target))
            except (OSError, ValueError) as exc:
                report(f"[Discovery] Αποτυχία broadcast: {exc}")
            stop.wait(interval)
    finally:
        senders.close()


#: Κάθε πόσο ξαναδιαβάζονται οι διευθύνσεις του υπολογιστή (DHCP, Wi-Fi που αλλάζει).
INTERFACE_REFRESH_SECONDS = 30.0


def local_ipv4_addresses() -> list[str]:
    """IPv4 όλων των καρτών δικτύου (εκτός loopback), όπως τις δίνει το σύστημα."""
    try:
        items = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    return sorted({item[4][0] for item in items if not item[4][0].startswith("127.")})


class BroadcastSenders:
    """Broadcast από κάθε κάρτα δικτύου.

    Στα Windows το 255.255.255.255 βγαίνει μόνο από μία κάρτα (συνήθως αυτή με τη
    χαμηλότερη μετρική, π.χ. Wi-Fi ή εικονική κάρτα Hyper-V/WSL), οπότε ένας Manager με
    καλώδιο μπορεί να μη φαίνεται στους Clients (4.4, Εκτέλεση 3). Εκεί στέλνουμε από socket
    δεμένο σε κάθε IPv4 του υπολογιστή. Στο Linux το broadcast ακολουθεί τον πίνακα
    δρομολόγησης όπως και πριν, άρα μένει ένα socket.
    """

    def __init__(self, udp_port: int, *, per_interface: bool | None = None,
                 addresses=local_ipv4_addresses, clock=time.monotonic) -> None:
        self.udp_port = udp_port
        self.per_interface = os.name == "nt" if per_interface is None else per_interface
        self._addresses = addresses
        self._clock = clock
        self.default = self._open()
        self.bound: dict[str, socket.socket] = {}
        self._refreshed = None

    @staticmethod
    def _open(address: str | None = None) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if address is not None:
            try:
                sock.bind((address, 0))
            except OSError:
                sock.close()
                raise
        return sock

    def refresh(self) -> None:
        now = self._clock()
        if self._refreshed is not None and now - self._refreshed < INTERFACE_REFRESH_SECONDS:
            return
        self._refreshed = now
        wanted = set(self._addresses()) if self.per_interface else set()
        for address in set(self.bound) - wanted:
            self.bound.pop(address).close()
        for address in wanted - set(self.bound):
            try:
                self.bound[address] = self._open(address)
            except OSError:
                continue

    def send(self, packet: bytes) -> int:
        """Πλήθος καρτών από τις οποίες στάλθηκε· σφάλμα μόνο αν απέτυχαν όλες."""
        self.refresh()
        sent, error = 0, None
        for sock in list(self.bound.values()) or [self.default]:
            try:
                sock.sendto(packet, ("255.255.255.255", self.udp_port))
                sent += 1
            except OSError as exc:
                error = exc
        if not sent and error is not None:
            raise error
        return sent

    def close(self) -> None:
        for sock in [self.default, *self.bound.values()]:
            sock.close()
        self.bound.clear()


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
                    # Windows: πακέτο μεγαλύτερο από το buffer δίνει WSAEMSGSIZE (10040)
                    # αντί να κοπεί, και ένα ICMP «unreachable» δίνει WSAECONNRESET.
                    # Ένα τέτοιο πακέτο από οποιονδήποτε στο LAN δεν πρέπει να
                    # σταματά για πάντα την παρακολούθηση άλλων Managers.
                    if self._stop.wait(0.05):
                        break
                    continue
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
            except OSError:
                # Windows: υπερμεγέθες πακέτο (WSAEMSGSIZE) ή WSAECONNRESET. Αγνοείται
                # όπως κάθε άκυρο beacon· αλλιώς ένα πακέτο εμπόδιζε τη σύνδεση.
                continue
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


#: Μέγιστο πλήθος υποψηφίων που δοκιμάζει ο Client με TLS (4.3C1).
MAX_CANDIDATES = 8


def discover_candidates(*, udp_port: int = DISCOVERY_PORT, timeout: float = DISCOVERY_WINDOW,
                        preferred_id: str | None = None,
                        confirm: float = CONFIRM_WINDOW) -> list[tuple[str, int, str]]:
    """Όλοι οι πιθανοί Managers (IP πηγής, TCP port, ID), χωρίς να εμπιστεύεται κανέναν.

    4.3C1: ένα ψεύτικο beacon δεν πρέπει να εμποδίζει τη σύνδεση. Ο Client δοκιμάζει
    τους υποψηφίους με TLS και κρατά μόνο αυτόν που επαληθεύεται. Αν έχει βρεθεί ο
    προτιμώμενος Manager, επιστρέφονται μόνο οι διευθύνσεις του (μετά το confirm)·
    αν δεν εμφανιστεί σε όλο το παράθυρο, επιστρέφονται όλοι (π.χ. ο Manager μεταφέρθηκε
    σε άλλον υπολογιστή με νέο ID). Το πλήθος περιορίζεται σε ``MAX_CANDIDATES``.
    """
    found: dict[tuple[str, int, str], None] = {}
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
        early = start + min(confirm, timeout)
        while True:
            now = time.monotonic()
            preferred_seen = preferred_id is not None and any(
                identifier == preferred_id for _, _, identifier in found)
            if preferred_seen and now >= early:
                break
            remaining = deadline - now
            if remaining <= 0:
                break
            listener.settimeout(remaining)
            try:
                packet, sender = listener.recvfrom(MAX_BEACON_BYTES + 1)
            except socket.timeout:
                break
            except OSError:
                continue   # Windows: υπερμεγέθες πακέτο/ICMP· όπως κάθε άκυρο beacon
            try:
                identifier, tcp_port = decode_beacon(packet)
            except ValueError:
                continue
            key = (sender[0], tcp_port, identifier)
            if key not in found and len(found) < 4 * MAX_CANDIDATES:
                found[key] = None
    candidates = list(found)
    if preferred_id is not None and any(item[2] == preferred_id for item in candidates):
        candidates = [item for item in candidates if item[2] == preferred_id]
    return candidates[:MAX_CANDIDATES]
