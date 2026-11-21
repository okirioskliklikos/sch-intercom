"""Director: μία λήψη μικροφώνου, μετάδοση σε πολλούς TCP Clients.

ΠΡΟΣΟΧΗ: πειραματικό mTLS v5 με υπογεγραμμένο μητρώο Clients.
Η δικτυακή εγγραφή και το GUI έγκρισης δεν έχουν υλοποιηθεί ακόμη.
"""

import argparse
from functools import partial
import ssl
from pathlib import Path
import queue
import socket
import threading
import time

from director.broadcast import AudioBroadcaster, MAX_CLIENTS
from shared.client_registry import RegistryError, SignedClientRegistry
from shared.enrollment import EnrollmentError, EnrollmentServer, ENROLL_PORT
from shared.discovery import announce, director_id, DISCOVERY_PORT
from shared.identity import ProtocolMismatch, receive_hello, send_welcome
from shared.transport_security import accept_tls, server_context
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
  revoke <UUID> Ανάκληση Client και άμεση αποσύνδεσή του
  enroll on/off  Άνοιγμα/κλείσιμο περιορισμένης θύρας εγγραφής
  pending        Εκκρεμή αιτήματα με κωδικό επιβεβαίωσης SAS
  approve <ID> <SAS>  Έγκριση αιτήματος μετά τη σύγκριση κωδικού
  deny <ID>      Απόρριψη αιτήματος εγγραφής
  set-room <UUID> <αίθουσα>  Αλλαγή αίθουσας χωρίς νέο κλειδί
  set-name <UUID> <όνομα>    Αλλαγή ονόματος χωρίς νέο κλειδί
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


#: Ταυτόχρονες προσπάθειες σύνδεσης (TLS + HELLO σε εξέλιξη), όχι όριο
#: συνδεδεμένων Clients. Οι επιπλέον κλείνουν αμέσως και ο Client ξαναδοκιμάζει
#: μετά από 5 s ±1 s (client.main.RECONNECT_DELAY_SECONDS).
MAX_PENDING_HANDSHAKES = 25
#: Ένα μήνυμα ανά τόσα δευτερόλεπτα για απορρίψεις λόγω φόρτου.
BUSY_LOG_INTERVAL_SECONDS = 5.0
HELLO_TIMEOUT_SECONDS = 2.0


def _admit_client(connection: socket.socket, address, broadcaster: AudioBroadcaster,
                  tls_context: ssl.SSLContext, *,
                  registry: SignedClientRegistry | None = None) -> None:
    """TLS και HELLO ενός Client, σε δικό του νήμα ώστε να μην καθυστερεί τους άλλους."""
    # Το TLS ολοκληρώνεται πριν επιτραπεί οποιοδήποτε HELLO/PCM.
    try:
        connection = accept_tls(connection, tls_context)
        connection.settimeout(HELLO_TIMEOUT_SECONDS)
        client_id, room = receive_hello(connection)
        if registry is not None:
            with registry.lock:
                reason = registry.rejection_reason(
                    connection.getpeercert(binary_form=True), client_id, room)
                if reason is not None:
                    print(f"[Director] Μη εγκεκριμένος Client {address}: {client_id}: {reason}",
                          flush=True)
                    connection.close()
                    return
                entry = registry.approved_entry(client_id)
                if entry is None:
                    connection.close()
                    return
                if room != entry["room"]:
                    print(f"[Director] Client {client_id}: δηλώνει {room}, "
                          f"ο Manager ορίζει {entry['room']}", flush=True)
                send_welcome(connection, accepted=True)
                if not broadcaster.add_client(connection, client_id, entry["room"],
                                              address, name=entry.get("name") or ""):
                    print(f"[Director] Όριο Clients: {broadcaster.max_clients}", flush=True)
                return
        # Legacy isolated transport tests only; production always supplies registry.
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
        return
    except (OSError, EOFError, ValueError) as exc:
        print(f"[Director] Μη αποδεκτή δήλωση {address}: {exc}", flush=True)
        connection.close()
        return
    if not broadcaster.add_client(connection, client_id, room, address):
        print(f"[Director] Απόρριψη Client {address}: όριο {broadcaster.max_clients}", flush=True)


class RegistryTrust:
    """Τρέχον mTLS context ενωμένο με το μητρώο: ανάκληση και αναδημιουργία ατομικά."""

    def __init__(self, registry: SignedClientRegistry, certfile: Path, keyfile: Path):
        self.registry = registry
        self._certfile = certfile
        self._keyfile = keyfile
        self._context = registry.tls_server_context(certfile, keyfile)

    def context(self) -> ssl.SSLContext:
        with self.registry.lock:
            return self._context

    def approve(self, client_id: str, room: str, cert_pem: bytes, *,
                name: str = "") -> None:
        """Έγκριση (και νέου κλειδιού μετά από ανάκληση) με άμεσο νέο mTLS context."""
        with self.registry.lock:
            self.registry.approve(client_id, room, cert_pem, name=name)
            self._context = self.registry.tls_server_context(self._certfile, self._keyfile)

    def update_info(self, client_id: str, broadcaster: AudioBroadcaster, *,
                    room: str | None = None, name: str | None = None) -> dict:
        with self.registry.lock:
            info = self.registry.update_client_info(client_id, room=room, name=name)
            broadcaster.update_client_info(client_id, info["room"], info["name"])
            return info

    def revoke(self, client_id: str, broadcaster: AudioBroadcaster) -> None:
        with self.registry.lock:
            self.registry.revoke(client_id)
            broadcaster.disconnect_client(client_id)
            self._context = self.registry.tls_server_context(self._certfile, self._keyfile)


def accept_clients(server: socket.socket, broadcaster: AudioBroadcaster,
                   stop: threading.Event, *, tls_context: ssl.SSLContext | None = None,
                   max_pending: int = MAX_PENDING_HANDSHAKES,
                   registry: SignedClientRegistry | None = None,
                   context_provider=None) -> None:
    """Αποδέχεται νέους Clients ανεξάρτητα από την καταγραφή ήχου.

    Το νήμα αυτό μόνο δέχεται συνδέσεις. Το TLS και το HELLO κάθε Client γίνονται
    σε ξεχωριστό νήμα, ώστε μια αργή ή σιωπηλή σύνδεση να μην καθυστερεί τους
    υπόλοιπους. Το πολύ `max_pending` τέτοια νήματα ταυτόχρονα.
    """
    if not isinstance(tls_context, ssl.SSLContext):
        raise ValueError("TLS context required for incoming Clients")
    pending = threading.BoundedSemaphore(max_pending)
    busy_rejections = 0
    last_busy_log = 0.0
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
        if not pending.acquire(blocking=False):
            # Ο Client ξαναδοκιμάζει μόνος του· ένα σύντομο μήνυμα, όχι ένα ανά σύνδεση.
            connection.close()
            busy_rejections += 1
            now = time.monotonic()
            if now - last_busy_log >= BUSY_LOG_INTERVAL_SECONDS:
                print(f"[Director] {busy_rejections} συνδέσεις σε αναμονή λόγω φόρτου· "
                      "οι Clients ξαναδοκιμάζουν αυτόματα", flush=True)
                busy_rejections = 0
                last_busy_log = now
            continue

        def admit(connection=connection, address=address):
            try:
                current_tls = context_provider() if context_provider is not None else tls_context
                _admit_client(connection, address, broadcaster, current_tls, registry=registry)
            finally:
                pending.release()

        threading.Thread(target=admit, daemon=True, name=f"admit-{address}").start()


def command_loop(broadcaster: AudioBroadcaster, stop: threading.Event,
                 stats: DirectorStats, *, revoke=None,
                 enrollment: EnrollmentServer | None = None, trust=None) -> None:
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
            elif command == "revoke" and len(values) == 1 and revoke is not None:
                revoke(values[0])
                print(f"Ανακλήθηκε ο Client {values[0]}", flush=True)
            elif command == "enroll" and values == ["on"] and enrollment is not None:
                enrollment.start()
            elif command == "enroll" and values == ["off"] and enrollment is not None:
                enrollment.stop()
            elif command == "pending" and not values and enrollment is not None:
                requests = enrollment.pending()
                if not requests:
                    print("Δεν υπάρχουν εκκρεμείς εγγραφές.", flush=True)
                for request_id, name, room, sas in requests:
                    print(f"{request_id} | {name} | {room} | SAS {sas}", flush=True)
            elif command == "approve" and len(values) == 2 and enrollment is not None:
                enrollment.decide(values[0], values[1], approve=True)
                print(f"Εγκρίθηκε αίτημα {values[0]}", flush=True)
            elif command == "deny" and len(values) == 1 and enrollment is not None:
                enrollment.decide(values[0], approve=False)
                print(f"Απορρίφθηκε αίτημα {values[0]}", flush=True)
            elif command == "set-room" and len(values) == 2 and trust is not None:
                info = trust.update_info(values[0], broadcaster, room=values[1])
                print(f"Ενημερώθηκε αίθουσα: {info['room']}", flush=True)
            elif command == "set-name" and len(values) >= 2 and trust is not None:
                info = trust.update_info(values[0], broadcaster, name=" ".join(values[1:]))
                print(f"Ενημερώθηκε όνομα: {info['name']}", flush=True)
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
    parser.add_argument("--tls-cert", type=Path, required=True,
                        help="Πιστοποιητικό TLS Director (PEM, SAN DNS)")
    parser.add_argument("--tls-key", type=Path, required=True,
                        help="Ιδιωτικό κλειδί TLS Director (εκτός repository)")
    parser.add_argument("--enrollment-port", type=int, default=ENROLL_PORT,
                        help="Ξεχωριστή TLS θύρα εγγραφής (ανοίγει μόνο με enroll on)")
    parser.add_argument("--registry-file", type=Path,
                        default=Path.home() / ".school-intercom" / "clients-registry.json",
                        help="Υπογεγραμμένο μητρώο Clients")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--approve-client-cert", type=Path,
                         help="Έγκριση από τοπικό πιστοποιητικό PEM (εκτός σύνδεσης)")
    actions.add_argument("--revoke-client", metavar="UUID",
                         help="Ανάκληση πιστοποίησης Client (εκτός σύνδεσης)")
    actions.add_argument("--list-clients", action="store_true",
                         help="Προβολή μητρώου Clients")
    parser.add_argument("--approve-room", default=None,
                        help="Αίθουσα που αντιστοιχεί στο πιστοποιητικό")
    parser.add_argument("--gui", action="store_true",
                        help="Κεντρική οθόνη Director (PySide6, pip install -e .[gui])")
    args = parser.parse_args()
    tls_context = server_context(args.tls_cert, args.tls_key)
    try:
        registry = SignedClientRegistry(args.registry_file, args.tls_cert, args.tls_key)
        if args.approve_client_cert:
            from cryptography import x509
            from cryptography.x509.oid import NameOID
            if args.approve_room is None:
                parser.error("--approve-client-cert απαιτεί --approve-room")
            pem = args.approve_client_cert.read_bytes()
            names = x509.load_pem_x509_certificate(pem).subject.get_attributes_for_oid(
                NameOID.COMMON_NAME)
            if len(names) != 1:
                raise RegistryError("Certificate identity does not match UUID")
            client_id = names[0].value
            registry.approve(client_id, args.approve_room, pem)
            print(f"Εγκρίθηκε Client {client_id} (v{registry.version})", flush=True)
            return
        if args.revoke_client:
            registry.revoke(args.revoke_client)
            print(f"Ανακλήθηκε Client {args.revoke_client} (v{registry.version})", flush=True)
            return
    except (RegistryError, ValueError, OSError) as exc:
        parser.exit(2, f"Σφάλμα μητρώου Clients: {exc}\n")
    if args.list_clients:
        for entry in registry.snapshot()["clients"]:
            print("{} | {} | {}".format(entry["client_id"], entry["room"], entry["status"]))
        return
    trust = RegistryTrust(registry, args.tls_cert, args.tls_key)
    secure_accept = partial(accept_clients, tls_context=trust.context(),
                            registry=registry, context_provider=trust.context)
    if args.gui:
        from director.gui import run_gui
        run_gui(args, accept_clients=secure_accept, stream_microphone=stream_microphone)
        return

    broadcaster = AudioBroadcaster(max_clients=MAX_CLIENTS, on_event=lambda s: print(s, flush=True))
    enrollment = EnrollmentServer(args.tls_cert, args.tls_key, trust,
                                  host=args.host, port=args.enrollment_port)
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
        receiver = threading.Thread(target=secure_accept, args=(server, broadcaster, stop),
                                    daemon=True, name="accept-clients")
        receiver.start()
        commands = threading.Thread(target=command_loop, args=(broadcaster, stop, stats),
                                    kwargs={"revoke": lambda client_id: trust.revoke(client_id, broadcaster),
                                            "enrollment": enrollment, "trust": trust},
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
            enrollment.stop()
            receiver.join(timeout=1)
            beacon.join(timeout=1)
            reporter.join(timeout=1)
            broadcaster.close()


if __name__ == "__main__":
    main()
