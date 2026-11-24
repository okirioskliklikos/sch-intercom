"""Αρχείο μετρήσεων ΜΟΝΟ για τις αυτόματες πραγματικές δοκιμές 4.4 (κρυφό ``--test-report``).

Μία γραμμή JSON ανά γεγονός, με χρόνο ``t`` σε μs του ``time.monotonic`` του
υπολογιστή (κοινό ρολόι για όλες τις διεργασίες του ίδιου υπολογιστή). Το νήμα ήχου
μόνο προσθέτει σε λίστα· η εγγραφή στον δίσκο γίνεται από ξεχωριστό νήμα, ώστε η
μέτρηση να μην καθυστερεί την αναπαραγωγή. Χωρίς ``--test-report`` δεν υπάρχει
κανένα αντικείμενο και ο κώδικας ήχου δεν αλλάζει συμπεριφορά.
"""
from __future__ import annotations

import json
from pathlib import Path
import threading
import time

FLUSH_SECONDS = 0.5


def now_us() -> int:
    return time.monotonic_ns() // 1000


class TestReport:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8")
        self._pending: list[dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="test-report")
        self._thread.start()

    def event(self, ev: str, *, t: int | None = None, **fields) -> None:
        item = {"ev": ev, "t": now_us() if t is None else int(t), **fields}
        with self._lock:
            self._pending.append(item)

    def flush(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, []
        if pending:
            self._file.write("".join(json.dumps(item, separators=(",", ":")) + "\n"
                                     for item in pending))
            self._file.flush()

    def _run(self) -> None:
        while not self._stop.wait(FLUSH_SECONDS):
            try:
                self.flush()
            except OSError:
                return

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        try:
            self.flush()
        finally:
            self._file.close()


def read_report(path: Path) -> list[dict]:
    """Όλα τα γεγονότα· μισή τελευταία γραμμή (διεργασία που σκοτώθηκε) αγνοείται."""
    events = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return events
    for line in text.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events
