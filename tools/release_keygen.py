"""Φάση 5B1: δημιουργία κλειδιού υπογραφής εκδόσεων Client (μία φορά).

Τρέχει ΜΟΝΟ στον υπολογιστή του συντηρητή:

    python tools/release_keygen.py --out <φάκελος εκτός repository>

Γράφει το ιδιωτικό κλειδί κρυπτογραφημένο με κωδικό (PKCS#8, ≥ 12 χαρακτήρες) και τυπώνει
το δημόσιο κλειδί για το shared/release_keys.py. Κρατήστε αντίγραφο του αρχείου και του
κωδικού σε ξεχωριστό ασφαλές μέσο: χωρίς αυτά δεν βγαίνουν νέες εκδόσεις για τους
εγκατεστημένους Clients. Το κλειδί δεν μπαίνει ποτέ στο repository ή στον Manager.
"""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.package import MIN_PASSPHRASE, generate_key, public_hex, save_private_key  # noqa: E402

KEY_NAME = "sch-intercom-release-key.pem"


def ask_passphrase(env: str | None) -> str:
    if env:
        value = os.environ.get(env, "")
        if not value:
            raise SystemExit(f"Η μεταβλητή {env} είναι κενή")
        return value
    first = getpass.getpass(f"Νέος κωδικός κλειδιού (≥ {MIN_PASSPHRASE} χαρακτήρες): ")
    second = getpass.getpass("Ξανά ο κωδικός: ")
    if first != second:
        raise SystemExit("Οι κωδικοί δεν ταιριάζουν")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Νέο κλειδί υπογραφής εκδόσεων Client")
    parser.add_argument("--out", type=Path, required=True,
                        help="Φάκελος για το ιδιωτικό κλειδί (εκτός repository)")
    parser.add_argument("--passphrase-env", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    out = args.out.resolve()
    if out == ROOT or ROOT in out.parents:
        parser.error("Το κλειδί δεν μπαίνει μέσα στο repository· δώστε άλλο φάκελο")
    path = out / KEY_NAME
    if path.exists():
        parser.error(f"Υπάρχει ήδη κλειδί: {path}")
    passphrase = ask_passphrase(args.passphrase_env)
    key = generate_key()
    try:
        save_private_key(key, path, passphrase)
    except ValueError as exc:
        parser.error(str(exc))
    public = public_hex(key)
    (out / "sch-intercom-release-key.pub.txt").write_text(public + "\n", encoding="ascii")
    print(f"Ιδιωτικό κλειδί: {path}")
    print("Κρατήστε αντίγραφο του αρχείου και του κωδικού σε ξεχωριστό ασφαλές μέσο.")
    print(f"Δημόσιο κλειδί (για το shared/release_keys.py):\n{public}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
