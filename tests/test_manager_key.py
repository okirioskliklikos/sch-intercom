"""Φάση 4.2C2: κλειδί TLS του Manager κρυπτογραφημένο στον δίσκο (DPAPI στα Windows)."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from director.backup import create, restore
from director.tls_identity import create_director_identity
from shared.client_registry import SignedClientRegistry
from shared.device_identity import _write_secret
from shared.manager_key import (ManagerKeyError, is_protected, key_password, load_private_key,
                                migrate, password_path, plaintext_pem)
from shared.transport_security import server_context

PASSWORD = "ΚωδικόςΔοκιμής-2026!"


def public_der(key) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.DER,
                                         serialization.PublicFormat.SubjectPublicKeyInfo)


class ManagerKeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cert = self.root / "tls" / "director-tls.crt"
        self.key = self.root / "tls" / "director-tls.key"

    def test_new_identity_is_encrypted_at_rest(self):
        create_director_identity(self.cert, self.key)
        content = self.key.read_bytes()
        self.assertTrue(is_protected(content))
        self.assertNotIn(b"BEGIN PRIVATE KEY", content)
        stored = password_path(self.key).read_bytes()
        password = key_password(self.key)
        self.assertEqual(len(password), 32)
        if os.name == "nt":
            # DPAPI: στο αρχείο δεν υπάρχει ο ίδιος ο κωδικός.
            self.assertNotEqual(stored, password)
            self.assertNotIn(password, stored)
        else:
            self.assertEqual(stored, password)
            self.assertEqual(password_path(self.key).stat().st_mode & 0o777, 0o600)
        server_context(self.cert, self.key)  # φορτώνει χωρίς προσωρινό αρχείο
        SignedClientRegistry(self.root / "clients-registry.json", self.cert, self.key)

    def test_existing_identity_files_are_never_overwritten(self):
        create_director_identity(self.cert, self.key)
        self.cert.unlink()
        self.key.unlink()
        with self.assertRaises(FileExistsError):  # έμεινε το .pass
            create_director_identity(self.cert, self.key)

    def legacy_key(self) -> bytes:
        """Ακρυπτογράφητο κλειδί όπως πριν από την 4.2C2."""
        create_director_identity(self.cert, self.key)
        plain = plaintext_pem(self.key)
        self.key.unlink()
        password_path(self.key).unlink()
        _write_secret(self.key, plain)
        return plain

    def test_migration_keeps_same_key_and_fingerprint(self):
        plain = self.legacy_key()
        self.assertIsNone(key_password(self.key))
        server_context(self.cert, self.key)  # το παλιό κλειδί δουλεύει ακόμη
        self.assertTrue(migrate(self.key))
        self.assertTrue(is_protected(self.key.read_bytes()))
        self.assertEqual(plaintext_pem(self.key), plain)
        self.assertFalse(migrate(self.key))  # δεύτερη φορά: τίποτα
        server_context(self.cert, self.key)
        self.assertEqual(list(self.key.parent.glob("*.tmp")), [])

    def test_interrupted_migration_is_retried(self):
        plain = self.legacy_key()
        # Διακοπή ανάμεσα: γράφτηκε νέο .pass, το κλειδί έμεινε ακρυπτογράφητο.
        password_path(self.key).write_bytes(b"stale")
        self.assertIsNone(key_password(self.key))
        self.assertTrue(migrate(self.key))
        self.assertEqual(plaintext_pem(self.key), plain)

    def test_missing_or_wrong_password_gives_restore_hint(self):
        create_director_identity(self.cert, self.key)
        stored = password_path(self.key).read_bytes()
        password_path(self.key).unlink()
        with self.assertRaises(ManagerKeyError) as missing:
            server_context(self.cert, self.key)
        self.assertIn("backup", str(missing.exception))
        password_path(self.key).write_bytes(bytes(b ^ 0xFF for b in stored))
        with self.assertRaises(ManagerKeyError) as wrong:
            load_private_key(self.key)
        self.assertIn("backup", str(wrong.exception))

    def test_backup_restore_relocks_key_for_new_computer(self):
        create_director_identity(self.cert, self.key)
        registry = self.root / "clients-registry.json"
        SignedClientRegistry(registry, self.cert, self.key)
        backup = self.root / "b.sib"
        create(backup, PASSWORD, registry_file=registry, tls_cert=self.cert, tls_key=self.key,
               media_dir=self.root / "media", kdf_n=1 << 15)
        new = self.root / "new"
        new_cert, new_key = new / "tls" / "director-tls.crt", new / "tls" / "director-tls.key"
        restore(backup, PASSWORD, registry_file=new / "clients-registry.json",
                tls_cert=new_cert, tls_key=new_key, media_dir=new / "media")
        self.assertTrue(is_protected(new_key.read_bytes()))
        self.assertNotEqual(password_path(new_key).read_bytes(),
                            password_path(self.key).read_bytes())
        self.assertEqual(public_der(load_private_key(new_key)),
                         public_der(load_private_key(self.key)))
        server_context(new_cert, new_key)
        # Κανένα ξεκλείδωτο κλειδί πουθενά στον νέο υπολογιστή.
        secret = plaintext_pem(self.key)
        for path in new.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret, path.read_bytes(), str(path))

    def test_restore_over_existing_keeps_old_password_in_pre_restore(self):
        create_director_identity(self.cert, self.key)
        registry = self.root / "clients-registry.json"
        SignedClientRegistry(registry, self.cert, self.key)
        backup = self.root / "b.sib"
        create(backup, PASSWORD, registry_file=registry, tls_cert=self.cert, tls_key=self.key,
               media_dir=self.root / "media", kdf_n=1 << 15)
        old_password = password_path(self.key).read_bytes()
        result = restore(backup, PASSWORD, registry_file=registry, tls_cert=self.cert,
                         tls_key=self.key, media_dir=self.root / "media")
        self.assertNotEqual(password_path(self.key).read_bytes(), old_password)
        moved = list(Path(result["previous"]).rglob("key.pem.pass"))
        self.assertEqual([p.read_bytes() for p in moved], [old_password])
        server_context(self.cert, self.key)

    def test_legacy_backup_key_is_accepted(self):
        """Backup από εγκατάσταση με ακρυπτογράφητο κλειδί (πριν από 4.2C2)."""
        self.legacy_key()
        registry = self.root / "clients-registry.json"
        SignedClientRegistry(registry, self.cert, self.key)
        backup = self.root / "legacy.sib"
        create(backup, PASSWORD, registry_file=registry, tls_cert=self.cert, tls_key=self.key,
               media_dir=self.root / "media", kdf_n=1 << 15)
        new = self.root / "new"
        restore(backup, PASSWORD, registry_file=new / "clients-registry.json",
                tls_cert=new / "tls" / "c.crt", tls_key=new / "tls" / "c.key",
                media_dir=new / "media")
        self.assertTrue(is_protected((new / "tls" / "c.key").read_bytes()))
        self.assertIsInstance(load_private_key(new / "tls" / "c.key"),
                              ec.EllipticCurvePrivateKey)


if __name__ == "__main__":
    unittest.main()
