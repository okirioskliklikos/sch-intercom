"""Director: μία λήψη μικροφώνου, μετάδοση σε πολλούς TCP Clients.

ΠΡΟΣΟΧΗ: πειραματικό πρωτόκολλο χωρίς TLS/πιστοποίηση.
Χρήση μόνο σε ελεγχόμενο δίκτυο δοκιμών.
"""

import argparse
import queue
import socket
import threading
import time

from director.broadcast import AudioBroadcaster, MAX_CLIENTS
from shared.audio import BLOCK_BYTES, BLOCK_SAMPLES, CHANNELS, DEFAULT_PORT, DTYPE, SAMPLE_RATE

# Κοινή ουρά λήψης του μικροφώνου (5 x 20 ms = 100 ms).
MAX_PENDING_BLOCKS = 5


def stream_microphone(broadcaster: AudioBroadcaster, *, device: int | None = None,
                      latency: str | float = "low", stop: threading.Event | None = None) -> None:
    import sounddevice as sd

    pending: queue.Queue[tuple[int, bytes]] = queue.Queue(maxsize=MAX_PENDING_BLOCKS)
    dropped = 0

    def on_audio(indata, frames, timestamp, status):
        nonlocal dropped
        if status:
            print(f"[ήχος] {status}", flush=True)
        if frames != BLOCK_SAMPLES:
            return
        block = bytes(indata)
        if len(block) != BLOCK_BYTES:
            return
        item = (time.monotonic_ns() // 1000, block)
        try:
            pending.put_nowait(item)
        except queue.Full:
            dropped += 1
            try:
                pending.get_nowait()
                pending.put_nowait(item)
            except queue.Empty:
                pass

    frames_sent = 0
    started = time.monotonic()
    with sd.RawInputStream(
        samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE,
        blocksize=BLOCK_SAMPLES, device=device, latency=latency, callback=on_audio,
    ) as microphone:
        print(
            f"Μικρόφωνο ενεργό — μιλήστε. Ctrl+C για τερματισμό. "
            f"Καθυστέρηση κάρτας ήχου {microphone.latency * 1000:.0f} ms",
            flush=True,
        )
        while stop is None or not stop.is_set():
            try:
                captured_us, block = pending.get(timeout=0.2)
            except queue.Empty:
                continue
            broadcaster.publish(block, captured_us)
            frames_sent += 1
            if frames_sent % 250 == 0:
                seconds = round(time.monotonic() - started, 1)
                print(
                    f"[Director] {frames_sent} blocks / {seconds}s, "
                    f"χαμένα μικροφώνου: {dropped}, "
                    f"χαμένα ουρών Clients: {broadcaster.dropped}, "
                    f"Clients: {broadcaster.client_count}", flush=True,
                )


def accept_clients(server: socket.socket, broadcaster: AudioBroadcaster,
                   stop: threading.Event) -> None:
    """Αποδέχεται νέους Clients ανεξάρτητα από την καταγραφή ήχου."""
    server.settimeout(0.2)
    while not stop.is_set():
        try:
            connection, address = server.accept()
        except socket.timeout:
            continue
        except OSError:
            if not stop.is_set():
                raise
            break
        if not broadcaster.add_client(connection, address):
            print(f"[Director] Απόρριψη Client {address}: όριο {broadcaster.max_clients}", flush=True)


def parse_latency(value: str) -> str | float:
    if value in ("low", "high"):
        return value
    return float(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="School Intercom — Director (πρωτότυπο)")
    parser.add_argument("--host", default="0.0.0.0", help="IP δέσμευσης (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--device", type=int, default=None, help="Αριθμός συσκευής εισόδου PortAudio")
    parser.add_argument("--latency", type=parse_latency, default="low",
                        help="Καθυστέρηση κάρτας ήχου: low, high ή δευτερόλεπτα")
    args = parser.parse_args()

    broadcaster = AudioBroadcaster(max_clients=MAX_CLIENTS, on_event=lambda s: print(s, flush=True))
    stop = threading.Event()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.host, args.port))
        server.listen(MAX_CLIENTS)
        print(f"Director ακούει στο {args.host}:{args.port} — έως {MAX_CLIENTS} Clients", flush=True)
        receiver = threading.Thread(target=accept_clients, args=(server, broadcaster, stop),
                                    daemon=True, name="accept-clients")
        receiver.start()
        try:
            stream_microphone(broadcaster, device=args.device, latency=args.latency, stop=stop)
        except KeyboardInterrupt:
            print("\nΟ Director τερματίστηκε.", flush=True)
        finally:
            stop.set()
            receiver.join(timeout=1)
            broadcaster.close()


if __name__ == "__main__":
    main()
