"""4.2D3: ο Client θυμάται τη μεγαλύτερη έκδοση μητρώου που είδε (phase4-2.md §4).

Μία εγγραφή ανά Manager, με κλειδί το SHA-256 του πιστοποιητικού TLS του (ίδιο
σε Κύριο και Εφεδρικό της ίδιας εγκατάστασης, διαφορετικό σε άλλη εγκατάσταση).
Το αρχείο είναι μόνο ένδειξη: χαλασμένο ή άγνωστο αρχείο σημαίνει «δεν έχω δει
τίποτα», ποτέ άρνηση σύνδεσης.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

from shared.identity import validate_seen

FORMAT = 1
#: Όριο εγγραφών ώστε το αρχείο να μη μεγαλώνει χωρίς τέλος (π.χ. εργαστήριο δοκιμών).
MAX_MANAGERS = 16


def manager_key(manager_cert_der: bytes) -> str:
    """Κλειδί εγγραφής: SHA-256 του πιστοποιητικού Manager που πέρασε τον έλεγχο TLS."""
    return hashlib.sha256(manager_cert_der).hexdigest()


def _load_all(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeError, ValueError, OSError):
        return {}
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return {}
    managers = data.get("managers")
    if not isinstance(managers, dict):
        return {}
    result = {}
    for key, value in managers.items():
        if not isinstance(key, str) or len(key) != 64:
            continue
        try:
            seen = validate_seen(value)
        except ValueError:
            continue
        if seen is not None:
            result[key] = seen
    return result


def load_seen(path: Path, key: str) -> dict | None:
    """{"rv", "hash"} της μεγαλύτερης έκδοσης που είδε ο Client σε αυτόν τον Manager."""
    return _load_all(path).get(key)


def record_seen(path: Path, key: str, welcome: dict | None) -> bool:
    """Αποθηκεύει την έκδοση του WELCOME μόνο αν είναι μεγαλύτερη. True αν άλλαξε."""
    if welcome is None:
        return False
    welcome = validate_seen(welcome)
    managers = _load_all(path)
    current = managers.get(key)
    if current is not None and current["rv"] >= welcome["rv"]:
        return False
    managers.pop(key, None)
    managers[key] = welcome  # πιο πρόσφατη στο τέλος· οι παλαιότερες φεύγουν πρώτες
    while len(managers) > MAX_MANAGERS:
        managers.pop(next(iter(managers)))
    _atomic_write_json(Path(path), {"format": FORMAT, "managers": managers})
    return True


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
