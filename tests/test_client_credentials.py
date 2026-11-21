"""Phase 4.2A1: portable Client identity, signed registry and fail-closed authorization."""
import json
from pathlib import Path
import ssl
import tempfile
import unittest
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from shared.client_registry import (RegistryError, SignedClientRegistry,
                                    public_key_fingerprint, verify_document)
from shared.device_identity import (DeviceIdentityError, create_device_identity,
                                    identity_credentials)
from tests.tls_support import LocalTLSFixture


class ClientIdentityTests(unittest.TestCase):
    def test_encrypted_identity_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            client_id = str(uuid.uuid4())
            cert, key, password = create_device_identity(Path(directory), client_id)
            self.assertNotIn(b"BEGIN PRIVATE KEY-----", key.read_bytes())
            self.assertIn(b"BEGIN ENCRYPTED PRIVATE KEY", key.read_bytes())
            self.assertEqual(create_device_identity(Path(directory), client_id),
                             (cert, key, password))
            actual = x509.load_pem_x509_certificate(cert.read_bytes())
            self.assertFalse(actual.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
            self.assertEqual(actual.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value,
                             client_id)

    def test_incomplete_identity_must_not_regenerate(self):
        with tempfile.TemporaryDirectory() as directory:
            client_id = str(uuid.uuid4())
            cert, key, password = create_device_identity(Path(directory), client_id)
            key.unlink()
            with self.assertRaises(DeviceIdentityError):
                create_device_identity(Path(directory), client_id)


class SignedRegistryTests(LocalTLSFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.registry_path = Path(self._tls_temp.name) / "registry.json"
        self.manager_cert = Path(self._tls_temp.name) / "director.crt"
        self.manager_key = Path(self._tls_temp.name) / "director.key"
        self.registry = SignedClientRegistry(self.registry_path, self.manager_cert, self.manager_key)
        self.client_id = str(uuid.uuid4())
        self.device_dir = Path(self._tls_temp.name) / "device"
        self.cert, self.key, self.password = create_device_identity(self.device_dir, self.client_id)

    def test_signed_registry_approve_reload_revoke(self):
        self.assertEqual(self.registry.approved_certificates(), [])
        cert_bytes = self.cert.read_bytes()
        peer = x509.load_pem_x509_certificate(cert_bytes)
        self.assertFalse(self.registry.authorize(peer.public_bytes(serialization.Encoding.DER),
                                                 self.client_id, "Α1"))
        self.registry.approve(self.client_id, "Α1", cert_bytes)
        self.assertEqual(self.registry.version, 2)
        self.assertTrue(self.registry.authorize(peer.public_bytes(serialization.Encoding.DER),
                                                self.client_id, "Α1"))
        self.assertFalse(self.registry.authorize(peer.public_bytes(serialization.Encoding.DER),
                                                 str(uuid.uuid4()), "Α1"))
        self.assertFalse(self.registry.authorize(peer.public_bytes(serialization.Encoding.DER),
                                                 self.client_id, "Α2"))
        reloaded = SignedClientRegistry(self.registry_path, self.manager_cert, self.manager_key)
        self.assertEqual(reloaded.version, 2)
        reloaded.revoke(self.client_id)
        self.assertFalse(reloaded.authorize(peer.public_bytes(serialization.Encoding.DER),
                                            self.client_id, "Α1"))
        with self.assertRaises(RegistryError):
            reloaded.approve(self.client_id, "Α1", cert_bytes)

    def test_modified_signature_rejected(self):
        document = json.loads(self.registry_path.read_text(encoding="utf-8"))
        document["registry"]["school_id"] = str(uuid.uuid4())
        self.registry_path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(RegistryError):
            SignedClientRegistry(self.registry_path, self.manager_cert, self.manager_key)

    def test_unknown_cert_has_no_trust_anchor(self):
        context = self.registry.tls_server_context(self.manager_cert, self.manager_key)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.registry.approve(self.client_id, "Α1", self.cert.read_bytes())
        context = self.registry.tls_server_context(self.manager_cert, self.manager_key)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreaterEqual(context.cert_store_stats()["x509"], 1)

    def test_spki_is_stable(self):
        fp = public_key_fingerprint(self.cert.read_bytes())
        self.assertEqual(len(fp), 64)
        self.assertEqual(fp, public_key_fingerprint(self.cert.read_bytes()))


if __name__ == "__main__":
    unittest.main()
