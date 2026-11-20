"""Client: σύνδεση στον Director και αναπαραγωγή PCM.

ΠΡΟΣΟΧΗ: πειραματικό πρωτόκολλο χωρίς TLS/πιστοποίηση.
Χρήση μόνο σε ελεγχόμενο δίκτυο δοκιμών.
"""

import argparse
import socket
import time

from shared.audio import BLOCK_SAMPLES, CHANNELS, DEFAULT_PORT, DTYPE, SAMPLE_RATE, receive_frame

RECONNECT_DELAY_SECONDS = 3


def play_stream(connection: socket.socket, *, device: int | None = None) -> None:
    import sounddevice as sd

    received = 0
    with sd.RawOutputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype=DTYPE,
        blocksize=BLOCK_SAMPLES,
        device=device,
    ) as output:
        print("Λήψη και αναπαραγωγή ήχου...", flush=True)
        while True:
            output.write(receive_frame(connection))
            received += 1
            if received % 250 == 0:
                print(f"[Client] λήφθηκαν {received} blocks", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="School Intercom — Classroom Client (πρωτότυπο)")
    parser.add_argument("--host", required=True, help="IP υπολογιστή Director")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--device", type=int, default=None, help="Αριθμός συσκευής εξόδου PortAudio")
    args = parser.parse_args()

    try:
        while True:
            try:
                print(f"Σύνδεση στον Director {args.host}:{args.port}...", flush=True)
                with socket.create_connection((args.host, args.port), timeout=5) as connection:
                    connection.settimeout(None)
                    connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    play_stream(connection, device=args.device)
            except (OSError, EOFError, ValueError) as exc:
                print(f"Η σύνδεση διακόπηκε: {exc}. Νέα προσπάθεια σε {RECONNECT_DELAY_SECONDS}s.", flush=True)
                time.sleep(RECONNECT_DELAY_SECONDS)
    except KeyboardInterrupt:
        print("\nΟ Client τερματίστηκε.", flush=True)


if __name__ == "__main__":
    main()
