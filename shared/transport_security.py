"""TLS transport for the School Intercom Director/Client connection.

Certificates and private keys are supplied at runtime; they are never generated
or stored in the repository. Client authorization is a separate Phase 4 step.
"""

import socket
import ssl
from pathlib import Path

MINIMUM_TLS_VERSION = ssl.TLSVersion.TLSv1_2
HANDSHAKE_TIMEOUT_SECONDS = 3.0
DEFAULT_SERVER_NAME = "school-intercom-director"


def server_context(certfile: Path, keyfile: Path) -> ssl.SSLContext:
    """Load an explicitly supplied Director certificate and private key."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = MINIMUM_TLS_VERSION
    ctx.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
    return ctx


def client_context(cafile: Path) -> ssl.SSLContext:
    """Require a trusted Director certificate and a matching DNS identity."""
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(cafile))
    ctx.minimum_version = MINIMUM_TLS_VERSION
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    return ctx


def accept_tls(connection: socket.socket, context: ssl.SSLContext) -> ssl.SSLSocket:
    """Perform TLS negotiation before reading the unauthenticated HELLO."""
    connection.settimeout(HANDSHAKE_TIMEOUT_SECONDS)
    return context.wrap_socket(connection, server_side=True)


def connect_tls(connection: socket.socket, context: ssl.SSLContext,
                server_name: str = DEFAULT_SERVER_NAME) -> ssl.SSLSocket:
    """Verify the Director certificate before the protocol handshake."""
    if not server_name:
        raise ValueError("TLS server name must not be empty")
    connection.settimeout(HANDSHAKE_TIMEOUT_SECONDS)
    return context.wrap_socket(connection, server_hostname=server_name)
