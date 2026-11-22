"""Explicit, TLS-protected first enrollment (Phase 4.2A2).

This protocol is NOT an audio endpoint. Only the existing Director certificate
may authenticate the server. Client proves possession of its private key before
its request can be displayed. Only the Client shows the six-digit SAS; the
administrator types it at the Manager, which never displays it (no copy-paste approval).

Offline approval (user decision 21/11/2026): the Client submits its request with the
desired room/name, shows the code and exits. Requests are kept on disk for one day,
so the administrator can collect codes on paper and approve each listed request
later, whether the Client is running or not and even with the enrollment port closed.
"""
from __future__ import annotations

import base64
from collections import deque
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import struct
import tempfile
import threading
import time
import uuid

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from shared.client_registry import canonical
from shared.identity import validate_identity
from shared.transport_security import server_context, client_context, connect_tls

ENROLL_PORT = 50559
PROTOCOL = 2  # v2: ο Client δεν περιμένει την έγκριση (εγγραφή χωρίς αναμονή)
MAX_MESSAGE = 8192
MAX_CONNECTIONS = 16  # ταυτόχρονες συνδέσεις στη θύρα εγγραφής
MAX_PENDING = 50  # απόφαση χρήστη 22/11/2026: έως 50 εκκρεμή αιτήματα
#: Μήνυμα στον Client όταν ο κατάλογος αιτημάτων είναι γεμάτος.
TEXT_FULL = "Πάρα πολλά αιτήματα εγγραφής σε αναμονή. Δοκιμάστε αργότερα."
MAX_ATTEMPTS_PER_MINUTE = 8
REQUEST_TTL = 24 * 3600  # απόφαση χρήστη: τα αιτήματα ισχύουν μία ημέρα
NETWORK_TIMEOUT = 8
PENDING_FILE_VERSION = 1
MAX_CODE_ATTEMPTS = 3  # απόφαση χρήστη: 3 προσπάθειες κωδικού ανά αίτημα


class EnrollmentError(ValueError):
    pass


class RequestsFullError(EnrollmentError):
    """Ο Manager έχει ήδη ``MAX_PENDING`` αιτήματα σε αναμονή."""


class IncorrectCodeError(EnrollmentError):
    """Λάθος κωδικός· ``remaining`` = προσπάθειες που μένουν (0: το αίτημα απορρίφθηκε)."""

    def __init__(self, remaining: int):
        super().__init__("Incorrect SAS; enrollment denied" if remaining == 0
                         else f"Incorrect SAS; {remaining} attempts left")
        self.remaining = remaining


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
        # Proof of possession alone is insufficient if the certificate envelope
        # was not actually signed by the advertised key.
        cert.public_key().verify(cert.signature, cert.tbs_certificate_bytes,
                                 ec.ECDSA(cert.signature_hash_algorithm))
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
    def __init__(self, request: dict, code: str, created: float, attempts: int = 0):
        self.request = request  # υπογεγραμμένο αίτημα (μαζί με proof)
        self.code = code
        self.created = created  # wall clock: τα αιτήματα επιβιώνουν επανεκκίνηση
        self.attempts = attempts  # λάθος κωδικοί μέχρι τώρα (σώζονται στο αρχείο)

    @property
    def expires(self) -> float:
        return self.created + REQUEST_TTL


class EnrollmentServer:
    """Registration socket, independent from the authenticated audio port.

    Η θύρα ανοίγει μαζί με τον Director και μένει ανοιχτή (απόφαση χρήστη 22/11/2026)·
    προστασία: έως ``MAX_PENDING`` αιτήματα, κωδικός 6 ψηφίων με 3 προσπάθειες, λήξη
    σε μία ημέρα, όριο συνδέσεων ανά IP. Ο κατάλογος αιτημάτων (``pending_file``)
    υπάρχει ανεξάρτητα από τη θύρα: η έγκριση γίνεται και με κλειστό Client.
    """

    def __init__(self, certfile: Path, keyfile: Path, trust, *,
                 host: str = "0.0.0.0", port: int = ENROLL_PORT,
                 pending_file: Path | None = None, log=print, clock=time.time,
                 max_pending: int | None = None):
        self.host, self.port = host, port
        self.trust = trust
        self.log = log
        self.clock = clock
        self.pending_file = Path(pending_file) if pending_file is not None else None
        self._max_pending = max_pending  # μόνο για αυτόματες δοκιμές· αλλιώς MAX_PENDING
        self.context = server_context(certfile, keyfile)
        self._lock = threading.RLock()
        self._pending: dict[str, Pending] = {}
        self._listener = None
        self._thread = None
        self._stop = threading.Event()
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._attempts: dict[str, deque[float]] = {}
        #: Γιατί δεν άνοιξε η θύρα στο ``open`` (π.χ. κατειλημμένη), αλλιώς None.
        self.start_error: str | None = None
        self._load()

    # ------------------------------------------------------------ αποθήκευση
    def _load(self) -> None:
        if self.pending_file is None:
            return
        try:
            data = json.loads(self.pending_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, UnicodeError, ValueError) as exc:
            self.log(f"[Εγγραφή] Αγνοήθηκε αρχείο εκκρεμών αιτημάτων: {exc}")
            return
        if not isinstance(data, dict) or data.get("version") != PENDING_FILE_VERSION:
            self.log("[Εγγραφή] Αγνοήθηκε αρχείο εκκρεμών αιτημάτων άγνωστης μορφής")
            return
        now = self.clock()
        for item in data.get("requests", [])[:self.max_pending]:
            try:
                request, code, created = item["request"], item["code"], float(item["created"])
                attempts = item.get("attempts", 0)
                unsigned = _unsigned_request(request)  # ξαναελέγχεται η υπογραφή
                if not (isinstance(code, str) and len(code) == 6 and code.isdigit()):
                    raise ValueError
                if type(attempts) is not int or not 0 <= attempts < MAX_CODE_ATTEMPTS:
                    raise ValueError
            except (EnrollmentError, KeyError, TypeError, ValueError):
                continue
            if created <= now < created + REQUEST_TTL:
                self._pending[unsigned["request_id"]] = Pending(request, code, created,
                                                                attempts)

    def _save(self) -> None:
        """Ατομική εγγραφή· καλείται με κρατημένο το lock."""
        if self.pending_file is None:
            return
        data = {"version": PENDING_FILE_VERSION, "requests": [
            {"request": p.request, "code": p.code, "created": p.created,
             "attempts": p.attempts}
            for p in self._pending.values()]}
        path = self.pending_file
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _purge(self) -> None:
        """Αφαιρεί ληγμένα αιτήματα· καλείται με κρατημένο το lock."""
        now = self.clock()
        expired = [rid for rid, p in self._pending.items() if now >= p.expires]
        for rid in expired:
            del self._pending[rid]
        if expired:
            self._save()

    # ------------------------------------------------------------ θύρα εγγραφής
    def start(self):
        with self._lock:
            if self._listener is not None:
                return
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((self.host, self.port))
                self.port = listener.getsockname()[1]
                listener.listen(MAX_CONNECTIONS)
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

    @property
    def max_pending(self) -> int:
        return self._max_pending if self._max_pending is not None else MAX_PENDING

    def open(self) -> bool:
        """Ανοίγει τη θύρα με την εκκίνηση του Director (πάντα ανοιχτή, απόφαση 22/11/2026).

        Αποτυχία (π.χ. κατειλημμένη θύρα) δεν σταματά τον Director: καταγράφεται και
        φαίνεται στη σελίδα «Ασφάλεια»· η έγκριση αιτημάτων συνεχίζει να λειτουργεί.
        """
        try:
            self.start()
        except OSError as exc:
            self.start_error = exc.strerror or str(exc)
            self.log(f"[Εγγραφή] Η θύρα εγγραφής {self.port} δεν άνοιξε: {self.start_error}")
            return False
        self.start_error = None
        return True

    @property
    def is_full(self) -> bool:
        """Αν συμπληρώθηκε το όριο ``MAX_PENDING`` εκκρεμών αιτημάτων."""
        with self._lock:
            self._purge()
            return len(self._pending) >= self.max_pending

    @property
    def is_open(self) -> bool:
        """Αν η θύρα εγγραφής δέχεται αυτή τη στιγμή νέα αιτήματα."""
        with self._lock:
            return self._listener is not None

    def stop(self):
        """Κλείνει μόνο τη θύρα· τα αποθηκευμένα αιτήματα παραμένουν για έγκριση."""
        with self._lock:
            self._stop.set()
            listener, self._listener = self._listener, None
            thread, self._thread = self._thread, None
            if listener is not None:
                listener.close()
        if thread is not None and threading.current_thread() is not thread:
            thread.join(timeout=1)
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
            with self._lock:
                now = time.monotonic()
                attempts = self._attempts.setdefault(address[0], deque())
                while attempts and now - attempts[0] >= 60:
                    attempts.popleft()
                limited = len(attempts) >= MAX_ATTEMPTS_PER_MINUTE
                if not limited:
                    attempts.append(now)
                if len(self._attempts) > 256:
                    self._attempts = {ip: q for ip, q in self._attempts.items()
                                      if q and now - q[-1] < 60}
            if limited or not self._slots.acquire(False):
                raw.close()
                continue
            threading.Thread(target=self._handle, args=(raw, address),
                             daemon=True).start()

    def _handle(self, raw, address):
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
                        self._purge()
                        if self._stop.is_set():
                            raise EnrollmentError("Registration closed")
                        if request_id in self._pending:
                            raise EnrollmentError("Duplicate request")
                        if self.trust.registry.approved_entry(unsigned["client_id"]):
                            raise EnrollmentError("Client already approved")
                        # Νέο αίτημα της ίδιας συσκευής αντικαθιστά το παλιό (νέος κωδικός).
                        for rid, old in list(self._pending.items()):
                            if old.request["client_id"] == unsigned["client_id"]:
                                del self._pending[rid]
                        full = len(self._pending) >= self.max_pending
                        if not full:
                            pending = Pending(request, code, self.clock())
                            self._pending[request_id] = pending
                        self._save()
                    if full:
                        # Ο Client μαθαίνει γιατί απορρίφθηκε (όριο 50, απόφαση χρήστη).
                        send_message(stream, {"type": "rejected", "reason": "full"})
                        raise RequestsFullError("Too many pending requests")
                    send_message(stream, {"type": "queued", "request_id": request_id,
                                          "director_nonce": nonce,
                                          "expires_at": int(pending.expires)})
                    # Ο SAS δεν εμφανίζεται ποτέ στον Manager: ο διαχειριστής πληκτρολογεί
                    # αυτόν που έγραψε από την οθόνη του Client.
                    self.log(f"[Εγγραφή] Νέο αίτημα {request_id[:8]} | {unsigned['room']} | "
                             f"{unsigned['name']}")
        except (OSError, ssl.SSLError, EOFError, EnrollmentError, ValueError) as exc:
            self.log(f"[Εγγραφή] Απόρριψη {address[0]}: {exc}")
        finally:
            self._slots.release()

    # ------------------------------------------------------------ απόφαση
    def pending(self) -> list[tuple[str, str, str, float]]:
        """Εκκρεμή αιτήματα (ID, αίθουσα, όνομα, λήξη) χωρίς τον SAS."""
        with self._lock:
            self._purge()
            items = sorted(self._pending.items(),
                           key=lambda item: (item[1].request["room"], item[1].request["name"]))
            return [(rid, p.request["room"], p.request["name"], p.expires)
                    for rid, p in items]

    def resolve(self, prefix: str) -> str:
        """Πλήρες ID από μοναδικό πρόθεμα (≥ 4 χαρακτήρες), για ευκολία στο CLI."""
        with self._lock:
            if prefix in self._pending:
                return prefix
            matches = [rid for rid in self._pending if rid.startswith(prefix)]
        if len(prefix) < 4 or len(matches) != 1:
            raise EnrollmentError("Unknown or ambiguous enrollment request ID")
        return matches[0]

    def decide(self, request_id: str, code: str | None = None, *,
               approve: bool = False) -> dict:
        """Έγκριση με τον κωδικό του συγκεκριμένου αιτήματος ή απόρριψη.

        Λάθος κωδικός μετρά προσπάθεια· μετά από ``MAX_CODE_ATTEMPTS`` λάθη το αίτημα
        απορρίπτεται (η συσκευή ζητά ξανά εγγραφή). Επιστρέφει αίθουσα/όνομα του
        αιτήματος για επιβεβαίωση στον διαχειριστή.
        """
        with self._lock:
            self._purge()
            pending = self._pending.get(request_id)
            if pending is None:
                raise EnrollmentError("No active enrollment request")
            req = pending.request
            info = {"client_id": req["client_id"], "room": req["room"], "name": req["name"]}
            try:
                if approve and not (isinstance(code, str)
                                    and hmac.compare_digest(code, pending.code)):
                    pending.attempts += 1
                    remaining = MAX_CODE_ATTEMPTS - pending.attempts
                    if remaining <= 0:
                        del self._pending[request_id]
                    raise IncorrectCodeError(max(remaining, 0))
                del self._pending[request_id]
                if approve:
                    self.trust.approve(req["client_id"], req["room"],
                                       req["cert_pem"].encode("ascii"), name=req["name"])
                return info
            finally:
                self._save()


def enroll_client(host: str, port: int, cafile: Path, server_name: str,
                  request: dict) -> dict:
    """Validate the pinned Manager TLS cert, submit the request, return SAS and expiry.

    Ο Client δεν περιμένει την απόφαση: συνδέεται μόνος του μετά την έγκριση.
    """
    ctx = client_context(cafile)
    raw = socket.create_connection((host, port), timeout=NETWORK_TIMEOUT)
    try:
        with connect_tls(raw, ctx, server_name) as conn:
            conn.settimeout(NETWORK_TIMEOUT)
            send_message(conn, request)
            reply = read_message(conn)
            if reply.get("type") == "rejected" and reply.get("reason") == "full":
                raise RequestsFullError(TEXT_FULL)
            if reply.get("type") != "queued" or (
                reply.get("request_id") != request["request_id"]):
                raise EnrollmentError("Unexpected enrollment reply")
            expires = reply.get("expires_at")
            if type(expires) is not int:
                raise EnrollmentError("Invalid enrollment expiry")
            unsigned = {k: v for k, v in request.items() if k != "proof"}
            return {"code": sas_code(unsigned, reply["director_nonce"]),
                    "request_id": request["request_id"], "expires_at": expires}
    finally:
        raw.close()
