"""Director: μία λήψη μικροφώνου, μετάδοση σε πολλούς TCP Clients.

ΠΡΟΣΟΧΗ: πειραματικό mTLS v5 με υπογεγραμμένο μητρώο Clients.
Η δικτυακή εγγραφή και το GUI έγκρισης δεν έχουν υλοποιηθεί ακόμη.
"""

import argparse
from contextlib import ExitStack
from functools import partial
import os
import ssl
import subprocess
import sys
from pathlib import Path
import queue
import socket
import threading
import time
import uuid

from director.backup import BackupError, ensure_no_interrupted_restore, manager_lock, rollback
from director.broadcast import AudioBroadcaster, MAX_CLIENTS
from director.registry_reports import RegistryReports, ReportedRegistry
from shared.manager_key import ManagerKeyError, migrate as migrate_manager_key, password_path
from director.security import LastSeenStore, last_seen_path, track_last_seen
from shared.client_registry import RegistryError, SignedClientRegistry, digest
from director.manager_state import (RoleError, PROMOTE_WARNING, acknowledge,
                                    change_role, describe, load as load_manager_state,
                                    path_for as manager_state_path, require_primary)
from director.serving import ServingConflict, ServingController, SocketServingService
from director.registry_sync import (apply_from_file, examine, export_registry,
                                    status as registry_export_status, _display as show_registry_preview)
from shared.discovery import BeaconWatcher
from shared.enrollment import (EnrollmentError, EnrollmentServer, ENROLL_PORT,
                               IncorrectCodeError)
from shared.discovery import announce, director_id, DISCOVERY_PORT
from shared.identity import ProtocolMismatch, receive_hello_details, send_welcome
from shared.transport_security import accept_tls, server_context
from shared.audio import BLOCK_BYTES, BLOCK_SAMPLES, CHANNELS, DEFAULT_PORT, DTYPE, SAMPLE_RATE

# Κοινή ουρά λήψης του μικροφώνου (5 x 20 ms = 100 ms).
MAX_PENDING_BLOCKS = 5
STATS_INTERVAL_SECONDS = 5


class RoleAwareEnrollmentServer(EnrollmentServer):
    """Ελέγχει ρόλο ΠΡΙΝ αφαιρεθεί το εκκρεμές αίτημα από το decide."""

    def decide(self, request_id: str, code: str | None = None, *,
               approve: bool = False) -> dict:
        if self.pending_file is not None:
            require_primary(load_manager_state(
                manager_state_path(self.pending_file)))
        return super().decide(request_id, code, approve=approve)


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
  status        Ρόλος/εξυπηρέτηση/μητρώο και Online/Offline Clients
  stats         Εμφάνιση στατιστικών μία φορά
  stats on      Στατιστικά κάθε 5 δευτερόλεπτα
  stats off     Διακοπή περιοδικών στατιστικών (προεπιλογή)
  all           Μετάδοση σε όλους τους Clients
  none          Παύση ομιλίας προς όλους
  room <όνομα>  Μετάδοση σε μία ή περισσότερες αίθουσες
  client <UUID> Μετάδοση σε έναν ή περισσότερους Clients
  revoke <UUID> Ανάκληση Client και άμεση αποσύνδεσή του
  pending        Εκκρεμή αιτήματα: ID, αίθουσα, όνομα, λήξη (1 ημέρα)
  approve <ID> <κωδικός>  Έγκριση με τον κωδικό που έγραψε ο Client
                 (ID = οι 8 χαρακτήρες του pending· 3 προσπάθειες κωδικού)
  deny <ID>      Απόρριψη αιτήματος εγγραφής
  set-room <UUID> <αίθουσα>  Αλλαγή αίθουσας χωρίς νέο κλειδί
  set-name <UUID> <όνομα>    Αλλαγή ονόματος χωρίς νέο κλειδί
  serve on [force] / serve off      Έναρξη/διακοπή εξυπηρέτησης
  role primary confirm / role backup confirm  Αλλαγή ρόλου
  notice ok       Επιβεβαίωση ειδοποίησης μετά από restore
  registry export <φάκελος ή αρχείο>   Εξαγωγή υπογεγραμμένου μητρώου
  registry import <αρχείο> [confirm]  Εισαγωγή νεότερου μητρώου
  registry import <αρχείο> merge [prefer-incoming] confirm  Συγχώνευση
  registry accept-current [confirm]   Συνέχεια με αυτό το μητρώο (νεότερο που ανέφεραν
                                      Clients θεωρείται χαμένο· μόνο Κύριος)
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
                  registry: SignedClientRegistry | None = None,
                  reports=None) -> None:
    """TLS και HELLO ενός Client, σε δικό του νήμα ώστε να μην καθυστερεί τους άλλους."""
    # Το TLS ολοκληρώνεται πριν επιτραπεί οποιοδήποτε HELLO/PCM.
    try:
        connection = accept_tls(connection, tls_context)
        connection.settimeout(HELLO_TIMEOUT_SECONDS)
        client_id, room, seen = receive_hello_details(connection)
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
                registry_version, registry_hash = registry.welcome_info()
                if reports is not None:
                    # 4.2D3: μόνο εγκεκριμένος Client· μόνο ένδειξη, όχι εμπιστοσύνη.
                    reports.record(client_id, seen, room=entry["room"],
                                   name=entry.get("name") or "",
                                   local_rv=registry_version, local_hash=registry_hash)
                send_welcome(connection, accepted=True, registry_version=registry_version,
                             registry_hash=registry_hash)
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
        self._manager_state_path = manager_state_path(registry.path)
        self.reports = RegistryReports()

    def reported(self) -> ReportedRegistry | None:
        """Νεότερο/διαφορετικό μητρώο που ανέφεραν εγκεκριμένοι Clients (4.2D3)."""
        version, registry_hash = self.registry.welcome_info()
        approved = {e["client_id"] for e in self.registry.snapshot()["clients"]
                    if e["status"] == "approved"}
        return self.reports.reported(version, registry_hash, approved=approved)

    def accept_current(self, *, newer_hosts: tuple[str, ...] = ()) -> int:
        """«Συνέχεια με αυτό το μητρώο»: μόνο στον Κύριο, μόνο για αναφορές Clients.

        ``newer_hosts``: Managers σε λειτουργία με νεότερο μητρώο (beacon)· τότε
        η σωστή ενέργεια είναι η εισαγωγή, οπότε άρνηση.
        """
        with self.registry.lock:
            require_primary(load_manager_state(self._manager_state_path))
            if newer_hosts:
                raise RegistryError(f"Ο υπολογιστής {newer_hosts[0]} έχει νεότερο μητρώο "
                                    "σε λειτουργία· εισαγάγετέ το αντί γι' αυτό.")
            reported = self.reported()
            if reported is None:
                raise RegistryError("Δεν υπάρχει αναφορά νεότερου μητρώου από Clients.")
            return self.registry.advance_version(reported.target_version)

    def context(self) -> ssl.SSLContext:
        with self.registry.lock:
            return self._context

    def approve(self, client_id: str, room: str, cert_pem: bytes, *,
                name: str = "") -> None:
        """Έγκριση (και νέου κλειδιού μετά από ανάκληση) με άμεσο νέο mTLS context."""
        with self.registry.lock:
            require_primary(load_manager_state(self._manager_state_path))
            self.registry.approve(client_id, room, cert_pem, name=name)
            self._context = self.registry.tls_server_context(self._certfile, self._keyfile)

    def update_info(self, client_id: str, broadcaster: AudioBroadcaster, *,
                    room: str | None = None, name: str | None = None) -> dict:
        with self.registry.lock:
            require_primary(load_manager_state(self._manager_state_path))
            info = self.registry.update_client_info(client_id, room=room, name=name)
            broadcaster.update_client_info(client_id, info["room"], info["name"])
            return info

    def revoke(self, client_id: str, broadcaster: AudioBroadcaster) -> None:
        with self.registry.lock:
            require_primary(load_manager_state(self._manager_state_path))
            self.registry.revoke(client_id)
            self.reports.forget(client_id)
            broadcaster.disconnect_client(client_id)
            self._context = self.registry.tls_server_context(self._certfile, self._keyfile)


    def import_registry(self, source: Path, broadcaster: AudioBroadcaster,
                        *, merge_conflict: bool = False,
                        prefer: str = "local",
                        expected_digest: str | None = None) -> dict:
        """Εφαρμογή/αντικατάσταση TLSContext και ζωντανών συνδέσεων ατομικά."""
        with self.registry.lock:
            old = {e["client_id"]: e for e in self.registry.snapshot()["clients"]}
            result = apply_from_file(
                self.registry, source, merge_conflict=merge_conflict,
                prefer=prefer, expected_digest=expected_digest)
            if not result["changed"]:
                return result
            new = {e["client_id"]: e for e in result["registry"]["clients"]}
            self._context = self.registry.tls_server_context(self._certfile, self._keyfile)
            for client_id, previous in old.items():
                current = new.get(client_id)
                if (current is None or current["status"] != "approved" or
                        current["spki_sha256"] != previous["spki_sha256"]):
                    broadcaster.disconnect_client(client_id)
            for client_id, current in new.items():
                if current["status"] == "approved":
                    broadcaster.update_client_info(
                        client_id, current["room"], current.get("name", ""))
            return result


def accept_clients(server: socket.socket, broadcaster: AudioBroadcaster,
                   stop: threading.Event, *, tls_context: ssl.SSLContext | None = None,
                   max_pending: int = MAX_PENDING_HANDSHAKES,
                   registry: SignedClientRegistry | None = None,
                   context_provider=None, reports=None) -> None:
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
                _admit_client(connection, address, broadcaster, current_tls, registry=registry,
                              reports=reports)
            finally:
                pending.release()

        threading.Thread(target=admit, daemon=True, name=f"admit-{address}").start()


def command_loop(broadcaster: AudioBroadcaster, stop: threading.Event,
                 stats: DirectorStats, *, revoke=None,
                 enrollment: EnrollmentServer | None = None, trust=None,
                 controller=None, watcher=None, state_path=None,
                 registry_file=None, tls_cert=None) -> None:
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
                if state_path is not None:
                    local_state = load_manager_state(state_path)
                    print(describe(local_state), flush=True)
                    if controller is not None:
                        print("[Manager] Πραγματική εξυπηρέτηση: " +
                              ("ενεργή" if controller.active else "σταματημένη"), flush=True)
                    if trust is not None:
                        snapshot = trust.registry.snapshot()
                        print(f'[Manager] Μητρώο v{snapshot["registry_version"]}', flush=True)
                        if local_state.role == "primary" and tls_cert is not None:
                            info = registry_export_status(registry_file, tls_cert)
                            print("[Manager] Αλλαγές που δεν έχουν εξαχθεί: " +
                                  ("ποτέ" if info["unexported"] is None else
                                   str(info["unexported"])), flush=True)
                    if trust is not None:
                        reported = trust.reported()
                        if reported is not None:
                            print(reported.status_line(), flush=True)
                    if watcher is not None:
                        for other in watcher.others():
                            print(f'[Manager] Άλλος Manager: {other.info.host} '
                                  f'({other.ip}), μητρώο v{other.info.rv}', flush=True)
                            if trust is not None and other.info.rv > trust.registry.version:
                                print(f'[Manager] Το αντίγραφο χρειάζεται ενημέρωση: '
                                      f'ο υπολογιστής {other.info.host} έχει μητρώο '
                                      f'v{other.info.rv}, εδώ v{trust.registry.version}.', flush=True)
                clients = broadcaster.status_snapshot()
                online = sum(1 for _, _, active, _ in clients if active)
                print(f"Clients online: {online} | offline: {len(clients) - online}", flush=True)
                for client_id, room, active, age in clients:
                    state = "Online" if active else "Offline"
                    print(f"  {client_id} | {room} | {state} | τελευταίο heartbeat πριν {age:.1f}s", flush=True)
            elif command == "serve" and controller is not None:
                if values in (["on"], ["on", "force"]):
                    controller.start(force=values == ["on", "force"])
                    print("[Manager] Εξυπηρέτηση: ενεργή", flush=True)
                elif values == ["off"]:
                    controller.stop()
                    print("[Manager] Εξυπηρέτηση: σταματημένη", flush=True)
                else:
                    print("Χρήση: serve on [force] ή serve off", flush=True)
            elif command == "role" and state_path is not None and values:
                if values == ["primary"]:
                    print(PROMOTE_WARNING, flush=True)
                    if watcher is not None and trust is not None:
                        for other in watcher.newer(trust.registry.version):
                            print(f"Ο υπολογιστής {other.info.host} έχει νεότερο "
                                  f"μητρώο (v{other.info.rv}). Εισαγάγετέ το πρώτα.",
                                  flush=True)
                    print("Για επιβεβαίωση: role primary confirm", flush=True)
                elif values == ["backup"]:
                    print("Για επιβεβαίωση: role backup confirm", flush=True)
                elif len(values) == 2 and values[1] == "confirm" and values[0] in (
                        "primary", "backup"):
                    if values[0] == "primary":
                        print(PROMOTE_WARNING, flush=True)
                        if watcher is not None and trust is not None:
                            for other in watcher.newer(trust.registry.version):
                                print(f"Ο υπολογιστής {other.info.host} έχει νεότερο "
                                      f"μητρώο (v{other.info.rv}). Εισαγάγετέ το πρώτα.",
                                      flush=True)
                    new_state = change_role(state_path, values[0])
                    if controller is not None:
                        controller.role_changed()
                    print(describe(new_state), flush=True)
                else:
                    print("Χρήση: role primary|backup confirm", flush=True)
            elif command == "notice" and values == ["ok"] and state_path is not None:
                print(describe(acknowledge(state_path)), flush=True)
            elif command == "registry" and values and trust is not None:
                action = values[0]
                if action == "export" and len(values) >= 2:
                    destination = " ".join(values[1:])
                    result = export_registry(registry_file, tls_cert, Path(destination))
                    print(f"[Manager] Εξαγωγή: {result}", flush=True)
                elif action == "import" and len(values) >= 2:
                    source = Path(values[1])
                    merge_conflict = "merge" in values[2:]
                    prefer = "incoming" if "prefer-incoming" in values[2:] else "local"
                    approved = "confirm" in values[2:]
                    if any(item not in ("merge", "prefer-incoming", "confirm")
                           for item in values[2:]):
                        raise ValueError("Άγνωστη επιλογή εισαγωγής")
                    document, local, relation_kind, view = examine(
                        registry_file, tls_cert, source,
                        allow_merge=merge_conflict, prefer=prefer)
                    show_registry_preview(view)
                    if relation_kind == "same":
                        print("Το μητρώο είναι ήδη ενημερωμένο.", flush=True)
                    elif not approved:
                        print("Προσθέστε confirm για εφαρμογή.", flush=True)
                    else:
                        result = trust.import_registry(
                            source, broadcaster, merge_conflict=merge_conflict,
                            prefer=prefer, expected_digest=digest(local))
                        print(f'[Manager] Εισαγωγή: {result["relation"]}', flush=True)
                elif action == "accept-current" and values[1:] in ([], ["confirm"]):
                    newer_hosts = (tuple(o.info.host for o in watcher.newer(
                        trust.registry.version)) if watcher is not None else ())
                    reported = trust.reported()
                    if reported is None:
                        print("Δεν υπάρχει αναφορά νεότερου μητρώου από Clients.", flush=True)
                    elif values[1:] == []:
                        print(reported.status_line(), flush=True)
                        print(reported.accept_warning(), flush=True)
                        print("Για επιβεβαίωση: registry accept-current confirm", flush=True)
                    else:
                        version = trust.accept_current(newer_hosts=newer_hosts)
                        print(f"[Manager] Συνέχεια με αυτό το μητρώο: νέα έκδοση v{version}",
                              flush=True)
                else:
                    print("Χρήση: registry export|import <αρχείο> ή registry accept-current "
                          "[confirm]", flush=True)
            elif command == "revoke" and len(values) == 1 and revoke is not None:
                revoke(values[0])
                print(f"Ανακλήθηκε ο Client {values[0]}", flush=True)
            elif command == "pending" and not values and enrollment is not None:
                requests = enrollment.pending()
                if not requests:
                    print("Δεν υπάρχουν εκκρεμείς εγγραφές.", flush=True)
                for request_id, room, name, expires in requests:
                    until = time.strftime("%d/%m %H:%M", time.localtime(expires))
                    print(f"  {request_id[:8]} | {room} | {name} | λήγει {until}", flush=True)
            elif command == "approve" and len(values) == 2 and enrollment is not None:
                request_id = enrollment.resolve(values[0])
                try:
                    info = enrollment.decide(request_id, values[1], approve=True)
                except IncorrectCodeError as exc:
                    if exc.remaining:
                        left = ("Απομένει 1 προσπάθεια" if exc.remaining == 1
                                else f"Απομένουν {exc.remaining} προσπάθειες")
                        print(f"Λάθος κωδικός για {request_id[:8]}. {left}.", flush=True)
                    else:
                        print(f"Λάθος κωδικός: το αίτημα {request_id[:8]} απορρίφθηκε "
                              "(3 λάθη). Ζητήστε νέα εγγραφή από τον υπολογιστή.", flush=True)
                    continue
                print(f"Εγκρίθηκε: {info['room']} | {info['name']} | {info['client_id']}",
                      flush=True)
            elif command == "deny" and len(values) == 1 and enrollment is not None:
                request_id = enrollment.resolve(values[0])
                info = enrollment.decide(request_id, approve=False)
                print(f"Απορρίφθηκε: {info['room']} | {info['name']}", flush=True)
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
                        help="Ξεχωριστή TLS θύρα εγγραφής (πάντα ανοιχτή, έως 50 αιτήματα)")
    # Μόνο για τα αυτόματα scripts πραγματικής δοκιμής (όριο αιτημάτων χωρίς 50 εγγραφές).
    parser.add_argument("--max-pending-requests", type=int, default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--watch-port", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--extra-beacon-port", type=int, default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--start-check-seconds", type=float, default=3.0,
                        help=argparse.SUPPRESS)
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
    data_dir = args.registry_file.parent
    runtime = ExitStack()
    # Χωριστά, ώστε το GUI να αφήσει το κλείδωμα πριν από επαναφορά (Ρυθμίσεις → Συντήρηση).
    lock = ExitStack()
    runtime.callback(lock.close)
    restart = False
    try:
        if not (args.approve_client_cert or args.revoke_client or args.list_clients):
            # Ο Manager κρατά το κλείδωμα σε ΟΛΗ τη λειτουργία του (CLI και --gui), ώστε
            # το «python -m director.backup restore» να αρνείται όσο τρέχει (4.2C §6.2).
            # Οι σύντομες offline εντολές (approve/revoke/list) δεν το κρατούν, γιατί
            # τρέχουν και δίπλα σε ενεργό Manager· ελέγχουν μόνο το journal.
            lock.enter_context(manager_lock(data_dir))
            try:
                ensure_no_interrupted_restore(data_dir)
            except BackupError:
                # Με --gui: ερώτηση στο παράθυρο αντί για εντολή στο τερματικό.
                if not args.gui or not _ask_gui_rollback():
                    raise
                rollback(data_dir)
            # 4.2C2: παλιό ακρυπτογράφητο κλειδί → κρυπτογράφηση στη θέση του (ίδιο αποτύπωμα).
            if migrate_manager_key(args.tls_key):
                print("Το ιδιωτικό κλειδί TLS του Manager κρυπτογραφήθηκε στον δίσκο "
                      f"(κωδικός στο {password_path(args.tls_key).name}).", flush=True)
        else:
            ensure_no_interrupted_restore(data_dir)
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
                require_primary(load_manager_state(manager_state_path(args.registry_file)))
                registry.approve(client_id, args.approve_room, pem)
                print(f"Εγκρίθηκε Client {client_id} (v{registry.version})", flush=True)
                return
            if args.revoke_client:
                require_primary(load_manager_state(manager_state_path(args.registry_file)))
                registry.revoke(args.revoke_client)
                print(f"Ανακλήθηκε Client {args.revoke_client} (v{registry.version})", flush=True)
                return
        except RoleError as exc:
            parser.exit(6, f"Σφάλμα ρόλου Manager: {exc}\n")
        except (RegistryError, ValueError, OSError) as exc:
            parser.exit(2, f"Σφάλμα μητρώου Clients: {exc}\n")
        if args.list_clients:
            for entry in registry.snapshot()["clients"]:
                print("{} | {} | {}".format(entry["client_id"], entry["room"], entry["status"]))
            return
        trust = RegistryTrust(registry, args.tls_cert, args.tls_key)
        secure_accept = partial(accept_clients, tls_context=trust.context(),
                                registry=registry, context_provider=trust.context,
                                reports=trust.reports)
        # Η θύρα εγγραφής είναι πάντα ανοιχτή (απόφαση χρήστη 22/11/2026): ανοίγει με το
        # CLI παρακάτω ή με το GuiRuntime.start στο --gui.
        enrollment = RoleAwareEnrollmentServer(args.tls_cert, args.tls_key, trust,
                                      host=args.host, port=args.enrollment_port,
                                      max_pending=args.max_pending_requests,
                                      pending_file=args.registry_file.with_name(
                                          "enrollment-pending.json"))
        last_seen = LastSeenStore(last_seen_path(args.registry_file))
        if args.gui:
            from director.gui import run_gui
            restart = bool(run_gui(args, accept_clients=secure_accept,
                                   stream_microphone=stream_microphone,
                                   enrollment=enrollment, trust=trust, last_seen=last_seen,
                                   release_lock=lock.close))
            return

        broadcaster = AudioBroadcaster(max_clients=MAX_CLIENTS, on_event=lambda s: print(s, flush=True))
        stop = threading.Event()
        stats = DirectorStats()
        identifier = director_id(args.director_id_file)
        # Ο BeaconWatcher τρέχει ακόμη και όταν η εξυπηρέτηση είναι σταματημένη.
        instance_id = str(uuid.uuid4())
        school_id = registry.snapshot()["school_id"]
        watcher = BeaconWatcher(school_id, instance_id,
                                port=args.watch_port or args.discovery_port)
        network = SocketServingService(
            args, identifier=identifier, inst=instance_id,
            registry=registry, accept_clients=secure_accept,
            broadcaster=broadcaster, enrollment=enrollment)
        controller = ServingController(
            manager_state_path(args.registry_file), watcher,
            start_network=network.start, stop_network=network.stop,
            change_enrollment=network.change_enrollment,
            check_seconds=args.start_check_seconds)
        watcher.start()
        try:
            controller.startup()
            print(describe(load_manager_state(manager_state_path(args.registry_file))),
                  flush=True)
            commands = threading.Thread(
                target=command_loop, args=(broadcaster, stop, stats),
                kwargs={"revoke": lambda cid: trust.revoke(cid, broadcaster),
                        "enrollment": enrollment, "trust": trust,
                        "controller": controller, "watcher": watcher,
                        "state_path": manager_state_path(args.registry_file),
                        "registry_file": args.registry_file, "tls_cert": args.tls_cert},
                daemon=True, name="director-commands")
            commands.start()
            reporter = threading.Thread(
                target=periodic_stats, args=(stats, broadcaster, stop),
                daemon=True, name="director-stats")
            reporter.start()
            seen = threading.Thread(
                target=track_last_seen, args=(last_seen, broadcaster, stop),
                daemon=True, name="director-last-seen")
            seen.start()

            def monitor_other_managers():
                while not stop.wait(0.5):
                    controller.poll_conflicts()
            monitoring = threading.Thread(
                target=monitor_other_managers,
                daemon=True, name="director-collision-monitor")
            monitoring.start()
            try:
                stream_microphone(broadcaster, device=args.device, latency=args.latency,
                                  stop=stop, stats=stats)
            except KeyboardInterrupt:
                print("\nΟ Director τερματίστηκε.", flush=True)
            finally:
                stop.set()
                controller.shutdown()
                watcher.close()
                reporter.join(timeout=1)
                monitoring.join(timeout=1)
                seen.join(timeout=2)
                broadcaster.close()
        finally:
            controller.shutdown()
            watcher.close()

    except BackupError as exc:
        parser.exit(exc.status, str(exc) + "\n")
    except ManagerKeyError as exc:
        parser.exit(2, str(exc) + "\n")
    finally:
        runtime.close()
        if restart:  # μετά το runtime.close(): όλα τα sockets και το κλείδωμα είναι ελεύθερα
            restart_manager()


def _ask_gui_rollback() -> bool:
    try:
        from director.qt_maintenance import ask_rollback
    except ImportError:
        return False
    return ask_rollback()


def restart_command() -> list[str]:
    """Η ίδια εντολή με την οποία ξεκίνησε ο Manager (π.χ. python -m director.main --gui …)."""
    return [sys.executable, *sys.orig_argv[1:]]


def restart_manager() -> None:
    """Νέα εκκίνηση μετά από επαναφορά από το GUI· το κλείδωμα έχει ήδη αφεθεί."""
    print("Επανεκκίνηση Manager μετά την επαναφορά…", flush=True)
    command = restart_command()
    if os.name == "nt":
        subprocess.Popen(command)  # ίδια κονσόλα· η τρέχουσα διεργασία τελειώνει αμέσως
        return
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
