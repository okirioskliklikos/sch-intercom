"""Διαγνωστική δοκιμή διαχείρισης ήχου σε Windows 10/11 (Φάση 3, βήμα 1).

Το script εκτελείται μία φορά από τον χρήστη, χωρίς δικαιώματα διαχειριστή, και
ελέγχει αν από απλό λογαριασμό μπορούμε:

1. να διαβάσουμε/ρυθμίσουμε την ένταση και το mute της προεπιλεγμένης εξόδου
   (Core Audio μέσω ``pycaw``)·
2. να «σιγάσουμε» τις άλλες εφαρμογές χαμηλώνοντας σταδιακά την ένταση της
   συνεδρίας τους στο 0% και να την επαναφέρουμε σταδιακά·
3. να κάνουμε pause/play στα media που παίζουν (Windows System Media
   Transport Controls μέσω ``winrt``/``winsdk``): πρώτα ένταση στο 0% και μετά
   pause· στο τέλος πρώτα play και μετά σταδιακή επαναφορά της έντασης.

Κανόνας επαναφοράς: επαναφέρουμε **μόνο** ό,τι αλλάξαμε εμείς και βρίσκεται
ακόμη στην τιμή που ορίσαμε. Αν κάτι άλλαξε χειροκίνητα στο μεταξύ, δεν το
αγγίζουμε και το καταγράφουμε στην αναφορά.

Δεν είναι κώδικας παραγωγής· η πραγματική λειτουργία θα μπει αργότερα στον
Client. Σε μη Windows συστήματα το script απλώς τερματίζει με μήνυμα.

Εκτέλεση (Windows):
    py -m pip install pycaw comtypes psutil winrt-runtime ^
        winrt-Windows.Media.Control winrt-Windows.Foundation ^
        winrt-Windows.Foundation.Collections
    py tools\\windows_audio_check.py
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import os
import platform
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol

VOLUME_TOLERANCE = 0.01  # 1 % ανοχή στη σύγκριση έντασης (στρογγυλοποιήσεις Windows)
DEFAULT_RAMP_MS = 150  # σταδιακή αλλαγή έντασης αντί για απότομο βήμα («κλικ»)
RAMP_STEP_MS = 10

# Τιμές του GlobalSystemMediaTransportControlsSessionPlaybackStatus.
PLAYBACK_STATUS_NAMES = {
    0: "Closed",
    1: "Opened",
    2: "Changing",
    3: "Stopped",
    4: "Playing",
    5: "Paused",
}
STATUS_PLAYING = 4
STATUS_PAUSED = 5


# ---------------------------------------------------------------------------
# Αναφορά
# ---------------------------------------------------------------------------


class Report:
    """Συλλέγει γραμμές αναφοράς και τις τυπώνει ταυτόχρονα."""

    def __init__(self, echo: bool = True) -> None:
        self.lines: list[str] = []
        self.echo = echo

    def section(self, title: str) -> None:
        self.add("")
        self.add(f"== {title} ==")

    def add(self, line: str = "") -> None:
        self.lines.append(line)
        if self.echo:
            print(line, flush=True)

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


# ---------------------------------------------------------------------------
# Αφηρημένες διεπαφές (ώστε η λογική να ελέγχεται σε Linux με ψεύτικα αντικείμενα)
# ---------------------------------------------------------------------------


class MasterVolume(Protocol):
    def get_volume(self) -> float: ...  # 0.0–1.0
    def set_volume(self, value: float) -> None: ...
    def get_mute(self) -> bool: ...
    def set_mute(self, muted: bool) -> None: ...


class AppSession(Protocol):
    name: str
    pid: int

    def get_mute(self) -> bool: ...
    def set_mute(self, muted: bool) -> None: ...
    def get_volume(self) -> float: ...
    def set_volume(self, value: float) -> None: ...
    def is_active(self) -> bool: ...  # παίζει ήχο αυτή τη στιγμή


class MediaSession(Protocol):
    app_id: str

    def status(self) -> int: ...
    def can_pause(self) -> bool: ...
    def can_play(self) -> bool: ...
    async def pause(self) -> bool: ...
    async def play(self) -> bool: ...


# ---------------------------------------------------------------------------
# Λογική δοκιμών (ανεξάρτητη από Windows)
# ---------------------------------------------------------------------------


Sleep = Callable[[float], None]


def ramp_volume(set_volume: Callable[[float], None], start: float, end: float,
                ramp_ms: float, sleep: Sleep = time.sleep) -> None:
    """Αλλάζει την ένταση σταδιακά από start σε end μέσα σε ramp_ms.

    Με ramp_ms <= 0 η αλλαγή γίνεται με ένα βήμα (όπως στην αρχική έκδοση).
    """
    steps = max(1, int(ramp_ms // RAMP_STEP_MS)) if ramp_ms > 0 else 1
    for i in range(1, steps + 1):
        set_volume(end if i == steps else start + (end - start) * i / steps)
        if i < steps:
            sleep(ramp_ms / steps / 1000)


def pick_test_volume(current: float, requested: float) -> float:
    """Επιλέγει δοκιμαστική ένταση αισθητά διαφορετική από την τρέχουσα."""
    if abs(current - requested) >= 0.10:
        return requested
    return 0.60 if current < 0.45 else 0.20


def _same_volume(a: float, b: float) -> bool:
    return abs(a - b) <= VOLUME_TOLERANCE


@dataclass
class MasterChange:
    original_volume: float
    original_mute: bool
    applied_volume: float | None = None
    applied_mute: bool | None = None


def apply_master(master: MasterVolume, test_volume: float, ramp_ms: float = 0,
                 sleep: Sleep = time.sleep) -> MasterChange:
    """Αποθηκεύει την τρέχουσα κατάσταση και εφαρμόζει ένταση ανακοίνωσης χωρίς mute."""
    change = MasterChange(master.get_volume(), master.get_mute())
    ramp_volume(master.set_volume, change.original_volume, test_volume, ramp_ms, sleep)
    change.applied_volume = test_volume
    if change.original_mute:
        master.set_mute(False)
        change.applied_mute = False
    return change


def restore_master(master: MasterVolume, change: MasterChange, ramp_ms: float = 0,
                   sleep: Sleep = time.sleep) -> list[str]:
    """Επαναφέρει μόνο ό,τι αλλάξαμε και δεν άλλαξε χειροκίνητα στο μεταξύ."""
    notes: list[str] = []
    if change.applied_volume is not None:
        current = master.get_volume()
        if _same_volume(current, change.applied_volume):
            ramp_volume(master.set_volume, current, change.original_volume, ramp_ms, sleep)
            notes.append(f"ένταση: επαναφορά σε {pct(change.original_volume)}")
        else:
            notes.append(
                f"ένταση: ΔΕΝ επαναφέρθηκε, άλλαξε χειροκίνητα σε {pct(current)} "
                f"(είχαμε ορίσει {pct(change.applied_volume)})"
            )
    if change.applied_mute is not None:
        current_mute = master.get_mute()
        if current_mute == change.applied_mute:
            master.set_mute(change.original_mute)
            notes.append(f"mute: επαναφορά σε {onoff(change.original_mute)}")
        else:
            notes.append("mute: ΔΕΝ επαναφέρθηκε, άλλαξε χειροκίνητα")
    return notes


@dataclass
class VolumeHold:
    """Συνεδρία ήχου που χαμηλώσαμε εμείς στο 0% και η ένταση που είχε πριν."""

    session: AppSession
    original: float


def fade_out_session(session: AppSession, ramp_ms: float,
                     sleep: Sleep = time.sleep) -> VolumeHold:
    """Χαμηλώνει σταδιακά την ένταση της εφαρμογής στο 0% (χωρίς σημαία mute)."""
    original = session.get_volume()
    ramp_volume(session.set_volume, original, 0.0, ramp_ms, sleep)
    return VolumeHold(session, original)


def fade_in_session(hold: VolumeHold, ramp_ms: float, sleep: Sleep = time.sleep) -> str:
    """Επαναφέρει σταδιακά την ένταση μόνο αν είναι ακόμη στο 0% που ορίσαμε."""
    label = session_label(hold.session)
    try:
        current = hold.session.get_volume()
        if not _same_volume(current, 0.0):
            return f"{label}: ένταση {pct(current)} (χειροκίνητη αλλαγή), δεν αγγίζεται"
        ramp_volume(hold.session.set_volume, 0.0, hold.original, ramp_ms, sleep)
        return f"{label}: επαναφορά έντασης σε {pct(hold.original)} OK"
    except Exception as exc:  # noqa: BLE001 - π.χ. η εφαρμογή έκλεισε
        return f"{label}: ΣΦΑΛΜΑ επαναφοράς (ίσως έκλεισε): {exc!r}"


def silence_session(session: AppSession, ramp_ms: float,
                    sleep: Sleep = time.sleep) -> tuple[VolumeHold | None, str]:
    """Φέρνει μία συνεδρία στο 0%, εκτός αν είναι ήδη σε mute ή στο 0%."""
    label = session_label(session)
    try:
        if session.get_mute():
            return None, f"{label}: ήταν ήδη σε mute, δεν αγγίζεται"
        if _same_volume(session.get_volume(), 0.0):
            return None, f"{label}: ήταν ήδη στο 0%, δεν αγγίζεται"
        hold = fade_out_session(session, ramp_ms, sleep)
        if _same_volume(session.get_volume(), 0.0):
            return hold, f"{label}: {pct(hold.original)} → 0% OK"
        return None, f"{label}: η μείωση στο 0% δεν έπιασε"
    except Exception as exc:  # noqa: BLE001 - η αναφορά πρέπει να συνεχίσει
        return None, f"{label}: ΣΦΑΛΜΑ μείωσης έντασης: {exc!r}"


def mute_other_sessions(sessions: Iterable[AppSession], own_pid: int, ramp_ms: float = 0,
                        sleep: Sleep = time.sleep) -> tuple[list[VolumeHold], list[str]]:
    """«Σίγαση» = σταδιακή μείωση της έντασης κάθε άλλης εφαρμογής στο 0%.

    Δεν χρησιμοποιείται η σημαία mute, ώστε η επαναφορά να γίνεται επίσης
    σταδιακά. Επιστρέφει τις συνεδρίες που χαμηλώσαμε εμείς.
    """
    holds: list[VolumeHold] = []
    notes: list[str] = []
    for session in sessions:
        if session.pid == own_pid:
            notes.append(f"{session_label(session)}: παραλείπεται (το ίδιο το script)")
            continue
        hold, note = silence_session(session, ramp_ms, sleep)
        notes.append(note)
        if hold is not None:
            holds.append(hold)
    return holds, notes


def unmute_our_sessions(holds: Iterable[VolumeHold], ramp_ms: float = 0,
                        sleep: Sleep = time.sleep) -> list[str]:
    """Επαναφέρει σταδιακά μόνο όσες χαμηλώσαμε εμείς και είναι ακόμη στο 0%."""
    return [fade_in_session(hold, ramp_ms, sleep) for hold in holds]


def _process_stem(name: str) -> str:
    name = name.lower()
    return name[:-4] if name.endswith(".exe") else name


def match_audio_sessions(app_id: str, audio_sessions: Iterable[AppSession],
                         own_pid: int = -1) -> list[AppSession]:
    """Βρίσκει τις συνεδρίες ήχου της εφαρμογής ενός media session.

    Το Windows δίνει για τα media ένα AppUserModelID (π.χ. «Chrome», «MSEdge»,
    «Spotify.exe»), όχι PID. Ταιριάζουμε με το όνομα διεργασίας χωρίς «.exe».
    Ορισμένες εφαρμογές (π.χ. Firefox) δίνουν κωδικό που δεν ταιριάζει· τότε
    γίνεται pause χωρίς σταδιακή μείωση και καταγράφεται στην αναφορά.
    """
    target = app_id.lower()
    matches = []
    for session in audio_sessions:
        if session.pid in (0, own_pid):
            continue
        stem = _process_stem(session.name)
        if len(stem) >= 3 and stem in target:
            matches.append(session)
    return matches


# Εφαρμογές που δηλώνουν στα Windows media με κωδικό αντί για όνομα (π.χ. ο
# Firefox δίνει «308046B0AF4A39CB»). Χρησιμοποιούνται μόνο ως εφεδρεία.
FALLBACK_APPS = ("firefox",)


def fallback_audio_sessions(unmatched_playing: list[MediaSession],
                            audio_sessions: Iterable[AppSession],
                            claimed: Iterable[AppSession],
                            own_pid: int = -1) -> tuple[list[AppSession], str]:
    """Εφεδρική αντιστοίχιση για media χωρίς όνομα εφαρμογής (π.χ. Firefox).

    Ασφαλής μόνο όταν δεν υπάρχει αμφιβολία: ακριβώς ένα media που παίζει
    χωρίς αντιστοίχιση και ενεργές συνεδρίες ήχου από **μία** μόνο γνωστή
    εφαρμογή (FALLBACK_APPS) που δεν ανήκουν σε άλλο media. Αλλιώς επιστρέφει
    κενή λίστα και τον λόγο, ώστε να γίνει απλό pause.
    """
    if len(unmatched_playing) != 1:
        return [], f"{len(unmatched_playing)} media χωρίς αντιστοίχιση, δεν γίνεται εικασία"
    claimed_ids = {id(a) for a in claimed}
    pool = []
    for audio in audio_sessions:
        if audio.pid in (0, own_pid) or id(audio) in claimed_ids:
            continue
        if _process_stem(audio.name) not in FALLBACK_APPS:
            continue
        try:
            if audio.is_active():
                pool.append(audio)
        except Exception:  # noqa: BLE001
            continue
    apps = {_process_stem(a.name) for a in pool}
    if not pool:
        return [], "καμία ενεργή συνεδρία ήχου γνωστής εφαρμογής (π.χ. firefox.exe)"
    if len(apps) != 1:
        return [], "περισσότερες από μία πιθανές εφαρμογές: " + ", ".join(sorted(apps))
    return pool, "εφεδρική αντιστοίχιση με " + ", ".join(sorted({a.name for a in pool}))


@dataclass
class PausedMedia:
    session: MediaSession
    holds: list[VolumeHold] = field(default_factory=list)


async def pause_playing_media(
    sessions: Iterable[MediaSession],
    audio_sessions: Iterable[AppSession] = (),
    *,
    own_pid: int = -1,
    ramp_ms: float = 0,
    sleep: Sleep = time.sleep,
    settle_seconds: float = 1.0,
) -> tuple[list[PausedMedia], list[str]]:
    """Για κάθε media που παίζει: σταδιακά στο 0%, μετά pause.

    Επιστρέφει όσα πέρασαν πράγματι σε Paused, μαζί με τις εντάσεις που
    χαμηλώσαμε. Αν το pause αποτύχει, η ένταση επανέρχεται αμέσως.
    """
    audio_sessions = list(audio_sessions)
    sessions = list(sessions)
    candidates: list[PausedMedia] = []
    notes: list[str] = []

    # Αντιστοίχιση media → συνεδρίες ήχου: πρώτα με όνομα, μετά εφεδρικά.
    matches: dict[int, list[AppSession]] = {}
    claimed: list[AppSession] = []
    unmatched_playing: list[MediaSession] = []
    for session in sessions:
        found = match_audio_sessions(session.app_id, audio_sessions, own_pid)
        matches[id(session)] = found
        claimed.extend(found)
        try:
            playing = session.status() == STATUS_PLAYING and session.can_pause()
        except Exception:  # noqa: BLE001
            playing = False
        if not found and playing:
            unmatched_playing.append(session)
    fallback_note = ""
    if unmatched_playing:
        pool, fallback_note = fallback_audio_sessions(unmatched_playing, audio_sessions,
                                                      claimed, own_pid)
        if pool:
            matches[id(unmatched_playing[0])] = pool

    for session in sessions:
        status = session.status()
        if status != STATUS_PLAYING:
            notes.append(f"{session.app_id}: κατάσταση {status_name(status)}, δεν αγγίζεται")
            continue
        if not session.can_pause():
            notes.append(f"{session.app_id}: παίζει αλλά ΔΕΝ δηλώνει δυνατότητα pause")
            continue
        entry = PausedMedia(session)
        matched = matches.get(id(session), [])
        if session in unmatched_playing:
            if matched:
                notes.append(f"{session.app_id}: {fallback_note}")
            else:
                notes.append(f"{session.app_id}: δεν βρέθηκε συνεδρία ήχου ({fallback_note}), "
                             "pause χωρίς σταδιακή μείωση")
        for audio in matched:
            hold, note = silence_session(audio, ramp_ms, sleep)
            notes.append(f"{session.app_id}: {note}")
            if hold is not None:
                entry.holds.append(hold)
        try:
            accepted = await session.pause()
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{session.app_id}: ΣΦΑΛΜΑ pause: {exc!r}")
            accepted = False
        if not accepted:
            notes.append(f"{session.app_id}: το pause απέτυχε, επαναφορά έντασης")
            notes.extend(unmute_our_sessions(entry.holds, ramp_ms, sleep))
            continue
        candidates.append(entry)

    if candidates and settle_seconds > 0:
        await asyncio.sleep(settle_seconds)

    paused_by_us: list[PausedMedia] = []
    for entry in candidates:
        status = entry.session.status()
        if status == STATUS_PAUSED:
            paused_by_us.append(entry)
            notes.append(f"{entry.session.app_id}: pause OK")
        else:
            notes.append(
                f"{entry.session.app_id}: δέχτηκε το pause αλλά η κατάσταση είναι "
                f"{status_name(status)}, επαναφορά έντασης"
            )
            notes.extend(unmute_our_sessions(entry.holds, ramp_ms, sleep))
    return paused_by_us, notes


async def resume_our_media(paused_by_us: Iterable[PausedMedia], *, ramp_ms: float = 0,
                           sleep: Sleep = time.sleep) -> list[str]:
    """Play αμέσως (με ένταση ακόμη 0%) και μετά σταδιακή επαναφορά της έντασης.

    Play γίνεται μόνο σε όσα σταματήσαμε εμείς και είναι ακόμη σε Paused. Η
    ένταση επανέρχεται σε κάθε περίπτωση αν είναι ακόμη στο 0% που ορίσαμε,
    ώστε να ακούγεται και όταν ο χρήστης πάτησε play μόνος του.
    """
    notes: list[str] = []
    for entry in paused_by_us:
        app_id = entry.session.app_id
        try:
            status = entry.session.status()
            if status != STATUS_PAUSED:
                notes.append(
                    f"{app_id}: κατάσταση {status_name(status)} (χειροκίνητη αλλαγή), χωρίς play"
                )
            else:
                accepted = await entry.session.play()
                notes.append(f"{app_id}: play {'OK' if accepted else 'απορρίφθηκε'}")
        except Exception as exc:  # noqa: BLE001 - π.χ. το tab έκλεισε
            notes.append(f"{app_id}: ΣΦΑΛΜΑ play (ίσως έκλεισε): {exc!r}")
        notes.extend(f"{app_id}: {note}" for note in unmute_our_sessions(entry.holds, ramp_ms, sleep))
    return notes


# ---------------------------------------------------------------------------
# Βοηθητικά μορφοποίησης
# ---------------------------------------------------------------------------


def pct(value: float) -> str:
    return f"{round(value * 100)}%"


def onoff(muted: bool) -> str:
    return "ON" if muted else "OFF"


def status_name(status: int) -> str:
    return PLAYBACK_STATUS_NAMES.get(int(status), str(status))


def session_label(session: AppSession) -> str:
    return f"{session.name} (pid {session.pid})"


# ---------------------------------------------------------------------------
# Υλοποιήσεις Windows (εισάγονται μόνο σε Windows)
# ---------------------------------------------------------------------------


class PycawMaster:
    def __init__(self) -> None:
        from pycaw.pycaw import AudioUtilities

        speakers = AudioUtilities.GetSpeakers()
        self.device_name = getattr(speakers, "FriendlyName", None)
        endpoint = getattr(speakers, "EndpointVolume", None)
        if endpoint is None:
            # Παλαιότερες εκδόσεις pycaw: το GetSpeakers επιστρέφει IMMDevice.
            from ctypes import POINTER, cast

            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import IAudioEndpointVolume

            interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            endpoint = cast(interface, POINTER(IAudioEndpointVolume))
            if self.device_name is None:
                try:
                    self.device_name = AudioUtilities.CreateDevice(speakers).FriendlyName
                except Exception:  # noqa: BLE001
                    self.device_name = "(άγνωστο όνομα)"
        self._endpoint = endpoint

    def get_volume(self) -> float:
        return float(self._endpoint.GetMasterVolumeLevelScalar())

    def set_volume(self, value: float) -> None:
        self._endpoint.SetMasterVolumeLevelScalar(float(value), None)

    def get_mute(self) -> bool:
        return bool(self._endpoint.GetMute())

    def set_mute(self, muted: bool) -> None:
        self._endpoint.SetMute(1 if muted else 0, None)


class PycawSession:
    def __init__(self, session) -> None:
        self._session = session
        self._volume = session.SimpleAudioVolume
        process = session.Process
        self.pid = int(getattr(session, "ProcessId", 0) or 0)
        if process is not None:
            try:
                self.name = process.name()
            except Exception:  # noqa: BLE001
                self.name = "(άγνωστη διεργασία)"
        elif self.pid == 0:
            self.name = "Ήχοι συστήματος"
        else:
            self.name = "(άγνωστη διεργασία)"

    def get_mute(self) -> bool:
        return bool(self._volume.GetMute())

    def set_mute(self, muted: bool) -> None:
        self._volume.SetMute(1 if muted else 0, None)

    def get_volume(self) -> float:
        return float(self._volume.GetMasterVolume())

    def is_active(self) -> bool:
        # AudioSessionState: 0 = ανενεργή, 1 = ενεργή (παίζει), 2 = έληξε.
        return int(getattr(self._session, "State", 1)) == 1

    def set_volume(self, value: float) -> None:
        self._volume.SetMasterVolume(float(value), None)


def load_pycaw_sessions() -> list[PycawSession]:
    from pycaw.pycaw import AudioUtilities

    return [PycawSession(s) for s in AudioUtilities.GetAllSessions()]


def _import_media_control():
    """Επιστρέφει το module Windows.Media.Control από winrt ή winsdk."""
    try:
        from winrt.windows.media import control  # type: ignore

        return control, "winrt"
    except ImportError:
        from winsdk.windows.media import control  # type: ignore

        return control, "winsdk"


class SmtcSession:
    def __init__(self, session) -> None:
        self._session = session
        self.app_id = session.source_app_user_model_id or "(χωρίς όνομα)"

    def _info(self):
        return self._session.get_playback_info()

    def status(self) -> int:
        return int(self._info().playback_status)

    def can_pause(self) -> bool:
        return bool(self._info().controls.is_pause_enabled)

    def can_play(self) -> bool:
        return bool(self._info().controls.is_play_enabled)

    async def pause(self) -> bool:
        return bool(await self._session.try_pause_async())

    async def play(self) -> bool:
        return bool(await self._session.try_play_async())


async def load_smtc_sessions() -> list[SmtcSession]:
    control, _ = _import_media_control()
    manager = await control.GlobalSystemMediaTransportControlsSessionManager.request_async()
    return [SmtcSession(s) for s in manager.get_sessions()]


def is_admin() -> str:
    try:
        import ctypes

        return "ναι" if ctypes.windll.shell32.IsUserAnAdmin() else "όχι"
    except Exception:  # noqa: BLE001
        return "άγνωστο"


def package_version(name: str) -> str:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:  # noqa: BLE001
        return "δεν βρέθηκε"


# ---------------------------------------------------------------------------
# Ροή εκτέλεσης
# ---------------------------------------------------------------------------


def make_waiter(interactive: bool, hold_seconds: float, report: Report) -> Callable[[str], None]:
    """Αναμονή όσο ισχύει η αλλαγή: ερώτηση στον χρήστη ή σκέτη καθυστέρηση."""

    def wait(question: str) -> None:
        if interactive:
            try:
                answer = input(f">>> {question} [ν/ο, Enter = δεν ξέρω]: ").strip().lower()
            except EOFError:
                answer = ""
            mapping = {"ν": "ναι", "n": "ναι", "y": "ναι", "ο": "όχι", "o": "όχι"}
            report.add(f"Απάντηση χρήστη: «{question}» → {mapping.get(answer, answer or 'δεν απαντήθηκε')}")
        else:
            print(f"... αναμονή {hold_seconds:g} s ({question})", flush=True)
            time.sleep(hold_seconds)

    return wait


def check_master(report: Report, args, wait: Callable[[str], None]) -> None:
    report.section("1. Ένταση/mute προεπιλεγμένης εξόδου (Core Audio)")
    try:
        master = PycawMaster()
    except Exception as exc:  # noqa: BLE001
        report.add(f"ΣΦΑΛΜΑ πρόσβασης στη συσκευή εξόδου: {exc!r}")
        return
    report.add(f"Συσκευή: {master.device_name}")
    report.add(f"Τρέχουσα ένταση: {pct(master.get_volume())}, mute: {onoff(master.get_mute())}")
    if args.dry_run:
        report.add("(dry-run: χωρίς αλλαγές)")
        return

    target = pick_test_volume(master.get_volume(), args.test_volume / 100)
    change = None
    try:
        change = apply_master(master, target, args.ramp_ms)
        now_vol, now_mute = master.get_volume(), master.get_mute()
        ok = _same_volume(now_vol, target) and not now_mute
        report.add(
            f"Ορίστηκε δοκιμαστική ένταση {pct(target)} χωρίς mute → διαβάστηκε "
            f"{pct(now_vol)}, mute {onoff(now_mute)}: {'OK' if ok else 'ΑΠΟΚΛΙΣΗ'}"
        )
        wait(f"Ακούστηκε η αλλαγή έντασης στο {pct(target)};")
    finally:
        if change is not None:
            for note in restore_master(master, change, args.ramp_ms):
                report.add(f"Επαναφορά {note}")
            report.add(
                f"Τελική ένταση: {pct(master.get_volume())}, mute: {onoff(master.get_mute())}"
            )


def check_sessions(report: Report, args, wait: Callable[[str], None]) -> None:
    report.section("2. Συνεδρίες ήχου εφαρμογών (σταδιακά στο 0% και πίσω)")
    try:
        sessions = load_pycaw_sessions()
    except Exception as exc:  # noqa: BLE001
        report.add(f"ΣΦΑΛΜΑ ανάγνωσης συνεδριών: {exc!r}")
        return
    if not sessions:
        report.add("Δεν βρέθηκαν συνεδρίες ήχου. Βάλε κάτι να παίζει και ξανατρέξε.")
        return
    for s in sessions:
        try:
            report.add(f"- {session_label(s)}: ένταση {pct(s.get_volume())}, mute {onoff(s.get_mute())}")
        except Exception as exc:  # noqa: BLE001
            report.add(f"- {session_label(s)}: ΣΦΑΛΜΑ ανάγνωσης {exc!r}")
    if args.dry_run:
        report.add("(dry-run: χωρίς αλλαγές)")
        return

    holds: list[VolumeHold] = []
    try:
        holds, notes = mute_other_sessions(sessions, os.getpid(), args.ramp_ms)
        for note in notes:
            report.add(f"Σίγαση: {note}")
        wait("Σταμάτησε να ακούγεται ο ήχος των εφαρμογών;")
    finally:
        for note in unmute_our_sessions(holds, args.ramp_ms):
            report.add(f"Επαναφορά: {note}")


def check_media(report: Report, args, wait: Callable[[str], None]) -> None:
    report.section("3. Pause/Play media (System Media Transport Controls)")
    try:
        _, source = _import_media_control()
    except ImportError as exc:
        report.add(f"Δεν βρέθηκε winrt/winsdk ({exc}). Δες docs/windows-audio-check.md.")
        return
    report.add(f"Βιβλιοθήκη: {source}")

    async def run() -> None:
        sessions = await load_smtc_sessions()
        if not sessions:
            report.add("Καμία εφαρμογή δεν δηλώνει media στα Windows αυτή τη στιγμή.")
            return
        for s in sessions:
            try:
                report.add(
                    f"- {s.app_id}: {status_name(s.status())}, "
                    f"pause {'ναι' if s.can_pause() else 'όχι'}, play {'ναι' if s.can_play() else 'όχι'}"
                )
            except Exception as exc:  # noqa: BLE001
                report.add(f"- {s.app_id}: ΣΦΑΛΜΑ ανάγνωσης {exc!r}")
        if args.dry_run:
            report.add("(dry-run: χωρίς αλλαγές)")
            return

        try:
            audio_sessions = load_pycaw_sessions()
        except Exception as exc:  # noqa: BLE001
            report.add(f"Χωρίς συνεδρίες ήχου ({exc!r}): pause χωρίς σταδιακή μείωση")
            audio_sessions = []

        paused_by_us: list[PausedMedia] = []
        try:
            paused_by_us, notes = await pause_playing_media(
                sessions, audio_sessions, own_pid=os.getpid(), ramp_ms=args.ramp_ms)
            for note in notes:
                report.add(f"Pause: {note}")
            if not paused_by_us:
                report.add("Δεν έγινε pause σε καμία εφαρμογή (μήπως δεν έπαιζε κάτι;)")
                return
            # Η αναμονή μπλοκάρει το event loop σκόπιμα: δεν τρέχει τίποτε άλλο στο
            # μεταξύ, και έτσι το Ctrl+C φτάνει αμέσως στο finally.
            wait("Σταμάτησαν (pause) τα media που έπαιζαν;")
        finally:
            for note in await resume_our_media(paused_by_us, ramp_ms=args.ramp_ms):
                report.add(f"Συνέχιση: {note}")

    try:
        asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        report.add(f"ΣΦΑΛΜΑ SMTC: {exc!r}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Διαγνωστική δοκιμή ήχου Windows (Φάση 3)")
    parser.add_argument("--dry-run", action="store_true", help="μόνο ανάγνωση, χωρίς αλλαγές")
    parser.add_argument(
        "--test-volume", type=int, default=30, help="δοκιμαστική ένταση %% (προεπιλογή 30)"
    )
    parser.add_argument(
        "--hold-seconds", type=float, default=5.0,
        help="διάρκεια κάθε αλλαγής όταν δεν γίνονται ερωτήσεις (προεπιλογή 5)",
    )
    parser.add_argument(
        "--ramp-ms", type=float, default=DEFAULT_RAMP_MS,
        help=f"σταδιακή αλλαγή έντασης/σίγασης σε ms, 0 = απότομα (προεπιλογή {DEFAULT_RAMP_MS})",
    )
    parser.add_argument("--no-questions", action="store_true", help="χωρίς ερωτήσεις ν/ο")
    parser.add_argument("--report", default=None, help="αρχείο αναφοράς (προεπιλογή με ημερομηνία)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if sys.platform != "win32":
        print("Το script τρέχει μόνο σε Windows 10/11.")
        return 2
    if not 0 <= args.test_volume <= 100:
        print("Το --test-volume πρέπει να είναι 0–100.")
        return 2

    report = Report()
    report.add("School Intercom — διαγνωστική δοκιμή ήχου Windows")
    report.add(f"Ημερομηνία: {_dt.datetime.now():%Y-%m-%d %H:%M:%S}")
    report.add(f"Windows: {platform.platform()} (build {platform.version()})")
    report.add(f"Python: {platform.python_version()} {platform.architecture()[0]}")
    report.add(f"Διαχειριστής: {is_admin()}")
    for pkg in ("pycaw", "comtypes", "psutil", "winrt-runtime", "winrt-Windows.Media.Control", "winsdk"):
        report.add(f"{pkg}: {package_version(pkg)}")
    report.add(f"Λειτουργία: {'dry-run (μόνο ανάγνωση)' if args.dry_run else 'με αλλαγές και επαναφορά'}")
    report.add(f"Σταδιακή αλλαγή (ramp): {args.ramp_ms:g} ms")

    interactive = sys.stdin.isatty() and not args.no_questions and not args.dry_run
    wait = make_waiter(interactive, args.hold_seconds, report)

    try:
        check_master(report, args, wait)
        check_sessions(report, args, wait)
        check_media(report, args, wait)
    except KeyboardInterrupt:
        report.add("")
        report.add("Διακόπηκε από τον χρήστη (Ctrl+C). Οι επαναφορές που είχαν ξεκινήσει ολοκληρώθηκαν.")

    report.section("Τέλος")
    path = args.report or f"windows_audio_check_{_dt.datetime.now():%Y%m%d_%H%M%S}.txt"
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(report.text())
        print(f"Η αναφορά αποθηκεύτηκε στο: {os.path.abspath(path)}")
    except OSError as exc:
        print(f"Δεν αποθηκεύτηκε η αναφορά ({exc}). Αντέγραψε το κείμενο από το παράθυρο.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
