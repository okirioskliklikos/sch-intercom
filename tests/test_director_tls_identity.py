"""Ταυτότητα TLS Director χωρίς λήξη: ισχύει για TLS και για υπογραφή μητρώου."""
import datetime
from pathlib import Path
import socket
import tempfile
import threading
import unittest
import uuid

from cryptography import x509

from director.tls_identity import create_director_identity
from shared.client_registry import SignedClientRegistry
from shared.device_identity import create_device_identity
from shared.transport_security import (DEFAULT_SERVER_NAME, client_context,
                                       server_context)


class DirectorIdentityTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.cert = self.root / "director-tls.crt"
        self.key = self.root / "director-tls.key"

    def test_no_expiry_and_san(self):
        fingerprint = create_director_identity(self.cert, self.key)
        self.assertEqual(len(fingerprint.split(":")), 32)
        certificate = x509.load_pem_x509_certificate(self.cert.read_bytes())
        self.assertEqual(certificate.not_valid_after_utc,
                         datetime.datetime(9999, 12, 31, 23, 59, 59,
                                           tzinfo=datetime.timezone.utc))
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
        self.assertEqual(san.value.get_values_for_type(x509.DNSName),
                         ["school-intercom-director"])

    def test_never_overwrites(self):
        create_director_identity(self.cert, self.key)
        before = self.key.read_bytes()
        with self.assertRaises(FileExistsError):
            create_director_identity(self.cert, self.key)
        self.assertEqual(self.key.read_bytes(), before)

    def test_works_for_tls_and_registry_signature(self):
        create_director_identity(self.cert, self.key)
        server_context(self.cert, self.key)
        client_context(self.cert)
        registry = SignedClientRegistry(self.root / "registry.json", self.cert, self.key)
        client_id = str(uuid.uuid4())
        device_cert, _, _ = create_device_identity(self.root / "device", client_id)
        registry.approve(client_id, "Α1", device_cert.read_bytes())
        reloaded = SignedClientRegistry(self.root / "registry.json", self.cert, self.key)
        self.assertEqual(reloaded.version, 2)

    def test_real_handshake_with_hostname_check(self):
        create_director_identity(self.cert, self.key)
        server_side, client_side = socket.socketpair()
        server_side.settimeout(3)
        client_side.settimeout(3)
        result = []

        def serve():
            with server_context(self.cert, self.key).wrap_socket(
                    server_side, server_side=True) as secure:
                secure.sendall(b"A")
                result.append(True)

        worker = threading.Thread(target=serve)
        worker.start()
        with client_context(self.cert).wrap_socket(
                client_side, server_hostname=DEFAULT_SERVER_NAME) as secure:
            self.assertEqual(secure.recv(1), b"A")
        worker.join(timeout=3)
        self.assertEqual(result, [True])


if __name__ == "__main__":
    unittest.main()
