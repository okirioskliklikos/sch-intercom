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

from client.install_paths import Layout
from client.single_instance import AlreadyRunning, InstanceLock

EXIT_ALREADY_RUNNING = 3
BACKOFF_SECONDS = (2, 5, 10, 30)
STABLE_AFTER_SECONDS = 60
LOG_BYTES = 1_000_000
LOG_BACKUPS = 5
CREATE_NO_WINDOW = 0x08000000


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


def send_command(layout: Layout, command: str, *, timeout: float = 5.0) -> dict:
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
                connection.send({"command": command})
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
                 client_args: tuple[str, ...] = ()):
        self.layout = layout
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
                    "home": str(self.layout.home), "data": str(self.layout.data)}

    # -- Client -----------------------------------------------------------------
    def _start_child(self) -> None:
        try:
            self.version = self.layout.current_version()
        except ValueError as exc:
            self.log.info(f"[supervisor] {exc}· εκκίνηση από τον κώδικα χωρίς έκδοση")
            self.version = None
        command = self.layout.module_command("client.main", *self.client_args,
                                             version=self.version)
        env = self.layout.child_env(self.version)
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

    def step(self) -> None:
        """Ένας κύκλος του βρόχου (ξεχωριστό για τα tests)."""
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
    def handle(self, command: str) -> dict:
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
                        connection.send(self.handle(str(command)))
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

    def run(self, *, poll: float = 0.5) -> int:
        lock = InstanceLock(self.layout.lock_file)
        try:
            lock.acquire()
        except AlreadyRunning:
            self.log.info("[supervisor] Εκτελείται ήδη· έξοδος.")
            return EXIT_ALREADY_RUNNING
        try:
            self._open_control()
            self.log.info(f"[supervisor] Εκκίνηση (PID {os.getpid()}, φάκελος {self.layout.home})")
            while not self._stop.is_set():
                self.step()
                self._wake.wait(poll)
                self._wake.clear()
            self._stop_child()
            self.log.info("[supervisor] Τερματισμός")
            return 0
        finally:
            self._close_control()
            lock.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Supervisor του Classroom Client")
    parser.add_argument("--home", type=Path, default=None, help="Φάκελος προγράμματος")
    parser.add_argument("--data-dir", type=Path, default=None, help="Φάκελος δεδομένων")
    args = parser.parse_args(argv)
    layout = Layout(args.home, args.data_dir)
    return Supervisor(layout).run()


if __name__ == "__main__":
    sys.exit(main())
