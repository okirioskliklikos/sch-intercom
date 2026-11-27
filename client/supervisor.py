"""Supervisor του εγκατεστημένου Client (Φάση 5A, docs/phase5.md §4.3).

Ξεκινά στο Windows logon (HKCU\\...\\Run, με pythonw, χωρίς παράθυρο):

    pythonw -m client.supervisor --home %LOCALAPPDATA%\\sch-intercom

- Ένα αντίγραφο ανά εγκατάσταση (κλείδωμα αρχείου)· το δεύτερο τερματίζει με κωδικό 3.
- Τρέχει τον Client της έκδοσης του current.json και τον ξαναξεκινά αν πέσει
  (μετά από 2, 5, 10, 30 s· ξανά από την αρχή αν έτρεξε σταθερά ≥ 60 s).
- Η έξοδος του Client γράφεται στο logs\\client.log (περιστροφή 5 × 1 MB).
- Εντολές από τη γραμμή εντολών (client.ctl) μέσω τοπικού named pipe (AF_UNIX
  εκτός Windows) με μυστικό κλειδί στον φάκελο του προγράμματος.
- Στα Windows ο Client μπαίνει σε Job object: αν τερματιστεί ο supervisor
  (π.χ. kill), τερματίζεται και ο Client· δεν μένουν δύο Clients με ίδια ταυτότητα.

Φάση 5B2b (docs/phase5.md §5.4): εναλλαγή έκδοσης. Ο Client ζητά ``switch <έκδοση>``
όταν η νέα έκδοση είναι έτοιμη και δεν παίζει ανακοίνωση. Ο supervisor γράφει
``trial`` στο current.json και ξεκινά τη νέα. Αν αυτή στείλει ``trial-ok`` (αποδεκτό
WELCOME) μέσα σε 90 s: commit (current/previous, HKCU\\Run, .cmd, σβήσιμο παλιών).
Αλλιώς, ή αν πέσει, ή αν βρεθεί ``trial`` στην εκκίνηση (π.χ. κλείσιμο υπολογιστή):
rollback στην παλιά και η νέα μπαίνει στο ``failed``. Το αποτέλεσμα μένει στο
update-result.json μέχρι να το αναφέρει ο Client στον Manager. Ο ίδιος ο supervisor
συνεχίζει από την παλιά έκδοση ως την επόμενη σύνδεση στα Windows.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
from logging.handlers import RotatingFileHandler
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client as PipeClient, Listener
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time

from client.install_paths import Layout, atomic_write_json
from client.single_instance import AlreadyRunning, InstanceLock
from shared.version import parse_version

EXIT_ALREADY_RUNNING = 3
BACKOFF_SECONDS = (2, 5, 10, 30)
STABLE_AFTER_SECONDS = 60
LOG_BYTES = 1_000_000
LOG_BACKUPS = 5
CREATE_NO_WINDOW = 0x08000000
#: Η νέα έκδοση πρέπει να συνδεθεί (αποδεκτό WELCOME) μέσα σε τόσα s (§5.4.6).
TRIAL_SECONDS = 90
VERSION_ENV = "SCH_INTERCOM_VERSION"
TRIAL_ENV = "SCH_INTERCOM_TRIAL"
RESULT_FILE = "update-result.json"


# -- κανάλι εντολών -----------------------------------------------------------
def control_address(layout: Layout) -> tuple[str, str]:
    digest = hashlib.sha256(str(layout.home.resolve()).lower().encode("utf-8")).hexdigest()[:16]
    if os.name == "nt":
        return rf"\\.\pipe\sch-intercom-client-{digest}", "AF_PIPE"
    path = layout.home / "supervisor.sock"
    if len(str(path)) > 100:                      # όριο μήκους AF_UNIX
        path = Path(tempfile.gettempdir()) / f"sch-intercom-{digest}.sock"
    return str(path), "AF_UNIX"


def _new_key(layout: Layout) -> bytes:
    key = secrets.token_bytes(32)
    layout.home.mkdir(parents=True, exist_ok=True)
    temp = layout.key_file.with_suffix(".tmp")
    fd = os.open(temp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key)
    os.replace(temp, layout.key_file)
    return key


def send_command(layout: Layout, command: str, *, timeout: float = 5.0, **fields) -> dict:
    """Στέλνει εντολή στον supervisor. OSError αν δεν εκτελείται."""
    try:
        key = layout.key_file.read_bytes()
    except FileNotFoundError as exc:
        raise ConnectionRefusedError("Ο supervisor δεν εκτελείται") from exc
    address, family = control_address(layout)
    if family == "AF_UNIX" and not Path(address).exists():
        raise ConnectionRefusedError("Ο supervisor δεν εκτελείται")
    result: dict = {}
    error: list[BaseException] = []

    def talk():
        try:
            with PipeClient(address, family=family, authkey=key) as connection:
                connection.send({**fields, "command": command})
                if connection.poll(timeout):
                    result.update(connection.recv())
                else:
                    error.append(TimeoutError("Καμία απάντηση από τον supervisor"))
        except BaseException as exc:          # noqa: BLE001 — μεταφέρεται στον καλούντα
            error.append(exc)

    worker = threading.Thread(target=talk, daemon=True)
    worker.start()
    worker.join(timeout + 1)
    if worker.is_alive():
        raise TimeoutError("Καμία απάντηση από τον supervisor")
    if error:
        exc = error[0]
        if isinstance(exc, AuthenticationError):
            raise PermissionError("Λάθος κλειδί καναλιού εντολών") from exc
        if isinstance(exc, (OSError, EOFError)):
            raise ConnectionRefusedError(f"Ο supervisor δεν απαντά: {exc}") from exc
        raise exc
    return result


# -- Job object (Windows) -----------------------------------------------------
class _KillOnCloseJob:
    """Job object με KILL_ON_JOB_CLOSE: κλείνει ο supervisor → κλείνει και ο Client."""

    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                     ctypes.c_void_p, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel32 = kernel32
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.handle = handle

    def assign(self, process: subprocess.Popen) -> bool:
        if self.handle is None or not hasattr(process, "_handle"):
            return False
        return bool(self._kernel32.AssignProcessToJobObject(self.handle, int(process._handle)))


# -- supervisor ---------------------------------------------------------------
def _rotating_logger(name: str, path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"sch-intercom.{name}.{path}")
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(path, maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS,
                                      encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s",
                                               "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def default_spawn(command: list[str], env: dict, cwd: Path) -> subprocess.Popen:
    flags = CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env, cwd=str(cwd),
                            creationflags=flags)


class Supervisor:
    def __init__(self, layout: Layout, *, spawn=default_spawn, clock=time.monotonic,
                 backoff=BACKOFF_SECONDS, stable_after: float = STABLE_AFTER_SECONDS,
                 client_args: tuple[str, ...] = (), trial_seconds: float = TRIAL_SECONDS,
                 registry=None, uninstall=None):
        self.layout = layout
        self.trial_seconds = trial_seconds
        # Για τα tests: αντικατάσταση του HKCU\Run και της έκδοσης στις «Εφαρμογές»
        # (None = winreg στα Windows).
        self.registry = registry
        self.uninstall = uninstall
        self.trial: str | None = None
        self.trial_started = 0.0
        self._trial_ok = threading.Event()
        self._switch_to: str | None = None
        self.spawn = spawn
        self.clock = clock
        self.backoff = tuple(backoff)
        self.stable_after = stable_after
        self.client_args = tuple(client_args)
        self.log = _rotating_logger("supervisor", layout.logs / "supervisor.log")
        self.client_log = _rotating_logger("client", layout.logs / "client.log")
        self.child: subprocess.Popen | None = None
        self.child_started = 0.0
        self.version: str | None = None
        self.restarts = 0
        self.failures = 0
        self.last_exit: int | None = None
        self.next_start = 0.0
        self.started_at = time.time()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._restart = threading.Event()
        self._lock = threading.Lock()
        self._job = _KillOnCloseJob()
        self._listener: Listener | None = None
        self._server: threading.Thread | None = None

    # -- κατάσταση ------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            child = self.child
            return {"running": True, "supervisor_pid": os.getpid(),
                    "client_pid": child.pid if child is not None and child.poll() is None else None,
                    "version": self.version, "restarts": self.restarts,
                    "last_exit": self.last_exit,
                    "uptime": round(time.time() - self.started_at),
                    "client_uptime": (round(self.clock() - self.child_started)
                                      if child is not None and child.poll() is None else None),
                    "trial": self.trial,
                    "home": str(self.layout.home), "data": str(self.layout.data)}

    # -- Client -----------------------------------------------------------------
    def _start_child(self) -> None:
        if self.trial is not None:
            self.version = self.trial
        else:
            try:
                self.version = self.layout.current_version()
            except ValueError as exc:
                self.log.info(f"[supervisor] {exc}· εκκίνηση από τον κώδικα χωρίς έκδοση")
                self.version = None
        command = self.layout.module_command("client.main", *self.client_args,
                                             version=self.version)
        env = self.layout.child_env(self.version)
        env.pop(TRIAL_ENV, None)
        if self.version is not None:
            env[VERSION_ENV] = self.version
        if self.trial is not None:
            env[TRIAL_ENV] = self.trial
        try:
            child = self.spawn(command, env, self.layout.home)
        except OSError as exc:
            self.log.info(f"[supervisor] Ο Client δεν ξεκίνησε: {exc}")
            self._schedule_retry()
            return
        self._job.assign(child)
        with self._lock:
            self.child = child
            self.child_started = self.clock()
        self.log.info(f"[supervisor] Client v{self.version or '-'} ξεκίνησε (PID {child.pid})")
        if getattr(child, "stdout", None) is not None:
            threading.Thread(target=self._pump, args=(child,), name="client-log",
                             daemon=True).start()

    def _pump(self, child) -> None:
        for raw in iter(child.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line:
                self.client_log.info(line)
        child.stdout.close()

    def _schedule_retry(self) -> None:
        delay = self.backoff[min(self.failures, len(self.backoff) - 1)]
        self.failures += 1
        self.next_start = self.clock() + delay
        self.log.info(f"[supervisor] Νέα εκκίνηση σε {delay} s")

    def _stop_child(self) -> None:
        with self._lock:
            child, self.child = self.child, None
        if child is None or child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        self.last_exit = child.returncode
        self.log.info(f"[supervisor] Client σταμάτησε (PID {child.pid})")

    # -- εναλλαγή έκδοσης (5B2b) ---------------------------------------------------
    def _write_state(self, state: dict) -> None:
        atomic_write_json(self.layout.current_file, state)

    def _result(self, version: str, ok: bool, reason: str = "") -> None:
        try:
            atomic_write_json(self.layout.home / RESULT_FILE,
                              {"version": version, "ok": ok, "reason": reason,
                               "at": int(time.time())})
        except OSError as exc:
            self.log.info(f"[supervisor] Δεν γράφτηκε το {RESULT_FILE}: {exc}")

    def can_switch(self, version) -> str | None:
        """None αν επιτρέπεται η εναλλαγή στην ``version``· αλλιώς ο λόγος."""
        try:
            parse_version(version)
            state = self.layout.read_current()
        except ValueError as exc:
            return str(exc)
        if state is None:
            return "Δεν υπάρχει εγκατεστημένη έκδοση"
        if self.trial is not None or self._switch_to is not None:
            return "Εναλλαγή ήδη σε εξέλιξη"
        if version in state["failed"]:
            return f"Η έκδοση {version} απέτυχε ήδη"
        if parse_version(version) <= parse_version(state["current"]):
            return f"Η έκδοση {version} δεν είναι νεότερη από την {state['current']}"
        if not (self.layout.version_dir(version) / "VERSION").is_file():
            return f"Η έκδοση {version} δεν είναι έτοιμη"
        return None

    def recover_trial(self) -> None:
        """Εκκίνηση με trial στο current.json: η δοκιμή δεν ολοκληρώθηκε (κλείσιμο, kill)."""
        try:
            state = self.layout.read_current()
        except ValueError:
            return
        if state and state.get("trial"):
            version = state["trial"]
            self.log.info(f"[supervisor] Η δοκιμή της {version} δεν ολοκληρώθηκε· rollback")
            self._mark_failed(state, version,
                              "Η δοκιμή διακόπηκε (κλείσιμο υπολογιστή ή τερματισμός)")

    def _mark_failed(self, state: dict, version: str, reason: str) -> None:
        state["trial"] = None
        if version not in state["failed"]:
            state["failed"] = (state["failed"] + [version])[-20:]
        self._write_state(state)
        self._result(version, False, reason)
        remove_tree(self.layout.version_dir(version))

    def _begin_trial(self, version: str) -> None:
        reason = None
        self._switch_to = None
        state = self.layout.read_current()
        if version in state["failed"] or not (self.layout.version_dir(version) / "VERSION").is_file():
            reason = "μη διαθέσιμη"
        if reason is not None:
            self.log.info(f"[supervisor] Εναλλαγή στην {version} ακυρώθηκε: {reason}")
            return
        state["trial"] = version
        self._write_state(state)
        self.log.info(f"[supervisor] Δοκιμή έκδοσης {version} (από {state['current']}), "
                      f"έως {self.trial_seconds:.0f} s για σύνδεση")
        self._stop_child()
        self.trial = version
        self.trial_started = self.clock()
        self._trial_ok.clear()
        self.failures = 0
        self.next_start = 0.0
        self._start_child()

    def _rollback(self, reason: str) -> None:
        version, self.trial = self.trial, None
        self.log.info(f"[supervisor] Rollback από την {version}: {reason}")
        self._stop_child()
        state = self.layout.read_current()
        self._mark_failed(state, version, reason)
        self.failures = 0
        self.next_start = 0.0
        self._start_child()

    def _commit(self) -> None:
        version, self.trial = self.trial, None
        state = self.layout.read_current()
        old = state["current"]
        state.update(current=version, previous=old, trial=None)
        self._write_state(state)
        try:
            self.layout.set_install_version(version)
        except (OSError, ValueError) as exc:
            self.log.info(f"[supervisor] install.json: {exc}")
        self._rewrite_launchers(version)
        self._cleanup_versions(state)
        self._result(version, True)
        self.log.info(f"[supervisor] Η έκδοση {version} εγκαταστάθηκε (προηγούμενη {old})")

    def _rewrite_launchers(self, version: str) -> None:
        from client.ctl import CLI_TEMPLATE
        try:
            if self.layout.cli_file.exists():
                self.layout.cli_file.write_bytes(CLI_TEMPLATE.format(version=version)
                                                 .encode("ascii"))
        except OSError as exc:
            self.log.info(f"[supervisor] Δεν ξαναγράφτηκε το {self.layout.cli_file.name}: {exc}")
        command = run_command(self.layout, version)
        try:
            if self.registry is not None:
                self.registry(self.layout.run_name, command)
            elif os.name == "nt":
                set_run_value(self.layout.run_name, command)
        except OSError as exc:
            self.log.info(f"[supervisor] Δεν ενημερώθηκε το HKCU\\Run: {exc}")
        try:
            if self.uninstall is not None:
                self.uninstall(version)
            elif os.name == "nt":
                set_uninstall_version(version)
        except OSError as exc:
            self.log.info(f"[supervisor] Δεν ενημερώθηκε η έκδοση στις «Εφαρμογές»: {exc}")

    def _cleanup_versions(self, state: dict) -> None:
        """Μένουν μόνο current και previous (και η έκδοση από την οποία τρέχω)."""
        keep = {state["current"], state.get("previous")}
        running = Path(sys.executable).resolve()
        if not self.layout.versions.is_dir():
            return
        for folder in self.layout.versions.iterdir():
            if folder.name in keep or not folder.is_dir():
                continue
            if folder.resolve() in running.parents:
                continue   # ο supervisor τρέχει από εδώ· σβήνεται σε επόμενη εναλλαγή
            remove_tree(folder)
            if not folder.exists():
                self.log.info(f"[supervisor] Σβήστηκε η παλιά έκδοση {folder.name}")

    def step(self) -> None:
        """Ένας κύκλος του βρόχου (ξεχωριστό για τα tests)."""
        if self._switch_to is not None and self.trial is None:
            self._begin_trial(self._switch_to)
        if self.trial is not None:
            if self._trial_ok.is_set():
                self._commit()
            else:
                with self._lock:
                    child = self.child
                if child is not None and child.poll() is not None:
                    self.last_exit = child.returncode
                    with self._lock:
                        self.child = None
                    self._rollback(f"Η νέα έκδοση τερματίστηκε (κωδικός {child.returncode})")
                    return
                if self.clock() - self.trial_started >= self.trial_seconds:
                    self._rollback(f"Δεν συνδέθηκε μέσα σε {self.trial_seconds:.0f} s")
                    return
        if self._restart.is_set():
            self._restart.clear()
            self._stop_child()
            self.restarts += 1
            self.failures = 0
            self.next_start = 0.0
        with self._lock:
            child = self.child
        if child is not None and child.poll() is not None:
            ran = self.clock() - self.child_started
            self.last_exit = child.returncode
            with self._lock:
                self.child = None
            self.restarts += 1
            if ran >= self.stable_after:
                self.failures = 0
            self.log.info(f"[supervisor] Ο Client τερματίστηκε (κωδικός {child.returncode}, "
                          f"έτρεξε {ran:.0f} s)")
            self._schedule_retry()
            child = None
        if child is None and self.clock() >= self.next_start:
            self._start_child()

    # -- εντολές ----------------------------------------------------------------
    def handle(self, command: str, message: dict | None = None) -> dict:
        if command == "status":
            return self.status()
        if command == "restart":
            self._restart.set()
            self._wake.set()
            return {"ok": True}
        if command == "stop":
            self._stop.set()
            self._wake.set()
            return {"ok": True}
        if command == "switch":
            version = (message or {}).get("version")
            reason = self.can_switch(version)
            if reason is not None:
                return {"ok": False, "error": reason}
            self._switch_to = version
            self._wake.set()
            return {"ok": True}
        if command == "trial-ok":
            if self.trial is None or (message or {}).get("version") != self.trial:
                return {"ok": False, "error": "Καμία δοκιμή για αυτή την έκδοση"}
            self._trial_ok.set()
            self._wake.set()
            return {"ok": True}
        return {"ok": False, "error": f"Άγνωστη εντολή: {command}"}

    def _serve(self, listener: Listener) -> None:
        while not self._stop.is_set():
            try:
                connection = listener.accept()
            except Exception:                  # noqa: BLE001 — λάθος κλειδί, κλείσιμο κ.λπ.
                if self._stop.is_set():
                    return
                continue
            try:
                with connection:
                    if connection.poll(5):
                        message = connection.recv()
                        command = message.get("command") if isinstance(message, dict) else None
                        connection.send(self.handle(str(command), message))
            except (OSError, EOFError, ValueError):
                pass

    def _open_control(self) -> None:
        key = _new_key(self.layout)
        address, family = control_address(self.layout)
        if family == "AF_UNIX":
            try:
                os.unlink(address)
            except FileNotFoundError:
                pass
        self._listener = Listener(address, family=family, authkey=key)
        self._server = threading.Thread(target=self._serve, args=(self._listener,),
                                        name="control", daemon=True)
        self._server.start()

    def _close_control(self) -> None:
        address, family = control_address(self.layout)
        if self._listener is not None:
            def wake():                        # ξυπνά το accept() αν ακόμη περιμένει
                try:
                    key = self.layout.key_file.read_bytes()
                    PipeClient(address, family=family, authkey=key).close()
                except Exception:              # noqa: BLE001
                    pass
            if self._server is not None and self._server.is_alive():
                waker = threading.Thread(target=wake, daemon=True)
                waker.start()
                waker.join(2)
            try:
                self._listener.close()
            except OSError:
                pass
        for path in (self.layout.key_file,
                     Path(address) if family == "AF_UNIX" else None):
            if path is not None:
                try:
                    path.unlink()
                except OSError:
                    pass

    def close(self) -> None:
        """Κλείνει τα αρχεία log (στα Windows ένα ανοιχτό αρχείο δεν σβήνεται)."""
        for logger in (self.log, self.client_log):
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()

    def run(self, *, poll: float = 0.5) -> int:
        lock = InstanceLock(self.layout.lock_file)
        try:
            lock.acquire()
        except AlreadyRunning:
            self.log.info("[supervisor] Εκτελείται ήδη· έξοδος.")
            self.close()
            return EXIT_ALREADY_RUNNING
        try:
            self._open_control()
            self.log.info(f"[supervisor] Εκκίνηση (PID {os.getpid()}, φάκελος {self.layout.home})")
            self.recover_trial()
            while not self._stop.is_set():
                self.step()
                self._wake.wait(poll)
                self._wake.clear()
            self._stop_child()
            if self.trial is not None:
                # Τερματισμός μέσα στη δοκιμή: η επόμενη εκκίνηση κάνει rollback.
                self.log.info(f"[supervisor] Τερματισμός μέσα στη δοκιμή της {self.trial}")
            self.log.info("[supervisor] Τερματισμός")
            return 0
        finally:
            self._close_control()
            lock.release()
            self.close()


def remove_tree(path: Path, *, timeout: float = 5.0) -> bool:
    """Σβήσιμο φακέλου έκδοσης. Στα Windows τα αρχεία μιας διεργασίας που μόλις
    τερματίστηκε ελευθερώνονται λίγο αργότερα: νέες προσπάθειες έως ``timeout``."""
    import shutil
    deadline = time.monotonic() + timeout
    while True:
        shutil.rmtree(path, ignore_errors=True)
        if not Path(path).exists() or time.monotonic() >= deadline:
            return not Path(path).exists()
        time.sleep(0.25)


def run_command(layout: Layout, version: str) -> str:
    """Η τιμή του HKCU\\Run, ίδια με αυτήν που γράφει ο εγκαταστάτης."""
    home = str(layout.home)
    python = layout.version_dir(version) / "python" / "pythonw.exe"
    return f'"{python}" -m client.supervisor --home "{home}"'


def set_run_value(name: str, command: str) -> None:
    """Ενημερώνει μόνο υπάρχουσα τιμή (αν ο χρήστης έβγαλε το autostart, μένει έξω)."""
    import winreg
    path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                        winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
        try:
            winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            return
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)


#: Κλειδί απεγκατάστασης του Inno Setup (AppId του installer/sch-intercom-client.iss).
APP_ID = "{6B0E7C55-2F3D-4C41-9E0B-5C1D2A7E9F10}"
UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}_is1"


def set_uninstall_version(version: str) -> None:
    """5B2c: η έκδοση στις «Εφαρμογές» των Windows μετά από ενημέρωση (μόνο αν υπάρχει)."""
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0,
                             winreg.KEY_SET_VALUE)
    except FileNotFoundError:
        return   # χωρίς εγκαταστάτη (φορητό ή δοκιμή)
    with key:
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, version)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Supervisor του Classroom Client")
    parser.add_argument("--home", type=Path, default=None, help="Φάκελος προγράμματος")
    parser.add_argument("--data-dir", type=Path, default=None, help="Φάκελος δεδομένων")
    # Μόνο για τις αυτόματες δοκιμές 5B2b: συντομότερη δοκιμή και επιπλέον ορίσματα Client
    # (μετά από «--»), π.χ. θύρες δοκιμής και κλειδί υπογραφής δοκιμής.
    parser.add_argument("--trial-seconds", type=float, default=TRIAL_SECONDS,
                        help=argparse.SUPPRESS)
    argv = list(sys.argv[1:] if argv is None else argv)
    client_args: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, client_args = argv[:split], argv[split + 1:]
    args = parser.parse_args(argv)
    layout = Layout(args.home, args.data_dir)
    return Supervisor(layout, client_args=tuple(client_args),
                      trial_seconds=args.trial_seconds).run()


if __name__ == "__main__":
    sys.exit(main())
