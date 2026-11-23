"""Ασφαλής εξαγωγή, σύγκριση και εισαγωγή μητρώου Manager (4.2D1)."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

from director.backup import (BackupError, BackupLocked, ensure_no_interrupted_restore,
                             manager_lock)
from director.manager_state import load as load_state, path_for, require_primary
from shared.client_registry import (
    RegistryError, SignedClientRegistry, _atomic_write, canonical, digest, merge,
    relation, verify_document,
)

MAX_IMPORT_BYTES = 4 * 1024 * 1024
LAST_EXPORT = "registry-export.json"
MERGES_LOG = "registry-merges.log"


class SyncError(ValueError):
    def __init__(self, message: str, status: int = 2):
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def _read(path: Path, cert: Path) -> tuple[bytes, dict, dict]:
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_IMPORT_BYTES:
        raise SyncError("Μη έγκυρο μέγεθος αρχείου μητρώου.")
    data = path.read_bytes()
    try:
        document = json.loads(data)
        registry = verify_document(document, Path(cert).read_bytes())
    except (ValueError, UnicodeError, OSError, TypeError) as exc:
        raise SyncError("Υπογεγραμμένο από άλλον Manager ή αλλοιωμένο.") from exc
    return data, document, registry


def status(registry_file: Path, tls_cert: Path) -> dict:
    _, _, registry = _read(registry_file, tls_cert)
    current = registry["registry_version"]
    base = Path(registry_file).parent
    versions = []
    for name in (LAST_EXPORT, "last-backup.json"):
        try:
            record = json.loads((base / name).read_text(encoding="utf-8"))
            if type(record["registry_version"]) is int and record["registry_version"] >= 1:
                versions.append(record["registry_version"])
        except (FileNotFoundError, ValueError, TypeError, KeyError):
            pass
    return {"registry_version": current,
            "unexported": max(0, current - max(versions)) if versions else None,
            "last_export": max(versions) if versions else None}


def export_registry(registry_file: Path, tls_cert: Path, out: Path) -> Path:
    content, _, registry = _read(registry_file, tls_cert)
    out = Path(out)
    if out.is_dir() or (not out.suffix and not out.exists()):
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        out = out / ("sch-intercom-registry-v" +
                     str(registry["registry_version"]) + "-" + stamp + ".sicr")
    if out.suffix.lower() != ".sicr":
        raise SyncError("Η εξαγωγή χρειάζεται επέκταση .sicr.")
    out.parent.mkdir(parents=True, exist_ok=True)
    # Η δημιουργία του τελικού ονόματος είναι αποκλειστική: ποτέ αντικατάσταση.
    fd, temp = tempfile.mkstemp(prefix=out.name + ".", suffix=".tmp", dir=out.parent)
    reserved = False
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # Η αποκλειστική κράτηση προστατεύει και από ταυτόχρονο export.
        with open(out, "xb"):
            reserved = True
        os.replace(temp, out)
    except BaseException:
        if reserved:
            out.unlink(missing_ok=True)
        raise
    finally:
        Path(temp).unlink(missing_ok=True)
    _atomic_write(Path(registry_file).parent / LAST_EXPORT, canonical({
        "time": _now(), "file_name": out.name,
        "registry_version": registry["registry_version"],
        "registry_hash": digest(registry)}) + b"\n")
    return out


def preview(local: dict, incoming: dict, *, prefer: str = "local",
            merged: dict | None = None, decisions: list | None = None) -> dict:
    old = {entry["client_id"]: entry for entry in local["clients"]}
    new_source = merged if merged is not None else incoming
    new = {entry["client_id"]: entry for entry in new_source["clients"]}
    report = {"relation": relation(local, incoming),
              "local_version": local["registry_version"],
              "incoming_version": incoming["registry_version"],
              "added": [], "revoked": [], "metadata": [], "keys": [],
              "conflicts": decisions or []}
    for cid, entry in new.items():
        old_entry = old.get(cid)
        label = f'{entry["room"]} · {entry.get("name", "")}'.strip()
        if old_entry is None:
            report["added"].append(label)
        else:
            if old_entry["status"] != entry["status"]:
                report["revoked"].append(label + " (" + entry["status"] + ")")
            if ((old_entry["room"], old_entry.get("name")) !=
                    (entry["room"], entry.get("name"))):
                report["metadata"].append(label)
            if old_entry["spki_sha256"] != entry["spki_sha256"]:
                report["keys"].append(label)
    return report


def examine(registry_file: Path, tls_cert: Path, source: Path, *,
            prefer: str = "local", allow_merge: bool = False) -> tuple[dict, dict, str, dict]:
    _, _, local = _read(registry_file, tls_cert)
    _, document, incoming = _read(source, tls_cert)
    if (incoming["school_id"] != local["school_id"] or
            incoming["schema_version"] != local["schema_version"]):
        raise SyncError("Το αρχείο ανήκει σε άλλη εγκατάσταση.", 5)
    kind = relation(local, incoming)
    if kind == "older":
        raise SyncError("Το αρχείο είναι παλαιότερο από το τοπικό μητρώο.", 5)
    if kind == "conflict" and not allow_merge:
        raise SyncError("Τα μητρώα έχουν αποκλίνει· χρειάζεται --merge.", 5)
    merged = None
    decisions = []
    if kind == "conflict":
        try:
            merged, details = merge(local, incoming, prefer=prefer)
            decisions = details["decisions"]
        except RegistryError as exc:
            raise SyncError(str(exc)) from exc
    return document, local, kind, preview(local, incoming, prefer=prefer,
                                           merged=merged, decisions=decisions)


def _import_verified(registry: SignedClientRegistry, document: dict,
                     incoming: dict, *, merge_conflict: bool,
                     prefer: str, expected_digest: str | None) -> dict:
    local = registry.snapshot()
    old_digest = digest(local)
    if expected_digest is not None and expected_digest != old_digest:
        raise SyncError("Το μητρώο άλλαξε· ξαναδοκιμάστε.")
    if incoming["school_id"] != local["school_id"]:
        raise SyncError("Το αρχείο ανήκει σε άλλη εγκατάσταση.", 5)
    kind = relation(local, incoming)
    if kind == "same":
        return {"relation": kind, "changed": False, "registry": local}
    if kind == "older":
        raise SyncError("Το αρχείο είναι παλαιότερο από το τοπικό μητρώο.", 5)
    if kind == "conflict" and not merge_conflict:
        raise SyncError("Τα μητρώα έχουν αποκλίνει· χρειάζεται συγχώνευση.", 5)
    report = None
    merged = None
    if kind == "conflict":
        merged, report = merge(local, incoming, prefer=prefer)
    actual = registry.apply_import(document, old_digest, merged=merged, prefer=prefer)
    if report is not None:
        record = {"time": _now(), "local_version": local["registry_version"],
                  "incoming_version": incoming["registry_version"],
                  "local_digest": old_digest, "incoming_digest": digest(incoming),
                  "decisions": report["decisions"]}
        with (registry.path.parent / MERGES_LOG).open("a", encoding="utf-8") as log:
            log.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"relation": kind, "changed": True, "registry": actual,
            "report": report}


def apply_from_file(registry: SignedClientRegistry, source: Path, *,
                    merge_conflict: bool = False, prefer: str = "local",
                    expected_digest: str | None = None) -> dict:
    _, document, incoming = _read_verified_source(source, registry)
    return _import_verified(registry, document, incoming,
                            merge_conflict=merge_conflict, prefer=prefer,
                            expected_digest=expected_digest)


def _read_verified_source(source: Path, registry: SignedClientRegistry):
    # Δεν μεταφέρεται το κλειδί· το πιστοποιητικό έχει ήδη φορτωθεί και επαληθευτεί.
    source = Path(source)
    if not source.is_file() or source.stat().st_size > MAX_IMPORT_BYTES:
        raise SyncError("Μη έγκυρο μέγεθος αρχείου μητρώου.")
    raw = source.read_bytes()
    try:
        doc = json.loads(raw)
        incoming = verify_document(doc, registry._cert_pem)
    except (ValueError, UnicodeError, TypeError) as exc:
        raise SyncError("Υπογεγραμμένο από άλλον Manager ή αλλοιωμένο.") from exc
    return raw, doc, incoming


def _display(preview_data: dict) -> None:
    print(f'Μητρώο τοπικό v{preview_data["local_version"]}, '
          f'εισερχόμενο v{preview_data["incoming_version"]} '
          f'({preview_data["relation"]})')
    for key, title in (("added", "Προστίθενται"),
                       ("revoked", "Ανακαλούνται/αποκλείονται"),
                       ("metadata", "Αλλαγές αίθουσας/ονόματος"),
                       ("keys", "Αλλαγές κλειδιών")):
        for entry in preview_data[key]:
            print(f"  {title}: {entry}")
    for conflict in preview_data["conflicts"]:
        print("  Σύγκρουση: " + str(conflict))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Μεταφορά υπογεγραμμένου μητρώου Manager")
    parser.add_argument("--registry-file", type=Path,
                        default=Path.home() / ".school-intercom" / "clients-registry.json")
    parser.add_argument("--tls-cert", type=Path,
                        default=Path.home() / ".school-intercom" / "tls" / "director-tls.crt")
    parser.add_argument("--tls-key", type=Path,
                        default=Path.home() / ".school-intercom" / "tls" / "director-tls.key")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    ex = commands.add_parser("export")
    ex.add_argument("--out", type=Path, required=True)
    im = commands.add_parser("import")
    im.add_argument("file", type=Path)
    im.add_argument("--merge", action="store_true")
    im.add_argument("--prefer", choices=("local", "incoming"), default="local")
    im.add_argument("--yes", action="store_true")
    for sub in (commands.choices["status"], ex, im):
        for flag in ("registry-file", "tls-cert", "tls-key"):
            sub.add_argument("--" + flag, type=Path, default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    base = args.registry_file.parent
    try:
        ensure_no_interrupted_restore(base)
        if args.command == "status":
            current = status(args.registry_file, args.tls_cert)
            print(f'[Manager] Μητρώο v{current["registry_version"]}')
            if load_state(path_for(args.registry_file)).role == "primary":
                count = current["unexported"]
                print("[Manager] Αλλαγές που δεν έχουν εξαχθεί: " +
                      ("ποτέ" if count is None else str(count)))
            return 0
        if args.command == "export":
            print("Εξαγωγή:", export_registry(args.registry_file, args.tls_cert, args.out))
            return 0

        document, local, kind, report = examine(
            args.registry_file, args.tls_cert, args.file,
            prefer=args.prefer, allow_merge=args.merge)
        _display(report)
        if kind == "same":
            print("Το μητρώο είναι ήδη ενημερωμένο.")
            return 0
        if not args.yes and input("Για εφαρμογή πληκτρολογήστε ΝΑΙ: ").strip() != "ΝΑΙ":
            return 5
        with manager_lock(base):
            ensure_no_interrupted_restore(base)
            registry = SignedClientRegistry(args.registry_file, args.tls_cert, args.tls_key)
            result = _import_verified(
                registry, document, verify_document(document, registry._cert_pem),
                merge_conflict=args.merge, prefer=args.prefer, expected_digest=digest(local))
        print(f'Εισαγωγή: {result["relation"]} — μητρώο v'
              f'{result["registry"]["registry_version"]}')
        return 0
    except BackupLocked:
        print("Ο Manager τρέχει· χρησιμοποιήστε τις εντολές του "
              "(status, role, registry …).", file=sys.stderr)
        return 4
    except SyncError as exc:
        print(str(exc), file=sys.stderr)
        return exc.status
    except BackupError as exc:
        print(str(exc), file=sys.stderr)
        return exc.status
    except (OSError, ValueError, RegistryError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
