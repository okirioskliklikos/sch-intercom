"""Explicit, TLS-protected first enrollment (Phase 4.2A2).

This protocol is NOT an audio endpoint. Only the existing Director certificate
may authenticate the server. Client proves possession of its private key before
its request can be displayed. The human compares a six-digit SAS on BOTH PCs.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import socket
import ssl
import struct
import threading
import time
import uuid

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from shared.client_registry import canonical, RegistryError
from shared.identity import validate_identity
from shared.transport_security import server_context, client_context, connect_tls

ENROLL_PORT = 50559
PROTOCOL = 1
MAX_MESSAGE = 8192
MAX_PENDING = 16
REQUEST_TTL = 120
NETWORK_TIMEOUT = 8


class EnrollmentError(ValueError):
    pass


def send_message(stream, message: dict) -> None:
    encoded = canonical(message)
    if len(encoded) > MAX_MESSAGE:
        raise EnrollmentError("Enrollment message too large")
    stream.sendall(struct.pack("!I", len(encoded)) + encoded)


def _exact(stream, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        block = stream.recv(size - len(chunks))
        if not block:
            raise EOFError("Enrollment connection closed")
        chunks.extend(block)
    return bytes(chunks)


def read_message(stream) -> dict:
    length = struct.unpack("!I", _exact(stream, 4))[0]
    if not 1 <= length <= MAX_MESSAGE:
        raise EnrollmentError("Invalid enrollment message length")
    try:
        message = json.loads(_exact(stream, length).decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise EnrollmentError("Invalid enrollment JSON") from exc
    if not isinstance(message, dict):
        raise EnrollmentError("Invalid enrollment message")
    return message


def _name(value: str) -> str:
    if not isinstance(value, str) or not (1 <= len(value) <= 64):
        raise EnrollmentError("Invalid computer name")
    if value != value.strip() or any(ord(ch) < 32 for ch in value):
        raise EnrollmentError("Invalid computer name")
    return value


def _unsigned_request(request: dict) -> dict:
    expected = {"type", "version", "request_id", "client_id", "room", "name",
                "cert_pem", "client_nonce"}
    if not isinstance(request, dict) or set(request) != expected | {"proof"}:
        raise EnrollmentError("Invalid enrollment fields")
    if request["type"] != "enroll" or type(request["version"]) is not int or request["version"] != PROTOCOL:
        raise EnrollmentError("Unsupported enrollment version")
    try:
        if str(uuid.UUID(request["request_id"])) != request["request_id"]:
            raise ValueError
        validate_identity(request["client_id"], request["room"])
        _name(request["name"])
        nonce = base64.b64decode(request["client_nonce"], validate=True)
        cert = x509.load_pem_x509_certificate(request["cert_pem"].encode("ascii"))
        signature = base64.b64decode(request["proof"], validate=True)
        if len(nonce) != 32 or len(signature) > 256:
            raise ValueError
        subject = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if len(subject) != 1 or subject[0].value != request["client_id"]:
            raise ValueError
        if not isinstance(cert.public_key(), ec.EllipticCurvePublicKey):
            raise ValueError
        unsigned = {k: request[k] for k in expected}
        cert.public_key().verify(signature, canonical(unsigned), ec.ECDSA(hashes.SHA256()))
        return unsigned
    except (ValueError, KeyError, TypeError, InvalidSignature) as exc:
        raise EnrollmentError("Invalid Client identity or key-possession proof") from exc


def make_request(client_id: str, room: str, name: str, cert: bytes,
                 encrypted_key: bytes, password: bytes) -> dict:
    validate_identity(client_id, room)
    _name(name)
    unsigned = {
        "type": "enroll", "version": PROTOCOL,
        "request_id": str(uuid.uuid4()), "client_id": client_id, "room": room,
        "name": name, "cert_pem": cert.decode("ascii"),
        "client_nonce": base64.b64encode(secrets.token_bytes(32)).decode("ascii")}
    key = serialization.load_pem_private_key(encrypted_key, password=password)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise EnrollmentError("Expected ECDSA Client key")
    return {**unsigned, "proof": base64.b64encode(
        key.sign(canonical(unsigned), ec.ECDSA(hashes.SHA256()))).decode("ascii")}


def sas_code(unsigned: dict, director_nonce: str) -> str:
    nonce = base64.b64decode(director_nonce, validate=True)
    if len(nonce) != 32:
        raise EnrollmentError("Invalid Director nonce")
    payload = canonical({"request": unsigned, "director_nonce": director_nonce})
    return f"{int.from_bytes(hashlib.sha256(payload).digest(), 'big') % 1000000:06d}"


class Pending:
    def __init__(self, request: dict, code: str):
        self.request = request
        self.code = code
        self.created = time.monotonic()
        self.event = threading.Event()
        self.result = "denied"


class EnrollmentServer:
    """Opt-in registration socket, independent from the authenticated audio port."""

    def __init__(self, certfile: Path, keyfile: Path, trust, *,
                 host: str = "0.0.0.0", port: int = ENROLL_PORT,
                 log=print):
        self.host, self.port = host, port
        self.trust = trust
        self.log = log
        self.context = server_context(certfile, keyfile)
        self._lock = threading.RLock()
        self._pending: dict[str, Pending] = {}
        self._listener = None
        self._thread = None
        self._stop = threading.Event()
        self._slots = threading.BoundedSemaphore(MAX_PENDING)

    def start(self):
        with self._lock:
            if self._listener is not None:
                return
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((self.host, self.port))
                listener.listen(MAX_PENDING)
                listener.settimeout(0.2)
            except BaseException:
                listener.close()
                raise
            self._stop.clear()
            self._listener = listener
            self._thread = threading.Thread(target=self._serve, daemon=True,
                                             name="enroll-listener")
            self._thread.start()
            self.log(f"[Εγγραφή] Ενεργή TLS θύρα {self.port} (μόνο εγγραφή).")

    def stop(self):
        with self._lock:
            self._stop.set()
            listener, self._listener = self._listener, None
            if listener is not None:
                listener.close()
            for pending in self._pending.values():
                pending.result = "denied"
                pending.event.set()
        self.log("[Εγγραφή] Η λειτουργία εγγραφής σταμάτησε.")

    def _serve(self):
        while not self._stop.is_set():
            with self._lock:
                listener = self._listener
            if listener is None:
                return
            try:
                raw, address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self._slots.acquire(False):
                raw.close()
                continue
            threading.Thread(target=self._handle, args=(raw, address),
                             daemon=True).start()

    def _handle(self, raw, address):
        request_id = None
        try:
            with raw:
                raw.settimeout(NETWORK_TIMEOUT)
                with self.context.wrap_socket(raw, server_side=True) as stream:
                    request = read_message(stream)
                    unsigned = _unsigned_request(request)
                    request_id = unsigned["request_id"]
                    nonce = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
                    code = sas_code(unsigned, nonce)
                    with self._lock:
                        if self._stop.is_set() or len(self._pending) >= MAX_PENDING:
                            raise EnrollmentError("Registration closed or full")
                        if request_id in self._pending:
                            raise EnrollmentError("Duplicate request")
                        if any(p.request["client_id"] == unsigned["client_id"]
                               for p in self._pending.values()):
                            raise EnrollmentError("Client already pending")
                        pending = Pending(unsigned, code)
                        self._pending[request_id] = pending
                    send_message(stream, {"type": "challenge", "request_id": request_id,
                                          "director_nonce": nonce})
                    self.log(f"[Εγγραφή] {request_id} | {unsigned['name']} | "
                             f"{unsigned['room']} | SAS {code}")
                    pending.event.wait(REQUEST_TTL)
                    with self._lock:
                        if time.monotonic() - pending.created >= REQUEST_TTL:
                            pending.result = "expired"
                        result = pending.result
                    send_message(stream, {"type": result, "request_id": request_id})
        except (OSError, ssl.SSLError, EOFError, EnrollmentError, ValueError) as exc:
            self.log(f"[Εγγραφή] Απόρριψη {address[0]}: {exc}")
        finally:
            if request_id is not None:
                with self._lock:
                    self._pending.pop(request_id, None)
            self._slots.release()

    def pending(self) -> list[tuple[str, str, str, str]]:
        with self._lock:
            return [(rid, p.request["name"], p.request["room"], p.code)
                    for rid, p in self._pending.items()
                    if not p.event.is_set() and time.monotonic() - p.created < REQUEST_TTL]

    def decide(self, request_id: str, code: str | None = None, *, approve: bool = False):
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None or pending.event.is_set() or (
                time.monotonic() - pending.created >= REQUEST_TTL):
                raise EnrollmentError("No active enrollment request")
            if approve:
                if not isinstance(code, str) or not hmac.compare_digest(code, pending.code):
                    raise EnrollmentError("Incorrect SAS; enrollment denied")
                req = pending.request
                self.trust.approve(req["client_id"], req["room"],
                                   req["cert_pem"].encode("ascii"), name=req["name"])
                pending.result = "approved"
            else:
                pending.result = "denied"
            pending.event.set()


def enroll_client(host: str, port: int, cafile: Path, server_name: str,
                  request: dict, *, log=print) -> bool:
    """Validate the pinned Manager TLS cert, show SAS, await explicit decision."""
    ctx = client_context(cafile)
    raw = socket.create_connection((host, port), timeout=NETWORK_TIMEOUT)
    try:
        with connect_tls(raw, ctx, server_name) as conn:
            conn.settimeout(NETWORK_TIMEOUT)
            send_message(conn, request)
            challenge = read_message(conn)
            if challenge.get("type") != "challenge" or (
                challenge.get("request_id") != request["request_id"]):
                raise EnrollmentError("Unexpected enrollment challenge")
            unsigned = {k: v for k, v in request.items() if k != "proof"}
            code = sas_code(unsigned, challenge["director_nonce"])
            log(f"Κωδικός επιβεβαίωσης SAS: {code}. Συγκρίνετε με τον Manager.")
            conn.settimeout(REQUEST_TTL + 5)
            result = read_message(conn)
            if result.get("request_id") != request["request_id"]:
                raise EnrollmentError("Mismatched enrollment result")
            if result.get("type") not in ("approved", "denied", "expired"):
                raise EnrollmentError("Unknown enrollment result")
            log(f"Κατάσταση εγγραφής: {result['type']}")
            return result["type"] == "approved"
    finally:
        raw.close()
