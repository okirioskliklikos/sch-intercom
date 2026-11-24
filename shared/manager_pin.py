"""4.3C1: ο Client θυμάται τον Manager που επαλήθευσε με TLS (ID beacon + αποτύπωμα).

Δεν είναι μηχανισμός εμπιστοσύνης· αυτό το κάνει το TLS με το ``--tls-ca``. Χρησιμεύει
ώστε μετά από επανεκκίνηση του Client τα ψεύτικα beacons άλλων ID να αγνοούνται και,
αν απαντούν δύο έγκυροι Managers, να προτιμάται αυτός που ήξερε.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import uuid

_FINGERPRINT = re.compile(r"[0-9a-f]{64}")


def load_pin(path: Path | None) -> dict | None:
    """{"director_id", "fingerprint"} ή None (χωρίς αρχείο ή χαλασμένο αρχείο)."""
    if path is None:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        director_id, fingerprint = data["director_id"], data["fingerprint"]
        if (not isinstance(director_id, str) or str(uuid.UUID(director_id)) != director_id
                or not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint)):
            return None
        return {"director_id": director_id, "fingerprint": fingerprint}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def save_pin(path: Path | None, director_id: str | None, fingerprint: str) -> bool:
    """Ατομική αποθήκευση· True αν άλλαξε κάτι."""
    if path is None or director_id is None:
        return False
    current = load_pin(path)
    wanted = {"director_id": director_id, "fingerprint": fingerprint}
    if current == wanted:
        return False
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(wanted, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True
