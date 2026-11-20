"""Client: σύνδεση στον Director και αναπαραγωγή PCM.

ΠΡΟΣΟΧΗ: πειραματικό πρωτόκολλο χωρίς TLS/πιστοποίηση.
Χρήση μόνο σε ελεγχόμενο δίκτυο δοκιμών.
"""

import argparse
from pathlib import Path
import socket
import threading
import time
from collections.abc import Callable

from shared.audio import (BLOCK_BYTES, BLOCK_MS, BLOCK_SAMPLES, CHANNELS, DEFAULT_PORT,
                          DTYPE, SAMPLE_RATE, Frame, MESSAGE_STREAM_STOP,
                          receive_message)
from shared.jitter import DelayTracker, JitterBuffer
from shared.identity import load_identity, receive_welcome, send_hello
from shared.discovery import discover, DISCOVERY_PORT
from shared.control import heartbeat_sender
from client.windows_volume import (AnnouncementVolume, create_volume_controller,
                                   recover_pending_volume)
from client.windows_apps import ApplicationAudio

RECONNECT_DELAY_SECONDS = 3
# Έγκυρο PCM, KEEPALIVE ή STREAM_STOP ανανεώνει το όριο. Απώλεια όλων
# των μηνυμάτων για 3 s ενεργοποιεί τον υπάρχοντα μηχανισμό επανασύνδεσης.
RECEIVE_TIMEOUT_SECONDS = 3.0
CONNECT_TIMEOUT_SECONDS = 5
DEFAULT_BUFFER_MS = 250
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
    volume: AnnouncementVolume | None = None,
) -> None:
    """PCM/έλεγχος στην ίδια κατεύθυνση TCP, χωρίς reconnect σε αναμονή."""
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
            delay.record(message.seq, message.timestamp_us, clock_us())
            jitter.push(message.pcm)
            received += 1
        elif message.kind == MESSAGE_STREAM_STOP:
            if in_announcement:
                jitter.finish_announcement()
                in_announcement = False
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
                       resume_fade_ms: int = DEFAULT_RESUME_FADE_MS) -> JitterBuffer:
    fade_samples = max(0, fade_ms) * SAMPLE_RATE // 1000
    return JitterBuffer(max(1, buffer_ms // BLOCK_MS), BLOCK_BYTES,
                        prefill_blocks=max(1, ANNOUNCEMENT_PREFILL_MS // BLOCK_MS),
                        fade_in_samples=fade_samples, fade_out_samples=fade_samples,
                        resume_fade_samples=max(0, resume_fade_ms) * SAMPLE_RATE // 1000)


def play_stream(
    connection: socket.socket,
    *,
    device: int | None = None,
    buffer_ms: int = DEFAULT_BUFFER_MS,
    latency: str | float = DEFAULT_LATENCY,
    fade_ms: int = DEFAULT_FADE_MS,
    resume_fade_ms: int = DEFAULT_RESUME_FADE_MS,
    announcement_volume: int | None = None,
    mute_others: bool = False,
    recovery_path: Path | None = None,
) -> None:
    import sounddevice as sd

    jitter = make_jitter_buffer(buffer_ms, fade_ms, resume_fade_ms)
    jitter.standby()
    delay = DelayTracker()
    journal = recovery_path or Path.home() / ".school-intercom" / "volume-recovery.json"
    # Αν προηγούμενος Client έπεσε, ανάκαμψη και χωρίς νέα ενεργοποίηση
    # της λειτουργίας από CLI. Δεν επιχειρούμε αλλαγές σε Linux.
    # Κανένα πρόβλημα έντασης/ανάκαμψης δεν σταματά τον Client: μόνο προειδοποίηση.
    if announcement_volume is None:
        recover_pending_volume(journal, device=device)
    volume = (create_volume_controller(
        announcement_volume, device=device, recovery_path=journal,
        apps=ApplicationAudio(log=lambda msg: print(msg, flush=True)) if mute_others else None)
        if announcement_volume is not None else None)

    def on_audio(outdata, frames, timestamp, status):
        if status:
            print(f"[ήχος] {status}", flush=True)
        block = jitter.pop()
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
        print(
            f"Λήψη και αναπαραγωγή ήχου... όριο ουράς {jitter.max_blocks * BLOCK_MS} ms, "
            f"καθυστέρηση κάρτας ήχου {output.latency * 1000:.0f} ms",
            flush=True,
        )
        try:
            pump_frames(connection, jitter, delay,
                        report=lambda line: print(line, flush=True), volume=volume)
        finally:
            if volume is not None:
                # Δίκτυο/τερματισμός: καλύτερα αποκατάσταση παρά παραμονή σε
                # αυξημένη ένταση. Το κανονικό STOP περιμένει πρώτα το drain.
                # Σφάλμα εδώ δεν πρέπει να κρύψει το αρχικό ούτε να σταματήσει
                # την επανασύνδεση· το αρχείο ανάκαμψης μένει για την επόμενη φορά.
                try:
                    volume.end()
                except Exception as exc:  # noqa: BLE001
                    print(f"[ήχος] Αποτυχία επαναφοράς έντασης: {exc}", flush=True)


def parse_latency(value: str) -> str | float:
    if value in ("low", "high"):
        return value
    return float(value)


def open_connection(host: str, port: int, *,
                    receive_timeout: float = RECEIVE_TIMEOUT_SECONDS) -> socket.socket:
    """Συνδέεται στον Director με όριο αναμονής για κάθε λήψη."""
    connection = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_SECONDS)
    try:
        connection.settimeout(receive_timeout)
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        connection.close()
        raise
    return connection


def run_with_reconnect(
    host: str | None,
    port: int,
    session: Callable[[socket.socket], None],
    *,
    receive_timeout: float = RECEIVE_TIMEOUT_SECONDS,
    reconnect_delay: float = RECONNECT_DELAY_SECONDS,
    discovery_port: int = DISCOVERY_PORT,
    discovery: Callable[..., tuple[str, int, str] | None] = discover,
    max_sessions: int | None = None,
    log: Callable[[str], None] = lambda line: print(line, flush=True),
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Τρέχει session(connection) και ξανασυνδέεται όταν η σύνδεση χαθεί.

    Η παλιά σύνδεση κλείνει πάντα πριν από τη νέα προσπάθεια.
    max_sessions υπάρχει μόνο για τα tests (None = για πάντα).
    """
    attempts = 0
    preferred_id: str | None = None
    while max_sessions is None or attempts < max_sessions:
        attempts += 1
        try:
            target_host, target_port = host, port
            if host is None:
                result = discovery(udp_port=discovery_port, preferred_id=preferred_id)
                if result is None:
                    log("Δεν εντοπίστηκε Director στο LAN· επανάληψη ανακάλυψης.")
                    raise ConnectionError("Ο Director δεν είναι διαθέσιμος")
                target_host, target_port, found_id = result
                if preferred_id is None:
                    preferred_id = found_id
                log(f"Discovery: Director {target_host}:{target_port} ({found_id})")
            log(f"Σύνδεση στον Director {target_host}:{target_port}...")
            with open_connection(target_host, target_port, receive_timeout=receive_timeout) as connection:
                session(connection)
        except socket.timeout:
            log(f"Δεν ήρθε έγκυρο μήνυμα από τον Director για {receive_timeout:g}s· "
                f"η σύνδεση θεωρείται χαμένη. Νέα προσπάθεια σε {reconnect_delay:g}s.")
        except (OSError, EOFError, ValueError) as exc:
            log(f"Η σύνδεση διακόπηκε: {exc}. Νέα προσπάθεια σε {reconnect_delay:g}s.")
        if max_sessions is None or attempts < max_sessions:
            sleep(reconnect_delay)


def main() -> None:
    parser = argparse.ArgumentParser(description="School Intercom — Classroom Client (πρωτότυπο)")
    parser.add_argument("--host", default=None,
                        help="Προαιρετική IP Director (παρακάμπτει το UDP discovery)")
    parser.add_argument("--discovery-port", type=int, default=DISCOVERY_PORT,
                        help="UDP θύρα Discovery (default 50556)")
    parser.add_argument("--room", default=None, help="Αίθουσα, π.χ. C2627-Γ1 (αποθηκεύεται)")
    parser.add_argument("--identity-file", type=Path,
                        default=Path.home() / ".school-intercom" / "client.json",
                        help="Τοπικό αρχείο σταθερής ταυτότητας")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--device", type=int, default=None, help="Αριθμός συσκευής εξόδου PortAudio")
    parser.add_argument(
        "--buffer-ms", type=int, default=DEFAULT_BUFFER_MS,
        help=f"Μέγιστος ήχος σε αναμονή πριν πεταχτούν παλιά blocks (default {DEFAULT_BUFFER_MS})",
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
        help="Πειραματικό: μειώνει προσωρινά τις άλλες εφαρμογές στο 0% και "
             "κάνει pause στα διαθέσιμα media (απαιτεί --announcement-volume)",
    )
    args = parser.parse_args()
    if args.mute_others and args.announcement_volume is None:
        parser.error("--mute-others απαιτεί --announcement-volume")
    if args.announcement_volume is not None and not 0 <= args.announcement_volume <= 100:
        parser.error("--announcement-volume: επιτρέπεται τιμή 0–100")
    if args.announcement_volume is not None and args.device is not None:
        parser.error("--announcement-volume δεν συνδυάζεται ακόμη με --device")

    client_id, room = load_identity(args.identity_file, room_override=args.room)
    print(f"Client ID: {client_id} | Αίθουσα: {room}", flush=True)

    def session(connection: socket.socket) -> None:
        send_hello(connection, client_id, room)
        receive_welcome(connection)
        heartbeat_stop = threading.Event()
        sender = threading.Thread(target=heartbeat_sender, args=(connection, heartbeat_stop),
                                  name="client-heartbeat", daemon=True)
        sender.start()
        try:
            play_stream(connection, device=args.device,
                        buffer_ms=args.buffer_ms, latency=args.latency,
                        fade_ms=args.fade_ms, resume_fade_ms=args.resume_fade_ms,
                        announcement_volume=args.announcement_volume,
                        mute_others=args.mute_others)
        finally:
            heartbeat_stop.set()
            sender.join(timeout=1)

    try:
        run_with_reconnect(args.host, args.port, session,
                           discovery_port=args.discovery_port)
    except KeyboardInterrupt:
        print("\nΟ Client τερματίστηκε.", flush=True)


if __name__ == "__main__":
    main()
