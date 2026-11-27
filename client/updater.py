"""Λήψη ενημερώσεων Client από τον Manager (Φάση 5B2a, docs/phase5.md §5.3–5.4).

Σε κάθε σύνδεση/επανασύνδεση (και όταν ο Manager το ζητήσει με UPDATE_CHECK) ο Client
στέλνει CHECK στη θύρα 50560 με mTLS. Αν υπάρχει νεότερη έκδοση:

1. λήψη στο ``staging\\<έκδοση>.part`` με συνέχιση από offset· όσο παίζει ανακοίνωση
   δεν ζητείται επόμενο κομμάτι (η σύνδεση κλείνει και η λήψη συνεχίζει μετά)·
2. SHA-256 όλου του αρχείου όπως το ανακοίνωσε ο Manager·
3. επαλήθευση **εδώ** (ενσωματωμένο κλειδί, hashes, όχι downgrade, συμβατός updater)
   και αποσυμπίεση σε ``versions\\<έκδοση>`` (μέσω ``<έκδοση>.new``)·
4. REPORT «ready». Την εναλλαγή έκδοσης την κάνει ο supervisor (5B2b).

Ο Client δεν εμπιστεύεται ούτε τον Manager ούτε το δίκτυο: μόνο την υπογραφή.
"""
from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import threading
import time

from client.install_paths import Layout, atomic_write_json
from shared.identity import CONNECTION_PROTOCOL
from shared.package import UPDATER_VERSION, PackageError, extract_package
from shared.transport_security import DEFAULT_SERVER_NAME
from shared.update_protocol import (CHUNK_BYTES, IDLE_TIMEOUT, UPDATE_PORT, UpdateProtocolError,
                                    check_message, parse_offer, read_exact, read_message,
                                    report_message, send_message)
from shared.version import parse_version

STAGING = "staging"
CONNECT_TIMEOUT = 5.0
#: 5B2b: εναλλαγή μόνο 10 s μετά το τέλος της τελευταίας ανακοίνωσης (§5.4.4).
SWITCH_QUIET_SECONDS = 10.0
#: Διακοπή στη μέση λήψης (δίκτυο/Manager): συνέχιση από το offset μετά από τόσα s.
ERROR_RETRY = 30.0


class AnnouncementState:
    """Αν παίζει τώρα ανακοίνωση (το ενημερώνει η λήψη ήχου)."""

    def __init__(self, clock=time.monotonic) -> None:
        self._active = threading.Event()
        self.clock = clock
        self._ended: float | None = None

    def set(self, active: bool) -> None:
        if active:
            self._active.set()
        else:
            if self._active.is_set():
                self._ended = self.clock()
            self._active.clear()

    def quiet_for(self) -> float:
        """Δευτερόλεπτα χωρίς ανακοίνωση (από το τελευταίο τέλος· άπειρο αν καμία)."""
        if self._active.is_set():
            return 0.0
        return float("inf") if self._ended is None else self.clock() - self._ended

    @property
    def active(self) -> bool:
        return self._active.is_set()


class Retry(Exception):
    def __init__(self, seconds: float, reason: str):
        super().__init__(reason)
        self.seconds = seconds


class Updater:
    """Ένα νήμα παρασκηνίου· ``trigger(host)`` ζητά έλεγχο στον Manager ``host``."""

    def __init__(self, layout: Layout, client_id: str, tls_context: ssl.SSLContext, *,
                 server_name: str = DEFAULT_SERVER_NAME, port: int = UPDATE_PORT,
                 trusted_keys=None, announcement: AnnouncementState | None = None,
                 log: Callable[[str], None] = lambda s: print(s, flush=True),
                 idle_timeout: float = IDLE_TIMEOUT, pause_poll: float = 0.5,
                 error_retry: float = ERROR_RETRY,
                 switch_quiet: float = SWITCH_QUIET_SECONDS,
                 send_command: Callable | None = None):
        self.layout = layout
        self.client_id = client_id
        self.tls_context = tls_context
        self.server_name = server_name
        self.port = port
        self.trusted_keys = trusted_keys
        self.announcement = announcement or AnnouncementState()
        self.log = log
        self.idle_timeout = idle_timeout
        self.pause_poll = pause_poll
        self.error_retry = error_retry
        self.switch_quiet = switch_quiet
        if send_command is None:
            from client.supervisor import send_command
        self.send_command = send_command
        self.ready_version: str | None = None
        self._downloading = False
        self._host: str | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_result: str | None = None

    @property
    def staging(self) -> Path:
        return self.layout.home / STAGING

    # -- νήμα ------------------------------------------------------------------------
    def trigger(self, host: str | None) -> None:
        if host:
            self._host = host
            self._wake.set()

    def start(self) -> "Updater":
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="client-updater")
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                return
            host = self._host
            try:
                self.last_result = self.run_once(host)
                if self.last_result == "ready" and self.ready_version:
                    self.request_switch(self.ready_version)
            except Retry as exc:
                self.last_result = f"retry: {exc}"
                self.log(f"[Ενημέρωση] {exc}· νέα προσπάθεια σε {exc.seconds:.0f}s.")
                if not self._stop.wait(exc.seconds):
                    self._wake.set()
            except (OSError, EOFError, ValueError) as exc:
                self.last_result = f"error: {exc}"
                if self._downloading:
                    # Μισή λήψη: συνέχιση από το offset χωρίς να περιμένουμε επανασύνδεση.
                    self.log(f"[Ενημέρωση] Η λήψη διακόπηκε: {exc}· συνέχιση σε "
                             f"{self.error_retry:.0f}s.")
                    if not self._stop.wait(self.error_retry):
                        self._wake.set()
                else:
                    # Ο Manager θα ξαναελεγχθεί στην επόμενη σύνδεση ή όταν το ζητήσει.
                    self.log(f"[Ενημέρωση] Ο έλεγχος δεν ολοκληρώθηκε: {exc}")
            finally:
                self._downloading = False

    # -- ένας έλεγχος ------------------------------------------------------------------
    def _connect(self, host: str):
        raw = socket.create_connection((host, self.port), timeout=CONNECT_TIMEOUT)
        try:
            raw.settimeout(self.idle_timeout)
            return self.tls_context.wrap_socket(raw, server_hostname=self.server_name)
        except BaseException:
            raw.close()
            raise

    def _report(self, stream, state: str, version: str | None, detail: str = "") -> None:
        send_message(stream, report_message(state, version, detail))
        reply = read_message(stream)
        if reply.get("type") != "ok":
            raise UpdateProtocolError("Ο Manager δεν επιβεβαίωσε την αναφορά")

    def _mark_failed(self, version: str) -> None:
        state = self.layout.read_current()
        if state is None or version in state["failed"]:
            return
        state["failed"] = (state["failed"] + [version])[-20:]
        atomic_write_json(self.layout.current_file, state)

    def installed_version(self, state: dict) -> str:
        """Η έκδοση που τρέχει: ο supervisor τη δίνει στο περιβάλλον (και στη δοκιμή)."""
        from client.supervisor import VERSION_ENV
        running = os.environ.get(VERSION_ENV)
        try:
            parse_version(running)
            return running
        except ValueError:
            return state["current"]

    def _send_result(self, stream) -> None:
        """5B2b: αποτέλεσμα εναλλαγής από τον supervisor (update-result.json) στον Manager."""
        from client.supervisor import RESULT_FILE
        path = self.layout.home / RESULT_FILE
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            version = result["version"]
            parse_version(version)
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError):
            path.unlink(missing_ok=True)
            return
        state = "installed" if result.get("ok") else "failed"
        self._report(stream, state, version, str(result.get("reason") or "")[:300])
        path.unlink(missing_ok=True)

    def request_switch(self, version: str) -> bool:
        """Ζητά από τον supervisor εναλλαγή όταν δεν παίζει ανακοίνωση εδώ και 10 s."""
        waited = False
        while not self._stop.is_set():
            if self.announcement.quiet_for() >= self.switch_quiet:
                break
            if not waited:
                self.log(f"[Ενημέρωση] Η εναλλαγή στην {version} περιμένει το τέλος της "
                         "ανακοίνωσης (+10 s).")
                waited = True
            time.sleep(self.pause_poll)
        if self._stop.is_set():
            return False
        try:
            answer = self.send_command(self.layout, "switch", version=version)
        except OSError as exc:
            self.log(f"[Ενημέρωση] Χωρίς supervisor δεν γίνεται εναλλαγή: {exc}")
            return False
        if not answer.get("ok"):
            self.log(f"[Ενημέρωση] Ο supervisor δεν έκανε εναλλαγή: {answer.get('error')}")
            return False
        self.log(f"[Ενημέρωση] Εναλλαγή στην έκδοση {version}.")
        return True

    def run_once(self, host: str) -> str:
        state = self.layout.read_current()
        if state is None:
            return "not-installed"
        installed = self.installed_version(state)
        self.ready_version = None
        with self._connect(host) as stream:
            send_message(stream, check_message(self.client_id, installed,
                                               updater=UPDATER_VERSION,
                                               protocol=CONNECTION_PROTOCOL,
                                               failed=state["failed"]))
            offer = parse_offer(read_message(stream))
            self._send_result(stream)
            if offer is None:
                return "none"
            version = offer["version"]
            if version in state["failed"] or \
                    parse_version(version) <= parse_version(installed):
                return "none"   # ο Manager δεν μπορεί να επιβάλει downgrade
            if (self.layout.version_dir(version) / "VERSION").is_file():
                self._report(stream, "ready", version)
                self.ready_version = version
                return "ready"
            self._downloading = True
            part = self._download(stream, offer)
            self._downloading = False
            return self._install(stream, part, offer, installed)

    def _staged(self, offer: dict) -> tuple[Path, int]:
        """Το .part συνεχίζεται μόνο αν είναι για το ίδιο ακριβώς πακέτο (SHA-256)."""
        self.staging.mkdir(parents=True, exist_ok=True)
        part = self.staging / f"{offer['version']}.part"
        meta = self.staging / f"{offer['version']}.json"
        try:
            known = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            known = None
        expected = {"sha256": offer["sha256"], "size": offer["size"]}
        if known != expected or not part.exists() or part.stat().st_size > offer["size"]:
            part.unlink(missing_ok=True)
            atomic_write_json(meta, expected)
        for other in self.staging.iterdir():   # παλιότερες μισές λήψεις
            if other.stem != offer["version"]:
                if other.is_dir():
                    shutil.rmtree(other, ignore_errors=True)
                else:
                    other.unlink(missing_ok=True)
        return part, part.stat().st_size if part.exists() else 0

    def _download(self, stream, offer: dict) -> Path:
        part, offset = self._staged(offer)
        size, version = offer["size"], offer["version"]
        if offset:
            self.log(f"[Ενημέρωση] Συνέχιση λήψης {version} από {offset * 100 // size}%")
        else:
            self.log(f"[Ενημέρωση] Λήψη έκδοσης {version} ({size / 1e6:.1f} MB)")
        with open(part, "ab") as sink:
            while offset < size:
                if self.announcement.active:
                    self._report(stream, "waiting", version, "ανακοίνωση σε εξέλιξη")
                    stream.close()
                    self.log("[Ενημέρωση] Παύση λήψης όσο παίζει ανακοίνωση.")
                    while self.announcement.active and not self._stop.is_set():
                        time.sleep(self.pause_poll)
                    raise Retry(0, f"Συνέχεια λήψης {version} μετά την ανακοίνωση")
                send_message(stream, {"type": "get", "version": version, "offset": offset})
                reply = read_message(stream)
                kind = reply.get("type")
                if kind == "busy":
                    retry = reply.get("retry")
                    seconds = retry if type(retry) is int and 1 <= retry <= 600 else 30
                    raise Retry(seconds, "Ο Manager στέλνει ήδη σε άλλους υπολογιστές")
                if kind == "error":
                    raise UpdateProtocolError(f"Manager: {reply.get('reason')}")
                length = reply.get("size")
                if kind != "data" or reply.get("offset") != offset or \
                        type(length) is not int or not 0 < length <= CHUNK_BYTES or \
                        offset + length > size:
                    raise UpdateProtocolError("Άκυρο κομμάτι ενημέρωσης")
                sink.write(read_exact(stream, length))
                sink.flush()
                offset += length
        return part

    def _install(self, stream, part: Path, offer: dict, installed: str) -> str:
        version = offer["version"]
        digest = hashlib.sha256()
        with open(part, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != offer["sha256"]:
            part.unlink(missing_ok=True)
            # Μπορεί να φταίει μόνο η μεταφορά: ξαναδοκιμάζεται από την αρχή στον επόμενο έλεγχο.
            self._report(stream, "failed", version, "Λάθος SHA-256 λήψης")
            return "failed"
        try:
            extract_package(part, self.layout.version_dir(version),
                            trusted_keys=self.trusted_keys, installed_version=installed,
                            updater_version=UPDATER_VERSION)
        except PackageError as exc:
            # Υπογραφή/περιεχόμενο: δεν ξανακατεβαίνει μέχρι να υπάρξει νεότερη έκδοση.
            self._mark_failed(version)
            self._cleanup(version)
            self.log(f"[Ενημέρωση] Απορρίφθηκε η έκδοση {version}: {exc}")
            self._report(stream, "failed", version, str(exc))
            return "failed"
        self._cleanup(version)
        self.log(f"[Ενημέρωση] Η έκδοση {version} επαληθεύτηκε και είναι έτοιμη.")
        self._report(stream, "ready", version)
        self.ready_version = version
        return "ready"

    def _cleanup(self, version: str) -> None:
        for suffix in (".part", ".json"):
            (self.staging / f"{version}{suffix}").unlink(missing_ok=True)


def peer_host(connection) -> str | None:
    """IP του Manager της σύνδεσης ήχου (εκεί είναι και η θύρα ενημερώσεων)."""
    getter = getattr(connection, "getpeername", None)
    try:
        return getter()[0] if getter else None
    except (OSError, IndexError, TypeError):
        return None

