"""Φάση 5B1: υπογεγραμμένο πακέτο .sicu, κλειδιά και εργαλεία υπογραφής."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from shared import package as pkg
from shared.release_keys import TRUSTED_KEYS
from tools import release_keygen, release_sign, release_verify

PASS = "correct horse battery"


def make_tree(root: Path, version: str = "1.0.1") -> Path:
    tree = root / "release" / version
    (tree / "python").mkdir(parents=True)
    (tree / "python" / "python.exe").write_bytes(b"MZ" + bytes(range(256)) * 10)
    (tree / "app" / "client").mkdir(parents=True)
    (tree / "app" / "client" / "main.py").write_text("print('Γεια')\n", encoding="utf-8")
    (tree / "app" / "client" / "__pycache__").mkdir()
    (tree / "app" / "client" / "__pycache__" / "x.pyc").write_bytes(b"cache")
    (tree / "VERSION").write_text(version + "\n", encoding="ascii")
    return tree


def rewrite(source: Path, target: Path, change) -> None:
    """Αντίγραφο του zip με αλλαγή: change(name, data) -> data ή None (παράλειψη)."""
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(target, "w") as new:
        for info in old.infolist():
            data = change(info.filename, old.read(info))
            if data is not None:
                new.writestr(info.filename, data)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.tree = make_tree(self.root)
        self.key = pkg.generate_key()
        self.public = pkg.public_hex(self.key)
        self.out = self.root / "c.sicu"
        self.manifest = pkg.write_package(self.tree, self.out, self.key, "1.0.1",
                                          connection_protocol=7, created_at=1)

    def verify(self, path=None, **options):
        options.setdefault("trusted_keys", [self.public])
        return pkg.verify_package(path or self.out, **options)

    def test_round_trip_and_extract(self):
        manifest = self.verify(installed_version="1.0.0")
        paths = [item["path"] for item in manifest["files"]]
        self.assertEqual(paths, ["VERSION", "app/client/main.py", "python/python.exe"])
        self.assertEqual(manifest["connection_protocol"], 7)
        dest = self.root / "versions" / "1.0.1"
        pkg.extract_package(self.out, dest, trusted_keys=[self.public])
        for path in paths:
            self.assertEqual((dest / path).read_bytes(), (self.tree / path).read_bytes())
        self.assertFalse(dest.with_name("1.0.1.new").exists())
        with self.assertRaises(pkg.PackageError):
            pkg.extract_package(self.out, dest, trusted_keys=[self.public])   # υπάρχει ήδη

    def test_wrong_key_and_no_keys(self):
        with self.assertRaisesRegex(pkg.PackageError, "Άκυρη υπογραφή"):
            self.verify(trusted_keys=[pkg.public_hex(pkg.generate_key())])
        with self.assertRaisesRegex(pkg.PackageError, "ενσωματωμένο κλειδί"):
            self.verify(trusted_keys=[])
        # Δύο κλειδιά: αρκεί το ένα.
        self.verify(trusted_keys=[pkg.public_hex(pkg.generate_key()), self.public])

    def test_tampered_file_rejected(self):
        bad = self.root / "bad.sicu"
        rewrite(self.out, bad, lambda name, data: data[:-1] + bytes([data[-1] ^ 1])
                if name == "files/python/python.exe" else data)
        with self.assertRaisesRegex(pkg.PackageError, "Αλλοιωμένο αρχείο: python/python.exe"):
            self.verify(bad)
        dest = self.root / "x"
        with self.assertRaises(pkg.PackageError):
            pkg.extract_package(bad, dest, trusted_keys=[self.public])
        self.assertFalse(dest.exists())

    def test_tampered_manifest_rejected(self):
        bad = self.root / "bad.sicu"

        def change(name, data):
            if name == pkg.MANIFEST:
                manifest = json.loads(data)
                manifest["version"] = "9.9.9"
                return pkg.manifest_bytes(manifest)
            return data
        rewrite(self.out, bad, change)
        with self.assertRaisesRegex(pkg.PackageError, "Άκυρη υπογραφή"):
            self.verify(bad)

    def test_extra_or_missing_member_rejected(self):
        extra = self.root / "extra.sicu"
        rewrite(self.out, extra, lambda name, data: data)
        with zipfile.ZipFile(extra, "a") as archive:
            archive.writestr("files/app/evil.py", b"x")
        with self.assertRaisesRegex(pkg.PackageError, "εκτός manifest"):
            self.verify(extra)
        missing = self.root / "missing.sicu"
        rewrite(self.out, missing, lambda name, data: None if name == "files/VERSION" else data)
        with self.assertRaisesRegex(pkg.PackageError, "Λείπει αρχείο: VERSION"):
            self.verify(missing)

    def test_unsafe_paths_rejected(self):
        for bad in ("../x", "/etc/x", "a/../b", "C:/x", "a\\b", "a//b", "./a", "", "a/./b"):
            with self.subTest(path=bad), self.assertRaises(pkg.PackageError):
                pkg.safe_path(bad)
        manifest = json.loads(pkg.manifest_bytes(self.manifest))
        manifest["files"][0]["path"] = "../../evil"
        data = pkg.manifest_bytes(manifest)
        bad = self.root / "slip.sicu"
        rewrite(self.out, bad, lambda name, old: data if name == pkg.MANIFEST else
                (self.key.sign(data) if name == pkg.SIGNATURE else old))
        with self.assertRaisesRegex(pkg.PackageError, "Μη ασφαλής διαδρομή"):
            self.verify(bad)

    def test_versions_and_updater(self):
        for installed in ("1.0.1", "1.0.2", "2.0.0"):
            with self.subTest(installed=installed), \
                    self.assertRaisesRegex(pkg.PackageError, "δεν είναι νεότερη"):
                self.verify(installed_version=installed)
        self.verify(installed_version="0.9.9")
        newer = self.root / "newer.sicu"
        pkg.write_package(self.tree, newer, self.key, "1.0.2", min_updater=2)
        with self.assertRaisesRegex(pkg.PackageError, "updater v2"):
            self.verify(newer)
        self.verify(newer, updater_version=2)

    def test_not_a_package(self):
        junk = self.root / "junk.sicu"
        junk.write_bytes(b"not a zip")
        with self.assertRaisesRegex(pkg.PackageError, "Μη αναγνώσιμο"):
            self.verify(junk)
        with mock.patch.object(pkg, "MAX_PACKAGE_BYTES", 10):
            with self.assertRaisesRegex(pkg.PackageError, "300 MB"):
                self.verify()

    def test_pycache_excluded(self):
        self.assertFalse(any("__pycache__" in item["path"] for item in self.manifest["files"]))


class KeyToolTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def test_private_key_encrypted_and_passphrase_checked(self):
        key = pkg.generate_key()
        path = self.root / "k.pem"
        with self.assertRaises(ValueError):
            pkg.save_private_key(key, path, "short")
        pkg.save_private_key(key, path, PASS)
        self.assertIn(b"ENCRYPTED PRIVATE KEY", path.read_bytes())
        self.assertEqual(pkg.public_hex(pkg.load_private_key(path, PASS)), pkg.public_hex(key))
        with self.assertRaisesRegex(pkg.PackageError, "Λάθος κωδικός"):
            pkg.load_private_key(path, "wrong passphrase!")
        with self.assertRaises(FileExistsError):
            pkg.save_private_key(key, path, PASS)

    def test_keygen_sign_verify_tools(self):
        keys = self.root / "keys"
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"TEST_PASS": PASS}), mock.patch("sys.stdout", new=out):
            self.assertEqual(release_keygen.main(["--out", str(keys),
                                                  "--passphrase-env", "TEST_PASS"]), 0)
        public = (keys / "sch-intercom-release-key.pub.txt").read_text().strip()
        self.assertIn(public, out.getvalue())
        tree = make_tree(self.root, "1.2.0")
        with mock.patch.dict(os.environ, {"TEST_PASS": PASS}), \
                mock.patch("sys.stdout", new=io.StringIO()) as printed:
            self.assertEqual(release_sign.main(["--key", str(keys / release_keygen.KEY_NAME),
                                                "--tree", str(tree), "--out", str(self.root),
                                                "--passphrase-env", "TEST_PASS"]), 0)
        self.assertIn("ΠΡΟΣΟΧΗ", printed.getvalue())       # κλειδί δοκιμής, όχι στο repo
        package = self.root / "sch-intercom-client-1.2.0.sicu"
        with mock.patch("sys.stdout", new=io.StringIO()) as printed:
            self.assertEqual(release_verify.main([str(package), "--public-key", public]), 0)
            self.assertEqual(release_verify.main([str(package), "--public-key", public,
                                                  "--installed", "1.2.0"]), 1)
            self.assertEqual(release_verify.main([str(package)]), 1)   # όχι το κλειδί του repo
        self.assertIn("ΕΓΚΥΡΟ: έκδοση 1.2.0", printed.getvalue())
        with mock.patch.dict(os.environ, {"TEST_PASS": "wrong passphrase!"}), \
                mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(release_sign.main(["--key", str(keys / release_keygen.KEY_NAME),
                                                "--tree", str(tree), "--out", str(self.root),
                                                "--passphrase-env", "TEST_PASS"]), 1)

    def test_keygen_refuses_repository_folder(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", new=io.StringIO()):
            release_keygen.main(["--out", str(release_keygen.ROOT / "keys")])

    def test_trusted_keys_are_valid_hex(self):
        self.assertEqual(len(TRUSTED_KEYS), 1)
        for key in TRUSTED_KEYS:
            self.assertRegex(key, r"^[0-9a-f]{64}$")
        pkg._public_keys(TRUSTED_KEYS)                  # έγκυρα σημεία Ed25519


if __name__ == "__main__":
    unittest.main()
