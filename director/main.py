"""Director: μονοφωνική μετάδοση μικροφώνου σε έναν Client μέσω TCP.

ΠΡΟΣΟΧΗ: πειραματικό πρωτόκολλο χωρίς TLS/πιστοποίηση.
Χρήση μόνο σε ελεγχόμενο δίκτυο δοκιμών.
"""

import argparse
import queue
import socket
import time

from shared.audio import BLOCK_BYTES, BLOCK_SAMPLES, CHANNELS, DEFAULT_PORT, DTYPE, SAMPLE_RATE, send_frame


def stream_microphone(connection: socket.socket, *, device: int | None = None) -> None:
    import sounddevice as sd

    pending: queue.Queue[bytes] = queue.Queue(maxsize=12)
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
        try:
            pending.put_nowait(block)
        except queue.Full:
            dropped += 1  # δεν αφήνουμε την ουρά να αυξάνει χωρίς όριο
            try:
                pending.get_nowait()
                pending.put_nowait(block)
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
        callback=on_audio,
    ):
        print("Μικρόφωνο ενεργό — μιλήστε. Ctrl+C για τερματισμό.", flush=True)
        while True:
            block = pending.get(timeout=2)
            send_frame(connection, block)
            frames_sent += 1
            if frames_sent % 250 == 0:
                seconds = round(time.monotonic() - started, 1)
                print(f"[Director] {frames_sent} blocks / {seconds}s, χαμένα: {dropped}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="School Intercom — Director (πρωτότυπο)")
    parser.add_argument("--host", default="0.0.0.0", help="IP δέσμευσης (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--device", type=int, default=None, help="Αριθμός συσκευής εισόδου PortAudio")
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
                    print(f"Σύνδεση Client {address}", flush=True)
                    try:
                        stream_microphone(connection, device=args.device)
                    except (BrokenPipeError, ConnectionResetError, EOFError, OSError) as exc:
                        print(f"Αποσύνδεση/σφάλμα: {exc}", flush=True)
                    print("Αναμονή επόμενης σύνδεσης...", flush=True)
        except KeyboardInterrupt:
            print("\nΟ Director τερματίστηκε.", flush=True)


if __name__ == "__main__":
    main()
