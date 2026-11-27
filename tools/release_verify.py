"""Φάση 5B1: έλεγχος πακέτου .sicu όπως θα τον κάνει ο Client.

    python tools/release_verify.py <πακέτο.sicu> [--installed 1.0.0] [--public-key HEX]
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.package import PackageError, verify_package  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Έλεγχος πακέτου έκδοσης Client")
    parser.add_argument("package", type=Path)
    parser.add_argument("--installed", default=None, help="Εγκατεστημένη έκδοση (για downgrade)")
    parser.add_argument("--public-key", action="append", default=None,
                        help="Δημόσιο κλειδί hex (προεπιλογή: shared/release_keys.py)")
    args = parser.parse_args(argv)
    try:
        manifest = verify_package(args.package, trusted_keys=args.public_key,
                                  installed_version=args.installed)
    except PackageError as exc:
        print(f"ΑΠΟΡΡΙΠΤΕΤΑΙ: {exc}")
        return 1
    print(f"ΕΓΚΥΡΟ: έκδοση {manifest['version']}, {len(manifest['files'])} αρχεία, "
          f"updater ≥ v{manifest['min_updater']}, πρωτόκολλο {manifest['connection_protocol']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
