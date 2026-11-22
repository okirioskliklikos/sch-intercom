"""School Intercom 4.2C — encrypted portable Manager backups.

The .sib file contains only ciphertext after an authenticated public header.
No key material or passphrase is printed, and no plaintext staging is placed
outside the Manager's protected data directory.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import getpass
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import sys
import time
import unicodedata
import uuid

from cryptography import x509
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from shared.client_registry import canonical, digest, verify_document
from shared.device_identity import _write_secret

MAGIC = b"SIBK"
FORMAT = 1
MAX_FILE = 16 * 1024 * 1024
MAX_HEADER = 4096
KDF_N = 1 << 17
ERROR_SECRET = "Λάθος κωδικός ή αλλοιωμένο αρχείο. Ελέγξτε και τη γλώσσα του πληκτρολογίου."


def _app_version() -> str:
    try:
        return version("school-intercom")
    except PackageNotFoundError:
        return "0.1.0"  # pyproject.toml when running directly from source


FILES = {
    "enrollment_pending": "enrollment-pending.json",
    "last_seen": "clients-last-seen.json",
    "director_id": "director.id",
    "gui_settings": "director-gui.json",
}
JSON_SECTIONS = {"enrollment_pending", "last_seen", "gui_settings"}
SECTION_NAMES = frozenset({"tls_identity", "client_registry", *FILES, "media_manifest"})


class BackupError(ValueError):
    def __init__(self, message: str, status: int = 2):
        super().__init__(message)
        self.status = status


class BackupLocked(BackupError):
    def __init__(self):
        super().__init__("Ο Manager τρέχει. Κλείστε τον πρώτα.", 4)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _un64(text: str) -> bytes:
    return base64.b64decode(text, validate=True)


def _json(data: bytes) -> dict:
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    return value


def _password(value: str, *, strict: bool = True) -> bytes:
    value = unicodedata.normalize("NFC", value)
    if strict and len(value) < 12:
        raise BackupError("Ο κωδικός χρειάζεται τουλάχιστον 12 χαρακτήρες.")
    return value.encode("utf-8")


def _key(password: bytes, salt: bytes, n: int, p: int = 1) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=8, p=p).derive(password)


def _pair(cert: bytes, secret: bytes) -> str:
    certificate = x509.load_pem_x509_certificate(cert)
    key = serialization.load_pem_private_key(secret, password=None)
    public_a = certificate.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    public_b = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if public_a != public_b:
        raise BackupError("Το ιδιωτικό κλειδί δεν ταιριάζει με το πιστοποιητικό.")
    return certificate.fingerprint(hashes.SHA256()).hex(":").upper()


def _paths(registry_file: Path, tls_cert: Path, tls_key: Path) -> dict[str, dict[str, Path]]:
    base = registry_file.parent
    return {
        "tls_identity": {"cert.pem": tls_cert, "key.pem": tls_key},
        "client_registry": {"clients-registry.json": registry_file},
        **{name: {filename: base / filename} for name, filename in FILES.items()},
    }


def _read_sections(registry_file: Path, tls_cert: Path, tls_key: Path) -> tuple[dict, dict]:
    sections = {}
    for name, paths in _paths(registry_file, tls_cert, tls_key).items():
        if name in ("tls_identity", "client_registry") or all(p.exists() for p in paths.values()):
            raw = {}
            for filename, path in paths.items():
                raw[filename] = path.read_bytes()
                if name in JSON_SECTIONS:
                    try:
                        _json(raw[filename])
                    except ValueError:
                        # Μία επανάληψη: ο Manager μπορεί να γράφει το αρχείο εκείνη τη στιγμή.
                        time.sleep(0.2)
                        raw[filename] = path.read_bytes()
                        _json(raw[filename])
            sections[name] = {"schema": 1, "files": {k: _b64(v) for k, v in raw.items()}}
    cert = _un64(sections["tls_identity"]["files"]["cert.pem"])
    secret = _un64(sections["tls_identity"]["files"]["key.pem"])
    fingerprint = _pair(cert, secret)
    doc = _json(_un64(sections["client_registry"]["files"]["clients-registry.json"]))
    registry = verify_document(doc, cert)
    return sections, {
        "fingerprint": fingerprint, "registry": registry,
        "registry_hash": digest(registry),
    }


def _media_entries(media_dir: Path) -> list[dict]:
    entries = []
    if not media_dir.is_dir():
        return entries
    for path in sorted(media_dir.iterdir()):
        if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".mp3":
            continue
        content = path.read_bytes()
        entries.append({"name": path.name, "size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(), "role": None})
    return entries


def _validate_contents(contents: dict) -> tuple[dict, dict, list[str]]:
    manifest = contents["manifest"]
    sections = contents["sections"]
    if not isinstance(manifest, dict) or not isinstance(sections, dict):
        raise BackupError("Μη έγκυρο περιεχόμενο backup.")
    if manifest.get("backup_format", 0) > FORMAT:
        raise BackupError("Το backup δημιουργήθηκε από νεότερη έκδοση. Αναβαθμίστε πρώτα τον Manager.")
    if manifest.get("backup_format") != FORMAT:
        raise BackupError("Μη υποστηριζόμενη μορφή backup.")
    if not isinstance(manifest.get("sections"), dict):
        raise BackupError("Μη έγκυρο manifest.")
    for mandatory in ("tls_identity", "client_registry"):
        if mandatory not in sections:
            raise BackupError("Το backup δεν περιέχει υποχρεωτική ενότητα: " + mandatory)
    warnings = []
    validated = {}
    for name, section in sections.items():
        if name not in SECTION_NAMES:
            warnings.append("Αγνοήθηκε: " + name)
            continue
        if not isinstance(section, dict) or type(section.get("schema")) is not int:
            raise BackupError("Μη έγκυρη ενότητα backup.")
        if section["schema"] != 1:
            if name in ("tls_identity", "client_registry"):
                raise BackupError("Μη υποστηριζόμενη υποχρεωτική ενότητα.")
            warnings.append("Αγνοήθηκε: " + name)
            continue
        if name not in manifest["sections"] or not isinstance(manifest["sections"][name], dict):
            raise BackupError("Λείπει hash ενότητας.")
        if hashlib.sha256(canonical(section)).hexdigest() != manifest["sections"][name]["sha256"]:
            raise BackupError("Ασυμφωνία ακεραιότητας ενότητας.")
        if name == "media_manifest":
            entries = section.get("files", [])
            if not isinstance(entries, list) or any(
                    not isinstance(e, dict) or not isinstance(e.get("name"), str)
                    or Path(e["name"]).name != e["name"] or e["name"] in (".", "..")
                    or not isinstance(e.get("sha256"), str)
                    or len(e["sha256"]) != 64
                    or type(e.get("size")) is not int or e["size"] < 0
                    for e in entries):
                raise BackupError("Μη έγκυρο manifest MP3.")
        else:
            if not isinstance(section.get("files"), dict):
                raise BackupError("Μη έγκυρα αρχεία ενότητας.")
            required_files = _paths(Path("clients-registry.json"),
                                    Path("cert.pem"), Path("key.pem"))[name]
            if set(section["files"]) != set(required_files):
                raise BackupError("Μη έγκυρα ονόματα αρχείων ενότητας.")
        validated[name] = section
    cert = _un64(validated["tls_identity"]["files"]["cert.pem"])
    secret = _un64(validated["tls_identity"]["files"]["key.pem"])
    fingerprint = _pair(cert, secret)
    doc = _json(_un64(validated["client_registry"]["files"]["clients-registry.json"]))
    registry = verify_document(doc, cert)
    if (manifest["tls_fingerprint"] != fingerprint
            or manifest["school_id"] != registry["school_id"]
            or manifest["registry_hash"] != digest(registry)
            or manifest["registry_version"] != registry["registry_version"]):
        raise BackupError("Το manifest δεν συμφωνεί με την ταυτότητα ή το μητρώο.")
    for name in JSON_SECTIONS.intersection(validated):
        for encoded in validated[name]["files"].values():
            _json(_un64(encoded))
    return manifest, validated, warnings


def _write_exclusive(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _replace_with_retry(source: Path, target: Path) -> None:
    for attempt in range(3):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.5)


def create(out: Path, password: str, *, registry_file: Path, tls_cert: Path,
           tls_key: Path, media_dir: Path, media_copy: str = "no",
           kdf_n: int = KDF_N) -> tuple[Path, list[str]]:
    secret = _password(password)
    sections, info = _read_sections(registry_file, tls_cert, tls_key)
    media = _media_entries(media_dir)
    if media:
        sections["media_manifest"] = {
            "schema": 1, "folder": str(media_dir), "files": media}
    created = _now()
    manifest = {
        "backup_format": FORMAT, "created_at": created, "app_version": _app_version(),
        "source_computer": socket.gethostname(), "school_id": info["registry"]["school_id"],
        "registry_version": info["registry"]["registry_version"],
        "registry_hash": info["registry_hash"], "tls_fingerprint": info["fingerprint"],
        "sections": {name: {"schema": sec["schema"],
                            "sha256": hashlib.sha256(canonical(sec)).hexdigest()}
                     for name, sec in sections.items()},
    }
    payload = canonical({"manifest": manifest, "sections": sections})
    salt, nonce = os.urandom(16), os.urandom(12)
    header = canonical({
        "format": FORMAT, "created_at": created, "app_version": _app_version(),
        "kdf": {"name": "scrypt", "n": kdf_n, "r": 8, "p": 1, "salt": _b64(salt)},
        "cipher": {"name": "AES-256-GCM", "nonce": _b64(nonce)},
    })
    prefix = MAGIC + struct.pack(">I", len(header)) + header
    encrypted = prefix + AESGCM(_key(secret, salt, kdf_n)).encrypt(nonce, payload, prefix)
    if len(encrypted) > MAX_FILE:
        raise BackupError("Το backup υπερβαίνει τα 16 MiB.")
    out = Path(out)
    if out.is_dir() or not out.suffix:
        out = out / ("sch-intercom-backup-" + datetime.now().strftime("%Y%m%d-%H%M") + ".sib")
    if out.exists() or out.with_name(out.name + ".tmp").exists():
        raise BackupError("Το backup υπάρχει ήδη· δεν αντικαθίσταται.")
    warnings = []
    try:
        if out.resolve().is_relative_to(registry_file.parent.resolve()):
            warnings.append("Το backup είναι στον ίδιο δίσκο με τα δεδομένα. Προτιμήστε USB ή άλλο υπολογιστή.")
    except ValueError:
        pass
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_name(out.name + ".tmp")
    try:
        _write_exclusive(temp, encrypted)
        _replace_with_retry(temp, out)
    finally:
        temp.unlink(missing_ok=True)
    if media:
        warnings.append(f"Ο φάκελος MP3 έχει {len(media)} αρχεία που δεν μπαίνουν στο κρυπτογραφημένο backup.")
        if media_copy == "yes":
            target_dir = out.with_suffix("").with_name(out.stem + "-media")
            target_dir.mkdir(exist_ok=False)
            for entry in media:
                destination = target_dir / entry["name"]
                shutil.copyfile(media_dir / entry["name"], destination)
                if hashlib.sha256(destination.read_bytes()).hexdigest() != entry["sha256"]:
                    raise BackupError("Αποτυχία επαλήθευσης αντιγράφου MP3.")
    from shared.client_registry import _atomic_write
    last = registry_file.parent / "last-backup.json"
    _atomic_write(last, canonical({"time": created, "file_name": out.name,
                                   "registry_version": manifest["registry_version"],
                                   "registry_hash": manifest["registry_hash"]}))
    return out, warnings


def _read_backup(path: Path, password: str | None = None) -> tuple[dict, dict | None]:
    if path.stat().st_size > MAX_FILE:
        raise BackupError("Το backup υπερβαίνει τα 16 MiB.")
    raw = path.read_bytes()
    if len(raw) < 8 or raw[:4] != MAGIC:
        raise BackupError("Μη έγκυρη μορφή αρχείου.")
    size = struct.unpack(">I", raw[4:8])[0]
    if size > MAX_HEADER or len(raw) < 8 + size + 16:
        raise BackupError("Μη έγκυρη κεφαλίδα backup.")
    try:
        header = _json(raw[8:8 + size])
        if header.get("format", 0) > FORMAT:
            raise BackupError("Το backup δημιουργήθηκε από νεότερη έκδοση. Αναβαθμίστε πρώτα τον Manager.")
        if header["format"] != FORMAT:
            raise BackupError("Μη υποστηριζόμενη μορφή backup.")
        cfg, cipher = header["kdf"], header["cipher"]
        n, rr, pp = cfg["n"], cfg["r"], cfg["p"]
        if (cfg["name"] != "scrypt" or type(n) is not int or n < 1 << 15
                or n > 1 << 20 or n & (n - 1) or rr != 8
                or type(pp) is not int or not 1 <= pp <= 4
                or cipher["name"] != "AES-256-GCM"):
            raise BackupError("Μη αποδεκτές παράμετροι κρυπτογράφησης.")
        salt, nonce = _un64(cfg["salt"]), _un64(cipher["nonce"])
        if len(salt) != 16 or len(nonce) != 12:
            raise BackupError("Μη έγκυρες παράμετροι κρυπτογράφησης.")
    except (KeyError, ValueError, TypeError) as exc:
        if isinstance(exc, BackupError):
            raise
        raise BackupError("Μη έγκυρη κεφαλίδα backup.") from exc
    if password is None:
        return header, None
    try:
        decrypted = AESGCM(_key(_password(password, strict=False), salt, n, pp)).decrypt(
            nonce, raw[8 + size:], raw[:8 + size])
    except (InvalidTag, ValueError):
        raise BackupError(ERROR_SECRET, 3) from None
    try:
        return header, _json(decrypted)
    except (ValueError, UnicodeError) as exc:
        raise BackupError("Μη έγκυρο περιεχόμενο backup.") from exc


def inspect(path: Path, password: str | None = None) -> dict:
    header, contents = _read_backup(Path(path), password)
    if contents is None:
        return {"header": header}
    manifest, sections, warnings = _validate_contents(contents)
    registry = _json(_un64(sections["client_registry"]["files"]["clients-registry.json"]))["registry"]
    return {
        "header": header,
        "summary": {"created_at": manifest["created_at"],
                    "source_computer": manifest["source_computer"],
                    "registry_version": manifest["registry_version"],
                    "clients": len(registry["clients"]),
                    "pending": len(_json(_un64(sections["enrollment_pending"]["files"][FILES["enrollment_pending"]])).get("requests", []))
                    if "enrollment_pending" in sections else 0,
                    "tls_fingerprint": manifest["tls_fingerprint"],
                    "sections": sorted(sections),
                    "mp3": len(sections.get("media_manifest", {}).get("files", [])),
                    "warnings": warnings}}


@contextmanager
def manager_lock(base: Path):
    """Advisory cross-process lock: lifetime covers Manager runtime or entire restore."""
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    path = base / "manager.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BackupLocked() from exc
        yield
    finally:
        if os.name == "nt":
            try:
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(fd)


def ensure_no_interrupted_restore(base: Path) -> None:
    if (Path(base) / "restore-journal.json").exists():
        raise BackupError("Η επαναφορά διακόπηκε. Τρέξτε python -m director.backup rollback.")


def _media_diff(section: dict | None, media_dir: Path) -> dict[str, list[str]]:
    if not section:
        return {"missing": [], "different": [], "extra": []}
    expected = {e["name"]: e for e in section["files"]}
    actual = {p.name: p for p in media_dir.glob("*.mp3")} if media_dir.is_dir() else {}
    missing, different = [], []
    for name, entry in expected.items():
        if name not in actual:
            missing.append(name)
        elif hashlib.sha256(actual[name].read_bytes()).hexdigest() != entry["sha256"]:
            different.append(name)
    return {"missing": missing, "different": different,
            "extra": sorted(set(actual) - set(expected))}


def _journal_path(base: Path) -> Path:
    return base / "restore-journal.json"


PRE_RESTORE_NOTICE = ("Ο φάκελος {path} περιέχει τα προηγούμενα αρχεία, μαζί με το προηγούμενο "
                      "ιδιωτικό κλειδί. Σβήστε τον αφού βεβαιωθείτε ότι όλα λειτουργούν.")


def _remove_orphans(base: Path) -> None:
    """Σβήνει staging που δεν ανήκει σε journal (διακοπή πριν από την πρώτη μετακίνηση).

    Καλείται μόνο με το κλείδωμα του Manager και χωρίς journal, άρα κανένα restore
    δεν τα χρησιμοποιεί. Το staging μπορεί να περιέχει ακρυπτογράφητο κλειδί.
    """
    for orphan in base.glob(".restore-staging-*"):
        shutil.rmtree(orphan, ignore_errors=True)
    for orphan in base.glob("pre-restore-*"):
        _remove_empty_tree(orphan)
    (base / "restore-journal.json.tmp").unlink(missing_ok=True)


def _remove_empty_tree(path: Path) -> None:
    """Σβήνει τον φάκελο μόνο αν δεν περιέχει κανένα αρχείο."""
    if path.is_dir() and not any(p.is_file() for p in path.rglob("*")):
        shutil.rmtree(path, ignore_errors=True)


def rollback(base: Path) -> bool:
    """Επαναφέρει την κατάσταση πριν από διακοπτόμενο restore. Ασφαλές σε επανάληψη."""
    base = Path(base)
    path = _journal_path(base)
    if not path.exists():
        _remove_orphans(base)
        return False
    try:
        journal = _json(path.read_bytes())
        moves = journal["moves"]
    except (ValueError, KeyError, TypeError):
        # Το journal γράφεται ατομικά πριν από την πρώτη μετακίνηση· άκυρο journal
        # σημαίνει ότι δεν έγινε καμία μετακίνηση. Μένει μόνο ο καθαρισμός.
        path.unlink()
        _remove_orphans(base)
        return True
    for item in reversed(moves):
        target = Path(item["target"])
        previous = Path(item["previous"])
        if item["had_previous"]:
            if previous.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                _replace_with_retry(previous, target)
        elif target.exists():
            target.unlink()
    path.unlink()
    if journal.get("staging"):
        shutil.rmtree(journal["staging"], ignore_errors=True)
    _remove_orphans(base)
    return True


def restore(path: Path, password: str, *, registry_file: Path, tls_cert: Path,
            tls_key: Path, media_dir: Path, replace_identity: bool = False,
            accept_older: bool = False, restore_media: bool = False,
            fail_after: int | None = None) -> dict:
    base = registry_file.parent
    with manager_lock(base):
        ensure_no_interrupted_restore(base)
        _remove_orphans(base)
        _, data = _read_backup(Path(path), password)
        manifest, sections, warnings = _validate_contents(data)
        locations = _paths(registry_file, tls_cert, tls_key)
        backup_cert = _un64(sections["tls_identity"]["files"]["cert.pem"])
        identity_matches = True
        if tls_cert.exists():
            existing_fp = x509.load_pem_x509_certificate(tls_cert.read_bytes()).fingerprint(
                hashes.SHA256()).hex(":").upper()
            identity_matches = existing_fp == manifest["tls_fingerprint"]
            if not identity_matches and not replace_identity:
                raise BackupError(
                    "Αυτός ο υπολογιστής έχει άλλη ταυτότητα Manager. Χρειάζεται --replace-identity.", 5)
        # A deliberate identity replacement is not a registry-history merge:
        # two different installations cannot share ancestry.
        if registry_file.exists() and identity_matches:
            current_doc = _json(registry_file.read_bytes())
            current = verify_document(current_doc, tls_cert.read_bytes())
            from_backup = _json(_un64(sections["client_registry"]["files"]["clients-registry.json"]))["registry"]
            if (digest(from_backup) in current["history"]
                    or (digest(from_backup) != digest(current)
                        and digest(current) not in from_backup["history"])):
                if not accept_older:
                    raise BackupError("Παλαιότερο ή ασύμβατο μητρώο· χρειάζεται --accept-older.", 5)
        suffix = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        staging = base / (".restore-staging-" + suffix)
        previous_dir = base / ("pre-restore-" + suffix)
        staging.mkdir(parents=True, exist_ok=False)
        previous_dir.mkdir(parents=True, exist_ok=False)
        moves = []
        operations = 0

        def step() -> None:
            # Μόνο για tests/scripts: τεχνητή διακοπή (σαν kill) μετά από N μετακινήσεις.
            nonlocal operations
            operations += 1
            if fail_after is not None and operations >= fail_after:
                raise KeyboardInterrupt("Injected restore interruption")

        try:
            for name, sec in sections.items():
                if name not in locations:
                    continue
                for filename, encoded in sec["files"].items():
                    target = locations[name][filename]
                    stage = staging / name / filename
                    _write_exclusive(stage, _un64(encoded))
                    prior = previous_dir / name / filename
                    moves.append({"target": str(target.resolve()), "stage": str(stage.resolve()),
                                  "previous": str(prior.resolve()),
                                  "had_previous": target.exists()})
            # Missing optional sections reset to defaults by removing their old files.
            for name in FILES:
                if name not in sections:
                    for filename, target in locations[name].items():
                        moves.append({"target": str(target.resolve()), "stage": None,
                                      "previous": str((previous_dir / name / filename).resolve()),
                                      "had_previous": target.exists()})
            journal = {"staging": str(staging.resolve()), "moves": moves}
            # Ατομικά: είτε υπάρχει πλήρες journal είτε κανένα (πριν από κάθε μετακίνηση).
            journal_tmp = base / "restore-journal.json.tmp"
            _write_exclusive(journal_tmp, canonical(journal))
            _replace_with_retry(journal_tmp, _journal_path(base))
        except BaseException:
            # Καμία μετακίνηση δεν έγινε: σβήνουμε το staging (περιέχει το κλειδί).
            shutil.rmtree(staging, ignore_errors=True)
            _remove_empty_tree(previous_dir)
            (base / "restore-journal.json.tmp").unlink(missing_ok=True)
            raise
        try:
            for item in moves:
                target, prior = Path(item["target"]), Path(item["previous"])
                if item["had_previous"]:
                    prior.parent.mkdir(parents=True, exist_ok=True)
                    _replace_with_retry(target, prior)
                    step()
                if item["stage"] is not None:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _replace_with_retry(Path(item["stage"]), target)
                    step()
        except Exception:
            # Κανονικό σφάλμα: αυτόματη επιστροφή. Σε kill/διακοπή (BaseException)
            # το journal μένει και ο Manager αρνείται να ξεκινήσει μέχρι το rollback.
            rollback(base)
            raise
        _journal_path(base).unlink()
        shutil.rmtree(staging, ignore_errors=True)
        _remove_empty_tree(previous_dir)
        if previous_dir.exists():
            warnings.append(PRE_RESTORE_NOTICE.format(path=previous_dir))
        copied = []
        if restore_media and sections.get("media_manifest"):
            source = Path(path).with_name(Path(path).stem + "-media")
            for entry in sections["media_manifest"]["files"]:
                name = entry["name"]
                if Path(name).name != name:
                    continue
                target = media_dir / name
                original = source / name
                if not target.exists() and original.is_file() and (
                        hashlib.sha256(original.read_bytes()).hexdigest() == entry["sha256"]):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(original, target)
                    copied.append(name)
        # Η αναφορά MP3 βγαίνει ΜΕΤΑ το --restore-media, ώστε να δείχνει την τελική κατάσταση.
        diff = _media_diff(sections.get("media_manifest"), media_dir)
        diff["copied"] = copied
        return {"registry_version": manifest["registry_version"],
                "tls_fingerprint": manifest["tls_fingerprint"],
                "previous": str(previous_dir) if previous_dir.exists() else None,
                "media": diff, "warnings": warnings}


def _passphrase(command: str, env: str | None) -> str:
    if env:
        if env not in os.environ:
            raise BackupError("Δεν βρέθηκε η μεταβλητή κωδικού.")
        return os.environ[env]
    first = getpass.getpass("Κωδικός backup: ")
    if command == "create":
        repeat = getpass.getpass("Επανάληψη κωδικού: ")
        if unicodedata.normalize("NFC", first) != unicodedata.normalize("NFC", repeat):
            raise BackupError("Οι κωδικοί δεν ταιριάζουν.")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="School Intercom — κρυπτογραφημένο backup Manager")
    parser.add_argument("--registry-file", type=Path,
                        default=Path.home() / ".school-intercom" / "clients-registry.json")
    parser.add_argument("--tls-cert", type=Path,
                        default=Path.home() / ".school-intercom" / "tls" / "director-tls.crt")
    parser.add_argument("--tls-key", type=Path,
                        default=Path.home() / ".school-intercom" / "tls" / "director-tls.key")
    parser.add_argument("--media-dir", type=Path, default=None)
    parser.add_argument("--passphrase-env", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    cr = sub.add_parser("create")
    cr.add_argument("--out", type=Path, required=True)
    cr.add_argument("--media-copy", choices=("ask", "yes", "no"), default="ask")
    ins = sub.add_parser("inspect")
    ins.add_argument("file", type=Path)
    ins.add_argument("--decrypt", action="store_true", help="Σύνοψη manifest με κωδικό")
    res = sub.add_parser("restore")
    res.add_argument("file", type=Path)
    res.add_argument("--replace-identity", action="store_true")
    res.add_argument("--accept-older", action="store_true")
    res.add_argument("--restore-media", action="store_true")
    rb = sub.add_parser("rollback")
    # Support common options before OR after the subcommand.
    for command_parser in (cr, ins, res, rb):
        command_parser.add_argument("--registry-file", type=Path, default=argparse.SUPPRESS)
        command_parser.add_argument("--tls-cert", type=Path, default=argparse.SUPPRESS)
        command_parser.add_argument("--tls-key", type=Path, default=argparse.SUPPRESS)
        command_parser.add_argument("--media-dir", type=Path, default=argparse.SUPPRESS)
        command_parser.add_argument("--passphrase-env", default=argparse.SUPPRESS,
                                    help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    media = args.media_dir or args.registry_file.parent / "media"
    try:
        if args.command == "create":
            choice = args.media_copy
            if choice == "ask" and _media_entries(media):
                answer = input("Να γίνει και αντίγραφο του φακέλου MP3 δίπλα στο backup; [Ν/ο] ")
                choice = "no" if answer.strip().casefold() in ("ο", "o", "όχι", "no", "n") else "yes"
            result, notices = create(args.out, _passphrase("create", args.passphrase_env),
                                     registry_file=args.registry_file,
                                     tls_cert=args.tls_cert, tls_key=args.tls_key,
                                     media_dir=media, media_copy=choice)
            print("Backup:", result)
            for notice in notices:
                print(notice)
            print("Χωρίς τον κωδικό το backup δεν ανοίγει. Φυλάξτε τον χωριστά από το αρχείο.")
        elif args.command == "inspect":
            result = inspect(args.file, _passphrase("inspect", args.passphrase_env)
                             if args.decrypt else None)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "restore":
            result = restore(args.file, _passphrase("restore", args.passphrase_env),
                             registry_file=args.registry_file, tls_cert=args.tls_cert,
                             tls_key=args.tls_key, media_dir=media,
                             replace_identity=args.replace_identity,
                             accept_older=args.accept_older, restore_media=args.restore_media,
                             fail_after=int(os.environ["SCH_BACKUP_TEST_FAIL_AFTER"])
                             if "SCH_BACKUP_TEST_FAIL_AFTER" in os.environ else None)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            with manager_lock(args.registry_file.parent):
                print("Rollback:", "ολοκληρώθηκε" if rollback(args.registry_file.parent)
                      else "δεν υπάρχει εκκρεμής επαναφορά")
    except BackupError as exc:
        print(str(exc), file=sys.stderr)
        return exc.status
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print("Αποτυχία backup/restore: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
