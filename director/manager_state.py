"""Τοπικός ρόλος και προτίμηση εξυπηρέτησης Manager (4.2D1).

Δεν μεταφέρεται σε backup: κάθε υπολογιστής κρατά τη δική του απόφαση.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from shared.client_registry import RegistryError, _atomic_write, canonical

STATE_FILE = "manager-state.json"
ROLE_MESSAGE = ("Ο υπολογιστής είναι Εφεδρικός. "
                "Οι αλλαγές υπολογιστών γίνονται μόνο στον Κύριο.")
PROMOTE_WARNING = ("Αν και ο άλλος υπολογιστής μείνει Κύριος, τα μητρώα θα "
                   "αποκλίνουν και θα χρειαστεί συγχώνευση. Συνεχίστε μόνο αν "
                   "ο άλλος δεν χρησιμοποιείται πια ή θα γίνει Εφεδρικός.")


class RoleError(RegistryError):
    """Απαγορευμένη τροποποίηση μητρώου από Εφεδρικό Manager."""


@dataclass(frozen=True)
class ManagerState:
    role: str = "primary"
    serving: bool = True
    notice: str | None = None
    changed_at: str | None = None

    def __post_init__(self):
        if self.role not in ("primary", "backup") or type(self.serving) is not bool:
            raise ValueError("Μη έγκυρη κατάσταση Manager")
        if self.notice not in (None, "restored_as_backup"):
            raise ValueError("Μη έγκυρη ειδοποίηση Manager")


def path_for(registry_file: Path) -> Path:
    return Path(registry_file).parent / STATE_FILE


def load(path: Path) -> ManagerState:
    """Ελλείπον αρχείο: Κύριος. Κατεστραμμένο αρχείο: ασφαλής Εφεδρικός."""
    path = Path(path)
    if not path.exists():
        return ManagerState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) != {
                "format", "role", "serving", "notice", "changed_at"}:
            raise ValueError("Invalid keys")
        if type(raw["format"]) is not int or raw["format"] != 1:
            raise ValueError("Unknown state format")
        if not isinstance(raw["changed_at"], str):
            raise ValueError("Invalid timestamp")
        return ManagerState(raw["role"], raw["serving"],
                            raw["notice"], raw["changed_at"])
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        print("[Manager] Μη έγκυρο manager-state.json· "
              "ασφαλής λειτουργία Εφεδρικού με σταματημένη εξυπηρέτηση.",
              file=sys.stderr, flush=True)
        return ManagerState(role="backup", serving=False)


def save(path: Path, state: ManagerState) -> None:
    timestamp = datetime.now(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    data = {"format": 1, "role": state.role, "serving": state.serving,
            "notice": state.notice, "changed_at": timestamp}
    _atomic_write(Path(path), canonical(data) + b"\n")


def require_primary(state: ManagerState | Path) -> None:
    if isinstance(state, Path):
        state = load(state)
    if state.role != "primary":
        raise RoleError(ROLE_MESSAGE)


def change_role(path: Path, role: str) -> ManagerState:
    if role not in ("primary", "backup"):
        raise ValueError("Άγνωστος ρόλος")
    current = load(path)
    updated = replace(current, role=role, notice=None)
    save(path, updated)
    return updated


def change_serving(path: Path, serving: bool) -> ManagerState:
    current = load(path)
    updated = replace(current, serving=serving)
    save(path, updated)
    return updated


def acknowledge(path: Path) -> ManagerState:
    current = load(path)
    updated = replace(current, notice=None)
    save(path, updated)
    return updated


def restored(path: Path) -> ManagerState:
    state = ManagerState(role="backup", serving=False,
                         notice="restored_as_backup")
    save(path, state)
    return state


def describe(state: ManagerState) -> str:
    role = "Κύριος" if state.role == "primary" else "Εφεδρικός"
    serving = "ενεργή" if state.serving else "σταματημένη"
    message = f"[Manager] Ρόλος: {role}\n[Manager] Εξυπηρέτηση: {serving}"
    if state.notice == "restored_as_backup":
        message += ("\n[Manager] Επαναφορά από backup: ο υπολογιστής είναι "
                    "Εφεδρικός χωρίς εξυπηρέτηση. Για αντικατάσταση του Κύριου "
                    "αλλάξτε ρόλο και ξεκινήστε την εξυπηρέτηση.")
    return message


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Τοπική κατάσταση Manager")
    parser.add_argument("command", choices=(
        "status", "primary", "backup", "serve-on", "serve-off", "notice-ok"))
    parser.add_argument("--registry-file", type=Path,
                        default=Path.home() / ".school-intercom" / "clients-registry.json")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    from director.backup import (BackupError, BackupLocked,
                                 ensure_no_interrupted_restore, manager_lock)
    state_path = path_for(args.registry_file)
    try:
        ensure_no_interrupted_restore(state_path.parent)
        if args.command == "status":
            print(describe(load(state_path)))
            return 0
        if args.command == "primary":
            print(PROMOTE_WARNING)
        if not args.yes:
            if input("Πληκτρολογήστε ΝΑΙ για επιβεβαίωση: ").strip() != "ΝΑΙ":
                return 5
        with manager_lock(state_path.parent):
            ensure_no_interrupted_restore(state_path.parent)
            if args.command in ("primary", "backup"):
                state = change_role(state_path, args.command)
            elif args.command in ("serve-on", "serve-off"):
                state = change_serving(state_path, args.command == "serve-on")
            else:
                state = acknowledge(state_path)
        print(describe(state))
        return 0
    except BackupLocked:
        print("Ο Manager τρέχει· χρησιμοποιήστε τις εντολές του "
              "(status, role, registry …).", file=sys.stderr)
        return 4
    except (BackupError, OSError, ValueError) as exc:
        print(f"Σφάλμα: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
