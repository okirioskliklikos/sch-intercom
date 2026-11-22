"""Phase 4.2C: encrypted Manager backup tests with temporary identities only."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from director.backup import (BackupError, BackupLocked, ERROR_SECRET, FORMAT,
                             MAGIC, _key, _read_backup, create, ensure_no_interrupted_restore,
                             inspect, manager_lock, restore, rollback)
from director.tls_identity import create_director_identity
from shared.client_registry import SignedClientRegistry, canonical, digest

PASSWORD = "ΑσφαλήςΚωδικός2026!"
N = 1 << 15


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.old = self.root / "old"
        self.new = self.root / "new"
        self.old.mkdir()
        self.new.mkdir()
        self.cert = self.old / "tls" / "director-tls.crt"
        self.key = self.old / "tls" / "director-tls.key"
        self.cert.parent.mkdir()
        create_director_identity(self.cert, self.key)
        self.registry = self.old / "clients-registry.json"
        SignedClientRegistry(self.registry, self.cert, self.key)
        (self.old / "director.id").write_text("test-director", encoding="utf-8")
        (self.old / "director-gui.json").write_text('{"width":440}', encoding="utf-8")
        (self.old / "clients-last-seen.json").write_text('{"version":1}', encoding="utf-8")
        (self.old / "enrollment-pending.json").write_text('{"requests":[]}', encoding="utf-8")
        self.media = self.old / "media"
        self.media.mkdir()
        (self.media / "test.mp3").write_bytes(b"fake sample bytes - not real student data")
        self.backup = self.root / "snapshot.sib"

    def make(self, **extra):
        return create(self.backup, PASSWORD, registry_file=self.registry,
                      tls_cert=self.cert, tls_key=self.key, media_dir=self.media,
                      kdf_n=N, **extra)

    def transfer(self, **extra):
        return restore(self.backup, PASSWORD, registry_file=self.new / "clients-registry.json",
                       tls_cert=self.new / "tls" / "director-tls.crt",
                       tls_key=self.new / "tls" / "director-tls.key",
                       media_dir=self.new / "media", **extra)

    def test_roundtrip_identity_registry_optional_files(self):
        self.make()
        result = self.transfer()
        self.assertEqual(result["registry_version"], 1)
        self.assertTrue((self.new / "tls" / "director-tls.crt").read_bytes() == self.cert.read_bytes())
        self.assertEqual((self.new / "tls" / "director-tls.key").read_bytes(), self.key.read_bytes())
        for name in ("clients-registry.json", "director.id", "director-gui.json",
                     "clients-last-seen.json", "enrollment-pending.json"):
            self.assertEqual((self.new / name).read_bytes(), (self.old / name).read_bytes())
        reread = SignedClientRegistry(self.new / "clients-registry.json",
                                      self.new / "tls" / "director-tls.crt",
                                      self.new / "tls" / "director-tls.key")
        self.assertEqual(reread.snapshot()["school_id"],
                         SignedClientRegistry(self.registry, self.cert, self.key).snapshot()["school_id"])

    def test_wrong_password_leaves_destination_untouched(self):
        self.make()
        with self.assertRaises(BackupError) as cm:
            restore(self.backup, "wrong", registry_file=self.new / "clients-registry.json",
                    tls_cert=self.new / "tls" / "director-tls.crt",
                    tls_key=self.new / "tls" / "director-tls.key", media_dir=self.new / "media")
        self.assertEqual(cm.exception.status, 3)
        self.assertEqual(str(cm.exception), ERROR_SECRET)
        self.assertFalse((self.new / "tls").exists())

    def test_tamper_ciphertext_and_tag(self):
        self.make()
        raw = self.backup.read_bytes()
        for index in (-1, -20):
            edited = bytearray(raw)
            edited[index] ^= 1
            self.backup.write_bytes(edited)
            with self.assertRaises(BackupError) as cm:
                self.transfer()
            self.assertEqual(cm.exception.status, 3)
        self.backup.write_bytes(raw)

    def test_tamper_header_authenticated(self):
        self.make()
        raw = self.backup.read_bytes()
        header_length = struct.unpack(">I", raw[4:8])[0]
        header = json.loads(raw[8:8 + header_length])
        changed = dict(header)
        changed["app_version"] = "0.0X"
        new_header = canonical(changed)
        self.backup.write_bytes(raw[:4] + struct.pack(">I", len(new_header)) +
                                new_header + raw[8 + header_length:])
        with self.assertRaises(BackupError) as cm:
            self.transfer()
        self.assertEqual(cm.exception.status, 3)

    def test_invalid_header_kdf_refused_before_derivation(self):
        self.make()
        raw = self.backup.read_bytes()
        n = struct.unpack(">I", raw[4:8])[0]
        for field, value in (("n", 1 << 30), ("r", 7)):
            header = json.loads(raw[8:8 + n])
            header["kdf"][field] = value
            new_header = canonical(header)
            self.backup.write_bytes(raw[:4] + struct.pack(">I", len(new_header)) +
                                    new_header + raw[8 + n:])
            with patch("director.backup._key", side_effect=AssertionError("KDF invoked")):
                with self.assertRaises(BackupError):
                    self.transfer()

    def test_invalid_format_sizes_and_magic(self):
        self.make()
        raw = self.backup.read_bytes()
        for edited in (b"broken", b"BOOM" + raw[4:], raw[:12],
                       b"x" * (16 * 1024 * 1024 + 1)):
            self.backup.write_bytes(edited)
            with self.assertRaises(BackupError) as cm:
                self.transfer()
            self.assertEqual(cm.exception.status, 2)

    def test_newer_header_format_refused(self):
        self.make()
        raw = self.backup.read_bytes()
        n = struct.unpack(">I", raw[4:8])[0]
        header = json.loads(raw[8:8 + n])
        header["format"] = FORMAT + 1
        edited = canonical(header)
        self.backup.write_bytes(raw[:4] + struct.pack(">I", len(edited)) +
                                edited + raw[8 + n:])
        with self.assertRaisesRegex(BackupError, "νεότερη έκδοση"):
            self.transfer()

    def test_optional_sections_can_be_missing(self):
        for name in ("director.id", "director-gui.json", "clients-last-seen.json",
                     "enrollment-pending.json"):
            (self.old / name).unlink()
        self.make()
        self.transfer()
        self.assertFalse((self.new / "director.id").exists())

    def test_invalid_private_key_or_signature_refused_on_create(self):
        other = self.root / "other"
        other.mkdir()
        other_cert, other_key = other / "cert.pem", other / "key.pem"
        create_director_identity(other_cert, other_key)
        with self.assertRaises(BackupError):
            create(self.backup, PASSWORD, registry_file=self.registry, tls_cert=self.cert,
                   tls_key=other_key, media_dir=self.media, kdf_n=N)
        doc = json.loads(self.registry.read_text(encoding="utf-8"))
        doc["registry"]["registry_version"] = 3
        self.registry.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaises(Exception):
            self.make()

    def test_password_length_nfc_and_salt_random(self):
        with self.assertRaises(BackupError):
            create(self.backup, "short", registry_file=self.registry,
                   tls_cert=self.cert, tls_key=self.key, media_dir=self.media, kdf_n=N)
        name = "ΔοκιμαστικόςΚωδικός2026!"
        self.make()
        data = self.backup.read_bytes()
        backup2 = self.root / "snapshot2.sib"
        create(backup2, name, registry_file=self.registry, tls_cert=self.cert,
               tls_key=self.key, media_dir=self.media, kdf_n=N)
        self.assertNotEqual(data, backup2.read_bytes())
        from unicodedata import normalize
        self.assertIsNotNone(inspect(backup2, normalize("NFD", name))["summary"])

    def test_manager_lock_blocks_restore(self):
        self.make()
        with manager_lock(self.new):
            with self.assertRaises(BackupLocked):
                self.transfer()

    def test_restore_different_identity_needs_opt_in(self):
        self.make()
        old_cert, old_key = self.new / "tls" / "director-tls.crt", self.new / "tls" / "director-tls.key"
        old_cert.parent.mkdir()
        create_director_identity(old_cert, old_key)
        existing = old_cert.read_bytes()
        with self.assertRaises(BackupError) as cm:
            self.transfer()
        self.assertEqual(cm.exception.status, 5)
        self.transfer(replace_identity=True)
        self.assertNotEqual(old_cert.read_bytes(), existing)
        self.assertTrue(list(self.new.glob("pre-restore-*")))

    def test_older_and_conflicting_registry_rejected(self):
        self.make()
        self.transfer()
        reg = SignedClientRegistry(self.new / "clients-registry.json",
                                   self.new / "tls" / "director-tls.crt",
                                   self.new / "tls" / "director-tls.key")
        # Simulate a valid newer registry by a signed mutation using another client.
        from shared.device_identity import create_device_identity
        import uuid
        client_id = str(uuid.uuid4())
        client_cert, _, _ = create_device_identity(self.root / "client", client_id)
        reg.approve(client_id, "C2627-Γ1", client_cert.read_bytes())
        with self.assertRaises(BackupError) as cm:
            self.transfer()
        self.assertEqual(cm.exception.status, 5)
        self.transfer(accept_older=True)

    def test_rollback_after_interruption(self):
        self.make()
        self.transfer()
        original = (self.new / "clients-registry.json").read_bytes()
        try:
            self.transfer(fail_after=2)
        except RuntimeError:
            pass
        self.assertEqual((self.new / "clients-registry.json").read_bytes(), original)
        self.assertFalse((self.new / "restore-journal.json").exists())
        self.assertFalse(rollback(self.new))

    def test_journal_prevents_start_and_rollback_idempotent(self):
        self.make()
        self.transfer()
        with self.assertRaises(BackupError):
            (self.new / "restore-journal.json").write_text("{}", encoding="utf-8")
            ensure_no_interrupted_restore(self.new)
        (self.new / "restore-journal.json").unlink()
        self.assertFalse(rollback(self.new))

    def test_media_copy_and_restore_missing_only(self):
        self.make(media_copy="yes")
        self.transfer(restore_media=True)
        self.assertEqual((self.new / "media" / "test.mp3").read_bytes(),
                         (self.media / "test.mp3").read_bytes())
        (self.new / "media" / "test.mp3").write_bytes(b"locally changed")
        self.transfer(restore_media=True)
        self.assertEqual((self.new / "media" / "test.mp3").read_bytes(), b"locally changed")

    def test_inspect_never_prints_keys(self):
        self.make()
        output = json.dumps(inspect(self.backup, PASSWORD))
        self.assertNotIn("PRIVATE KEY", output)
        self.assertNotIn("CERTIFICATE", output)
        self.assertNotIn(base64.b64encode(self.key.read_bytes()).decode(), output)

    def test_existing_backup_not_replaced(self):
        self.make()
        original = self.backup.read_bytes()
        with self.assertRaises(BackupError):
            self.make()
        self.assertEqual(self.backup.read_bytes(), original)
        self.assertFalse(self.backup.with_name(self.backup.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
