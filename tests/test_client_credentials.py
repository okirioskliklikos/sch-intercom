"""Phase 4.2A1: portable Client identity, signed registry and fail-closed authorization."""
import datetime
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
                                    identity_credentials, replace_device_identity)
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
            # Χωρίς λήξη (RFC 5280: 99991231235959Z, GeneralizedTime).
            self.assertEqual(actual.not_valid_after_utc,
                             datetime.datetime(9999, 12, 31, 23, 59, 59,
                                               tzinfo=datetime.timezone.utc))
            self.assertIn(b"99991231235959Z", actual.public_bytes(serialization.Encoding.DER))

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

    def test_change_by_other_process_is_adopted_not_overwritten(self):
        # Manager running (self.registry) while the CLI approves in another process.
        cli = SignedClientRegistry(self.registry_path, self.manager_cert, self.manager_key)
        cli.approve(self.client_id, "Α1", self.cert.read_bytes())
        other_id = str(uuid.uuid4())
        other_cert, _, _ = create_device_identity(Path(self._tls_temp.name) / "other", other_id)
        self.registry.approve(other_id, "Α2", other_cert.read_bytes())
        reloaded = SignedClientRegistry(self.registry_path, self.manager_cert, self.manager_key)
        self.assertEqual(reloaded.version, 3)
        self.assertEqual({entry["client_id"] for entry in reloaded.snapshot()["clients"]},
                         {self.client_id, other_id})

    def test_diverged_file_is_never_overwritten(self):
        older = self.registry_path.read_bytes()  # v1, signed by the same Manager
        self.registry.approve(self.client_id, "Α1", self.cert.read_bytes())  # v2
        # An older backup restored on disk is not a descendant of the running v2.
        self.registry_path.write_bytes(older)
        with self.assertRaises(RegistryError):
            self.registry.revoke(self.client_id)
        self.assertEqual(self.registry_path.read_bytes(), older)
        self.assertEqual(self.registry.version, 2)

    def test_certificate_without_client_extensions_rejected_cleanly(self):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, self.client_id)])
        now = datetime.datetime.now(datetime.timezone.utc)
        bare = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
                .sign(key, hashes.SHA256())).public_bytes(serialization.Encoding.PEM)
        with self.assertRaises(RegistryError):
            self.registry.approve(self.client_id, "Α1", bare)
        with self.assertRaises(RegistryError):
            self.registry.approve(self.client_id, "Α1", b"not a certificate")
        self.assertEqual(self.registry.version, 1)

    def test_rejection_reason_names_the_mismatch(self):
        der = x509.load_pem_x509_certificate(self.cert.read_bytes()).public_bytes(
            serialization.Encoding.DER)
        self.assertIn("άγνωστο", self.registry.rejection_reason(der, self.client_id, "Α1"))
        self.registry.approve(self.client_id, "Α1", self.cert.read_bytes())
        self.assertIsNone(self.registry.rejection_reason(der, self.client_id, "Α1"))
        self.assertIn("UUID", self.registry.rejection_reason(der, str(uuid.uuid4()), "Α1"))
        self.assertIn("Α1", self.registry.rejection_reason(der, self.client_id, "Α2"))
        self.registry.revoke(self.client_id)
        self.assertIn("revoked", self.registry.rejection_reason(der, self.client_id, "Α1"))

    def der(self, cert_path):
        return x509.load_pem_x509_certificate(cert_path.read_bytes()).public_bytes(
            serialization.Encoding.DER)

    def test_reapproval_after_revoke_needs_new_key_and_bans_old_one(self):
        old_cert = self.cert.read_bytes()
        old_der = self.der(self.cert)
        self.registry.approve(self.client_id, "Α1", old_cert)
        with self.assertRaises(RegistryError):  # ενεργός: πρώτα ανάκληση
            self.registry.approve(self.client_id, "Α1", old_cert)
        self.registry.revoke(self.client_id)
        with self.assertRaises(RegistryError):  # το παλιό κλειδί ποτέ ξανά
            self.registry.approve(self.client_id, "Α1", old_cert)
        new_cert, _, _ = replace_device_identity(self.device_dir, self.client_id)
        self.assertNotEqual(public_key_fingerprint(new_cert.read_bytes()),
                            public_key_fingerprint(old_cert))
        self.registry.approve(self.client_id, "Α2", new_cert.read_bytes())  # και νέα αίθουσα
        self.assertTrue(self.registry.authorize(self.der(new_cert), self.client_id, "Α2"))
        self.assertIn("ανακλημένο", self.registry.rejection_reason(old_der, self.client_id, "Α2"))
        self.assertEqual(len(self.registry.snapshot()["clients"]), 1)
        context = self.registry.tls_server_context(self.manager_cert, self.manager_key)
        self.assertEqual(context.cert_store_stats()["x509"], 1)
        # Μετά από επανεκκίνηση ισχύουν τα ίδια (υπογεγραμμένο και επαληθευμένο αρχείο).
        reloaded = SignedClientRegistry(self.registry_path, self.manager_cert, self.manager_key)
        self.assertTrue(reloaded.authorize(self.der(new_cert), self.client_id, "Α2"))
        self.assertFalse(reloaded.authorize(old_der, self.client_id, "Α2"))

    def test_revoked_key_is_recorded_in_signed_registry(self):
        cert = self.cert.read_bytes()
        self.registry.approve(self.client_id, "Α1", cert)
        self.registry.revoke(self.client_id)
        replace_device_identity(self.device_dir, self.client_id)
        new_cert = self.cert.read_bytes()
        self.registry.approve(self.client_id, "Α1", new_cert)
        snapshot = self.registry.snapshot()
        old_fp = snapshot["clients"][0]["previous_spki"][0]
        self.assertEqual(old_fp, public_key_fingerprint(cert))

    def test_blocked_client_cannot_be_reapproved(self):
        self.registry.approve(self.client_id, "Α1", self.cert.read_bytes())
        self.registry.revoke(self.client_id, blocked=True)
        new_cert, _, _ = replace_device_identity(self.device_dir, self.client_id)
        with self.assertRaises(RegistryError):
            self.registry.approve(self.client_id, "Α1", new_cert.read_bytes())

    def test_signed_registry_reusing_revoked_key_is_rejected(self):
        # Ακόμη και υπογεγραμμένο αρχείο δεν επιτρέπει το ίδιο κλειδί σε δύο θέσεις.
        self.registry.approve(self.client_id, "Α1", self.cert.read_bytes())
        self.registry.revoke(self.client_id)
        replace_device_identity(self.device_dir, self.client_id)
        self.registry.approve(self.client_id, "Α1", self.cert.read_bytes())
        registry = self.registry.snapshot()
        entry = registry["clients"][0]
        entry["previous_spki"].append(entry["spki_sha256"])
        from shared.client_registry import _wrap
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        key = load_pem_private_key(self.manager_key.read_bytes(), password=None)
        with self.assertRaises(RegistryError):
            verify_document(_wrap(registry, key), self.manager_cert.read_bytes())

    def test_replace_identity_keeps_uuid(self):
        old = public_key_fingerprint(self.cert.read_bytes())
        cert, _, _ = replace_device_identity(self.device_dir, self.client_id)
        actual = x509.load_pem_x509_certificate(cert.read_bytes())
        self.assertEqual(actual.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value,
                         self.client_id)
        self.assertNotEqual(public_key_fingerprint(cert.read_bytes()), old)
        self.assertEqual(identity_credentials(self.device_dir)[0], cert)

    def test_spki_is_stable(self):
        fp = public_key_fingerprint(self.cert.read_bytes())
        self.assertEqual(len(fp), 64)
        self.assertEqual(fp, public_key_fingerprint(self.cert.read_bytes()))


if __name__ == "__main__":
    unittest.main()
