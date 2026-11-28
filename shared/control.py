"""Client → Director στο αντίστροφο μισό του TCP/TLS socket: heartbeat και (v8) αιτήματα.

Δεν μεταβάλλει τα frames PCM που στέλνονται Director → Client.
Πρωτόκολλο σύνδεσης v8 (5C2): ανάμεσα στα heartbeat ο Client μπορεί να στείλει αίτημα
αλλαγής αίθουσας/ονόματος: ``SICR`` + μήκος (2 bytes) + JSON (shared.change_request).
"""

import queue
import socket
import struct
import threading
import time

HEARTBEAT = b"SIHB"
#: 5C2: αίτημα αλλαγής αίθουσας/ονόματος (μετρά και ως heartbeat).
CHANGE_REQUEST = b"SICR"
MAGIC_BYTES = 4
CHANGE_LENGTH = struct.Struct("!H")
HEARTBEAT_INTERVAL_SECONDS = 2.0
HEARTBEAT_TIMEOUT_SECONDS = 6.0
# Director → Client: σε κάθε Client που δεν έλαβε τίποτα για τόσο χρόνο
# (στην πράξη στους μη επιλεγμένους, και στους επιλεγμένους αν σταματήσει το μικρόφωνο).
KEEPALIVE_INTERVAL_SECONDS = 0.5


def encode_change_request(room: str, name: str) -> bytes:
    from shared.change_request import encode_request
    payload = encode_request(room, name)
    return CHANGE_REQUEST + CHANGE_LENGTH.pack(len(payload)) + payload


def heartbeat_sender(connection: socket.socket, stop: threading.Event, *,
                     interval: float = HEARTBEAT_INTERVAL_SECONDS,
                     outbox: "queue.Queue[bytes] | None" = None) -> None:
    """Εκπέμπει αμέσως και περιοδικά heartbeat· σε σφάλμα ξυπνά το PCM reader.

    ``outbox``: 5C2, έτοιμα μηνύματα (``encode_change_request``) που στέλνονται από
    αυτό το νήμα, ώστε να μη γράφουν δύο νήματα ταυτόχρονα στο ίδιο TLS socket.
    """
    if interval <= 0:
        raise ValueError("Το διάστημα heartbeat πρέπει να είναι θετικό")
    due = 0.0
    while not stop.is_set():
        now = time.monotonic()
        try:
            if now >= due:
                connection.sendall(HEARTBEAT)
                due = now + interval
            if outbox is None:
                if stop.wait(max(0.0, due - time.monotonic())):
                    return
                continue
            try:
                message = outbox.get(timeout=max(0.0, min(0.2, due - time.monotonic())))
            except queue.Empty:
                continue
            connection.sendall(message)
        except OSError:
            if not stop.is_set():
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            return


def heartbeat_receiver(connection: socket.socket, stop: threading.Event,
                       on_heartbeat, *, timeout: float = HEARTBEAT_TIMEOUT_SECONDS,
                       clock=time.monotonic, on_change_request=None) -> None:
    """Διαβάζει αποκλειστικά Client → Director, με timeout από το τελευταίο heartbeat.

    Συγκεντρώνει τμηματικές TCP αναγνώσεις. Η socket timeout 1 s του
    broadcaster δεν διαγράφει μερικώς ληφθέντα bytes.
    ``on_change_request(room, name)``: 5C2· χωρίς αυτό ένα αίτημα είναι σφάλμα.
    Άκυρο αίτημα (μορφή/τιμές) κλείνει τη σύνδεση όπως ένα άκυρο heartbeat.
    """
    if timeout <= 0:
        raise ValueError("Το όριο heartbeat πρέπει να είναι θετικό")
    from shared.change_request import MAX_PAYLOAD, decode_request
    latest = clock()
    pending = bytearray()
    wanted = MAGIC_BYTES
    while not stop.is_set():
        if clock() - latest >= timeout:
            raise TimeoutError("Δεν λήφθηκε heartbeat")
        try:
            piece = connection.recv(wanted - len(pending))
        except socket.timeout:
            continue
        if not piece:
            raise EOFError("Ο Client αποσυνδέθηκε")
        pending.extend(piece)
        if len(pending) < wanted:
            continue
        magic = bytes(pending[:MAGIC_BYTES])
        if magic == HEARTBEAT:
            latest = clock()
            on_heartbeat(latest)
            pending.clear()
            wanted = MAGIC_BYTES
        elif magic == CHANGE_REQUEST and on_change_request is not None:
            header = MAGIC_BYTES + CHANGE_LENGTH.size
            if wanted == MAGIC_BYTES:
                wanted = header
                continue
            if wanted == header:
                length = CHANGE_LENGTH.unpack(bytes(pending[MAGIC_BYTES:header]))[0]
                if not 1 <= length <= MAX_PAYLOAD:
                    raise ValueError("Μη έγκυρο αίτημα αλλαγής")
                wanted = header + length
                continue
            room, name = decode_request(bytes(pending[header:]))
            latest = clock()
            on_heartbeat(latest)
            on_change_request(room, name)
            pending.clear()
            wanted = MAGIC_BYTES
        else:
            raise ValueError("Μη έγκυρο heartbeat")
