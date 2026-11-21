"""Local Client cryptographic identity. Secrets never go into the repository.

A Client's UUID is stable; its self-signed CA:FALSE certificate proves possession
of a different private key from every other device. Keys are encrypted at rest.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import secrets
import sys
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


class DeviceIdentityError(ValueError):
    """Incomplete or unreadable local identity; never silently rotate its key."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes):
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _dpapi(data: bytes, *, unprotect: bool) -> bytes:
    """Windows DPAPI CurrentUser, with memory freed by LocalFree."""
    from ctypes import windll
    incoming, keep_alive = _blob(data)
    outgoing = _DataBlob()
    # Explicit pointer signatures: ctypes defaults truncate 64-bit pointers on Windows.
    blob_ptr = ctypes.POINTER(_DataBlob)
    if unprotect:
        windll.crypt32.CryptUnprotectData.argtypes = [blob_ptr, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_ptr]
        windll.crypt32.CryptUnprotectData.restype = wintypes.BOOL
    else:
        windll.crypt32.CryptProtectData.argtypes = [blob_ptr, ctypes.c_wchar_p,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_ptr]
        windll.crypt32.CryptProtectData.restype = wintypes.BOOL
    windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    windll.kernel32.LocalFree.restype = ctypes.c_void_p
    flags = 0x01  # CRYPTPROTECT_UI_FORBIDDEN
    if unprotect:
        ok = windll.crypt32.CryptUnprotectData(
            ctypes.byref(incoming), None, None, None, None, flags,
            ctypes.byref(outgoing))
    else:
        ok = windll.crypt32.CryptProtectData(
            ctypes.byref(incoming), None, None, None, None, flags,
            ctypes.byref(outgoing))
    if not ok:
        raise OSError("Windows DPAPI failed")
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        windll.kernel32.LocalFree(outgoing.pbData)


def _write_secret(path: Path, content: bytes) -> None:
    """Exclusive creation, without following an existing file/symlink."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _paths(directory: Path) -> tuple[Path, Path, Path]:
    return directory / "client-cert.pem", directory / "client-key.pem", directory / "client-password.bin"


def identity_credentials(directory: Path) -> tuple[Path, Path, bytes]:
    """Return cert/key paths and ephemeral password for ssl.load_cert_chain."""
    cert, key, password_file = _paths(Path(directory))
    if not all(path.is_file() for path in (cert, key, password_file)):
        raise DeviceIdentityError("Incomplete Client identity; restore or re-enrol explicitly")
    secret = password_file.read_bytes()
    password = _dpapi(secret, unprotect=True) if sys.platform == "win32" else secret
    private_key = serialization.load_pem_private_key(key.read_bytes(), password=password)
    certificate = x509.load_pem_x509_certificate(cert.read_bytes())
    if certificate.public_key().public_numbers() != private_key.public_key().public_numbers():
        raise DeviceIdentityError("Certificate and private key do not match")
    return cert, key, password


def create_device_identity(directory: Path, client_id: str) -> tuple[Path, Path, bytes]:
    """Create only on a new directory; never overwrite or auto-repair keys."""
    if str(uuid.UUID(client_id)) != client_id:
        raise DeviceIdentityError("Invalid Client UUID")
    directory = Path(directory)
    cert, keyfile, password_file = _paths(directory)
    if any(path.exists() for path in (cert, keyfile, password_file)):
        return identity_credentials(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(directory, 0o700)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, client_id)])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1095))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=False,
            crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256()))
    password = secrets.token_bytes(32)
    encrypted = key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.BestAvailableEncryption(password))
    protected = _dpapi(password, unprotect=False) if sys.platform == "win32" else password
    try:
        _write_secret(keyfile, encrypted)
        _write_secret(password_file, protected)
        _write_secret(cert, certificate.public_bytes(serialization.Encoding.PEM))
    except BaseException:
        for path in (cert, keyfile, password_file):
            path.unlink(missing_ok=True)
        raise
    return identity_credentials(directory)
