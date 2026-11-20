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

RECONNECT_DELAY_SECONDS = 3
# Έγκυρο PCM, KEEPALIVE ή STREAM_STOP ανανεώνει το όριο. Απώλεια όλων
# των μηνυμάτων για 3 s ενεργοποιεί τον υπάρχοντα μηχανισμό επανασύνδεσης.
RECEIVE_TIMEOUT_SECONDS = 3.0
CONNECT_TIMEOUT_SECONDS = 5
DEFAULT_BUFFER_MS = 250
ANNOUNCEMENT_PREFILL_MS = 120
REPORT_EVERY_SECONDS = 5
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
) -> None:
    """PCM/έλεγχος στην ίδια κατεύθυνση TCP, χωρίς reconnect σε αναμονή."""
    received = 0
    in_announcement = False
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
                jitter.begin_announcement()
                delay.reset()
                in_announcement = True
            delay.record(message.seq, message.timestamp_us, clock_us())
            jitter.push(message.pcm)
            received += 1
        elif message.kind == MESSAGE_STREAM_STOP:
            if in_announcement:
                jitter.finish_announcement()
                in_announcement = False
        # KEEPALIVE: μόνο επιβεβαίωση ζωντανής σύνδεσης· όχι PCM/underrun.
        if report and time.monotonic() >= next_report:
            mode = "ανακοίνωση" if in_announcement or jitter.mode == "ανακοίνωση" else "αναμονή"
            report(format_report(jitter, delay, received, mode=mode))
            next_report = time.monotonic() + report_every_s


def play_stream(
    connection: socket.socket,
    *,
    device: int | None = None,
    buffer_ms: int = DEFAULT_BUFFER_MS,
    latency: str | float = DEFAULT_LATENCY,
) -> None:
    import sounddevice as sd

    jitter = JitterBuffer(max(1, buffer_ms // BLOCK_MS), BLOCK_BYTES,
                          prefill_blocks=max(1, ANNOUNCEMENT_PREFILL_MS // BLOCK_MS))
    jitter.standby()
    delay = DelayTracker()

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
        pump_frames(connection, jitter, delay, report=lambda line: print(line, flush=True))


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
    args = parser.parse_args()

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
                        buffer_ms=args.buffer_ms, latency=args.latency)
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
