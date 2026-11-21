"""Εργαστηριακή δήλωση ταυτότητας Client πριν από την PCM ροή.

Η δήλωση δεν πιστοποιείται κρυπτογραφικά: ΔΕΝ είναι μηχανισμός έγκρισης.
"""

import json
import os
from pathlib import Path
import re
import socket
import struct
import uuid

from shared.audio import read_exact

HELLO_MAGIC = b"SICH"
MAX_HELLO_BYTES = 512
# Έκδοση πρωτοκόλλου σύνδεσης Director <-> Client. Αυξάνεται σε κάθε ασύμβατη
# αλλαγή σε οποιαδήποτε κατεύθυνση. 1 = πριν από το heartbeat (χωρίς πεδίο),
# 2 = heartbeat Client -> Director και απάντηση WELCOME.
# 3 = KEEPALIVE/STREAM_STOP αντί για σιωπηλά PCM σε μη επιλεγμένους Clients.
# 4 = TLS υποχρεωτικό πριν από HELLO/WELCOME και τη ροή ήχου.
# 5 = πιστοποιημένο mTLS Client και υπογεγραμμένο registry πριν από PCM.
CONNECTION_PROTOCOL = 5
WELCOME_MAGIC = b"SIWK"
WELCOME = struct.Struct("!4sBB")
WELCOME_OK = 0
WELCOME_INCOMPATIBLE = 1
UPDATE_HINT = "ενημερώστε μαζί Director και Client"


class ProtocolMismatch(ValueError):
    """Ο Client και ο Director έχουν ασύμβατες εκδόσεις πρωτοκόλλου."""

    def __init__(self, message: str, *, client_version: int | None = None,
                 director_version: int | None = None):
        super().__init__(message)
        self.client_version = client_version
        self.director_version = director_version
ROOM_PATTERN = re.compile(r"^[^\x00-\x1f\x7f]{1,64}$")


def validate_identity(client_id: str, room: str) -> tuple[str, str]:
    if not isinstance(client_id, str) or not isinstance(room, str):
        raise ValueError("Μη έγκυρος τύπος ταυτότητας Client")
    try:
        if str(uuid.UUID(client_id)) != client_id:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise ValueError("Μη έγκυρο UUID Client") from exc
    if not ROOM_PATTERN.fullmatch(room) or room.strip() != room:
        raise ValueError("Μη έγκυρη αίθουσα (1-64 χαρακτήρες)")
    return client_id, room


def send_hello(connection: socket.socket, client_id: str, room: str, *,
               protocol: int = CONNECTION_PROTOCOL) -> None:
    validate_identity(client_id, room)
    payload = json.dumps({"client_id": client_id, "room": room, "protocol": protocol},
                         ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_HELLO_BYTES:
        raise ValueError("Πολύ μεγάλη δήλωση Client")
    connection.sendall(HELLO_MAGIC + struct.pack("!H", len(payload)) + payload)


def receive_hello(connection: socket.socket) -> tuple[str, str]:
    if read_exact(connection, 4) != HELLO_MAGIC:
        raise ValueError("Μη υποστηριζόμενη δήλωση Client: ενημερώστε Director και Client")
    length = struct.unpack("!H", read_exact(connection, 2))[0]
    if not 1 <= length <= MAX_HELLO_BYTES:
        raise ValueError("Μη έγκυρο μέγεθος δήλωσης")
    try:
        data = json.loads(read_exact(connection, length).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Μη έγκυρα δεδομένα Client") from exc
    if isinstance(data, dict) and set(data) == {"client_id", "room"}:
        raise ProtocolMismatch(
            f"Παλιός Client χωρίς heartbeat/έκδοση πρωτοκόλλου (Director v{CONNECTION_PROTOCOL}): "
            f"{UPDATE_HINT}", director_version=CONNECTION_PROTOCOL)
    if not isinstance(data, dict) or set(data) != {"client_id", "room", "protocol"}:
        raise ValueError("Μη έγκυρη δήλωση Client")
    identity = validate_identity(data["client_id"], data["room"])
    version = data["protocol"]
    if type(version) is not int or not 1 <= version <= 255:
        raise ValueError("Μη έγκυρη έκδοση πρωτοκόλλου Client")
    if version != CONNECTION_PROTOCOL:
        raise ProtocolMismatch(
            f"Ασύμβατη έκδοση: Client v{version}, Director v{CONNECTION_PROTOCOL}: {UPDATE_HINT}",
            client_version=version, director_version=CONNECTION_PROTOCOL)
    return identity


def send_welcome(connection: socket.socket, *, accepted: bool) -> None:
    """Απάντηση Director στη δήλωση: αποδοχή ή ασύμβατη έκδοση."""
    status = WELCOME_OK if accepted else WELCOME_INCOMPATIBLE
    connection.sendall(WELCOME.pack(WELCOME_MAGIC, CONNECTION_PROTOCOL, status))


def receive_welcome(connection: socket.socket) -> None:
    """Ο Client περιμένει την επιβεβαίωση του Director πριν από τον ήχο."""
    try:
        packet = read_exact(connection, WELCOME.size)
    except EOFError as exc:
        raise EOFError("Ο Director έκλεισε τη σύνδεση χωρίς επιβεβαίωση· πιθανώς "
                       f"παλαιότερη έκδοση Director: {UPDATE_HINT}") from exc
    magic, version, status = WELCOME.unpack(packet)
    if magic != WELCOME_MAGIC:
        raise ProtocolMismatch(f"Άγνωστη απάντηση Director: {UPDATE_HINT}",
                               client_version=CONNECTION_PROTOCOL)
    if status != WELCOME_OK or version != CONNECTION_PROTOCOL:
        raise ProtocolMismatch(
            f"Ασύμβατη έκδοση: Client v{CONNECTION_PROTOCOL}, Director v{version}: {UPDATE_HINT}",
            client_version=CONNECTION_PROTOCOL, director_version=version)


def load_identity(path: Path, *, room_override: str | None = None) -> tuple[str, str]:
    """Διατηρεί UUID και αίθουσα μετά από επανεκκίνηση/επανασύνδεση.

    Το αρχείο δημιουργείται τοπικά με μοναδικό UUID, χωρίς στοιχεία μαθητών.
    """
    path = Path(path).expanduser()
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        client_id, room = validate_identity(data["client_id"], data["room"])
    else:
        client_id, room = str(uuid.uuid4()), "Unassigned"
    if room_override is not None:
        room = room_override
    validate_identity(client_id, room)
    if not path.exists() or room_override is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as output:
                json.dump({"client_id": client_id, "room": room}, output, ensure_ascii=False, indent=2)
                output.write("\n")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return client_id, room
