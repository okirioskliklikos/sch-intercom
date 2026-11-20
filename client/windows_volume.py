"""Προσωρινή ένταση εξόδου Windows για ανακοινώσεις (Φάση 3/βήμα 2).

Δεν αγγίζει συνεδρίες άλλων εφαρμογών ή media controls. Οι κλήσεις Core Audio
γίνονται στο thread λήψης, όχι στο real-time callback της κάρτας ήχου.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Callable

from client.windows_apps import ApplicationAudio, restore_record_apps

from shared.windows_audio import PycawMaster, ramp_volume, same_volume as _same_volume

RAMP_MS = 150


class AnnouncementVolume:
    """Αποθηκεύει πριν από κάθε αλλαγή και επαναφέρει μόνο δικές μας τιμές."""

    def __init__(self, master, recovery_path: Path, *, target_percent: int,
                 ramp_ms: float = RAMP_MS, sleep: Callable | None = None,
                 log: Callable[[str], None] = print,
                 apps: ApplicationAudio | None = None):
        if not 0 <= target_percent <= 100:
            raise ValueError("Η ένταση ανακοίνωσης πρέπει να είναι 0–100%")
        self.master = master
        self.path = Path(recovery_path)
        self.target = target_percent / 100
        self.ramp_ms = ramp_ms
        self.sleep = sleep
        self.log = log
        self.active = False
        self._saved = None
        self.apps = apps

    def _ramp(self, start: float, end: float) -> None:
        if self.sleep is None:
            ramp_volume(self.master.set_volume, start, end, self.ramp_ms)
        else:
            ramp_volume(self.master.set_volume, start, end, self.ramp_ms, self.sleep)

    def _device(self) -> str:
        return str(getattr(self.master, "device_name", "(άγνωστη έξοδος)"))

    def _write(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                           "w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _read(self) -> dict | None:
        if not self.path.exists():
            return None
        if self.path.stat().st_size > 4096:
            raise ValueError("Το αρχείο ανάκαμψης είναι υπερβολικά μεγάλο")
        record = json.loads(self.path.read_text(encoding="utf-8"))
        if (not isinstance(record, dict) or record.get("version") != 1
                or not all(type(record.get(k)) in (int, float) and 0 <= record[k] <= 1
                           for k in ("original", "target"))
                or type(record.get("original_mute")) is not bool
                or type(record.get("device")) is not str
                or record.get("stage") not in ("pending", "applied")):
            raise ValueError("Μη έγκυρο αρχείο ανάκαμψης ήχου")
        return record

    def recover(self) -> bool:
        """Επαναφορά μετά από crash. Ξένη/άγνωστη συσκευή δεν τροποποιείται."""
        record = self._read()
        if record is None:
            return True
        if record["device"] != self._device():
            self.log("[ήχος] Εκκρεμεί ανάκαμψη σε διαφορετική έξοδο· "
                     "δεν αλλάζει η τρέχουσα συσκευή.")
            return False
        return self._restore_record(record)

    def _restore_record(self, record: dict) -> bool:
        current = self.master.get_volume()
        original, target = record["original"], record["target"]
        # Σε crash κατά το ramp το journal μπορεί να γράφει pending. Επιτρέπεται
        # αποκατάσταση μόνο αν η τρέχουσα στάθμη είναι μεταξύ αρχικής/στόχου.
        during_ramp = (record["stage"] == "pending"
                       and min(original, target) - 0.01 <= current
                       <= max(original, target) + 0.01)
        if _same_volume(current, target) or during_ramp:
            self._ramp(current, original)
        else:
            self.log("[ήχος] Η ένταση άλλαξε εξωτερικά· δεν αναιρείται.")
        # Αν ο Client είχε άρει mute, το επαναφέρει μόνο αν είναι ακόμη off.
        # Το flag 'unmuted' καταχωρείται πριν από το SetMute ώστε να καλύπτει crash.
        if record.get("unmuted") and not self.master.get_mute():
            self.master.set_mute(True)
        # Το ίδιο αρχείο προστατεύει master volume, εφαρμογές και media.
        if not restore_record_apps(record, self._write, app_audio=self.apps):
            self.log("[ήχος] Εκκρεμεί επαναφορά εφαρμογών· το journal διατηρείται.")
            return False
        self.path.unlink(missing_ok=True)
        return True

    def begin(self) -> bool:
        if self.active:
            return True  # διαδοχική ανακοίνωση: κρατάμε την αρχική τιμή
        if not self.recover():
            return False
        original = self.master.get_volume()
        original_mute = self.master.get_mute()
        record = {"version": 1, "device": self._device(), "original": original,
                  "original_mute": original_mute, "target": self.target,
                  "stage": "pending", "unmuted": original_mute}
        self._write(record)  # ΠΡΙΝ από οποιαδήποτε αλλαγή Windows
        try:
            if self.apps is not None:
                self.apps.begin(record, self._write)
            self._ramp(original, self.target)
            if original_mute:
                self.master.set_mute(False)
            record["stage"] = "applied"
            self._write(record)
        except Exception:
            try:
                self._restore_record(record)
            except Exception:
                self.log("[ήχος] Αποτυχία επαναφοράς· διατηρείται το αρχείο ανάκαμψης.")
            raise
        self.active = True
        self._saved = record
        self.log(f"[ήχος] Προσωρινή ένταση ανακοίνωσης {round(self.target * 100)}%")
        return True

    def end(self) -> None:
        if not self.active:
            return
        record = self._saved
        try:
            self._restore_record(record)
        finally:
            self.active = False
            self._saved = None


def _console(line: str) -> None:
    print(line, flush=True)


def create_volume_controller(percent: int, *, device: int | None,
                             recovery_path: Path,
                             master_factory: Callable[[], object] | None = None,
                             log: Callable[[str], None] = _console,
                             apps: ApplicationAudio | None = None,
                             ) -> AnnouncementVolume | None:
    """Αλλαγή μόνο της *προεπιλεγμένης* εξόδου (όπως στο Windows test script).

    Δεν εγείρει ποτέ σφάλμα: η ένταση είναι βοηθητική λειτουργία και δεν πρέπει
    να εμποδίζει την εκκίνηση ή την επανασύνδεση του Client. Σε πρόβλημα
    γράφεται προειδοποίηση και επιστρέφεται None (ο Client παίζει χωρίς αλλαγή
    έντασης Windows).
    """
    if not 0 <= percent <= 100:
        log("[ήχος] --announcement-volume εκτός 0–100· η ένταση Windows δεν αλλάζει.")
        return None
    if master_factory is None and sys.platform != "win32":
        log("[ήχος] Η ένταση Windows δεν υποστηρίζεται εδώ· παραμένει ανενεργή.")
        return None
    if device is not None:
        log("[ήχος] Η προσωρινή ένταση Windows δεν υποστηρίζει ακόμη --device "
            "(θα μπορούσε να αλλάξει λάθος συσκευή εξόδου)· παραμένει ανενεργή.")
        return None
    try:
        controller = AnnouncementVolume((master_factory or PycawMaster)(), recovery_path,
                                        target_percent=percent, log=log, apps=apps)
        if not controller.recover():
            # Ανάκαμψη για άλλη έξοδο: δεν αλλάζουμε την τρέχουσα. Το begin()
            # ξαναδοκιμάζει την ανάκαμψη και δεν αλλάζει ένταση όσο εκκρεμεί.
            log("[ήχος] Η προσωρινή ένταση μένει ανενεργή μέχρι να επιλυθεί η ανάκαμψη "
                f"({recovery_path}).")
        return controller
    except Exception as exc:  # noqa: BLE001 - ποτέ εμπόδιο για τον ήχο ανακοίνωσης
        log(f"[ήχος] Αδυναμία ελέγχου έντασης Windows ({exc}); συνέχεια χωρίς αυτήν.")
        return None


def recover_pending_volume(recovery_path: Path, *, device: int | None,
                           master_factory: Callable[[], object] | None = None,
                           log: Callable[[str], None] = _console) -> None:
    """Ανάκαμψη στην εκκίνηση όταν υπάρχει αρχείο από προηγούμενο crash.

    Καλείται και χωρίς --announcement-volume. Δεν εγείρει ποτέ σφάλμα.
    """
    try:
        if not Path(recovery_path).exists():
            return
    except OSError:
        return
    if device is not None:
        log("[ήχος] Υπάρχει αρχείο ανάκαμψης έντασης, αλλά με --device δεν γίνεται "
            "αυτόματη επαναφορά (άγνωστη αντιστοίχιση συσκευής).")
        return
    if master_factory is None and sys.platform != "win32":
        return
    try:
        controller = AnnouncementVolume((master_factory or PycawMaster)(), recovery_path,
                                        target_percent=0, log=log)
        if not controller.recover():
            log(f"[ήχος] Το αρχείο ανάκαμψης ({recovery_path}) αφορά άλλη έξοδο ήχου· "
                "διατηρείται για την επόμενη φορά. Ο Client συνεχίζει κανονικά.")
    except Exception as exc:  # noqa: BLE001
        log(f"[ήχος] Αποτυχία ανάκαμψης έντασης ({exc}); ο Client συνεχίζει κανονικά.")
