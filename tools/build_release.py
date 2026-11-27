"""Φάση 5A: δέντρο έκδοσης Client και setup.exe (docs/phase5.md §4.1).

Τρέχει στον υπολογιστή του συντηρητή (internet για την embeddable Python και τα wheels):

    python tools/build_release.py [--version 1.0.0] [--no-setup]

Αποτέλεσμα:
    dist/release/<έκδοση>/python/   embeddable Python x64 + site-packages (wheels win_amd64)
    dist/release/<έκδοση>/app/      client/ και shared/ (μόνο ό,τι χρειάζεται ο Client)
    dist/sch-intercom-client-<έκδοση>-setup.exe   (Inno Setup· παραλείπεται χωρίς ISCC)

Η έκδοση είναι από προεπιλογή η shared/version.py. Το δέντρο φτιάχνεται και στο Linux
(--embed-zip, --no-setup) για τα tests· το setup.exe μόνο στα Windows με Inno Setup 6.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.version import APP_VERSION, parse_version  # noqa: E402
from tools import make_portable as mp  # noqa: E402

PACKAGES = ("client", "shared")
#: Εξαρτήσεις Client στα Windows (ένταση/εφαρμογές/media: docs/windows-audio-check.md).
REQUIREMENTS = ("sounddevice>=0.5,<1", "cryptography>=42", "pycaw", "comtypes", "psutil",
                "winrt-runtime", "winrt-Windows.Media.Control", "winrt-Windows.Foundation",
                "winrt-Windows.Foundation.Collections")
ISS = ROOT / "installer" / "sch-intercom-client.iss"
ISCC_CANDIDATES = (
    r"%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe",
    r"%ProgramFiles%\Inno Setup 6\ISCC.exe",
    r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe",
)


def code_files(root: Path = ROOT) -> list[Path]:
    """.py (και πόροι του shared/ui) των πακέτων του Client, χωρίς cache."""
    found = []
    for package in PACKAGES:
        for path in sorted((root / package).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix in (".py", ".svg"):
                found.append(path.relative_to(root))
    return found


def install_packages(target: Path, python_version: str) -> None:
    major_minor = ".".join(python_version.split(".")[:2])
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-compile",
                    "--only-binary=:all:", "--platform", "win_amd64",
                    "--python-version", major_minor, "--implementation", "cp",
                    "--target", str(target), *REQUIREMENTS], check=True)


def build_tree(out_dir: Path, version: str, *, python_version: str,
               embed_zip: Path | None = None, root: Path = ROOT,
               install=install_packages, report=print) -> Path:
    parse_version(version)
    tree = Path(out_dir) / "release" / version
    if tree.exists():
        shutil.rmtree(tree)
    python_dir = tree / "python"
    if embed_zip is not None:
        report(f"Ενσωματωμένη Python από {embed_zip}")
        data = Path(embed_zip).read_bytes()
    else:
        found = mp.find_embed(python_version)
        report(f"Λήψη ενσωματωμένης Python {found} (x64) από python.org...")
        with urllib.request.urlopen(mp.embed_url(found), timeout=120) as response:
            data = response.read()
    mp.unpack_embed(data, python_dir)
    report("Λήψη πακέτων για Windows: " + ", ".join(REQUIREMENTS))
    install(python_dir / "Lib" / "site-packages", python_version)
    files = code_files(root)
    report(f"Αντιγραφή κώδικα Client ({len(files)} αρχεία)")
    for relative in files:
        destination = tree / "app" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, destination)
    (tree / "VERSION").write_text(version + "\n", encoding="ascii")
    return tree


def find_iscc(explicit: str | None = None) -> Path | None:
    if explicit:
        return Path(explicit) if Path(explicit).exists() else None
    found = shutil.which("ISCC") or shutil.which("iscc")
    if found:
        return Path(found)
    for candidate in ISCC_CANDIDATES:
        path = Path(os.path.expandvars(candidate))
        if "%" not in str(path) and path.exists():
            return path
    return None


def iscc_command(iscc: Path, tree: Path, version: str, out_dir: Path) -> list[str]:
    return [str(iscc), f"/DAppVersion={version}", f"/DSourceDir={tree.resolve()}",
            f"/O{Path(out_dir).resolve()}", f"/Fsch-intercom-client-{version}-setup", str(ISS)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Έκδοση Client: δέντρο αρχείων και setup.exe")
    parser.add_argument("--version", default=APP_VERSION, help=f"Έκδοση (προεπιλογή {APP_VERSION})")
    parser.add_argument("--python-version", default=platform.python_version())
    parser.add_argument("--embed-zip", type=Path, help="Τοπικό python-3.x.y-embed-amd64.zip")
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--iscc", default=None, help="Διαδρομή ISCC.exe (Inno Setup 6)")
    parser.add_argument("--no-setup", action="store_true", help="Μόνο το δέντρο, χωρίς setup.exe")
    args = parser.parse_args(argv)
    try:
        parse_version(args.version)
    except ValueError as exc:
        parser.error(str(exc))
    tree = build_tree(args.out, args.version, python_version=args.python_version,
                      embed_zip=args.embed_zip)
    print(f"Δέντρο έκδοσης: {tree}", flush=True)
    if args.no_setup:
        return 0
    iscc = find_iscc(args.iscc)
    if iscc is None:
        print("Δεν βρέθηκε το Inno Setup 6 (ISCC.exe)· δεν φτιάχτηκε setup.exe. "
              "Εγκαταστήστε το από https://jrsoftware.org/isdl.php ή δώστε --iscc.", flush=True)
        return 2
    subprocess.run(iscc_command(iscc, tree, args.version, args.out), check=True)
    setup = Path(args.out) / f"sch-intercom-client-{args.version}-setup.exe"
    print(f"Έτοιμο: {setup} ({setup.stat().st_size / 1_000_000:.1f} MB)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
