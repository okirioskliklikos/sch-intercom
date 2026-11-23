"""Λογική (χωρίς Qt) για το tab «Πιστοποιητικά» και τη σελίδα Ρυθμίσεις → «Συντήρηση».

Ό,τι δείχνουν και κάνουν οι σελίδες βρίσκεται εδώ, ώστε να ελέγχεται χωρίς παράθυρο.
Το backup και η επαναφορά χρησιμοποιούν τις ίδιες συναρτήσεις με το CLI
(`director/backup.py`, docs/phase4-2c-backup.md). Κανένα κείμενο εδώ δεν περιέχει
ιδιωτικό κλειδί ή κωδικό backup.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import time
import unicodedata

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

from director import backup
from director import registry_sync
from director.manager_state import load as load_state, path_for as state_path
from director.security import ago_text
from shared.client_registry import digest, merge, verify_document
from shared.manager_key import ManagerKeyError, is_protected, key_password

PASSPHRASE_MIN = backup.PASSWORD_MIN
NO_EXPIRY_YEAR = 9999
PRE_RESTORE_PREFIX = "pre-restore-"

# Κείμενα (ίδια με τα mockups του PR, ui-style.md §6–§8).
TEXT_NO_EXPIRY = "Χωρίς λήξη"
TEXT_KEY_DPAPI = "Κρυπτογραφημένο (DPAPI)"
TEXT_KEY_PASSFILE = "Κρυπτογραφημένο (αρχείο κωδικού)"
TEXT_KEY_PLAIN = "Χωρίς κρυπτογράφηση"
TEXT_KEY_BROKEN = "Δεν ανοίγει σε αυτόν τον χρήστη"
TEXT_SIGNATURE_OK = "υπογραφή έγκυρη"
TEXT_SIGNATURE_BAD = "άκυρη υπογραφή"
TEXT_NO_CHANGES = "καμία αλλαγή ✓"
TEXT_SAME_IDENTITY = "ίδια με αυτόν τον Manager ✓"
TEXT_OTHER_IDENTITY = "άλλη από αυτόν τον Manager"
TEXT_NEW_INSTALL = "νέα εγκατάσταση"
TEXT_SECRET_REMINDER = "Χωρίς τον κωδικό το backup δεν ανοίγει. Φυλάξτε τον χωριστά από το αρχείο."
TEXT_PASS_SHORT = f"Τουλάχιστον {PASSPHRASE_MIN} χαρακτήρες"
TEXT_PASS_MISMATCH = "Οι κωδικοί δεν ταιριάζουν"
TEXT_PASS_MATCH = "ίδιος ✓"
TEXT_RESTART_NOTE = ("Ο Manager σταματά, γίνεται η επαναφορά και ξεκινά ξανά. "
                     "Οι Clients επανασυνδέονται μόνοι τους.")
TEXT_ACCEPT_OLDER = "Συνέχεια με το παλαιότερο μητρώο"
TEXT_REPLACE_IDENTITY = "Αντικατάσταση της ταυτότητας Manager"
TEXT_PREVIOUS_HINT = "περιέχει το προηγούμενο ιδιωτικό κλειδί"
TEXT_UNEXPORTED_NONE = "καμία αλλαγή ✓"
TEXT_SAME_MANAGER = "ίδιος Manager ✓"
TEXT_ALREADY_CURRENT = "Το μητρώο είναι ήδη ενημερωμένο."
TEXT_REVOCATIONS_KEEP = "Οι ανακλήσεις και των δύο ισχύουν πάντα."
REGISTRY_FILTER = "Μητρώο School Intercom (*.sicr)"
RESTORE_STEPS = ("Διακοπή εξυπηρέτησης Clients", "Έλεγχος και εγκατάσταση αρχείων",
                 "Επανεκκίνηση Manager")


def local_time(value: datetime | str | float) -> datetime:
    """UTC (ISO «…Z», datetime ή epoch) → τοπική ώρα του υπολογιστή."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif not isinstance(value, datetime):
        value = datetime.fromtimestamp(value, timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone()


def date_text(value: datetime | str | float) -> str:
    return local_time(value).strftime("%d/%m/%Y %H:%M")


def fingerprint_lines(fingerprint: str) -> str:
    """Αποτύπωμα SHA-256 (32 bytes) σε δύο γραμμές των 16, όπως στο mockup."""
    parts = fingerprint.split(":")
    half = (len(parts) + 1) // 2
    return ":".join(parts[:half]) + "\n" + ":".join(parts[half:])


MASK = "\u2022\u2022"


def fingerprint_masked(fingerprint: str) -> str:
    """Κρυμμένο αποτύπωμα (προεπιλογή): όλο το μήκος σε δύο γραμμές, με φανερά μόνο τα
    2 πρώτα και τα 2 τελευταία bytes· τα υπόλοιπα «••» (απόφαση χρήστη 23/11/2026)."""
    parts = fingerprint.split(":")
    if len(parts) <= 4:
        return fingerprint_lines(fingerprint)
    shown = [p if i < 2 or i >= len(parts) - 2 else MASK for i, p in enumerate(parts)]
    return fingerprint_lines(":".join(shown))


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def changes_text(count: int) -> str:
    return TEXT_NO_CHANGES if count <= 0 else _plural(count, "αλλαγή μητρώου", "αλλαγές μητρώου")


def size_text(size: int) -> str:
    if size < 1024 * 1024:
        return f"{max(1, round(size / 1024))} KB"
    return f"{size / (1024 * 1024):.1f} MB".replace(".", ",")


def passphrase_problem(first: str, second: str | None = None) -> str | None:
    """None αν ο κωδικός γίνεται δεκτός· αλλιώς το μήνυμα για τον διάλογο."""
    first = unicodedata.normalize("NFC", first)
    if len(first) < PASSPHRASE_MIN:
        return TEXT_PASS_SHORT
    if second is not None and unicodedata.normalize("NFC", second) != first:
        return TEXT_PASS_MISMATCH
    return None


def default_backup_name(now: datetime | None = None) -> str:
    return "sch-intercom-backup-" + (now or datetime.now()).strftime("%Y%m%d-%H%M") + backup.EXTENSION


# --- Πιστοποιητικά ------------------------------------------------------------
@dataclass(frozen=True)
class ManagerIdentity:
    name: str
    fingerprint: str
    created: datetime
    expires: datetime | None          # None = χωρίς λήξη (9999-12-31)
    algorithm: str
    key_state: str                    # dpapi, password_file, plain, broken
    registry_version: int | None
    approved: int
    signature_ok: bool
    folder: Path

    @property
    def key_ok(self) -> bool:
        return self.key_state in ("dpapi", "password_file")

    def key_text(self) -> str:
        return {"dpapi": TEXT_KEY_DPAPI, "password_file": TEXT_KEY_PASSFILE,
                "plain": TEXT_KEY_PLAIN}.get(self.key_state, TEXT_KEY_BROKEN)

    def expires_text(self) -> str:
        return TEXT_NO_EXPIRY if self.expires is None else date_text(self.expires)

    def registry_text(self) -> str:
        if self.registry_version is None:
            return "—"
        return f"v{self.registry_version} · " + _plural(self.approved, "υπολογιστής",
                                                         "υπολογιστές")


def _algorithm(public_key) -> str:
    if isinstance(public_key, ec.EllipticCurvePublicKey):
        names = {"secp256r1": "P-256", "secp384r1": "P-384", "secp521r1": "P-521"}
        return "ECDSA " + names.get(public_key.curve.name, public_key.curve.name)
    if isinstance(public_key, rsa.RSAPublicKey):
        return f"RSA {public_key.key_size}"
    return type(public_key).__name__


def key_state(tls_key: Path) -> str:
    try:
        if not is_protected(Path(tls_key).read_bytes()):
            return "plain"
        key_password(tls_key)
    except (OSError, ManagerKeyError):
        return "broken"
    return "dpapi" if os.name == "nt" else "password_file"


def manager_identity(tls_cert: Path, tls_key: Path, registry_file: Path) -> ManagerIdentity:
    """Ταυτότητα του Manager από τα αρχεία του. Δεν διαβάζει/δείχνει το ιδιωτικό κλειδί."""
    cert_pem = Path(tls_cert).read_bytes()
    certificate = x509.load_pem_x509_certificate(cert_pem)
    try:
        names = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        names = []
    if not names:
        names = [a.value for a in certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)]
    expires = certificate.not_valid_after_utc
    version, approved, signature_ok = None, 0, False
    try:
        document = json.loads(Path(registry_file).read_text(encoding="utf-8"))
        registry = verify_document(document, cert_pem)
        version = registry["registry_version"]
        approved = sum(1 for c in registry["clients"] if c.get("status") == "approved")
        signature_ok = True
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return ManagerIdentity(
        name=names[0] if names else "—",
        fingerprint=certificate.fingerprint(hashes.SHA256()).hex(":").upper(),
        created=certificate.not_valid_before_utc,
        expires=None if expires.year >= NO_EXPIRY_YEAR else expires,
        algorithm=_algorithm(certificate.public_key()),
        key_state=key_state(tls_key),
        registry_version=version, approved=approved, signature_ok=signature_ok,
        folder=Path(tls_cert).resolve().parent)


# --- Συντήρηση ----------------------------------------------------------------
@dataclass(frozen=True)
class RestoreRequest:
    """Ό,τι χρειάζεται η επαναφορά μετά την επιβεβαίωση στον διάλογο."""
    path: Path
    password: str
    replace_identity: bool = False
    accept_older: bool = False
    restore_media: bool = False

    def __repr__(self) -> str:  # ο κωδικός δεν εμφανίζεται ποτέ σε log/traceback
        return (f"RestoreRequest(path={str(self.path)!r}, replace_identity={self.replace_identity}, "
                f"accept_older={self.accept_older}, restore_media={self.restore_media})")


class MaintenanceModel:
    """Αρχεία του Manager και οι ενέργειες της «Συντήρησης», πάνω στο `director.backup`.

    ``registry_version``: τρέχουσα έκδοση μητρώου (από το ζωντανό μητρώο του Manager)·
    αν λείπει διαβάζεται από τον δίσκο.
    """

    def __init__(self, registry_file: Path, tls_cert: Path, tls_key: Path, *,
                 media_dir: Path | None = None,
                 registry_version: Callable[[], int | None] | None = None,
                 importer: Callable[..., dict] | None = None,
                 clock: Callable[[], float] = time.time, kdf_n: int = backup.KDF_N):
        self.registry_file = Path(registry_file)
        self.tls_cert = Path(tls_cert)
        self.tls_key = Path(tls_key)
        self.data_dir = self.registry_file.parent
        self.media_dir = Path(media_dir) if media_dir else self.data_dir / "media"
        self._registry_version = registry_version
        # Εισαγωγή μητρώου στον Manager που τρέχει (RegistryTrust.import_registry):
        # ξαναφτιάχνει το mTLS context και αποσυνδέει όσους δεν είναι πια εγκεκριμένοι.
        self.importer = importer
        self.clock = clock
        self.kdf_n = kdf_n  # χαμηλότερο μόνο στα tests

    # --- πιστοποιητικά ----------------------------------------------------
    def identity(self) -> ManagerIdentity:
        return manager_identity(self.tls_cert, self.tls_key, self.registry_file)

    # --- κατάσταση --------------------------------------------------------
    def registry_version(self) -> int | None:
        if self._registry_version is not None:
            return self._registry_version()
        try:
            document = json.loads(self.registry_file.read_text(encoding="utf-8"))
            return document["registry"]["registry_version"]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _read_json(self, name: str) -> dict | None:
        try:
            value = json.loads((self.data_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def last_backup(self) -> dict | None:
        """{when, file_name, changes} από το last-backup.json, ή None."""
        info = self._read_json(backup.LAST_BACKUP)
        if not info or "time" not in info:
            return None
        current = self.registry_version()
        changes = None
        if current is not None and isinstance(info.get("registry_version"), int):
            changes = max(0, current - info["registry_version"])
        when = local_time(info["time"])
        return {"when": f"{date_text(when)} · {ago_text(self.clock() - when.timestamp())}",
                "file_name": str(info.get("file_name", "")), "changes": changes}

    def last_restore(self) -> dict | None:
        info = self._read_json(backup.LAST_RESTORE)
        if not info or "time" not in info:
            return None
        text = date_text(info["time"])
        if isinstance(info.get("registry_version"), int):
            text += f" · v{info['registry_version']}"
        return {"when": text, "file_name": str(info.get("file_name", ""))}

    def previous_folders(self) -> list[Path]:
        """Φάκελοι pre-restore-* (περιέχουν το προηγούμενο ιδιωτικό κλειδί), νεότεροι πρώτα."""
        if not self.data_dir.is_dir():
            return []
        return sorted((p for p in self.data_dir.glob(PRE_RESTORE_PREFIX + "*") if p.is_dir()),
                      key=lambda p: p.name, reverse=True)

    def delete_previous(self) -> int:
        folders = self.previous_folders()
        for folder in folders:
            shutil.rmtree(folder)
        return len(folders)

    def media_files(self) -> tuple[int, int]:
        """(πλήθος, συνολικό μέγεθος) των MP3 του φακέλου MP3."""
        if not self.media_dir.is_dir():
            return 0, 0
        files = [p for p in self.media_dir.iterdir()
                 if p.is_file() and not p.is_symlink() and p.suffix.lower() == ".mp3"]
        return len(files), sum(p.stat().st_size for p in files)

    # --- ενέργειες --------------------------------------------------------
    def create_backup(self, out: Path, password: str, confirm: str, *,
                      copy_media: bool) -> tuple[Path, list[str]]:
        problem = passphrase_problem(password, confirm)
        if problem:
            raise backup.BackupError(problem)
        return backup.create(Path(out), password, registry_file=self.registry_file,
                             tls_cert=self.tls_cert, tls_key=self.tls_key,
                             media_dir=self.media_dir,
                             media_copy="yes" if copy_media else "no", kdf_n=self.kdf_n)

    def preview(self, path: Path, password: str) -> dict:
        return backup.preview(Path(path), password, registry_file=self.registry_file,
                              tls_cert=self.tls_cert, media_dir=self.media_dir)

    def restore(self, request: RestoreRequest, *, fail_after: int | None = None) -> dict:
        """Χρειάζεται ελεύθερο manager.lock: ο Manager πρέπει να έχει σταματήσει."""
        return backup.restore(request.path, request.password,
                              registry_file=self.registry_file, tls_cert=self.tls_cert,
                              tls_key=self.tls_key, media_dir=self.media_dir,
                              replace_identity=request.replace_identity,
                              accept_older=request.accept_older,
                              restore_media=request.restore_media, fail_after=fail_after)


    # --- μητρώο υπολογιστών (εξαγωγή/εισαγωγή, phase4-2d.md §6–§7) ----------
    def primary(self) -> bool:
        return load_state(state_path(self.registry_file)).role == "primary"

    def registry_card(self) -> dict:
        """{registry, last_export, unexported}: κείμενα της κάρτας «Μητρώο υπολογιστών».

        Το «Από τότε» (μη εξαγμένες αλλαγές) υπάρχει μόνο στον Κύριο και μετρά από την
        τελευταία εξαγωγή ή το τελευταίο backup (phase4-2d.md §6).
        """
        identity = self.identity()
        export = self._read_json(registry_sync.LAST_EXPORT)
        last = None
        if export and "time" in export:
            last = date_text(export["time"])
            if isinstance(export.get("registry_version"), int):
                last += f" · v{export['registry_version']}"
        unexported = None
        if self.primary():
            try:
                count = registry_sync.status(self.registry_file, self.tls_cert)["unexported"]
            except (registry_sync.SyncError, OSError, ValueError):
                count = None
            unexported = (TEXT_UNEXPORTED_NONE, "ok") if count == 0 else (
                ("ποτέ", "err") if count is None else
                (_plural(count, "αλλαγή που δεν έχει εξαχθεί",
                         "αλλαγές που δεν έχουν εξαχθεί"), "err"))
        return {"registry": identity.registry_text(), "last_export": last,
                "unexported": unexported}

    def export_registry(self, out: Path) -> Path:
        return registry_sync.export_registry(self.registry_file, self.tls_cert, Path(out))

    def default_export_name(self) -> str:
        version = self.registry_version() or 0
        return (f"sch-intercom-registry-v{version}-"
                + datetime.now().strftime("%Y%m%d-%H%M") + ".sicr")

    def import_view(self, source: Path, *, prefer: str = "local") -> "ImportView":
        """Έλεγχος και προεπισκόπηση πριν από την εισαγωγή. Σφάλματα: SyncError."""
        source = Path(source)
        document, local, kind, _ = registry_sync.examine(
            self.registry_file, self.tls_cert, source, prefer=prefer, allow_merge=True)
        incoming = document["registry"]
        result = incoming
        if kind == "conflict":
            result, _report = merge(local, incoming, prefer=prefer)
        return import_view(source, local, incoming, result, kind, prefer)

    def apply_import(self, view: "ImportView") -> dict:
        if self.importer is None:
            raise registry_sync.SyncError("Η εισαγωγή χρειάζεται τον Manager σε λειτουργία.")
        return self.importer(view.path, merge_conflict=view.relation == "conflict",
                             prefer=view.prefer, expected_digest=view.expected_digest)


@dataclass(frozen=True)
class ImportView:
    """Τι θα αλλάξει με την εισαγωγή (διάλογος «Εισαγωγή μητρώου»)."""
    path: Path
    relation: str                     # same | newer | conflict
    prefer: str
    expected_digest: str
    version_text: str
    version_tone: str | None
    added: tuple[str, ...] = ()
    revoked: tuple[str, ...] = ()
    moved: tuple[str, ...] = ()
    renamed: tuple[str, ...] = ()
    keys: tuple[str, ...] = ()
    differences: tuple[str, ...] = ()

    @property
    def changes(self) -> list[tuple[str, tuple[str, ...]]]:
        """Γραμμές προεπισκόπησης με τη σειρά του mockup· μόνο όσες έχουν στοιχεία."""
        rows = [("Προστίθενται", self.added), ("Ανακαλούνται", self.revoked),
                ("Αλλάζουν αίθουσα", self.moved), ("Μετονομάζονται", self.renamed),
                ("Αλλάζουν κλειδί", self.keys), ("Διαφορές", self.differences)]
        return [(title, items) for title, items in rows if items]


def _computer_label(entry: dict) -> str:
    return f'{entry["room"]} · {entry.get("name") or entry["client_id"][:8]}'


def import_view(path: Path, local: dict, incoming: dict, result: dict, kind: str,
                prefer: str = "local") -> ImportView:
    """Καθαρή συνάρτηση: σύγκριση τοπικού μητρώου με το αποτέλεσμα της εισαγωγής."""
    old = {e["client_id"]: e for e in local["clients"]}
    theirs = {e["client_id"]: e for e in incoming["clients"]}
    here, there = local["registry_version"], incoming["registry_version"]
    if kind == "same":
        version = (f"v{there} · ίδιο με αυτόν τον υπολογιστή", "muted")
    elif kind == "newer":
        version = (f"v{there} · εδώ v{here} · νεότερο ✓", "ok")
    else:
        version = (f"v{there} · εδώ v{here} · έχουν αποκλίνει", "err")
    added, revoked, moved, renamed, keys, differences = [], [], [], [], [], []
    for entry in result["clients"]:
        before = old.get(entry["client_id"])
        if before is None:
            if entry["status"] == "approved":
                added.append(_computer_label(entry))
            continue
        if before["status"] == "approved" and entry["status"] != "approved":
            revoked.append(_computer_label(before))
            continue
        if entry["status"] != "approved":
            continue
        if before["room"] != entry["room"]:
            target = (entry["room"] if before.get("name") == entry.get("name")
                      else _computer_label(entry))
            moved.append(f"{_computer_label(before)} → {target}")
        elif before.get("name") != entry.get("name"):
            renamed.append(f"{_computer_label(before)} → {entry.get('name', '')}")
        if before["status"] == "approved" and before["spki_sha256"] != entry["spki_sha256"]:
            keys.append(_computer_label(entry))
    if kind == "conflict":
        for client_id, mine in old.items():
            other = theirs.get(client_id)
            if other is None:
                continue
            parts = []
            if mine["room"] != other["room"]:
                parts.append(f'αίθουσα {mine["room"]} εδώ, {other["room"]} στο αρχείο')
            if mine.get("name") != other.get("name"):
                parts.append(f'όνομα {mine.get("name", "")} εδώ, '
                             f'{other.get("name", "")} στο αρχείο')
            if parts:
                differences.append(f"{_computer_label(mine)}: " + "; ".join(parts))
    return ImportView(Path(path), kind, prefer, digest(local), *version,
                      tuple(added), tuple(revoked), tuple(moved), tuple(renamed),
                      tuple(keys), tuple(differences))


def preview_rows(info: dict) -> list[tuple[str, str, str | None]]:
    """Γραμμές σύνοψης του διαλόγου επαναφοράς: (ετικέτα, κείμενο, τόνος ok/err/muted)."""
    registry = (f"v{info['registry_version']} · "
                + _plural(info.get("approved", info["clients"]), "υπολογιστής", "υπολογιστές")
                + " · " + _plural(info["pending"], "αίτημα", "αιτήματα"))
    identity = {"same": (TEXT_SAME_IDENTITY, "ok"), "different": (TEXT_OTHER_IDENTITY, "err"),
                "none": (TEXT_NEW_INSTALL, "muted")}[info["identity"]]
    rows = [("Δημιουργήθηκε", f"{date_text(info['created_at'])} · {info['source_computer']}", None),
            ("Μητρώο", registry, None),
            ("Ταυτότητα", identity[0], identity[1])]
    media = info["media"]
    if info["mp3"]:
        text = _plural(info["mp3"], "αρχείο", "αρχεία")
        problems = []
        if media["missing"]:
            problems.append(("λείπει " if len(media["missing"]) == 1 else "λείπουν ")
                            + str(len(media["missing"])))
        if media["different"]:
            problems.append(("διαφέρει " if len(media["different"]) == 1 else "διαφέρουν ")
                            + str(len(media["different"])))
        rows.append(("MP3", text + (" · " + ", ".join(problems) if problems else " ✓"),
                     "err" if problems else None))
    return rows


def preview_warning(info: dict) -> tuple[str, str] | None:
    """(κείμενο προειδοποίησης, κείμενο επιβεβαίωσης) όταν χρειάζεται ρητή συναίνεση."""
    if info["identity"] == "different":
        return ("Αυτός ο υπολογιστής έχει άλλη ταυτότητα Manager. Οι Clients που είναι "
                "εγγεγραμμένοι σε αυτήν θα σταματήσουν να συνδέονται εδώ.", TEXT_REPLACE_IDENTITY)
    local, version = info.get("local_registry_version"), info["registry_version"]
    if info["relation"] == "older":
        return (f"Το backup έχει παλαιότερο μητρώο (v{version} < v{local}). Εγκρίσεις και "
                f"ανακλήσεις μετά τη v{version} θα χαθούν.", TEXT_ACCEPT_OLDER)
    if info["relation"] == "conflict":
        return (f"Το μητρώο του backup (v{version}) δεν προέρχεται από το τρέχον (v{local}). "
                "Οι αλλαγές του τρέχοντος μητρώου θα χαθούν.", TEXT_ACCEPT_OLDER)
    return None
