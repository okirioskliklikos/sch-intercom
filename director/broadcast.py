"""Διανομή μίας ροής PCM σε επιλεγμένες αίθουσες ή Clients.

Ο broadcaster δεν πιστοποιεί ο ίδιος ταυτότητες: ο Manager οφείλει
να ελέγξει το πιστοποιητικό πριν από την προσθήκη Client.
"""

import queue
import socket
import threading
import time
from collections.abc import Callable

from shared.audio import (encode_frame, encode_control, encode_client_settings, MESSAGE_KEEPALIVE,
                          MESSAGE_STREAM_STOP)
from shared.control import (HEARTBEAT_TIMEOUT_SECONDS, KEEPALIVE_INTERVAL_SECONDS,
                            heartbeat_receiver)
from shared.identity import validate_identity

#: Συνδεδεμένοι Clients ταυτόχρονα. Ροή ανά Client ~0,3 Mbps στο καλώδιο
#: (PCM 16 kHz mono + TLS/TCP/IP): 70 Clients ≈ 21 Mbps στη σύνδεση του Director.
#: Στο μέλλον μπορεί να χρειαστεί συμπίεση ήχου (π.χ. Opus)· βλ. docs/prototype.md.
MAX_CLIENTS = 70
MAX_CLIENT_PENDING_BLOCKS = 5
#: 4.3C1: ένα μήνυμα «γεμάτος» ανά Client ανά λεπτό· «περιμένει» όσο ξαναδοκιμάζει
#: (ο Client ξαναδοκιμάζει ανά 30 s όταν ο Manager είναι γεμάτος).
FULL_LOG_SECONDS = 60.0
FULL_WAITING_SECONDS = 70.0
#: 4.3C1: ίδια ταυτότητα από άλλη IP πάνω από τόσες φορές μέσα στο παράθυρο =
#: πιθανός κλωνοποιημένος υπολογιστής. Μία προειδοποίηση ανά 5 λεπτά ανά Client.
DUPLICATE_REPLACEMENTS = 3
DUPLICATE_WINDOW_SECONDS = 60.0
DUPLICATE_WARN_SECONDS = 300.0
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
        self._full: dict[str, tuple[str, str, float, float]] = {}   # id -> room, name, seen, logged
        self._replacements: dict[str, list[tuple[float, str, str]]] = {}
        self._duplicates: dict[str, tuple[str, tuple[str, ...], float]] = {}
        self.clock: Callable[[], float] = time.monotonic

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

    def has_room(self, client_id: str) -> bool:
        """Χωράει ο Client; Ίδιο UUID με ενεργή σύνδεση πάντα χωράει (αντικατάσταση)."""
        with self._lock:
            return (not self._closed and (client_id in self._sessions
                                          or len(self._sessions) < self.max_clients))

    def note_full(self, client_id: str, room: str, name: str = "") -> bool:
        """Καταγράφει Client που απορρίφθηκε λόγω ορίου· True αν πρέπει να γραφτεί log."""
        now = self.clock()
        with self._lock:
            previous = self._full.get(client_id)
            log = previous is None or now - previous[3] >= FULL_LOG_SECONDS
            self._full[client_id] = (room, name, now, now if log else previous[3])
            if len(self._full) > 256:
                del self._full[min(self._full, key=lambda key: self._full[key][2])]
        return log

    def full_waiting(self) -> list[tuple[str, str, str]]:
        """Clients που περιμένουν θέση (UUID, αίθουσα, όνομα), για status/GUI."""
        now = self.clock()
        with self._lock:
            self._full = {key: value for key, value in self._full.items()
                          if now - value[2] < FULL_WAITING_SECONDS}
            return sorted((key, value[0], value[1]) for key, value in self._full.items())

    def duplicate_identities(self) -> list[tuple[str, str, tuple[str, ...]]]:
        """Ταυτότητες που χρησιμοποιούν δύο υπολογιστές (UUID, αίθουσα, IP), για status/GUI."""
        now = self.clock()
        with self._lock:
            self._duplicates = {key: value for key, value in self._duplicates.items()
                                if now - value[2] < DUPLICATE_WARN_SECONDS}
            return sorted((key, value[0], value[1]) for key, value in self._duplicates.items())

    def client_names(self) -> dict[str, str]:
        """Όνομα υπολογιστή ανά UUID για τους συνδεδεμένους (για τις ειδοποιήσεις GUI)."""
        with self._lock:
            return {key: item.name for key, item in self._sessions.items() if item.name}

    def _note_replacement(self, client_id: str, room: str, old_ip: str,
                          new_ip: str) -> str | None:
        """Μέσα στο lock. Επιστρέφει προειδοποίηση όταν δύο IP «κλέβουν» τη σύνδεση."""
        now = self.clock()
        history = [item for item in self._replacements.get(client_id, [])
                   if now - item[0] < DUPLICATE_WINDOW_SECONDS]
        history.append((now, old_ip, new_ip))
        self._replacements[client_id] = history
        if len(self._replacements) > 256:
            del self._replacements[min(self._replacements,
                                       key=lambda key: self._replacements[key][-1][0])]
        if len(history) <= DUPLICATE_REPLACEMENTS:
            return None
        addresses = tuple(sorted({ip for _, a, b in history for ip in (a, b)}))
        previous = self._duplicates.get(client_id)
        self._duplicates[client_id] = (room, addresses, now)
        if previous is not None and now - previous[2] < DUPLICATE_WARN_SECONDS:
            self._duplicates[client_id] = (room, addresses, previous[2])
            return None
        return (f"[Director] ΠΡΟΣΟΧΗ: η ταυτότητα {client_id} ({room}) χρησιμοποιείται "
                f"από δύο υπολογιστές ({', '.join(addresses)})· πιθανώς κλωνοποιημένος "
                f"δίσκος. Στον έναν: client --new-identity και νέα έγκριση.")

    def add_client(self, connection: socket.socket, client_id: str, room: str,
                   address: object = "local", *, name: str | None = None) -> bool:
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
            if name is not None:
                session.name = name
                session.enqueue(encode_client_settings(room, name))
            self._sessions[client_id] = session
            self._offline.pop(client_id, None)
            self._full.pop(client_id, None)
            warning = None
            if existing is not None:
                old_ip, new_ip = _ip(existing.address), _ip(address)
                if old_ip is not None and new_ip is not None and old_ip != new_ip:
                    warning = self._note_replacement(client_id, room, old_ip, new_ip)
        if warning is not None:
            self.on_event(warning)
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

    def publish(self, pcm: bytes, timestamp_us: int) -> tuple[int, int] | None:
        """Μόνο επιλεγμένοι λαμβάνουν PCM· οι άλλοι λαμβάνουν keepalive από το sender.

        Επιστρέφει (αύξων αριθμός, πλήθος επιλεγμένων Clients) για τις μετρήσεις 4.4.
        """
        with self._lock:
            if self._closed:
                return None
            seq = self._sequence
            self._sequence = (seq + 1) & 0xFFFFFFFF
            sessions = tuple(self._sessions.values())
            # Μετάβαση και enqueue γίνονται με ένα lock: δεν περνά PCM μετά το STOP.
            voice = encode_frame(pcm, seq, timestamp_us)
            selected = 0
            for session in sessions:
                session.set_selected(self._is_selected(session))
                if session.selected:
                    selected += 1
                    if session.enqueue(voice):
                        self.dropped += 1
        return seq, selected

    def update_client_info(self, client_id: str, room: str, name: str) -> None:
        """Apply Manager-owned room immediately and queue Client metadata update."""
        frame = encode_client_settings(room, name)
        with self._lock:
            session = self._sessions.get(client_id)
            if session is None:
                return
            # FIFO preserves settings relative to PCM; routing changes now.
            session.room = room
            session.name = name
            session.set_selected(self._is_selected(session))
            session.enqueue(frame)

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


def _ip(address: object) -> str | None:
    if isinstance(address, tuple) and address and isinstance(address[0], str):
        return address[0]
    return None


class _ClientSession:
    def __init__(self, client_id: str, room: str, connection: socket.socket,
                 address: object, queue_blocks: int,
                 on_disconnect: Callable[["_ClientSession"], None],
                 on_heartbeat: Callable[["_ClientSession", float], None] | None = None,
                 heartbeat_timeout: float = HEARTBEAT_TIMEOUT_SECONDS):
        self.client_id = client_id
        self.room = room
        self.name = ""
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
