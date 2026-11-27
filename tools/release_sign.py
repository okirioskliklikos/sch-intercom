"""Φάση 5B1: υπογραφή δέντρου έκδοσης → dist/sch-intercom-client-<έκδοση>.sicu.

    python tools/release_sign.py --key <κλειδί.pem> [--tree dist/release/<έκδοση>]

Ζητά τον κωδικό του κλειδιού (δεν τον αποθηκεύει), φτιάχνει το πακέτο και το επαληθεύει
αμέσως. Προειδοποιεί αν το κλειδί δεν είναι στο shared/release_keys.py (τότε οι Clients
θα απορρίψουν το πακέτο).
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.package import (PackageError, UPDATER_VERSION, load_private_key,  # noqa: E402
                            public_hex, verify_package, write_package)
from shared.release_keys import TRUSTED_KEYS  # noqa: E402
from shared.version import APP_VERSION, parse_version  # noqa: E402


def package_name(version: str) -> str:
    return f"sch-intercom-client-{version}.sicu"


def sign_tree(tree: Path, key_path: Path, out_dir: Path, *, passphrase: str,
              version: str | None = None, min_updater: int = UPDATER_VERSION,
              report=print) -> Path:
    tree = Path(tree)
    if version is None:
        version = (tree / "VERSION").read_text(encoding="ascii").strip()
    parse_version(version)
    key = load_private_key(key_path, passphrase)
    out = Path(out_dir) / package_name(version)
    manifest = write_package(tree, out, key, version, min_updater=min_updater)
    verify_package(out, trusted_keys=[public_hex(key)])
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    report(f"Πακέτο: {out} ({out.stat().st_size / 1_000_000:.1f} MB, "
           f"{len(manifest['files'])} αρχεία), SHA-256 {digest}")
    if public_hex(key) not in TRUSTED_KEYS:
        report("ΠΡΟΣΟΧΗ: το κλειδί δεν είναι στο shared/release_keys.py· οι Clients "
               "θα απορρίψουν αυτό το πακέτο.")
    return out


def ask_passphrase(env: str | None) -> str:
    if env:
        return os.environ.get(env, "")
    return getpass.getpass("Κωδικός κλειδιού υπογραφής: ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Υπογραφή έκδοσης Client (.sicu)")
    parser.add_argument("--key", type=Path, required=True, help="Ιδιωτικό κλειδί (PEM)")
    parser.add_argument("--tree", type=Path, default=None,
                        help=f"Δέντρο έκδοσης (προεπιλογή dist/release/{APP_VERSION})")
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--min-updater", type=int, default=UPDATER_VERSION)
    parser.add_argument("--passphrase-env", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    tree = args.tree or ROOT / "dist" / "release" / APP_VERSION
    try:
        sign_tree(tree, args.key, args.out, passphrase=ask_passphrase(args.passphrase_env),
                  min_updater=args.min_updater)
    except (OSError, ValueError, PackageError) as exc:
        print(f"Η υπογραφή απέτυχε: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
