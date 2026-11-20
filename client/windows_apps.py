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
PAUSE_TIMEOUT_S = 1.0  # συνολικό όριο για όλα τα pause μαζί (εκτελούνται παράλληλα)
PLAY_TIMEOUT_S = 1.0


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
        # Τα media που σταμάτησε *αυτή* η εκτέλεση, ως ζωντανά αντικείμενα.
        # Το play γίνεται σε αυτά (όχι αναζήτηση με όνομα), ώστε να επανέρχονται
        # και δύο media της ίδιας εφαρμογής (π.χ. δύο παράθυρα Chrome).
        self._paused: list[tuple[object, dict]] = []

    def _ramp(self, session, start, end):
        if self.sleep is None:
            ramp_volume(session.set_volume, start, end, self.ramp_ms)
        else:
            ramp_volume(session.set_volume, start, end, self.ramp_ms, self.sleep)

    def begin(self, record: dict, save: Callable[[dict], None]) -> None:
        """Εφαρμογές στο 0% και μετά pause (για χρήση χωρίς ένταση Windows)."""
        self.fade_out_apps(record, save)
        self.pause_media(record, save)

    def fade_out_apps(self, record: dict, save: Callable[[dict], None]) -> None:
        """Γρήγορο μέρος (~150 ms): όλες οι εφαρμογές μαζί στο 0%."""
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

    def pause_media(self, record: dict, save: Callable[[dict], None]) -> None:
        """Αργό μέρος: pause, αφού οι εφαρμογές είναι ήδη στο 0%.

        Τα media controls είναι προαιρετικά: αν λείπουν δεν πρέπει να
        εμποδίσουν την ανακοίνωση.
        """
        record.setdefault("media", [])
        try:
            asyncio.run(self._pause_media(record, save))
        except Exception as exc:
            self.log(f"[ήχος] Media Controls μη διαθέσιμα ({exc}); μόνο μείωση έντασης.")

    async def _pause_media(self, record, save):
        media = await self.media()
        targets = []
        for entry in media:
            try:
                if entry.status() == PLAYING and entry.can_pause():
                    targets.append(entry)
            except Exception as exc:
                self.log(f"[ήχος] Αδυναμία ανάγνωσης media ({exc}).")
        if not targets:
            return
        items = [{"app_id": _media_key(entry), "intent": True} for entry in targets]
        record["media"].extend(items)
        save(record)  # πρόθεση ΠΡΙΝ από το pause

        async def pause_one(entry, item):
            try:
                if not await entry.pause():
                    return
                # Το Windows Media status μπορεί να ενημερωθεί λίγο αργότερα.
                while entry.status() != PAUSED:
                    await asyncio.sleep(0.05)
                item["paused"] = True
            except Exception as exc:
                self.log(f"[ήχος] Αποτυχία pause σε media ({exc}).")

        # Όλα τα pause παράλληλα, με ένα συνολικό όριο χρόνου.
        tasks = [asyncio.ensure_future(pause_one(e, i)) for e, i in zip(targets, items)]
        done, pending = await asyncio.wait(tasks, timeout=PAUSE_TIMEOUT_S)
        for task in pending:
            task.cancel()
        for entry, item in zip(targets, items):
            if item.get("paused") is True:
                self._paused.append((entry, item))
            else:
                record["media"].remove(item)
        save(record)

    def restore(self, record: dict, save: Callable[[dict], None], *,
                play_media: bool = True) -> bool:
        """Media play (μία προσπάθεια), έπειτα επαναφορά session volume.

        play_media=False στην ανάκαμψη μετά από crash: δεν ξέρουμε πόση ώρα
        πέρασε, οπότε επανέρχονται μόνο οι εντάσεις και ποτέ δεν γίνεται play.
        """
        ok = True
        if record.get("media"):
            try:
                if play_media:
                    asyncio.run(self._resume_media(record))
            except Exception as exc:
                self.log(f"[ήχος] Πρόβλημα επαναφοράς media: {exc}")
            # Μία μόνο προσπάθεια: ό,τι δεν επανήλθε δεν μένει στο journal,
            # ώστε να μη «κολλήσει» η λειτουργία ούτε να γίνει play αργότερα
            # σε media που ο χρήστης ίσως σταμάτησε ο ίδιος.
            for item in record["media"]:
                if not item.get("_resumed"):
                    self.log(f"[ήχος] Χωρίς play για {item.get('app_id') or 'media'}.")
            record["media"] = []
            self._paused = []
            save(record)
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

    async def _resume_media(self, record):
        """Play μόνο στα ζωντανά media που σταματήσαμε και είναι ακόμη σε pause."""
        in_record = {id(item) for item in record["media"]}

        async def play_one(entry, item):
            try:
                if entry.status() != PAUSED:
                    self.log("[ήχος] Το media άλλαξε κατάσταση από τον χρήστη· χωρίς play.")
                    return
                if await entry.play():
                    item["_resumed"] = True
                else:
                    self.log("[ήχος] Το media αρνήθηκε το play.")
            except Exception as exc:
                self.log(f"[ήχος] Το play απέτυχε: {exc}")

        tasks = [asyncio.ensure_future(play_one(entry, item))
                 for entry, item in self._paused if id(item) in in_record]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=PLAY_TIMEOUT_S)
            for task in pending:
                task.cancel()


def restore_record_apps(record: dict, save: Callable[[dict], None],
                        *, app_audio: ApplicationAudio | None = None,
                        play_media: bool = False) -> bool:
    """Επαναφορά εφαρμογών/media. Χωρίς play εκτός αν ζητηθεί ρητά.

    Η ανάκαμψη μετά από νέα εκκίνηση του Client καλεί με play_media=False.
    """
    if not record.get("apps") and not record.get("media"):
        return True
    if sys.platform != "win32" and app_audio is None:
        return False
    return (app_audio or ApplicationAudio()).restore(record, save, play_media=play_media)
