"""Ενημερώσεις Client από τον Manager (Φάση 5B2, docs/phase5.md §5.2–5.3).

Φάκελος ``<δεδομένα Manager>/updates/``:
    updates-config.json   auto, current/previous πακέτο, «Έναρξη τώρα»
    updates-status.json   κατάσταση ανά Client (γράφει ο Manager που εξυπηρετεί)
    sch-intercom-client-<έκδοση>.sicu   το τρέχον και το προηγούμενο πακέτο· τα παλιότερα
                          σβήνονται (το προηγούμενο δεν διανέμεται, κρατιέται για ασφάλεια)

Τη ρύθμιση τη γράφει η γραμμή εντολών (``python -m director.updates``) και αργότερα το GUI·
ο Manager που τρέχει την ξαναδιαβάζει κάθε 2 s και ζητά από τους συνδεδεμένους Clients
να κάνουν CHECK όταν υπάρχει κάτι νέο. Διανέμει μόνο ο Κύριος που εξυπηρετεί.
"""
from __future__ import annotations

import argparse
from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import threading
import time
import zipfile

from shared.package import (FILES_PREFIX, PRODUCT, PackageError, read_signed_manifest,
                            validate_manifest, verify_package)
from shared.update_protocol import (CHUNK_BYTES, IDLE_TIMEOUT, MAX_DOWNLOADS, UPDATE_PORT,
                                    UpdateProtocolError, busy_retry, parse_check,
                                    parse_get_file, parse_get_manifest, parse_report,
                                    read_message, send_message)
from shared.version import parse_version

FOLDER = "updates"
CONFIG_FILE = "updates-config.json"
STATUS_FILE = "updates-status.json"
#: Ταυτόχρονες συνδέσεις στη θύρα ενημερώσεων (οι λήψεις είναι MAX_DOWNLOADS).
MAX_CONNECTIONS = 80
WATCH_SECONDS = 2.0
STATUS_WRITE_SECONDS = 2.0


class UpdateError(Exception):
    """Σφάλμα εισαγωγής ή ρύθμισης ενημερώσεων (μήνυμα για τον χρήστη)."""


def updates_dir(data_dir: Path) -> Path:
    return Path(data_dir) / FOLDER


def package_name(version: str) -> str:
    return f"{PRODUCT}-{version}.sicu"


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(temp, path)
            return
        except PermissionError:   # Windows: ο αναγνώστης κρατά το αρχείο μια στιγμή
            if attempt == 19:
                temp.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_config() -> dict:
    return {"auto": True, "current": None, "previous": None,
            "start": {"seq": 0, "version": None, "all": False, "clients": []}}


class UpdateStore:
    """Πακέτα και ρύθμιση στον δίσκο. Ασφαλές για νήματα· διαβάζει ξανά αν άλλαξε το αρχείο."""

    def __init__(self, folder: Path, *, trusted_keys=None):
        self.folder = Path(folder)
        self.trusted_keys = trusted_keys
        self._lock = threading.RLock()
        self._config = default_config()
        self._stamp = None

    @property
    def config_file(self) -> Path:
        return self.folder / CONFIG_FILE

    def _file_stamp(self):
        try:
            info = self.config_file.stat()
        except FileNotFoundError:
            return None
        return (info.st_mtime_ns, info.st_size)

    def config(self) -> dict:
        """Τρέχουσα ρύθμιση (αντίγραφο)· ξαναδιαβάζεται μόνο όταν άλλαξε το αρχείο."""
        with self._lock:
            stamp = self._file_stamp()
            if stamp != self._stamp:
                config = default_config()
                if stamp is not None:
                    try:
                        loaded = json.loads(self.config_file.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        loaded = None   # μισογραμμένο αρχείο: η επόμενη ανάγνωση θα το πιάσει
                        stamp = None
                    if isinstance(loaded, dict):
                        config.update({k: v for k, v in loaded.items() if k in config})
                self._config, self._stamp = config, stamp
            return json.loads(json.dumps(self._config))

    def _save(self, config: dict) -> None:
        _atomic_json(self.config_file, config)
        self._config, self._stamp = config, self._file_stamp()

    # -- πακέτα -------------------------------------------------------------
    def current(self) -> dict | None:
        return self.config()["current"]

    def package_path(self, version: str) -> Path:
        return self.folder / package_name(version)

    def package_missing(self, label: str = "current") -> bool:
        """Το πακέτο (current/previous) της ρύθμισης δεν υπάρχει στον δίσκο (π.χ. μετά από
        επαναφορά χωρίς τον φάκελο -updates)."""
        entry = self.config()[label]
        if entry is None:
            return False
        path = self.folder / entry["file"]
        try:
            return path.stat().st_size != entry["size"]
        except OSError:
            return True

    def inspect_package(self, source: Path) -> dict:
        """Για τον διάλογο εισαγωγής (5B3): πλήρης έλεγχος και τι αλλάζει σε σχέση με το
        τρέχον πακέτο. Δεν γράφει τίποτα. ``error`` αντί για εξαίρεση."""
        source = Path(source)
        info = {"path": source, "error": None}
        config = self.config()
        installed = config["current"]["version"] if config["current"] else None
        try:
            manifest = verify_package(source, trusted_keys=self.trusted_keys,
                                      installed_version=installed, updater_version=10**6)
        except PackageError as exc:
            info["error"] = str(exc)
            return info
        files = {item["path"]: item for item in manifest["files"]}
        info.update(version=manifest["version"], size=source.stat().st_size,
                    created_at=manifest["created_at"], files=len(files), changed=None,
                    changed_bytes=None, compared_with=None)
        if installed is not None and not self.package_missing():
            try:
                old = {item["path"]: item["sha256"] for item in
                       validate_manifest(json.loads(read_signed_manifest(
                           self.package_path(installed))[0].decode("utf-8")))["files"]}
            except (PackageError, ValueError, UnicodeDecodeError):
                old = None
            if old is not None:
                changed = [item for path, item in files.items() if old.get(path) != item["sha256"]]
                info.update(changed=len(changed),
                            changed_bytes=sum(item["size"] for item in changed),
                            compared_with=installed)
        return info

    def import_package(self, source: Path) -> dict:
        """Επαλήθευση (υπογραφή, hashes, νεότερη έκδοση) και αντιγραφή ως τρέχον πακέτο.
        Κρατιούνται το τρέχον και το προηγούμενο· τα άλλα .sicu σβήνονται (5B3)."""
        source = Path(source)
        with self._lock:
            config = self.config()
            installed = config["current"]["version"] if config["current"] else None
            try:
                manifest = verify_package(source, trusted_keys=self.trusted_keys,
                                          installed_version=installed, updater_version=10**6)
            except PackageError as exc:
                raise UpdateError(str(exc)) from exc
            if manifest["product"] != PRODUCT:
                raise UpdateError(f"Άλλο προϊόν: {manifest['product']}")
            version = manifest["version"]
            self.folder.mkdir(parents=True, exist_ok=True)
            target = self.package_path(version)
            partial = target.with_name(target.name + ".part")
            shutil.copyfile(source, partial)
            try:
                # Ξανά στο αντίγραφο: αυτό είναι που θα σταλεί στους Clients.
                verify_package(partial, trusted_keys=self.trusted_keys,
                               installed_version=installed, updater_version=10**6)
            except PackageError as exc:
                partial.unlink(missing_ok=True)
                raise UpdateError(f"Το αντίγραφο δεν επαληθεύτηκε: {exc}") from exc
            os.replace(partial, target)
            entry = {"version": version, "file": target.name, "size": target.stat().st_size,
                     "sha256": _sha256(target), "min_updater": manifest["min_updater"],
                     "connection_protocol": manifest["connection_protocol"],
                     "created_at": manifest["created_at"], "imported_at": int(time.time())}
            config["previous"], config["current"] = config["current"], entry
            config["start"] = {"seq": config["start"]["seq"], "version": None,
                               "all": False, "clients": []}
            self._save(config)
            self.remove_old_packages()
            return entry

    def remove_old_packages(self) -> None:
        """Σβήνει τα .sicu εκτός από το τρέχον και το προηγούμενο. Στα Windows ένα αρχείο που διαβάζεται
        ακόμη από μια λήψη δεν σβήνεται· θα σβηστεί στην επόμενη κλήση."""
        with self._lock:
            config = self.config()
            keep = {entry["file"] for entry in (config["current"], config["previous"]) if entry}
            if not keep:
                return      # χωρίς ρύθμιση (π.χ. παλιό backup) δεν σβήνεται τίποτα
            for other in self.folder.glob("*.sicu"):
                if other.name not in keep:
                    try:
                        other.unlink()
                    except OSError:
                        pass

    def set_auto(self, enabled: bool) -> dict:
        with self._lock:
            config = self.config()
            config["auto"] = bool(enabled)
            self._save(config)
            return config

    def start_now(self, clients: list[str] | None = None) -> dict:
        """«Έναρξη τώρα»: None = όλοι. Ισχύει για το τρέχον πακέτο (ακόμη και με auto off)."""
        with self._lock:
            config = self.config()
            if config["current"] is None:
                raise UpdateError("Δεν υπάρχει πακέτο ενημέρωσης. Κάντε πρώτα εισαγωγή.")
            start = config["start"]
            same = start.get("version") == config["current"]["version"]
            selected = set(start.get("clients", [])) if same else set()
            if clients:
                selected.update(clients)
            config["start"] = {"seq": int(start.get("seq", 0)) + 1,
                               "version": config["current"]["version"],
                               "all": clients is None or (same and bool(start.get("all"))),
                               "clients": sorted(selected)}
            self._save(config)
            return config

    def offer_for(self, check: dict) -> dict | None:
        """Τι προσφέρεται στον Client του CHECK (None = τίποτα)."""
        config = self.config()
        current = config["current"]
        if current is None:
            return None
        version = current["version"]
        if parse_version(version) <= parse_version(check["app"]):
            return None
        if version in check["failed"] or current["min_updater"] > check["updater"]:
            return None
        start = config["start"]
        allowed = config["auto"] or (start.get("version") == version and (
            start.get("all") or check["client_id"] in start.get("clients", [])))
        if not allowed:
            return None
        return current


class PackageIndex:
    """Το manifest ενός εισηγμένου πακέτου (5B2c): τι προσφέρεται και ποια αρχεία υπάρχουν.

    Ο Manager δεν ξαναελέγχει την υπογραφή εδώ (έγινε στην εισαγωγή)· την ελέγχει ο Client.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.manifest_data, self.signature = read_signed_manifest(self.path)
        try:
            manifest = validate_manifest(json.loads(self.manifest_data.decode("utf-8")))
        except (UnicodeDecodeError, ValueError) as exc:
            raise PackageError("Μη αναγνώσιμο manifest") from exc
        self.version = manifest["version"]
        self.files = {item["path"]: item for item in manifest["files"]}
        self.size = sum(item["size"] for item in manifest["files"])
        self.sha256 = hashlib.sha256(self.manifest_data).hexdigest()


class FileReader:
    """Ανάγνωση αρχείων από το .sicu μιας σύνδεσης· η σειριακή ανάγνωση συνεχίζει χωρίς
    να ξαναποσυμπιέζεται το αρχείο από την αρχή."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.archive = zipfile.ZipFile(path)
        self._name: str | None = None
        self._stream = None
        self._position = 0

    def read(self, name: str, offset: int, size: int) -> bytes:
        if self._name != name or self._position != offset:
            self._close_stream()
            self._stream = self.archive.open(FILES_PREFIX + name)
            if offset:
                self._stream.seek(offset)
            self._name, self._position = name, offset
        data = self._stream.read(size)
        self._position += len(data)
        return data

    def _close_stream(self) -> None:
        if self._stream is not None:
            self._stream.close()
        self._stream, self._name = None, None

    def close(self) -> None:
        self._close_stream()
        self.archive.close()


class UpdateStatus:
    """Κατάσταση ανά Client, στη μνήμη και στο updates-status.json (με όριο εγγραφών)."""

    def __init__(self, path: Path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.Lock()
        self._written = 0.0
        self._dirty = False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._clients = data.get("clients", {}) if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self._clients = {}

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._clients))

    def update(self, client_id: str, *, force: bool = False, **fields) -> None:
        with self._lock:
            entry = self._clients.setdefault(client_id, {})
            changed = any(entry.get(k) != v for k, v in fields.items())
            state_changed = "state" in fields and entry.get("state") != fields["state"]
            entry.update(fields)
            entry["updated_at"] = int(self.clock())
            self._dirty = self._dirty or changed
            if self._dirty and (force or state_changed or
                                time.monotonic() - self._written >= STATUS_WRITE_SECONDS):
                self._write()

    def flush(self) -> None:
        with self._lock:
            if self._dirty:
                self._write()

    def _write(self) -> None:
        try:
            _atomic_json(self.path, {"version": 1, "clients": self._clients})
            self._dirty = False
            self._written = time.monotonic()
        except OSError:
            pass   # η κατάσταση είναι ένδειξη· ξαναγράφεται στην επόμενη αλλαγή


class UpdateServer:
    """TLS θύρα 50560 (mTLS με το τρέχον context του μητρώου), όπως η θύρα εγγραφής.

    ``push(client_ids | None)``: ζητά από συνδεδεμένους Clients CHECK (μήνυμα στη
    σύνδεση ήχου). Το δίνει ο SocketServingService όταν ανοίγει τη θύρα.
    """

    def __init__(self, trust, store: UpdateStore, status: UpdateStatus, *,
                 host: str = "0.0.0.0", port: int = UPDATE_PORT,
                 log: Callable[[str], None] = lambda s: print(s, flush=True),
                 max_downloads: int = MAX_DOWNLOADS, idle_timeout: float = IDLE_TIMEOUT,
                 watch_seconds: float = WATCH_SECONDS):
        self.trust, self.store, self.status = trust, store, status
        self.host, self.port = host, port
        self.log = log
        self.idle_timeout = idle_timeout
        self.watch_seconds = watch_seconds
        self.max_downloads = max_downloads
        self._downloads = threading.BoundedSemaphore(max_downloads)
        self._connections = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._lock = threading.Lock()
        self._listener: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._push: Callable | None = None
        self.start_error: str | None = None
        self.active_downloads = 0
        self._indexes: dict[tuple[str, str], PackageIndex] = {}

    def index(self, entry: dict) -> PackageIndex:
        """Manifest του πακέτου της ρύθμισης (μία ανάγνωση ανά εισηγμένο πακέτο)."""
        key = (entry["version"], entry["sha256"])
        with self._lock:
            index = self._indexes.get(key)
        if index is None:
            index = PackageIndex(self.store.package_path(entry["version"]))
            with self._lock:
                self._indexes = {key: index}
        return index

    # -- άνοιγμα / κλείσιμο ---------------------------------------------------
    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._listener is not None

    def open(self, push: Callable | None = None) -> bool:
        """Αποτυχία (κατειλημμένη θύρα) δεν σταματά τον Manager· μόνο καταγράφεται."""
        with self._lock:
            if push is not None:
                self._push = push
            if self._listener is not None:
                return True
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((self.host, self.port))
                self.port = listener.getsockname()[1]
                listener.listen(MAX_CONNECTIONS)
                listener.settimeout(0.2)
            except OSError as exc:
                listener.close()
                self.start_error = exc.strerror or str(exc)
                self.log(f"[Ενημερώσεις] Η θύρα {self.port} δεν άνοιξε: {self.start_error}")
                return False
            self.start_error = None
            self._stop = threading.Event()
            self._listener = listener
            self._threads = [
                threading.Thread(target=self._serve, args=(listener, self._stop),
                                 daemon=True, name="updates-listener"),
                threading.Thread(target=self._watch, args=(self._stop,),
                                 daemon=True, name="updates-watch")]
            for thread in self._threads:
                thread.start()
        current = self.store.current()
        self.log(f"[Ενημερώσεις] Ενεργή TLS θύρα {self.port}"
                 + (f" (πακέτο {current['version']})" if current else " (χωρίς πακέτο)") + ".")
        return True

    def stop(self) -> None:
        with self._lock:
            listener, self._listener = self._listener, None
            threads, self._threads = self._threads, []
            self._stop.set()
            if listener is not None:
                listener.close()
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=1)
        self.status.flush()
        if listener is not None:
            self.log("[Ενημερώσεις] Η θύρα ενημερώσεων έκλεισε.")

    # -- παρακολούθηση ρύθμισης --------------------------------------------------
    def _watch(self, stop: threading.Event) -> None:
        def key(config):
            current = config["current"]["version"] if config["current"] else None
            return current, config["auto"], config["start"].get("seq")
        last = key(self.store.config())
        while not stop.wait(self.watch_seconds):
            config = self.store.config()
            now = key(config)
            if now == last:
                continue
            current, auto, seq = now
            if current is not None and self._push is not None:
                if seq != last[2]:
                    start = config["start"]
                    targets = None if start.get("all") else list(start.get("clients", []))
                    self.log("[Ενημερώσεις] Έναρξη τώρα: "
                             + ("όλοι" if targets is None else ", ".join(targets)))
                    self._push(targets)
                elif current != last[0] or (auto and not last[1]):
                    self.log(f"[Ενημερώσεις] Νέο πακέτο {current}: οι Clients ελέγχουν τώρα.")
                    self._push(None)
            last = now

    # -- συνδέσεις -------------------------------------------------------------------
    def _serve(self, listener: socket.socket, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                raw, address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self._connections.acquire(False):
                raw.close()
                continue
            threading.Thread(target=self._handle, args=(raw, address), daemon=True,
                             name="updates-connection").start()

    def _handle(self, raw: socket.socket, address) -> None:
        holding = False
        handle = None
        try:
            with raw:
                raw.settimeout(self.idle_timeout)
                with self.trust.context().wrap_socket(raw, server_side=True) as stream:
                    peer = stream.getpeercert(binary_form=True)
                    check = parse_check(read_message(stream))
                    client_id = check["client_id"]
                    registry = self.trust.registry
                    with registry.lock:
                        reason = registry.rejection_reason(peer, client_id, "")
                    if reason is not None:
                        self.log(f"[Ενημερώσεις] Απόρριψη {address[0]}: {reason}")
                        return
                    offer = self._answer_check(stream, check)
                    while not self._stop.is_set():
                        try:
                            message = read_message(stream)
                        except EOFError:
                            return
                        kind = message.get("type")
                        if kind == "check":
                            check = parse_check(message)
                            if check["client_id"] != client_id:
                                raise UpdateProtocolError("Άλλο client_id στην ίδια σύνδεση")
                            offer = self._answer_check(stream, check)
                        elif kind == "get-manifest":
                            request = parse_get_manifest(message)
                            if offer is None or request["version"] != offer.version:
                                send_message(stream, {"type": "error",
                                                      "reason": "Το πακέτο δεν διατίθεται"})
                                continue
                            send_message(stream, {"type": "manifest",
                                                  "size": len(offer.manifest_data),
                                                  "sig_size": len(offer.signature)},
                                         offer.manifest_data + offer.signature)
                        elif kind == "get-file":
                            request = parse_get_file(message)
                            item = offer.files.get(request["path"]) if offer else None
                            if item is None or request["version"] != offer.version or \
                                    request["offset"] >= item["size"]:
                                send_message(stream, {"type": "error",
                                                      "reason": "Το αρχείο δεν διατίθεται"})
                                continue
                            if not holding:
                                if not self._downloads.acquire(False):
                                    send_message(stream, {"type": "busy",
                                                          "retry": busy_retry()})
                                    self.status.update(client_id, state="available",
                                                       detail="αναμονή σειράς λήψης")
                                    continue
                                holding = True
                                with self._lock:
                                    self.active_downloads += 1
                            if handle is None or handle.path != offer.path:
                                if handle is not None:
                                    handle.close()
                                handle = FileReader(offer.path)
                            data = handle.read(request["path"], request["offset"],
                                               CHUNK_BYTES)
                            send_message(stream, {"type": "data", "path": request["path"],
                                                  "offset": request["offset"],
                                                  "size": len(data)}, data)
                            fields = {"state": "downloading", "available": offer.version}
                            if request["progress"] is not None:
                                fields["progress"] = request["progress"]
                            self.status.update(client_id, **fields)
                        elif kind == "report":
                            report = parse_report(message)
                            fields = {"state": report["state"], "detail": report["detail"]}
                            if report["progress"] is not None:
                                fields["progress"] = report["progress"]
                            if report["version"] is not None:
                                fields["available"] = report["version"]
                            self.status.update(client_id, force=True, **fields)
                            self.log(f"[Ενημερώσεις] {client_id}: {report['state']}"
                                     + (f" {report['version']}" if report["version"] else "")
                                     + (f" — {report['detail']}" if report["detail"] else ""))
                            send_message(stream, {"type": "ok"})
                        else:
                            raise UpdateProtocolError(f"Άγνωστο μήνυμα: {kind}")
        except (OSError, EOFError, ValueError) as exc:
            # Το ssl.SSLError είναι OSError· άγνωστος/ανακλημένος Client κόβεται στο mTLS.
            if not isinstance(exc, (socket.timeout, EOFError)):
                self.log(f"[Ενημερώσεις] Σύνδεση {address[0]}: {exc}")
        finally:
            if handle is not None:
                handle.close()
            if holding:
                with self._lock:
                    self.active_downloads -= 1
                self._downloads.release()
            self._connections.release()

    def _answer_check(self, stream, check: dict) -> PackageIndex | None:
        entry = self.store.offer_for(check)
        client_id = check["client_id"]
        current = self.store.current()
        fields = {"app": check["app"], "seen_at": int(time.time())}
        offer = None
        if entry is not None:
            try:
                offer = self.index(entry)
            except PackageError as exc:
                self.log(f"[Ενημερώσεις] Το πακέτο {entry['version']} δεν διαβάζεται: {exc}")
        if offer is not None:
            fields.update(state="available", available=offer.version, detail="")
            send_message(stream, {"type": "offer", "version": offer.version,
                                  "size": offer.size, "sha256": offer.sha256})
        else:
            if current is None or parse_version(check["app"]) >= parse_version(current["version"]):
                fields.update(state="current", available=None, detail="", progress=None)
            elif current["version"] in check["failed"]:
                fields.update(state="failed", available=current["version"])
            else:
                # Νεότερο πακέτο υπάρχει, αλλά δεν δόθηκε «Έναρξη τώρα» (ή παλιός updater).
                fields.update(available=current["version"])
                fields.setdefault("state", "available")
            send_message(stream, {"type": "none"})
        self.status.update(client_id, **fields)
        return offer


# -- γραμμή εντολών --------------------------------------------------------------------
def _describe(store: UpdateStore, status_path: Path) -> str:
    config = store.config()
    lines = [f"Αυτόματες ενημερώσεις: {'ενεργές' if config['auto'] else 'ανενεργές'}"]
    for label in ("current", "previous"):
        entry = config[label]
        name = "Τρέχον πακέτο" if label == "current" else "Προηγούμενο"
        lines.append(f"{name}: " + ("—" if entry is None else
                                     f"{entry['version']} ({entry['size'] / 1e6:.1f} MB)"
                                     + (" — λείπει το αρχείο"
                                        if store.package_missing(label) else "")))
    try:
        clients = json.loads(status_path.read_text(encoding="utf-8")).get("clients", {})
    except (OSError, ValueError):
        clients = {}
    for client_id, entry in sorted(clients.items()):
        when = time.strftime("%d/%m %H:%M", time.localtime(entry.get("updated_at", 0)))
        progress = f" {entry['progress']}%" if entry.get("state") == "downloading" else ""
        lines.append(f"{client_id} | έκδοση {entry.get('app', '?')} | "
                     f"{entry.get('state', '?')}{progress}"
                     + (f" → {entry['available']}" if entry.get("available") else "")
                     + (f" | {entry['detail']}" if entry.get("detail") else "")
                     + f" | {when}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ενημερώσεις Client από τον Manager")
    parser.add_argument("--registry-file", type=Path,
                        default=Path.home() / ".school-intercom" / "clients-registry.json",
                        help="Μητρώο Clients· οι ενημερώσεις είναι στον ίδιο φάκελο")
    # Μόνο για τις αυτόματες δοκιμές: κλειδί δοκιμής αντί του ενσωματωμένου.
    parser.add_argument("--trusted-key", action="append", default=None, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)
    imported = commands.add_parser("import", help="Εισαγωγή υπογεγραμμένου πακέτου .sicu")
    imported.add_argument("package", type=Path)
    commands.add_parser("status", help="Πακέτα και κατάσταση ανά υπολογιστή")
    auto = commands.add_parser("auto", help="Αυτόματες ενημερώσεις on/off")
    auto.add_argument("value", choices=("on", "off"))
    start = commands.add_parser("start", help="Έναρξη τώρα")
    group = start.add_mutually_exclusive_group(required=True)
    group.add_argument("--client", action="append", metavar="ID")
    group.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)
    folder = updates_dir(args.registry_file.parent)
    store = UpdateStore(folder, trusted_keys=args.trusted_key)
    try:
        if args.command == "import":
            entry = store.import_package(args.package)
            print(f"Εισήχθη η έκδοση {entry['version']} ({entry['size'] / 1e6:.1f} MB). "
                  "Ο Manager που εξυπηρετεί τη στέλνει στους Clients.")
        elif args.command == "auto":
            store.set_auto(args.value == "on")
            print(f"Αυτόματες ενημερώσεις: {'ενεργές' if args.value == 'on' else 'ανενεργές'}")
        elif args.command == "start":
            config = store.start_now(None if args.all else args.client)
            print(f"Έναρξη τώρα για την έκδοση {config['current']['version']}: "
                  + ("όλοι" if args.all else ", ".join(args.client)))
        else:
            print(_describe(store, folder / STATUS_FILE))
    except UpdateError as exc:
        print(f"Σφάλμα: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
