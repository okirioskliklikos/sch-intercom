"""Ουρά αναπαραγωγής με όριο και μετρήσεις καθυστέρησης.

Ο Client παίζει πάντα με σταθερό ρυθμό. Αν ο ήχος φτάνει καθυστερημένα και
μετά «μαζεμένος», μια ουρά χωρίς όριο κρατά για πάντα την επιπλέον
καθυστέρηση. Εδώ η ουρά έχει μέγιστο μέγεθος και πετάει τα παλιότερα blocks.
"""

import threading
from collections import deque


class JitterBuffer:
    """Thread-safe ουρά blocks σταθερού μεγέθους.

    max_blocks: μέγιστος αριθμός blocks σε αναμονή· τα παλιότερα πετιούνται.
    prefill_blocks: μετά από άδειασμα, πόσα blocks περιμένουμε πριν συνεχίσει η
    αναπαραγωγή (ώστε να μην εναλλάσσονται συνεχώς ήχος και σιωπή).
    """

    def __init__(self, max_blocks: int, block_bytes: int, prefill_blocks: int | None = None):
        if max_blocks < 1:
            raise ValueError("max_blocks πρέπει να είναι τουλάχιστον 1")
        self.max_blocks = max_blocks
        self.prefill_blocks = max(1, min(max_blocks, prefill_blocks if prefill_blocks else (max_blocks + 1) // 2))
        self._silence = bytes(block_bytes)
        self._blocks: deque[bytes] = deque()
        self._lock = threading.Lock()
        self._filling = True
        self.dropped = 0
        self.underruns = 0
        self._levels: list[int] = []

    def __len__(self) -> int:
        with self._lock:
            return len(self._blocks)

    def push(self, block: bytes) -> None:
        with self._lock:
            self._blocks.append(block)
            while len(self._blocks) > self.max_blocks:
                self._blocks.popleft()
                self.dropped += 1

    def pop(self) -> bytes:
        """Επιστρέφει το επόμενο block ή σιωπή αν η ουρά δεν έχει αρκετά δεδομένα."""
        with self._lock:
            self._levels.append(len(self._blocks))
            if self._filling:
                if len(self._blocks) < self.prefill_blocks:
                    return self._silence
                self._filling = False
            if not self._blocks:
                self.underruns += 1
                self._filling = True
                return self._silence
            return self._blocks.popleft()

    def take_level_stats(self) -> tuple[int, float, int] | None:
        """(ελάχιστο, μέσο, μέγιστο) πλήθος blocks σε αναμονή από την τελευταία κλήση."""
        with self._lock:
            levels, self._levels = self._levels, []
        if not levels:
            return None
        return min(levels), sum(levels) / len(levels), max(levels)


class DelayTracker:
    """Μετρά την επιπλέον καθυστέρηση δικτύου σε σχέση με την καλύτερη που έχει παρατηρηθεί.

    Τα ρολόγια Director και Client δεν είναι συγχρονισμένα, οπότε η απόλυτη
    διαφορά δεν σημαίνει τίποτα. Η διαφορά από το ελάχιστο όμως δείχνει πόσο
    αργότερα από το «καλύτερο δυνατό» έφτασε κάθε πακέτο.
    """

    def __init__(self) -> None:
        self._best_offset_us: int | None = None
        self._extra_us: list[int] = []
        self.gaps = 0
        self._last_seq: int | None = None

    def record(self, seq: int, sent_us: int, received_us: int) -> None:
        if self._last_seq is not None and seq != (self._last_seq + 1) & 0xFFFFFFFF:
            self.gaps += 1
        self._last_seq = seq
        offset = received_us - sent_us
        if self._best_offset_us is None or offset < self._best_offset_us:
            self._best_offset_us = offset
        self._extra_us.append(offset - self._best_offset_us)

    def take_stats(self) -> tuple[float, float] | None:
        """(μέση, μέγιστη) επιπλέον καθυστέρηση σε ms από την τελευταία κλήση."""
        extra, self._extra_us = self._extra_us, []
        if not extra:
            return None
        return sum(extra) / len(extra) / 1000, max(extra) / 1000
