"""Enrollment TLS/SAS and Manager-authoritative metadata tests (no physical devices)."""
import socket
import ssl
import threading
import time
from pathlib import Path
import tempfile
import unittest
import uuid

from director.main import RegistryTrust
from director.broadcast import AudioBroadcaster
from shared.audio import (ControlMessage, MESSAGE_CLIENT_SETTINGS,
                          decode_client_settings, encode_client_settings,
                          receive_message)
from shared.client_hints import load_hints, save_hints
from shared.client_registry import SignedClientRegistry
from shared.device_identity import create_device_identity
from shared.enrollment import (EnrollmentError, EnrollmentServer, REQUEST_TTL,
                               _unsigned_request, enroll_client, make_request, sas_code)
from tests.tls_support import LocalTLSFixture


class EnrollmentProtocolTests(LocalTLSFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        root = Path(self._tls_temp.name)
        self.manager_cert = root / "director.crt"
        self.manager_key = root / "director.key"
        self.registry = SignedClientRegistry(root / "registry.json",
                                             self.manager_cert, self.manager_key)
        self.trust = RegistryTrust(self.registry, self.manager_cert, self.manager_key)
        self.messages = []
        self.server = EnrollmentServer(self.manager_cert, self.manager_key, self.trust,
                                       host="127.0.0.1", port=0,
                                       log=self.messages.append)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.client_id = str(uuid.uuid4())
        self.client_dir = root / "client"
        self.cert, self.key, self.password = create_device_identity(
            self.client_dir, self.client_id)
        self.request = make_request(self.client_id, "C2627-Γ1", "Board1",
                                    self.cert.read_bytes(), self.key.read_bytes(),
                                    self.password)

    def _start_client(self):
        self.result = []
        self.client_log = []

        def do_enrollment():
            try:
                accepted = enroll_client("127.0.0.1", self.server.port,
                                         self.manager_cert, "school-intercom-director",
                                         self.request, log=self.client_log.append)
                self.result.append(accepted)
            except Exception as exc:
                self.result.append(exc)

        thread = threading.Thread(target=do_enrollment, daemon=True)
        thread.start()
        end = time.monotonic() + 4
        while not self.server.pending() and time.monotonic() < end:
            time.sleep(0.01)
        self.assertTrue(self.server.pending(), "No pending authenticated request")
        return thread

    def test_wrong_manager_hostname_cannot_enroll(self):
        with self.assertRaises((ssl.SSLError, OSError)):
            enroll_client("127.0.0.1", self.server.port, self.manager_cert,
                          "untrusted-manager", self.request, log=lambda message: None)
        self.assertFalse(self.server.pending())
        self.assertIsNone(self.registry.approved_entry(self.client_id))

    def test_signed_request_cannot_change_room_or_name(self):
        self.assertEqual(_unsigned_request(self.request)["client_id"], self.client_id)
        for field, value in (("room", "C2627-Γ2"), ("name", "Attacker")):
            altered = dict(self.request)
            altered[field] = value
            with self.subTest(field=field), self.assertRaises(EnrollmentError):
                _unsigned_request(altered)

    def test_challenge_matches_sas_and_explicit_approval(self):
        thread = self._start_client()
        request_id, name, room, code = self.server.pending()[0]
        self.assertEqual((name, room), ("Board1", "C2627-Γ1"))
        end = time.monotonic() + 4
        while not self.client_log and time.monotonic() < end:
            time.sleep(0.01)
        self.assertIn(code, " ".join(self.client_log))
        self.server.decide(request_id, code, approve=True)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.result, [True])
        record = self.registry.approved_entry(self.client_id)
        self.assertEqual((record["room"], record["name"]), ("C2627-Γ1", "Board1"))
        self.assertEqual(self.trust.context().cert_store_stats()["x509"], 1)

    def test_wrong_sas_denies_without_approval(self):
        thread = self._start_client()
        request_id, name, room, code = self.server.pending()[0]
        wrong = "000000" if code != "000000" else "999999"
        with self.assertRaisesRegex(EnrollmentError, "Incorrect SAS"):
            self.server.decide(request_id, wrong, approve=True)
        thread.join(timeout=5)
        self.assertEqual(self.result, [False])
        self.assertIsNone(self.registry.approved_entry(self.client_id))

    def test_expired_request_cannot_be_approved(self):
        thread = self._start_client()
        request_id = self.server.pending()[0][0]
        with self.server._lock:
            self.server._pending[request_id].created -= REQUEST_TTL + 1
        with self.assertRaises(EnrollmentError):
            self.server.decide(request_id, self.request["request_id"], approve=True)
        self.server.stop()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(self.registry.approved_entry(self.client_id))

    def test_approval_at_expiry_is_not_reported_as_expired(self):
        import shared.enrollment as enrollment

        class TimedOutEvent(threading.Event):
            def wait(self, timeout=None):
                super().wait(2.0)  # η έγκριση φτάνει…
                return False       # …αλλά η αναμονή «έληξε» ταυτόχρονα

        class LatePending(enrollment.Pending):
            def __init__(self, *args):
                super().__init__(*args)
                self.event = TimedOutEvent()

        original = enrollment.Pending
        enrollment.Pending = LatePending
        self.addCleanup(setattr, enrollment, "Pending", original)
        thread = self._start_client()
        request_id, _, _, code = self.server.pending()[0]
        self.server.decide(request_id, code, approve=True)
        thread.join(timeout=5)
        self.assertEqual(self.result, [True])  # όχι «expired» για εγκεκριμένο Client
        self.assertIsNotNone(self.registry.approved_entry(self.client_id))

    def test_resolve_accepts_unique_prefix(self):
        thread = self._start_client()
        request_id, _, _, code = self.server.pending()[0]
        self.assertEqual(self.server.resolve(request_id[:8]), request_id)
        with self.assertRaises(EnrollmentError):
            self.server.resolve(request_id[:3])
        with self.assertRaises(EnrollmentError):
            self.server.resolve("zzzz")
        self.server.decide(self.server.resolve(request_id[:6]), code, approve=True)
        thread.join(timeout=5)
        self.assertEqual(self.result, [True])

    def test_port_closed_until_explicit_enrollment(self):
        self.server.stop()
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", self.server.port), timeout=1)


class ClientSettingsTests(unittest.TestCase):
    def test_broadcaster_sends_initial_and_live_manager_settings(self):
        broadcaster = AudioBroadcaster()
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client = socket.create_connection(listener.getsockname(), timeout=2)
        accepted, _ = listener.accept()
        client.settimeout(3)
        try:
            client_id = str(uuid.uuid4())
            self.assertTrue(broadcaster.add_client(
                accepted, client_id, "Α1", name="PC"))
            first = receive_message(client)
            self.assertEqual(decode_client_settings(first.payload),
                             {"room": "Α1", "name": "PC"})
            broadcaster.update_client_info(client_id, "Α2", "Board")
            second = receive_message(client)
            self.assertEqual(decode_client_settings(second.payload),
                             {"room": "Α2", "name": "Board"})
            self.assertEqual(broadcaster.clients_snapshot(), [(client_id, "Α2")])
        finally:
            broadcaster.close()
            client.close()
            listener.close()

    def test_settings_control_wire_format(self):
        a, b = socket.socketpair()
        try:
            a.sendall(encode_client_settings("C2627-Γ3", "Board 2"))
            parsed = receive_message(b)
            self.assertIsInstance(parsed, ControlMessage)
            self.assertEqual(parsed.kind, MESSAGE_CLIENT_SETTINGS)
            self.assertEqual(decode_client_settings(parsed.payload),
                             {"room": "C2627-Γ3", "name": "Board 2"})
        finally:
            a.close()
            b.close()

    def test_last_used_hints_never_change_client_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hints.json"
            save_hints(path, "C2627-Γ1", "PC")
            save_hints(path, "C2627-Γ2", "Board")
            self.assertEqual(load_hints(path),
                             {"room": "C2627-Γ2", "name": "Board"})

    def test_metadata_change_keeps_cert_key_and_identity(self):
        from cryptography import x509
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client_id = str(uuid.uuid4())
            cert, key, password = create_device_identity(root / "client", client_id)
            before = cert.read_bytes()
            # The Manager-owned room/name is independent of this certificate.
            save_hints(root / "hints.json", "C2627-Γ3", "Board")
            self.assertEqual(cert.read_bytes(), before)
            self.assertEqual(x509.load_pem_x509_certificate(before).subject.rfc4514_string(),
                             "CN=" + client_id)


if __name__ == "__main__":
    unittest.main()
