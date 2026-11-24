"""4.2D3: αναφορές Clients «έχω δει μητρώο vN» (phase4-2.md §4, phase4-2d.md §14).

Μόνο εγκεκριμένοι Clients (μετά από mTLS και έλεγχο μητρώου) καταγράφονται. Οι
αναφορές κρατιούνται στη μνήμη: μετά από επανεκκίνηση του Manager οι Clients
ξανασυνδέονται και αναφέρουν ξανά. Είναι **μόνο ένδειξη**, ποτέ αλλαγή εμπιστοσύνης.

Συναγερμός όταν ένας Client έχει δει:
- μεγαλύτερη έκδοση από την τοπική («νεότερο μητρώο»), ή
- την ίδια έκδοση με άλλο hash («διαφορετικό μητρώο», απόκλιση δύο Κυρίων).
"""
from __future__ import annotations

from dataclasses import dataclass
import threading

TEXT_ACCEPT_TITLE = "Συνέχεια με αυτό το μητρώο"


@dataclass(frozen=True)
class Report:
    client_id: str
    rv: int
    hash: str
    room: str
    name: str

    @property
    def label(self) -> str:
        return f"{self.room} · {self.name}" if self.name else self.room


@dataclass(frozen=True)
class ReportedRegistry:
    """Το μεγαλύτερο μητρώο που ανέφεραν Clients και δεν υπάρχει εδώ."""
    rv: int
    local_rv: int
    divergent: bool            # ίδια έκδοση με άλλο hash
    reporters: tuple[str, ...]  # «Αίθουσα · Υπολογιστής», ταξινομημένα

    def reporters_text(self, limit: int = 3) -> str:
        shown = ", ".join(self.reporters[:limit])
        more = len(self.reporters) - limit
        return shown + (f" (+{more})" if more > 0 else "")

    def status_line(self) -> str:
        """Σταθερό κείμενο του `status` (το αναγνωρίζει το script δοκιμής)."""
        kind = "διαφορετικό" if self.divergent else "νεότερο"
        return (f"[Manager] Clients αναφέρουν {kind} μητρώο: v{self.rv}, "
                f"εδώ v{self.local_rv} ({self.reporters_text()})")

    def bar_text(self) -> str:
        """Κείμενο της κόκκινης ειδοποίησης (phase4-2.md §4)."""
        if self.divergent:
            return (f"Υπάρχει διαφορετικό μητρώο v{self.rv} (απόκλιση). Οι ανακλήσεις "
                    "μπορεί να μην ισχύουν. Εισαγάγετέ το και κάντε συγχώνευση.")
        return (f"Υπάρχει νεότερο μητρώο (v{self.rv}). Οι ανακλήσεις μπορεί να μην "
                "ισχύουν. Εισαγάγετε το ενημερωμένο μητρώο.")

    def accept_warning(self) -> str:
        """Προειδοποίηση πριν από «Συνέχεια με αυτό το μητρώο»."""
        return (f"Το μητρώο v{self.rv} θα θεωρηθεί χαμένο. Ανακλήσεις και εγκρίσεις "
                f"που έγιναν εκεί μετά το v{min(self.local_rv, self.rv)} δεν ισχύουν εδώ: "
                "υπολογιστές που ανακλήθηκαν εκεί μπορεί να συνδέονται ξανά. "
                "Ελέγξτε τη λίστα υπολογιστών μετά.")

    @property
    def target_version(self) -> int:
        """Η νέα έκδοση: μεγαλύτερη από κάθε αναφερόμενη, ώστε η ειδοποίηση να σβήσει."""
        return max(self.rv, self.local_rv) + 1


class RegistryReports:
    """Τελευταία αναφορά ανά Client (thread-safe)."""

    def __init__(self, on_event=None):
        self._lock = threading.Lock()
        self._reports: dict[str, Report] = {}
        self._on_event = on_event or (lambda text: print(text, flush=True))

    def record(self, client_id: str, seen: dict | None, *, room: str, name: str,
               local_rv: int, local_hash: str) -> bool:
        """Καταγραφή από `_admit_client`. True αν η αναφορά προκαλεί συναγερμό."""
        with self._lock:
            if seen is None:
                self._reports.pop(client_id, None)
                return False
            report = Report(client_id, seen["rv"], seen["hash"], room, name or "")
            self._reports[client_id] = report
        alarm = _alarming(report, local_rv, local_hash)
        if alarm:
            kind = "διαφορετικό" if report.rv == local_rv else "νεότερο"
            self._on_event(f"[Manager] Ο Client {report.label} έχει δει {kind} μητρώο "
                           f"v{report.rv}, εδώ v{local_rv}.")
        return alarm

    def forget(self, client_id: str) -> None:
        with self._lock:
            self._reports.pop(client_id, None)

    def reported(self, local_rv: int, local_hash: str, *,
                 approved: set[str] | None = None) -> ReportedRegistry | None:
        """``approved``: μόνο αυτοί οι Clients μετρούν (π.χ. όχι όσοι ανακλήθηκαν μετά)."""
        with self._lock:
            reports = [r for r in self._reports.values()
                       if _alarming(r, local_rv, local_hash)
                       and (approved is None or r.client_id in approved)]
        if not reports:
            return None
        top = max(r.rv for r in reports)
        reporters = tuple(sorted({r.label for r in reports if r.rv == top}))
        return ReportedRegistry(top, local_rv, top == local_rv, reporters)


def _alarming(report: Report, local_rv: int, local_hash: str) -> bool:
    return report.rv > local_rv or (report.rv == local_rv and report.hash != local_hash)
