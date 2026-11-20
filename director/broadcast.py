"""Ανεξάρτητη διανομή των ίδιων blocks PCM σε πολλούς TCP Clients.

Πειραματικό εργαστηριακό πρωτόκολλο, χωρίς πιστοποίηση ή κρυπτογράφηση.
Κανένα νήμα αποστολής δεν επιτρέπεται να εμποδίσει τα υπόλοιπα.
"""

import queue
import socket
import threading
from collections.abc import Callable

from shared.audio import encode_frame

MAX_CLIENTS = 32
MAX_CLIENT_PENDING_BLOCKS = 5  # 100 ms στα 20 ms/block
SEND_TIMEOUT_SECONDS = 1.0


class AudioBroadcaster:
    """Διανέμει κάθε block σε όλους τους ενεργούς Clients χωρίς blocking I/O."""

    def __init__(self, *, max_clients: int = MAX_CLIENTS,
                 queue_blocks: int = MAX_CLIENT_PENDING_BLOCKS,
                 on_event: Callable[[str], None] | None = None):
        if max_clients < 1 or queue_blocks < 1:
            raise ValueError("max_clients και queue_blocks πρέπει να είναι θετικά")
        self.max_clients = max_clients
        self.queue_blocks = queue_blocks
        self.on_event = on_event or (lambda message: None)
        self._lock = threading.Lock()
        self._sessions: dict[int, _ClientSession] = {}
        self._next_client_id = 0
        self._sequence = 0
        self._closed = False
        self.dropped = 0

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def add_client(self, connection: socket.socket, address: object = "local") -> bool:
        """Αναλαμβάνει το socket (και το κλείνει αργότερα)."""
        with self._lock:
            if self._closed or len(self._sessions) >= self.max_clients:
                accepted = False
            else:
                self._next_client_id += 1
                client_id = self._next_client_id
                session = _ClientSession(client_id, connection, address, self.queue_blocks,
                                         self._remove_client)
                self._sessions[client_id] = session
                accepted = True
        if not accepted:
            connection.close()
            return False
        try:
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            connection.settimeout(SEND_TIMEOUT_SECONDS)
            session.start()
        except OSError:
            self._remove_client(client_id)
            return False
        self.on_event(f"[Director] Client #{client_id} {address} συνδέθηκε (σύνολο {self.client_count})")
        return True

    def publish(self, pcm: bytes, timestamp_us: int) -> None:
        """Εκτελείται από μία πηγή ήχου. Δεν κάνει network I/O."""
        with self._lock:
            if self._closed:
                return
            seq = self._sequence
            self._sequence = (seq + 1) & 0xFFFFFFFF
            sessions = tuple(self._sessions.values())
        frame = encode_frame(pcm, seq, timestamp_us)
        for session in sessions:
            if session.enqueue(frame):
                with self._lock:
                    self.dropped += 1

    def close(self) -> None:
        with self._lock:
            self._closed = True
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.stop()
        for session in sessions:
            session.join(timeout=2)

    def _remove_client(self, client_id: int) -> None:
        with self._lock:
            session = self._sessions.pop(client_id, None)
        if session is not None:
            session.stop()
            self.on_event(f"[Director] Client #{client_id} αποσυνδέθηκε (σύνολο {self.client_count})")


class _ClientSession:
    def __init__(self, client_id: int, connection: socket.socket, address: object,
                 queue_blocks: int, on_disconnect: Callable[[int], None]):
        self.client_id = client_id
        self.connection = connection
        self.address = address
        self.pending: queue.Queue[bytes] = queue.Queue(maxsize=queue_blocks)
        self.on_disconnect = on_disconnect
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name=f"audio-client-{client_id}")

    def start(self) -> None:
        self.thread.start()

    def enqueue(self, frame: bytes) -> bool:
        if self.stopped.is_set():
            return False
        try:
            self.pending.put_nowait(frame)
            return False
        except queue.Full:
            # Real-time προτεραιότητα: νεότερος ήχος αντί συσσώρευσης παλιών blocks.
            try:
                self.pending.get_nowait()
            except queue.Empty:
                pass
            try:
                self.pending.put_nowait(frame)
            except queue.Full:
                pass
            return True

    def stop(self) -> None:
        self.stopped.set()
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.connection.close()

    def join(self, *, timeout: float) -> None:
        if self.thread.ident is not None and threading.current_thread() is not self.thread:
            self.thread.join(timeout)

    def _run(self) -> None:
        try:
            while not self.stopped.is_set():
                try:
                    frame = self.pending.get(timeout=0.1)
                except queue.Empty:
                    continue
                self.connection.sendall(frame)
        except OSError:
            pass
        finally:
            self.on_disconnect(self.client_id)
