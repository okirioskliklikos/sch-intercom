"""Επανασύνδεση Client όταν ο Director σωπαίνει χωρίς να κλείσει το TCP.

Πραγματικά loopback sockets, χωρίς συσκευές ήχου.
"""

import socket
import threading
import time
import unittest

from client.main import RECEIVE_TIMEOUT_SECONDS, pump_frames, run_with_reconnect
from shared.audio import BLOCK_BYTES, BLOCK_MS, encode_frame
from shared.jitter import DelayTracker, JitterBuffer


def pcm(n):
    return bytes([n % 256]) * BLOCK_BYTES


class SilentDirector:
    """Δέχεται συνδέσεις· στην πρώτη στέλνει λίγα blocks και μετά σωπαίνει
    κρατώντας το socket ανοιχτό (όπως Director που έχασε ρεύμα/Wi-Fi)."""

    def __init__(self, frames_before_silence=5):
        self.frames_before_silence = frames_before_silence
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.server.settimeout(5)
        self.connections = []
        self.second_connected = threading.Event()
        self.first_closed_by_client = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    @property
    def address(self):
        return self.server.getsockname()

    def _run(self):
        first, _ = self.server.accept()
        self.connections.append(first)
        for seq in range(self.frames_before_silence):
            first.sendall(encode_frame(pcm(seq), seq, seq * BLOCK_MS * 1000))
            time.sleep(BLOCK_MS / 1000)
        # Σιωπή χωρίς close/shutdown.
        second, _ = self.server.accept()
        self.connections.append(second)
        self.second_connected.set()
        # Ο Client πρέπει να έχει κλείσει την παλιά σύνδεση: recv επιστρέφει b"".
        first.settimeout(2)
        try:
            if first.recv(1) == b"":
                self.first_closed_by_client.set()
        except OSError:
            pass
        second.sendall(encode_frame(pcm(99), 99, 0))
        second.close()  # κανονικό κλείσιμο για να τελειώσει το test

    def close(self):
        for c in self.connections:
            c.close()
        self.server.close()


class ReconnectTests(unittest.TestCase):
    def test_default_timeout_is_2_to_3_seconds(self):
        self.assertGreaterEqual(RECEIVE_TIMEOUT_SECONDS, 2)
        self.assertLessEqual(RECEIVE_TIMEOUT_SECONDS, 3)

    def test_silent_director_triggers_clean_reconnect(self):
        director = SilentDirector()
        self.addCleanup(director.close)
        jitters, logs, sleeps = [], [], []

        def session(connection):
            jitter = JitterBuffer(50, BLOCK_BYTES)
            jitters.append(jitter)
            pump_frames(connection, jitter, DelayTracker(), report=None)

        started = time.monotonic()
        run_with_reconnect(*director.address, session, receive_timeout=0.5,
                           reconnect_delay=0.01, max_sessions=2,
                           log=logs.append, sleep=sleeps.append)
        elapsed = time.monotonic() - started

        self.assertTrue(director.second_connected.is_set(), "δεν έγινε επανασύνδεση")
        self.assertTrue(director.first_closed_by_client.is_set(),
                        "η παλιά σύνδεση δεν έκλεισε από τον Client")
        self.assertEqual(len(jitters[0]), 5, "χάθηκαν blocks πριν από τη σιωπή")
        self.assertEqual(len(jitters[1]), 1)
        self.assertTrue(any("0.5s" in line and "χαμένη" in line for line in logs), logs)
        self.assertEqual(sleeps, [0.01])
        self.assertLess(elapsed, 4)

    def test_wifi_stall_shorter_than_timeout_keeps_connection(self):
        """Κενό 0,6 s (όπως τα μετρημένα κολλήματα Wi-Fi) με timeout 1 s: καμία επανασύνδεση."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)

        def director():
            conn, _ = server.accept()
            with conn:
                for seq in range(10):
                    if seq == 5:
                        time.sleep(0.6)
                    conn.sendall(encode_frame(pcm(seq), seq, 0))
                    time.sleep(BLOCK_MS / 1000)

        threading.Thread(target=director, daemon=True).start()
        jitters, logs = [], []

        def session(connection):
            jitter = JitterBuffer(50, BLOCK_BYTES)
            jitters.append(jitter)
            pump_frames(connection, jitter, DelayTracker(), report=None)

        run_with_reconnect(*server.getsockname(), session, receive_timeout=1.0,
                           reconnect_delay=0, max_sessions=1, log=logs.append, sleep=lambda s: None)
        self.assertEqual(len(jitters), 1)
        self.assertEqual(len(jitters[0]), 10, "χάθηκαν blocks στο σύντομο κενό")
        self.assertFalse(any("χαμένη" in line for line in logs), logs)
        self.assertTrue(any("τερματίστηκε" in line for line in logs), logs)


if __name__ == "__main__":
    unittest.main()
