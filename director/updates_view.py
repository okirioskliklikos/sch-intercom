"""Λογική (χωρίς Qt) για Ρυθμίσεις → «Ενημερώσεις» (Φάση 5B3, εγκεκριμένο mockup 27/11/2026).

Η κάρτα «Πακέτο Client» δείχνει το τρέχον και το προηγούμενο πακέτο του Manager και η
κάρτα «Υπολογιστές» την έκδοση και την κατάσταση κάθε εγκεκριμένου υπολογιστή, από το
`UpdateStatus` (ό,τι ανέφεραν οι Clients στη θύρα ενημερώσεων) και τη ζωντανή σύνδεση.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import time

from director.maintenance import date_text, size_text
from director.security import ago_text
from director.updates import UpdateError, UpdateStatus, UpdateStore
from shared.version import parse_version

TITLE_PACKAGE = "Πακέτο Client"
TITLE_COMPUTERS = "Υπολογιστές"
TEXT_NOT_IMPORTED = "Δεν έχει γίνει εισαγωγή"
TEXT_SIGNATURE = "έγκυρη"
TEXT_AUTO = "Αυτόματες ενημερώσεις"
TEXT_AUTO_HINT = "Αλλιώς μόνο με «Έναρξη τώρα»"
TEXT_IMPORT = "Εισαγωγή…"
TEXT_START_ALL = "Έναρξη τώρα σε όλους"
TEXT_START = "Έναρξη τώρα"
TEXT_BACKUP_NOTE = ("Η κατάσταση είναι από την τελευταία φορά που εξυπηρετούσε αυτός ο "
                    "υπολογιστής. Ενημερώσεις διανέμει μόνο ο Κύριος.")
TEXT_UPDATED = "Ενημερωμένος"
TEXT_NEVER = "Δεν έχει συνδεθεί"
TEXT_ONLINE = "Online"
TEXT_WAIT_ANNOUNCEMENT = "Αναμονή: ανακοίνωση"
TEXT_WAIT_SLOT = "Αναμονή σειράς λήψης"
TEXT_NO_RETRY = "δεν θα ξαναδοκιμαστεί"
TEXT_IMPORT_TITLE = "Εισαγωγή πακέτου"
TEXT_SIGNATURE_RELEASE = "έγκυρη (κλειδί έκδοσης)"
TEXT_AUTO_ON_NOTE = ("Οι αυτόματες ενημερώσεις είναι ενεργές: οι υπολογιστές που είναι "
                     "συνδεδεμένοι ξεκινούν αμέσως. Η εναλλαγή γίνεται μόνο όταν δεν παίζει "
                     "ανακοίνωση.")
TEXT_AUTO_OFF_NOTE = ("Οι αυτόματες ενημερώσεις είναι ανενεργές: η ενημέρωση ξεκινά μόνο με "
                      "«Έναρξη τώρα». Η εναλλαγή γίνεται μόνο όταν δεν παίζει ανακοίνωση.")
PACKAGE_FILTER = "Πακέτο Client School Intercom (*.sicu)"

# Τόνοι κατάστασης: ok (πράσινο), run (μπλε), wait/off (γκρι), fail (κόκκινο με ⚠).
COUNTER_LABELS = (("ok", "ενημερωμένοι"), ("available", "διαθέσιμη"),
                  ("run", "σε εξέλιξη"), ("fail", "αποτυχία"), ("off", "εκτός σύνδεσης"))


def number_text(value: int) -> str:
    """1912 → «1.912» (ελληνικά διαχωριστικά χιλιάδων)."""
    return f"{value:,}".replace(",", ".")


@dataclass(frozen=True)
class ComputerUpdate:
    """Μία γραμμή του πίνακα «Υπολογιστές»."""
    client_id: str
    room: str
    name: str
    version: str                      # «—» αν δεν έχει αναφέρει ποτέ
    text: str
    tone: str                         # ok | run | wait | off | fail | available
    progress: int | None = None       # μπάρα μόνο στη λήψη
    tooltip: str = ""
    can_start: bool = False           # ⋮ «Έναρξη τώρα»

    @property
    def counter(self) -> str:
        return "run" if self.tone == "wait" else self.tone


@dataclass(frozen=True)
class PackageInfo:
    """Κάρτα «Πακέτο Client»."""
    current: str | None               # «1.0.6 · 42,3 MB»
    imported: str | None
    previous: str
    missing: str | None               # κείμενο κόκκινης σημείωσης
    auto: bool
    missing_file: str | None = None   # όνομα .sicu (έντονο στη σημείωση)


class UpdatesModel:
    """``rooms``: `SecurityModel.rooms` (εγκεκριμένοι, online, τελευταία σύνδεση)·
    ``primary``: ο υπολογιστής είναι Κύριος (αλλιώς μόνο ανάγνωση)."""

    def __init__(self, store: UpdateStore, status: UpdateStatus, *,
                 rooms: Callable[[], list], primary: Callable[[], bool] = lambda: True,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.status = status
        self.rooms = rooms
        self.primary = primary
        self.clock = clock

    # --- κάρτα πακέτου --------------------------------------------------------
    def package(self) -> PackageInfo:
        config = self.store.config()
        current, previous = config["current"], config["previous"]
        missing = None
        if current is not None and self.store.package_missing():
            missing = (f"Λείπει το αρχείο του πακέτου {current['version']} (επαναφορά από "
                       f"backup). Κάντε ξανά εισαγωγή του {current['file']}.")
        return PackageInfo(
            current=None if current is None else
            f"{current['version']} · {size_text(current['size'])}",
            imported=None if current is None else date_text(current["imported_at"]),
            previous="—" if previous is None else previous["version"],
            missing=missing, auto=bool(config["auto"]),
            missing_file=current["file"] if missing else None)

    # --- πίνακας υπολογιστών ----------------------------------------------------
    def computers(self) -> list[ComputerUpdate]:
        config = self.store.config()
        current = config["current"]
        target = current["version"] if current else None
        usable = current is not None and not self.store.package_missing()
        primary = self.primary()
        snapshot = self.status.snapshot()
        now = self.clock()
        rows = []
        for room in self.rooms():
            for computer in room.computers:
                entry = snapshot.get(computer.client_id)
                rows.append(self._row(computer, entry, target, config, usable and primary, now))
        return rows

    def _row(self, computer, entry: dict | None, target: str | None, config: dict,
             can_act: bool, now: float) -> ComputerUpdate:
        base = {"client_id": computer.client_id, "room": computer.room, "name": computer.name}
        if not entry or not entry.get("app"):
            if computer.online:
                return ComputerUpdate(**base, version="—", text=TEXT_ONLINE, tone="off")
            return ComputerUpdate(**base, version="—", text=TEXT_NEVER, tone="off")
        version = entry["app"]
        try:
            behind = target is not None and parse_version(version) < parse_version(target)
        except ValueError:
            behind = False
        state = entry.get("state")
        if target is None:
            # Χωρίς πακέτο: μόνο online/εκτός σύνδεσης.
            if computer.online:
                return ComputerUpdate(**base, version=version, text=TEXT_ONLINE, tone="off")
            return ComputerUpdate(**base, version=version, text=self._offline(computer, entry, now),
                                  tone="off")
        if not behind:
            return ComputerUpdate(**base, version=version, text=TEXT_UPDATED, tone="ok")
        if state == "failed" and entry.get("available") == target:
            when = date_text(entry.get("updated_at") or now)
            reason = entry.get("detail") or "Η εναλλαγή απέτυχε"
            return ComputerUpdate(**base, version=version, text=f"⚠ Αποτυχία {target}",
                                  tone="fail", tooltip=f"{reason}\n{when} · {TEXT_NO_RETRY}")
        if not computer.online:
            return ComputerUpdate(**base, version=version,
                                  text=self._offline(computer, entry, now), tone="off",
                                  can_start=can_act)
        same = entry.get("available") == target
        if same and state == "downloading":
            progress = int(entry.get("progress") or 0)
            return ComputerUpdate(**base, version=version, text=f"Λήψη {progress}%", tone="run",
                                  progress=progress)
        if same and state == "waiting":
            return ComputerUpdate(**base, version=version, text=TEXT_WAIT_ANNOUNCEMENT,
                                  tone="wait")
        if same and state in ("ready", "installing", "installed"):
            return ComputerUpdate(**base, version=version, text=f"Εναλλαγή σε {target}…",
                                  tone="run")
        if same and state == "available" and entry.get("detail") == "αναμονή σειράς λήψης":
            return ComputerUpdate(**base, version=version, text=TEXT_WAIT_SLOT, tone="wait")
        return ComputerUpdate(**base, version=version, text=f"Διαθέσιμη η {target}",
                              tone="available", can_start=can_act)

    @staticmethod
    def _offline(computer, entry: dict, now: float) -> str:
        seen = computer.last_seen or entry.get("seen_at") or entry.get("updated_at")
        return "Εκτός σύνδεσης · " + ago_text(now - seen) if seen else TEXT_NEVER

    @staticmethod
    def counters(rows: list[ComputerUpdate], *, imported: bool = True) -> list[tuple[int, str]]:
        """Μετρητές πάνω δεξιά στην κάρτα «Υπολογιστές» (μόνο όσοι δεν είναι 0)."""
        if not imported:
            versions: dict[str, int] = {}
            for row in rows:
                if row.version != "—":
                    versions[row.version] = versions.get(row.version, 0) + 1
            items = [(count, f"με την {version}") for version, count in
                     sorted(versions.items(), key=lambda item: parse_version(item[0]),
                            reverse=True)]
            never = sum(1 for row in rows if row.version == "—")
            return items + ([(never, "δεν έχει συνδεθεί")] if never else [])
        counts = {key: 0 for key, _ in COUNTER_LABELS}
        for row in rows:
            counts[row.counter] += 1
        return [(counts[key], label) for key, label in COUNTER_LABELS if counts[key]]

    # --- ενέργειες ----------------------------------------------------------------
    def versions(self) -> tuple[str | None, dict[str, str]]:
        """Για το ✓ των αιθουσών: (τρέχουσα έκδοση ή None, {client_id: έκδοση})."""
        current = self.store.config()["current"]
        snapshot = self.status.snapshot()
        return (None if current is None else current["version"],
                {cid: entry["app"] for cid, entry in snapshot.items() if entry.get("app")})

    def set_auto(self, enabled: bool) -> None:
        self._require_primary()
        self.store.set_auto(enabled)

    def start_all(self) -> None:
        self._require_primary()
        self.store.start_now(None)

    def start(self, client_id: str) -> None:
        self._require_primary()
        self.store.start_now([client_id])

    def inspect(self, path: Path) -> "ImportView":
        return import_view(self.store.inspect_package(Path(path)), auto=self.store.config()["auto"])

    def import_package(self, path: Path) -> dict:
        self._require_primary()
        return self.store.import_package(Path(path))

    def _require_primary(self) -> None:
        if not self.primary():
            raise UpdateError("Ενημερώσεις διανέμει μόνο ο Κύριος.")


@dataclass(frozen=True)
class ImportView:
    """Διάλογος «Εισαγωγή πακέτου» (mockup #import / #import-bad)."""
    path: Path
    error: str | None
    rows: tuple[tuple[str, str, str | None], ...]   # (ετικέτα, κείμενο, τόνος)
    note: str | None


def import_view(info: dict, *, auto: bool) -> ImportView:
    path = Path(info["path"])
    rows: list[tuple[str, str, str | None]] = [("Αρχείο", str(path), None)]
    if info["error"]:
        # Στον Manager η σύγκριση είναι με το τρέχον πακέτο, όχι με εγκατάσταση.
        message = info["error"].replace("από την εγκατεστημένη", "από την τρέχουσα")
        if not message.endswith("."):
            message += "."
        return ImportView(path, message, tuple(rows), None)
    rows.append(("Έκδοση", f"{info['version']} · {size_text(info['size'])} · "
                 f"{date_text(info['created_at'])}", "bold"))
    rows.append(("Υπογραφή", TEXT_SIGNATURE_RELEASE, "ok"))
    if info["compared_with"] is not None:
        changes = (f"{number_text(info['changed'])} από {number_text(info['files'])} αρχεία "
                   f"σε σχέση με την {info['compared_with']} ({size_text(info['changed_bytes'])})")
    else:
        changes = f"{number_text(info['files'])} αρχεία (πρώτο πακέτο)"
    rows.append(("Αλλαγές", changes, None))
    return ImportView(path, None, tuple(rows), TEXT_AUTO_ON_NOTE if auto else TEXT_AUTO_OFF_NOTE)
