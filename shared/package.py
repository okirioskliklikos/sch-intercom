"""Υπογεγραμμένο πακέτο έκδοσης Client `.sicu` (Φάση 5B1, docs/phase5.md §5.1).

Μορφή (zip):
    manifest.json   product, version, min_updater, connection_protocol, created_at,
                    files [{path, size, sha256}] — κανονική σειριοποίηση JSON
    manifest.sig    Ed25519 (64 bytes) πάνω στα bytes του manifest.json
    files/<path>    τα αρχεία του versions\\<έκδοση> (python\\, app\\, VERSION)

Η επαλήθευση γίνεται στον Client: υπογραφή με ενσωματωμένο κλειδί, SHA-256 και μέγεθος
κάθε αρχείου, ασφαλείς διαδρομές, νεότερη έκδοση, συμβατός updater. Ο Client δεν
εμπιστεύεται ούτε τον Manager ούτε το δίκτυο.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import time
import zipfile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (Ed25519PrivateKey,
                                                               Ed25519PublicKey)

from shared.version import parse_version

PRODUCT = "sch-intercom-client"
FORMAT = 1
#: Έκδοση του μηχανισμού ενημέρωσης του Client (UPDATE_PROTOCOL, §5.3).
#: 2 = λήψη μόνο των αρχείων που άλλαξαν (5B2c).
UPDATER_VERSION = 2
MANIFEST = "manifest.json"
SIGNATURE = "manifest.sig"
FILES_PREFIX = "files/"
MAX_PACKAGE_BYTES = 300 * 1024 * 1024
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024
MAX_FILES = 20000
MIN_PASSPHRASE = 12
_HASH = re.compile(r"^[0-9a-f]{64}$")
_KEY = re.compile(r"^[0-9a-f]{64}$")
_CHUNK = 1024 * 1024


class PackageError(Exception):
    """Το πακέτο απορρίπτεται· το μήνυμα λέει γιατί (ελληνικά)."""


# -- κλειδιά --------------------------------------------------------------------
def generate_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def public_hex(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def save_private_key(key: Ed25519PrivateKey, path: Path, passphrase: str) -> None:
    if len(passphrase) < MIN_PASSPHRASE:
        raise ValueError(f"Ο κωδικός θέλει τουλάχιστον {MIN_PASSPHRASE} χαρακτήρες")
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"Υπάρχει ήδη: {path}")
    data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.BestAvailableEncryption(passphrase.encode("utf-8")))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def load_private_key(path: Path, passphrase: str) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(Path(path).read_bytes(),
                                                 passphrase.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise PackageError("Λάθος κωδικός ή κατεστραμμένο αρχείο κλειδιού") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise PackageError("Το κλειδί δεν είναι Ed25519")
    return key


def _public_keys(trusted) -> list[Ed25519PublicKey]:
    keys = []
    for item in trusted:
        if isinstance(item, Ed25519PublicKey):
            keys.append(item)
            continue
        if not isinstance(item, str) or not _KEY.fullmatch(item):
            raise PackageError("Μη έγκυρο δημόσιο κλειδί")
        keys.append(Ed25519PublicKey.from_public_bytes(bytes.fromhex(item)))
    if not keys:
        raise PackageError("Δεν υπάρχει ενσωματωμένο κλειδί υπογραφής· κανένα πακέτο δεν γίνεται δεκτό")
    return keys


# -- manifest -------------------------------------------------------------------
def manifest_bytes(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def safe_path(text) -> str:
    """Σχετική διαδρομή με '/', χωρίς '..', απόλυτες, δίσκους ή ειδικά ονόματα."""
    if not isinstance(text, str) or not text or len(text) > 400:
        raise PackageError(f"Μη έγκυρη διαδρομή: {text!r}")
    if "\\" in text or "\x00" in text or ":" in text or text.startswith("/"):
        raise PackageError(f"Μη ασφαλής διαδρομή: {text!r}")
    parts = PurePosixPath(text).parts
    if any(part in ("", ".", "..") for part in parts) or PurePosixPath(text).as_posix() != text:
        raise PackageError(f"Μη ασφαλής διαδρομή: {text!r}")
    return text


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_files(tree: Path) -> list[Path]:
    tree = Path(tree)
    return sorted((path for path in tree.rglob("*")
                   if path.is_file() and "__pycache__" not in path.relative_to(tree).parts),
                  key=lambda path: path.relative_to(tree).as_posix())


def build_manifest(tree: Path, version: str, *, min_updater: int = UPDATER_VERSION,
                   connection_protocol: int | None = None, created_at: int | None = None) -> dict:
    parse_version(version)
    if connection_protocol is None:
        from shared.identity import CONNECTION_PROTOCOL
        connection_protocol = CONNECTION_PROTOCOL
    files = []
    for path in tree_files(tree):
        relative = safe_path(path.relative_to(tree).as_posix())
        files.append({"path": relative, "size": path.stat().st_size,
                      "sha256": _sha256_file(path)})
    if not files:
        raise PackageError(f"Κενό δέντρο έκδοσης: {tree}")
    return {"format": FORMAT, "product": PRODUCT, "version": version,
            "min_updater": int(min_updater), "connection_protocol": int(connection_protocol),
            "created_at": int(created_at if created_at is not None else time.time()),
            "files": files}


def validate_manifest(manifest) -> dict:
    if not isinstance(manifest, dict):
        raise PackageError("Μη έγκυρο manifest")
    expected = {"format", "product", "version", "min_updater", "connection_protocol",
                "created_at", "files"}
    if set(manifest) != expected:
        raise PackageError("Μη έγκυρα πεδία manifest")
    if manifest["format"] != FORMAT or manifest["product"] != PRODUCT:
        raise PackageError("Το πακέτο δεν είναι Client του School Intercom")
    try:
        parse_version(manifest["version"])
    except ValueError as exc:
        raise PackageError(str(exc)) from exc
    for field in ("min_updater", "connection_protocol", "created_at"):
        if type(manifest[field]) is not int or manifest[field] < 0:
            raise PackageError(f"Μη έγκυρο πεδίο manifest: {field}")
    files = manifest["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise PackageError("Μη έγκυρη λίστα αρχείων")
    seen = set()
    total = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise PackageError("Μη έγκυρη εγγραφή αρχείου")
        path = safe_path(item["path"])
        if path.lower() in seen:
            raise PackageError(f"Διπλό αρχείο: {path}")
        seen.add(path.lower())
        if type(item["size"]) is not int or item["size"] < 0:
            raise PackageError(f"Μη έγκυρο μέγεθος: {path}")
        if not isinstance(item["sha256"], str) or not _HASH.fullmatch(item["sha256"]):
            raise PackageError(f"Μη έγκυρο hash: {path}")
        total += item["size"]
    if total > MAX_UNPACKED_BYTES:
        raise PackageError("Πολύ μεγάλο πακέτο μετά την αποσυμπίεση")
    return manifest


# -- πακέτο ---------------------------------------------------------------------
def write_package(tree: Path, out: Path, key: Ed25519PrivateKey, version: str, *,
                  min_updater: int = UPDATER_VERSION, connection_protocol: int | None = None,
                  created_at: int | None = None) -> dict:
    tree = Path(tree)
    manifest = build_manifest(tree, version, min_updater=min_updater,
                              connection_protocol=connection_protocol, created_at=created_at)
    data = manifest_bytes(manifest)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_name(out.name + ".tmp")
    with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.writestr(MANIFEST, data)
        archive.writestr(SIGNATURE, key.sign(data))
        for item in manifest["files"]:
            archive.write(tree / item["path"], FILES_PREFIX + item["path"])
    os.replace(temp, out)
    if out.stat().st_size > MAX_PACKAGE_BYTES:
        out.unlink()
        raise PackageError("Το πακέτο ξεπερνά τα 300 MB")
    return manifest


def check_version(manifest: dict, installed_version: str | None, updater_version: int) -> None:
    if manifest["min_updater"] > updater_version:
        raise PackageError(f"Το πακέτο θέλει updater v{manifest['min_updater']}· "
                           f"εδώ υπάρχει v{updater_version} (χειροκίνητη εγκατάσταση)")
    if installed_version is not None and \
            parse_version(manifest["version"]) <= parse_version(installed_version):
        raise PackageError(f"Η έκδοση {manifest['version']} δεν είναι νεότερη από την "
                           f"εγκατεστημένη {installed_version}")


def verify_manifest(data: bytes, signature: bytes, *, trusted_keys=None) -> dict:
    """Υπογραφή και μορφή του manifest (5B2c: ο Client το παίρνει χωρίς το πακέτο)."""
    if trusted_keys is None:
        from shared.release_keys import TRUSTED_KEYS
        trusted_keys = TRUSTED_KEYS
    for key in _public_keys(trusted_keys):
        try:
            key.verify(signature, data)
            break
        except InvalidSignature:
            continue
    else:
        raise PackageError("Άκυρη υπογραφή: το πακέτο δεν είναι υπογεγραμμένο με έμπιστο κλειδί")
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PackageError("Μη αναγνώσιμο manifest") from exc
    if manifest_bytes(manifest) != data:
        raise PackageError("Το manifest δεν είναι σε κανονική μορφή")
    return validate_manifest(manifest)


def read_signed_manifest(path: Path) -> tuple[bytes, bytes]:
    """Τα bytes του manifest.json και του manifest.sig, χωρίς έλεγχο."""
    path = Path(path)
    try:
        if path.stat().st_size > MAX_PACKAGE_BYTES:
            raise PackageError("Το πακέτο ξεπερνά τα 300 MB")
        with zipfile.ZipFile(path) as archive:
            return archive.read(MANIFEST), archive.read(SIGNATURE)
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise PackageError(f"Μη αναγνώσιμο πακέτο: {exc}") from exc


def read_manifest(path: Path, *, trusted_keys=None) -> dict:
    """Ελέγχει μόνο υπογραφή και manifest (γρήγορο, πριν από την πλήρη επαλήθευση)."""
    if trusted_keys is None:
        from shared.release_keys import TRUSTED_KEYS
        trusted_keys = TRUSTED_KEYS
    keys = _public_keys(trusted_keys)
    data, signature = read_signed_manifest(path)
    return verify_manifest(data, signature, trusted_keys=keys)


def verify_package(path: Path, *, trusted_keys=None, installed_version: str | None = None,
                   updater_version: int = UPDATER_VERSION) -> dict:
    """Πλήρης έλεγχος: υπογραφή, έκδοση, ότι τα περιεχόμενα είναι ακριβώς τα αρχεία
    του manifest με το σωστό μέγεθος και SHA-256. Επιστρέφει το manifest."""
    manifest = read_manifest(path, trusted_keys=trusted_keys)
    check_version(manifest, installed_version, updater_version)
    wanted = {FILES_PREFIX + item["path"]: item for item in manifest["files"]}
    try:
        with zipfile.ZipFile(path) as archive:
            names = [info.filename for info in archive.infolist()]
            if len(names) != len(set(names)):
                raise PackageError("Διπλά ονόματα μέσα στο πακέτο")
            extra = set(names) - set(wanted) - {MANIFEST, SIGNATURE}
            if extra:
                raise PackageError(f"Αρχείο εκτός manifest: {sorted(extra)[0]}")
            missing = set(wanted) - set(names)
            if missing:
                raise PackageError(f"Λείπει αρχείο: {sorted(missing)[0][len(FILES_PREFIX):]}")
            for name, item in wanted.items():
                _check_member(archive, name, item)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PackageError(f"Μη αναγνώσιμο πακέτο: {exc}") from exc
    return manifest


def _check_member(archive: zipfile.ZipFile, name: str, item: dict, sink=None) -> None:
    info = archive.getinfo(name)
    if info.file_size != item["size"]:
        raise PackageError(f"Λάθος μέγεθος: {item['path']}")
    digest = hashlib.sha256()
    read = 0
    with archive.open(info) as source:
        for chunk in iter(lambda: source.read(_CHUNK), b""):
            read += len(chunk)
            if read > item["size"]:
                raise PackageError(f"Λάθος μέγεθος: {item['path']}")
            digest.update(chunk)
            if sink is not None:
                sink.write(chunk)
    if read != item["size"] or digest.hexdigest() != item["sha256"]:
        raise PackageError(f"Αλλοιωμένο αρχείο: {item['path']}")


def extract_package(path: Path, destination: Path, *, trusted_keys=None,
                    installed_version: str | None = None,
                    updater_version: int = UPDATER_VERSION) -> dict:
    """Επαλήθευση και αποσυμπίεση σε νέο φάκελο (ξαναελέγχει κάθε hash καθώς γράφει).

    Γράφει στο <destination>.new και μετονομάζει μόνο αν όλα είναι σωστά.
    """
    manifest = verify_package(path, trusted_keys=trusted_keys,
                              installed_version=installed_version,
                              updater_version=updater_version)
    destination = Path(destination)
    if destination.exists():
        raise PackageError(f"Υπάρχει ήδη: {destination}")
    staging = destination.with_name(destination.name + ".new")
    if staging.exists():
        shutil.rmtree(staging)
    try:
        with zipfile.ZipFile(path) as archive:
            for item in manifest["files"]:
                target = staging.joinpath(*PurePosixPath(item["path"]).parts)
                if staging.resolve() not in target.resolve().parents:
                    raise PackageError(f"Μη ασφαλής διαδρομή: {item['path']}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "wb") as sink:
                    _check_member(archive, FILES_PREFIX + item["path"], item, sink)
        os.replace(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest
