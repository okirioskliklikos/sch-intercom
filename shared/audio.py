"""Πρωτόκολλο PCM και ελέγχου Director → Client, μόνο για έμπιστο LAN.

20-byte header: magic SI (2) | audio version (1) | type (1) |
length (4) | sequence (4) | Director monotonic timestamp µs (8).
Type 0 = PCM (640 bytes), 1 = KEEPALIVE, 2 = STREAM_STOP (χωρίς payload).
Η έκδοση σύνδεσης ελέγχεται χωριστά στο shared.identity.
"""

import select
from array import array
import socket
import struct
import time
from typing import NamedTuple

SAMPLE_RATE = 16_000
CHANNELS = 1
DTYPE = "int16"
BLOCK_MS = 20
BLOCK_SAMPLES = SAMPLE_RATE * BLOCK_MS // 1000
BLOCK_BYTES = BLOCK_SAMPLES * CHANNELS * 2
DEFAULT_PORT = 50555
MAX_FRAME_BYTES = 4096
PROTOCOL_MAGIC = b"SI"
PROTOCOL_VERSION = 1
MESSAGE_PCM = 0
MESSAGE_KEEPALIVE = 1
MESSAGE_STREAM_STOP = 2
_HEADER = struct.Struct("!2sBBIIQ")
HEADER_BYTES = _HEADER.size


class Frame(NamedTuple):
    seq: int
    timestamp_us: int
    pcm: bytes


class ControlMessage(NamedTuple):
    kind: int


def _buffered(connection) -> bool:
    """Με TLS, δεδομένα μπορεί να περιμένουν ήδη αποκρυπτογραφημένα στο SSL
    buffer ενώ το socket δεν είναι readable· τότε δεν περιμένουμε στο select."""
    pending = getattr(connection, "pending", None)
    return pending is not None and pending() > 0


def read_exact(connection: socket.socket, count: int, *,
               deadline: float | None = None) -> bytes:
    """Διαβάζει ακριβώς count bytes ή εγείρει EOFError/timeout.

    Με deadline γίνεται select() πριν από κάθε recv, χωρίς αλλαγή του socket
    timeout (χρησιμοποιείται και από το ανεξάρτητο νήμα heartbeat).
    Τμηματικά/άκυρα μηνύματα δεν ανανεώνουν τεχνητά το timeout.
    """
    data = bytearray()
    while len(data) < count:
        if deadline is not None and not _buffered(connection):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([connection], [], [], remaining)[0]:
                raise socket.timeout("Δεν ελήφθη πλήρες έγκυρο μήνυμα")
        piece = connection.recv(count - len(data))
        if not piece:
            raise EOFError("Η σύνδεση τερματίστηκε")
        data.extend(piece)
    return bytes(data)


def encode_frame(pcm: bytes, seq: int, timestamp_us: int) -> bytes:
    """Επιστρέφει επικεφαλίδα + PCM για ένα block."""
    if len(pcm) != BLOCK_BYTES:
        raise ValueError(f"Αναμενόταν block {BLOCK_BYTES} bytes, βρέθηκαν {len(pcm)}")
    header = _HEADER.pack(PROTOCOL_MAGIC, PROTOCOL_VERSION, MESSAGE_PCM,
                          len(pcm), seq & 0xFFFFFFFF, timestamp_us)
    return header + pcm


def encode_control(kind: int) -> bytes:
    """Μόνο τα δύο γνωστά μηνύματα ελέγχου, χωρίς payload."""
    if kind not in (MESSAGE_KEEPALIVE, MESSAGE_STREAM_STOP):
        raise ValueError("Άγνωστος τύπος μηνύματος ελέγχου")
    return _HEADER.pack(PROTOCOL_MAGIC, PROTOCOL_VERSION, kind, 0, 0, 0)


def send_frame(connection: socket.socket, pcm: bytes, seq: int = 0,
               timestamp_us: int = 0) -> None:
    connection.sendall(encode_frame(pcm, seq, timestamp_us))


def receive_message(connection: socket.socket, *,
                    deadline: float | None = None) -> Frame | ControlMessage:
    """Ένα πλήρες PCM, KEEPALIVE ή STREAM_STOP. Άκυρο μήνυμα απορρίπτεται."""
    magic, version, kind, length, seq, timestamp_us = _HEADER.unpack(
        read_exact(connection, HEADER_BYTES, deadline=deadline))
    if magic != PROTOCOL_MAGIC:
        raise ValueError("Άγνωστο πρωτόκολλο· Director και Client πρέπει να έχουν την ίδια έκδοση")
    if version != PROTOCOL_VERSION:
        raise ValueError(f"Μη υποστηριζόμενη έκδοση πρωτοκόλλου: {version}")
    if kind == MESSAGE_PCM:
        if length != BLOCK_BYTES or length > MAX_FRAME_BYTES:
            raise ValueError(f"Μη έγκυρο μήκος block ήχου: {length}")
        return Frame(seq, timestamp_us, read_exact(connection, length, deadline=deadline))
    if kind in (MESSAGE_KEEPALIVE, MESSAGE_STREAM_STOP):
        if length != 0 or seq != 0 or timestamp_us != 0:
            raise ValueError("Μη έγκυρο μήνυμα ελέγχου")
        return ControlMessage(kind)
    raise ValueError(f"Άγνωστος τύπος μηνύματος: {kind}")


def receive_frame(connection: socket.socket) -> Frame:
    """Συμβατό helper για δοκιμές καθαρής ροής PCM."""
    message = receive_message(connection)
    if not isinstance(message, Frame):
        raise ValueError("Αναμενόταν PCM frame")
    return message


def apply_gain_ramp(pcm: bytes, start_gain: float, end_gain: float) -> bytes:
    """Γραμμική αλλαγή έντασης σε block int16 (fade-in/fade-out χωρίς «κλικ»).

    Το πρώτο δείγμα παίρνει start_gain και το τελευταίο end_gain. Με σταθερό
    gain 1.0 επιστρέφεται το ίδιο block χωρίς αντιγραφή.
    """
    if start_gain >= 1.0 and end_gain >= 1.0:
        return pcm
    samples = array("h")
    samples.frombytes(pcm)
    count = len(samples)
    if count == 0:
        return pcm
    step = (end_gain - start_gain) / (count - 1) if count > 1 else 0.0
    for i in range(count):
        gain = start_gain + step * i
        samples[i] = int(samples[i] * max(0.0, min(1.0, gain)))
    return samples.tobytes()
