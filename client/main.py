"""Client: σύνδεση στον Director και αναπαραγωγή PCM.

ΠΡΟΣΟΧΗ: πειραματικό πρωτόκολλο χωρίς TLS/πιστοποίηση.
Χρήση μόνο σε ελεγχόμενο δίκτυο δοκιμών.
"""

import argparse
import socket
import time
from collections.abc import Callable

from shared.audio import BLOCK_BYTES, BLOCK_MS, BLOCK_SAMPLES, CHANNELS, DEFAULT_PORT, DTYPE, SAMPLE_RATE, receive_frame
from shared.jitter import DelayTracker, JitterBuffer

RECONNECT_DELAY_SECONDS = 3
# Ο Director στέλνει 50 πακέτα/s. Αν δεν έρθει τίποτα για 3 s, η σύνδεση θεωρείται
# νεκρή (π.χ. ο Director έχασε ρεύμα/Wi-Fi χωρίς να κλείσει το TCP) και ο Client
# ξανασυνδέεται. Τα σύντομα κολλήματα Wi-Fi που μετρήθηκαν (0,4-0,6 s) δεν το αγγίζουν.
RECEIVE_TIMEOUT_SECONDS = 3.0
CONNECT_TIMEOUT_SECONDS = 5
DEFAULT_BUFFER_MS = 250
REPORT_EVERY_SECONDS = 5
# 40 ms: δοκιμάστηκε από τον χρήστη σε Windows χωρίς κροτάλισμα (το "low" έδινε 100 ms).
DEFAULT_LATENCY = 0.04


def now_us() -> int:
    return time.monotonic_ns() // 1000


def format_report(jitter: JitterBuffer, delay: DelayTracker, received: int) -> str:
    levels = jitter.take_level_stats()
    net = delay.take_stats()
    queue_text = "—"
    if levels:
        low, avg, high = levels
        queue_text = f"{low * BLOCK_MS}/{avg * BLOCK_MS:.0f}/{high * BLOCK_MS} ms"
    net_text = "—" if net is None else f"{net[0]:.0f}/{net[1]:.0f} ms"
    return (
        f"[Client] blocks {received} | ουρά min/μέση/max {queue_text} | "
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
    """Διαβάζει συνεχώς πακέτα από το δίκτυο και τα βάζει στην ουρά.

    Τρέχει μέχρι να κλείσει η σύνδεση (EOFError) ή να εμφανιστεί σφάλμα.
    """
    received = 0
    next_report = time.monotonic() + report_every_s
    while True:
        frame = receive_frame(connection)
        delay.record(frame.seq, frame.timestamp_us, clock_us())
        jitter.push(frame.pcm)
        received += 1
        if report and time.monotonic() >= next_report:
            report(format_report(jitter, delay, received))
            next_report += report_every_s


def play_stream(
    connection: socket.socket,
    *,
    device: int | None = None,
    buffer_ms: int = DEFAULT_BUFFER_MS,
    latency: str | float = DEFAULT_LATENCY,
) -> None:
    import sounddevice as sd

    jitter = JitterBuffer(max(1, buffer_ms // BLOCK_MS), BLOCK_BYTES)
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
    host: str,
    port: int,
    session: Callable[[socket.socket], None],
    *,
    receive_timeout: float = RECEIVE_TIMEOUT_SECONDS,
    reconnect_delay: float = RECONNECT_DELAY_SECONDS,
    max_sessions: int | None = None,
    log: Callable[[str], None] = lambda line: print(line, flush=True),
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Τρέχει session(connection) και ξανασυνδέεται όταν η σύνδεση χαθεί.

    Η παλιά σύνδεση κλείνει πάντα πριν από τη νέα προσπάθεια.
    max_sessions υπάρχει μόνο για τα tests (None = για πάντα).
    """
    attempts = 0
    while max_sessions is None or attempts < max_sessions:
        attempts += 1
        try:
            log(f"Σύνδεση στον Director {host}:{port}...")
            with open_connection(host, port, receive_timeout=receive_timeout) as connection:
                session(connection)
        except socket.timeout:
            log(f"Δεν ήρθε ήχος από τον Director για {receive_timeout:g}s· "
                f"η σύνδεση θεωρείται χαμένη. Νέα προσπάθεια σε {reconnect_delay:g}s.")
        except (OSError, EOFError, ValueError) as exc:
            log(f"Η σύνδεση διακόπηκε: {exc}. Νέα προσπάθεια σε {reconnect_delay:g}s.")
        if max_sessions is None or attempts < max_sessions:
            sleep(reconnect_delay)


def main() -> None:
    parser = argparse.ArgumentParser(description="School Intercom — Classroom Client (πρωτότυπο)")
    parser.add_argument("--host", required=True, help="IP υπολογιστή Director")
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

    try:
        run_with_reconnect(
            args.host, args.port,
            lambda connection: play_stream(connection, device=args.device,
                                           buffer_ms=args.buffer_ms, latency=args.latency),
        )
    except KeyboardInterrupt:
        print("\nΟ Client τερματίστηκε.", flush=True)


if __name__ == "__main__":
    main()
