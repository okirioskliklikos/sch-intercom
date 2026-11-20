"""Διανομή μίας ροής PCM σε επιλεγμένες αίθουσες ή Clients.

Πρωτότυπο χωρίς αυθεντικοποίηση: οι δηλωμένες ταυτότητες ΔΕΝ είναι έμπιστες.
"""

import queue
import socket
import threading
from collections.abc import Callable

from shared.audio import BLOCK_BYTES, encode_frame
from shared.identity import validate_identity

MAX_CLIENTS = 32
MAX_CLIENT_PENDING_BLOCKS = 5
SEND_TIMEOUT_SECONDS = 1.0


class AudioBroadcaster:
    def __init__(self, *, max_clients: int = MAX_CLIENTS,
                 queue_blocks: int = MAX_CLIENT_PENDING_BLOCKS,
                 on_event: Callable[[str], None] | None = None):
        if max_clients < 1 or queue_blocks < 1:
            raise ValueError("max_clients και queue_blocks πρέπει να είναι θετικά")
        self.max_clients = max_clients
        self.queue_blocks = queue_blocks
        self.on_event = on_event or (lambda message: None)
        self._lock = threading.Lock()
        self._sessions: dict[str, _ClientSession] = {}
        self._selected_rooms: frozenset[str] | None = None
        self._selected_clients: frozenset[str] | None = None
        self._sequence = 0
        self._closed = False
        self.dropped = 0

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def clients_snapshot(self) -> list[tuple[str, str]]:
        with self._lock:
            return sorted((key, item.room) for key, item in self._sessions.items())

    def set_targets(self, *, rooms: set[str] | None = None,
                    client_ids: set[str] | None = None) -> None:
        """None/None σημαίνει όλους. Κενά σύνολα σημαίνουν κανέναν."""
        if rooms is not None:
            from shared.identity import ROOM_PATTERN
            if any(not isinstance(r, str) or not ROOM_PATTERN.fullmatch(r) or r.strip() != r for r in rooms):
                raise ValueError("Μη έγκυρη αίθουσα")
        if client_ids is not None:
            from uuid import UUID
            for client_id in client_ids:
                if not isinstance(client_id, str) or str(UUID(client_id)) != client_id:
                    raise ValueError("Μη έγκυρο αναγνωριστικό Client")
        with self._lock:
            self._selected_rooms = None if rooms is None else frozenset(rooms)
            self._selected_clients = None if client_ids is None else frozenset(client_ids)

    def add_client(self, connection: socket.socket, client_id: str, room: str,
                   address: object = "local") -> bool:
        """Αναλαμβάνει το socket. Διπλό UUID αντικαθιστά την προηγούμενη σύνδεση."""
        validate_identity(client_id, room)
        with self._lock:
            existing = self._sessions.get(client_id)
            if self._closed or (existing is None and len(self._sessions) >= self.max_clients):
                connection.close()
                return False
            session = _ClientSession(client_id, room, connection, address,
                                     self.queue_blocks, self._remove_client)
            self._sessions[client_id] = session
        if existing is not None:
            existing.stop()
        try:
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            connection.settimeout(SEND_TIMEOUT_SECONDS)
            session.start()
        except OSError:
            self._remove_client(session)
            return False
        self.on_event(f"[Director] {client_id} ({room}) συνδέθηκε — σύνολο {self.client_count}")
        return True

    def publish(self, pcm: bytes, timestamp_us: int) -> None:
        with self._lock:
            if self._closed:
                return
            seq = self._sequence
            self._sequence = (seq + 1) & 0xFFFFFFFF
            rooms, ids = self._selected_rooms, self._selected_clients
            sessions = tuple(self._sessions.values())
        voice = encode_frame(pcm, seq, timestamp_us)
        silence = encode_frame(bytes(BLOCK_BYTES), seq, timestamp_us)
        for session in sessions:
            selected = ((rooms is None and ids is None)
                        or (rooms is not None and session.room in rooms)
                        or (ids is not None and session.client_id in ids))
            # Συνεχής ροή σιωπής σε μη επιλεγμένους, ώστε το timeout λήψης
            # του Client να μη θεωρήσει άδικα ότι χάθηκε η σύνδεση.
            if session.enqueue(voice if selected else silence):
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

    def _remove_client(self, session: "_ClientSession") -> None:
        with self._lock:
            if self._sessions.get(session.client_id) is not session:
                return
            del self._sessions[session.client_id]
        session.stop()
        self.on_event(f"[Director] {session.client_id} αποσυνδέθηκε — σύνολο {self.client_count}")


class _ClientSession:
    def __init__(self, client_id: str, room: str, connection: socket.socket,
                 address: object, queue_blocks: int,
                 on_disconnect: Callable[["_ClientSession"], None]):
        self.client_id = client_id
        self.room = room
        self.connection = connection
        self.address = address
        self.pending: queue.Queue[bytes] = queue.Queue(maxsize=queue_blocks)
        self.on_disconnect = on_disconnect
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name=f"audio-client-{client_id[:8]}")

    def start(self) -> None:
        self.thread.start()

    def enqueue(self, frame: bytes) -> bool:
        if self.stopped.is_set():
            return False
        try:
            self.pending.put_nowait(frame)
            return False
        except queue.Full:
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
            self.on_disconnect(self)
