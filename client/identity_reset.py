"""4.3C1: νέα ταυτότητα Client μετά από κλωνοποίηση ή με ``--new-identity``.

Τίποτα δεν σβήνεται: τα παλιά αρχεία μετακινούνται σε υποφάκελο δίπλα τους
(``cloned-<ημερομηνία>`` ή ``previous-identity-<ημερομηνία>``), ώστε να υπάρχει
πάντα δρόμος επιστροφής. Η νέα ταυτότητα (νέο UUID, νέο κλειδί) χρειάζεται νέα έγκριση.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import shutil

from shared.machine_binding import (Machine, binding_path, clone_reason, current_machine,
                                    load_binding, merged, save_binding)

CLONED = "cloned"
PREVIOUS = "previous-identity"


def check_clone(identity_file: Path, credentials_dir: Path, *,
                machine: Machine | None = None) -> str | None:
    """Λόγος για τον οποίο η αποθηκευμένη ταυτότητα ανήκει σε άλλον υπολογιστή, ή None.

    Χωρίς ταυτότητα: τίποτα να ελεγχθεί. Ταυτότητα από πριν το 4.3C1 (χωρίς αποτύπωμα):
    θεωρείται δική του και σημειώνεται τώρα (η προειδοποίηση του Manager μένει δίχτυ).
    """
    if not Path(identity_file).exists() or not Path(credentials_dir).exists():
        return None
    machine = machine or current_machine()
    path = binding_path(credentials_dir)
    stored = load_binding(path)
    if stored is None:
        save_binding(path, machine)
        return None
    reason = clone_reason(stored, machine)
    if reason is None:
        updated = merged(stored, machine)
        if updated != stored:
            save_binding(path, updated)
    return reason


def remember_machine(credentials_dir: Path, *, machine: Machine | None = None) -> None:
    """Μετά τη δημιουργία νέας ταυτότητας: σημειώνει τον υπολογιστή στον οποίο ανήκει."""
    path = binding_path(credentials_dir)
    if load_binding(path) is None:
        save_binding(path, machine or current_machine())


def previous_room(identity_file: Path) -> str | None:
    try:
        room = json.loads(Path(identity_file).read_text(encoding="utf-8"))["room"]
        return room if isinstance(room, str) and room != "Unassigned" else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def archive_identity(paths: list[Path], label: str, *,
                     now: datetime | None = None) -> Path | None:
    """Μετακινεί όσα από τα paths υπάρχουν σε νέο υποφάκελο δίπλα στο πρώτο path.

    Επιστρέφει τον φάκελο ή None αν δεν υπήρχε τίποτα. Δεν αντικαθιστά υπάρχοντα φάκελο.
    """
    existing = [Path(path) for path in paths if Path(path).exists()]
    if not existing:
        return None
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    base = Path(paths[0]).parent
    target = base / f"{label}-{stamp}"
    counter = 1
    while target.exists():
        counter += 1
        target = base / f"{label}-{stamp}-{counter}"
    target.mkdir(parents=True)
    for path in existing:
        destination = target / path.name
        if destination.exists():
            destination = target / f"{path.parent.name}-{path.name}"
        shutil.move(str(path), str(destination))
    # Ο φάκελος ταυτότητας στα Windows έχει ήδη ACL χρήστη· στο Linux μόνο ο χρήστης.
    if os.name != "nt":
        target.chmod(0o700)
    return target
