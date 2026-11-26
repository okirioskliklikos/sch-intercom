"""4.4 βήμα 2: φορητό zip του βοηθού δοκιμής για υπολογιστές Client χωρίς εγκατάσταση.

Τρέχει στον υπολογιστή του χρήστη (με internet) και φτιάχνει το
``dist/sch-intercom-client.zip``:

    python/        ενσωματωμένη Python για Windows (embeddable, x64)
      Lib/site-packages/   sounddevice, cryptography, pycaw (wheels για win_amd64)
    app/           client, shared, director, tools (μόνο .py)
    run-client.bat διπλό κλικ: τρέχει τον βοηθό της δοκιμής 4.4
    README.txt     οδηγίες (ελληνικά, UTF-8)

Στο σχολικό PC: αποσυμπίεση οπουδήποτε, διπλό κλικ στο run-client.bat. Δεν χρειάζεται
εγκατάσταση, admin ή internet. Ο βοηθός συνδέεται μόνο προς τα έξω (καμία ερώτηση firewall).

Τα wheels κατεβαίνουν για Windows από οποιοδήποτε λειτουργικό (pip --platform), άρα
το script δοκιμάζεται και στο Linux με ``--embed-zip`` (τοπικό αρχείο).
"""
from __future__ import annotations

import argparse
import io
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("client", "shared", "director", "tools")
REQUIREMENTS = ("sounddevice>=0.5,<1", "cryptography>=42", "pycaw")
EMBED_URL = "https://www.python.org/ftp/python/{version}/python-{version}-embed-amd64.zip"
ZIP_NAME = "sch-intercom-client.zip"
FOLDER = "sch-intercom-client"

#: Μόνο ASCII: το cmd διαβάζει το .bat με την κωδικοσελίδα OEM.
RUN_CLIENT_BAT = r"""@echo off
rem sch-intercom 4.4: network test helper (Client). Double click, or: run-client.bat --director-host IP
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
"%~dp0python\python.exe" "%~dp0app\tools\network_live_test.py" client %*
echo.
pause
"""

README = """sch-intercom: φορητός βοηθός για τη δοκιμή δικτύου 4.4 (Client)

1. Αποσυμπιέστε τον φάκελο οπουδήποτε (π.χ. στην Επιφάνεια εργασίας).
2. Στον υπολογιστή του Director ξεκινήστε πρώτα τη δοκιμή:
   powershell -ExecutionPolicy Bypass -File tools\\network_live_test.ps1 -Role director -Clients 2
3. Εδώ: διπλό κλικ στο run-client.bat.
   Αν δεν βρεθεί ο Director σε ~10 s, θα ζητηθεί η IP του (τη δείχνει ο Director
   στην αρχή). Ή απευθείας: run-client.bat --director-host 192.168.x.y
4. Αφήστε τα ηχεία ανοιχτά. Η δοκιμή διαρκεί ~12 λεπτά· όλα τα αποτελέσματα
   εμφανίζονται στον Director.

Δεν χρειάζεται εγκατάσταση, δικαιώματα διαχειριστή ή internet. Ο βοηθός μόνο
συνδέεται προς τον Director. Τα προσωρινά κλειδιά δοκιμής σβήνονται στο τέλος.
Για αφαίρεση: διαγραφή του φακέλου.
"""


def embed_url(version: str) -> str:
    return EMBED_URL.format(version=version)


def candidate_versions(version: str) -> list[str]:
    """Η ίδια έκδοση και οι προηγούμενες του ίδιου κλάδου (π.χ. 3.13.17 → … → 3.13.0).

    Οι εκδόσεις «μόνο ασφάλειας» δεν έχουν embeddable zip· τα wheels cp313 ταιριάζουν
    σε κάθε 3.13.x, άρα αρκεί η νεότερη που υπάρχει.
    """
    major, minor, micro = (int(part) for part in version.split(".")[:3])
    return [f"{major}.{minor}.{patch}" for patch in range(micro, -1, -1)]


def url_exists(url: str) -> bool:
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status == 200
    except urllib.error.HTTPError:
        return False


def find_embed(version: str, exists=url_exists) -> str:
    for candidate in candidate_versions(version):
        if exists(embed_url(candidate)):
            return candidate
    raise SystemExit(f"Δεν βρέθηκε embeddable Python για {version} ή παλιότερη 3.x.y "
                     "στο python.org· δώστε --python-version ή --embed-zip")


def patch_pth(text: str) -> str:
    """python3XX._pth: + site-packages, + ../app, ενεργό το import site."""
    lines = [line.strip() for line in text.splitlines()
             if line.strip() and not line.strip().startswith("# ")]
    lines = [line for line in lines if line.lstrip("#").strip() != "import site"]
    for extra in ("Lib\\site-packages", "..\\app"):
        if extra not in lines:
            lines.append(extra)
    lines.append("import site")
    return "\n".join(lines) + "\n"


def code_files(root: Path = ROOT) -> list[Path]:
    """Αρχεία .py (και .ps1/.sh των tools) των πακέτων, χωρίς tests και cache."""
    found = []
    for package in PACKAGES:
        for path in sorted((root / package).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix == ".py" or (package == "tools" and path.suffix in (".ps1", ".sh")):
                found.append(path.relative_to(root))
    return found


def install_packages(target: Path, python_version: str) -> None:
    major_minor = ".".join(python_version.split(".")[:2])
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-compile",
                    "--only-binary=:all:", "--platform", "win_amd64",
                    "--python-version", major_minor, "--implementation", "cp",
                    "--target", str(target), *REQUIREMENTS], check=True)


def unpack_embed(data: bytes, target: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        archive.extractall(target)
    pth = sorted(target.glob("python3*._pth"))
    if not pth:
        raise SystemExit("Το embeddable zip δεν έχει αρχείο python3XX._pth")
    pth[0].write_text(patch_pth(pth[0].read_text(encoding="utf-8")), encoding="utf-8")


def build(out_dir: Path, *, python_version: str, embed_zip: Path | None = None,
          root: Path = ROOT, report=print) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out_dir) as work:
        stage = Path(work) / FOLDER
        python_dir = stage / "python"
        if embed_zip is not None:
            report(f"Ενσωματωμένη Python από {embed_zip}")
            data = Path(embed_zip).read_bytes()
        else:
            version = find_embed(python_version)
            report(f"Λήψη ενσωματωμένης Python {version} (x64) από python.org...")
            with urllib.request.urlopen(embed_url(version), timeout=120) as response:
                data = response.read()
        unpack_embed(data, python_dir)
        report("Λήψη πακέτων για Windows: " + ", ".join(REQUIREMENTS))
        install_packages(python_dir / "Lib" / "site-packages", python_version)
        files = code_files(root)
        report(f"Αντιγραφή κώδικα ({len(files)} αρχεία)")
        for relative in files:
            destination = stage / "app" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / relative, destination)
        (stage / "run-client.bat").write_bytes(RUN_CLIENT_BAT.replace("\n", "\r\n")
                                              .encode("utf-8"))
        (stage / "README.txt").write_bytes(
            b"\xef\xbb\xbf" + README.replace("\n", "\r\n").encode("utf-8"))
        result = out_dir / ZIP_NAME
        if result.exists():
            result.unlink()
        with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    archive.write(path, path.relative_to(stage.parent).as_posix())
    report(f"Έτοιμο: {result} ({result.stat().st_size / 1_000_000:.1f} MB)")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Φορητό zip του βοηθού δοκιμής 4.4 (Client)")
    parser.add_argument("--python-version", default=platform.python_version(),
                        help="Έκδοση Python 3.x.y (προεπιλογή: αυτή που τρέχει το script)")
    parser.add_argument("--embed-zip", type=Path,
                        help="Τοπικό python-3.x.y-embed-amd64.zip αντί για λήψη")
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    args = parser.parse_args(argv)
    if not args.python_version.startswith("3."):
        parser.error("Χρειάζεται Python 3.x.y")
    build(args.out, python_version=args.python_version, embed_zip=args.embed_zip)
    return 0


if __name__ == "__main__":
    sys.exit(main())
