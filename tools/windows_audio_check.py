"""Διαγνωστική δοκιμή διαχείρισης ήχου σε Windows 10/11 (Φάση 3, βήμα 1).

Το script εκτελείται μία φορά από τον χρήστη, χωρίς δικαιώματα διαχειριστή, και
ελέγχει αν από απλό λογαριασμό μπορούμε:

1. να διαβάσουμε/ρυθμίσουμε την ένταση και το mute της προεπιλεγμένης εξόδου
   (Core Audio μέσω ``pycaw``)·
2. να σιγάσουμε/ξεσιγάσουμε τις συνεδρίες ήχου άλλων εφαρμογών·
3. να κάνουμε pause/play στα media που παίζουν (Windows System Media
   Transport Controls μέσω ``winrt``/``winsdk``).

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


def fade_mute(session: AppSession, ramp_ms: float, sleep: Sleep = time.sleep) -> None:
    """Χαμηλώνει σταδιακά στο 0, κάνει mute και ξαναβάζει την αρχική ένταση.

    Η ένταση της εφαρμογής μένει όπως ήταν· αλλάζει μόνο το mute. Έτσι, αν ο
    χρήστης την ξεσιγάσει χειροκίνητα, δεν τη βρίσκει «κολλημένη» στο 0.
    """
    if ramp_ms <= 0:
        session.set_mute(True)
        return
    original = session.get_volume()
    ramp_volume(session.set_volume, original, 0.0, ramp_ms, sleep)
    session.set_mute(True)
    session.set_volume(original)


def fade_unmute(session: AppSession, ramp_ms: float, sleep: Sleep = time.sleep) -> None:
    """Αντίστροφα: ένταση 0 όσο είναι σε mute, unmute και σταδιακή άνοδος."""
    if ramp_ms <= 0:
        session.set_mute(False)
        return
    original = session.get_volume()
    session.set_volume(0.0)
    session.set_mute(False)
    ramp_volume(session.set_volume, 0.0, original, ramp_ms, sleep)


def mute_other_sessions(sessions: Iterable[AppSession], own_pid: int, ramp_ms: float = 0,
                        sleep: Sleep = time.sleep) -> tuple[list[AppSession], list[str]]:
    """Σιγάζει όσες συνεδρίες δεν είναι ήδη σιγασμένες, εκτός από τη δική μας.

    Επιστρέφει τις συνεδρίες που σιγάσαμε εμείς και σημειώσεις για την αναφορά.
    """
    muted_by_us: list[AppSession] = []
    notes: list[str] = []
    for session in sessions:
        label = session_label(session)
        if session.pid == own_pid:
            notes.append(f"{label}: παραλείπεται (το ίδιο το script)")
            continue
        try:
            if session.get_mute():
                notes.append(f"{label}: ήταν ήδη σε mute, δεν αγγίζεται")
                continue
            fade_mute(session, ramp_ms, sleep)
            if session.get_mute():
                muted_by_us.append(session)
                notes.append(f"{label}: σίγαση OK")
            else:
                notes.append(f"{label}: η σίγαση δεν έπιασε")
        except Exception as exc:  # noqa: BLE001 - η αναφορά πρέπει να συνεχίσει
            notes.append(f"{label}: ΣΦΑΛΜΑ σίγασης: {exc!r}")
    return muted_by_us, notes


def unmute_our_sessions(muted_by_us: Iterable[AppSession], ramp_ms: float = 0,
                        sleep: Sleep = time.sleep) -> list[str]:
    """Ξεσιγάζει μόνο όσες σιγάσαμε εμείς και είναι ακόμη σιγασμένες."""
    notes: list[str] = []
    for session in muted_by_us:
        label = session_label(session)
        try:
            if not session.get_mute():
                notes.append(f"{label}: ήδη χωρίς mute (χειροκίνητη αλλαγή), δεν αγγίζεται")
                continue
            fade_unmute(session, ramp_ms, sleep)
            notes.append(f"{label}: επαναφορά OK")
        except Exception as exc:  # noqa: BLE001 - π.χ. η εφαρμογή έκλεισε
            notes.append(f"{label}: ΣΦΑΛΜΑ επαναφοράς (ίσως έκλεισε): {exc!r}")
    return notes


async def pause_playing_media(
    sessions: Iterable[MediaSession], settle_seconds: float = 1.0
) -> tuple[list[MediaSession], list[str]]:
    """Κάνει pause όσα παίζουν και επιστρέφει όσα πέρασαν πράγματι σε Paused."""
    candidates: list[MediaSession] = []
    notes: list[str] = []
    for session in sessions:
        status = session.status()
        if status != STATUS_PLAYING:
            notes.append(f"{session.app_id}: κατάσταση {status_name(status)}, δεν αγγίζεται")
            continue
        if not session.can_pause():
            notes.append(f"{session.app_id}: παίζει αλλά ΔΕΝ δηλώνει δυνατότητα pause")
            continue
        try:
            accepted = await session.pause()
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{session.app_id}: ΣΦΑΛΜΑ pause: {exc!r}")
            continue
        if not accepted:
            notes.append(f"{session.app_id}: το pause απορρίφθηκε")
            continue
        candidates.append(session)

    if candidates and settle_seconds > 0:
        await asyncio.sleep(settle_seconds)

    paused_by_us: list[MediaSession] = []
    for session in candidates:
        status = session.status()
        if status == STATUS_PAUSED:
            paused_by_us.append(session)
            notes.append(f"{session.app_id}: pause OK")
        else:
            notes.append(
                f"{session.app_id}: δέχτηκε το pause αλλά η κατάσταση είναι {status_name(status)}"
            )
    return paused_by_us, notes


async def resume_our_media(paused_by_us: Iterable[MediaSession]) -> list[str]:
    """Κάνει play μόνο όσα σταματήσαμε εμείς και είναι ακόμη σε Paused."""
    notes: list[str] = []
    for session in paused_by_us:
        try:
            status = session.status()
            if status != STATUS_PAUSED:
                notes.append(
                    f"{session.app_id}: κατάσταση {status_name(status)} (χειροκίνητη αλλαγή), δεν αγγίζεται"
                )
                continue
            accepted = await session.play()
            notes.append(f"{session.app_id}: play {'OK' if accepted else 'απορρίφθηκε'}")
        except Exception as exc:  # noqa: BLE001 - π.χ. το tab έκλεισε
            notes.append(f"{session.app_id}: ΣΦΑΛΜΑ play (ίσως έκλεισε): {exc!r}")
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
    report.section("2. Συνεδρίες ήχου εφαρμογών (σίγαση ανά εφαρμογή)")
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

    muted_by_us: list[AppSession] = []
    try:
        muted_by_us, notes = mute_other_sessions(sessions, os.getpid(), args.ramp_ms)
        for note in notes:
            report.add(f"Σίγαση: {note}")
        wait("Σταμάτησε να ακούγεται ο ήχος των εφαρμογών;")
    finally:
        for note in unmute_our_sessions(muted_by_us, args.ramp_ms):
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

        paused_by_us: list[MediaSession] = []
        try:
            paused_by_us, notes = await pause_playing_media(sessions)
            for note in notes:
                report.add(f"Pause: {note}")
            if not paused_by_us:
                report.add("Δεν έγινε pause σε καμία εφαρμογή (μήπως δεν έπαιζε κάτι;)")
                return
            # Η αναμονή μπλοκάρει το event loop σκόπιμα: δεν τρέχει τίποτε άλλο στο
            # μεταξύ, και έτσι το Ctrl+C φτάνει αμέσως στο finally.
            wait("Σταμάτησαν (pause) τα media που έπαιζαν;")
        finally:
            for note in await resume_our_media(paused_by_us):
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
