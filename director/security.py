"""Λογική της σελίδας «Ασφάλεια» του Director, ανεξάρτητη από Qt.

Η σελίδα PySide6 βρίσκεται στο `director/qt_security.py` και καλεί μόνο αυτό το
αρχείο. Έτσι εγγραφές, έγκριση με κωδικό, αλλαγή αίθουσας/ονόματος, ανάκληση και
«τελευταία σύνδεση» ελέγχονται χωρίς οθόνη (docs/ui-style.md §6, phase4-2.md §3).

Τελευταία σύνδεση (απόφαση χρήστη 22/11/2026): ο Manager θυμάται πότε ήταν
τελευταία φορά συνδεδεμένος κάθε υπολογιστής, και μετά από επανεκκίνηση, στο
`clients-last-seen.json` δίπλα στο μητρώο. Για την αίθουσα ισχύει ο πιο πρόσφατος
από τους υπολογιστές της.
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from shared.client_registry import RegistryError, _atomic_write
from shared.enrollment import (MAX_PENDING, REQUEST_TTL, EnrollmentError,
                               IncorrectCodeError)
from shared.identity import ROOM_PATTERN

LAST_SEEN_VERSION = 1
#: Χωρίς αλλαγή κατάστασης online/offline, αποθήκευση το πολύ μία φορά ανά λεπτό.
LAST_SEEN_SAVE_SECONDS = 60.0
LAST_SEEN_POLL_SECONDS = 1.0
CODE_LENGTH = 6

TEXT_APPROVED = "Εγκρίθηκε ✓ · θα συνδεθεί μόνος του"
TEXT_DENIED = "Απορρίφθηκε"
TEXT_EXPIRED = "Το αίτημα δεν υπάρχει πια (έληξε ή απαντήθηκε)"
TEXT_REJECTED = ("Λάθος κωδικός: το αίτημα απορρίφθηκε. "
                 "Ζητήστε νέα εγγραφή από τον υπολογιστή.")
TEXT_NEVER = "Δεν έχει συνδεθεί"
def full_notice(limit: int) -> str:
    return f"Όριο {limit} αιτημάτων: νέα αιτήματα απορρίπτονται"


TEXT_FULL_NOTICE = full_notice(MAX_PENDING)


def last_seen_path(registry_file: Path) -> Path:
    return Path(registry_file).with_name("clients-last-seen.json")


class LastSeenStore:
    """Πότε ήταν τελευταία φορά online κάθε Client (wall clock, δευτερόλεπτα epoch).

    Ασφαλές για πολλά νήματα. Γράφει αμέσως όταν κάποιος Client συνδεθεί ή
    αποσυνδεθεί, αλλιώς το πολύ κάθε ``LAST_SEEN_SAVE_SECONDS`` (σε απότομο
    τερματισμό χάνεται έως ένα λεπτό ακρίβειας, όχι η εγγραφή).
    """

    def __init__(self, path: Path | None, *, clock: Callable[[], float] = time.time,
                 log=print):
        self.path = Path(path) if path is not None else None
        self.clock = clock
        self.log = log
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}
        self._online: set[str] = set()
        self._dirty = False
        self._saved_at = clock()
        self._load()

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, UnicodeError, ValueError) as exc:
            self.log(f"[Ασφάλεια] Αγνοήθηκε αρχείο τελευταίας σύνδεσης: {exc}")
            return
        clients = data.get("clients") if isinstance(data, dict) else None
        if not isinstance(clients, dict) or data.get("version") != LAST_SEEN_VERSION:
            self.log("[Ασφάλεια] Αγνοήθηκε αρχείο τελευταίας σύνδεσης άγνωστης μορφής")
            return
        for client_id, value in clients.items():
            if isinstance(client_id, str) and isinstance(value, (int, float)) \
                    and not isinstance(value, bool) and value > 0:
                self._seen[client_id] = float(value)

    def get(self, client_id: str) -> float | None:
        with self._lock:
            return self._seen.get(client_id)

    def update(self, status: list[tuple[str, str, bool, float]]) -> None:
        """Ενημέρωση από το `AudioBroadcaster.status_snapshot()`.

        Για κάθε Client η στιγμή = τώρα − «δευτερόλεπτα από τελευταία επικοινωνία»,
        οπότε και ένας αποσυνδεδεμένος κρατά την ακριβή ώρα του τελευταίου heartbeat.
        """
        now = self.clock()
        online = {client_id for client_id, _room, active, _age in status if active}
        with self._lock:
            # Νέα αποσύνδεση (και σύνδεση που κράτησε λιγότερο από ένα γύρο ελέγχου).
            disconnected = False
            for client_id, _room, active, age in status:
                instant = now - max(0.0, float(age))
                if instant > self._seen.get(client_id, 0.0):
                    self._seen[client_id] = instant
                    self._dirty = True
                    disconnected = disconnected or not active
            changed = online != self._online or disconnected
            self._online = online
            if self._dirty and (changed or now - self._saved_at >= LAST_SEEN_SAVE_SECONDS):
                self._save_locked(now)

    def flush(self) -> None:
        with self._lock:
            if self._dirty:
                self._save_locked(self.clock())

    def _save_locked(self, now: float) -> None:
        self._dirty = False
        self._saved_at = now
        if self.path is None:
            return
        data = {"version": LAST_SEEN_VERSION,
                "clients": {key: round(value, 1) for key, value in sorted(self._seen.items())}}
        try:
            _atomic_write(self.path, json.dumps(data, indent=1).encode("utf-8"))
        except OSError as exc:
            self._dirty = True
            self.log(f"[Ασφάλεια] Αποτυχία αποθήκευσης τελευταίας σύνδεσης: {exc}")


def track_last_seen(store: LastSeenStore, broadcaster, stop: threading.Event,
                    interval: float = LAST_SEEN_POLL_SECONDS) -> None:
    """Νήμα: καταγράφει online/offline όσο τρέχει ο Director (GUI ή τερματικό)."""
    try:
        while not stop.wait(interval):
            store.update(broadcaster.status_snapshot())
    finally:
        store.update(broadcaster.status_snapshot())
        store.flush()


# ------------------------------------------------------------ κείμενα

def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def computers_text(count: int) -> str:
    return _plural(count, "υπολογιστής", "υπολογιστές")


def ago_text(seconds: float) -> str:
    """«πριν 20″», «πριν 3 λεπτά», «πριν 2 ώρες», «πριν 3 ημέρες» (όπως το mockup)."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"πριν {seconds}″"
    if seconds < 3600:
        return "πριν " + _plural(seconds // 60, "λεπτό", "λεπτά")
    if seconds < 86400:
        return "πριν " + _plural(seconds // 3600, "ώρα", "ώρες")
    return "πριν " + _plural(seconds // 86400, "ημέρα", "ημέρες")


def expires_text(expires: float, now: float) -> str:
    """«λήγει σήμερα 18:05», «λήγει αύριο 07:12» ή «λήγει 24/11 07:12»."""
    when = time.localtime(expires)
    today = time.localtime(now)
    clock = time.strftime("%H:%M", when)
    days = (time.mktime((when.tm_year, when.tm_mon, when.tm_mday, 12, 0, 0, 0, 0, -1))
            - time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 12, 0, 0, 0, 0, -1)))
    days = round(days / 86400)
    if days == 0:
        return f"λήγει σήμερα {clock}"
    if days == 1:
        return f"λήγει αύριο {clock}"
    return f"λήγει {time.strftime('%d/%m', when)} {clock}"


def valid_code(code: str) -> bool:
    return len(code) == CODE_LENGTH and code.isascii() and code.isdigit()


def check_room(room: str) -> str:
    room = room.strip()
    if not ROOM_PATTERN.fullmatch(room):
        raise ValueError("Μη έγκυρη αίθουσα (1-64 χαρακτήρες)")
    return room


def check_name(name: str) -> str:
    name = name.strip()
    if not 1 <= len(name) <= 64 or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ValueError("Μη έγκυρο όνομα υπολογιστή (1-64 χαρακτήρες)")
    return name


# ------------------------------------------------------------ δεδομένα σελίδας

@dataclass(frozen=True)
class RequestView:
    request_id: str
    room: str
    name: str
    created: float
    expires: float

    @property
    def short_id(self) -> str:
        return self.request_id[:8]

    @property
    def title(self) -> str:
        return f"{self.room} · {self.name}"

    def meta(self, now: float) -> str:
        return (f"ID {self.short_id} · {ago_text(now - self.created)} · "
                f"{expires_text(self.expires, now)}")


@dataclass(frozen=True)
class ComputerView:
    client_id: str
    name: str
    room: str
    online: bool
    last_seen: float | None

    def state(self, now: float) -> str:
        if self.online:
            return "Online"
        if self.last_seen is None:
            return TEXT_NEVER
        return ago_text(now - self.last_seen)


@dataclass
class RoomView:
    room: str
    computers: list[ComputerView] = field(default_factory=list)

    @property
    def online(self) -> bool:
        return any(c.online for c in self.computers)

    @property
    def last_seen(self) -> float | None:
        """Η αίθουσα μετρά από τον πιο πρόσφατο υπολογιστή της."""
        seen = [c.last_seen for c in self.computers if c.last_seen is not None]
        return max(seen) if seen else None

    def state(self, now: float) -> str:
        if self.online:
            return "Online"
        seen = self.last_seen
        return TEXT_NEVER if seen is None else ago_text(now - seen)


@dataclass(frozen=True)
class Decision:
    """Αποτέλεσμα έγκρισης/απόρριψης για εμφάνιση στη γραμμή του αιτήματος.

    ``final``: το αίτημα έκλεισε (η γραμμή κουμπιών αντικαθίσταται)· αλλιώς
    μένουν τα κουμπιά για νέα προσπάθεια. ``ok``: πράσινο, αλλιώς κόκκινο.
    """
    text: str
    ok: bool
    final: bool


def _sort_key(text: str) -> str:
    return text.casefold()


class SecurityModel:
    """Ό,τι χρειάζεται η καρτέλα «Υπολογιστές», πάνω στα πραγματικά αντικείμενα.

    ``enrollment``: `shared.enrollment.EnrollmentServer`
    ``trust``: `director.main.RegistryTrust` (μητρώο + mTLS context)
    ``broadcaster``: `director.broadcast.AudioBroadcaster` (ποιοι είναι online)
    """

    def __init__(self, enrollment, trust, broadcaster, last_seen: LastSeenStore, *,
                 clock: Callable[[], float] = time.time):
        self.enrollment = enrollment
        self.trust = trust
        self.broadcaster = broadcaster
        self.last_seen = last_seen
        self.clock = clock

    # --- θύρα εγγραφής ------------------------------------------------------
    def port_notice(self) -> str | None:
        """Κείμενο μόνο όταν οι εγγραφές δεν δέχονται νέα αιτήματα, αλλιώς None.

        Οι εγγραφές είναι πάντα ανοιχτές (απόφαση χρήστη 22/11/2026)· εμφανίζεται
        προειδοποίηση μόνο αν η θύρα δεν άνοιξε ή αν συμπληρώθηκε το όριο αιτημάτων.
        """
        if not self.enrollment.is_open:
            error = self.enrollment.start_error or "κλειστή"
            return f"Η θύρα εγγραφής {self.enrollment.port} δεν άνοιξε: {error}"
        if self.enrollment.is_full:
            return full_notice(self.enrollment.max_pending)
        return None

    # --- αιτήματα -----------------------------------------------------------
    def requests(self) -> list[RequestView]:
        """Τα πιο πρόσφατα πρώτα. Ο κωδικός (SAS) δεν περιλαμβάνεται ποτέ."""
        items = [RequestView(rid, room, name, expires - REQUEST_TTL, expires)
                 for rid, room, name, expires in self.enrollment.pending()]
        return sorted(items, key=lambda r: (-r.created, r.request_id))

    def approve(self, request_id: str, code: str) -> Decision:
        code = code.strip()
        if not valid_code(code):
            return Decision(f"Ο κωδικός έχει {CODE_LENGTH} ψηφία", False, False)
        try:
            self.enrollment.decide(request_id, code, approve=True)
        except IncorrectCodeError as exc:
            if exc.remaining <= 0:
                return Decision(TEXT_REJECTED, False, True)
            left = ("Απομένει 1 προσπάθεια" if exc.remaining == 1
                    else f"Απομένουν {exc.remaining} προσπάθειες")
            return Decision(f"Λάθος κωδικός. {left}.", False, False)
        except (EnrollmentError, RegistryError, ValueError) as exc:
            return self._failure(exc)
        return Decision(TEXT_APPROVED, True, True)

    def deny(self, request_id: str) -> Decision:
        try:
            self.enrollment.decide(request_id, approve=False)
        except (EnrollmentError, RegistryError, ValueError) as exc:
            return self._failure(exc)
        return Decision(TEXT_DENIED, False, True)

    @staticmethod
    def _failure(exc: Exception) -> Decision:
        if "No active enrollment request" in str(exc):
            return Decision(TEXT_EXPIRED, False, True)
        return Decision(f"Σφάλμα: {exc}", False, True)

    # --- εγγεγραμμένοι ------------------------------------------------------
    def rooms(self) -> list[RoomView]:
        """Εγκεκριμένοι υπολογιστές ανά αίθουσα (αλφαβητικά), με online/τελευταία σύνδεση."""
        now = self.clock()
        status = self.broadcaster.status_snapshot()
        online = {client_id for client_id, _room, active, _age in status if active}
        # Η ζωντανή κατάσταση μετρά αμέσως, πριν από τον επόμενο γύρο του LastSeenStore.
        live = {client_id: now - max(0.0, float(age)) for client_id, _r, _a, age in status}
        groups: dict[str, RoomView] = {}
        for entry in self.trust.registry.snapshot()["clients"]:
            if entry.get("status") != "approved":
                continue
            client_id = entry["client_id"]
            name = entry.get("name") or client_id[:8]
            seen = [t for t in (self.last_seen.get(client_id), live.get(client_id))
                    if t is not None]
            computer = ComputerView(client_id, name, entry["room"], client_id in online,
                                    max(seen) if seen else None)
            groups.setdefault(entry["room"], RoomView(entry["room"])).computers.append(computer)
        for group in groups.values():
            group.computers.sort(key=lambda c: (_sort_key(c.name), c.client_id))
        return [groups[room] for room in sorted(groups, key=_sort_key)]

    @staticmethod
    def totals(rooms: list[RoomView]) -> str:
        computers = [c for room in rooms for c in room.computers]
        online = sum(c.online for c in computers)
        return f"{computers_text(len(computers))} · {online} online"

    def change_room(self, client_id: str, room: str) -> dict:
        try:
            return self.trust.update_info(client_id, self.broadcaster, room=check_room(room))
        except RegistryError as exc:
            raise ValueError(f"Η αλλαγή αίθουσας απέτυχε: {exc}") from exc

    def rename(self, client_id: str, name: str) -> dict:
        try:
            return self.trust.update_info(client_id, self.broadcaster, name=check_name(name))
        except RegistryError as exc:
            raise ValueError(f"Η μετονομασία απέτυχε: {exc}") from exc

    def revoke(self, client_id: str) -> None:
        """Άμεση αποσύνδεση· για επιστροφή χρειάζεται νέα εγγραφή με νέο κλειδί."""
        try:
            self.trust.revoke(client_id, self.broadcaster)
        except RegistryError as exc:
            raise ValueError(f"Η ανάκληση απέτυχε: {exc}") from exc
