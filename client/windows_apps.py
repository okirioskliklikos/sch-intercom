"""Έλεγχος συνεδριών εφαρμογών για ανακοινώσεις Windows.

Το module δεν εισάγει Windows βιβλιοθήκες κατά το import, ώστε τα tests στο
Linux να μπορούν να χρησιμοποιήσουν fake sessions. Αποθήκευση όλων των αλλαγών
στο *ίδιο* journal του client.windows_volume, πριν από το mutation.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Callable

from shared.windows_audio import ramp_volume, same_volume

RAMP_MS = 150
PLAYING, PAUSED = 4, 5


class AppSession:
    """Μία αναγνωρίσιμη Core Audio session, όχι απλώς ένα όνομα διεργασίας."""

    def __init__(self, session):
        self.raw = session
        self.pid = int(session.ProcessId or 0)
        self.name = session.Process.name() if session.Process else ""
        self.session_id = str(session.InstanceIdentifier or "")
        # Αποφυγή επαναφοράς σε PID που ανακυκλώθηκε μετά από crash.
        self.started = float(session.Process.create_time()) if session.Process else 0.0
        self.audio = session.SimpleAudioVolume

    def get_volume(self):
        return float(self.audio.GetMasterVolume())

    def set_volume(self, value):
        self.audio.SetMasterVolume(float(value), None)

    def get_mute(self):
        return bool(self.audio.GetMute())


def load_sessions():
    from pycaw.pycaw import AudioUtilities
    result = []
    for raw in AudioUtilities.GetAllSessions():
        try:
            item = AppSession(raw)
            if item.pid > 0 and item.session_id and item.started > 0:
                result.append(item)
        except Exception:
            continue
    return result


class MediaSession:
    def __init__(self, raw):
        self.raw = raw
        self.app_id = str(raw.source_app_user_model_id or "")

    def status(self):
        return int(self.raw.get_playback_info().playback_status)

    def can_pause(self):
        return bool(self.raw.get_playback_info().controls.is_pause_enabled)

    async def pause(self):
        return bool(await self.raw.try_pause_async())

    async def play(self):
        return bool(await self.raw.try_play_async())


async def load_media():
    try:
        from winrt.windows.media import control
    except ImportError:
        from winsdk.windows.media import control
    manager = await control.GlobalSystemMediaTransportControlsSessionManager.request_async()
    return [MediaSession(s) for s in manager.get_sessions()]


def _same_identity(session, item):
    return (session.pid == item.get("pid")
            and session.name == item.get("name")
            and session.session_id == item.get("session_id")
            and abs(session.started - item.get("started", -1)) < 0.01)


def _media_key(media):
    return media.app_id


class ApplicationAudio:
    def __init__(self, *, sessions: Callable = load_sessions,
                 media: Callable = load_media, own_pid: int | None = None,
                 ramp_ms: int = RAMP_MS, sleep: Callable | None = None,
                 log: Callable[[str], None] = print):
        self.sessions = sessions
        self.media = media
        self.own_pid = os.getpid() if own_pid is None else own_pid
        self.ramp_ms = ramp_ms
        self.sleep = sleep
        self.log = log

    def _ramp(self, session, start, end):
        if self.sleep is None:
            ramp_volume(session.set_volume, start, end, self.ramp_ms)
        else:
            ramp_volume(session.set_volume, start, end, self.ramp_ms, self.sleep)

    def begin(self, record: dict, save: Callable[[dict], None]) -> None:
        record.setdefault("apps", [])
        record.setdefault("media", [])
        sessions = self.sessions()
        changes = []
        # Ένα journal write για ΟΛΕΣ τις συνεδρίες ΠΡΙΝ από οποιαδήποτε
        # αλλαγή. Η μείωση εκτελείται παράλληλα ανά βήμα (~150 ms συνολικά),
        # όχι 150 ms * αριθμό εφαρμογών (αλλιώς χάνονται αρχικές συλλαβές).
        for session in sessions[:64]:
            try:
                if session.pid in (0, self.own_pid) or session.get_mute():
                    continue
                original = session.get_volume()
                if same_volume(original, 0):
                    continue
                item = {"pid": session.pid, "name": session.name,
                        "session_id": session.session_id, "started": session.started,
                        "original": original, "stage": "pending"}
                record["apps"].append(item)
                changes.append((session, item))
            except Exception as exc:
                self.log(f"[ήχος] Αδυναμία ανάγνωσης έντασης εφαρμογής: {exc}")
        if changes:
            save(record)
            import time
            steps = max(1, int(self.ramp_ms // 10))
            failed = set()
            for index in range(1, steps + 1):
                for session, item in changes:
                    if id(item) in failed:
                        continue
                    try:
                        session.set_volume(item["original"] * (1 - index / steps))
                    except Exception as exc:
                        failed.add(id(item))
                        self.log(f"[ήχος] Αποτυχία μείωσης εφαρμογής: {exc}")
                if index < steps and self.sleep is None:
                    time.sleep(self.ramp_ms / steps / 1000)
                elif index < steps:
                    self.sleep(self.ramp_ms / steps / 1000)
            for session, item in changes:
                if id(item) not in failed:
                    item["stage"] = "zero"
            save(record)

        # Τα media controls είναι προαιρετικά: αν λείπουν δεν πρέπει να
        # εμποδίσουν την ανακοίνωση. Ακολουθεί pause ΜΕΤΑ το volume 0%.
        try:
            asyncio.run(self._pause_media(record, save, sessions))
        except Exception as exc:
            self.log(f"[ήχος] Media Controls μη διαθέσιμα ({exc}); μόνο μείωση έντασης.")

    async def _pause_media(self, record, save, sessions):
        media = await self.media()
        for entry in media:
            try:
                if entry.status() != PLAYING or not entry.can_pause():
                    continue
                item = {"app_id": _media_key(entry), "intent": True}
                record["media"].append(item)
                save(record)
                accepted = await asyncio.wait_for(entry.pause(), timeout=1.0)
                paused = False
                if accepted:
                    # Το Windows Media status μπορεί να ενημερωθεί λίγο
                    # αργότερα από την επιτυχή απόκριση του pause.
                    for _ in range(6):
                        if entry.status() == PAUSED:
                            paused = True
                            break
                        await asyncio.sleep(0.1)
                item["paused"] = paused
                save(record)
                if not item["paused"]:
                    record["media"].remove(item)
                    save(record)
            except Exception as exc:
                self.log(f"[ήχος] Αποτυχία pause σε media ({exc}).")

    def restore(self, record: dict, save: Callable[[dict], None]) -> bool:
        """Δοκιμάζει media play, έπειτα επαναφορά session volume· ελέγχει identity."""
        ok = True
        if record.get("media"):
            try:
                asyncio.run(self._resume_media(record, save))
            except Exception as exc:
                self.log(f"[ήχος] Πρόβλημα επαναφοράς media: {exc}")
                ok = False
        if record.get("apps"):
            try:
                sessions = self.sessions()
            except Exception as exc:
                self.log(f"[ήχος] Αδυναμία ανάγνωσης εφαρμογών για επαναφορά: {exc}")
                return False
            changes = []
            for item in list(record["apps"]):
                matches = [s for s in sessions if _same_identity(s, item)]
                if not matches:
                    # Κλειστή/αντικατασταθείσα session: ποτέ σε νέα διεργασία.
                    self.log("[ήχος] Η προηγούμενη συνεδρία δεν υπάρχει πλέον.")
                    record["apps"].remove(item)
                    save(record)
                    continue
                session = matches[0]
                try:
                    current = session.get_volume()
                    original = item["original"]
                    can_restore = same_volume(current, 0.0) or (
                        item["stage"] in ("pending", "restoring")
                        and min(0.0, original) - 0.01 <= current
                        <= max(0.0, original) + 0.01)
                    if can_restore:
                        item["stage"] = "restoring"  # crash mid fade-in
                        changes.append((session, item, current))
                    else:
                        self.log("[ήχος] Χειροκίνητη αλλαγή έντασης· δεν αναιρείται.")
                        record["apps"].remove(item)
                        save(record)
                except Exception as exc:
                    ok = False
                    self.log(f"[ήχος] Εκκρεμεί επαναφορά εφαρμογής: {exc}")
            if changes:
                save(record)  # journaling ΠΡΙΝ το fade-in
                import time
                steps = max(1, int(self.ramp_ms // 10))
                failed = set()
                for index in range(1, steps + 1):
                    for session, item, current in changes:
                        if id(item) in failed:
                            continue
                        try:
                            session.set_volume(
                                item["original"] if index == steps
                                else current + (item["original"] - current) * index / steps)
                        except Exception as exc:
                            failed.add(id(item))
                            ok = False
                            self.log(f"[ήχος] Αποτυχία επαναφοράς εφαρμογής: {exc}")
                    if index < steps and self.sleep is None:
                        time.sleep(self.ramp_ms / steps / 1000)
                    elif index < steps:
                        self.sleep(self.ramp_ms / steps / 1000)
                for session, item, current in changes:
                    if id(item) not in failed:
                        record["apps"].remove(item)
                save(record)
        return ok and not record.get("apps") and not record.get("media")

    async def _resume_media(self, record, save):
        try:
            media = await self.media()
        except Exception as exc:
            self.log(f"[ήχος] Media Controls μη διαθέσιμα: {exc}")
            return
        for item in list(record["media"]):
            matches = [m for m in media if m.app_id == item["app_id"]]
            if len(matches) == 1:
                try:
                    # Μόνο όσα σταματήσαμε και παραμένουν σε pause.
                    # Μόνο επιβεβαιωμένο pause από εμάς: ένα crash στο μικρό
                    # διάστημα μεταξύ πρόθεσης και απόκρισης δεν δικαιολογεί
                    # αυτόματο play ενός media που ίσως σταμάτησε ο χρήστης.
                    if item.get("paused") is True and matches[0].status() == PAUSED:
                        accepted = await asyncio.wait_for(matches[0].play(), timeout=1.0)
                        if not accepted:
                            self.log("[ήχος] Media αρνήθηκε το play· εκκρεμεί επαναφορά.")
                            continue
                except Exception as exc:
                    self.log(f"[ήχος] Το play απέτυχε: {exc}")
                    continue
            record["media"].remove(item)
            save(record)


def restore_record_apps(record: dict, save: Callable[[dict], None],
                        *, app_audio: ApplicationAudio | None = None) -> bool:
    """Ανεξάρτητη επαναφορά και μετά από νέα εκκίνηση του Client."""
    if not record.get("apps") and not record.get("media"):
        return True
    if sys.platform != "win32" and app_audio is None:
        return False
    return (app_audio or ApplicationAudio()).restore(record, save)
