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


def send_hello(connection: socket.socket, client_id: str, room: str) -> None:
    validate_identity(client_id, room)
    payload = json.dumps({"client_id": client_id, "room": room}, ensure_ascii=False).encode("utf-8")
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
    if not isinstance(data, dict) or set(data) != {"client_id", "room"}:
        raise ValueError("Μη έγκυρη δήλωση Client")
    return validate_identity(data["client_id"], data["room"])


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
