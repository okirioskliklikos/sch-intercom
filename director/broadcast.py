"""Διανομή μίας ροής PCM σε επιλεγμένες αίθουσες ή Clients.

Πρωτότυπο χωρίς αυθεντικοποίηση: οι δηλωμένες ταυτότητες ΔΕΝ είναι έμπιστες.
"""

import queue
import socket
import threading
import time
from collections.abc import Callable

from shared.audio import (encode_frame, encode_control, MESSAGE_KEEPALIVE,
                          MESSAGE_STREAM_STOP)
from shared.control import (HEARTBEAT_TIMEOUT_SECONDS, KEEPALIVE_INTERVAL_SECONDS,
                            heartbeat_receiver)
from shared.identity import validate_identity

#: Συνδεδεμένοι Clients ταυτόχρονα. Ροή ανά Client ~0,3 Mbps στο καλώδιο
#: (PCM 16 kHz mono + TLS/TCP/IP): 70 Clients ≈ 21 Mbps στη σύνδεση του Director.
#: Στο μέλλον μπορεί να χρειαστεί συμπίεση ήχου (π.χ. Opus)· βλ. docs/prototype.md.
MAX_CLIENTS = 70
MAX_CLIENT_PENDING_BLOCKS = 5
SEND_TIMEOUT_SECONDS = 1.0


class AudioBroadcaster:
    def __init__(self, *, max_clients: int = MAX_CLIENTS,
                 queue_blocks: int = MAX_CLIENT_PENDING_BLOCKS,
                 on_event: Callable[[str], None] | None = None,
                 heartbeat_timeout: float = HEARTBEAT_TIMEOUT_SECONDS):
        if heartbeat_timeout <= 0:
            raise ValueError("heartbeat_timeout πρέπει να είναι θετικό")
        self.heartbeat_timeout = heartbeat_timeout
        if max_clients < 1 or queue_blocks < 1:
            raise ValueError("max_clients και queue_blocks πρέπει να είναι θετικά")
        self.max_clients = max_clients
        self.queue_blocks = queue_blocks
        self.on_event = on_event or (lambda message: None)
        self._lock = threading.Lock()
        self._sessions: dict[str, _ClientSession] = {}
        self._offline: dict[str, tuple[str, float]] = {}
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

    def status_snapshot(self) -> list[tuple[str, str, bool, float]]:
        """UUID, αίθουσα, online/offline, δευτερόλεπτα από τελευταία επικοινωνία."""
        now = time.monotonic()
        with self._lock:
            result = [(key, item.room, True, max(0.0, now - item.last_heartbeat))
                      for key, item in self._sessions.items()]
            result.extend((key, room, False, max(0.0, now - last))
                          for key, (room, last) in self._offline.items()
                          if key not in self._sessions)
        return sorted(result)

    def _record_heartbeat(self, session: "_ClientSession", instant: float) -> None:
        with self._lock:
            if self._sessions.get(session.client_id) is session:
                session.last_heartbeat = instant

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
            for session in self._sessions.values():
                session.set_selected(self._is_selected(session))

    def _is_selected(self, session: "_ClientSession") -> bool:
        rooms, ids = self._selected_rooms, self._selected_clients
        return ((rooms is None and ids is None)
                or (rooms is not None and session.room in rooms)
                or (ids is not None and session.client_id in ids))

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
                                     self.queue_blocks, self._remove_client,
                                     self._record_heartbeat, self.heartbeat_timeout)
            session.selected = self._is_selected(session)
            self._sessions[client_id] = session
            self._offline.pop(client_id, None)
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
        """Μόνο επιλεγμένοι λαμβάνουν PCM· οι άλλοι λαμβάνουν keepalive από το sender."""
        with self._lock:
            if self._closed:
                return
            seq = self._sequence
            self._sequence = (seq + 1) & 0xFFFFFFFF
            sessions = tuple(self._sessions.values())
            # Μετάβαση και enqueue γίνονται με ένα lock: δεν περνά PCM μετά το STOP.
            voice = encode_frame(pcm, seq, timestamp_us)
            for session in sessions:
                session.set_selected(self._is_selected(session))
                if session.selected and session.enqueue(voice):
                    self.dropped += 1

    def disconnect_client(self, client_id: str) -> None:
        """Drop an authenticated Client immediately on registry revocation."""
        with self._lock:
            session = self._sessions.get(client_id)
        if session is not None:
            self._remove_client(session)

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
            self._offline[session.client_id] = (session.room, session.last_heartbeat)
            if len(self._offline) > 128:
                oldest = min(self._offline, key=lambda key: self._offline[key][1])
                del self._offline[oldest]
        session.stop()
        self.on_event(f"[Director] {session.client_id} αποσυνδέθηκε — σύνολο {self.client_count}")


class _ClientSession:
    def __init__(self, client_id: str, room: str, connection: socket.socket,
                 address: object, queue_blocks: int,
                 on_disconnect: Callable[["_ClientSession"], None],
                 on_heartbeat: Callable[["_ClientSession", float], None] | None = None,
                 heartbeat_timeout: float = HEARTBEAT_TIMEOUT_SECONDS):
        self.client_id = client_id
        self.room = room
        self.connection = connection
        self.address = address
        self.pending: queue.Queue[bytes] = queue.Queue(maxsize=queue_blocks)
        self.on_disconnect = on_disconnect
        self.on_heartbeat = on_heartbeat or (lambda session, at: None)
        self.heartbeat_timeout = heartbeat_timeout
        self.last_heartbeat = time.monotonic()
        self.selected = True
        self.bytes_sent = 0
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name=f"audio-client-{client_id[:8]}")
        self.reader = threading.Thread(target=self._read_heartbeat, daemon=True,
                                       name=f"heartbeat-client-{client_id[:8]}")

    def start(self) -> None:
        self.thread.start()
        self.reader.start()

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

    def set_selected(self, selected: bool) -> None:
        """Οριοθετεί μία ανακοίνωση στην ίδια FIFO ουρά με τα PCM."""
        if self.selected and not selected:
            self.enqueue(encode_control(MESSAGE_STREAM_STOP))
        self.selected = selected

    def stop(self) -> None:
        self.stopped.set()
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.connection.close()

    def join(self, *, timeout: float) -> None:
        for thread in (self.thread, self.reader):
            if thread.ident is not None and threading.current_thread() is not thread:
                thread.join(timeout)

    def _read_heartbeat(self) -> None:
        try:
            heartbeat_receiver(self.connection, self.stopped,
                               lambda at: self.on_heartbeat(self, at),
                               timeout=self.heartbeat_timeout)
        except (OSError, EOFError, ValueError, TimeoutError):
            pass
        finally:
            self.on_disconnect(self)

    def _run(self) -> None:
        # KEEPALIVE σε όποιον δεν έλαβε τίποτα για 0,5 s, επιλεγμένο ή όχι: αν
        # σταματήσει η λήψη μικροφώνου του Director, οι επιλεγμένοι Clients δεν
        # αποσυνδέονται άδικα στα 3 s ενώ το δίκτυο είναι υγιές.
        last_sent = time.monotonic()
        try:
            while not self.stopped.is_set():
                try:
                    frame = self.pending.get(timeout=0.1)
                except queue.Empty:
                    if time.monotonic() - last_sent < KEEPALIVE_INTERVAL_SECONDS:
                        continue
                    frame = encode_control(MESSAGE_KEEPALIVE)
                self.connection.sendall(frame)
                last_sent = time.monotonic()
                self.bytes_sent += len(frame)
        except OSError:
            pass
        finally:
            self.on_disconnect(self)
