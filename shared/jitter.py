"""Ουρά αναπαραγωγής με όριο και μετρήσεις καθυστέρησης.

Ο Client παίζει πάντα με σταθερό ρυθμό. Αν ο ήχος φτάνει καθυστερημένα και
μετά «μαζεμένος», μια ουρά χωρίς όριο κρατά για πάντα την επιπλέον
καθυστέρηση. Εδώ η ουρά έχει μέγιστο μέγεθος και πετάει τα παλιότερα blocks.
"""

import threading
from collections import deque

from shared.audio import apply_gain_ramp


class JitterBuffer:
    """Thread-safe ουρά blocks σταθερού μεγέθους.

    max_blocks: μέγιστος αριθμός blocks σε αναμονή· τα παλιότερα πετιούνται.
    prefill_blocks: μετά από άδειασμα, πόσα blocks περιμένουμε πριν συνεχίσει η
    αναπαραγωγή (ώστε να μην εναλλάσσονται συνεχώς ήχος και σιωπή).
    fade_in_samples / fade_out_samples: γρήγορο fade-in στην αρχή της ανακοίνωσης
    και fade-out στο τέλος της (μετά το STREAM_STOP), ώστε να μην ακούγεται «κλικ»
    από το απότομο πέρασμα σιωπή ↔ φωνή. resume_fade_samples: συντομότερο fade-in
    όταν ο ήχος συνεχίζει μετά από κενό δικτύου. 0 = χωρίς fade.
    """

    def __init__(self, max_blocks: int, block_bytes: int, prefill_blocks: int | None = None,
                 *, fade_in_samples: int = 0, fade_out_samples: int = 0,
                 resume_fade_samples: int = 0):
        if max_blocks < 1:
            raise ValueError("max_blocks πρέπει να είναι τουλάχιστον 1")
        self.max_blocks = max_blocks
        self.prefill_blocks = max(1, min(max_blocks, prefill_blocks if prefill_blocks else (max_blocks + 1) // 2))
        self._silence = bytes(block_bytes)
        # (block, tag): tag = αύξων αριθμός πακέτου, μόνο για τις μετρήσεις δοκιμών 4.4.
        self._blocks: deque[tuple[bytes, object]] = deque()
        #: Ετικέτα του block που επέστρεψε το τελευταίο pop() (None = σιωπή).
        self.last_tag: object = None
        self._lock = threading.Lock()
        self._filling = True
        self.dropped = 0
        self.underruns = 0
        self._levels: list[int] = []
        # Το legacy JitterBuffer παραμένει ενεργό για ανεξάρτητες δοκιμές.
        # Στον πραγματικό Client καλείται standby() πριν αρχίσει η λήψη.
        self._active = True
        self._draining = False
        self._block_samples = block_bytes // 2
        self.fade_in_samples = max(0, fade_in_samples)
        self.fade_out_samples = max(0, fade_out_samples)
        self.resume_fade_samples = max(0, resume_fade_samples)
        self._started = False  # έχει ήδη ακουστεί PCM σε αυτή την ανακοίνωση
        self._fade_total = 0  # μήκος τρέχοντος fade-in σε δείγματα
        self._fade_done = 0  # δείγματα του fade-in που έχουν ήδη παιχτεί

    def __len__(self) -> int:
        with self._lock:
            return len(self._blocks)

    @property
    def mode(self) -> str:
        with self._lock:
            return "ανακοίνωση" if self._active else "αναμονή"

    @property
    def playing(self) -> bool:
        """Ανακοίνωση σε εξέλιξη μετά το πρώτο block (σιωπή τώρα = κενό, όχι prefill)."""
        with self._lock:
            return self._active and self._started and not self._draining

    def standby(self) -> None:
        """Καθαρή αναμονή χωρίς underruns ούτε καταχώριση επιπέδων."""
        with self._lock:
            self._active = False
            self._draining = False
            self._filling = True
            self._started = False
            self._fade_total = self._fade_done = 0
            self._blocks.clear()
            self._levels.clear()

    def begin_announcement(self) -> None:
        """Νέα ανακοίνωση: απομάκρυνση παλιάς ουράς και prefill."""
        with self._lock:
            self._blocks.clear()
            self._levels.clear()
            self._filling = True
            self._draining = False
            self._active = True
            self._started = False
            self._fade_total = self._fade_done = 0
            self.dropped = 0
            self.underruns = 0

    def finish_announcement(self) -> None:
        """STREAM_STOP: παίζουμε όλα τα εναπομείναντα PCM χωρίς νέο prefill."""
        with self._lock:
            if self._active:
                self._draining = True

    def push(self, block: bytes, tag: object = None) -> None:
        with self._lock:
            self._blocks.append((block, tag))
            while len(self._blocks) > self.max_blocks:
                self._blocks.popleft()
                self.dropped += 1

    def pop(self) -> bytes:
        """Επιστρέφει το επόμενο block ή σιωπή αν η ουρά δεν έχει αρκετά δεδομένα."""
        with self._lock:
            item, fade_in, fade_out = self._pop_locked()
            block, self.last_tag = item
        if fade_in is not None:
            block = apply_gain_ramp(block, *fade_in)
        if fade_out is not None:
            block = apply_gain_ramp(block, *fade_out)
        return block

    def _start_fade_in(self) -> None:
        """Ξεκινά fade-in: πλήρες στην αρχή, σύντομο μετά από κενό δικτύου."""
        self._fade_total = self.resume_fade_samples if self._started else self.fade_in_samples
        self._fade_done = 0
        self._started = True

    def _next_fade_in(self) -> tuple[float, float] | None:
        """Gain πρώτου/τελευταίου δείγματος του block· 0 → 1 σε fade_total δείγματα.

        Οι τιμές πάνω από 1 κόβονται ανά δείγμα στο apply_gain_ramp, ώστε η κλίση
        να μένει ίδια από block σε block.
        """
        if self._fade_done >= self._fade_total:
            return None
        n = self._block_samples
        span = max(1, self._fade_total - 1)
        start = self._fade_done / span
        end = (self._fade_done + n - 1) / span
        self._fade_done += n
        return start, end

    def _pop_locked(self):
        if not self._active:
            return (self._silence, None), None, None
        if self._draining:
            if self._blocks:
                if not self._started:
                    self._start_fade_in()
                self._levels.append(len(self._blocks))
                fade_out = None
                if self.fade_out_samples:
                    # Μετά το STREAM_STOP ξέρουμε πόσος ήχος απομένει: fade-out
                    # ώστε το τελευταίο δείγμα να καταλήγει ομαλά στη σιωπή.
                    # Gain δείγματος = (r - 1) / (F - 1), όπου r τα δείγματα που
                    # απομένουν μαζί με αυτό: το τελευταίο δείγμα παίζει με 0.
                    remaining = len(self._blocks) * self._block_samples
                    last = remaining - self._block_samples + 1
                    if last <= self.fade_out_samples:
                        span = max(1, self.fade_out_samples - 1)
                        fade_out = ((remaining - 1) / span, (last - 1) / span)
                return self._blocks.popleft(), self._next_fade_in(), fade_out
            self._active = False
            self._draining = False
            self._filling = True
            return (self._silence, None), None, None
        self._levels.append(len(self._blocks))
        if self._filling:
            if len(self._blocks) < self.prefill_blocks:
                return (self._silence, None), None, None
            self._filling = False
            self._start_fade_in()
        if not self._blocks:
            self.underruns += 1
            self._filling = True
            return (self._silence, None), None, None
        return self._blocks.popleft(), self._next_fade_in(), None

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

    def reset(self) -> None:
        """Οι μετρήσεις δικτύου αρχίζουν από την αρχή κάθε ανακοίνωσης."""
        self._best_offset_us = None
        self._extra_us.clear()
        self.gaps = 0
        self._last_seq = None

    def take_stats(self) -> tuple[float, float] | None:
        """(μέση, μέγιστη) επιπλέον καθυστέρηση σε ms από την τελευταία κλήση."""
        extra, self._extra_us = self._extra_us, []
        if not extra:
            return None
        return sum(extra) / len(extra) / 1000, max(extra) / 1000
