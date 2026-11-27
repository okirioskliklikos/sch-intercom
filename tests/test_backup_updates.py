"""Φάση 5B3: ρύθμιση ενημερώσεων στο backup και αντίγραφο των .sicu στο «<όνομα>-updates»."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from director.backup import BackupError, create, inspect, preview, restore, updates_copy_dir
from director.tls_identity import create_director_identity
from director.updates import CONFIG_FILE, UpdateStore, updates_dir
from shared.client_registry import SignedClientRegistry
from tests.test_backup import N, PASSWORD
from tests.test_updates import Packages


class BackupUpdatesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.old, self.new = self.root / "old", self.root / "new"
        self.old.mkdir()
        self.new.mkdir()
        self.cert = self.old / "tls" / "director-tls.crt"
        self.key = self.old / "tls" / "director-tls.key"
        self.cert.parent.mkdir()
        create_director_identity(self.cert, self.key)
        self.registry = self.old / "clients-registry.json"
        SignedClientRegistry(self.registry, self.cert, self.key)
        self.packages = Packages(self.root / "packages")
        self.packages.root.mkdir()
        self.store = UpdateStore(updates_dir(self.old), trusted_keys=[self.packages.public])
        self.backup = self.root / "usb" / "snapshot.sicb"

    def make(self, **extra):
        return create(self.backup, PASSWORD, registry_file=self.registry, tls_cert=self.cert,
                      tls_key=self.key, media_dir=self.old / "media", kdf_n=N, **extra)

    def transfer(self, **extra):
        return restore(self.backup, PASSWORD, registry_file=self.new / "clients-registry.json",
                       tls_cert=self.new / "tls" / "director-tls.crt",
                       tls_key=self.new / "tls" / "director-tls.key",
                       media_dir=self.new / "media",
                       update_trusted_keys=[self.packages.public], **extra)

    def new_store(self) -> UpdateStore:
        return UpdateStore(updates_dir(self.new), trusted_keys=[self.packages.public])

    def import_two(self):
        for version in ("1.0.1", "1.0.2", "1.0.3"):
            self.store.import_package(self.packages.make(version, size=1))
        self.store.set_auto(False)

    def test_section_restores_config_without_packages(self):
        self.import_two()
        self.make()
        self.assertFalse(updates_copy_dir(self.backup).exists())
        self.assertEqual(inspect(self.backup, PASSWORD)["summary"]["updates"], ["1.0.3", "1.0.2"])
        result = self.transfer()
        store = self.new_store()
        config = store.config()
        self.assertEqual((config["current"]["version"], config["previous"]["version"]),
                         ("1.0.3", "1.0.2"))
        self.assertFalse(config["auto"])
        self.assertTrue(store.package_missing())
        self.assertTrue(store.package_missing("previous"))
        self.assertEqual(result["updates"]["missing"], ["1.0.3", "1.0.2"])
        self.assertEqual(result["updates"]["imported"], [])

    def test_copy_folder_and_import_missing_only(self):
        self.import_two()
        _out, warnings = self.make(updates_copy="yes")
        copy = updates_copy_dir(self.backup)
        self.assertEqual(sorted(p.name for p in copy.iterdir()),
                         ["sch-intercom-client-1.0.2.sicu", "sch-intercom-client-1.0.3.sicu"])
        info = preview(self.backup, PASSWORD, registry_file=self.new / "clients-registry.json",
                       tls_cert=self.new / "tls" / "director-tls.crt", media_dir=self.new / "media")
        self.assertEqual(info["updates_info"]["copy_available"], ["1.0.3", "1.0.2"])
        result = self.transfer(restore_updates=True)
        self.assertEqual(result["updates"]["imported"], ["1.0.3", "1.0.2"])
        self.assertEqual(result["updates"]["missing"], [])
        store = self.new_store()
        self.assertFalse(store.package_missing())
        self.assertEqual(store.package_path("1.0.3").read_bytes(),
                         self.store.package_path("1.0.3").read_bytes())
        self.assertEqual(list(updates_dir(self.new).glob("*.part")), [])
        # Δεύτερη επαναφορά: τα πακέτα υπάρχουν ήδη, δεν ξαναγράφονται.
        result = self.transfer(restore_updates=True)
        self.assertEqual(result["updates"]["imported"], [])

    def test_tampered_or_unsigned_copy_is_not_imported(self):
        self.import_two()
        self.make(updates_copy="yes")
        copy = updates_copy_dir(self.backup)
        data = bytearray((copy / "sch-intercom-client-1.0.3.sicu").read_bytes())
        data[-30] ^= 0xFF
        (copy / "sch-intercom-client-1.0.3.sicu").write_bytes(bytes(data))
        result = restore(self.backup, PASSWORD, registry_file=self.new / "clients-registry.json",
                         tls_cert=self.new / "tls" / "director-tls.crt",
                         tls_key=self.new / "tls" / "director-tls.key",
                         media_dir=self.new / "media", restore_updates=True,
                         update_trusted_keys=[self.packages.public])
        self.assertEqual(result["updates"]["imported"], ["1.0.2"])
        self.assertEqual(result["updates"]["missing"], ["1.0.3"])
        # Σωστό SHA-256 αλλά άλλο κλειδί υπογραφής: δεν γίνεται δεκτό.
        shutil.rmtree(updates_dir(self.new))
        result = restore(self.backup, PASSWORD, registry_file=self.new / "clients-registry.json",
                         tls_cert=self.new / "tls" / "director-tls.crt",
                         tls_key=self.new / "tls" / "director-tls.key",
                         media_dir=self.new / "media", restore_updates=True,
                         update_trusted_keys=[Packages(self.root).public])
        self.assertEqual(result["updates"]["imported"], [])

    def test_old_backup_without_section_resets_config(self):
        self.make()                                         # χωρίς εισαγωγή πακέτου
        self.assertEqual(inspect(self.backup, PASSWORD)["summary"]["updates"], [])
        target = updates_dir(self.new) / CONFIG_FILE
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({"auto": False, "current": None, "previous": None}))
        result = self.transfer()
        self.assertTrue(self.new_store().config()["auto"])
        self.assertEqual(result["updates"]["versions"], [])
        # Το rollback (pre-restore) κρατά την παλιά ρύθμιση.
        previous = Path(result["previous"]) / "updates" / CONFIG_FILE
        self.assertFalse(json.loads(previous.read_text())["auto"])

    def test_invalid_section_rejected(self):
        from director import backup
        bad = {"schema": 1, "config": {"auto": True, "current": {"file": "../x.sicu"},
                                       "previous": None}}
        with self.assertRaisesRegex(BackupError, "ενημερώσεων"):
            backup._validate_contents({"manifest": {"backup_format": 1, "sections": {
                "updates_manifest": {"schema": 1, "sha256": backup.hashlib.sha256(
                    backup.canonical(bad)).hexdigest()}}},
                "sections": {"updates_manifest": bad, "tls_identity": {},
                             "client_registry": {}}})


if __name__ == "__main__":
    unittest.main()
