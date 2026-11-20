"""Director: μία λήψη μικροφώνου, μετάδοση σε πολλούς TCP Clients.

ΠΡΟΣΟΧΗ: πειραματικό πρωτόκολλο χωρίς TLS/πιστοποίηση.
Χρήση μόνο σε ελεγχόμενο δίκτυο δοκιμών.
"""

import argparse
from pathlib import Path
import queue
import socket
import threading
import time

from director.broadcast import AudioBroadcaster, MAX_CLIENTS
from shared.discovery import announce, director_id, DISCOVERY_PORT
from shared.identity import ProtocolMismatch, receive_hello, send_welcome
from shared.audio import BLOCK_BYTES, BLOCK_SAMPLES, CHANNELS, DEFAULT_PORT, DTYPE, SAMPLE_RATE

# Κοινή ουρά λήψης του μικροφώνου (5 x 20 ms = 100 ms).
MAX_PENDING_BLOCKS = 5
STATS_INTERVAL_SECONDS = 5


class DirectorStats:
    """Thread-safe μετρητές και προαιρετική περιοδική εμφάνιση."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started = time.monotonic()
        self.frames = 0
        self.microphone_dropped = 0
        self.periodic = False

    def update(self, frames: int, dropped: int) -> None:
        with self._lock:
            self.frames = frames
            self.microphone_dropped = dropped

    def set_periodic(self, enabled: bool) -> None:
        with self._lock:
            self.periodic = enabled

    def periodic_enabled(self) -> bool:
        with self._lock:
            return self.periodic

    def report(self, broadcaster: AudioBroadcaster) -> str:
        with self._lock:
            frames, lost = self.frames, self.microphone_dropped
        elapsed = time.monotonic() - self.started
        return (
            f"[Director] {frames} blocks / {elapsed:.1f}s | "
            f"χαμένα μικροφώνου: {lost} | "
            f"χαμένα ουρών Clients: {broadcaster.dropped} | "
            f"Clients: {broadcaster.client_count}"
        )


def periodic_stats(stats: DirectorStats, broadcaster: AudioBroadcaster,
                   stop: threading.Event) -> None:
    while not stop.wait(STATS_INTERVAL_SECONDS):
        if stats.periodic_enabled():
            print(stats.report(broadcaster), flush=True)


def show_help() -> str:
    return """Διαθέσιμες εντολές:
  help, ?       Εμφάνιση διαθέσιμων εντολών
  status        Online/Offline Clients, αίθουσες και τελευταίο heartbeat
  stats         Εμφάνιση στατιστικών μία φορά
  stats on      Στατιστικά κάθε 5 δευτερόλεπτα
  stats off     Διακοπή περιοδικών στατιστικών (προεπιλογή)
  all           Μετάδοση σε όλους τους Clients
  none          Παύση ομιλίας προς όλους
  room <όνομα>  Μετάδοση σε μία ή περισσότερες αίθουσες
  client <UUID> Μετάδοση σε έναν ή περισσότερους Clients
  quit          Τερματισμός Director"""




def stream_microphone(broadcaster: AudioBroadcaster, *, device: int | None = None,
                      latency: str | float = "low", stop: threading.Event | None = None,
                      stats: DirectorStats | None = None,
                      level_callback=None, ready_callback=None) -> None:
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
        if level_callback is not None:
            # Η στάθμη είναι ένδειξη λήψης μικροφώνου, όχι επιβεβαίωση
            # ότι ακούστηκε η ανακοίνωση σε κάποιον Client.
            samples = memoryview(block).cast("h")
            peak = max((abs(sample) for sample in samples), default=0) / 32768
            level_callback(min(1.0, peak))
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
    with sd.RawInputStream(
        samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=DTYPE,
        blocksize=BLOCK_SAMPLES, device=device, latency=latency, callback=on_audio,
    ) as microphone:
        if ready_callback is not None:
            ready_callback()
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
            if stats is not None:
                stats.update(frames_sent, dropped)


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
        # Πειραματική δήλωση Client: μη πιστοποιημένα στοιχεία.
        connection.settimeout(2.0)
        try:
            client_id, room = receive_hello(connection)
            send_welcome(connection, accepted=True)
        except ProtocolMismatch as exc:
            print(f"[Director] Απόρριψη {address}: {exc}", flush=True)
            if exc.client_version is not None:
                # Ο νεότερος/διαφορετικός Client καταλαβαίνει την απάντηση· ο παλιός όχι.
                try:
                    send_welcome(connection, accepted=False)
                except OSError:
                    pass
            connection.close()
            continue
        except (OSError, EOFError, ValueError) as exc:
            print(f"[Director] Μη αποδεκτή δήλωση {address}: {exc}", flush=True)
            connection.close()
            continue
        if not broadcaster.add_client(connection, client_id, room, address):
            print(f"[Director] Απόρριψη Client {address}: όριο {broadcaster.max_clients}", flush=True)


def command_loop(broadcaster: AudioBroadcaster, stop: threading.Event,
                 stats: DirectorStats) -> None:
    """Προσωρινός έλεγχος παραληπτών από το τερματικό του Director."""
    print("Πληκτρολογήστε help για τις διαθέσιμες εντολές.", flush=True)
    while not stop.is_set():
        try:
            line = input("Director> ").strip()
        except (EOFError, OSError):
            return
        if not line:
            continue
        command, *values = line.split()
        try:
            if command in ("help", "?") and not values:
                print(show_help(), flush=True)
            elif command == "stats" and not values:
                print(stats.report(broadcaster), flush=True)
            elif command == "stats" and values == ["on"]:
                stats.set_periodic(True)
                print("Περιοδικά στατιστικά ενεργά (κάθε 5 δευτερόλεπτα)", flush=True)
            elif command == "stats" and values == ["off"]:
                stats.set_periodic(False)
                print("Περιοδικά στατιστικά ανενεργά", flush=True)
            elif command == "quit" and not values:
                stop.set()
                print("Τερματισμός Director...", flush=True)
                return
            elif command == "status" and not values:
                clients = broadcaster.status_snapshot()
                online = sum(1 for _, _, active, _ in clients if active)
                print(f"Clients online: {online} | offline: {len(clients) - online}", flush=True)
                for client_id, room, active, age in clients:
                    state = "Online" if active else "Offline"
                    print(f"  {client_id} | {room} | {state} | τελευταίο heartbeat πριν {age:.1f}s", flush=True)
            elif command == "all" and not values:
                broadcaster.set_targets()
                print("Μετάδοση σε όλους τους Clients", flush=True)
            elif command == "none" and not values:
                broadcaster.set_targets(rooms=set(), client_ids=set())
                print("Παύση ομιλίας προς όλους (σιωπηλά blocks)", flush=True)
            elif command == "room" and values:
                broadcaster.set_targets(rooms=set(values), client_ids=set())
                print("Επιλεγμένες αίθουσες:", ", ".join(values), flush=True)
            elif command == "client" and values:
                broadcaster.set_targets(rooms=set(), client_ids=set(values))
                print("Επιλεγμένοι Clients:", ", ".join(values), flush=True)
            else:
                print("Άγνωστη εντολή. Πληκτρολογήστε help.", flush=True)
        except ValueError as exc:
            print(f"Μη έγκυρη επιλογή: {exc}", flush=True)


def parse_latency(value: str) -> str | float:
    if value in ("low", "high"):
        return value
    return float(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="School Intercom — Director (πρωτότυπο)")
    parser.add_argument("--host", default="0.0.0.0", help="IP δέσμευσης (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--discovery-port", type=int, default=DISCOVERY_PORT,
                        help="UDP θύρα ανακάλυψης (default 50556)")
    parser.add_argument("--director-id-file", type=Path,
                        default=Path.home() / ".school-intercom" / "director.id",
                        help="Αρχείο σταθερής ταυτότητας Director")
    parser.add_argument("--device", type=int, default=None, help="Αριθμός συσκευής εισόδου PortAudio")
    parser.add_argument("--latency", type=parse_latency, default="low",
                        help="Καθυστέρηση κάρτας ήχου: low, high ή δευτερόλεπτα")
    parser.add_argument("--gui", action="store_true",
                        help="Προσωρινό γραφικό περιβάλλον δοκιμών (tkinter)")
    args = parser.parse_args()
    if args.gui:
        from director.gui import run_gui
        run_gui(args)
        return

    broadcaster = AudioBroadcaster(max_clients=MAX_CLIENTS, on_event=lambda s: print(s, flush=True))
    stop = threading.Event()
    stats = DirectorStats()
    identifier = director_id(args.director_id_file)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.host, args.port))
        server.listen(MAX_CLIENTS)
        print(f"Director ακούει στο {args.host}:{args.port} — έως {MAX_CLIENTS} Clients", flush=True)
        beacon = threading.Thread(target=announce,
                                  args=(stop, identifier, args.port),
                                  kwargs={"udp_port": args.discovery_port},
                                  daemon=True, name="director-discovery")
        beacon.start()
        receiver = threading.Thread(target=accept_clients, args=(server, broadcaster, stop),
                                    daemon=True, name="accept-clients")
        receiver.start()
        commands = threading.Thread(target=command_loop, args=(broadcaster, stop, stats),
                                    daemon=True, name="director-commands")
        commands.start()
        reporter = threading.Thread(target=periodic_stats,
                                    args=(stats, broadcaster, stop),
                                    daemon=True, name="director-stats")
        reporter.start()
        try:
            stream_microphone(broadcaster, device=args.device, latency=args.latency,
                              stop=stop, stats=stats)
        except KeyboardInterrupt:
            print("\nΟ Director τερματίστηκε.", flush=True)
        finally:
            stop.set()
            receiver.join(timeout=1)
            beacon.join(timeout=1)
            reporter.join(timeout=1)
            broadcaster.close()


if __name__ == "__main__":
    main()
