"""Ephemeral TLS identities for loopback integration tests.

Keys are generated under TemporaryDirectory, never committed to the repository.
"""
import socket
import tempfile
import unittest
from pathlib import Path

from director.tls_identity import create_director_identity
from shared.transport_security import (
    DEFAULT_SERVER_NAME, client_context, connect_tls, server_context,
)


class LocalTLSFixture:
    def setUp(self):
        super().setUp()
        self._tls_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tls_temp.cleanup)
        directory = Path(self._tls_temp.name)
        cert = directory / "director.crt"
        key = directory / "director.key"
        # Με cryptography (όχι το πρόγραμμα openssl, που δεν υπάρχει στα Windows).
        create_director_identity(cert, key)
        self.tls_server_context = server_context(cert, key)
        self.tls_client_context = client_context(cert)

    def connect_tls_client(self, address, timeout=3):
        plain = socket.create_connection(address, timeout=timeout)
        try:
            wrapped = connect_tls(plain, self.tls_client_context)
            wrapped.settimeout(timeout)
            return wrapped
        except BaseException:
            plain.close()
            raise
