"""Ελαφρύ Heartbeat Client → Director στο αντίστροφο μισό του TCP socket.

Δεν μεταβάλλει τα frames PCM που στέλνονται Director → Client.
Πειραματικό πρωτόκολλο χωρίς αυθεντικοποίηση.
"""

import socket
import threading
import time

HEARTBEAT = b"SIHB"
HEARTBEAT_INTERVAL_SECONDS = 2.0
HEARTBEAT_TIMEOUT_SECONDS = 6.0
# Director → Client: σε κάθε Client που δεν έλαβε τίποτα για τόσο χρόνο
# (στην πράξη στους μη επιλεγμένους, και στους επιλεγμένους αν σταματήσει το μικρόφωνο).
KEEPALIVE_INTERVAL_SECONDS = 0.5


def heartbeat_sender(connection: socket.socket, stop: threading.Event, *,
                     interval: float = HEARTBEAT_INTERVAL_SECONDS) -> None:
    """Εκπέμπει αμέσως και περιοδικά heartbeat· σε σφάλμα ξυπνά το PCM reader."""
    if interval <= 0:
        raise ValueError("Το διάστημα heartbeat πρέπει να είναι θετικό")
    while not stop.is_set():
        try:
            connection.sendall(HEARTBEAT)
        except OSError:
            if not stop.is_set():
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            return
        if stop.wait(interval):
            return


def heartbeat_receiver(connection: socket.socket, stop: threading.Event,
                       on_heartbeat, *, timeout: float = HEARTBEAT_TIMEOUT_SECONDS,
                       clock=time.monotonic) -> None:
    """Διαβάζει αποκλειστικά Client → Director, με timeout από το τελευταίο heartbeat.

    Συγκεντρώνει τμηματικές TCP αναγνώσεις. Η socket timeout 1 s του
    broadcaster δεν διαγράφει μερικώς ληφθέντα bytes.
    """
    if timeout <= 0:
        raise ValueError("Το όριο heartbeat πρέπει να είναι θετικό")
    latest = clock()
    pending = bytearray()
    while not stop.is_set():
        if clock() - latest >= timeout:
            raise TimeoutError("Δεν λήφθηκε heartbeat")
        try:
            piece = connection.recv(len(HEARTBEAT) - len(pending))
        except socket.timeout:
            continue
        if not piece:
            raise EOFError("Ο Client αποσυνδέθηκε")
        pending.extend(piece)
        if len(pending) == len(HEARTBEAT):
            if pending != HEARTBEAT:
                raise ValueError("Μη έγκυρο heartbeat")
            latest = clock()
            on_heartbeat(latest)
            pending.clear()
