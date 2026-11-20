"""Πρωτόκολλο ήχου για δοκιμές σε έμπιστο LAN (χωρίς ασφάλεια)."""

import socket
import struct

SAMPLE_RATE = 16_000
CHANNELS = 1
DTYPE = "int16"
BLOCK_MS = 20
BLOCK_SAMPLES = SAMPLE_RATE * BLOCK_MS // 1000
BLOCK_BYTES = BLOCK_SAMPLES * CHANNELS * 2
DEFAULT_PORT = 50555
MAX_FRAME_BYTES = 4096
_HEADER = struct.Struct("!I")


def read_exact(connection: socket.socket, count: int) -> bytes:
    """Διαβάζει ακριβώς count bytes ή εγείρει EOFError."""
    data = bytearray()
    while len(data) < count:
        piece = connection.recv(count - len(data))
        if not piece:
            raise EOFError("Η σύνδεση τερματίστηκε")
        data.extend(piece)
    return bytes(data)


def send_frame(connection: socket.socket, pcm: bytes) -> None:
    """Στέλνει ένα block PCM με πρόθεμα μήκους 4 bytes."""
    if len(pcm) != BLOCK_BYTES:
        raise ValueError(f"Αναμενόταν block {BLOCK_BYTES} bytes, βρέθηκαν {len(pcm)}")
    connection.sendall(_HEADER.pack(len(pcm)) + pcm)


def receive_frame(connection: socket.socket) -> bytes:
    """Λαμβάνει και ελέγχει ένα block PCM."""
    (length,) = _HEADER.unpack(read_exact(connection, _HEADER.size))
    if length != BLOCK_BYTES or length > MAX_FRAME_BYTES:
        raise ValueError(f"Μη έγκυρο μήκος block ήχου: {length}")
    return read_exact(connection, length)
