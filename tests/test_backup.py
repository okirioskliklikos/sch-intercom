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
from shared.manager_key import is_protected, password_path, plaintext_pem

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

    def transfer(self, source: Path | None = None, **extra):
        return restore(source or self.backup, PASSWORD, registry_file=self.new / "clients-registry.json",
                       tls_cert=self.new / "tls" / "director-tls.crt",
                       tls_key=self.new / "tls" / "director-tls.key",
                       media_dir=self.new / "media", **extra)

    def test_roundtrip_identity_registry_optional_files(self):
        self.make()
        result = self.transfer()
        self.assertEqual(result["registry_version"], 1)
        self.assertTrue((self.new / "tls" / "director-tls.crt").read_bytes() == self.cert.read_bytes())
        # 4.2C2: ίδιο κλειδί, αλλά ξανακλειδωμένο με νέο κωδικό για τον νέο υπολογιστή.
        new_key = self.new / "tls" / "director-tls.key"
        self.assertEqual(plaintext_pem(new_key), plaintext_pem(self.key))
        self.assertNotEqual(new_key.read_bytes(), self.key.read_bytes())
        self.assertTrue(is_protected(new_key.read_bytes()))
        self.assertTrue(password_path(new_key).is_file())
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

    def snapshot(self) -> dict[str, bytes]:
        """Όλα τα αρχεία του νέου υπολογιστή, εκτός από pre-restore/staging/lock."""
        files = {}
        for path in self.new.rglob("*"):
            relative = path.relative_to(self.new)
            if (path.is_file() and relative.parts[0] != "manager.lock"
                    and not relative.parts[0].startswith(("pre-restore-", ".restore-staging-"))):
                files[relative.as_posix()] = path.read_bytes()
        return files

    def second_backup(self) -> Path:
        """Νεότερο backup με διαφορετικό περιεχόμενο σε ΟΛΕΣ τις ενότητες."""
        import uuid
        from shared.device_identity import create_device_identity
        client_id = str(uuid.uuid4())
        client_cert, _, _ = create_device_identity(self.root / "client-2", client_id)
        SignedClientRegistry(self.registry, self.cert, self.key).approve(
            client_id, "C2627-Γ2", client_cert.read_bytes())
        (self.old / "director.id").write_text("changed-director", encoding="utf-8")
        (self.old / "director-gui.json").write_text('{"width":999}', encoding="utf-8")
        (self.old / "clients-last-seen.json").write_text('{"version":2}', encoding="utf-8")
        (self.old / "enrollment-pending.json").write_text('{"requests":[1]}', encoding="utf-8")
        second = self.root / "second.sib"
        create(second, PASSWORD, registry_file=self.registry, tls_cert=self.cert,
               tls_key=self.key, media_dir=self.media, kdf_n=N)
        return second

    def assert_clean(self):
        self.assertFalse((self.new / "restore-journal.json").exists())
        self.assertFalse((self.new / "restore-journal.json.tmp").exists())
        self.assertEqual(list(self.new.glob(".restore-staging-*")), [])

    def test_rollback_after_interruption_at_every_step(self):
        """Τεχνητό kill μετά από ΚΑΘΕ μετακίνηση (και ανάμεσα στις δύο μιας εγγραφής)."""
        self.make()
        self.transfer()
        before = self.snapshot()
        second = self.second_backup()
        interrupted = 0
        for fail_after in range(1, 40):
            try:
                self.transfer(second, fail_after=fail_after)
            except KeyboardInterrupt:
                interrupted += 1
                self.assertTrue((self.new / "restore-journal.json").exists())
                with self.assertRaises(BackupError):
                    ensure_no_interrupted_restore(self.new)
                self.assertTrue(rollback(self.new))
                self.assertEqual(self.snapshot(), before, f"fail_after={fail_after}")
                self.assert_clean()
                self.assertFalse(rollback(self.new))
                continue
            break
        # 2 κινήσεις ανά υπάρχον αρχείο (παλιό → pre-restore, νέο → θέση) × 8 αρχεία
        # (μαζί με το .pass του κλειδιού, 4.2C2).
        self.assertEqual(interrupted, 16)
        after = self.snapshot()
        self.assertNotEqual(after, before)
        self.assertEqual(after["director-gui.json"], b'{"width":999}')

    def test_cli_interruption_prints_clean_message_not_traceback(self):
        import contextlib
        import io
        from director.backup import INTERRUPTED, main
        self.make()
        self.transfer()
        stderr = io.StringIO()
        env = {"SCH_TEST_PASS": PASSWORD, "SCH_BACKUP_TEST_FAIL_AFTER": "3"}
        with patch.dict(os.environ, env), contextlib.redirect_stderr(stderr), \
                contextlib.redirect_stdout(io.StringIO()):
            status = main(["restore", str(self.backup), "--accept-older",
                           "--registry-file", str(self.new / "clients-registry.json"),
                           "--tls-cert", str(self.new / "tls" / "director-tls.crt"),
                           "--tls-key", str(self.new / "tls" / "director-tls.key"),
                           "--passphrase-env", "SCH_TEST_PASS"])
        self.assertEqual(status, INTERRUPTED)
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertIn("αναμενόμενη", stderr.getvalue())
        self.assertIn("rollback", stderr.getvalue())
        self.assertTrue((self.new / "restore-journal.json").exists())
        self.assertTrue(rollback(self.new))

    def test_error_during_moves_rolls_back_automatically(self):
        import director.backup as backup
        self.make()
        self.transfer()
        before = self.snapshot()
        second = self.second_backup()
        real = backup._replace_with_retry
        calls = []

        def failing(source, target):
            calls.append(source)
            if len(calls) == 5:  # 1 = journal, 2.. = μετακινήσεις
                raise PermissionError("simulated antivirus lock")
            return real(source, target)

        with patch.object(backup, "_replace_with_retry", failing):
            with self.assertRaises(PermissionError):
                self.transfer(second)
        self.assertEqual(self.snapshot(), before)
        self.assert_clean()

    def test_failure_before_journal_leaves_no_plaintext_key(self):
        import director.backup as backup
        self.make()
        real = backup._write_exclusive
        calls = []

        def failing(path, content):
            calls.append(path)
            if len(calls) == 2:  # το κλειδί ή το πιστοποιητικό έχει ήδη γραφτεί στο staging
                raise OSError("simulated disk full")
            return real(path, content)

        with patch.object(backup, "_write_exclusive", failing):
            with self.assertRaises(OSError):
                self.transfer()
        self.assert_clean()
        self.assertEqual(list(self.new.glob("pre-restore-*")), [])
        secret = plaintext_pem(self.key)
        for path in self.new.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret, path.read_bytes(), str(path))

    def test_corrupt_journal_and_orphan_staging_are_cleaned(self):
        self.make()
        self.transfer()
        before = self.snapshot()
        orphan = self.new / ".restore-staging-crash"
        (orphan / "tls_identity").mkdir(parents=True)
        (orphan / "tls_identity" / "key.pem").write_bytes(self.key.read_bytes())
        (self.new / "restore-journal.json").write_bytes(b'{"moves": [')  # κομμένο
        with self.assertRaises(BackupError):
            ensure_no_interrupted_restore(self.new)
        self.assertTrue(rollback(self.new))
        ensure_no_interrupted_restore(self.new)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(orphan.exists())
        self.assert_clean()

    def test_pre_restore_notice_only_when_old_files_exist(self):
        self.make()
        first = self.transfer()
        self.assertIsNone(first["previous"])
        self.assertEqual(list(self.new.glob("pre-restore-*")), [])
        second = self.transfer()
        self.assertTrue(Path(second["previous"]).is_dir())
        self.assertTrue(any("ιδιωτικό κλειδί" in w for w in second["warnings"]))

    def test_media_report_reflects_restore_media(self):
        self.make(media_copy="yes")
        result = self.transfer(restore_media=True)
        self.assertEqual(result["media"]["missing"], [])
        self.assertEqual(result["media"]["copied"], ["test.mp3"])
        result = self.transfer(replace_identity=False)
        self.assertEqual(result["media"]["missing"], [])
        (self.new / "media" / "test.mp3").unlink()
        result = self.transfer()
        self.assertEqual(result["media"]["missing"], ["test.mp3"])
        self.assertEqual(result["media"]["copied"], [])

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
        self.assertNotIn(base64.b64encode(plaintext_pem(self.key)).decode(), output)

    def test_restored_manager_accepts_approved_client_over_real_mtls(self):
        import socket
        import threading
        from shared.device_identity import create_device_identity
        from shared.transport_security import client_context, connect_tls, accept_tls
        from director.main import RegistryTrust

        client_id = str(__import__("uuid").uuid4())
        client_cert, client_key, password = create_device_identity(
            self.root / "client-identity", client_id)
        registry = SignedClientRegistry(self.registry, self.cert, self.key)
        registry.approve(client_id, "C2627-Γ1", client_cert.read_bytes())
        self.make()
        self.transfer()
        new_cert = self.new / "tls" / "director-tls.crt"
        new_key = self.new / "tls" / "director-tls.key"
        trusted = SignedClientRegistry(self.new / "clients-registry.json", new_cert, new_key)
        tls = RegistryTrust(trusted, new_cert, new_key).context()
        status = []

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)

            def serve():
                try:
                    sock, _ = listener.accept()
                    with sock:
                        with accept_tls(sock, tls) as channel:
                            status.append(channel.recv(4) == b"PING")
                            channel.sendall(b"PONG")
                except Exception as exc:
                    status.append(exc)

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            client_tls = client_context(new_cert, certfile=client_cert,
                                        keyfile=client_key, password=password)
            with socket.create_connection(listener.getsockname(), timeout=3) as sock:
                with connect_tls(sock, client_tls) as channel:
                    channel.sendall(b"PING")
                    self.assertEqual(channel.recv(4), b"PONG")
            thread.join(timeout=5)
            self.assertEqual(status, [True])

    def test_running_manager_holds_lock_for_its_whole_lifetime(self):
        """Πραγματικός director.main (χωρίς μικρόφωνο): restore → 4 όσο τρέχει."""
        import socket
        import subprocess
        import sys
        import time
        self.make()
        ports = []
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM, socket.SOCK_STREAM):
            with socket.socket(socket.AF_INET, kind) as probe:
                probe.bind(("127.0.0.1", 0))
                ports.append(str(probe.getsockname()[1]))
        script = (
            "import sys, time, director.main as m\n"
            "def mic(broadcaster, stop=None, **kw):\n"
            "    while not stop.wait(0.1):\n"
            "        pass\n"
            "m.stream_microphone = mic\n"
            "sys.argv = ['director'] + sys.argv[1:]\n"
            "m.main()\n")
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
                   PYTHONPATH=str(root))
        director = subprocess.Popen(
            [sys.executable, "-c", script, "--host", "127.0.0.1", "--port", ports[0],
             "--discovery-port", ports[1], "--enrollment-port", ports[2],
             "--tls-cert", str(self.cert), "--tls-key", str(self.key),
             "--registry-file", str(self.registry),
             "--director-id-file", str(self.root / "runtime-director.id")],
            cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        try:
            line = ""
            deadline = time.monotonic() + 20
            while "Director ακούει" not in line and time.monotonic() < deadline:
                line = director.stdout.readline()
                if not line and director.poll() is not None:
                    break
            self.assertIn("Director ακούει", line)
            time.sleep(0.5)
            with self.assertRaises(BackupLocked):
                restore(self.backup, PASSWORD, registry_file=self.registry,
                        tls_cert=self.cert, tls_key=self.key, media_dir=self.media)
            # Οι σύντομες offline εντολές δουλεύουν δίπλα στον ενεργό Manager.
            listing = subprocess.run(
                [sys.executable, "-m", "director.main", "--tls-cert", str(self.cert),
                 "--tls-key", str(self.key), "--registry-file", str(self.registry),
                 "--list-clients"], cwd=root, env=env, capture_output=True, timeout=60)
            self.assertEqual(listing.returncode, 0, listing.stderr)
        finally:
            try:
                director.stdin.write("quit\n")
                director.stdin.flush()
            except OSError:
                pass  # ο Director έχει ήδη τερματίσει
            try:
                director.wait(timeout=10)
            except subprocess.TimeoutExpired:
                director.kill()
                director.wait(timeout=5)
            director.stdout.close()
        # Μετά τον τερματισμό το κλείδωμα ελευθερώνεται.
        restore(self.backup, PASSWORD, registry_file=self.registry,
                tls_cert=self.cert, tls_key=self.key, media_dir=self.media)

    def test_existing_backup_not_replaced(self):
        self.make()
        original = self.backup.read_bytes()
        with self.assertRaises(BackupError):
            self.make()
        self.assertEqual(self.backup.read_bytes(), original)
        self.assertFalse(self.backup.with_name(self.backup.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
