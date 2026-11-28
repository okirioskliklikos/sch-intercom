"""5C2: αιτήματα αλλαγής αίθουσας/ονόματος από ήδη εγκεκριμένο Client (phase5.md §6.5).

Ο κωδικός εγγραφής χρειάζεται μόνο στην πρώτη εγκατάσταση (§6.4). Ένας εγκεκριμένος
υπολογιστής ζητά νέα αίθουσα/όνομα μέσα από τη σύνδεση mTLS που ήδη έχει, και ο
διαχειριστής εγκρίνει με ένα κλικ, ακόμη και με τον Client κλειστό.

Ροή στο ίδιο TLS κανάλι ήχου (πρωτόκολλο σύνδεσης v8):
- Client → Director: ``SICR`` + μήκος (2 bytes) + JSON ``{"room", "name"}``, ανάμεσα
  στα heartbeat (``shared.control``).
- Director → Client: μήνυμα ελέγχου ``MESSAGE_CHANGE_STATUS`` με JSON
  ``{"state", "room", "name"}``: ``pending`` (καταχωρήθηκε), ``denied``, ``approved``
  ή ``none`` (δεν υπάρχει αίτημα· ο Client ξαναστέλνει ό,τι περιμένει).

Ένα αίτημα ανά υπολογιστή: νέο αντικαθιστά το προηγούμενο. Τα αιτήματα ισχύουν μία
ημέρα· οι αποφάσεις κρατιούνται επίσης μία ημέρα, ώστε ο Client να μάθει την
απόρριψη στην επόμενη σύνδεση.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from collections.abc import Callable

from shared.identity import ROOM_PATTERN

#: Ίδια διάρκεια με τα αιτήματα εγγραφής (απόφαση χρήστη 21/11/2026).
REQUEST_TTL = 24 * 3600
FILE_VERSION = 1
#: Όριο αποθηκευμένων αιτημάτων/αποφάσεων (ένα ανά υπολογιστή, άρα πρακτικά 70).
MAX_REQUESTS = 256
MAX_PAYLOAD = 512

PENDING = "pending"
DENIED = "denied"
APPROVED = "approved"
NONE = "none"
STATES = (PENDING, DENIED, APPROVED, NONE)
#: Στον Client: αποθηκεύτηκε τοπικά, δεν έχει επιβεβαιωθεί ακόμη από τον Director.
QUEUED = "queued"
CLIENT_FILE = "change-request.json"
DIRECTOR_FILE = "change-requests.json"


class ChangeRequestError(ValueError):
    pass


def check_room(room) -> str:
    if not isinstance(room, str) or not ROOM_PATTERN.fullmatch(room) or room.strip() != room:
        raise ChangeRequestError("Μη έγκυρη αίθουσα (1-64 χαρακτήρες)")
    return room


def check_name(name) -> str:
    if (not isinstance(name, str) or not 1 <= len(name) <= 64 or name.strip() != name
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)):
        raise ChangeRequestError("Μη έγκυρο όνομα υπολογιστή (1-64 χαρακτήρες)")
    return name


def clean(room: str, name: str) -> tuple[str, str]:
    """Από πεδία φόρμας: χωρίς κενά στις άκρες, μετά έλεγχος."""
    return check_room(str(room).strip()), check_name(str(name).strip())


def encode_request(room: str, name: str) -> bytes:
    payload = json.dumps({"room": check_room(room), "name": check_name(name)},
                         ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(payload) > MAX_PAYLOAD:
        raise ChangeRequestError("Πολύ μεγάλο αίτημα")
    return payload


def decode_request(payload: bytes) -> tuple[str, str]:
    if len(payload) > MAX_PAYLOAD:
        raise ChangeRequestError("Πολύ μεγάλο αίτημα")
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ChangeRequestError("Μη έγκυρο αίτημα αλλαγής") from exc
    if not isinstance(data, dict) or set(data) != {"room", "name"}:
        raise ChangeRequestError("Μη έγκυρο αίτημα αλλαγής")
    return check_room(data["room"]), check_name(data["name"])


def encode_status(state: str, room: str = "", name: str = "") -> bytes:
    if state not in STATES:
        raise ChangeRequestError("Άγνωστη κατάσταση αιτήματος")
    if state != NONE:
        check_room(room)
        check_name(name)
    payload = json.dumps({"state": state, "room": room, "name": name},
                         ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(payload) > MAX_PAYLOAD:
        raise ChangeRequestError("Πολύ μεγάλη απάντηση")
    return payload


def decode_status(payload: bytes) -> dict:
    if len(payload) > MAX_PAYLOAD:
        raise ChangeRequestError("Πολύ μεγάλη απάντηση")
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ChangeRequestError("Μη έγκυρη απάντηση αιτήματος") from exc
    if not isinstance(data, dict) or set(data) != {"state", "room", "name"} or \
            data["state"] not in STATES:
        raise ChangeRequestError("Μη έγκυρη απάντηση αιτήματος")
    if data["state"] != NONE:
        check_room(data["room"])
        check_name(data["name"])
    return {"state": data["state"], "room": data["room"], "name": data["name"]}


def _atomic_json(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=1)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


# --- Client: το δικό του αίτημα ---------------------------------------------------------
def load_local(path: Path, client_id: str, *, now: float | None = None) -> dict | None:
    """Το αίτημα αυτού του υπολογιστή: {room, name, state, at} ή None.

    Απόρριψη μένει ορατή μία ημέρα· ό,τι περιμένει (queued/pending) δεν λήγει τοπικά
    (ο Director απαντά «none» όταν το δικό του έληξε και ο Client το ξαναστέλνει).
    """
    now = time.time() if now is None else now
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    try:
        if not isinstance(data, dict) or data.get("client_id") != client_id:
            return None
        room, name = check_room(data["room"]), check_name(data["name"])
        state, at = data["state"], float(data["at"])
    except (KeyError, TypeError, ValueError):
        return None
    if state not in (QUEUED, PENDING, DENIED):
        return None
    if state == DENIED and now - at >= REQUEST_TTL:
        return None
    return {"room": room, "name": name, "state": state, "at": at}


def save_local(path: Path, client_id: str, room: str, name: str, state: str, *,
               at: float | None = None) -> dict:
    if state not in (QUEUED, PENDING, DENIED):
        raise ChangeRequestError("Άγνωστη κατάσταση")
    record = {"client_id": client_id, "room": check_room(room), "name": check_name(name),
              "state": state, "at": time.time() if at is None else float(at)}
    _atomic_json(path, record)
    return {k: record[k] for k in ("room", "name", "state", "at")}


def clear_local(path: Path) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


# --- Director: τα αιτήματα όλων --------------------------------------------------------
@dataclass(frozen=True)
class ChangeRequest:
    client_id: str
    room: str
    name: str
    old_room: str
    old_name: str
    created: float
    state: str = PENDING
    decided: float | None = None

    @property
    def expires(self) -> float:
        return self.created + REQUEST_TTL

    def as_dict(self) -> dict:
        return {"client_id": self.client_id, "room": self.room, "name": self.name,
                "old_room": self.old_room, "old_name": self.old_name,
                "created": self.created, "state": self.state, "decided": self.decided}


class ChangeRequests:
    """Αιτήματα αλλαγής στον Manager, στον δίσκο δίπλα στο μητρώο.

    ``apply(client_id, room, name)``: η πραγματική αλλαγή στο μητρώο (ελέγχει και τον
    ρόλο Κύριος)· ``notify(client_id, status)``: ενημέρωση του Client αν είναι online.
    """

    def __init__(self, path: Path | None, *, apply: Callable[[str, str, str], object]
                 | None = None, notify: Callable[[str, dict], None] | None = None,
                 clock: Callable[[], float] = time.time, log=print):
        self.path = Path(path) if path is not None else None
        self.apply = apply
        self.notify = notify or (lambda client_id, status: None)
        self.clock = clock
        self.log = log
        self._lock = threading.RLock()
        self._items: dict[str, ChangeRequest] = {}
        self._load()

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, UnicodeError, ValueError) as exc:
            self.log(f"[Αλλαγές] Αγνοήθηκε το αρχείο αιτημάτων: {exc}")
            return
        if not isinstance(data, dict) or data.get("version") != FILE_VERSION:
            self.log("[Αλλαγές] Αγνοήθηκε αρχείο αιτημάτων άγνωστης μορφής")
            return
        for item in data.get("requests", [])[:MAX_REQUESTS]:
            try:
                request = ChangeRequest(
                    str(item["client_id"]), check_room(item["room"]), check_name(item["name"]),
                    str(item.get("old_room") or ""), str(item.get("old_name") or ""),
                    float(item["created"]), item["state"],
                    None if item.get("decided") is None else float(item["decided"]))
                if request.state not in (PENDING, DENIED, APPROVED):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                continue
            self._items[request.client_id] = request
        self._purge()

    def _save(self) -> None:
        if self.path is None:
            return
        _atomic_json(self.path, {"version": FILE_VERSION,
                                 "requests": [r.as_dict() for r in self._items.values()]})

    def _purge(self) -> bool:
        now = self.clock()
        stale = [key for key, r in self._items.items()
                 if now - (r.decided if r.decided is not None else r.created) >= REQUEST_TTL]
        for key in stale:
            del self._items[key]
        return bool(stale)

    # --- από τον Client ------------------------------------------------------
    def submit(self, client_id: str, room: str, name: str, *, current_room: str,
               current_name: str) -> dict:
        """Νέο αίτημα (αντικαθιστά το προηγούμενο του ίδιου υπολογιστή)· η απάντηση
        προς τον Client. Ίδιες τιμές με το μητρώο: τίποτα να αλλάξει (``approved``)."""
        room, name = check_room(room), check_name(name)
        with self._lock:
            self._purge()
            if (room, name) == (current_room, current_name):
                previous = self._items.pop(client_id, None)
                if previous is not None and previous.state == PENDING:
                    self._save()
                return {"state": APPROVED, "room": room, "name": name}
            previous = self._items.get(client_id)
            if previous is not None and previous.state == PENDING and \
                    (previous.room, previous.name) == (room, name):
                return {"state": PENDING, "room": room, "name": name}
            if client_id not in self._items and len(self._items) >= MAX_REQUESTS:
                oldest = min(self._items, key=lambda key: self._items[key].created)
                del self._items[oldest]
            self._items[client_id] = ChangeRequest(client_id, room, name, current_room,
                                                   current_name, self.clock())
            self._save()
        self.log(f"[Αλλαγές] Αίτημα {current_room} · {current_name} → {room} · {name} "
                 f"({client_id[:8]})")
        return {"state": PENDING, "room": room, "name": name}

    def status_for(self, client_id: str) -> dict:
        """Τι στέλνεται στον Client στη σύνδεση."""
        with self._lock:
            if self._purge():
                self._save()
            request = self._items.get(client_id)
            if request is None:
                return {"state": NONE, "room": "", "name": ""}
            return {"state": request.state, "room": request.room, "name": request.name}

    # --- από τον Manager -----------------------------------------------------
    def pending(self) -> list[ChangeRequest]:
        with self._lock:
            if self._purge():
                self._save()
            return sorted((r for r in self._items.values() if r.state == PENDING),
                          key=lambda r: (-r.created, r.client_id))

    def get(self, client_id: str) -> ChangeRequest | None:
        with self._lock:
            return self._items.get(client_id)

    def resolve(self, prefix: str) -> str:
        """Πλήρες UUID από μοναδικό πρόθεμα (≥ 4 χαρακτήρες) ενός εκκρεμούς αιτήματος."""
        ids = [r.client_id for r in self.pending()]
        if prefix in ids:
            return prefix
        matches = [client_id for client_id in ids if client_id.startswith(prefix)]
        if len(prefix) < 4 or len(matches) != 1:
            raise ChangeRequestError("Άγνωστο ή διφορούμενο αίτημα αλλαγής")
        return matches[0]

    def decide(self, client_id: str, *, approve: bool) -> ChangeRequest:
        with self._lock:
            self._purge()
            request = self._items.get(client_id)
            if request is None or request.state != PENDING:
                raise ChangeRequestError("Δεν υπάρχει ενεργό αίτημα αλλαγής")
            if approve and self.apply is not None:
                self.apply(client_id, request.room, request.name)
            decided = ChangeRequest(request.client_id, request.room, request.name,
                                    request.old_room, request.old_name, request.created,
                                    APPROVED if approve else DENIED, self.clock())
            self._items[client_id] = decided
            self._save()
        self.log(f"[Αλλαγές] {'Εγκρίθηκε' if approve else 'Απορρίφθηκε'}: "
                 f"{request.old_room} · {request.old_name} → {request.room} · {request.name}")
        try:
            self.notify(client_id, {"state": decided.state, "room": decided.room,
                                    "name": decided.name})
        except Exception as exc:  # noqa: BLE001 — η απόφαση έχει ήδη αποθηκευτεί
            self.log(f"[Αλλαγές] Ο Client δεν ενημερώθηκε τώρα: {exc}")
        return decided

    def forget(self, client_id: str) -> None:
        """Ο υπολογιστής ανακλήθηκε: το αίτημά του δεν έχει πια νόημα."""
        with self._lock:
            if self._items.pop(client_id, None) is not None:
                self._save()
