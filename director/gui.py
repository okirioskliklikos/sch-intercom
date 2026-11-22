"""Λογική της κεντρικής οθόνης Director, ανεξάρτητη από Qt. Χωρίς αλλαγή πρωτοκόλλου.

Το παράθυρο PySide6 βρίσκεται στο `director/qt_gui.py` και καλεί μόνο τις
κλάσεις αυτού του αρχείου, ώστε η συμπεριφορά (στόχοι, μικρόφωνο, Mute,
προεπισκόπηση, σφάλματα) να ελέγχεται χωρίς οθόνη και χωρίς κάρτα ήχου.

Μικρόφωνο (development.md §8, requirements.md §9):
- Στην αναμονή η συσκευή είναι κλειστή. Ανοίγει μόνο για ζωντανή ανακοίνωση ή
  προεπισκόπηση, και ποτέ για προεπισκόπηση όσο ισχύει Mute.
- Mute κατά την ανακοίνωση: η συσκευή μένει ανοιχτή, απλώς δεν στέλνεται φωνή
  (χωρίς STREAM_STOP). Mute εκτός ανακοίνωσης κλείνει τη συσκευή.
- Σφάλμα μικροφώνου σταματά την ανακοίνωση/προεπισκόπηση και εμφανίζεται.
"""
from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable

from director.broadcast import AudioBroadcaster

#: Στάθμη παλαιότερη από αυτό θεωρείται σιωπή (ο μετρητής μηδενίζεται).
LEVEL_STALE_SECONDS = 0.3
#: Κλίμακα μετρητή: -48 dBFS (κενός) έως 0 dBFS (γεμάτος).
METER_FLOOR_DB = -48.0

MODE_ALL = "all"
MODE_ROOMS = "rooms"


class AnnouncementControls:
    """Ελάχιστος, ελέγξιμος χειρισμός στόχων και toggle χωρίς dependency GUI."""

    def __init__(self, broadcaster: AudioBroadcaster):
        self.broadcaster = broadcaster
        self.active = False
        self.target_rooms: set[str] | None = None
        # Κρίσιμο: ο broadcaster επιλέγει εξ ορισμού *όλους*.
        # Το GUI ΠΟΤΕ δεν ξεκινά με ενεργή ανακοίνωση.
        self.broadcaster.set_targets(rooms=set(), client_ids=set())

    def start(self, rooms: set[str] | None = None) -> None:
        if self.active:
            return
        if rooms is not None and not rooms:
            raise ValueError("Επίλεξε τουλάχιστον μία αίθουσα.")
        # Η επιλογή κατά την ανακοίνωση παραμένει σταθερή μέχρι τη λήξη.
        self.broadcaster.set_targets(rooms=rooms, client_ids=set() if rooms is not None else None)
        self.target_rooms = None if rooms is None else set(rooms)
        self.active = True

    def stop(self) -> None:
        # Idempotent: ασφαλής κλήση και κατά το κλείσιμο του παραθύρου.
        self.broadcaster.set_targets(rooms=set(), client_ids=set())
        self.active = False
        self.target_rooms = None


def summarize_rooms(status):
    """Ταυτόχρονοι Clients στην ίδια αίθουσα μετρώνται ξεχωριστά."""
    rooms = {}
    for client_id, room, online, age in status:
        counts = rooms.setdefault(room, [0, 0])
        counts[0 if online else 1] += 1
    return [(room, online, offline) for room, (online, offline) in sorted(rooms.items())]


def room_states(status) -> list[tuple[str, bool]]:
    """(αίθουσα, online). Αρκεί ένας online Client για online αίθουσα."""
    return [(room, online > 0) for room, online, _ in summarize_rooms(status)]


def choose_targets(mode: str, checked: set[str], status) -> set[str] | None:
    """Στόχοι για `AnnouncementControls.start`· ValueError με μήνυμα για τον χρήστη."""
    online_rooms = {room for room, online in room_states(status) if online}
    if not online_rooms:
        raise ValueError("Δεν υπάρχει συνδεδεμένος Client.")
    if mode == MODE_ALL:
        return None
    if mode != MODE_ROOMS:
        raise ValueError("Μη διαθέσιμη επιλογή αποδεκτών.")
    if not checked:
        raise ValueError("Επίλεξε τουλάχιστον μία αίθουσα.")
    if not checked & online_rooms:
        raise ValueError("Καμία επιλεγμένη αίθουσα δεν έχει online Client.")
    return set(checked)


def room_grid_shape(count: int, rows_fit: int, max_columns: int) -> tuple[int, int]:
    """(γραμμές ανά στήλη, στήλες) στην κάρτα αιθουσών (ui-style.md §5).

    Κάθε στήλη γεμίζει όσο χωρά στο ύψος (`rows_fit`) και μόνο τα υπόλοιπα
    πάνε στην επόμενη, έως `max_columns` στήλες (όσες χωρούν στο πλάτος).
    Αν δεν χωρούν ούτε τότε, μοιράζονται ισόποσα και η λίστα κυλά κατακόρυφα.
    """
    if count <= 0:
        return 0, 0
    rows_fit = max(1, rows_fit)
    max_columns = max(1, max_columns)
    if count <= rows_fit * max_columns:
        return min(count, rows_fit), -(-count // rows_fit)
    return -(-count // max_columns), max_columns


def meter_fraction(peak: float) -> float:
    """Κορυφή 0..1 → 0..1 σε λογαριθμική κλίμακα, όπως ακούγεται η φωνή."""
    if peak <= 0:
        return 0.0
    db = 20 * math.log10(min(1.0, peak))
    return max(0.0, min(1.0, 1 - db / METER_FLOOR_DB))


def _now_us() -> int:
    # Ίδιο ρολόι με το timestamp λήψης του director.main.stream_microphone.
    return time.monotonic_ns() // 1000


class VoiceGate:
    """Μπαίνει στη θέση του broadcaster για το μικρόφωνο.

    Περνά μπλοκ μόνο όταν είναι ανοιχτή, και μόνο αν το μπλοκ καταγράφηκε
    *μετά* το τελευταίο άνοιγμα: ήχος της προεπισκόπησης ή του Mute που
    περίμενε στην ουρά του μικροφώνου δεν στέλνεται ποτέ. Το κλείσιμο κρατά
    το ίδιο lock με το publish, άρα μετά το `close()` δεν περνά κανένα μπλοκ.
    """

    def __init__(self, broadcaster, clock_us: Callable[[], int] = _now_us):
        self.broadcaster = broadcaster
        self._clock_us = clock_us
        self._lock = threading.Lock()
        self._open = False
        self._since_us = 0

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._open

    def open(self) -> None:
        with self._lock:
            self._open = True
            self._since_us = self._clock_us()

    def close(self) -> None:
        with self._lock:
            self._open = False

    def publish(self, pcm: bytes, captured_us: int) -> None:
        with self._lock:
            if self._open and captured_us >= self._since_us:
                self.broadcaster.publish(pcm, captured_us)


class _MicSession:
    def __init__(self, previous: "_MicSession | None"):
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.previous = previous
        self.thread: threading.Thread | None = None


class DirectorMicrophone:
    """Ανακοίνωση, προεπισκόπηση και Mute. Οι μέθοδοι καλούνται από το GUI thread.

    Το worker thread του μικροφώνου επικοινωνεί μόνο μέσω lock/Events· το GUI
    καλεί `poll()` περιοδικά για να δει σφάλματα.
    """

    def __init__(self, stream_microphone, broadcaster, *,
                 controls: AnnouncementControls | None = None,
                 clock: Callable[[], float] = time.monotonic, **stream_kwargs):
        self._stream = stream_microphone
        self.controls = controls or AnnouncementControls(broadcaster)
        self.gate = VoiceGate(broadcaster)
        self._stream_kwargs = stream_kwargs
        self._clock = clock
        self._lock = threading.Lock()
        self._session: _MicSession | None = None
        self._last: _MicSession | None = None
        self._failure: tuple[_MicSession, str] | None = None
        self._peak = (0.0, -math.inf)
        self.muted = False
        self.preview = False
        self.error: str | None = None

    # --- κατάσταση ---------------------------------------------------------
    @property
    def live(self) -> bool:
        return self.controls.active

    @property
    def device_open(self) -> bool:
        """Ζητήθηκε ανοιχτή συσκευή (μπορεί να ανοίγει ακόμη)."""
        return self._session is not None

    @property
    def ready(self) -> bool:
        session = self._session
        return session is not None and session.ready.is_set()

    @property
    def can_start(self) -> bool:
        return not self.live and not self.muted

    @property
    def can_preview(self) -> bool:
        return not self.live and not self.muted

    def level(self) -> float:
        """Κορυφή 0..1 μόνο όταν υπάρχει ενεργή, μη σιγασμένη λήψη· αλλιώς 0."""
        if self.muted or not self.ready:
            return 0.0
        with self._lock:
            value, when = self._peak
        if self._clock() - when > LEVEL_STALE_SECONDS:
            return 0.0
        return value

    # --- ενέργειες ---------------------------------------------------------
    def start_announcement(self, rooms: set[str] | None) -> None:
        if self.live:
            return
        if self.muted:
            raise ValueError("Η σίγαση είναι ενεργή. Άρση σίγασης για έναρξη.")
        self.controls.start(rooms)   # ValueError: τίποτα δεν άλλαξε
        self.preview = False         # η έναρξη σταματά την προεπισκόπηση
        self.gate.open()
        self._sync()

    def stop_announcement(self) -> None:
        # Πρώτα κλείνει η πύλη, μετά STREAM_STOP: κανένα PCM μετά τη λήξη.
        self.gate.close()
        self.controls.stop()
        self._sync()

    def set_preview(self, enabled: bool) -> None:
        if enabled and not self.can_preview:
            return
        self.preview = enabled
        self._sync()

    def set_muted(self, muted: bool) -> None:
        self.muted = muted
        if self.live:
            # Η συσκευή μένει ανοιχτή· κόβεται μόνο η φωνή, χωρίς λήξη.
            if muted:
                self.gate.close()
            else:
                self.gate.open()
        elif muted:
            self.preview = False
        self._sync()

    def poll(self) -> str | None:
        """Σφάλμα του τρέχοντος ανοίγματος, μία φορά· σταματά ό,τι το χρειαζόταν."""
        with self._lock:
            failure, self._failure = self._failure, None
        if failure is None or failure[0] is not self._session:
            return None
        _, message = failure
        self.error = message
        self._close()
        self.preview = False
        if self.live:
            self.gate.close()
            self.controls.stop()
        return message

    def shutdown(self) -> None:
        self.gate.close()
        self.controls.stop()
        self.preview = False
        self._close()
        last = self._last
        if last is not None and last.thread is not None:
            last.thread.join(timeout=1)

    # --- εσωτερικά ---------------------------------------------------------
    def _wanted(self) -> bool:
        return self.live or (self.preview and not self.muted)

    def _sync(self) -> None:
        if self._wanted() and self._session is None:
            self._open()
        elif not self._wanted() and self._session is not None:
            self._close()

    def _open(self) -> None:
        session = _MicSession(self._last)
        self._session = self._last = session
        session.thread = threading.Thread(target=self._run, args=(session,), daemon=True,
                                          name="director-gui-microphone")
        session.thread.start()

    def _close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            session.stop.set()

    def _run(self, session: _MicSession) -> None:
        # Το προηγούμενο άνοιγμα κλείνει πρώτα: ποτέ δύο streams μαζί, και το
        # GUI thread δεν περιμένει ποτέ τη συσκευή.
        previous, session.previous = session.previous, None
        if previous is not None and previous.thread is not None:
            previous.thread.join()
        if session.stop.is_set():
            return

        def on_ready() -> None:
            session.ready.set()

        def on_level(value: float) -> None:
            with self._lock:
                self._peak = (value, self._clock())

        try:
            self._stream(self.gate, stop=session.stop, ready_callback=on_ready,
                         level_callback=on_level, **self._stream_kwargs)
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
        else:
            if session.stop.is_set():
                return
            message = "Η λήψη μικροφώνου σταμάτησε απροσδόκητα."
        finally:
            session.ready.clear()
        with self._lock:
            self._failure = (session, message)

    def clear_error_if_ready(self) -> None:
        """Το σφάλμα σβήνει μόλις η συσκευή ανοίξει ξανά επιτυχώς."""
        if self.error and self.ready:
            self.error = None


def run_gui(args, *, accept_clients, stream_microphone, **security) -> None:
    """Σημείο εισόδου του `director --gui`. Το PySide6 φορτώνεται μόνο εδώ."""
    try:
        from director.qt_gui import run_qt_gui
    except ImportError as exc:
        raise SystemExit(
            "Το --gui χρειάζεται PySide6: pip install -e .[gui]  "
            f"({exc})") from exc
    run_qt_gui(args, accept_clients=accept_clients, stream_microphone=stream_microphone,
               **security)
