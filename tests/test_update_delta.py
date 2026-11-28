"""Φάση 5B2c: λήψη μόνο των αρχείων που άλλαξαν (πρωτόκολλο ενημερώσεων v2)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import time
import unittest
from unittest import mock
import uuid

from director.updates import FileReader
from shared import package as pkg
from shared.update_protocol import (UpdateProtocolError, check_message, parse_check,
                                    read_message, send_message)
from tests.test_release_package import make_tree
from tests.test_updates import ServerFixture


def tree_hashes(folder: Path) -> dict[str, str]:
    return {path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(folder.rglob("*")) if path.is_file()}


class DeltaTests(ServerFixture, unittest.TestCase):
    def build(self, version: str, *, blob: bytes, extra: dict[str, bytes] | None = None) -> Path:
        tree = make_tree(self.packages.root / f"t-{version}-{uuid.uuid4().hex[:6]}", version)
        shutil.rmtree(tree / "app" / "client" / "__pycache__")
        (tree / "app" / "blob.bin").write_bytes(blob)
        for name, data in (extra or {}).items():
            target = tree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return tree

    def package(self, version: str, **options) -> Path:
        out = self.packages.root / f"{version}-{uuid.uuid4().hex[:6]}.sicu"
        pkg.write_package(self.build(version, **options), out, self.packages.key, version)
        return out

    def installed_client(self, version="1.0.0", **options):
        updater = self.client(installed=version)
        shutil.copytree(self.build(version, **options), updater.layout.version_dir(version))
        return updater

    def served(self):
        """Ποια αρχεία και πόσα bytes έστειλε ο Manager."""
        sent: list[tuple[str, int]] = []
        original = FileReader.read

        def read(reader, name, offset, size):
            data = original(reader, name, offset, size)
            sent.append((name, len(data)))
            return data
        patcher = mock.patch.object(FileReader, "read", read)
        patcher.start()
        self.addCleanup(patcher.stop)
        return sent

    def test_only_changed_files_are_downloaded(self):
        blob = bytes(range(256)) * 4096          # 1 MB που δεν αλλάζει
        self.store.import_package(self.package("1.0.1", blob=blob,
                                               extra={"app/client/new.py": b"x = 1\n"}))
        self.open_server()
        updater = self.installed_client(blob=blob)
        before = tree_hashes(updater.layout.version_dir("1.0.0"))
        sent = self.served()
        self.assertEqual(updater.run_once("127.0.0.1"), "ready")
        self.assertEqual(sorted(name for name, _ in sent), ["VERSION", "app/client/new.py"])
        self.assertLess(sum(size for _, size in sent), 100)
        new = updater.layout.version_dir("1.0.1")
        self.assertEqual((new / "VERSION").read_text().strip(), "1.0.1")
        self.assertEqual((new / "app" / "blob.bin").read_bytes(), blob)
        # Η παλιά έκδοση έμεινε ανέπαφη (αντιγραφή, όχι μετακίνηση) για το rollback.
        self.assertEqual(tree_hashes(updater.layout.version_dir("1.0.0")), before)
        self.assertEqual(list(updater.staging.iterdir()), [])
        self.assertTrue(any("Λήψη έκδοσης 1.0.1: 2 από 5 αρχεία" in line
                            for line in self.logs), self.logs)
        self.assertTrue(any("2 από 5 αρχεία, 1 KB από 1.1 MB" in line
                            for line in self.logs if line.startswith("[Ενημερώσεις]")), self.logs)

    def test_changed_python_is_downloaded(self):
        self.store.import_package(self.package("1.0.1", blob=b"new" * 1000))
        self.open_server()
        updater = self.installed_client(blob=b"old" * 1000)
        sent = self.served()
        self.assertEqual(updater.run_once("127.0.0.1"), "ready")
        self.assertEqual(sorted({name for name, _ in sent}), ["VERSION", "app/blob.bin"])

    def test_corrupted_local_file_is_downloaded_again(self):
        blob = b"same" * 1000
        self.store.import_package(self.package("1.0.1", blob=blob))
        self.open_server()
        updater = self.installed_client(blob=blob)
        local = updater.layout.version_dir("1.0.0") / "python" / "python.exe"
        local.write_bytes(b"X" * local.stat().st_size)      # ίδιο μέγεθος, άλλο περιεχόμενο
        sent = self.served()
        self.assertEqual(updater.run_once("127.0.0.1"), "ready")
        self.assertIn("python/python.exe", {name for name, _ in sent})
        self.assertTrue((updater.layout.version_dir("1.0.1") / "python" / "python.exe")
                        .read_bytes().startswith(b"MZ"))

    def test_without_local_version_everything_is_downloaded(self):
        self.store.import_package(self.package("1.0.1", blob=b"b" * 5000))
        self.open_server()
        updater = self.client()           # χωρίς versions\1.0.0
        sent = self.served()
        self.assertEqual(updater.run_once("127.0.0.1"), "ready")
        self.assertEqual(len({name for name, _ in sent}), 4)

    def test_empty_file_and_leftovers_in_staging(self):
        self.store.import_package(self.package("1.0.1", blob=b"b" * 10,
                                               extra={"app/empty.txt": b""}))
        self.open_server()
        updater = self.installed_client(blob=b"b" * 10)
        index = self.updates.index(self.store.current())
        tree = updater.staging / "1.0.1" / "tree"
        (tree / "app").mkdir(parents=True)
        (tree / "app" / "intruder.py").write_text("bad")   # δεν είναι στο manifest
        (updater.staging / "1.0.1.json").write_text(json.dumps({"sha256": index.sha256}))
        self.assertEqual(updater.run_once("127.0.0.1"), "ready")
        new = updater.layout.version_dir("1.0.1")
        self.assertEqual((new / "app" / "empty.txt").read_bytes(), b"")
        self.assertFalse((new / "app" / "intruder.py").exists())
        self.assertEqual(set(tree_hashes(new)), set(index.files))

    def test_slow_local_work_does_not_keep_the_connection_open(self):
        """VM Windows 10 (28/11/2026): η τοπική αντιγραφή/έλεγχος κρατούσε > 30 s και ο Manager
        έκλεινε την αδρανή σύνδεση (WinError 10053). Τώρα γίνονται χωρίς ανοιχτή σύνδεση."""
        from client import updater as module
        blob = bytes(range(256)) * 1024
        self.store.import_package(self.package("1.0.1", blob=blob))
        self.open_server(idle_timeout=0.5)
        updater = self.installed_client(blob=blob)
        slow_prepare, slow_finish = updater._prepare, updater._finish

        def prepare(*args):
            time.sleep(1.2)
            return slow_prepare(*args)

        def finish(*args):
            time.sleep(1.2)
            return slow_finish(*args)
        updater._prepare, updater._finish = prepare, finish
        self.assertEqual(updater.run_once("127.0.0.1"), "ready")
        self.assertTrue((updater.layout.version_dir("1.0.1") / "VERSION").is_file())
        self.assertEqual(self.status.snapshot()[updater.client_id]["state"], "ready")
        self.assertTrue(module.HASH_CACHE)

    def test_unchanged_files_are_hard_links_and_hashes_are_cached(self):
        from client import updater as module
        blob = bytes(range(256)) * 4096
        self.store.import_package(self.package("1.0.1", blob=blob))
        self.open_server()
        updater = self.installed_client(blob=blob)
        self.assertEqual(updater.run_once("127.0.0.1"), "ready")
        old, new = updater.layout.version_dir("1.0.0"), updater.layout.version_dir("1.0.1")
        self.assertTrue((new / "app" / "blob.bin").samefile(old / "app" / "blob.bin"))
        self.assertFalse((new / "VERSION").samefile(old / "VERSION"))
        self.assertEqual((new / "VERSION").read_text(encoding="ascii").strip(), "1.0.1")
        self.assertEqual((old / "VERSION").read_text(encoding="ascii").strip(), "1.0.0")
        cache = json.loads((updater.layout.home / module.HASH_CACHE).read_text(encoding="utf-8"))
        self.assertIn(str(new / "app" / "blob.bin"), cache)
        # Επόμενη ενημέρωση από την 1.0.1: τα hashes της έρχονται από την cache.
        self.store.import_package(self.package("1.0.2", blob=blob))
        state = updater.layout.read_current()
        state.update(current="1.0.1", previous="1.0.0")
        from client.install_paths import atomic_write_json
        atomic_write_json(updater.layout.current_file, state)
        hashed = []
        original = module._sha256

        def counting(path):
            hashed.append(Path(path).name)
            return original(path)
        with mock.patch.object(module, "_sha256", counting):
            self.assertEqual(updater.run_once("127.0.0.1"), "ready")
        self.assertNotIn("blob.bin", hashed)
        newest = updater.layout.version_dir("1.0.2")
        self.assertTrue((newest / "app" / "blob.bin").samefile(old / "app" / "blob.bin"))

    def test_copy_when_hard_links_are_not_possible(self):
        blob = bytes(range(256)) * 512
        self.store.import_package(self.package("1.0.1", blob=blob))
        self.open_server()
        updater = self.installed_client(blob=blob)
        sent = self.served()
        with mock.patch("client.updater.os.link", side_effect=OSError("FAT32")):
            self.assertEqual(updater.run_once("127.0.0.1"), "ready")
        new, old = updater.layout.version_dir("1.0.1"), updater.layout.version_dir("1.0.0")
        self.assertFalse((new / "app" / "blob.bin").samefile(old / "app" / "blob.bin"))
        self.assertEqual(sorted(name for name, _ in sent), ["VERSION"])

    def test_changed_running_file_is_not_linked(self):
        """Αρχείο της έκδοσης που τρέχει με ίδιο μέγεθος αλλά άλλο περιεχόμενο: κατεβαίνει."""
        blob = bytes(range(256)) * 512
        self.store.import_package(self.package("1.0.1", blob=blob))
        self.open_server()
        updater = self.installed_client(blob=blob)
        damaged = updater.layout.version_dir("1.0.0") / "app" / "blob.bin"
        damaged.write_bytes(b"\0" * len(blob))
        sent = self.served()
        self.assertEqual(updater.run_once("127.0.0.1"), "ready")
        self.assertEqual(sorted(name for name, _ in sent), ["VERSION", "app/blob.bin"])
        self.assertEqual((updater.layout.version_dir("1.0.1") / "app" / "blob.bin").read_bytes(),
                         blob)

    def test_forged_manifest_rejected_before_any_file(self):
        self.store.import_package(self.package("1.0.1", blob=b"b"))
        forged = self.packages.root / "forged.sicu"
        pkg.write_package(self.build("1.0.1", blob=b"evil"), forged, pkg.generate_key(), "1.0.1")
        shutil.copyfile(forged, self.store.package_path("1.0.1"))
        self.open_server()
        updater = self.installed_client(blob=b"b")
        sent = self.served()
        self.assertEqual(updater.run_once("127.0.0.1"), "failed")
        self.assertEqual(sent, [])
        self.assertEqual(updater.layout.read_current()["failed"], ["1.0.1"])
        self.assertIn("Άκυρη υπογραφή", self.status.snapshot()[updater.client_id]["detail"])

    def test_manager_refuses_paths_outside_manifest_and_old_protocol(self):
        self.store.import_package(self.package("1.0.1", blob=b"b"))
        self.open_server()
        updater = self.installed_client(blob=b"b")
        stream = updater._connect("127.0.0.1")
        self.addCleanup(stream.close)
        send_message(stream, check_message(updater.client_id, "1.0.0", updater=2, protocol=7))
        self.assertEqual(read_message(stream)["type"], "offer")
        for path in ("app/missing.py", "manifest.json"):
            send_message(stream, {"type": "get-file", "version": "1.0.1", "path": path,
                                  "offset": 0})
            self.assertEqual(read_message(stream)["type"], "error")
        old = dict(check_message(updater.client_id, "1.0.0", updater=1, protocol=7), v=1)
        with self.assertRaisesRegex(UpdateProtocolError, "Παλιός μηχανισμός"):
            parse_check(old)

    def test_reader_continues_sequentially_and_seeks(self):
        self.store.import_package(self.package("1.0.1", blob=bytes(range(256)) * 2000))
        reader = FileReader(self.store.package_path("1.0.1"))
        self.addCleanup(reader.close)
        first = reader.read("app/blob.bin", 0, 1000)
        second = reader.read("app/blob.bin", 1000, 1000)
        again = reader.read("app/blob.bin", 500, 10)
        self.assertEqual(first + second, (bytes(range(256)) * 2000)[:2000])
        self.assertEqual(again, (bytes(range(256)) * 2000)[500:510])


if __name__ == "__main__":
    unittest.main()
