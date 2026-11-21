"""Loopback TLS tests; all temporary private keys remain outside the repository."""

import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from shared.transport_security import (
    DEFAULT_SERVER_NAME, accept_tls, client_context, connect_tls, server_context,
)


@unittest.skipUnless(shutil.which("openssl"), "requires openssl CLI for ephemeral test certificates")
class TLSTransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)
        self.cert = base / "director.crt"
        self.key = base / "director.key"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-days", "1", "-sha256", "-keyout", str(self.key), "-out", str(self.cert),
            "-subj", f"/CN={DEFAULT_SERVER_NAME}",
            "-addext", f"subjectAltName=DNS:{DEFAULT_SERVER_NAME}",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    def _exchange(self, *, client_ca=None, server_name=DEFAULT_SERVER_NAME,
                  plaintext=False):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)
        self.addCleanup(listener.close)
        errors = []
        received = []
        def server():
            try:
                raw, _ = listener.accept()
                with raw:
                    with accept_tls(raw, server_context(self.cert, self.key)) as stream:
                        received.append(stream.recv(16))
                        stream.sendall(b"encrypted-pcm")
            except (OSError, ssl.SSLError) as exc:
                errors.append(exc)
        worker = threading.Thread(target=server, daemon=True)
        worker.start()
        try:
            raw = socket.create_connection(listener.getsockname(), timeout=3)
            with raw:
                if plaintext:
                    raw.sendall(b"cleartext")
                    raw.settimeout(2)
                    try:
                        raw.recv(16)
                    except (OSError, ConnectionError):
                        pass
                else:
                    with connect_tls(raw, client_context(client_ca or self.cert), server_name) as stream:
                        stream.sendall(b"pcm-block")
                        self.assertEqual(stream.recv(32), b"encrypted-pcm")
        finally:
            worker.join(timeout=4)
            self.assertFalse(worker.is_alive(), "server handshake blocked")
        return received, errors

    def test_valid_certificate_transfers_audio_payload(self):
        received, errors = self._exchange()
        self.assertEqual(received, [b"pcm-block"])
        self.assertFalse(errors)

    def test_wrong_server_name_rejected(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            self._exchange(server_name="impostor")

    def test_untrusted_certificate_rejected(self):
        # Trusting the default public roots cannot validate our ephemeral self-signed server.
        with self.assertRaises(ssl.SSLCertVerificationError):
            self._exchange(client_ca=Path(self.directory.name) / "missing.crt")

    def test_plaintext_is_not_a_protocol_connection(self):
        received, errors = self._exchange(plaintext=True)
        self.assertEqual(received, [])
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
