"""5C1: κατάσταση Client για το tray/παράθυρο και τοπικό ιστορικό ανακοινώσεων.

Χωρίς Qt, ώστε να ελέγχεται παντού. Το δίκτυο/ήχος ενημερώνει το ``ClientStatus`` από
τα δικά του threads· το GUI ακούει αλλαγές (listener) και διαβάζει ``snapshot()``.
Εμφάνιση και κείμενα: docs/phase5.md §6, docs/ui-style.md §10.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from collections.abc import Callable

from client.install_paths import atomic_write_json
from shared.room_names import room_title, short_room

#: Μόνο για δοκιμές/ανάπτυξη: 1 = χωρίς tray, όπως πριν από το 5C1.
NO_TRAY_ENV = "SCH_INTERCOM_NO_TRAY"

# --- Καταστάσεις σύνδεσης ------------------------------------------------------------
SEARCHING = "searching"        # discovery, κανένας Manager ακόμη
CONNECTING = "connecting"      # TLS/HELLO σε εξέλιξη
CONNECTED = "connected"
PENDING = "pending"            # η ταυτότητα δεν είναι εγκεκριμένη, αίτημα σε αναμονή
UNAPPROVED = "unapproved"      # δεν είναι εγκεκριμένη και δεν υπάρχει (έγκυρο) αίτημα
REVOKED = "revoked"            # ήταν εγκεκριμένη (είδε μητρώο) και πλέον απορρίπτεται
INCOMPATIBLE = "incompatible"  # άλλο πρωτόκολλο ήχου· αναμονή ενημέρωσης
FULL = "full"                  # ο Manager έχει ήδη 70 υπολογιστές
ERROR = "error"                # π.χ. ξένος Manager, δύο Managers

LABELS = {
    SEARCHING: "Αναζήτηση Director…",
    CONNECTING: "Σύνδεση…",
    CONNECTED: "Συνδεδεμένος",
    PENDING: "Αναμονή έγκρισης",
    UNAPPROVED: "Χωρίς έγκριση",
    REVOKED: "Ανακλήθηκε",
    INCOMPATIBLE: "Ασύμβατη έκδοση",
    FULL: "Ο Director είναι γεμάτος",
    ERROR: "Σφάλμα σύνδεσης",
}
#: Χρώμα κουκκίδας/κειμένου: ok = πράσινο, wait = κίτρινο, bad = κόκκινο.
TONES = {
    SEARCHING: "wait", CONNECTING: "wait", CONNECTED: "ok", PENDING: "wait",
    FULL: "wait", UNAPPROVED: "bad", REVOKED: "bad", INCOMPATIBLE: "bad", ERROR: "bad",
}
NEW_COMPUTER = "Νέος υπολογιστής"

# --- Ενημέρωση (από τον Updater) ------------------------------------------------------
UPDATE_CURRENT = "current"
UPDATE_STATES = ("current", "downloading", "waiting", "ready", "installed", "failed")

WEEKDAYS = ("Δευτέρα", "Τρίτη", "Τετάρτη", "Πέμπτη", "Παρασκευή", "Σάββατο", "Κυριακή")


class ClientStatus:
    """Thread-safe κατάσταση· κάθε αλλαγή ειδοποιεί τους listeners (χωρίς polling)."""

    FIELDS = ("state", "since", "detail", "room", "name", "manager", "fingerprint",
              "version", "update", "volume", "mute_others", "device", "client_id",
              "enrollment", "live_since", "approved_before",
              # 5C2: κλίμακα παραθύρου (%) και αίτημα αλλαγής {room, name, state}.
              "scale", "change")

    def __init__(self, *, clock: Callable[[], float] = time.time, **initial):
        self.clock = clock
        self._lock = threading.Lock()
        self._listeners: list[Callable[[], None]] = []
        self._data: dict = dict.fromkeys(self.FIELDS)
        self._data.update(state=SEARCHING, since=clock(), detail="", update=None,
                          mute_others=False, approved_before=False)
        self._data.update(initial)
        self.changes = 0

    def add_listener(self, listener: Callable[[], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    def snapshot(self) -> dict:
        with self._lock:
            data = dict(self._data)
        if data["update"] is not None:
            data["update"] = dict(data["update"])
        if data["enrollment"] is not None:
            data["enrollment"] = dict(data["enrollment"])
        if data["change"] is not None:
            data["change"] = dict(data["change"])
        return data

    def update(self, **fields) -> bool:
        unknown = set(fields) - set(self.FIELDS)
        if unknown:
            raise KeyError(", ".join(sorted(unknown)))
        with self._lock:
            changed = {k: v for k, v in fields.items() if self._data.get(k) != v}
            if not changed:
                return False
            self._data.update(changed)
            self.changes += 1
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:  # noqa: BLE001 — το GUI δεν σταματά ποτέ το δίκτυο
                pass
        return True

    def set_state(self, state: str, detail: str = "") -> None:
        if state not in LABELS:
            raise ValueError(state)
        with self._lock:
            same = self._data["state"] == state
        fields = {"state": state, "detail": detail}
        if not same:
            fields["since"] = self.clock()
        if state == CONNECTED:
            fields["approved_before"] = True
        self.update(**fields)

    def set_update(self, state: str, version: str | None = None, detail: str = "",
                   progress: int | None = None) -> None:
        current = self.snapshot()["update"] or {}
        if state == "failed" and not detail and current.get("state") == "failed" \
                and current.get("version") == version:
            detail = current.get("detail", "")   # ίδια αποτυχία, χωρίς νέα αιτία
        self.update(update={"state": state, "version": version, "detail": detail,
                            "progress": progress})


# --- Κείμενα (ίδια σε tray, παράθυρο και live test) -----------------------------------
def tone(snap: dict) -> str:
    """Χρώμα κουκκίδας tray: κόκκινη και για αποτυχία ενημέρωσης (phase5 §6.1)."""
    base = TONES[snap["state"]]
    if base == "ok" and (snap.get("update") or {}).get("state") == "failed":
        return "bad"
    return base


def title(snap: dict) -> str:
    """«Γ1 · Board1»: η αίθουσα σύντομα για την τρέχουσα σχολική χρονιά (shared.room_names)."""
    if snap.get("room") and snap.get("name"):
        return room_title(snap["room"], snap["name"])
    return NEW_COMPUTER


def format_code(code: str) -> str:
    code = str(code)
    return f"{code[:3]} {code[3:]}" if len(code) == 6 else code


def status_label(snap: dict) -> str:
    return LABELS[snap["state"]]


def tooltip(snap: dict) -> str:
    text = f"{title(snap)} — {status_label(snap)}"
    enrollment = snap.get("enrollment")
    if snap["state"] == PENDING and enrollment and enrollment.get("code"):
        text += f", κωδικός {format_code(enrollment['code'])}"
    if (snap.get("update") or {}).get("state") == "failed":
        text += " · αποτυχία ενημέρωσης"
    return text


def menu_lines(snap: dict) -> list[str]:
    """Οι δύο γκρι γραμμές του μενού tray (ακολουθούν «Ιστορικό…» και «Πληροφορίες…»· χωρίς Έξοδο)."""
    return [title(snap), status_label(snap)]


def clock_text(when: float, *, now: float | None = None) -> str:
    """«08:02» σήμερα, αλλιώς «26/11 08:02»."""
    now = time.time() if now is None else now
    moment = time.localtime(when)
    if time.strftime("%Y%m%d", moment) == time.strftime("%Y%m%d", time.localtime(now)):
        return time.strftime("%H:%M", moment)
    return time.strftime("%d/%m %H:%M", moment)


def expiry_text(expires: float, *, now: float | None = None) -> str:
    """«Λήγει αύριο 21:50» / «Λήγει σήμερα 21:50» / «Λήγει 30/11 21:50»."""
    now = time.time() if now is None else now
    day = _day_index(expires) - _day_index(now)
    at = time.strftime("%H:%M", time.localtime(expires))
    if day == 0:
        return f"Λήγει σήμερα {at}"
    if day == 1:
        return f"Λήγει αύριο {at}"
    return f"Λήγει {time.strftime('%d/%m', time.localtime(expires))} {at}"


def short_fingerprint(fingerprint: str | None) -> str:
    """«84:28:5C:A1 … 7E:41:9D:33» από hex SHA-256 (mockup Πληροφορίες)."""
    if not fingerprint:
        return ""
    pairs = [fingerprint[i:i + 2].upper() for i in range(0, len(fingerprint), 2)]
    if len(pairs) <= 8:
        return ":".join(pairs)
    return ":".join(pairs[:4]) + " … " + ":".join(pairs[-4:])


def update_text(update: dict | None) -> tuple[str, str, str]:
    """(κείμενο, tone, αιτία) για τη γραμμή «Ενημέρωση»."""
    if not update:
        return "—", "muted", ""
    state, version = update.get("state"), update.get("version") or ""
    if state in (UPDATE_CURRENT, "installed"):
        return "Ενημερωμένη", "ok", ""
    if state == "downloading":
        progress = update.get("progress")
        suffix = f" · {progress}%" if isinstance(progress, int) else ""
        return f"Λήψη {version}{suffix}", "", ""
    if state == "waiting":
        return f"Λήψη {version} σε παύση", "muted", update.get("detail") or ""
    if state == "ready":
        return f"Έτοιμη η {version}· εναλλαγή μετά τις ανακοινώσεις", "", ""
    if state == "failed":
        return f"Αποτυχία ενημέρωσης {version}".rstrip(), "err", update.get("detail") or ""
    if state == "none":
        return "Χωρίς εγκατάσταση (ανάπτυξη)", "muted", ""
    return str(state), "", ""


def volume_text(snap: dict) -> str:
    volume = snap.get("volume")
    if volume is None:
        text = "Χωρίς αλλαγή έντασης"
    else:
        text = f"{volume}%"
    if snap.get("mute_others"):
        text += " · σίγαση άλλων ήχων"
    return text


def info_rows(snap: dict, *, now: float | None = None) -> list[dict]:
    """Γραμμές της καρτέλας «Πληροφορίες» (ετικέτα, τιμή, tone, mono, σημείωση)."""
    rows = [{"label": "Κατάσταση", "value": status_label(snap), "tone": tone_name(snap),
             "dot": True, "note": f"από {clock_text(snap['since'], now=now)}"
             if snap.get("since") else ""}]
    if snap.get("detail") and snap["state"] != CONNECTED:
        rows[0]["note"] = (rows[0]["note"] + " · " if rows[0]["note"] else "") + snap["detail"]
    rows.append({"label": "Αίθουσα", "value": short_room(snap.get("room")) or "—"})
    # 5C2: «Αλλαγή…» τέρμα δεξιά στη γραμμή του Υπολογιστή (δεν κρύβεται ποτέ) και
    # «Κλίμακα» ακριβώς πάνω από τον Director (αποφάσεις χρήστη 28/11/2026).
    rows.append({"label": "Υπολογιστής", "value": snap.get("name") or "—", "action": ACTION_CHANGE})
    rows.append({"label": "Κλίμακα", "value": scale_text(snap.get("scale")),
                 "action": ACTION_SCALE})
    rows.append({"label": "Director", "value": snap.get("manager") or "—"})
    if snap.get("fingerprint"):
        rows.append({"label": "Αποτύπωμα Director",
                     "value": short_fingerprint(snap["fingerprint"]), "mono": True})
    rows.append({"label": "Έκδοση", "value": snap.get("version") or "—"})
    if snap["state"] == CONNECTED or snap.get("update"):
        text, update_tone, reason = update_text(snap.get("update"))
        rows.append({"label": "Ενημέρωση", "value": text, "tone": update_tone, "note": reason})
    if snap["state"] == CONNECTED or snap.get("approved_before"):
        rows.append({"label": "Ένταση ανακοίνωσης", "value": volume_text(snap)})
        rows.append({"label": "Συσκευή ήχου", "value": snap.get("device") or "Προεπιλογή"})
    rows.append({"label": "ID υπολογιστή", "value": snap.get("client_id") or "—", "mono": True})
    for row in rows:
        row.setdefault("action", "")
        row.setdefault("tone", "")
        row.setdefault("mono", False)
        row.setdefault("note", "")
        row.setdefault("dot", False)
    return rows


ACTION_CHANGE = "change"
ACTION_SCALE = "scale"
CHANGE_BUTTON = "Αλλαγή…"


def scale_text(scale) -> str:
    return f"{scale}%" if isinstance(scale, int) else "—"


# --- 5C2: «Αλλαγή…» αίθουσας/ονόματος ------------------------------------------------------
#: Τι κάνει το «Αλλαγή…» ανάλογα με την κατάσταση (phase5.md §6.5).
MODE_CHANGE = "change"   # εγκεκριμένος: αίτημα αλλαγής στον Director, χωρίς κωδικό
MODE_AMEND = "amend"     # πριν από την πρώτη έγκριση: διόρθωση αιτήματος, ίδιος κωδικός
MODE_ENROLL = "enroll"   # ανακλήθηκε / χωρίς αίτημα: νέα αίτηση εγγραφής με νέο κωδικό
CHANGE_TITLE = "Αλλαγή αίθουσας ή ονόματος"
CHANGE_DIALOG = {
    MODE_CHANGE: ("Θα σταλεί αίτημα στον Director. Τα στοιχεία αλλάζουν μόνο όταν το εγκρίνει.",
                  "Αποστολή αιτήματος"),
    MODE_AMEND: ("Ο υπολογιστής περιμένει ακόμη την πρώτη έγκριση. Θα διορθωθεί το αίτημα "
                 "εγγραφής· ο κωδικός μένει ο ίδιος.", "Διόρθωση αιτήματος"),
    MODE_ENROLL: ("Θα σταλεί νέα αίτηση εγγραφής με νέα ταυτότητα και νέο κωδικό, που θα τον "
                  "πληκτρολογήσετε στον Director.", "Νέα αίτηση εγγραφής"),
}
SCALE_TITLE = "Κλίμακα"
SCALE_INTRO = "Μέγεθος κειμένων και στοιχείων σε αυτό το παράθυρο."
SCALE_NOTE = "Ισχύει αμέσως· η σύνδεση και οι ανακοινώσεις δεν διακόπτονται."
SCALE_APPLY = "Εφαρμογή"


def change_mode(snap: dict) -> str:
    if snap["state"] in (REVOKED, UNAPPROVED):
        return MODE_ENROLL
    if snap.get("enrollment"):
        return MODE_AMEND
    if snap.get("approved_before") or snap["state"] == CONNECTED:
        return MODE_CHANGE
    return MODE_ENROLL


def _html(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def window_bars(snap: dict) -> tuple[str | None, str | None]:
    """(κόκκινη, μπλε) μπάρα του παραθύρου (rich text)."""
    state = snap["state"]
    if state == REVOKED:
        return ("Ο υπολογιστής ανακλήθηκε στον Director και δεν λαμβάνει ανακοινώσεις. "
                "Για να ξανασυνδεθεί: «Αλλαγή…» στον Υπολογιστή, για νέα αίτηση εγγραφής."), None
    if state == UNAPPROVED:
        return ("Ο υπολογιστής δεν έχει εγκριθεί και δεν υπάρχει αίτημα εγγραφής σε αναμονή. "
                "«Αλλαγή…» στον Υπολογιστή για νέα αίτηση εγγραφής."), None
    if state == INCOMPATIBLE:
        return ("Η έκδοση δεν είναι συμβατή με τον Director· αναμονή ενημέρωσης από τον "
                "Director."), None
    if state == ERROR:
        return "Δεν έγινε σύνδεση: " + _html(snap.get("detail") or "άγνωστο σφάλμα"), None
    change = snap.get("change") or {}
    target = f"<b>{_html(room_title(change.get('room', ''), change.get('name', '')))}</b>"
    if change.get("state") == "denied":
        return (f"Η αλλαγή σε {target} απορρίφθηκε από τον Director. "
                "Τα στοιχεία έμειναν ως είχαν."), None
    if change.get("state") == "failed":
        return (f"Η αλλαγή σε {target} δεν στάλθηκε: "
                + _html(change.get("detail") or "άγνωστο σφάλμα")), None
    info = None
    if state == FULL:
        info = ("Ο Director έχει συμπληρώσει το όριο υπολογιστών· νέα προσπάθεια "
                "κάθε 30 δευτερόλεπτα.")
    elif change.get("state") == "pending":
        info = f"Αίτημα αλλαγής σε {target}: αναμονή έγκρισης από τον Director"
    elif change.get("state") == "queued":
        info = f"Αίτημα αλλαγής σε {target}: θα σταλεί στον Director στην επόμενη σύνδεση"
    return None, info


def tone_name(snap: dict) -> str:
    """tone της κατάστασης (όχι της κουκκίδας tray): ok/wait/bad."""
    return TONES[snap["state"]]


# --- Αίτημα πρώτης εγκατάστασης (κωδικός) ----------------------------------------------
PENDING_FILE = "enrollment-pending.json"


def save_pending(path: Path, *, code: str, room: str, name: str, expires_at: float,
                 client_id: str, request_id: str | None = None, seq: int = 0) -> None:
    """5C2: και ``request_id``/``seq`` για τη διόρθωση του αιτήματος (ίδιος κωδικός)."""
    atomic_write_json(path, {"code": str(code), "room": room, "name": name,
                             "expires_at": float(expires_at), "client_id": client_id,
                             "request_id": request_id, "seq": int(seq)})


def load_pending(path: Path, client_id: str, *, now: float | None = None) -> dict | None:
    """Το αίτημα που περιμένει, αν ανήκει σε αυτή την ταυτότητα και δεν έληξε."""
    now = time.time() if now is None else now
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("client_id") != client_id:
        return None
    code, expires = data.get("code"), data.get("expires_at")
    if not isinstance(code, str) or not code.isdigit() or \
            not isinstance(expires, (int, float)) or expires <= now:
        return None
    request_id, seq = data.get("request_id"), data.get("seq")
    return {"code": code, "room": str(data.get("room") or ""),
            "name": str(data.get("name") or ""), "expires_at": float(expires),
            "request_id": request_id if isinstance(request_id, str) else None,
            "seq": seq if type(seq) is int and seq >= 0 else 0}


def clear_pending(path: Path) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


# --- Ιστορικό (history.jsonl) ----------------------------------------------------------
HISTORY_FILE = "history.jsonl"
HISTORY_DAYS = 30
HISTORY_VERSION = 1
KIND_LIVE = "live"
#: Γνωστά είδη· άγνωστο είδος (νεότερης έκδοσης) δείχνεται γενικά με τον τίτλο του.
KINDS = {KIND_LIVE: "Ζωντανή ανακοίνωση"}
GENERIC_KIND = "Ανακοίνωση"


class History:
    """Μία γραμμή JSON ανά γεγονός: ``{"v":1,"kind":"live","start":..,"end":..}``.

    Επεκτάσιμο (απόφαση χρήστη 28/11/2026): προαιρετικά ``title``, ``text``, ``source``
    και άγνωστα πεδία διατηρούνται στον καθαρισμό· άγνωστο ``kind`` δείχνεται γενικά.
    Μόνο τοπικά· δεν στέλνεται στον Manager.
    """

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time,
                 days: int = HISTORY_DAYS):
        self.path = Path(path)
        self.clock = clock
        self.days = days
        self._lock = threading.Lock()
        self.events: list[dict] = []
        self.current: dict | None = None

    def load(self, *, prune: bool = True) -> "History":
        """Διαβάζει το αρχείο· με ``prune`` σβήνει όσα είναι παλαιότερα των 30 ημερών."""
        events, dropped = [], 0
        cutoff = self.clock() - self.days * 86400
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError, UnicodeError):
            lines = []
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                dropped += 1
                continue
            if not isinstance(event, dict) or not isinstance(event.get("start"), (int, float)):
                dropped += 1
                continue
            if event["start"] < cutoff:
                dropped += 1
                continue
            events.append(event)
        with self._lock:
            self.events = sorted(events, key=lambda item: item["start"])
        if prune and dropped:
            self._rewrite()
        return self

    def _rewrite(self) -> None:
        with self._lock:
            text = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in self.events)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(self.path.name + ".tmp")
            temp.write_text(text, encoding="utf-8")
            os.replace(temp, self.path)
        except OSError:
            pass

    def begin(self, kind: str = KIND_LIVE) -> None:
        with self._lock:
            if self.current is None:
                self.current = {"v": HISTORY_VERSION, "kind": kind, "start": self.clock()}

    def end(self) -> dict | None:
        with self._lock:
            event, self.current = self.current, None
            if event is None:
                return None
            event["end"] = max(event["start"], self.clock())
            self.events.append(event)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return event

    def all(self) -> list[dict]:
        with self._lock:
            events = [dict(event) for event in self.events]
            if self.current is not None:
                events.append(dict(self.current))
        return events


def kind_label(event: dict) -> str:
    if event.get("kind") in KINDS:
        return KINDS[event["kind"]]
    return str(event.get("title") or GENERIC_KIND)


def duration_text(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def _day_index(when: float) -> int:
    moment = time.localtime(when)
    return int(time.mktime((moment.tm_year, moment.tm_mon, moment.tm_mday, 12, 0, 0, 0, 0,
                            -1)) // 86400)


def day_heading(when: float, *, now: float | None = None) -> str:
    now = time.time() if now is None else now
    moment = time.localtime(when)
    date = f"{WEEKDAYS[moment.tm_wday]} {moment.tm_mday}/{moment.tm_mon}"
    diff = _day_index(now) - _day_index(when)
    if diff == 0:
        return f"Σήμερα, {date}"
    if diff == 1:
        return f"Χθες, {date}"
    return date


def history_groups(events: list[dict], *, now: float | None = None) -> list[dict]:
    """Ανά ημέρα, νεότερα πρώτα: [{"day": "Σήμερα, …", "rows": [{time, kind, duration,
    live, title, text}]}]. Η τρέχουσα (χωρίς ``end``) έχει ``live`` True."""
    now = time.time() if now is None else now
    groups: list[dict] = []
    for event in sorted(events, key=lambda item: item["start"], reverse=True):
        heading = day_heading(event["start"], now=now)
        if not groups or groups[-1]["day"] != heading:
            groups.append({"day": heading, "rows": []})
        live = not isinstance(event.get("end"), (int, float))
        groups[-1]["rows"].append({
            "time": time.strftime("%H:%M", time.localtime(event["start"])),
            "kind": kind_label(event),
            "known": event.get("kind") in KINDS,
            "duration": "" if live else duration_text(event["end"] - event["start"]),
            "live": live,
            "text": str(event.get("text") or ""),
        })
    return groups


EMPTY_HISTORY = "Καμία ανακοίνωση τις τελευταίες 30 ημέρες"


def tray_supported(environ=None, platform: str | None = None) -> bool:
    """Υπάρχει γραφικό περιβάλλον; (αλλιώς ο Client τρέχει στην κονσόλα όπως πριν)."""
    import sys
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    if environ.get(NO_TRAY_ENV) == "1":
        return False
    if platform.startswith("win"):
        return True
    return any(environ.get(name) for name in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM"))
