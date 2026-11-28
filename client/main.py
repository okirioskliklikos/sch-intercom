"""Client: σύνδεση στον Director και αναπαραγωγή PCM.

ΠΡΟΣΟΧΗ: πειραματικό TLS v6 με πιστοποιημένη ταυτότητα Client.
Η δικτυακή εγγραφή με SAS ενεργοποιείται ρητά από τον χρήστη.
Χρήση μόνο σε ελεγχόμενο δίκτυο δοκιμών.
"""

import argparse
import os
import queue
import random
from pathlib import Path
import socket
import ssl
import threading
import time
from collections.abc import Callable

from shared.audio import (BLOCK_BYTES, BLOCK_MS, BLOCK_SAMPLES, CHANNELS, DEFAULT_PORT,
                          DTYPE, SAMPLE_RATE, Frame, MESSAGE_STREAM_STOP,
                          MESSAGE_CLIENT_SETTINGS, MESSAGE_UPDATE_CHECK, MESSAGE_CHANGE_STATUS,
                          MESSAGE_NEW_IDENTITY, decode_new_identity,
                          decode_client_settings, receive_message)
from shared.jitter import DelayTracker, JitterBuffer
from shared.identity import (ManagerFull, ProtocolMismatch, load_identity, receive_welcome,
                             send_hello)
from shared.enrollment import (ENROLL_PORT, SPONSOR_CLONE, SPONSOR_RESET, amend_enrollment,
                               make_amend, make_request, make_sponsor, enroll_client)
from shared import change_request as cr
from shared.client_hints import load_hints, save_hints
from shared.room_names import room_title
from shared.registry_seen import load_seen, manager_key, record_seen
from shared.device_identity import create_device_identity, replace_device_identity
from shared.transport_security import client_context, connect_tls, DEFAULT_SERVER_NAME
from shared.discovery import discover_candidates, DISCOVERY_PORT
from shared.manager_pin import load_pin, save_pin
from client.install_paths import MANAGER_CERT, Layout, data_dir, load_settings, save_settings
from client.changes import ChangeController, watch_inbox
from client.updater import AnnouncementState, Updater, peer_host
from client.supervisor import RESULT_FILE, TRIAL_ENV, send_command
from shared.update_protocol import UPDATE_PORT
from client.identity_reset import (CLONED, PREVIOUS, archive_identity, check_clone,
                                   old_identity,
                                   previous_room, remember_machine)
from shared.control import heartbeat_sender
from client.windows_volume import (AnnouncementVolume, AudioPolicyWorker,
                                   create_volume_controller, recover_pending_volume)
from client.windows_apps import ApplicationAudio
from client import status as st
from client.supervisor import VERSION_ENV
from shared.version import APP_VERSION

#: Νέα προσπάθεια μετά από 5 s ±1 s: το τυχαίο jitter απλώνει τις επανασυνδέσεις
#: όταν πολλοί Clients ξεκινούν μαζί (π.χ. πρωινό άνοιγμα υπολογιστών).
RECONNECT_DELAY_SECONDS = 5
RECONNECT_JITTER_SECONDS = 1
#: 4.3C1: ο Manager έχει ήδη το μέγιστο πλήθος υπολογιστών· αραιότερες προσπάθειες.
FULL_RETRY_SECONDS = 30
# Έγκυρο PCM, KEEPALIVE ή STREAM_STOP ανανεώνει το όριο. Απώλεια όλων
# των μηνυμάτων για 3 s ενεργοποιεί τον υπάρχοντα μηχανισμό επανασύνδεσης.
RECEIVE_TIMEOUT_SECONDS = 3.0
CONNECT_TIMEOUT_SECONDS = 5
#: Στόχος καθυστέρησης της ουράς. Ήταν 250 ms ως σκληρό όριο (τα παραπάνω πετιούνταν).
#: Με την προσαρμοζόμενη ουρά (4.4, απόφαση χρήστη 26/11/2026) οι ριπές μετά από κόλλημα
#: δικτύου κρατιούνται έως DEFAULT_MAX_BUFFER_MS και η καθυστέρηση επιστρέφει σε αυτόν
#: τον στόχο (λίγο πάνω από το prefill) μέσα στις παύσεις της ομιλίας.
DEFAULT_BUFFER_MS = 160
#: Ανώτατο κράτημα ριπών. 0 = παλιά συμπεριφορά (σκληρό όριο = buffer_ms).
DEFAULT_MAX_BUFFER_MS = 1500
ANNOUNCEMENT_PREFILL_MS = 120
# Fade-in/fade-out αρχής/τέλους ανακοίνωσης και σύντομο fade-in μετά από κενό
# δικτύου, ώστε να μην ακούγεται «κλικ» στα απότομα περάσματα σιωπή ↔ φωνή.
DEFAULT_FADE_MS = 150
DEFAULT_RESUME_FADE_MS = 20
REPORT_EVERY_SECONDS = 5
AUDIO_RESTORE_GRACE_SECONDS = 1.5
# 40 ms: δοκιμάστηκε από τον χρήστη σε Windows χωρίς κροτάλισμα (το "low" έδινε 100 ms).
DEFAULT_LATENCY = 0.04


def now_us() -> int:
    return time.monotonic_ns() // 1000


def format_report(jitter: JitterBuffer, delay: DelayTracker, received: int,
                  *, mode: str = "ανακοίνωση") -> str:
    if mode == "αναμονή":
        return ("[Client] κατάσταση: αναμονή | blocks "
                f"{received} | ουρά — | πεταμένα — | κενά — | δίκτυο —")
    levels = jitter.take_level_stats()
    net = delay.take_stats()
    queue_text = "—"
    if levels:
        low, avg, high = levels
        queue_text = f"{low * BLOCK_MS}/{avg * BLOCK_MS:.0f}/{high * BLOCK_MS} ms"
    net_text = "—" if net is None else f"{net[0]:.0f}/{net[1]:.0f} ms"
    return (
        f"[Client] κατάσταση: ανακοίνωση | blocks {received} | "
        f"ουρά min/μέση/max {queue_text} | "
        f"πεταμένα {jitter.dropped} | κενά {jitter.underruns} | "
        f"παύσεις που κόντυναν {jitter.skipped * BLOCK_MS} ms | "
        f"επιπλέον καθυστέρηση δικτύου μέση/max {net_text}"
    )


def pump_frames(
    connection: socket.socket,
    jitter: JitterBuffer,
    delay: DelayTracker,
    *,
    clock_us: Callable[[], int] = now_us,
    report: Callable[[str], None] | None = None,
    report_every_s: float = REPORT_EVERY_SECONDS,
    volume: AnnouncementVolume | AudioPolicyWorker | None = None,
    settings_callback=None,
    test_report=None,
    announcement: AnnouncementState | None = None,
    update_check: Callable[[], None] | None = None,
    change_status: Callable[[dict], None] | None = None,
    new_identity: Callable[[dict], None] | None = None,
) -> None:
    """PCM/έλεγχος στην ίδια κατεύθυνση TCP, χωρίς reconnect σε αναμονή.

    test_report: μόνο στις δοκιμές 4.4 (``--test-report``)· καταγράφει λήψεις και STOP.
    announcement: 5B2, η λήψη ενημέρωσης σταματά όσο παίζει ανακοίνωση.
    update_check: 5B2, ο Manager ζήτησε CHECK ενημέρωσης τώρα («Έναρξη τώρα»).
    change_status: 5C2, απάντηση του Director για το αίτημα αλλαγής αίθουσας/ονόματος.
    new_identity: 5C3, ο Director ζήτησε νέα ταυτότητα ({room, name, token}).
    Άγνωστα μικρά μηνύματα ελέγχου (νεότερου Manager) αγνοούνται.
    """
    received = 0
    in_announcement = False
    stop_received_at: float | None = None
    drained_at: float | None = None
    next_report = time.monotonic() + report_every_s
    timeout = connection.gettimeout()
    last_valid = time.monotonic()
    while True:
        # Η προθεσμία δεν παρατείνεται από μισό/άκυρο πακέτο.
        deadline = last_valid + timeout if timeout is not None and timeout > 0 else None
        message = receive_message(connection, deadline=deadline)
        last_valid = time.monotonic()
        if isinstance(message, Frame):
            if not in_announcement:
                if volume is not None:
                    try:
                        volume.begin()  # προετοιμασία πριν αρχίσει το prefill/η αναπαραγωγή
                    except Exception as exc:
                        print(f"[ήχος] Αδυναμία προσωρινής έντασης: {exc}", flush=True)
                jitter.begin_announcement()
                stop_received_at = None
                drained_at = None
                delay.reset()
                in_announcement = True
                if announcement is not None:
                    announcement.set(True)
            arrived_us = clock_us()
            delay.record(message.seq, message.timestamp_us, arrived_us)
            if test_report is not None:
                test_report.event("rx", t=arrived_us, seq=message.seq, ts=message.timestamp_us)
            jitter.push(message.pcm, message.seq)
            received += 1
        elif message.kind == MESSAGE_CLIENT_SETTINGS:
            if settings_callback is not None:
                try:
                    settings_callback(decode_client_settings(message.payload))
                except (OSError, ValueError) as exc:
                    print(f"[Client] Αποτυχία τοπικής αποθήκευσης στοιχείων: {exc}",
                          flush=True)
        elif message.kind == MESSAGE_UPDATE_CHECK:
            if update_check is not None:
                update_check()
        elif message.kind == MESSAGE_CHANGE_STATUS:
            if change_status is not None:
                try:
                    change_status(cr.decode_status(message.payload))
                except (OSError, ValueError) as exc:
                    print(f"[Client] Αγνοείται απάντηση αιτήματος αλλαγής: {exc}", flush=True)
        elif message.kind == MESSAGE_NEW_IDENTITY:
            if new_identity is not None:
                try:
                    data = decode_new_identity(message.payload)
                except ValueError as exc:
                    print(f"[Client] Αγνοείται εντολή νέας ταυτότητας: {exc}", flush=True)
                else:
                    new_identity(data)
        elif message.kind == MESSAGE_STREAM_STOP:
            if test_report is not None:
                test_report.event("stop", underruns=jitter.underruns, dropped=jitter.dropped)
            if in_announcement:
                jitter.finish_announcement()
                in_announcement = False
                if announcement is not None:
                    announcement.set(False)
                stop_received_at = time.monotonic()
                drained_at = None
        # Μετά το STREAM_STOP περιμένουμε να αδειάσει η ουρά από το callback.
        # Τα KEEPALIVE ανά 0,5 s δίνουν περιθώριο και για τη latency της κάρτας.
        if volume is not None and volume.active and not in_announcement:
            if stop_received_at is not None and jitter.mode == "αναμονή":
                if drained_at is None:
                    drained_at = time.monotonic()
                if time.monotonic() - drained_at >= AUDIO_RESTORE_GRACE_SECONDS:
                    try:
                        volume.end()
                    except Exception as exc:  # noqa: BLE001
                        print(f"[ήχος] Αποτυχία επαναφοράς ήχου: {exc}", flush=True)
        # KEEPALIVE: μόνο επιβεβαίωση ζωντανής σύνδεσης· όχι PCM/underrun.
        if report and time.monotonic() >= next_report:
            mode = "ανακοίνωση" if in_announcement or jitter.mode == "ανακοίνωση" else "αναμονή"
            report(format_report(jitter, delay, received, mode=mode))
            next_report = time.monotonic() + report_every_s


def make_jitter_buffer(buffer_ms: int = DEFAULT_BUFFER_MS, fade_ms: int = DEFAULT_FADE_MS,
                       resume_fade_ms: int = DEFAULT_RESUME_FADE_MS,
                       max_buffer_ms: int = DEFAULT_MAX_BUFFER_MS) -> JitterBuffer:
    fade_samples = max(0, fade_ms) * SAMPLE_RATE // 1000
    target = max(1, buffer_ms // BLOCK_MS)
    adaptive = max_buffer_ms > buffer_ms
    return JitterBuffer(max(target, max_buffer_ms // BLOCK_MS) if adaptive else target,
                        BLOCK_BYTES, target_blocks=target if adaptive else None,
                        prefill_blocks=max(1, ANNOUNCEMENT_PREFILL_MS // BLOCK_MS),
                        fade_in_samples=fade_samples, fade_out_samples=fade_samples,
                        resume_fade_samples=max(0, resume_fade_ms) * SAMPLE_RATE // 1000)


def play_stream(
    connection: socket.socket,
    *,
    device: int | None = None,
    buffer_ms: int = DEFAULT_BUFFER_MS,
    max_buffer_ms: int = DEFAULT_MAX_BUFFER_MS,
    latency: str | float = DEFAULT_LATENCY,
    fade_ms: int = DEFAULT_FADE_MS,
    resume_fade_ms: int = DEFAULT_RESUME_FADE_MS,
    announcement_volume: int | None = None,
    mute_others: bool = False,
    recovery_path: Path | None = None,
    settings_callback=None,
    test_report=None,
    announcement: AnnouncementState | None = None,
    update_check: Callable[[], None] | None = None,
    on_output: Callable[[object], None] | None = None,
    change_status: Callable[[dict], None] | None = None,
    new_identity: Callable[[dict], None] | None = None,
) -> None:
    import sounddevice as sd

    jitter = make_jitter_buffer(buffer_ms, fade_ms, resume_fade_ms, max_buffer_ms)
    jitter.standby()
    delay = DelayTracker()
    journal = recovery_path or data_dir() / "volume-recovery.json"
    # Αν προηγούμενος Client έπεσε, ανάκαμψη και χωρίς νέα ενεργοποίηση
    # της λειτουργίας από CLI. Δεν επιχειρούμε αλλαγές σε Linux.
    # Κανένα πρόβλημα έντασης/ανάκαμψης δεν σταματά τον Client: μόνο προειδοποίηση.
    if announcement_volume is None:
        recover_pending_volume(journal, device=device)
    # Οι αλλαγές ήχου Windows (fade, pause/play) γίνονται σε δικό τους νήμα,
    # ώστε η λήψη να μη σταματά και να μη χάνονται οι πρώτες λέξεις.
    volume = None
    if announcement_volume is not None:
        volume = AudioPolicyWorker(lambda: create_volume_controller(
            announcement_volume, device=device, recovery_path=journal,
            apps=ApplicationAudio(log=lambda msg: print(msg, flush=True))
            if mute_others else None))

    output_latency_us = int(DEFAULT_LATENCY * 1_000_000)

    def on_audio(outdata, frames, timestamp, status):
        if status:
            print(f"[ήχος] {status}", flush=True)
        underruns = jitter.underruns
        block = jitter.pop()
        if test_report is not None and jitter.underruns > underruns:
            test_report.event("underrun")
        if test_report is not None and jitter.skipped_tag is not None:
            test_report.event("skip", seq=jitter.skipped_tag)   # σιωπή που παραλείφθηκε
        if test_report is not None and jitter.last_tag is None and jitter.playing:
            test_report.event("silence")      # ένα block σιωπής μέσα σε ανακοίνωση
        if test_report is not None and jitter.last_tag is not None:
            # Πότε θα ακουστεί το block: τώρα + καθυστέρηση εξόδου που δίνει το PortAudio.
            try:
                ahead = timestamp.outputBufferDacTime - timestamp.currentTime
            except AttributeError:
                ahead = 0.0
            ahead_us = int(ahead * 1_000_000) if 0 < ahead < 1 else output_latency_us
            test_report.event("play", t=now_us() + ahead_us, seq=jitter.last_tag)
        if frames == BLOCK_SAMPLES:
            outdata[:] = block
        else:
            outdata[:] = bytes(len(outdata))

    with sd.RawOutputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype=DTYPE,
        blocksize=BLOCK_SAMPLES,
        device=device,
        latency=latency,
        callback=on_audio,
    ) as output:
        output_latency_us = int(output.latency * 1_000_000)
        if on_output is not None:
            try:
                on_output(output)   # 5C1: όνομα συσκευής για τις «Πληροφορίες»
            except Exception:  # noqa: BLE001
                pass
        print(
            f"Λήψη και αναπαραγωγή ήχου... όριο ουράς {jitter.max_blocks * BLOCK_MS} ms, "
            f"καθυστέρηση κάρτας ήχου {output.latency * 1000:.0f} ms",
            flush=True,
        )
        try:
            pump_frames(connection, jitter, delay,
                        report=lambda line: print(line, flush=True), volume=volume,
                        settings_callback=settings_callback, test_report=test_report,
                        announcement=announcement, update_check=update_check,
                        change_status=change_status, new_identity=new_identity)
        finally:
            if volume is not None:
                # Δίκτυο/τερματισμός: καλύτερα αποκατάσταση παρά παραμονή σε
                # αυξημένη ένταση. Το κανονικό STOP περιμένει πρώτα το drain.
                # Σφάλμα εδώ δεν πρέπει να κρύψει το αρχικό ούτε να σταματήσει
                # την επανασύνδεση· το αρχείο ανάκαμψης μένει για την επόμενη φορά.
                try:
                    volume.close()
                except Exception as exc:  # noqa: BLE001
                    print(f"[ήχος] Αποτυχία επαναφοράς έντασης: {exc}", flush=True)


def parse_latency(value: str) -> str | float:
    if value in ("low", "high"):
        return value
    return float(value)


def _retry_delay(base: float, jitter: float) -> float:
    """base ± jitter δευτερόλεπτα (ποτέ αρνητικό)."""
    if jitter <= 0:
        return base
    return max(0.0, base + random.uniform(-jitter, jitter))


def _require_tls(tls_context: ssl.SSLContext | None) -> None:
    """Χωρίς πιστοποιητικό Director δεν γίνεται σύνδεση (όχι σιωπηλό plaintext)."""
    if not isinstance(tls_context, ssl.SSLContext):
        raise ValueError("Απαιτείται TLS: ορίστε το πιστοποιητικό του Director (--tls-ca)")


def open_connection(host: str, port: int, *,
                    receive_timeout: float = RECEIVE_TIMEOUT_SECONDS,
                    tls_context: ssl.SSLContext | None = None,
                    tls_server_name: str = DEFAULT_SERVER_NAME) -> socket.socket:
    """Συνδέεται στον Director με υποχρεωτικό TLS και όριο αναμονής για κάθε λήψη."""
    _require_tls(tls_context)
    connection = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_SECONDS)
    try:
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        connection = connect_tls(connection, tls_context, tls_server_name)
        connection.settimeout(receive_timeout)
    except OSError:
        connection.close()
        raise
    return connection


def _fingerprint(connection: socket.socket) -> str:
    peer = getattr(connection, "getpeercert", None)   # οι ψεύτικες συνδέσεις των tests
    return manager_key((peer(binary_form=True) if peer else None) or b"")


def _close_quietly(connection: socket.socket) -> None:
    try:
        connection.close()
    except OSError:
        pass


def _verified_connection(candidates, *, port, pin, log, **connect):
    """Δοκιμάζει τους υποψηφίους με TLS· επιστρέφει (σύνδεση, ID) ή σηκώνει σφάλμα.

    Ένας υποψήφιος: απευθείας. Πολλοί: δοκιμάζονται όλοι (έως 8)· όσοι περνούν το TLS
    είναι ο ίδιος Manager αν έχουν το ίδιο πιστοποιητικό (π.χ. δύο κάρτες δικτύου).
    Με διαφορετικά πιστοποιητικά προτιμάται ο γνωστός (pin)· αλλιώς καμία σύνδεση.
    """
    verified: list[tuple[socket.socket, str | None, str]] = []
    for host, target_port, identifier in candidates:
        log(f"Σύνδεση στον Director {host}:{target_port}...")
        try:
            connection = open_connection(host, target_port, **connect)
        except OSError as exc:   # περιλαμβάνει ssl.SSLError (λάθος πιστοποιητικό)
            if len(candidates) > 1:
                log(f"Ο {host}:{target_port} δεν επαληθεύτηκε ως Manager: {exc}")
                continue
            raise
        verified.append((connection, identifier, f"{host}:{target_port}"))
        if len(candidates) == 1:
            break
    if not verified:
        raise ConnectionError(f"Κανένας από τους {len(candidates)} υποψήφιους Managers "
                              "δεν επαληθεύτηκε με TLS")
    prints = {_fingerprint(connection) for connection, _, _ in verified}
    chosen = verified[0]
    if len(prints) > 1:
        known = [item for item in verified
                 if pin is not None and _fingerprint(item[0]) == pin["fingerprint"]]
        if not known:
            for connection, _, _ in verified:
                _close_quietly(connection)
            raise ValueError("Δύο Managers με διαφορετικό πιστοποιητικό απαντούν έγκυρα ("
                             + ", ".join(where for _, _, where in verified)
                             + "): ορίστε χειροκίνητα --host")
        chosen = known[0]
    for item in verified:
        if item is not chosen:
            _close_quietly(item[0])
    if len(verified) > 1 or len(candidates) > 1:
        log(f"Επαληθεύτηκε ο Manager {chosen[2]} ({len(candidates)} υποψήφιοι)")
    return chosen[0], chosen[1]


#: 5C1: πώς τελείωσε μια προσπάθεια σύνδεσης (για το tray/παράθυρο, client.status).
REJECTED = "rejected"     # ο Manager απέρριψε το πιστοποιητικό του Client (TLS alert)


class NotAdmitted(ConnectionError):
    """Η σύνδεση κόπηκε πριν από το WELCOME, αμέσως μετά το TLS.

    Με TLS 1.3 ο Client τελειώνει τη χειραψία πριν ο Manager ελέγξει το πιστοποιητικό του·
    η απόρριψη φαίνεται στην πρώτη ανάγνωση ως TLS alert **ή** (ανάλογα με τον χρονισμό,
    και στα Windows) ως «connection reset»/κλείσιμο.
    """


def classify_failure(exc: BaseException) -> tuple[str, str]:
    """(είδος, λεπτομέρεια) αποτυχίας σύνδεσης.

    Μη εγκεκριμένο/ανακληθέν πιστοποιητικό Client: ο Manager στέλνει TLS alert
    (π.χ. ``TLSV1_ALERT_UNKNOWN_CA``)· λάθος πιστοποιητικό Manager: έλεγχος στον Client
    (``SSLCertVerificationError``).
    """
    from client import status as st
    if isinstance(exc, ManagerFull):
        return st.FULL, ""
    if isinstance(exc, ProtocolMismatch):
        return st.INCOMPATIBLE, str(exc)
    if isinstance(exc, ssl.SSLCertVerificationError):
        return st.ERROR, "το πιστοποιητικό του Manager δεν είναι το αναμενόμενο"
    if isinstance(exc, NotAdmitted) or (
            isinstance(exc, ssl.SSLError) and "ALERT" in str(exc).upper()):
        return REJECTED, ""
    if isinstance(exc, ValueError) and "Δύο Managers" in str(exc):
        return st.ERROR, str(exc)
    return st.CONNECTING, str(exc)


def run_with_reconnect(
    host: str | None,
    port: int,
    session: Callable[[socket.socket], None],
    *,
    receive_timeout: float = RECEIVE_TIMEOUT_SECONDS,
    reconnect_delay: float = RECONNECT_DELAY_SECONDS,
    reconnect_jitter: float = RECONNECT_JITTER_SECONDS,
    full_delay: float = FULL_RETRY_SECONDS,
    discovery_port: int = DISCOVERY_PORT,
    tls_context: ssl.SSLContext | None = None,
    tls_server_name: str = DEFAULT_SERVER_NAME,
    discovery: Callable[..., object] = discover_candidates,
    pin_file: Path | None = None,
    max_sessions: int | None = None,
    log: Callable[[str], None] = lambda line: print(line, flush=True),
    sleep: Callable[[float], None] = time.sleep,
    on_state: Callable[[str, str], None] | None = None,
    stop: threading.Event | None = None,
) -> None:
    """Τρέχει session(connection) και ξανασυνδέεται όταν η σύνδεση χαθεί.

    Η παλιά σύνδεση κλείνει πάντα πριν από τη νέα προσπάθεια.
    max_sessions υπάρχει μόνο για τα tests (None = για πάντα).
    on_state(είδος, λεπτομέρεια): 5C1, «searching»/«connecting» και το είδος της αποτυχίας
    (``classify_failure``). Μετά από απόρριψη/γεμάτο/ασύμβατο δεν ξαναστέλνεται
    «connecting» σε κάθε προσπάθεια, ώστε το tray να μην αναβοσβήνει.
    stop: 5C1, τερματισμός από το GUI (κλείνει και η τρέχουσα σύνδεση από τον καλούντα).
    """
    def report(kind: str, detail: str = "") -> None:
        if on_state is not None:
            try:
                on_state(kind, detail)
            except Exception:  # noqa: BLE001 — το GUI δεν σταματά τη σύνδεση
                pass

    from client import status as st
    sticky = False
    # Έλεγχος πριν από τον βρόχο: αλλιώς το σφάλμα θα γινόταν ατέρμονη επανασύνδεση.
    _require_tls(tls_context)
    attempts = 0
    pin = load_pin(pin_file)
    preferred_id: str | None = pin["director_id"] if pin else None
    while max_sessions is None or attempts < max_sessions:
        if stop is not None and stop.is_set():
            return
        attempts += 1
        try:
            if not sticky:
                report(st.SEARCHING if host is None else st.CONNECTING)
            if host is None:
                result = discovery(udp_port=discovery_port, preferred_id=preferred_id)
                candidates = [result] if isinstance(result, tuple) else list(result or [])
                if not candidates:
                    log("Δεν εντοπίστηκε Director στο LAN· επανάληψη ανακάλυψης.")
                    raise ConnectionError("Ο Director δεν είναι διαθέσιμος")
                for item in candidates:
                    log(f"Discovery: Director {item[0]}:{item[1]} ({item[2]})")
            else:
                candidates = [(host, port, None)]
            if host is None and not sticky:
                report(st.CONNECTING)
            connection, found_id = _verified_connection(
                candidates, port=port, pin=pin, log=log, receive_timeout=receive_timeout,
                tls_context=tls_context, tls_server_name=tls_server_name)
            with connection:
                # «Κλείδωμα» στον Director μόνο αφού το TLS τον επαλήθευσε· ένα
                # ψεύτικο beacon που απάντησε πρώτο δεν κρατά τον Client μακριά
                # από τον πραγματικό Director. 4.3C1: και μόνιμα, στον δίσκο.
                if host is None and found_id is not None:
                    preferred_id = found_id
                    fingerprint = _fingerprint(connection)
                    try:
                        if save_pin(pin_file, found_id, fingerprint):
                            pin = load_pin(pin_file)
                    except OSError as exc:
                        log(f"[Client] Δεν αποθηκεύτηκε ο Manager: {exc}")
                    if pin is None or pin.get("fingerprint") != fingerprint:
                        pin = {"director_id": found_id, "fingerprint": fingerprint}
                session(connection)
        except ManagerFull as exc:
            delay = _retry_delay(full_delay, reconnect_jitter)
            log(f"Ο Manager έχει συμπληρώσει το όριο υπολογιστών· "
                f"νέα προσπάθεια σε {delay:.0f}s.")
            kind, detail = classify_failure(exc)
        except socket.timeout as exc:
            delay = _retry_delay(reconnect_delay, reconnect_jitter)
            log(f"Δεν ήρθε έγκυρο μήνυμα από τον Director για {receive_timeout:g}s· "
                f"η σύνδεση θεωρείται χαμένη. Νέα προσπάθεια σε {delay:.1f}s.")
            kind, detail = st.CONNECTING, str(exc)
        except (OSError, EOFError, ValueError) as exc:
            delay = _retry_delay(reconnect_delay, reconnect_jitter)
            log(f"Η σύνδεση διακόπηκε: {exc}. Νέα προσπάθεια σε {delay:.1f}s.")
            kind, detail = classify_failure(exc)
        else:
            delay = _retry_delay(reconnect_delay, reconnect_jitter)
            kind, detail = st.CONNECTING, ""
        sticky = kind not in (st.CONNECTING, st.SEARCHING)
        report(kind, detail)
        if stop is not None and stop.is_set():
            return
        if max_sessions is None or attempts < max_sessions:
            if stop is not None and sleep is time.sleep:
                stop.wait(delay)
            else:
                sleep(delay)


def build_parser() -> argparse.ArgumentParser:
    # Φάση 5A: ο φάκελος δεδομένων αλλάζει με SCH_INTERCOM_DATA (εγκαταστάτης, δοκιμές).
    data = data_dir()
    parser = argparse.ArgumentParser(description="School Intercom — Classroom Client (πρωτότυπο)")
    parser.add_argument("--host", default=None,
                        help="Προαιρετική IP Director (παρακάμπτει το UDP discovery)")
    parser.add_argument("--discovery-port", type=int, default=DISCOVERY_PORT,
                        help="UDP θύρα Discovery (default 50556)")
    parser.add_argument("--room", default=None, help="Αίθουσα, π.χ. C2627-Γ1 (αποθηκεύεται)")
    parser.add_argument("--identity-file", type=Path,
                        default=data / "client.json",
                        help="Τοπικό αρχείο σταθερής ταυτότητας")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--tls-ca", type=Path, default=data / MANAGER_CERT,
                        help="Έμπιστο πιστοποιητικό/CA Director (PEM)· προεπιλογή "
                             "manager-ca.pem στον φάκελο δεδομένων")
    parser.add_argument("--client-credentials-dir", type=Path,
                        default=data / "credentials",
                        help="Φάκελος κρυπτογραφημένου κλειδιού Client")
    parser.add_argument("--new-client-key", action="store_true",
                        help="Δημιουργία νέου κλειδιού για τον ίδιο UUID (μετά από ανάκληση)· "
                             "απαιτεί νέα έγκριση από τον Director")
    parser.add_argument("--new-identity", action="store_true",
                        help="Νέα ταυτότητα (νέο UUID και κλειδί) και νέα αίτηση εγγραφής, "
                             "π.χ. μετά από αντιγραφή δίσκου· τα παλιά αρχεία μετακινούνται "
                             "σε φάκελο previous-identity-<ημερομηνία>")
    parser.add_argument("--enroll", action="store_true",
                        help="Μία αρχική εγγραφή μέσω TLS/SAS, χωρίς εκκίνηση ήχου")
    parser.add_argument("--enrollment-port", type=int, default=ENROLL_PORT,
                        help="Ξεχωριστή θύρα εγγραφής Director")
    parser.add_argument("--computer-name", default=None,
                        help="Προτεινόμενο όνομα υπολογιστή στην εγγραφή")
    parser.add_argument("--request-change", action="store_true",
                        help="Αίτημα αλλαγής αίθουσας/ονόματος (με --room και "
                             "--computer-name)· το στέλνει ο Client που τρέχει, χωρίς κωδικό "
                             "αν είναι ήδη εγκεκριμένος")
    parser.add_argument("--client-hints-file", type=Path,
                        default=data / "client-hints.json",
                        help="Τελευταία στοιχεία που όρισε ο Manager")
    parser.add_argument("--registry-seen-file", type=Path,
                        default=data / "registry-seen.json",
                        help="Μεγαλύτερη έκδοση μητρώου που έχει δει ο Client ανά Manager")
    parser.add_argument("--tls-server-name", default=DEFAULT_SERVER_NAME,
                        help="DNS όνομα στο SAN του πιστοποιητικού Director")
    parser.add_argument("--device", type=int, default=None, help="Αριθμός συσκευής εξόδου PortAudio")
    parser.add_argument(
        "--buffer-ms", type=int, default=DEFAULT_BUFFER_MS,
        help=f"Στόχος καθυστέρησης ουράς σε ms (default {DEFAULT_BUFFER_MS})",
    )
    parser.add_argument(
        "--max-buffer-ms", type=int, default=DEFAULT_MAX_BUFFER_MS,
        help=("Μέγιστο κράτημα ριπών μετά από κόλλημα δικτύου· η καθυστέρηση επιστρέφει "
              f"στο --buffer-ms στις παύσεις (default {DEFAULT_MAX_BUFFER_MS}, 0 = όχι)"),
    )
    parser.add_argument(
        "--latency", type=parse_latency, default=DEFAULT_LATENCY,
        help=f"Καθυστέρηση κάρτας ήχου: low, high ή δευτερόλεπτα. Default {DEFAULT_LATENCY}",
    )
    parser.add_argument(
        "--fade-ms", type=int, default=DEFAULT_FADE_MS,
        help=f"Fade-in/fade-out στην αρχή/τέλος ανακοίνωσης σε ms, 0 = χωρίς (default {DEFAULT_FADE_MS})",
    )
    parser.add_argument(
        "--resume-fade-ms", type=int, default=DEFAULT_RESUME_FADE_MS,
        help=f"Fade-in μετά από κενό δικτύου σε ms (default {DEFAULT_RESUME_FADE_MS})",
    )
    parser.add_argument(
        "--announcement-volume", type=int, default=None, metavar="0-100",
        help="Προαιρετική προσωρινή ένταση εξόδου Windows κατά την ανακοίνωση "
             "(αν παραλειφθεί, καμία αλλαγή στην ένταση του λειτουργικού)",
    )
    parser.add_argument(
        "--mute-others", action="store_true",
        help="Πειραματικό: μειώνει προσωρινά τις άλλες εφαρμογές στο 0%% και "
             "κάνει pause στα διαθέσιμα media (απαιτεί --announcement-volume)",
    )
    # Μόνο για αυτόματες δοκιμές: αρχείο ανάκαμψης έντασης εκτός της κανονικής εγκατάστασης.
    parser.add_argument("--volume-recovery-file", type=Path, default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--update-port", type=int, default=UPDATE_PORT,
                        help=argparse.SUPPRESS)
    # Μόνο για τις αυτόματες δοκιμές 5B2: κλειδί υπογραφής δοκιμής αντί του ενσωματωμένου.
    parser.add_argument("--update-trusted-key", action="append", default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--update-retry-seconds", type=float, default=None,
                        help=argparse.SUPPRESS)
    # Μόνο για τις αυτόματες δοκιμές 4.4: γεγονότα λήψης/αναπαραγωγής σε αρχείο JSONL.
    parser.add_argument("--test-report", type=Path, default=None, help=argparse.SUPPRESS)
    # 5C1: εικονίδιο tray και παράθυρο· χωρίς γραφικό περιβάλλον ή με --no-tray όπως πριν.
    parser.add_argument("--no-tray", action="store_true",
                        help="Χωρίς εικονίδιο tray και παράθυρο (μόνο κονσόλα)")
    # Μόνο για το live test 5C1: τι δείχνουν tray/παράθυρο, σε JSON σε κάθε αλλαγή.
    parser.add_argument("--status-dump", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--show-window", action="store_true", help=argparse.SUPPRESS)
    return parser


def apply_local_settings(args, settings: dict) -> None:
    """Φάση 5A: --host/--device από τις ρυθμίσεις της γραμμής εντολών, αν δεν δόθηκαν."""
    if args.host is None and settings.get("director_host"):
        args.host = settings["director_host"]
    if args.device is None and settings.get("device") is not None:
        args.device = settings["device"]


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        apply_local_settings(args, load_settings(args.identity_file.parent))
    except ValueError as exc:
        print(f"[Client] Αγνοούνται οι τοπικές ρυθμίσεις: {exc}", flush=True)
    if not args.tls_ca.exists():
        parser.error(f"Λείπει το πιστοποιητικό του Manager: {args.tls_ca} (δώστε --tls-ca)")
    if args.mute_others and args.announcement_volume is None:
        parser.error("--mute-others απαιτεί --announcement-volume")
    if args.announcement_volume is not None and not 0 <= args.announcement_volume <= 100:
        parser.error("--announcement-volume: επιτρέπεται τιμή 0–100")
    if args.announcement_volume is not None and args.device is not None:
        parser.error("--announcement-volume δεν συνδυάζεται ακόμη με --device")
    if args.new_identity and args.new_client_key:
        parser.error("--new-identity φτιάχνει ήδη νέο κλειδί· όχι μαζί με --new-client-key")

    # 4.3C1: κλωνοποιημένος υπολογιστής ή ρητή νέα ταυτότητα. Τα παλιά αρχεία
    # μετακινούνται (όχι διαγραφή) και ακολουθεί νέα αίτηση εγγραφής.
    pin_file = args.identity_file.with_name("manager-pin.json")
    data_folder = args.identity_file.parent
    pending_file = args.identity_file.with_name(st.PENDING_FILE)
    change_file = args.identity_file.with_name(cr.CLIENT_FILE)
    reset_label, old_room = None, previous_room(args.identity_file)
    if args.new_identity:
        reset_label = PREVIOUS
    else:
        try:
            reason = check_clone(args.identity_file, args.client_credentials_dir)
        except OSError as exc:
            print(f"[Client] Δεν ελέγχθηκε αν ο υπολογιστής είναι αντίγραφος: {exc}", flush=True)
            reason = None
        if reason is not None:
            reset_label = CLONED
            print(f"[Client] Αυτός ο υπολογιστής μοιάζει αντίγραφο άλλου ({reason}). "
                  "Η ταυτότητα του άλλου υπολογιστή δεν χρησιμοποιείται.", flush=True)

    #: Η τρέχουσα ταυτότητα· 5C2: αλλάζει χωρίς επανεκκίνηση με «Αλλαγή…» μετά από ανάκληση.
    ident: dict = {}
    #: 5C3: κλώνος· η παλιά (αντιγραμμένη) ταυτότητα συστήνει μία φορά τη νέα.
    clone_sponsor = (old_identity(args.identity_file, args.client_credentials_dir,
                                  args.client_hints_file, SPONSOR_CLONE)
                     if reset_label == CLONED else None)

    def activate(label: str | None, room_override: str | None, *, new_key: bool = False,
                 extra: tuple[Path, ...] = ()) -> None:
        if label is not None:
            folder = archive_identity([args.identity_file, args.client_credentials_dir,
                                       args.client_hints_file, *extra], label)
            print("[Client] Δημιουργείται νέα ταυτότητα· χρειάζεται νέα έγκριση στον Manager."
                  + (f" Τα παλιά αρχεία: {folder}" if folder else ""), flush=True)
        client_id, room = load_identity(args.identity_file, room_override=room_override)
        if new_key:
            certfile, keyfile, password = replace_device_identity(args.client_credentials_dir,
                                                                  client_id)
            print("Δημιουργήθηκε νέο κλειδί Client· το παλιό διαγράφηκε.", flush=True)
        else:
            certfile, keyfile, password = create_device_identity(args.client_credentials_dir,
                                                                 client_id)
        try:
            remember_machine(args.client_credentials_dir)
        except OSError as exc:
            print(f"[Client] Δεν σημειώθηκε το αποτύπωμα του υπολογιστή: {exc}", flush=True)
        ident.update(client_id=client_id, room=room, certfile=certfile, keyfile=keyfile,
                     password=password,
                     context=client_context(args.tls_ca, certfile=certfile, keyfile=keyfile,
                                            password=password))

    # Με --request-change η --room είναι η ζητούμενη, όχι η αίθουσα της ταυτότητας.
    room_override = None if args.request_change else args.room
    activate(reset_label, room_override or (old_room if reset_label is not None else None),
             new_key=args.new_client_key)
    client_id = ident["client_id"]

    if args.request_change:
        if not args.room or not args.computer_name:
            parser.error("--request-change χρειάζεται --room και --computer-name")
        try:
            ChangeController.write_inbox(data_folder, client_id, args.room, args.computer_name)
        except (OSError, ValueError) as exc:
            parser.exit(2, f"Το αίτημα αλλαγής δεν καταχωρήθηκε: {exc}\n")
        print(f"Αίτημα αλλαγής σε {args.room} · {args.computer_name}: το στέλνει ο Client "
              "στον Director (αν δεν τρέχει, στην επόμενη εκκίνηση).", flush=True)
        return

    # 5C1: κατάσταση για tray/παράθυρο (υπάρχει και χωρίς GUI· κοστίζει ελάχιστα).
    hints = load_hints(args.client_hints_file)
    pending = st.load_pending(pending_file, client_id)
    try:
        ui_scale = load_settings(data_folder)["ui_scale"]
    except ValueError:
        ui_scale = None
    from shared.ui.theme import default_ui_scale
    status = st.ClientStatus(
        room=(pending or {}).get("room") or hints.get("room") or ident["room"],
        name=(pending or {}).get("name") or hints.get("name"),
        client_id=client_id, volume=args.announcement_volume, mute_others=args.mute_others,
        approved_before=args.registry_seen_file.exists(), enrollment=pending,
        scale=ui_scale or default_ui_scale())
    changes = ChangeController(change_file, client_id, status=status)

    def enrollment_hosts() -> list[str]:
        if args.host:
            return [args.host]
        # 4.3C1: όπως στη σύνδεση, κάθε υποψήφιος ελέγχεται με TLS στο enroll_client.
        hosts = list(dict.fromkeys(item[0] for item in discover_candidates(
            udp_port=args.discovery_port,
            preferred_id=(load_pin(pin_file) or {}).get("director_id"))))
        if not hosts:
            raise ConnectionError("Δεν βρέθηκε Manager για εγγραφή. Δώστε --host.")
        return hosts

    def submit_enrollment(proposed_room: str | None = None,
                          computer_name: str | None = None, *,
                          sponsor: dict | None = None) -> None:
        """``sponsor`` (5C3): η παλιά ταυτότητα (``old_identity``) συστήνει τη νέα, ώστε ο
        Director να την εγκρίνει χωρίς κωδικό. Αν δεν γίνει δεκτή, ισχύει ο κωδικός."""
        hints = load_hints(args.client_hints_file)
        proposed_room = proposed_room or args.room or hints.get("room") or ident["room"]
        computer_name = (computer_name or args.computer_name or hints.get("name")
                         or socket.gethostname()[:64])
        request = make_request(ident["client_id"], proposed_room, computer_name,
                               ident["certfile"].read_bytes(), ident["keyfile"].read_bytes(),
                               ident["password"])
        attempts = [request]
        if sponsor is not None:
            try:
                attempts.insert(0, make_sponsor(request, sponsor["kind"], sponsor["origin"],
                                                sponsor.get("token", ""), sponsor["key"],
                                                sponsor["password"]))
            except (ValueError, TypeError) as exc:
                print(f"[Client] Χωρίς σύσταση της παλιάς ταυτότητας: {exc}", flush=True)
        errors = []
        queued = None
        # Με σύσταση πρώτα· αν ο Director δεν τη δέχεται (π.χ. παλιότερη έκδοση), χωρίς.
        for attempt in attempts:
            for enrollment_host in enrollment_hosts():
                try:
                    queued = enroll_client(enrollment_host, args.enrollment_port, args.tls_ca,
                                           args.tls_server_name, attempt)
                    break
                except (OSError, EOFError, ValueError) as exc:
                    errors.append(f"{enrollment_host}: {exc}")
            if queued is not None:
                break
        if queued is None:
            raise ConnectionError("; ".join(errors))
        save_hints(args.client_hints_file, proposed_room, computer_name)
        sponsored = None
        if queued.get("sponsored") and sponsor is not None:
            sponsored = {"kind": sponsor["kind"], "room": sponsor.get("room") or "",
                         "name": sponsor.get("name") or ""}
        try:
            st.save_pending(pending_file, code=queued["code"], room=proposed_room,
                            name=computer_name, expires_at=queued["expires_at"],
                            client_id=ident["client_id"], request_id=queued.get("request_id"),
                            sponsor=sponsored)
        except OSError as exc:
            print(f"[Client] Δεν αποθηκεύτηκε το αίτημα για το παράθυρο: {exc}", flush=True)
        waiting = st.load_pending(pending_file, ident["client_id"])
        if sponsored is not None and sponsored["kind"] == SPONSOR_CLONE:
            # «Νέος υπολογιστής»: αίθουσα και όνομα τα ορίζει ο Director στην έγκριση.
            status.update(room=None, name=None, enrollment=waiting)
        else:
            status.update(room=proposed_room, name=computer_name, enrollment=waiting)
        until = time.strftime("%d/%m/%Y %H:%M", time.localtime(queued["expires_at"]))
        if sponsored is not None:
            source = room_title(sponsored["room"], sponsored["name"]) or sponsor["origin"][:8]
            what = "Κλώνος του" if sponsored["kind"] == SPONSOR_CLONE else "Νέα ταυτότητα του"
            print(f"Αίθουσα: {proposed_room} | Υπολογιστής: {computer_name} | {what} "
                  f"{source}: έγκριση στον Director χωρίς κωδικό | Λήξη: {until}", flush=True)
        else:
            print(f"Αίθουσα: {proposed_room} | Υπολογιστής: {computer_name} | "
                  f"Κωδικός: {queued['code']} | Λήξη: {until}", flush=True)

    def amend(room_value: str, name_value: str) -> None:
        """5C2: πριν από την πρώτη έγκριση· διόρθωση του αιτήματος, ίδιος κωδικός."""
        cid = ident["client_id"]
        waiting = st.load_pending(pending_file, cid)
        if waiting is None or not waiting.get("request_id"):
            raise ConnectionError("δεν βρέθηκε το αίτημα εγγραφής αυτού του υπολογιστή")
        seq = waiting["seq"] + 1
        message = make_amend(waiting["request_id"], cid, room_value, name_value, seq,
                             ident["keyfile"].read_bytes(), ident["password"])
        errors = []
        for enrollment_host in enrollment_hosts():
            try:
                amend_enrollment(enrollment_host, args.enrollment_port, args.tls_ca,
                                 args.tls_server_name, message)
                break
            except (OSError, EOFError, ValueError) as exc:
                errors.append(f"{enrollment_host}: {exc}")
        else:
            raise ConnectionError("; ".join(errors))
        st.save_pending(pending_file, code=waiting["code"], room=room_value, name=name_value,
                        expires_at=waiting["expires_at"], client_id=cid,
                        request_id=waiting["request_id"], seq=seq)
        save_hints(args.client_hints_file, room_value, name_value)
        status.update(room=room_value, name=name_value, change=None,
                      enrollment=st.load_pending(pending_file, cid))
        print(f"[Client] Διορθώθηκε το αίτημα εγγραφής: {room_value} · {name_value} "
              f"(ίδιος κωδικός {waiting['code']})", flush=True)

    if args.enroll:
        try:
            submit_enrollment()
        except (OSError, EOFError, ValueError) as exc:
            parser.exit(2, f"Η αίτηση εγγραφής δεν καταχωρήθηκε: {exc}\n")
        print("Γράψτε τα και κλείστε. Μετά την έγκριση στον Manager ο Client "
              "συνδέεται μόνος του στην επόμενη εκκίνηση.", flush=True)
        return
    if reset_label is not None:
        try:
            submit_enrollment(sponsor=clone_sponsor)
            print("[Client] Νέα αίτηση εγγραφής· μετά την έγκριση με τον κωδικό στον Manager "
                  "ο Client συνδέεται μόνος του.", flush=True)
        except (OSError, EOFError, ValueError) as exc:
            print(f"[Client] Η νέα αίτηση εγγραφής δεν καταχωρήθηκε: {exc}. "
                  "Δοκιμάστε αργότερα με --enroll.", flush=True)
    print(f"Πιστοποιητικό Client: {ident['certfile']}", flush=True)
    print("Απαιτείται μία αρχική έγκριση του PEM από τον Director.", flush=True)
    print(f"Client ID: {client_id} | Αίθουσα: {ident['room']}", flush=True)

    # 5B2: ενημερώσεις μόνο σε εγκατεστημένο Client (current.json του εγκαταστάτη).
    history = st.History(args.identity_file.with_name(st.HISTORY_FILE)).load()

    def on_announcement(active: bool) -> None:
        if active:
            history.begin(st.KIND_LIVE)
            status.update(live_since=time.time())
        else:
            history.end()
            status.update(live_since=None)

    announcement = AnnouncementState(on_change=on_announcement)
    layout = Layout()
    try:
        installed = layout.current_version()
    except ValueError as exc:
        installed = None
        print(f"[Ενημέρωση] Αγνοείται το {layout.current_file}: {exc}", flush=True)
    worker: dict = {"updater": None}

    def start_updater() -> None:
        if installed is None:
            return
        worker["updater"] = Updater(layout, ident["client_id"], ident["context"],
                                    server_name=args.tls_server_name, port=args.update_port,
                                    trusted_keys=args.update_trusted_key,
                                    announcement=announcement,
                                    on_state=status.set_update,
                                    **({} if args.update_retry_seconds is None
                                       else {"error_retry": args.update_retry_seconds})).start()

    def trigger_update(manager_host: str | None) -> None:
        if worker["updater"] is not None:
            worker["updater"].trigger(manager_host)

    if installed is not None:
        start_updater()
        print(f"[Ενημέρωση] Εγκατεστημένη έκδοση {installed}", flush=True)
    else:
        status.set_update("none")
    running = os.environ.get(VERSION_ENV)
    status.update(version=running or installed or APP_VERSION)

    trial = {"version": os.environ.get(TRIAL_ENV), "sent": False}

    def confirm_trial(manager_host: str | None) -> None:
        """5B2b: νέα έκδοση σε δοκιμή· αποδεκτό WELCOME = «ΟΚ» στον supervisor (commit).

        Μετά το commit νέος έλεγχος, ώστε ο Manager να μάθει αμέσως «installed»: ο
        έλεγχος της σύνδεσης έτρεξε παράλληλα, πριν γραφτεί το αποτέλεσμα.
        """
        if not trial["version"] or trial["sent"]:
            return
        trial["sent"] = True

        def send():
            for attempt in range(5):
                try:
                    answer = send_command(layout, "trial-ok", version=trial["version"])
                    print(f"[Ενημέρωση] Η έκδοση {trial['version']} συνδέθηκε· "
                          + ("επιβεβαιώθηκε." if answer.get("ok")
                             else f"ο supervisor: {answer.get('error')}"), flush=True)
                    if answer.get("ok") and worker["updater"] is not None:
                        result = layout.home / RESULT_FILE
                        deadline = time.monotonic() + 15
                        while not result.exists() and time.monotonic() < deadline:
                            time.sleep(0.25)
                        trigger_update(manager_host)
                    return
                except OSError as exc:
                    error = exc
                    time.sleep(1 + attempt)
            print(f"[Ενημέρωση] Δεν επιβεβαιώθηκε η δοκιμή: {error}", flush=True)
        threading.Thread(target=send, daemon=True, name="trial-ok").start()

    current: dict = {"connection": None}

    def on_settings(info: dict) -> None:
        name = info["name"] or socket.gethostname()[:64]
        save_hints(args.client_hints_file, info["room"], name)
        changes.set_current(info["room"], name)
        status.update(room=info["room"], name=name)

    def on_output(output) -> None:
        import sounddevice as sd
        status.update(device=str(sd.query_devices(output.device)["name"]))

    def on_state(kind: str, detail: str) -> None:
        if kind != REJECTED:
            status.set_state(kind, detail)
            return
        # Ο Manager απέρριψε το πιστοποιητικό: αναμονή έγκρισης, ανάκληση ή καμία αίτηση.
        waiting = st.load_pending(pending_file, ident["client_id"])
        status.update(enrollment=waiting)
        if waiting is not None:
            status.set_state(st.PENDING)
        elif status.snapshot()["approved_before"]:
            status.set_state(st.REVOKED)
        else:
            status.set_state(st.UNAPPROVED)

    def session(connection: socket.socket) -> None:
        # 4.2D3: αναφορά «έχω δει vN» στον Manager που πέρασε τον έλεγχο TLS.
        key = manager_key(connection.getpeercert(binary_form=True) or b"")
        seen = load_seen(args.registry_seen_file, key)
        manager_host = peer_host(connection)
        try:
            send_hello(connection, ident["client_id"], ident["room"], seen=seen)
            welcome = receive_welcome(connection)
        except ProtocolMismatch:
            # Ασύμβατο πρωτόκολλο ήχου: η ενημέρωση είναι ο τρόπος να ξανασυνδεθεί.
            trigger_update(manager_host)
            raise
        except ManagerFull:
            raise
        except (ConnectionError, EOFError, ssl.SSLError) as exc:
            raise NotAdmitted(str(exc) or type(exc).__name__) from exc
        status.update(manager=manager_host, fingerprint=key, enrollment=None)
        status.set_state(st.CONNECTED)
        st.clear_pending(pending_file)
        confirm_trial(manager_host)
        trigger_update(manager_host)   # σε κάθε σύνδεση και επανασύνδεση
        if welcome is not None:
            if seen is not None and (seen["rv"] > welcome["rv"] or (
                    seen["rv"] == welcome["rv"] and seen["hash"] != welcome["hash"])):
                kind = "διαφορετικό μητρώο " if seen["rv"] == welcome["rv"] else ""
                print(f"[Client] Ο Manager έχει μητρώο v{welcome['rv']}· "
                      f"έχω δει {kind}v{seen['rv']} (αναφέρθηκε στον Manager).", flush=True)
            try:
                if record_seen(args.registry_seen_file, key, welcome):
                    print(f"[Client] Μητρώο Manager v{welcome['rv']}", flush=True)
            except OSError as exc:
                print(f"[Client] Δεν αποθηκεύτηκε η έκδοση μητρώου: {exc}", flush=True)
        if test_report is not None:
            test_report.event("connected")
        current["connection"] = connection
        heartbeat_stop = threading.Event()
        # 5C2: τα αιτήματα αλλαγής φεύγουν από το ίδιο νήμα με τα heartbeat.
        outbox: queue.Queue = queue.Queue()
        changes.connected(outbox)
        sender = threading.Thread(target=heartbeat_sender, args=(connection, heartbeat_stop),
                                  kwargs={"outbox": outbox},
                                  name="client-heartbeat", daemon=True)
        sender.start()
        try:
            play_stream(connection, device=args.device,
                        buffer_ms=args.buffer_ms, max_buffer_ms=args.max_buffer_ms,
                        latency=args.latency,
                        fade_ms=args.fade_ms, resume_fade_ms=args.resume_fade_ms,
                        announcement_volume=args.announcement_volume,
                        mute_others=args.mute_others,
                        recovery_path=args.volume_recovery_file,
                        settings_callback=on_settings,
                        on_output=on_output,
                        test_report=test_report, announcement=announcement,
                        update_check=lambda: trigger_update(manager_host),
                        change_status=changes.on_status,
                        new_identity=lambda data: on_new_identity(data))
        finally:
            changes.disconnected()
            current["connection"] = None
            announcement.set(False)
            if test_report is not None:
                test_report.event("lost")
            heartbeat_stop.set()
            sender.join(timeout=1)

    test_report = None
    if args.test_report is not None:
        from shared.test_report import TestReport
        test_report = TestReport(args.test_report)
    stop = threading.Event()
    epoch = threading.Event()        # τέλος του τρέχοντος βρόχου σύνδεσης (stop ή νέα ταυτότητα)
    reenroll: dict = {"target": None}

    def close_connection() -> None:
        connection = current["connection"]
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def new_enrollment(room_value: str, name_value: str, token: str | None = None) -> None:
        """5C2: μετά από ανάκληση/χωρίς αίτημα· νέα ταυτότητα και νέος κωδικός.

        5C3: με ``token`` είναι η «Νέα ταυτότητα» του Director· η παλιά ταυτότητα τη
        συστήνει και ο Director την εγκρίνει χωρίς κωδικό.
        """
        sponsor = None
        if token is not None:
            snap = status.snapshot()
            try:
                sponsor = {"kind": SPONSOR_RESET, "origin": ident["client_id"],
                           "token": token, "key": ident["keyfile"].read_bytes(),
                           "password": ident["password"], "room": snap.get("room") or "",
                           "name": snap.get("name") or ""}
            except OSError as exc:
                print(f"[Client] Δεν διαβάστηκε η παλιά ταυτότητα: {exc}", flush=True)
        if worker["updater"] is not None:
            worker["updater"].stop()
            worker["updater"] = None
        activate(PREVIOUS, room_value, extra=(pending_file, change_file))
        changes.reset(ident["client_id"])
        status.update(client_id=ident["client_id"], room=room_value, name=name_value,
                      enrollment=None, approved_before=False)
        try:
            submit_enrollment(room_value, name_value, sponsor=sponsor)
            status.set_state(st.PENDING)
        except (OSError, EOFError, ValueError) as exc:
            print(f"[Client] Η νέα αίτηση εγγραφής δεν καταχωρήθηκε: {exc}", flush=True)
            changes.fail(room_value, name_value, str(exc))
            status.set_state(st.UNAPPROVED)
        start_updater()

    def network() -> None:
        while not stop.is_set():
            epoch.clear()
            run_with_reconnect(args.host, args.port, session,
                               discovery_port=args.discovery_port, pin_file=pin_file,
                               tls_context=ident["context"],
                               tls_server_name=args.tls_server_name,
                               on_state=on_state, stop=epoch)
            target, reenroll["target"] = reenroll["target"], None
            if stop.is_set():
                return
            if target is not None:
                new_enrollment(*target)

    def on_new_identity(data: dict) -> None:
        """5C3: ο Director ζήτησε νέα ταυτότητα σε αυτόν τον υπολογιστή (από το νήμα λήψης)."""
        print(f"[Client] Ο Director ζήτησε νέα ταυτότητα: {data['room']} · {data['name']}",
              flush=True)
        reenroll["target"] = (data["room"], data["name"], data["token"])
        epoch.set()
        close_connection()

    def handle_change(room_value: str, name_value: str) -> str:
        """«Αλλαγή…» του παραθύρου και ``--request-change``: ανάλογα με την κατάσταση."""
        room_value, name_value = cr.check_room(room_value), cr.check_name(name_value)
        mode = st.change_mode(status.snapshot())
        if mode == st.MODE_CHANGE:
            changes.request(room_value, name_value)
        elif mode == st.MODE_AMEND:
            def work() -> None:
                try:
                    amend(room_value, name_value)
                except (OSError, EOFError, ValueError) as exc:
                    print(f"[Client] Δεν διορθώθηκε το αίτημα εγγραφής: {exc}", flush=True)
                    changes.fail(room_value, name_value, str(exc))
            threading.Thread(target=work, name="client-amend", daemon=True).start()
        else:
            reenroll["target"] = (room_value, name_value)
            epoch.set()
            close_connection()
        return mode

    def set_scale(scale: int) -> None:
        try:
            save_settings(data_folder, ui_scale=scale)
        except (OSError, ValueError) as exc:
            print(f"[Client] Δεν αποθηκεύτηκε η κλίμακα: {exc}", flush=True)
        status.update(scale=scale)

    def stop_network() -> None:
        """Από το GUI: τέλος βρόχου και κλείσιμο της τρέχουσας σύνδεσης (επαναφορά ήχου)."""
        stop.set()
        epoch.set()
        close_connection()

    threading.Thread(target=watch_inbox,
                     args=(data_folder, lambda: ident["client_id"], handle_change, stop),
                     name="client-change-inbox", daemon=True).start()
    try:
        if not args.no_tray and st.tray_supported():
            try:
                from client.qt_client import run_tray
            except ImportError as exc:
                print(f"[Client] Χωρίς tray (λείπει το PySide6: {exc})", flush=True)
            else:
                run_tray(status, history, network, stop=stop_network, dump_file=args.status_dump,
                         show_window=args.show_window, on_change=handle_change,
                         on_scale=set_scale)
                print("Ο Client τερματίστηκε.", flush=True)
                return
        network()
    except KeyboardInterrupt:
        print("\nΟ Client τερματίστηκε.", flush=True)
    finally:
        stop.set()
        if test_report is not None:
            test_report.close()


if __name__ == "__main__":
    main()
