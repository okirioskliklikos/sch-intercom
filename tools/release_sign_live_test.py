"""Αυτόματη πραγματική δοκιμή υπογραφής εκδόσεων (Φάση 5B1, docs/phase5.md §7).

Φτιάχνει πραγματικό δέντρο έκδοσης (embeddable Python + wheels Windows, όπως το
build_release), κλειδί δοκιμής σε προσωρινό φάκελο και πακέτο .sicu, και ελέγχει ότι
γίνεται δεκτό μόνο το σωστό: λάθος κλειδί, λάθος κωδικός, αλλοιωμένο αρχείο ή
manifest, downgrade και νεότερος updater απορρίπτονται. Το πραγματικό κλειδί του
συντηρητή δεν χρησιμοποιείται. Χρειάζεται internet (ή --tree με έτοιμο δέντρο).

    Windows:  powershell -ExecutionPolicy Bypass -File tools\\release_sign_live_test.ps1
    Linux:    tools/release_sign_live_test.sh
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import package as pkg  # noqa: E402
from shared.version import APP_VERSION, parse_version  # noqa: E402
from tools import build_release, release_keygen, release_sign  # noqa: E402

TOTAL = 10
PASSPHRASE = "δοκιμή-υπογραφής-5B1"


def bump(version: str) -> str:
    major, minor, patch = parse_version(version)
    return f"{major}.{minor}.{patch + 1}"


class LiveTest:
    def __init__(self, base: Path, tree: Path | None, embed_zip: Path | None):
        self.base = base
        self.tree = tree
        self.embed_zip = embed_zip
        self.version = bump(APP_VERSION)
        self.results: list[tuple[str, bool, str]] = []
        self.key_path = base / "keys" / release_keygen.KEY_NAME
        self.public: str | None = None
        self.package: Path | None = None

    def result(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, ok, detail))
        text = f"{'PASS' if ok else 'FAIL'} {len(self.results):02d}/{TOTAL}: {name}"
        print(f"\n=== {text}" + (f" — {detail}" if detail else "") + "\n", flush=True)

    def step(self, name: str, action) -> None:
        try:
            outcome = action()
            ok, detail = outcome if isinstance(outcome, tuple) else (bool(outcome), "")
        except Exception as exc:  # η δοκιμή αναφέρει, δεν κρύβει
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        self.result(name, ok, detail)

    def rejected(self, path: Path, expected: str, **options) -> tuple[bool, str]:
        options.setdefault("trusted_keys", [self.public])
        try:
            pkg.verify_package(path, **options)
        except pkg.PackageError as exc:
            return expected in str(exc), f"απορρίφθηκε: {exc}"
        return False, "έγινε δεκτό"

    def copy_with(self, name: str, change) -> Path:
        target = self.base / name
        with zipfile.ZipFile(self.package) as old, \
                zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as new:
            for info in old.infolist():
                new.writestr(info.filename, change(info.filename, old.read(info)))
        return target

    # ------------------------------------------------------------------ βήματα
    def s01_tree(self):
        if self.tree is None:
            self.tree = build_release.build_tree(self.base / "dist", self.version,
                                                 python_version=platform.python_version(),
                                                 embed_zip=self.embed_zip)
        files = pkg.tree_files(self.tree)
        python = (self.tree / "python" / "pythonw.exe").exists()
        return python and len(files) > 100, f"{self.tree} ({len(files)} αρχεία)"

    def s02_keygen(self):
        os.environ["SCH_LIVE_PASS"] = PASSPHRASE
        code = release_keygen.main(["--out", str(self.key_path.parent),
                                    "--passphrase-env", "SCH_LIVE_PASS"])
        self.public = (self.key_path.parent / "sch-intercom-release-key.pub.txt") \
            .read_text().strip()
        encrypted = b"ENCRYPTED PRIVATE KEY" in self.key_path.read_bytes()
        return code == 0 and encrypted, f"κρυπτογραφημένο: {encrypted}"

    def s03_sign(self):
        self.package = release_sign.sign_tree(self.tree, self.key_path, self.base,
                                              passphrase=PASSPHRASE, version=self.version)
        return self.package.exists(), f"{self.package.name}, {self.package.stat().st_size / 1e6:.1f} MB"

    def s04_verify(self):
        manifest = pkg.verify_package(self.package, trusted_keys=[self.public],
                                      installed_version=APP_VERSION)
        same = len(manifest["files"]) == len(pkg.tree_files(self.tree))
        return same and manifest["version"] == self.version, \
            f"έκδοση {manifest['version']}, {len(manifest['files'])} αρχεία"

    def s05_wrong_key(self):
        return self.rejected(self.package, "Άκυρη υπογραφή",
                             trusted_keys=[pkg.public_hex(pkg.generate_key())])

    def s06_wrong_passphrase(self):
        try:
            release_sign.sign_tree(self.tree, self.key_path, self.base / "wrong",
                                   passphrase="λάθος κωδικός!!", version=self.version)
        except pkg.PackageError as exc:
            return "Λάθος κωδικός" in str(exc), str(exc)
        return False, "υπέγραψε με λάθος κωδικό"

    def s07_tampered_file(self):
        target = "files/app/client/main.py"

        def change(name, data):
            # Ένα byte αλλαγμένο, ίδιο μέγεθος: το πιάνει μόνο το SHA-256.
            return data[:-1] + bytes([data[-1] ^ 1]) if name == target else data
        return self.rejected(self.copy_with("tampered.sicu", change),
                             "Αλλοιωμένο αρχείο: app/client/main.py")

    def s08_tampered_manifest(self):
        def change(name, data):
            if name == pkg.MANIFEST:
                manifest = json.loads(data)
                manifest["files"][0]["sha256"] = "0" * 64
                return pkg.manifest_bytes(manifest)
            return data
        return self.rejected(self.copy_with("manifest.sicu", change), "Άκυρη υπογραφή")

    def s09_versions(self):
        same = self.rejected(self.package, "δεν είναι νεότερη", installed_version=self.version)
        updater = self.rejected(self.package, "updater", updater_version=0)
        return same[0] and updater[0], f"{same[1]} | {updater[1]}"

    def s10_extract(self):
        dest = self.base / "versions" / self.version
        manifest = pkg.extract_package(self.package, dest, trusted_keys=[self.public],
                                       installed_version=APP_VERSION)
        for item in manifest["files"]:
            if (dest / item["path"]).read_bytes() != (self.tree / item["path"]).read_bytes():
                return False, f"διαφέρει: {item['path']}"
        return True, f"{len(manifest['files'])} αρχεία ίδια με το δέντρο"

    def run(self) -> int:
        steps = [
            ("Πραγματικό δέντρο έκδοσης (embeddable Python + wheels)", self.s01_tree),
            ("Κλειδί δοκιμής, κρυπτογραφημένο με κωδικό", self.s02_keygen),
            ("Υπογραφή → .sicu", self.s03_sign),
            ("Επαλήθευση όπως στον Client", self.s04_verify),
            ("Λάθος κλειδί: απόρριψη", self.s05_wrong_key),
            ("Λάθος κωδικός: δεν υπογράφει", self.s06_wrong_passphrase),
            ("Αλλοιωμένο αρχείο: απόρριψη", self.s07_tampered_file),
            ("Αλλοιωμένο manifest: απόρριψη", self.s08_tampered_manifest),
            ("Ίδια έκδοση και νεότερος updater: απόρριψη", self.s09_versions),
            ("Αποσυμπίεση: ίδια αρχεία με το δέντρο", self.s10_extract),
        ]
        for index, (name, action) in enumerate(steps):
            self.step(name, action)
            if index < 3 and not self.results[-1][1]:
                for later, _ in steps[index + 1:]:
                    self.result(later, False, "δεν έγινε το προηγούμενο βήμα")
                break
        passed = sum(1 for _, ok, _ in self.results if ok)
        print(f"\nΣΥΝΟΛΟ: {passed}/{TOTAL} PASS", flush=True)
        return 0 if passed == TOTAL else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Πραγματική δοκιμή υπογραφής εκδόσεων")
    parser.add_argument("--tree", type=Path, default=None, help="Έτοιμο δέντρο έκδοσης")
    parser.add_argument("--embed-zip", type=Path, default=None)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)
    base = Path(tempfile.mkdtemp(prefix="sch-sign-test-"))
    try:
        return LiveTest(base, args.tree, args.embed_zip).run()
    finally:
        os.environ.pop("SCH_LIVE_PASS", None)
        if not args.keep:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
