"""End-to-end Phase 4.2A1 admission tests using real mTLS loopback sockets."""
import socket
import threading
import time
from pathlib import Path
import tempfile
import unittest
import uuid

from director.broadcast import AudioBroadcaster
from director.main import accept_clients
from shared.client_registry import SignedClientRegistry
from shared.device_identity import create_device_identity
from shared.identity import receive_welcome, send_hello
from shared.transport_security import client_context, connect_tls
from tests.tls_support import LocalTLSFixture


class AuthenticatedAdmissionTests(LocalTLSFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        directory = Path(self._tls_temp.name)
        self.director_cert = directory / "director.crt"
        self.director_key = directory / "director.key"
        self.registry = SignedClientRegistry(directory / "registry.json",
                                             self.director_cert, self.director_key)
        self.broadcast = AudioBroadcaster()
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.stop = threading.Event()
        self.listener = threading.Thread(target=accept_clients,
            args=(self.server, self.broadcast, self.stop),
            kwargs={"tls_context": self.registry.tls_server_context(
                self.director_cert, self.director_key),
                "registry": self.registry}, daemon=True)
        self.listener.start()

    def tearDown(self):
        self.stop.set()
        self.listener.join(timeout=2)
        self.broadcast.close()
        self.server.close()

    def device(self, label: str):
        client_id = str(uuid.uuid4())
        directory = Path(self._tls_temp.name) / label
        cert, key, password = create_device_identity(directory, client_id)
        context = client_context(self.director_cert, certfile=cert,
                                 keyfile=key, password=password)
        return client_id, cert, context

    def connect(self, context):
        connection = socket.create_connection(self.server.getsockname(), timeout=3)
        connection.settimeout(3)
        try:
            return connect_tls(connection, context)
        except BaseException:
            connection.close()
            raise

    def test_unknown_client_cannot_receive_welcome(self):
        client_id, cert, context = self.device("unknown")
        try:
            with self.connect(context) as connection:
                send_hello(connection, client_id, "Α1")
                with self.assertRaises((OSError, EOFError, ValueError)):
                    receive_welcome(connection)
        except (OSError, EOFError):
            # OpenSSL may surface the rejected certificate during handshake
            # instead of during the first application read.
            pass
        self.assertEqual(self.broadcast.client_count, 0)

    def test_approved_client_joins_and_revoked_client_is_denied(self):
        self.stop.set()
        self.listener.join(timeout=2)
        client_id, cert, context = self.device("approved")
        self.registry.approve(client_id, "Α1", cert.read_bytes())
        self.stop = threading.Event()
        self.listener = threading.Thread(target=accept_clients,
            args=(self.server, self.broadcast, self.stop),
            kwargs={"tls_context": self.registry.tls_server_context(
                self.director_cert, self.director_key),
                "registry": self.registry}, daemon=True)
        self.listener.start()
        with self.connect(context) as connection:
            send_hello(connection, client_id, "Α1")
            receive_welcome(connection)
            deadline = time.monotonic() + 2
            while self.broadcast.client_count == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(self.broadcast.client_count, 1)
            with self.registry.lock:
                self.registry.revoke(client_id)
                self.broadcast.disconnect_client(client_id)
        self.assertEqual(self.broadcast.client_count, 0)
        try:
            with self.connect(context) as connection:
                send_hello(connection, client_id, "Α1")
                with self.assertRaises((OSError, EOFError, ValueError)):
                    receive_welcome(connection)
        except (OSError, EOFError):
            pass
        self.assertEqual(self.broadcast.client_count, 0)


if __name__ == "__main__":
    unittest.main()
