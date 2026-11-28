"""5C2: το αίτημα αλλαγής αίθουσας/ονόματος από την πλευρά του Client (χωρίς Qt).

Ένα αίτημα ανά υπολογιστή (``change-request.json``): ``queued`` μέχρι να σταλεί,
``pending`` όταν ο Director το κράτησε, ``denied`` όταν απορρίφθηκε (ορατό μία ημέρα).
Η αποστολή γίνεται από το νήμα heartbeat (``outbox``), ώστε να μη γράφουν δύο νήματα
στο ίδιο TLS socket. Η απάντηση του Director (``MESSAGE_CHANGE_STATUS``) έρχεται στη
σύνδεση και μετά από κάθε αίτημα· με «none» ο Client ξαναστέλνει ό,τι περιμένει ακόμη.

Το ``change-inbox.json`` είναι για τη γραμμή εντολών (``client.main --request-change``,
εγκαταστάτης): ο Client που τρέχει το διαβάζει, το σβήνει και το χειρίζεται όπως το
«Αλλαγή…» του παραθύρου.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import threading
import time
from collections.abc import Callable

from shared import change_request as cr
from shared.control import encode_change_request

INBOX_FILE = "change-inbox.json"
#: Κάθε πόσο ο Client κοιτάζει το inbox της γραμμής εντολών (ένα stat, όχι animation).
INBOX_POLL_SECONDS = 2.0
#: Κατάσταση μόνο για την οθόνη: η αποστολή/διόρθωση απέτυχε (δεν αποθηκεύεται).
FAILED = "failed"


class ChangeController:
    """Συνδέει το τοπικό αρχείο, την κατάσταση του παραθύρου και τη σύνδεση."""

    def __init__(self, path: Path, client_id: str, *, status=None,
                 log: Callable[[str], None] = lambda line: print(line, flush=True),
                 clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.client_id = client_id
        self.status = status
        self.log = log
        self.clock = clock
        self._lock = threading.Lock()
        self._outbox: queue.Queue | None = None
        self._current: tuple[str, str] | None = None
        self.local = cr.load_local(self.path, client_id, now=clock())
        self._publish()

    # --- κατάσταση ---------------------------------------------------------------
    def _publish(self, extra: dict | None = None) -> None:
        if self.status is None:
            return
        value = None
        if extra is not None:
            value = extra
        elif self.local is not None:
            value = {"room": self.local["room"], "name": self.local["name"],
                     "state": self.local["state"]}
        self.status.update(change=value)

    def _store(self, room: str, name: str, state: str) -> None:
        try:
            self.local = cr.save_local(self.path, self.client_id, room, name, state,
                                       at=self.clock())
        except OSError as exc:
            self.log(f"[Client] Δεν αποθηκεύτηκε το αίτημα αλλαγής: {exc}")
            self.local = {"room": room, "name": name, "state": state, "at": self.clock()}

    def _clear(self) -> None:
        self.local = None
        try:
            cr.clear_local(self.path)
        except OSError as exc:
            self.log(f"[Client] Δεν σβήστηκε το αίτημα αλλαγής: {exc}")

    def reset(self, client_id: str) -> None:
        """Νέα ταυτότητα: το αίτημα της παλιάς δεν ισχύει πια."""
        with self._lock:
            self.client_id = client_id
            self._clear()
            self._current = None
        self._publish()

    def fail(self, room: str, name: str, detail: str) -> None:
        """Αποτυχία διόρθωσης/νέας εγγραφής: μόνο στην οθόνη, μέχρι την επόμενη ενέργεια."""
        self._publish({"room": room, "name": name, "state": FAILED, "detail": detail})

    # --- από το παράθυρο / τη γραμμή εντολών ------------------------------------------
    def request(self, room: str, name: str) -> str:
        """Νέο αίτημα (αντικαθιστά το προηγούμενο). Επιστρέφει queued/pending ή approved."""
        room, name = cr.check_room(room), cr.check_name(name)
        with self._lock:
            if self._current == (room, name):
                had = self.local is not None
                self._clear()
                outbox = self._outbox
                if had and outbox is not None:   # ακύρωση στον Director: ίδιες τιμές
                    outbox.put(encode_change_request(room, name))
                result = cr.APPROVED
            else:
                self._store(room, name, cr.QUEUED)
                self._send_locked()
                result = cr.QUEUED
        self._publish()
        self.log(f"[Client] Αίτημα αλλαγής σε {room} · {name}"
                 + (" (ίδια με τα τρέχοντα)" if result == cr.APPROVED else ""))
        return result

    def _send_locked(self) -> None:
        if self._outbox is None or self.local is None or \
                self.local["state"] not in (cr.QUEUED, cr.PENDING):
            return
        self._outbox.put(encode_change_request(self.local["room"], self.local["name"]))

    # --- από τη σύνδεση --------------------------------------------------------------
    def set_current(self, room: str | None, name: str | None) -> None:
        """Τα στοιχεία που έστειλε ο Director (CLIENT_SETTINGS)."""
        with self._lock:
            self._current = (room, name) if room and name else None

    def connected(self, outbox: queue.Queue) -> None:
        """Η απάντηση του Director στη σύνδεση αποφασίζει αν θα σταλεί (``on_status``)."""
        with self._lock:
            self._outbox = outbox

    def disconnected(self) -> None:
        with self._lock:
            self._outbox = None

    def on_status(self, status: dict) -> None:
        state, room, name = status["state"], status["room"], status["name"]
        with self._lock:
            local = self.local
            same = local is not None and (local["room"], local["name"]) == (room, name)
            if state == cr.PENDING and same:
                if local["state"] != cr.PENDING:
                    self._store(room, name, cr.PENDING)
            elif state == cr.DENIED and same:
                if local["state"] != cr.DENIED:
                    self._store(room, name, cr.DENIED)
                    self.log(f"[Client] Ο Director απέρριψε την αλλαγή σε {room} · {name}")
            elif state == cr.APPROVED and same:
                self._clear()
                self.log(f"[Client] Ο Director ενέκρινε την αλλαγή σε {room} · {name}")
            elif local is None:
                pass
            elif local["state"] == cr.DENIED:
                if state == cr.NONE:
                    self._clear()   # ο Director ξέχασε την απόρριψη (μία ημέρα)
            else:
                # «none» ή απάντηση για παλιότερες τιμές: ξανά το τρέχον αίτημα.
                self._send_locked()
        self._publish()

    # --- inbox γραμμής εντολών -------------------------------------------------------
    @staticmethod
    def write_inbox(folder: Path, client_id: str, room: str, name: str) -> Path:
        room, name = cr.check_room(room), cr.check_name(name)
        path = Path(folder) / INBOX_FILE
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps({"client_id": client_id, "room": room, "name": name},
                                        ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
        return path

    @staticmethod
    def take_inbox(folder: Path, client_id: str) -> tuple[str, str] | None:
        """Διαβάζει και σβήνει το inbox· None αν δεν υπάρχει ή δεν είναι γι' αυτή την ταυτότητα."""
        path = Path(folder) / INBOX_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValueError):
            data = None
        try:
            path.unlink()
        except OSError:
            pass
        if not isinstance(data, dict) or data.get("client_id") != client_id:
            return None
        try:
            return cr.check_room(data["room"]), cr.check_name(data["name"])
        except (KeyError, TypeError, ValueError):
            return None


def watch_inbox(folder: Path, client_id: Callable[[], str],
                handle: Callable[[str, str], None], stop: threading.Event, *,
                interval: float = INBOX_POLL_SECONDS) -> None:
    """Νήμα: κάθε ``interval`` ένα stat στο inbox· το περιεχόμενο πάει στο ``handle``."""
    path = Path(folder) / INBOX_FILE
    while not stop.is_set():
        if path.exists():
            item = ChangeController.take_inbox(folder, client_id())
            if item is not None:
                try:
                    handle(*item)
                except Exception as exc:  # noqa: BLE001 — το νήμα δεν σταματά
                    print(f"[Client] Αποτυχία αιτήματος αλλαγής: {exc}", flush=True)
        stop.wait(interval)


__all__ = ["ChangeController", "FAILED", "INBOX_FILE", "watch_inbox"]
