"""Ψεύτικος Manager για τα tests εντοπισμού: απαντά σε κάθε ερώτηση με δοσμένα πακέτα.

Από 28/11/2026 ο Client δεν ακούει σε σταθερή θύρα· στέλνει ερώτηση και περιμένει
απάντηση (βλ. `shared/discovery.py`). Τα tests που πριν έστελναν beacons στη θύρα του
Client τώρα στήνουν έναν τέτοιο responder στη θύρα discovery.
"""
from __future__ import annotations

import socket
import threading

from shared.discovery import MAX_BEACON_BYTES, is_query


class FakeResponder:
    """Δένεται αμέσως στη θύρα· ``join()`` (όπως ένα thread) τον σταματά."""

    def __init__(self, port: int, packets: list[bytes]):
        self.packets = list(packets)
        self.queries = 0
        #: IP από την οποία ήρθε η ερώτηση (στο ίδιο μηχάνημα: η IP της κάρτας, όχι 127.0.0.1).
        self.client_ip = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        self.sock.bind(("", port))
        self.sock.settimeout(0.05)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                data, sender = self.sock.recvfrom(MAX_BEACON_BYTES + 1)
            except socket.timeout:
                continue
            except OSError:
                if self.stop.is_set():
                    return
                continue
            if not is_query(data):
                continue
            self.queries += 1
            self.client_ip = sender[0]
            for packet in self.packets:
                try:
                    self.sock.sendto(packet, sender)
                except OSError:
                    pass

    def join(self, timeout=None):
        self.stop.set()
        self.thread.join(timeout)
        self.sock.close()
