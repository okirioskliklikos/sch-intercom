"""Director: μονοφωνική μετάδοση μικροφώνου σε έναν Client μέσω TCP.

ΠΡΟΣΟΧΗ: πειραματικό πρωτόκολλο χωρίς TLS/πιστοποίηση.
Χρήση μόνο σε ελεγχόμενο δίκτυο δοκιμών.
"""

import argparse
import queue
import socket
import time

from shared.audio import BLOCK_BYTES, BLOCK_SAMPLES, CHANNELS, DEFAULT_PORT, DTYPE, SAMPLE_RATE, send_frame

# Μέγιστος ήχος που περιμένει στον Director (5 x 20 ms = 100 ms). Τα παλιότερα πετιούνται.
MAX_PENDING_BLOCKS = 5
# Μικρός buffer αποστολής TCP, ώστε να μη μαζεύονται δευτερόλεπτα ήχου μέσα στο λειτουργικό.
SEND_BUFFER_BYTES = 4096


def stream_microphone(connection: socket.socket, *, device: int | None = None, latency: str | float = "low") -> None:
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
            dropped += 1  # δεν αφήνουμε την ουρά να αυξάνει χωρίς όριο
            try:
                pending.get_nowait()
                pending.put_nowait(item)
            except queue.Empty:
                pass

    frames_sent = 0
    started = time.monotonic()
    with sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype=DTYPE,
        blocksize=BLOCK_SAMPLES,
        device=device,
        latency=latency,
        callback=on_audio,
    ) as microphone:
        print(
            f"Μικρόφωνο ενεργό — μιλήστε. Ctrl+C για τερματισμό. "
            f"Καθυστέρηση κάρτας ήχου {microphone.latency * 1000:.0f} ms",
            flush=True,
        )
        while True:
            captured_us, block = pending.get(timeout=2)
            send_frame(connection, block, frames_sent, captured_us)
            frames_sent += 1
            if frames_sent % 250 == 0:
                seconds = round(time.monotonic() - started, 1)
                print(f"[Director] {frames_sent} blocks / {seconds}s, χαμένα: {dropped}", flush=True)


def parse_latency(value: str) -> str | float:
    if value in ("low", "high"):
        return value
    return float(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="School Intercom — Director (πρωτότυπο)")
    parser.add_argument("--host", default="0.0.0.0", help="IP δέσμευσης (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--device", type=int, default=None, help="Αριθμός συσκευής εισόδου PortAudio")
    parser.add_argument(
        "--latency", type=parse_latency, default="low",
        help="Καθυστέρηση κάρτας ήχου: low, high ή δευτερόλεπτα (π.χ. 0.04). Default low",
    )
    args = parser.parse_args()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.host, args.port))
        server.listen(1)
        print(f"Director ακούει στο {args.host}:{args.port} — αναμονή Client", flush=True)
        try:
            while True:
                connection, address = server.accept()
                with connection:
                    connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SEND_BUFFER_BYTES)
                    print(f"Σύνδεση Client {address}", flush=True)
                    try:
                        stream_microphone(connection, device=args.device, latency=args.latency)
                    except (BrokenPipeError, ConnectionResetError, EOFError, OSError) as exc:
                        print(f"Αποσύνδεση/σφάλμα: {exc}", flush=True)
                    print("Αναμονή επόμενης σύνδεσης...", flush=True)
        except KeyboardInterrupt:
            print("\nΟ Director τερματίστηκε.", flush=True)


if __name__ == "__main__":
    main()
