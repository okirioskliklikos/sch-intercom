"""4.4: υπολογισμοί της δοκιμής πραγματικού δικτύου (χωρίς δίκτυο/ήχο, για unit tests).

Όλοι οι χρόνοι σε μs. Ο Director γράφει ένα «pub» ανά block (αύξων, χρόνος, πλήθος
παραληπτών) και κάθε Client «play» (αύξων, εκτιμώμενη στιγμή που ακούγεται) στο ρολόι
του δικού του υπολογιστή. Το ClockMap μεταφέρει τους χρόνους του Client στο ρολόι του
Director, από μετρήσεις τύπου NTP πάνω στο κανάλι της δοκιμής.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
import statistics

BLOCK_US = 20_000
EDGE_BLOCKS = 3          # πρώτα/τελευταία blocks κάθε ανακοίνωσης που πρέπει να ακουστούν
#: Κριτήρια ανά είδος διαδρομής (Wi-Fi αν οποιοδήποτε άκρο είναι Wi-Fi ή άγνωστο).
LIMITS = {
    "lan": {"p95_ms": 500, "interrupt": 0.01},
    "wifi": {"p95_ms": 800, "interrupt": 0.03},
}
SYNC_LIMIT_MS = 1000     # απόφαση χρήστη 24/11/2026: έως 1 s διαφορά μεταξύ αιθουσών
DRIFT_LIMIT_MS = 100
RECONNECT_LIMIT_S = 15.0
LOSS_DETECT_CLIENT_S = 4.5     # 3 s χωρίς μήνυμα + περιθώριο
LOSS_DETECT_DIRECTOR_S = 7.5   # 6 s χωρίς heartbeat + περιθώριο


def path_kind(*links: str | None) -> str:
    """«lan» μόνο αν όλα τα άκρα είναι σίγουρα καλώδιο."""
    return "lan" if links and all(link == "lan" for link in links) else "wifi"


def best_offset(samples: list[tuple[int, int, int]]) -> tuple[float, int]:
    """(offset, rtt) από το δείγμα με το μικρότερο RTT.

    Δείγμα = (Director πριν, Client, Director μετά). offset = Client − Director:
    χρόνος Director = χρόνος Client − offset. Σφάλμα το πολύ rtt/2.
    """
    if not samples:
        raise ValueError("Χωρίς δείγματα ρολογιού")
    before, client, after = min(samples, key=lambda item: item[2] - item[0])
    return client - (before + after) / 2, after - before


class ClockMap:
    """Γραμμική παρεμβολή του offset ανάμεσα σε μετρήσεις (ολίσθηση ρολογιών)."""

    def __init__(self) -> None:
        self.points: list[tuple[float, float]] = []   # (χρόνος Client, offset)
        self.rtts: list[int] = []

    def add(self, samples: list[tuple[int, int, int]]) -> None:
        offset, rtt = best_offset(samples)
        client_time = min(samples, key=lambda item: item[2] - item[0])[1]
        self.points.append((client_time, offset))
        self.points.sort()
        self.rtts.append(rtt)

    def offset(self, client_us: float) -> float:
        if not self.points:
            raise ValueError("Δεν έχει μετρηθεί το ρολόι")
        if len(self.points) == 1 or client_us <= self.points[0][0]:
            return self.points[0][1]
        if client_us >= self.points[-1][0]:
            return self.points[-1][1]
        index = bisect_left([point[0] for point in self.points], client_us)
        (t0, o0), (t1, o1) = self.points[index - 1], self.points[index]
        return o0 + (o1 - o0) * (client_us - t0) / (t1 - t0)

    def to_director(self, client_us: float) -> float:
        return client_us - self.offset(client_us)

    @property
    def error_ms(self) -> float:
        """Μέγιστο σφάλμα (μισό RTT) της χειρότερης μέτρησης."""
        return max(self.rtts, default=0) / 2000


@dataclass
class Run:
    """Μία ανακοίνωση: συνεχόμενα blocks με τουλάχιστον έναν παραλήπτη."""
    first: int
    last: int
    sent_us: dict[int, int] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return self.last - self.first + 1

    @property
    def start_us(self) -> int:
        return self.sent_us[self.first]

    @property
    def end_us(self) -> int:
        return self.sent_us[self.last]


def runs_from_pubs(pubs: list[dict]) -> list[Run]:
    runs: list[Run] = []
    current: Run | None = None
    for item in sorted(pubs, key=lambda event: event["seq"]):
        if item.get("n", 0) > 0:
            if current is not None and item["seq"] == current.last + 1:
                current.last = item["seq"]
            else:
                current = Run(item["seq"], item["seq"])
                runs.append(current)
            current.sent_us[item["seq"]] = item["t"]
        else:
            current = None
    return runs


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


@dataclass
class ClientResult:
    name: str
    blocks: int = 0
    played: int = 0
    lost: int = 0
    skipped: int = 0          # blocks σιωπής που παραλείφθηκαν (προσαρμοζόμενη ουρά)· όχι απώλεια
    underruns: int = 0
    silence: int = 0          # blocks σιωπής που έπαιξε ο Client μέσα σε ανακοίνωση
    edges_ok: int = 0
    edges_total: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    latency_times: list[float] = field(default_factory=list)  # χρόνος Director (μs)
    play_director_us: dict[int, float] = field(default_factory=dict)

    @property
    def interrupt(self) -> float:
        if not self.blocks:
            return 0.0
        return min(1.0, (self.lost + self.silence) / self.blocks)

    def summary(self) -> str:
        if not self.latencies_ms:
            return f"{self.name}: δεν ακούστηκε κανένα block"
        return (f"{self.name}: καθυστέρηση διάμεση {statistics.median(self.latencies_ms):.0f} / "
                f"p95 {percentile(self.latencies_ms, 0.95):.0f} / "
                f"max {max(self.latencies_ms):.0f} ms, χαμένα {self.lost}/{self.blocks}, "
                f"κενά {self.underruns} ({self.silence * 20} ms σιωπή), άκρα {self.edges_ok}/{self.edges_total}"
                + (f", παύσεις κόντυναν {self.skipped * 20} ms" if self.skipped else ""))


def analyse(name: str, runs: list[Run], events: list[dict], clock: ClockMap) -> ClientResult:
    """Αποτέλεσμα ενός πραγματικού Client για τις ανακοινώσεις runs."""
    result = ClientResult(name)
    plays: dict[int, float] = {}
    skips: set[int] = set()
    for event in events:
        if event.get("ev") == "play" and event["seq"] not in plays:
            plays[event["seq"]] = clock.to_director(event["t"])
        elif event.get("ev") == "skip":
            skips.add(event["seq"])
    for run in runs:
        played = [seq for seq in range(run.first, run.last + 1) if seq in plays]
        skipped = sum(1 for seq in range(run.first, run.last + 1)
                      if seq in skips and seq not in plays)
        result.blocks += run.size
        result.played += len(played)
        result.skipped += skipped
        result.lost += run.size - len(played) - skipped
        edges = sorted({*range(run.first, min(run.last, run.first + EDGE_BLOCKS - 1) + 1),
                        *range(max(run.first, run.last - EDGE_BLOCKS + 1), run.last + 1)})
        result.edges_total += len(edges)
        result.edges_ok += sum(seq in plays or seq in skips for seq in edges)
        for seq in played:
            result.latencies_ms.append((plays[seq] - run.sent_us[seq]) / 1000)
            result.latency_times.append(run.sent_us[seq])
            result.play_director_us[seq] = plays[seq]
        start = clock.offset(run.start_us) + run.start_us
        end = clock.offset(run.end_us) + run.end_us + 1_000_000
        result.underruns += sum(1 for event in events if event.get("ev") == "underrun"
                                and start <= event["t"] <= end)
        result.silence += sum(1 for event in events if event.get("ev") == "silence"
                              and start <= event["t"] <= end)
    return result


def check(result: ClientResult, kind: str, *, edges: bool = True) -> tuple[bool, str]:
    limits = LIMITS[kind]
    if not result.latencies_ms:
        return False, result.summary()
    ok = (percentile(result.latencies_ms, 0.95) <= limits["p95_ms"]
          and result.interrupt <= limits["interrupt"]
          and (not edges or result.edges_ok == result.edges_total))
    return ok, (f"{result.summary()}, διακοπές {result.interrupt * 100:.1f}% "
                f"(όριο {kind}: p95 ≤ {limits['p95_ms']} ms, ≤ {limits['interrupt'] * 100:.0f}%)")


def drift_ms(result: ClientResult, window_us: int = 60_000_000) -> float:
    """Διάμεση καθυστέρηση του τελευταίου λεπτού μείον του πρώτου."""
    if not result.latency_times:
        return float("nan")
    start, end = min(result.latency_times), max(result.latency_times)
    first = [v for t, v in zip(result.latency_times, result.latencies_ms) if t < start + window_us]
    last = [v for t, v in zip(result.latency_times, result.latencies_ms) if t > end - window_us]
    return statistics.median(last) - statistics.median(first)


def sync_spread_ms(results: list[ClientResult]) -> tuple[float, float, int]:
    """(μέγιστη, p95, πλήθος blocks) διαφορά στιγμής αναπαραγωγής ανάμεσα στους Clients."""
    common = set.intersection(*(set(item.play_director_us) for item in results)) \
        if results else set()
    spreads = [(max(item.play_director_us[seq] for item in results)
                - min(item.play_director_us[seq] for item in results)) / 1000
               for seq in common]
    if not spreads:
        return float("nan"), float("nan"), 0
    return max(spreads), percentile(spreads, 0.95), len(spreads)


def virtual_received(runs: list[Run], seqs: set[int]) -> tuple[int, int]:
    """(ληφθέντα, αναμενόμενα) blocks ενός εικονικού Client."""
    expected = sum(run.size for run in runs)
    got = sum(1 for run in runs for seq in range(run.first, run.last + 1) if seq in seqs)
    return got, expected


def recovery_s(result: ClientResult, resumed_us: int, margin_ms: float = 60.0) -> float | None:
    """Δευτερόλεπτα από το τέλος ενός κολλήματος μέχρι η καθυστέρηση να ξαναγίνει κανονική.

    Κανονική = διάμεση πριν από το κόλλημα + margin_ms. None αν δεν επέστρεψε ή αν δεν
    υπάρχουν μετρήσεις πριν/μετά.
    """
    before = [value for t, value in zip(result.latency_times, result.latencies_ms)
              if t < resumed_us - 500_000]
    after = sorted((t, value) for t, value in zip(result.latency_times, result.latencies_ms)
                   if t >= resumed_us)
    if not before or not after:
        return None
    limit = statistics.median(before) + margin_ms
    for t, value in after:
        if value <= limit:
            return max(0.0, (t - resumed_us) / 1e6)
    return None
