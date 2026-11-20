"""UDP beacon discovery για εργαστηριακό LAN (χωρίς πιστοποίηση)."""

import json
import os
from pathlib import Path
import socket
import time
import uuid

DISCOVERY_PORT = 50556
ANNOUNCE_INTERVAL = 1.0
DISCOVERY_WINDOW = 2.5
# Με γνωστό Director ο Client σταματά νωρίτερα, αλλά μόνο αφού περάσει λίγο
# παραπάνω από ένα διάστημα beacon: έτσι ο πραγματικός Director προλαβαίνει να
# ακουστεί και ένα αντιγραμμένο ID από άλλη διεύθυνση εντοπίζεται.
CONFIRM_WINDOW = ANNOUNCE_INTERVAL + 0.2
MAGIC = b"SID1:"
MAX_BEACON_BYTES = 128


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


def encode_beacon(identifier: str, tcp_port: int) -> bytes:
    if str(uuid.UUID(identifier)) != identifier or not 1 <= tcp_port <= 65535:
        raise ValueError("Μη έγκυρα στοιχεία Director")
    packet = MAGIC + json.dumps({"id": identifier, "port": tcp_port},
                                separators=(",", ":")).encode("ascii")
    if len(packet) > MAX_BEACON_BYTES:
        raise ValueError("Υπερβολικά μεγάλο beacon")
    return packet


def decode_beacon(packet: bytes) -> tuple[str, int]:
    if not packet.startswith(MAGIC) or len(packet) > MAX_BEACON_BYTES:
        raise ValueError("Άγνωστο UDP beacon")
    try:
        payload = json.loads(packet[len(MAGIC):].decode("ascii"))
        if not isinstance(payload, dict) or set(payload) != {"id", "port"}:
            raise ValueError("Μη έγκυρο beacon")
        identifier, port = payload["id"], payload["port"]
        if not isinstance(identifier, str) or str(uuid.UUID(identifier)) != identifier:
            raise ValueError("Μη έγκυρο Director ID")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Μη έγκυρη θύρα TCP")
        return identifier, port
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Μη έγκυρο UDP beacon") from exc


def announce(stop, identifier: str, tcp_port: int, *, udp_port: int = DISCOVERY_PORT,
             interval: float = ANNOUNCE_INTERVAL, report=print) -> None:
    """Περιοδικό broadcast, ανεξάρτητο από το TCP και το μικρόφωνο."""
    packet = encode_beacon(identifier, tcp_port)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while not stop.is_set():
            try:
                sender.sendto(packet, ("255.255.255.255", udp_port))
            except OSError as exc:
                report(f"[Discovery] Αποτυχία broadcast: {exc}")
            stop.wait(interval)


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
