"""Signed, portable Client allowlist for School Intercom (Phase 4.2).

The private signing key is the installation's existing Director TLS key. The
registry NEVER contains secrets. Version ancestry detects divergent backups.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import ssl
import tempfile
import threading
import uuid

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

SCHEMA_VERSION = 1
REGISTRY_FORMAT = "sch-intercom-registry"
VALID_STATES = frozenset({"approved", "revoked", "blocked"})


class RegistryError(ValueError):
    """Invalid signature, inconsistent identity, or unsafe registry mutation."""


def canonical(data: dict) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(data: dict) -> str:
    return hashlib.sha256(canonical(data)).hexdigest()


def public_key_fingerprint(cert_pem: bytes) -> str:
    certificate = x509.load_pem_x509_certificate(cert_pem)
    public_key = certificate.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(public_key).hexdigest()


def _sign(key, message: bytes) -> tuple[str, bytes]:
    if isinstance(key, ec.EllipticCurvePrivateKey):
        return "ES256", key.sign(message, ec.ECDSA(hashes.SHA256()))
    if isinstance(key, rsa.RSAPrivateKey):
        return "RS256", key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    raise RegistryError("Unsupported Manager signing key")


def _verify(key, algorithm: str, message: bytes, signature: bytes) -> None:
    if algorithm == "ES256" and isinstance(key, ec.EllipticCurvePublicKey):
        key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
    elif algorithm == "RS256" and isinstance(key, rsa.RSAPublicKey):
        key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
    else:
        raise RegistryError("Mismatched signature algorithm")


def _wrap(registry: dict, key) -> dict:
    alg, signature = _sign(key, canonical(registry))
    return {"registry": registry, "signature": {
        "alg": alg, "value": base64.b64encode(signature).decode("ascii")}}


def verify_document(document: dict, manager_cert_pem: bytes) -> dict:
    if not isinstance(document, dict) or set(document) != {"registry", "signature"}:
        raise RegistryError("Invalid registry document")
    registry = document["registry"]
    sig = document["signature"]
    try:
        if not isinstance(registry, dict) or not isinstance(sig, dict):
            raise RegistryError("Invalid registry data")
        signature = base64.b64decode(sig["value"], validate=True)
        _verify(x509.load_pem_x509_certificate(manager_cert_pem).public_key(),
                sig["alg"], canonical(registry), signature)
        if registry["format"] != REGISTRY_FORMAT or registry["schema_version"] != SCHEMA_VERSION:
            raise RegistryError("Unsupported registry schema")
        uuid.UUID(registry["school_id"])
        if type(registry["registry_version"]) is not int or registry["registry_version"] < 1:
            raise RegistryError("Invalid registry version")
        if not isinstance(registry["history"], list) or not isinstance(registry["clients"], list):
            raise RegistryError("Invalid registry collections")
        if registry["registry_version"] != len(registry["history"]) + 1:
            # Every mutation appends exactly one prior hash.
            raise RegistryError("Invalid version ancestry")
        seen_ids, seen_keys = set(), set()
        for entry in registry["clients"]:
            client_id = entry["client_id"]
            if str(uuid.UUID(client_id)) != client_id:
                raise RegistryError("Invalid Client UUID")
            cert = entry["cert_pem"].encode("ascii")
            if public_key_fingerprint(cert) != entry["spki_sha256"]:
                raise RegistryError("Client key fingerprint mismatch")
            if entry["status"] not in VALID_STATES or client_id in seen_ids:
                raise RegistryError("Duplicate Client or invalid status")
            if entry["spki_sha256"] in seen_keys:
                raise RegistryError("One key cannot represent multiple Clients")
            if not isinstance(entry["room"], str) or not entry["room"]:
                raise RegistryError("Invalid room")
            seen_ids.add(client_id)
            seen_keys.add(entry["spki_sha256"])
        return registry
    except (InvalidSignature, KeyError, TypeError, ValueError, IndexError) as exc:
        raise RegistryError("Invalid or tampered registry") from exc


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class SignedClientRegistry:
    """Mutable registry with serialized writes and explicit trust verification."""

    def __init__(self, path: Path, manager_cert: Path, manager_key: Path,
                 *, key_password: bytes | None = None):
        self.path = Path(path)
        self._cert_pem = Path(manager_cert).read_bytes()
        self._signing_key = serialization.load_pem_private_key(
            Path(manager_key).read_bytes(), password=key_password)
        cert_pub = x509.load_pem_x509_certificate(self._cert_pem).public_key()
        if cert_pub.public_bytes(serialization.Encoding.DER,
                              serialization.PublicFormat.SubjectPublicKeyInfo) != (
            self._signing_key.public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)):
            raise RegistryError("Manager certificate/private key mismatch")
        self.lock = threading.RLock()
        if self.path.exists():
            self._document = json.loads(self.path.read_text(encoding="utf-8"))
            verify_document(self._document, self._cert_pem)
        else:
            registry = {"format": REGISTRY_FORMAT, "schema_version": SCHEMA_VERSION,
                        "school_id": str(uuid.uuid4()), "registry_version": 1,
                        "history": [], "clients": []}
            self._document = _wrap(registry, self._signing_key)
            self._persist()

    @property
    def version(self) -> int:
        with self.lock:
            return self._document["registry"]["registry_version"]

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self._document["registry"]))

    def _persist(self, document: dict | None = None) -> None:
        data = self._document if document is None else document
        _atomic_write(self.path, json.dumps(data, sort_keys=True,
                                            ensure_ascii=False, indent=2).encode("utf-8") + b"\\n")

    def _commit(self, newer: dict) -> None:
        older = self._document["registry"]
        newer["history"] = older["history"] + [digest(older)]
        newer["registry_version"] = older["registry_version"] + 1
        document = _wrap(newer, self._signing_key)
        self._persist(document)  # No in-memory commit if disk write failed.
        self._document = document

    def approve(self, client_id: str, room: str, cert_pem: bytes, *, name: str = "") -> None:
        """CLI-first approval. A revoked key cannot be approved a second time."""
        from shared.identity import validate_identity
        validate_identity(client_id, room)
        cert = x509.load_pem_x509_certificate(cert_pem)
        if cert.subject != cert.issuer:
            raise RegistryError("Only self-signed device certificates are supported")
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
        if cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value != client_id:
            raise RegistryError("Certificate identity does not match UUID")
        if cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise RegistryError("Client certificate must not be a CA")
        if ExtendedKeyUsageOID.CLIENT_AUTH not in cert.extensions.get_extension_for_class(
                x509.ExtendedKeyUsage).value:
            raise RegistryError("Certificate is not valid for Client TLS")
        fp = public_key_fingerprint(cert_pem)
        with self.lock:
            updated = self.snapshot()
            if any(entry["client_id"] == client_id or entry["spki_sha256"] == fp
                   for entry in updated["clients"]):
                raise RegistryError("Existing/revoked Client identity; re-enrol with a new key")
            updated["clients"].append({
                "client_id": client_id, "name": name, "room": room,
                "spki_sha256": fp, "cert_pem": cert_pem.decode("ascii"),
                "status": "approved", "changed_in_version": self.version + 1})
            self._commit(updated)

    def revoke(self, client_id: str, *, blocked: bool = False) -> None:
        with self.lock:
            updated = self.snapshot()
            for entry in updated["clients"]:
                if entry["client_id"] == client_id:
                    if entry["status"] != "approved":
                        raise RegistryError("Client is already revoked")
                    entry["status"] = "blocked" if blocked else "revoked"
                    entry["changed_in_version"] = self.version + 1
                    self._commit(updated)
                    return
            raise RegistryError("Unknown Client")

    def approved_certificates(self) -> list[str]:
        with self.lock:
            return [entry["cert_pem"] for entry in self._document["registry"]["clients"]
                    if entry["status"] == "approved"]

    def authorize(self, peer_der: bytes | None, client_id: str, room: str) -> bool:
        if not peer_der:
            return False
        peer = x509.load_der_x509_certificate(peer_der)
        fp = public_key_fingerprint(peer.public_bytes(serialization.Encoding.PEM))
        with self.lock:
            return any(entry["spki_sha256"] == fp and entry["client_id"] == client_id
                       and entry["room"] == room and entry["status"] == "approved"
                       and x509.load_pem_x509_certificate(entry["cert_pem"].encode("ascii"))
                           .fingerprint(hashes.SHA256()) == peer.fingerprint(hashes.SHA256())
                       for entry in self._document["registry"]["clients"])

    def tls_server_context(self, certfile: Path, keyfile: Path) -> ssl.SSLContext:
        """Trust only registered approved certificates; empty list denies everyone."""
        from shared.transport_security import server_context
        ctx = server_context(certfile, keyfile)
        ctx.verify_mode = ssl.CERT_REQUIRED
        certificates = self.approved_certificates()
        if certificates:
            ctx.load_verify_locations(cadata="".join(certificates))
        return ctx
