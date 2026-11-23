"""4.2D1: registry exchange, ancestry, conflict rules, role restrictions."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock
import uuid

from director.main import RegistryTrust
from director.manager_state import RoleError, restored
from director.registry_sync import (SyncError, apply_from_file, examine, export_registry,
                                    main as sync_main, status as sync_status)
from director.tls_identity import create_director_identity
from shared.client_registry import (RegistryError, SignedClientRegistry, digest,
                                    merge, relation, verify_document)
from shared.device_identity import create_device_identity


class RegistrySyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.a = self.root / "a"
        self.b = self.root / "b"
        self.a.mkdir()
        self.b.mkdir()
        self.cert = self.root / "tls.crt"
        self.key = self.root / "tls.key"
        create_director_identity(self.cert, self.key)
        self.a_file = self.a / "clients-registry.json"
        self.b_file = self.b / "clients-registry.json"
        self.ra = SignedClientRegistry(self.a_file, self.cert, self.key)
        shutil.copyfile(self.a_file, self.b_file)
        self.rb = SignedClientRegistry(self.b_file, self.cert, self.key)

    def device(self, label):
        client_id = str(uuid.uuid4())
        cert, _key, _password = create_device_identity(self.root / label, client_id)
        return client_id, cert.read_bytes()

    def approve(self, registry, label, room="A1"):
        cid, cert = self.device(label)
        registry.approve(cid, room, cert, name="PC-" + label)
        return cid, cert

    def export_a(self, path=None):
        return export_registry(self.a_file, self.cert, path or (self.root / "export.sicr"))

    def test_relation_branches(self):
        self.assertEqual(relation(self.ra.snapshot(), self.rb.snapshot()), "same")
        self.approve(self.ra, "one")
        self.assertEqual(relation(self.rb.snapshot(), self.ra.snapshot()), "newer")
        self.assertEqual(relation(self.ra.snapshot(), self.rb.snapshot()), "older")
        self.approve(self.rb, "two")
        self.assertEqual(relation(self.ra.snapshot(), self.rb.snapshot()), "conflict")

    def test_export_identical_bytes_and_never_overwrite(self):
        path = self.export_a()
        self.assertEqual(path.read_bytes(), self.a_file.read_bytes())
        with self.assertRaises(FileExistsError):
            self.export_a()
        self.assertEqual(path.read_bytes(), self.a_file.read_bytes())
        self.assertEqual(list(self.root.glob("export.sicr.*.tmp")), [])
        metadata = json.loads((self.a / "registry-export.json").read_text())
        self.assertEqual(metadata["registry_hash"], digest(self.ra.snapshot()))

    def test_unexported_versions_and_backup_metadata(self):
        self.assertIsNone(sync_status(self.a_file, self.cert)["unexported"])
        self.export_a()
        self.assertEqual(sync_status(self.a_file, self.cert)["unexported"], 0)
        self.approve(self.ra, "one")
        self.assertEqual(sync_status(self.a_file, self.cert)["unexported"], 1)
        (self.a / "last-backup.json").write_text(
            json.dumps({"registry_version": 2}), encoding="utf-8")
        self.assertEqual(sync_status(self.a_file, self.cert)["unexported"], 0)

    def test_newer_import_and_repeat_is_same(self):
        cid, _ = self.approve(self.ra, "one")
        source = self.export_a()
        document, local, kind, view = examine(self.b_file, self.cert, source)
        self.assertEqual(kind, "newer")
        self.assertTrue(view["added"])
        result = apply_from_file(self.rb, source, expected_digest=digest(local))
        self.assertTrue(result["changed"])
        self.assertEqual(self.b_file.read_bytes(), source.read_bytes())
        self.assertEqual(apply_from_file(self.rb, source)["relation"], "same")
        with self.assertRaises(SyncError) as error:
            apply_from_file(self.ra, self.b_file.with_name("missing.sicr"))
        self.assertEqual(error.exception.status, 2)

    def test_older_is_never_accepted(self):
        source = self.export_a()
        self.approve(self.ra, "one")
        with self.assertRaises(SyncError) as error:
            apply_from_file(self.ra, source, merge_conflict=True)
        self.assertEqual(error.exception.status, 5)

    def test_conflict_requires_merge_and_is_signed(self):
        a_id, _ = self.approve(self.ra, "one")
        b_id, _ = self.approve(self.rb, "two")
        source = self.export_a()
        with self.assertRaises(SyncError) as error:
            apply_from_file(self.rb, source)
        self.assertEqual(error.exception.status, 5)
        a_version = self.ra.version
        b_version = self.rb.version
        result = apply_from_file(self.rb, source, merge_conflict=True)
        self.assertEqual(result["relation"], "conflict")
        self.assertGreater(self.rb.version, max(a_version, b_version))
        self.assertEqual({e["client_id"] for e in self.rb.snapshot()["clients"]},
                         {a_id, b_id})
        doc = json.loads(self.b_file.read_text())
        verify_document(doc, self.cert.read_bytes())
        self.assertTrue((self.b / "registry-merges.log").exists())
        self.assertEqual(relation(self.ra.snapshot(), self.rb.snapshot()), "newer")

    def test_preview_digest_race_is_refused(self):
        self.approve(self.ra, "one")
        source = self.export_a()
        digest_before = digest(self.rb.snapshot())
        self.approve(self.rb, "two")
        with self.assertRaises(SyncError):
            apply_from_file(self.rb, source, merge_conflict=True,
                            expected_digest=digest_before)

    def test_tampered_and_other_identity_denied(self):
        self.approve(self.ra, "one")
        source = self.export_a()
        raw = bytearray(source.read_bytes())
        raw[-8] ^= 1
        source.write_bytes(raw)
        with self.assertRaises(SyncError) as error:
            apply_from_file(self.rb, source)
        self.assertEqual(error.exception.status, 2)
        # Ανεξάρτητος Manager με διαφορετικό πιστοποιητικό υπογραφής.
        cert = self.root / "other.crt"
        key = self.root / "other.key"
        create_director_identity(cert, key)
        file = self.root / "other-registry.json"
        SignedClientRegistry(file, cert, key)
        with self.assertRaises(SyncError) as error:
            apply_from_file(self.rb, file)
        self.assertIn("άλλον Manager", str(error.exception))

    def test_offline_locked_import_but_status_export_allowed(self):
        from director.backup import manager_lock
        self.approve(self.ra, "one")
        source = self.export_a()
        with manager_lock(self.b):
            self.assertEqual(sync_main([
                "status", "--registry-file", str(self.b_file),
                "--tls-cert", str(self.cert)]), 0)
            self.assertEqual(sync_main([
                "export", "--registry-file", str(self.b_file),
                "--tls-cert", str(self.cert), "--out", str(self.root / "b.sicr")]), 0)
            self.assertEqual(sync_main([
                "import", str(source), "--registry-file", str(self.b_file),
                "--tls-cert", str(self.cert), "--tls-key", str(self.key),
                "--yes"]), 4)

    def test_offline_import_reports_running_manager_before_reading_file(self):
        # Βήμα 20: ίδιο ή παλαιότερο αρχείο με Manager σε λειτουργία → 4, όχι 0/5.
        from director.backup import manager_lock
        same = self.root / "same.sicr"
        same.write_bytes(self.b_file.read_bytes())
        with manager_lock(self.b):
            for source, extra in ((same, ["--yes"]), (same, [])):
                with self.subTest(extra=extra), \
                        mock.patch("builtins.input", side_effect=AssertionError("prompt")):
                    self.assertEqual(sync_main([
                        "import", str(source), "--registry-file", str(self.b_file),
                        "--tls-cert", str(self.cert), "--tls-key", str(self.key),
                        *extra]), 4)

    def test_restore_journal_denies_operations(self):
        (self.b / "restore-journal.json").write_text("{}", encoding="utf-8")
        self.assertEqual(sync_main([
            "status", "--registry-file", str(self.b_file),
            "--tls-cert", str(self.cert)]), 2)

    def test_live_trust_import_rotates_allowlist_and_disconnects(self):
        """Ο ενεργός RegistryTrust δέχεται νέο πιστοποιητικό χωρίς επανεκκίνηση,
        και το αφαιρεί αμέσως από το TLSContext μετά από εισαγόμενη ανάκληση.
        """
        cid, cert = self.approve(self.ra, "approved")
        trust = RegistryTrust(self.rb, self.cert, self.key)

        class FakeBroadcaster:
            def __init__(self):
                self.disconnected, self.updated = [], []

            def disconnect_client(self, client_id):
                self.disconnected.append(client_id)

            def update_client_info(self, client_id, room, name):
                self.updated.append((client_id, room, name))

        broadcaster = FakeBroadcaster()
        self.assertEqual(trust.context().cert_store_stats()["x509"], 0)
        source = self.export_a(self.root / "new-client.sicr")
        result = trust.import_registry(source, broadcaster)
        self.assertEqual(result["relation"], "newer")
        self.assertEqual(trust.context().cert_store_stats()["x509"], 1)
        self.assertEqual(broadcaster.updated[0][0], cid)

        self.ra.revoke(cid)
        revoked = self.export_a(self.root / "revoked-client.sicr")
        result = trust.import_registry(revoked, broadcaster)
        self.assertTrue(result["changed"])
        self.assertEqual(trust.context().cert_store_stats()["x509"], 0)
        self.assertIn(cid, broadcaster.disconnected)

    def test_role_guard(self):
        cid, cert = self.device("one")
        trust = RegistryTrust(self.ra, self.cert, self.key)
        from director.broadcast import AudioBroadcaster
        broadcaster = AudioBroadcaster()
        self.addCleanup(broadcaster.close)
        restored(self.a / "manager-state.json")
        with self.assertRaises(RoleError):
            trust.approve(cid, "A1", cert)
        with self.assertRaises(RoleError):
            trust.revoke(cid, broadcaster)
        with self.assertRaises(RoleError):
            trust.update_info(cid, broadcaster, room="B1")
        self.assertEqual(self.ra.version, 1)


def _entry(cid, key, room, *, status="approved", previous=None, name="PC"):
    return {"client_id": cid, "room": room, "name": name,
            "spki_sha256": key, "cert_pem": "PEM-" + key,
            "status": status, "previous_spki": previous or [],
            "changed_in_version": 2}


class MergeRulesTests(unittest.TestCase):
    def setUp(self):
        self.cid = str(uuid.uuid4())
        self.other = str(uuid.uuid4())
        self.a = {"format": "sch-intercom-registry", "schema_version": 1,
                  "school_id": str(uuid.uuid4()), "registry_version": 1,
                  "history": [], "clients": []}
        self.b = json.loads(json.dumps(self.a))

    def test_revocation_and_blocking_dominate(self):
        self.a["clients"] = [_entry(self.cid, "a" * 64, "A1")]
        self.b["clients"] = [_entry(self.cid, "a" * 64, "A2", status="revoked")]
        result, report = merge(self.a, self.b)
        self.assertEqual(result["clients"][0]["status"], "revoked")
        self.b["clients"][0]["status"] = "blocked"
        result, _ = merge(self.a, self.b, prefer="incoming")
        self.assertEqual(result["clients"][0]["status"], "blocked")

    def test_metadata_preference(self):
        self.a["clients"] = [_entry(self.cid, "a" * 64, "A1")]
        self.b["clients"] = [_entry(self.cid, "a" * 64, "B2")]
        local, _ = merge(self.a, self.b)
        other, _ = merge(self.a, self.b, prefer="incoming")
        self.assertEqual(local["clients"][0]["room"], "A1")
        self.assertEqual(other["clients"][0]["room"], "B2")

    def test_two_keys_preference_and_old_key_forbidden(self):
        self.a["clients"] = [_entry(self.cid, "a" * 64, "A1")]
        self.b["clients"] = [_entry(self.cid, "b" * 64, "B1")]
        merged, _ = merge(self.a, self.b)
        self.assertEqual(merged["clients"][0]["spki_sha256"], "a" * 64)
        self.assertIn("b" * 64, merged["clients"][0]["previous_spki"])
        merged, _ = merge(self.a, self.b, prefer="incoming")
        self.assertEqual(merged["clients"][0]["spki_sha256"], "b" * 64)
        self.assertIn("a" * 64, merged["clients"][0]["previous_spki"])
        self.b["clients"][0]["status"] = "revoked"
        merged, _ = merge(self.a, self.b, prefer="incoming")
        self.assertEqual(merged["clients"][0]["spki_sha256"], "a" * 64)
        self.assertEqual(merged["clients"][0]["status"], "approved")

    def test_forbidden_never_approved(self):
        self.a["clients"] = [_entry(self.cid, "a" * 64, "A1")]
        self.b["clients"] = [_entry(self.cid, "a" * 64, "A1", status="revoked")]
        for policy in ("local", "incoming"):
            merged, _ = merge(self.a, self.b, prefer=policy)
            self.assertNotEqual(merged["clients"][0]["status"], "approved")

    def test_duplicate_key_across_ids_denied(self):
        self.a["clients"] = [_entry(self.cid, "a" * 64, "A1")]
        self.b["clients"] = [_entry(self.other, "a" * 64, "A2")]
        with self.assertRaises(RegistryError):
            merge(self.a, self.b)

    def test_new_clients_preserve_local_order(self):
        self.a["clients"] = [_entry(self.cid, "a" * 64, "A1")]
        self.b["clients"] = [_entry(self.other, "b" * 64, "B1")]
        merged, _ = merge(self.a, self.b)
        self.assertEqual([c["client_id"] for c in merged["clients"]],
                         [self.cid, self.other])
        self.assertEqual(merged["registry_version"], len(merged["history"]) + 1)


if __name__ == "__main__":
    unittest.main()
