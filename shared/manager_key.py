"""Ιδιωτικό κλειδί TLS του Manager κρυπτογραφημένο στον δίσκο (Φάση 4.2C2).

Το κλειδί (π.χ. ``director-tls.key``) γράφεται ως κρυπτογραφημένο PKCS#8 με τυχαίο
κωδικό 32 bytes. Ο κωδικός φυλάγεται δίπλα, στο ``director-tls.key.pass``:

* Windows: προστατευμένος με DPAPI CurrentUser (ίδιο μοτίβο με το κλειδί του Client).
  Αντίγραφο των αρχείων σε άλλον χρήστη ή υπολογιστή δεν ανοίγει.
* Linux (μόνο ανάπτυξη): ο κωδικός σε αρχείο 0600, όπως στον Client.

Το κλειδί φορτώνεται απευθείας στη μνήμη (``load_cert_chain(password=…)``), χωρίς
προσωρινό ακρυπτογράφητο αρχείο. Το backup περιέχει το κλειδί ξεκλείδωτο ΜΟΝΟ μέσα στο
κρυπτογραφημένο payload, γιατί το DPAPI δεν μεταφέρεται· το restore το ξανακλειδώνει.
"""
from __future__ import annotations

import os
from pathlib import Path
import secrets

from cryptography.hazmat.primitives import serialization

from shared.device_identity import _dpapi, _write_secret

PASS_SUFFIX = ".pass"
RESTORE_HINT = "Επαναφέρετε την εγκατάσταση από backup (python -m director.backup restore)."


class ManagerKeyError(ValueError):
    """Το κλειδί του Manager δεν ανοίγει σε αυτόν τον χρήστη/υπολογιστή."""


def password_path(keyfile: Path) -> Path:
    keyfile = Path(keyfile)
    return keyfile.with_name(keyfile.name + PASS_SUFFIX)


def is_protected(key_pem: bytes) -> bool:
    return b"-----BEGIN ENCRYPTED PRIVATE KEY-----" in key_pem


def _protect_secret(password: bytes) -> bytes:
    return _dpapi(password, unprotect=False) if os.name == "nt" else password


def _unprotect_secret(stored: bytes) -> bytes:
    return _dpapi(stored, unprotect=True) if os.name == "nt" else stored


def key_password(keyfile: Path) -> bytes | None:
    """Κωδικός για το ``load_cert_chain``· None για παλιό ακρυπτογράφητο κλειδί."""
    keyfile = Path(keyfile)
    if not is_protected(keyfile.read_bytes()):
        return None
    stored_path = password_path(keyfile)
    if not stored_path.is_file():
        raise ManagerKeyError(
            f"Λείπει το {stored_path.name}: το κλειδί TLS του Manager δεν ανοίγει. "
            + RESTORE_HINT)
    try:
        password = _unprotect_secret(stored_path.read_bytes())
        # Έλεγχος εδώ, ώστε λάθος/αλλοιωμένο .pass να δίνει καθαρό μήνυμα και όχι
        # γενικό σφάλμα OpenSSL από το load_cert_chain.
        serialization.load_pem_private_key(keyfile.read_bytes(), password=password)
        return password
    except (OSError, TypeError, ValueError) as exc:
        raise ManagerKeyError(
            "Το κλειδί TLS του Manager δεν ανοίγει σε αυτόν τον χρήστη/υπολογιστή "
            "(DPAPI ή κωδικός .pass). " + RESTORE_HINT) from exc


def load_private_key(keyfile: Path):
    keyfile = Path(keyfile)
    try:
        return serialization.load_pem_private_key(keyfile.read_bytes(),
                                                  password=key_password(keyfile))
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ManagerKeyError):
            raise
        raise ManagerKeyError("Το κλειδί TLS του Manager δεν ανοίγει. " + RESTORE_HINT) from exc


def plaintext_pem(keyfile: Path) -> bytes:
    """Ξεκλείδωτο PKCS#8 μόνο στη μνήμη (για το κρυπτογραφημένο payload του backup)."""
    return load_private_key(keyfile).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())


def protect(key_pem: bytes) -> tuple[bytes, bytes]:
    """(κρυπτογραφημένο PEM, περιεχόμενο αρχείου .pass) με νέο τυχαίο κωδικό."""
    key = serialization.load_pem_private_key(key_pem, password=None)
    password = secrets.token_bytes(32)
    encrypted = key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.BestAvailableEncryption(password))
    return encrypted, _protect_secret(password)


def write_protected(keyfile: Path, key_pem: bytes) -> None:
    """Νέα αρχεία μόνο (αποκλειστική δημιουργία): πρώτα ο κωδικός, μετά το κλειδί."""
    keyfile = Path(keyfile)
    encrypted, stored = protect(key_pem)
    _write_secret(password_path(keyfile), stored)
    try:
        _write_secret(keyfile, encrypted)
    except BaseException:
        password_path(keyfile).unlink(missing_ok=True)
        raise


def migrate(keyfile: Path) -> bool:
    """Κρυπτογραφεί παλιό ακρυπτογράφητο κλειδί στη θέση του. True αν άλλαξε.

    Ίδιο κλειδί, άρα ίδιο αποτύπωμα: οι Clients δεν επηρεάζονται. Ασφαλές σε διακοπή:
    ο κωδικός μπαίνει πρώτος και το κλειδί αντικαθίσταται ατομικά· αν κοπεί ανάμεσα,
    το κλειδί είναι ακόμη ακρυπτογράφητο και η επόμενη εκκίνηση το ξαναπροσπαθεί.
    """
    keyfile = Path(keyfile)
    key_pem = keyfile.read_bytes()
    if is_protected(key_pem):
        return False
    encrypted, stored = protect(key_pem)
    stored_path = password_path(keyfile)
    for target, content in ((stored_path, stored), (keyfile, encrypted)):
        temp = target.with_name(target.name + ".tmp")
        temp.unlink(missing_ok=True)
        _write_secret(temp, content)
        os.replace(temp, target)
    return True
