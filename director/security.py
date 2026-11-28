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

import html
import json
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from shared.room_names import room_title

from shared.client_registry import RegistryError, _atomic_write
from shared.enrollment import (MAX_PENDING, REQUEST_TTL, EnrollmentError,
                               IncorrectCodeError)
from shared.change_request import ChangeRequestError
from director.notices import TEXT_DUPLICATE_STATE
from shared.identity import ROOM_PATTERN
from shared.ui import theme
from shared.version import parse_version

LAST_SEEN_VERSION = 1
#: Χωρίς αλλαγή κατάστασης online/offline, αποθήκευση το πολύ μία φορά ανά λεπτό.
LAST_SEEN_SAVE_SECONDS = 60.0
LAST_SEEN_POLL_SECONDS = 1.0
CODE_LENGTH = 6

# Τα αποτελέσματα μπαίνουν πάνω δεξιά στην κάρτα του αιτήματος (ui-style.md §6):
# σύντομα, με την εξήγηση σε tooltip.
TEXT_APPROVED = "Εγκρίθηκε ✓"
HINT_APPROVED = "Ο υπολογιστής θα συνδεθεί μόνος του."
HINT_CHANGE_APPROVED = "Ο υπολογιστής παίρνει τα νέα στοιχεία αμέσως ή στην επόμενη σύνδεση."
TEXT_DENIED = "Απορρίφθηκε"
TEXT_EXPIRED = "Δεν υπάρχει πια"
HINT_EXPIRED = "Το αίτημα έληξε ή απαντήθηκε."
TEXT_REJECTED = "Απορρίφθηκε · 3 λάθη"
HINT_REJECTED = "Λάθος κωδικός 3 φορές. Ζητήστε νέα εγγραφή από τον υπολογιστή."
TEXT_NEVER = "Δεν έχει συνδεθεί"
TEXT_JUST_NOW = "μόλις τώρα"
#: Από πόσα αιτήματα πριν από το όριο η ένδειξη γίνεται κόκκινη (45 για όριο 50).
WARN_MARGIN = 5


def full_text(limit: int) -> str:
    return f"Συμπληρώθηκε το όριο ({limit})"


def wrong_code_text(remaining: int) -> str:
    left = "απομένει 1" if remaining == 1 else f"απομένουν {remaining}"
    return f"Λάθος κωδικός · {left}"


def requests_status(count: int, limit: int = MAX_PENDING) -> tuple[str, bool]:
    """Κείμενο δεξιά στον τίτλο «Νέες εγγραφές» και αν είναι προειδοποίηση (κόκκινο).

    Απόφαση χρήστη 22/11/2026: γκρι «N αιτήματα», από τα 45 «N/50 αιτήματα» σε
    κόκκινο, στα 50 «Συμπληρώθηκε το όριο (50)». Χωρίς αιτήματα: κενό.
    """
    if count >= limit:
        return full_text(limit), True
    if count >= limit - WARN_MARGIN:
        return f"{count}/{limit} αιτήματα", True
    if count == 0:
        return "", False
    return _plural(count, "αίτημα", "αιτήματα"), False


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
    """«μόλις τώρα», «πριν 3 λεπτά», «πριν 2 ώρες», «πριν 3 ημέρες» (όπως το mockup)."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        # Απόφαση χρήστη 22/11/2026: όχι μέτρηση ανά δευτερόλεπτο.
        return TEXT_JUST_NOW
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

KIND_ENROLL = "enroll"
#: 5C2: αίτημα αλλαγής αίθουσας/ονόματος από εγκεκριμένο υπολογιστή (χωρίς κωδικό).
KIND_CHANGE = "change"
TAG_CHANGE = "Αλλαγή"
TEXT_OFFLINE = "εκτός σύνδεσης"


@dataclass(frozen=True)
class RequestView:
    request_id: str
    room: str
    name: str
    created: float
    expires: float
    kind: str = KIND_ENROLL
    old_room: str = ""
    old_name: str = ""
    #: Μόνο για αλλαγή: αν ο υπολογιστής είναι αυτή τη στιγμή συνδεδεμένος.
    online: bool = True

    @property
    def short_id(self) -> str:
        return self.request_id[:8]

    @property
    def title(self) -> str:
        return room_title(self.room, self.name)

    @property
    def old_title(self) -> str:
        return room_title(self.old_room, self.old_name)

    @property
    def key(self) -> str:
        """Μοναδικό ανάμεσα σε εγγραφές και αλλαγές (το ID της αλλαγής είναι το UUID)."""
        return f"{self.kind}:{self.request_id}"

    def meta(self, now: float) -> str:
        text = (f"ID {self.short_id} · {ago_text(now - self.created)} · "
                f"{expires_text(self.expires, now)}")
        if self.kind == KIND_CHANGE and not self.online:
            text += f" · {TEXT_OFFLINE}"
        return text


@dataclass(frozen=True)
class ComputerView:
    client_id: str
    name: str
    room: str
    online: bool
    last_seen: float | None
    #: 4.3C2: IP των δύο υπολογιστών όταν η ταυτότητα χρησιμοποιείται διπλά, αλλιώς ().
    duplicate: tuple[str, ...] = ()

    def state(self, now: float) -> str:
        if self.duplicate:
            return TEXT_DUPLICATE_STATE
        if self.online:
            return "Online"
        if self.last_seen is None:
            return TEXT_NEVER
        return ago_text(now - self.last_seen)


@dataclass
class RoomView:
    room: str
    computers: list[ComputerView] = field(default_factory=list)
    #: Ένδειξη «ενημερωμένη»: η τρέχουσα έκδοση πακέτου και οι online υπολογιστές
    #: της αίθουσας που την έχουν (κενό = χωρίς ✓, π.χ. χωρίς εισαγωγή πακέτου).
    version: str | None = None
    updated_by: tuple[str, ...] = ()

    @property
    def updated(self) -> bool:
        return self.version is not None and bool(self.updated_by)

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
    hint: str = ""


def _at_least(version: str | None, wanted) -> bool:
    if not version:
        return False
    try:
        return parse_version(version) >= wanted
    except ValueError:
        return False


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
        #: () -> (τρέχουσα έκδοση πακέτου ή None, {client_id: αναφερόμενη έκδοση}).
        #: Το ορίζει το GUI όταν υπάρχουν ενημερώσεις (director.updates_view).
        self.update_versions: Callable[[], tuple[str | None, dict[str, str]]] | None = None

    # --- θύρα εγγραφής ------------------------------------------------------
    def port_notice(self) -> str | None:
        """Κείμενο μόνο αν η θύρα εγγραφής δεν άνοιξε, αλλιώς None.

        Οι εγγραφές είναι πάντα ανοιχτές (απόφαση χρήστη 22/11/2026).
        """
        if not self.enrollment.is_open:
            error = self.enrollment.start_error or "κλειστή"
            return f"Η θύρα εγγραφής {self.enrollment.port} δεν άνοιξε: {error}"
        return None

    def requests_status(self, count: int) -> tuple[str, bool]:
        return requests_status(count, self.enrollment.max_pending)

    # --- αιτήματα -----------------------------------------------------------
    def requests(self) -> list[RequestView]:
        """Τα πιο πρόσφατα πρώτα. Ο κωδικός (SAS) δεν περιλαμβάνεται ποτέ.

        5C2: μαζί και τα αιτήματα αλλαγής αίθουσας/ονόματος (``kind="change"``).
        """
        items = [RequestView(rid, room, name, expires - REQUEST_TTL, expires)
                 for rid, room, name, expires in self.enrollment.pending()]
        changes = getattr(self.trust, "changes", None)
        if changes is not None:
            online = {client_id for client_id, _room, active, _age
                      in self.broadcaster.status_snapshot() if active}
            items += [RequestView(r.client_id, r.room, r.name, r.created, r.expires,
                                  KIND_CHANGE, r.old_room, r.old_name, r.client_id in online)
                      for r in changes.pending()]
        return sorted(items, key=lambda r: (-r.created, r.request_id))

    def approve(self, request_id: str, code: str, *, room: str | None = None,
                name: str | None = None) -> Decision:
        """Έγκριση εγγραφής με κωδικό· 5C2: με αίθουσα/όνομα όπως διορθώθηκαν στην κάρτα."""
        code = code.strip()
        if not valid_code(code):
            return Decision(f"Ο κωδικός έχει {CODE_LENGTH} ψηφία", False, False)
        try:
            if room is not None:
                room = check_room(room)
            if name is not None:
                name = check_name(name)
        except ValueError as exc:
            return Decision(str(exc), False, False)
        try:
            self.enrollment.decide(request_id, code, approve=True, room=room, name=name)
        except IncorrectCodeError as exc:
            if exc.remaining <= 0:
                return Decision(TEXT_REJECTED, False, True, HINT_REJECTED)
            return Decision(wrong_code_text(exc.remaining), False, False)
        except (EnrollmentError, RegistryError, ValueError) as exc:
            return self._failure(exc)
        return Decision(TEXT_APPROVED, True, True, HINT_APPROVED)

    def deny(self, request_id: str) -> Decision:
        try:
            self.enrollment.decide(request_id, approve=False)
        except (EnrollmentError, RegistryError, ValueError) as exc:
            return self._failure(exc)
        return Decision(TEXT_DENIED, False, True)

    def approve_change(self, client_id: str) -> Decision:
        """5C2: ένα κλικ, χωρίς κωδικό· το μητρώο αλλάζει αμέσως."""
        try:
            self.trust.changes.decide(client_id, approve=True)
        except (ChangeRequestError, RegistryError, ValueError) as exc:
            return self._failure(exc)
        return Decision(TEXT_APPROVED, True, True, HINT_CHANGE_APPROVED)

    def deny_change(self, client_id: str) -> Decision:
        try:
            self.trust.changes.decide(client_id, approve=False)
        except (ChangeRequestError, RegistryError, ValueError) as exc:
            return self._failure(exc)
        return Decision(TEXT_DENIED, False, True)

    @staticmethod
    def _failure(exc: Exception) -> Decision:
        if "No active enrollment request" in str(exc) or "Δεν υπάρχει ενεργό αίτημα" in str(exc):
            return Decision(TEXT_EXPIRED, False, True, HINT_EXPIRED)
        return Decision(f"Σφάλμα: {exc}", False, True)

    # --- εγγεγραμμένοι ------------------------------------------------------
    def rooms(self) -> list[RoomView]:
        """Εγκεκριμένοι υπολογιστές ανά αίθουσα (αλφαβητικά), με online/τελευταία σύνδεση."""
        now = self.clock()
        status = self.broadcaster.status_snapshot()
        online = {client_id for client_id, _room, active, _age in status if active}
        # Η ζωντανή κατάσταση μετρά αμέσως, πριν από τον επόμενο γύρο του LastSeenStore.
        live = {client_id: now - max(0.0, float(age)) for client_id, _r, _a, age in status}
        duplicates = {client_id: addresses for client_id, _room, addresses
                      in getattr(self.broadcaster, "duplicate_identities", list)()}
        groups: dict[str, RoomView] = {}
        for entry in self.trust.registry.snapshot()["clients"]:
            if entry.get("status") != "approved":
                continue
            client_id = entry["client_id"]
            name = entry.get("name") or client_id[:8]
            seen = [t for t in (self.last_seen.get(client_id), live.get(client_id))
                    if t is not None]
            computer = ComputerView(client_id, name, entry["room"], client_id in online,
                                    max(seen) if seen else None,
                                    duplicates.get(client_id, ()))
            groups.setdefault(entry["room"], RoomView(entry["room"])).computers.append(computer)
        for group in groups.values():
            group.computers.sort(key=lambda c: (_sort_key(c.name), c.client_id))
        self._mark_updated(groups.values())
        return [groups[room] for room in sorted(groups, key=_sort_key)]

    def _mark_updated(self, rooms) -> None:
        """✓ αίθουσας: ≥1 online υπολογιστής έχει (τουλάχιστον) την τρέχουσα έκδοση."""
        if self.update_versions is None:
            return
        target, versions = self.update_versions()
        if target is None:
            return
        wanted = parse_version(target)
        for room in rooms:
            room.version = target
            room.updated_by = tuple(c.name for c in room.computers
                                    if c.online and _at_least(versions.get(c.client_id), wanted))

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


# ------------------------------------------------------------ Κύριος / Εφεδρικός (4.2D2)

ROLE_NAMES = {"primary": "Κύριος", "backup": "Εφεδρικός"}
TEXT_SERVING_ON = "Ενεργή"
TEXT_SERVING_OFF = "Σταματημένη"
TEXT_NO_OTHER = "κανένας σε λειτουργία"
TEXT_PRIMARY_ONLY = "Μόνο στον Κύριο"
TEXT_CHANGES_PRIMARY_ONLY = "Αλλαγές μόνο στον Κύριο"
TEXT_CLOSED_BACKUP = "Κλειστές επειδή δεν είναι Κύριος"
TEXT_CLOSED_STOPPED = "Κλειστές όσο η εξυπηρέτηση είναι σταματημένη"
# Σύντομη μορφή της προειδοποίησης του phase4-2d.md §4, όπως στο εγκεκριμένο mockup.
TEXT_PROMOTE = ("Αν και ο άλλος υπολογιστής μείνει Κύριος, τα μητρώα θα αποκλίνουν "
                "και θα χρειαστεί συγχώνευση.")
TEXT_DEMOTE = ("Ο υπολογιστής γίνεται Εφεδρικός. Νέες εγγραφές και αλλαγές "
               "υπολογιστών γίνονται μόνο στον Κύριο.")
TEXT_TWO_MANAGERS = "Με δύο Manager σε λειτουργία οι Clients δεν συνδέονται σε κανέναν."


@dataclass(frozen=True)
class OtherManager:
    """Άλλος Manager της ίδιας εγκατάστασης που στέλνει beacon (δηλαδή εξυπηρετεί)."""
    host: str
    ip: str
    rv: int
    inst: str


@dataclass(frozen=True)
class RoleStatus:
    """Στιγμιότυπο για επάνω μπάρα, ειδοποιήσεις και κάρτα «Ρόλος Manager»."""
    role: str                       # primary | backup
    serving: bool                   # πραγματική εξυπηρέτηση (ServingController.active)
    notice: str | None              # "restored_as_backup" ή None
    others: tuple[OtherManager, ...]
    registry_version: int | None
    host: str
    #: 4.2D3: νεότερο/διαφορετικό μητρώο που ανέφεραν Clients
    #: (`director.registry_reports.ReportedRegistry`) ή None.
    reported: object | None = None

    @property
    def primary(self) -> bool:
        return self.role == "primary"

    @property
    def role_text(self) -> str:
        return ROLE_NAMES[self.role]

    @property
    def conflict(self) -> bool:
        """Εξυπηρετούμε και εξυπηρετεί και άλλος (κόκκινη ένδειξη)."""
        return self.serving and bool(self.others)

    @property
    def newer(self) -> tuple[OtherManager, ...]:
        if self.registry_version is None:
            return ()
        return tuple(o for o in self.others if o.rv > self.registry_version)

    @property
    def restored(self) -> bool:
        return self.notice == "restored_as_backup"

    @property
    def enrollment_closed(self) -> str | None:
        """Γιατί οι νέες εγγραφές είναι κλειστές (None = ανοιχτές)."""
        if not self.primary:
            return TEXT_CLOSED_BACKUP
        if not self.serving:
            return TEXT_CLOSED_STOPPED
        return None

    def header(self) -> tuple[str, str]:
        """(κείμενο, κατάσταση) επάνω δεξιά: up (πράσινο ●), conflict (κόκκινο ●),
        stopped (γκρι ○)· απόφαση χρήστη 23/11/2026."""
        role = self.role_text
        if not self.serving:
            return f"Χωρίς εξυπηρέτηση ({role})", "stopped"
        if self.others:
            return f"Δύο Manager σε λειτουργία ({role})", "conflict"
        return f"Συνδεδεμένος ({role})", "up"

    def serving_text(self) -> tuple[str, str]:
        return (TEXT_SERVING_ON, "ok") if self.serving else (TEXT_SERVING_OFF, "err")

    def others_html(self) -> tuple[str, str]:
        """Τιμή «Άλλος Manager» (rich text) και τόνος: muted, err ή κανονικό."""
        if not self.others:
            return TEXT_NO_OTHER, "muted"
        lines = []
        for other in self.others:
            line = f"{html.escape(other.host)} · {html.escape(other.ip)} · "
            if self.registry_version is not None and other.rv > self.registry_version:
                line += (f'<span style="color:{_LIVE}">μητρώο v{other.rv}, '
                         f"εδώ v{self.registry_version}</span>")
            else:
                line += "εξυπηρετεί"
            lines.append(line)
        return "<br>".join(lines), "err" if self.serving else ""

    # --- ειδοποιήσεις κάτω από τον τίτλο της σελίδας ----------------------------
    def conflict_html(self) -> str:
        names = ", ".join(f"<b>{html.escape(o.host)}</b> ({html.escape(o.ip)})"
                          for o in self.others)
        return f"Εξυπηρετεί και ο υπολογιστής {names}. Σταματήστε τον έναν."

    def restored_html(self) -> str:
        text = "Επαναφορά από backup: ο υπολογιστής είναι <b>Εφεδρικός</b>"
        return text + ("." if self.serving else " και δεν εξυπηρετεί.")

    def newer_html(self) -> str:
        other = max(self.newer, key=lambda o: o.rv)
        return (f"Το μητρώο χρειάζεται ενημέρωση: ο {html.escape(other.host)} έχει "
                f"v{other.rv}, εδώ v{self.registry_version}.")

    @property
    def registry_alarm(self) -> bool:
        """Κόκκινη ειδοποίηση μητρώου: από beacon άλλου Manager ή από Clients."""
        return bool(self.newer) or self.reported is not None

    @property
    def can_accept_current(self) -> bool:
        """«Συνέχεια με αυτό το μητρώο…»: μόνο στον Κύριο, μόνο όταν το νεότερο
        μητρώο το ανέφεραν Clients και κανένας Manager σε λειτουργία δεν το έχει."""
        return self.primary and self.reported is not None and not self.newer

    def registry_alarm_html(self) -> str:
        if self.newer:
            return self.newer_html()
        return html.escape(self.reported.bar_text())

    def newer_warning(self) -> str | None:
        """Γραμμή στον διάλογο «Αλλαγή σε Κύριο» (phase4-2d.md §4)."""
        if not self.newer:
            return None
        other = max(self.newer, key=lambda o: o.rv)
        return (f"Ο {other.host} έχει νεότερο μητρώο (v{other.rv}, "
                f"εδώ v{self.registry_version}). Εισαγάγετέ το πρώτα.")


# Χρώμα του σφάλματος μέσα σε rich text (το theme δεν εξαρτάται από Qt).
_LIVE = theme.LIVE


def local_host() -> str:
    """Όνομα υπολογιστή, όπως στέλνεται στο beacon (έως 63 χαρακτήρες)."""
    name = "".join(ch for ch in socket.gethostname() if ord(ch) >= 32 and ord(ch) != 127)
    return name[:63] or "—"


class RoleModel:
    """Ρόλος και εξυπηρέτηση για το GUI, πάνω στα αντικείμενα του 4.2D1.

    ``controller``: `director.serving.ServingController` (έναρξη/διακοπή)
    ``watcher``: `shared.discovery.BeaconWatcher` (άλλοι Managers)
    ``registry_version``: τρέχουσα έκδοση του ζωντανού μητρώου
    """

    def __init__(self, state_path: Path, controller, watcher, *,
                 registry_version: Callable[[], int | None] = lambda: None,
                 host: str | None = None,
                 reported: Callable[[], object | None] = lambda: None,
                 accept_current: Callable[..., int] | None = None):
        self.state_path = Path(state_path)
        self.controller = controller
        self.watcher = watcher
        self.registry_version = registry_version
        self.host = host or local_host()
        self.reported = reported
        self._accept_current = accept_current

    def status(self) -> RoleStatus:
        from director.manager_state import load
        state = load(self.state_path)
        others = tuple(OtherManager(item.info.host, item.ip, item.info.rv, item.info.inst)
                       for item in (self.watcher.others() if self.watcher else []))
        return RoleStatus(state.role, bool(self.controller and self.controller.active),
                          state.notice, others, self.registry_version(), self.host,
                          self.reported())

    def accept_current(self) -> int:
        """«Συνέχεια με αυτό το μητρώο» (4.2D3)· επιστρέφει τη νέα έκδοση."""
        if self._accept_current is None:
            raise ValueError("Δεν υπάρχει μητρώο")
        status = self.status()
        return self._accept_current(newer_hosts=tuple(o.host for o in status.newer))

    # --- ενέργειες ----------------------------------------------------------
    def promote(self) -> None:
        """Αλλαγή σε Κύριο: σβήνει την ειδοποίηση επαναφοράς, ανοίγει την εγγραφή
        αν εξυπηρετεί. Η εξυπηρέτηση δεν αλλάζει (phase4-2d.md §4)."""
        from director.manager_state import change_role
        change_role(self.state_path, "primary")
        if self.controller is not None:
            self.controller.role_changed()

    def demote(self) -> None:
        from director.manager_state import change_role
        change_role(self.state_path, "backup")
        if self.controller is not None:
            self.controller.role_changed()

    def acknowledge(self) -> None:
        """«Να μείνει Εφεδρικός»: σβήνει μόνο την ειδοποίηση."""
        from director.manager_state import acknowledge
        acknowledge(self.state_path)

    def start(self, force: bool = False) -> None:
        """Σε σύγκρουση χωρίς force: `director.serving.ServingConflict`."""
        self.controller.start(force=force)

    def stop(self) -> None:
        self.controller.stop()
