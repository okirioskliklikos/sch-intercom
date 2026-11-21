"""Ephemeral TLS identities for loopback integration tests.

Keys are generated under TemporaryDirectory, never committed to the repository.
"""
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

from shared.transport_security import (
    DEFAULT_SERVER_NAME, client_context, connect_tls, server_context,
)


class LocalTLSFixture:
    def setUp(self):
        super().setUp()
        if shutil.which("openssl") is None:
            self.skipTest("openssl required for ephemeral TLS integration tests")
        self._tls_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tls_temp.cleanup)
        directory = Path(self._tls_temp.name)
        cert = directory / "director.crt"
        key = directory / "director.key"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048",
             "-nodes", "-sha256", "-days", "1",
             "-keyout", str(key), "-out", str(cert),
             "-subj", "/CN=" + DEFAULT_SERVER_NAME,
             "-addext", "subjectAltName=DNS:" + DEFAULT_SERVER_NAME],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
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
