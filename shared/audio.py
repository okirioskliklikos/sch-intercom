"""Πρωτόκολλο ήχου για δοκιμές σε έμπιστο LAN (χωρίς ασφάλεια).

Κάθε πακέτο έχει επικεφαλίδα 20 bytes:
    magic "SI" (2) | έκδοση (1) | κενό (1) | μήκος PCM (4) | αύξων αριθμός (4) | χρονοσήμανση Director σε µs (8)
Η χρονοσήμανση προέρχεται από το μονοτονικό ρολόι του Director. Δεν είναι
συγχρονισμένη με τον Client· χρησιμεύει μόνο για σχετικές μετρήσεις καθυστέρησης.
"""

import socket
import struct
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
_HEADER = struct.Struct("!2sBxIIQ")
HEADER_BYTES = _HEADER.size


class Frame(NamedTuple):
    seq: int
    timestamp_us: int
    pcm: bytes


def read_exact(connection: socket.socket, count: int) -> bytes:
    """Διαβάζει ακριβώς count bytes ή εγείρει EOFError."""
    data = bytearray()
    while len(data) < count:
        piece = connection.recv(count - len(data))
        if not piece:
            raise EOFError("Η σύνδεση τερματίστηκε")
        data.extend(piece)
    return bytes(data)


def encode_frame(pcm: bytes, seq: int, timestamp_us: int) -> bytes:
    """Επιστρέφει επικεφαλίδα + PCM για ένα block."""
    if len(pcm) != BLOCK_BYTES:
        raise ValueError(f"Αναμενόταν block {BLOCK_BYTES} bytes, βρέθηκαν {len(pcm)}")
    header = _HEADER.pack(PROTOCOL_MAGIC, PROTOCOL_VERSION, len(pcm), seq & 0xFFFFFFFF, timestamp_us)
    return header + pcm


def send_frame(connection: socket.socket, pcm: bytes, seq: int = 0, timestamp_us: int = 0) -> None:
    """Στέλνει ένα block PCM με αύξοντα αριθμό και χρονοσήμανση."""
    connection.sendall(encode_frame(pcm, seq, timestamp_us))


def receive_frame(connection: socket.socket) -> Frame:
    """Λαμβάνει και ελέγχει ένα block PCM."""
    magic, version, length, seq, timestamp_us = _HEADER.unpack(read_exact(connection, HEADER_BYTES))
    if magic != PROTOCOL_MAGIC:
        raise ValueError("Άγνωστο πρωτόκολλο· Director και Client πρέπει να έχουν την ίδια έκδοση")
    if version != PROTOCOL_VERSION:
        raise ValueError(f"Μη υποστηριζόμενη έκδοση πρωτοκόλλου: {version}")
    if length != BLOCK_BYTES or length > MAX_FRAME_BYTES:
        raise ValueError(f"Μη έγκυρο μήκος block ήχου: {length}")
    return Frame(seq, timestamp_us, read_exact(connection, length))
