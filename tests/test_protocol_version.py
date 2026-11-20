"""Έλεγχος έκδοσης πρωτοκόλλου κατά τη σύνδεση Client -> Director."""

import contextlib
import io
import json
import socket
import struct
import threading
import time
import unittest
import uuid

from director.broadcast import AudioBroadcaster
from director.main import accept_clients
from shared.audio import read_exact
from shared.identity import (
    CONNECTION_PROTOCOL, HELLO_MAGIC, WELCOME, WELCOME_INCOMPATIBLE, WELCOME_MAGIC,
    WELCOME_OK, ProtocolMismatch, receive_hello, receive_welcome, send_hello, send_welcome,
)


def raw_hello(payload: dict) -> bytes:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return HELLO_MAGIC + struct.pack("!H", len(data)) + data


class HelloVersionTests(unittest.TestCase):
    def setUp(self):
        self.left, self.right = socket.socketpair()
        self.right.settimeout(2)
        self.client_id = str(uuid.uuid4())

    def tearDown(self):
        self.left.close()
        self.right.close()

    def test_current_version_accepted(self):
        send_hello(self.left, self.client_id, "C2627-Γ1")
        self.assertEqual(receive_hello(self.right), (self.client_id, "C2627-Γ1"))

    def test_old_client_without_version_rejected_clearly(self):
        self.left.sendall(raw_hello({"client_id": self.client_id, "room": "C2627-Γ1"}))
        with self.assertRaisesRegex(ProtocolMismatch, "Παλιός Client") as caught:
            receive_hello(self.right)
        self.assertIsNone(caught.exception.client_version)

    def test_other_client_version_rejected(self):
        send_hello(self.left, self.client_id, "C2627-Γ1", protocol=CONNECTION_PROTOCOL + 1)
        with self.assertRaisesRegex(ProtocolMismatch, "ενημερώστε μαζί") as caught:
            receive_hello(self.right)
        self.assertEqual(caught.exception.client_version, CONNECTION_PROTOCOL + 1)

    def test_invalid_version_values(self):
        for value in (True, "2", 0, 256, 2.0, None):
            with self.subTest(value=value):
                left, right = socket.socketpair()
                try:
                    left.sendall(raw_hello({"client_id": self.client_id, "room": "C2627-Γ1",
                                            "protocol": value}))
                    with self.assertRaises(ValueError) as caught:
                        receive_hello(right)
                    self.assertNotIsInstance(caught.exception, ProtocolMismatch)
                finally:
                    left.close()
                    right.close()


class WelcomeTests(unittest.TestCase):
    def setUp(self):
        self.left, self.right = socket.socketpair()
        self.right.settimeout(2)

    def tearDown(self):
        self.left.close()
        self.right.close()

    def test_accept(self):
        send_welcome(self.left, accepted=True)
        receive_welcome(self.right)

    def test_incompatible_status(self):
        send_welcome(self.left, accepted=False)
        with self.assertRaisesRegex(ProtocolMismatch, "Ασύμβατη έκδοση"):
            receive_welcome(self.right)

    def test_other_director_version(self):
        self.left.sendall(WELCOME.pack(WELCOME_MAGIC, CONNECTION_PROTOCOL + 1, WELCOME_OK))
        with self.assertRaisesRegex(ProtocolMismatch, f"Director v{CONNECTION_PROTOCOL + 1}"):
            receive_welcome(self.right)

    def test_unknown_reply(self):
        self.left.sendall(b"XXXX\x02\x00")
        with self.assertRaises(ProtocolMismatch):
            receive_welcome(self.right)

    def test_old_director_closes_without_reply(self):
        self.left.close()
        with self.assertRaisesRegex(EOFError, "παλαιότερη έκδοση Director"):
            receive_welcome(self.right)


class DirectorAcceptVersionTests(unittest.TestCase):
    def setUp(self):
        self.broadcast = AudioBroadcaster(queue_blocks=5)
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.stop = threading.Event()
        self.output = io.StringIO()
        self.listener = threading.Thread(target=self._accept, daemon=True)
        self.listener.start()

    def _accept(self):
        with contextlib.redirect_stdout(self.output):
            accept_clients(self.server, self.broadcast, self.stop)

    def tearDown(self):
        self.stop.set()
        self.listener.join(timeout=1)
        self.broadcast.close()
        self.server.close()

    def connect(self):
        connection = socket.create_connection(self.server.getsockname(), timeout=2)
        connection.settimeout(2)
        self.addCleanup(connection.close)
        return connection

    def test_current_client_accepted(self):
        connection = self.connect()
        send_hello(connection, str(uuid.uuid4()), "C2627-Γ1")
        receive_welcome(connection)
        end = time.monotonic() + 2
        while self.broadcast.client_count != 1 and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(self.broadcast.client_count, 1)

    def test_old_client_closed_without_reply(self):
        connection = self.connect()
        connection.sendall(raw_hello({"client_id": str(uuid.uuid4()), "room": "C2627-Γ1"}))
        self.assertEqual(connection.recv(16), b"")
        self.assertEqual(self.broadcast.client_count, 0)

    def test_other_version_gets_incompatible_reply(self):
        connection = self.connect()
        send_hello(connection, str(uuid.uuid4()), "C2627-Γ1", protocol=CONNECTION_PROTOCOL + 1)
        magic, version, status = WELCOME.unpack(read_exact(connection, WELCOME.size))
        self.assertEqual((magic, version, status),
                         (WELCOME_MAGIC, CONNECTION_PROTOCOL, WELCOME_INCOMPATIBLE))
        self.assertEqual(connection.recv(16), b"")
        self.assertEqual(self.broadcast.client_count, 0)
        self.stop.set()
        self.listener.join(timeout=1)
        self.assertIn("ενημερώστε μαζί", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
