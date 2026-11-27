"""Verified TLS transport for one full-duplex Director/Client connection.

Python's SSLSocket must not execute TLS reads and writes simultaneously from
separate threads. TLSConnection serializes SSL operations, without holding its
lock while waiting for network readiness. Certificates never live in the repo.
"""

import select
import socket
import ssl
import threading
import time
from pathlib import Path

MINIMUM_TLS_VERSION = ssl.TLSVersion.TLSv1_2
HANDSHAKE_TIMEOUT_SECONDS = 3.0
DEFAULT_SERVER_NAME = "school-intercom-director"


class TLSConnection:
    """Socket-like full-duplex wrapper with serialized nonblocking SSL I/O.

    Only SSL operations share the lock. Waiting for the network happens outside
    the lock, so heartbeat uploads cannot block outbound PCM for several seconds.
    Timeouts retain normal socket semantics for each read/write call.
    """

    def __init__(self, stream: ssl.SSLSocket):
        self._stream = stream
        self._lock = threading.Lock()
        self._timeout = stream.gettimeout()
        stream.setblocking(False)

    def settimeout(self, timeout: float | None) -> None:
        if timeout is not None and timeout < 0:
            raise ValueError("negative timeout")
        self._timeout = timeout

    def gettimeout(self) -> float | None:
        return self._timeout

    def setsockopt(self, *args) -> None:
        self._stream.setsockopt(*args)

    def fileno(self) -> int:
        return self._stream.fileno()

    def getpeername(self):
        return self._stream.getpeername()

    def getpeercert(self, *, binary_form: bool = False):
        """Verified TLS peer certificate; available only after the handshake."""
        with self._lock:
            return self._stream.getpeercert(binary_form=binary_form)

    def pending(self) -> int:
        """Αποκρυπτογραφημένα bytes ήδη στο SSL buffer (το select δεν τα βλέπει)."""
        with self._lock:
            return self._stream.pending()

    def shutdown(self, how: int) -> None:
        self._stream.shutdown(how)

    def close(self) -> None:
        self._stream.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def _deadline(self) -> float | None:
        return None if self._timeout is None else time.monotonic() + self._timeout

    def _wait(self, want_read: bool, deadline: float | None) -> None:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise socket.timeout("TLS I/O timed out")
        readable, writable, _ = select.select(
            [self._stream] if want_read else [],
            [] if want_read else [self._stream], [], remaining)
        if not (readable or writable):
            raise socket.timeout("TLS I/O timed out")

    def recv(self, size: int) -> bytes:
        deadline = self._deadline()
        while True:
            with self._lock:
                try:
                    return self._stream.recv(size)
                except ssl.SSLWantReadError:
                    want_read = True
                except ssl.SSLWantWriteError:
                    want_read = False
            self._wait(want_read, deadline)

    def sendall(self, data: bytes) -> None:
        deadline = self._deadline()
        payload = memoryview(data)
        offset = 0
        while offset < len(payload):
            with self._lock:
                try:
                    count = self._stream.send(payload[offset:])
                    if count == 0:
                        raise ConnectionError("TLS connection closed while sending")
                    offset += count
                    continue
                except ssl.SSLWantReadError:
                    want_read = True
                except ssl.SSLWantWriteError:
                    want_read = False
            self._wait(want_read, deadline)


def server_context(certfile: Path, keyfile: Path) -> ssl.SSLContext:
    """Load an explicitly supplied Director certificate and private key.

    Το κλειδί του Manager είναι κρυπτογραφημένο στον δίσκο (4.2C2)· ο κωδικός
    ανοίγει από το ``.pass`` (DPAPI στα Windows) και περνά μόνο στη μνήμη.
    """
    from shared.manager_key import key_password
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = MINIMUM_TLS_VERSION
    ctx.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile),
                        password=key_password(keyfile))
    return ctx


def client_context(cafile: Path, *, certfile: Path | None = None,
                   keyfile: Path | None = None,
                   password: bytes | None = None) -> ssl.SSLContext:
    """Require a trusted Director certificate and a matching DNS identity."""
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(cafile))
    ctx.minimum_version = MINIMUM_TLS_VERSION
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    if (certfile is None) != (keyfile is None):
        raise ValueError("Client certificate and key must be provided together")
    if certfile is not None:
        ctx.load_cert_chain(str(certfile), str(keyfile), password=password)
    return ctx


def accept_tls(connection: socket.socket, context: ssl.SSLContext) -> TLSConnection:
    """Perform TLS negotiation before reading the unauthenticated HELLO."""
    connection.settimeout(HANDSHAKE_TIMEOUT_SECONDS)
    return TLSConnection(context.wrap_socket(connection, server_side=True))


def connect_tls(connection: socket.socket, context: ssl.SSLContext,
                server_name: str = DEFAULT_SERVER_NAME) -> TLSConnection:
    """Verify the Director certificate before the protocol handshake."""
    if not server_name:
        raise ValueError("TLS server name must not be empty")
    connection.settimeout(HANDSHAKE_TIMEOUT_SECONDS)
    return TLSConnection(context.wrap_socket(connection, server_hostname=server_name))
