"""Πρωτόκολλο ενημερώσεων Client (Φάση 5B2, docs/phase5.md §5.3).

Ξεχωριστή θύρα TCP 50560 με mTLS (ίδια διαπιστευτήρια Client με τον ήχο). Ανεξάρτητο από
το CONNECTION_PROTOCOL του ήχου, ώστε να ενημερώνεται και Client με παλιό πρωτόκολλο ήχου.

Κάθε μήνυμα: 4 bytes μήκος (big endian) + κανονικό JSON (UTF-8, ταξινομημένα κλειδιά).
Μετά από ``data`` ακολουθούν ακριβώς ``size`` bytes του πακέτου.

    C → M  check   client_id, app, updater, protocol, failed
    M → C  offer   version, size, sha256       |  none
    C → M  get     version, offset
    M → C  data    offset, size + bytes        |  busy retry  |  error reason
    C → M  report  state, version, detail, progress
    M → C  ok
"""
from __future__ import annotations

import json
import random
import re
import struct

from shared.version import parse_version

UPDATE_PORT = 50560
UPDATE_PROTOCOL = 1
MAX_MESSAGE = 4096
#: Bytes ανά «data»: ο Client ζητά το επόμενο κομμάτι μόνο όταν δεν παίζει ανακοίνωση.
CHUNK_BYTES = 256 * 1024
#: Χρονικό όριο αδράνειας σύνδεσης (§5.3).
IDLE_TIMEOUT = 30.0
MAX_DOWNLOADS = 5
BUSY_RETRY = 30
BUSY_JITTER = 10
#: Καταστάσεις που αναφέρει ο Client (REPORT) και κρατά ο Manager (updates-status.json).
STATES = ("current", "available", "downloading", "waiting", "ready", "installing",
          "installed", "failed")
_HASH = re.compile(r"^[0-9a-f]{64}$")


class UpdateProtocolError(ValueError):
    """Άκυρο μήνυμα ενημέρωσης· η σύνδεση κλείνει."""


def busy_retry() -> int:
    """30 ± 10 s, ώστε οι Clients που περιμένουν να μην ξαναέρθουν όλοι μαζί."""
    return BUSY_RETRY + random.randint(-BUSY_JITTER, BUSY_JITTER)


def _encode(message: dict) -> bytes:
    return json.dumps(message, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def send_message(stream, message: dict, payload: bytes = b"") -> None:
    encoded = _encode(message)
    if len(encoded) > MAX_MESSAGE:
        raise UpdateProtocolError("Πολύ μεγάλο μήνυμα ενημέρωσης")
    stream.sendall(struct.pack("!I", len(encoded)) + encoded + payload)


def read_exact(stream, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        block = stream.recv(min(size - len(data), 1024 * 1024))
        if not block:
            raise EOFError("Η σύνδεση ενημέρωσης έκλεισε")
        data.extend(block)
    return bytes(data)


def read_message(stream) -> dict:
    length = struct.unpack("!I", read_exact(stream, 4))[0]
    if not 1 <= length <= MAX_MESSAGE:
        raise UpdateProtocolError("Άκυρο μήκος μηνύματος ενημέρωσης")
    try:
        message = json.loads(read_exact(stream, length).decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise UpdateProtocolError("Άκυρο JSON ενημέρωσης") from exc
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise UpdateProtocolError("Άκυρο μήνυμα ενημέρωσης")
    return message


# -- έλεγχος πεδίων ---------------------------------------------------------------
def _version(value) -> str:
    try:
        parse_version(value)
    except ValueError as exc:
        raise UpdateProtocolError(str(exc)) from exc
    return value


def _int(value, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise UpdateProtocolError("Άκυρος αριθμός σε μήνυμα ενημέρωσης")
    return value


def _text(value, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise UpdateProtocolError("Άκυρο κείμενο σε μήνυμα ενημέρωσης")
    return value


def check_message(client_id: str, app: str, *, updater: int, protocol: int,
                  failed=()) -> dict:
    return {"type": "check", "v": UPDATE_PROTOCOL, "client_id": client_id, "app": app,
            "updater": updater, "protocol": protocol, "failed": list(failed)[:20]}


def parse_check(message: dict) -> dict:
    if message.get("type") != "check":
        raise UpdateProtocolError("Αναμενόταν check")
    failed = message.get("failed", [])
    if not isinstance(failed, list) or len(failed) > 20:
        raise UpdateProtocolError("Άκυρη λίστα failed")
    return {"client_id": _text(message.get("client_id"), 64),
            "app": _version(message.get("app")),
            "updater": _int(message.get("updater"), 0, 1000),
            "protocol": _int(message.get("protocol"), 0, 1000),
            "failed": [_version(item) for item in failed]}


def parse_offer(message: dict) -> dict | None:
    if message.get("type") == "none":
        return None
    if message.get("type") != "offer":
        raise UpdateProtocolError("Αναμενόταν offer ή none")
    digest = message.get("sha256")
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise UpdateProtocolError("Άκυρο SHA-256 στο offer")
    from shared.package import MAX_PACKAGE_BYTES
    return {"version": _version(message.get("version")),
            "size": _int(message.get("size"), 1, MAX_PACKAGE_BYTES), "sha256": digest}


def parse_get(message: dict) -> dict:
    if message.get("type") != "get":
        raise UpdateProtocolError("Αναμενόταν get")
    from shared.package import MAX_PACKAGE_BYTES
    return {"version": _version(message.get("version")),
            "offset": _int(message.get("offset"), 0, MAX_PACKAGE_BYTES)}


def report_message(state: str, version: str | None, detail: str = "",
                   progress: int | None = None) -> dict:
    message = {"type": "report", "state": state, "version": version,
               "detail": detail[:300]}
    if progress is not None:
        message["progress"] = progress
    return message


def parse_report(message: dict) -> dict:
    if message.get("type") != "report":
        raise UpdateProtocolError("Αναμενόταν report")
    state = message.get("state")
    if state not in STATES:
        raise UpdateProtocolError("Άγνωστη κατάσταση ενημέρωσης")
    version = message.get("version")
    progress = message.get("progress")
    return {"state": state, "version": None if version is None else _version(version),
            "detail": _text(message.get("detail", ""), 300),
            "progress": None if progress is None else _int(progress, 0, 100)}
