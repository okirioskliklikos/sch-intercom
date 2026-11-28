"""Φάση 5: φάκελος της δοκιμής σε καθαρά Windows (tools/field_test.py, docs/phase5.md §7.1).

Τρέχει στον υπολογιστή του συντηρητή (Windows, Inno Setup 7 ή 6, internet, πραγματικό
κλειδί υπογραφής):

    powershell -ExecutionPolicy Bypass -File tools\\field_test_build.ps1 -Key <κλειδί.pem>

Αποτέλεσμα ``dist\\field-test\\`` (και ``dist\\sch-intercom-field-test.zip``):

    setup\\sch-intercom-client-0.6.0-setup.exe   ο κανονικός εγκαταστάτης, έκδοση 0.6.0
    packages\\sch-intercom-client-0.6.{1..4}.sicu  υπογεγραμμένα με το πραγματικό κλειδί:
        0.6.1 κανονική, 0.6.2 πέφτει αμέσως, 0.6.3 δεν συνδέεται, 0.6.4 κανονική
    manifests\\0.6.0.json                        λίστα αρχείων της 0.6.0 (έλεγχος καραντίνας)
    python\\, app\\                               ο βοηθός της δοκιμής (δεν εγκαθίσταται)
    field-test.cmd, field_test.ps1, README.txt

Οι εκδόσεις 0.6.x είναι πάντα παλιότερες από τις πραγματικές 1.x, άρα ακόμη κι αν ένα πακέτο
δοκιμής φτάσει σε πραγματικό Manager δεν «ενημερώνει» κανέναν Client (απόφαση 28/11/2026).
"""
from __future__ import annotations

import argparse
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import package as pkg  # noqa: E402
from shared.release_keys import TRUSTED_KEYS  # noqa: E402
from tools import build_release  # noqa: E402
from tools.field_test import (BASE, STATE_FILE, VARIANTS, make_variant, setup_file,  # noqa: E402
                              write_base_manifest)

#: Μόνο ASCII: το cmd διαβάζει το .cmd με την κωδικοσελίδα OEM.
FIELD_CMD = r"""@echo off
rem sch-intercom: field test (Windows client).
rem   field-test.cmd DIRECTOR_IP    start
rem   field-test.cmd -Resume        continue after the Windows restart (automatic via RunOnce)
rem   field-test.cmd -Cleanup       remove what an interrupted test left behind
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0field_test.ps1" %*
echo.
pause
"""

README = """sch-intercom: δοκιμή του Client σε καθαρά Windows (Φάση 5)

1. Αντιγράψτε ΟΛΟ τον φάκελο field-test (ή αποσυμπιέστε το zip) σε μόνιμη θέση, π.χ.
   στην Επιφάνεια εργασίας. Όχι μέσα από το zip: η δοκιμή συνεχίζει από εδώ μετά την
   επανεκκίνηση των Windows.
2. Στο Linux ξεκινήστε τον Director δοκιμής:   tools/field_test.sh director
   (κλείστε πρώτα τον κανονικό Director· δείχνει την IP του).
3. Εδώ: διπλό κλικ στο field-test.cmd και δώστε την IP, ή
   field-test.cmd 192.168.x.y
4. Η δοκιμή κάνει μόνη της επανεκκίνηση των Windows στη μέση. Συνδεθείτε στα Windows
   με τον ίδιο χρήστη· συνεχίζει μόνη της σε νέο παράθυρο. Στο τέλος τυπώνει PASS/FAIL
   ανά βήμα και ΣΥΝΟΛΟ, εδώ και στον Director. Η έξοδος γράφεται και στο logs\\.
5. Αν εμφανιστεί παράθυρο του antivirus για το setup.exe, κλείστε το (η δοκιμή μετρά
   πόσο κράτησε). Αν εμφανιστεί ερώτηση του Τείχους προστασίας, πατήστε Άκυρο (όπως θα
   έκανε ένας μαθητής) και σημειώστε το.

Δεν χρειάζεται admin ή internet. Αν υπάρχει ήδη %USERPROFILE%\\.school-intercom, μετακινείται
προσωρινά και επανέρχεται στο τέλος. Αν η δοκιμή διακοπεί: field-test.cmd -Cleanup
"""

#: Ο βοηθός χρειάζεται μόνο cryptography· τα μεγάλα πακέτα του Client μένουν έξω.
RUNNER_SKIP = ("PySide6", "shiboken6", "winrt", "pycaw", "comtypes", "sounddevice",
               "_sounddevice_data")


def runner_ignore(folder: str, names: list[str]) -> set[str]:
    return {name for name in names if name.startswith(RUNNER_SKIP) or name == "__pycache__"}


def runner_files(root: Path = ROOT) -> list[Path]:
    found = list(build_release.code_files(root))
    for path in sorted((root / "tools").glob("*.py")):
        found.append(path.relative_to(root))
    return found


def check_key(key_path: Path, passphrase: str) -> str:
    key = pkg.load_private_key(key_path, passphrase)
    public = pkg.public_hex(key)
    if public not in TRUSTED_KEYS:
        raise SystemExit("Το κλειδί δεν είναι στο shared/release_keys.py: οι Clients θα "
                         "απέρριπταν τα πακέτα. Δώστε το πραγματικό κλειδί έκδοσης.")
    return public


def build(out: Path, key_path: Path, passphrase: str, *, python_version: str,
          embed_zip: Path | None = None, iscc: Path | None = None, make_zip: bool = True,
          report=print, tree_builder=build_release.build_tree) -> Path:
    from tools import release_sign
    out = Path(out)
    state = out / STATE_FILE
    if state.exists() and '"rebooting"' in state.read_text(encoding="utf-8", errors="replace"):
        raise SystemExit(f"Στο {out} υπάρχει δοκιμή σε εξέλιξη (επανεκκίνηση)· δεν σβήνεται.")
    if out.exists():
        shutil.rmtree(out)
    work = out.parent / "field-test-work"
    if work.exists():
        shutil.rmtree(work)
    base = tree_builder(work, BASE, python_version=python_version, embed_zip=embed_zip)
    report(f"Δέντρο {BASE}: {base}")
    write_base_manifest(base, out / "manifests" / f"{BASE}.json")
    if iscc is not None:
        (out / "setup").mkdir(parents=True, exist_ok=True)
        subprocess.run(build_release.iscc_command(iscc, base, BASE, out / "setup"), check=True)
        report(f"Εγκαταστάτης: {out / 'setup' / setup_file(BASE)}")
    (out / "packages").mkdir(parents=True, exist_ok=True)
    for version, code in VARIANTS.items():
        tree = make_variant(base, work / "release" / version, version, code)
        release_sign.sign_tree(tree, key_path, out / "packages", passphrase=passphrase,
                               version=version, report=report)
    shutil.copytree(base / "python", out / "python", ignore=runner_ignore)
    for relative in runner_files():
        target = out / "app" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    shutil.copy2(ROOT / "tools" / "field_test.ps1", out / "field_test.ps1")
    (out / "field-test.cmd").write_bytes(FIELD_CMD.replace("\n", "\r\n").encode("ascii"))
    (out / "README.txt").write_bytes(b"\xef\xbb\xbf" + README.replace("\n", "\r\n").encode("utf-8"))
    shutil.rmtree(work, ignore_errors=True)
    if make_zip:
        archive_path = out.parent / "sch-intercom-field-test.zip"
        archive_path.unlink(missing_ok=True)
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(out.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    stored = path.suffix in (".sicu", ".exe", ".zip", ".pyd", ".dll")
                    archive.write(path, path.relative_to(out.parent).as_posix(),
                                  compress_type=zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED)
        report(f"Zip: {archive_path} ({archive_path.stat().st_size / 1_000_000:.0f} MB)")
    report(f"Έτοιμο: {out}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Φάκελος δοκιμής Client σε καθαρά Windows")
    parser.add_argument("--key", type=Path, required=True, help="Πραγματικό ιδιωτικό κλειδί (PEM)")
    parser.add_argument("--python-version", default=platform.python_version())
    parser.add_argument("--embed-zip", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=ROOT / "dist" / "field-test")
    parser.add_argument("--iscc", default=None, help="Διαδρομή ISCC.exe (Inno Setup 7 ή 6)")
    parser.add_argument("--no-zip", action="store_true")
    parser.add_argument("--passphrase-env", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args.key.exists():
        parser.error(f"Δεν βρέθηκε το κλειδί: {args.key}")
    iscc = build_release.find_iscc(args.iscc)
    if iscc is None:
        print("Δεν βρέθηκε το Inno Setup 7 ή 6 (ISCC.exe)· δώστε --iscc.", flush=True)
        return 2
    from tools import release_sign
    passphrase = release_sign.ask_passphrase(args.passphrase_env)
    check_key(args.key, passphrase)                 # πριν από τη λήψη/το build
    build(args.out, args.key, passphrase, python_version=args.python_version,
          embed_zip=args.embed_zip, iscc=iscc, make_zip=not args.no_zip)
    return 0


if __name__ == "__main__":
    sys.exit(main())
