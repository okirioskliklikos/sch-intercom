"""Enrollment TLS/SAS and Manager-authoritative metadata tests (no physical devices)."""
import json
import socket
import ssl
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
from shared.enrollment import (EnrollmentError, EnrollmentServer, IncorrectCodeError,
                               REQUEST_TTL, _unsigned_request, enroll_client, make_request)
from tests.tls_support import LocalTLSFixture


class EnrollmentProtocolTests(LocalTLSFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        root = Path(self._tls_temp.name)
        self.root = root
        self.manager_cert = root / "director.crt"
        self.manager_key = root / "director.key"
        self.registry = SignedClientRegistry(root / "registry.json",
                                             self.manager_cert, self.manager_key)
        self.trust = RegistryTrust(self.registry, self.manager_cert, self.manager_key)
        self.messages = []
        self.now = [1_800_000_000.0]
        self.pending_file = root / "enrollment-pending.json"
        self.server = self._server()
        self.server.start()
        self.addCleanup(self.server.stop)
        self.client_id = str(uuid.uuid4())
        self.client_dir = root / "client"
        self.cert, self.key, self.password = create_device_identity(
            self.client_dir, self.client_id)
        self.request = self._request()

    def _server(self):
        server = EnrollmentServer(self.manager_cert, self.manager_key, self.trust,
                                  host="127.0.0.1", port=0, pending_file=self.pending_file,
                                  log=self.messages.append, clock=lambda: self.now[0])
        self.addCleanup(server.stop)
        return server

    def _request(self, room="C2627-Γ1", name="Board1"):
        return make_request(self.client_id, room, name, self.cert.read_bytes(),
                            self.key.read_bytes(), self.password)

    def _enroll(self, request=None):
        """Ο Client υποβάλλει και επιστρέφει αμέσως (δεν περιμένει έγκριση)."""
        return enroll_client("127.0.0.1", self.server.port, self.manager_cert,
                             "school-intercom-director", request or self.request)

    def test_wrong_manager_hostname_cannot_enroll(self):
        with self.assertRaises((ssl.SSLError, OSError)):
            enroll_client("127.0.0.1", self.server.port, self.manager_cert,
                          "untrusted-manager", self.request)
        self.assertFalse(self.server.pending())
        self.assertIsNone(self.registry.approved_entry(self.client_id))

    def test_signed_request_cannot_change_room_or_name(self):
        self.assertEqual(_unsigned_request(self.request)["client_id"], self.client_id)
        for field, value in (("room", "C2627-Γ2"), ("name", "Attacker")):
            altered = dict(self.request)
            altered[field] = value
            with self.subTest(field=field), self.assertRaises(EnrollmentError):
                _unsigned_request(altered)

    def test_client_returns_immediately_and_approval_works_offline(self):
        started = time.monotonic()
        queued = self._enroll()
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(queued["expires_at"], int(self.now[0] + REQUEST_TTL))
        self.assertEqual(REQUEST_TTL, 24 * 3600)
        [(request_id, room, name, expires)] = self.server.pending()
        self.assertEqual((request_id, room, name), (self.request["request_id"],
                                                     "C2627-Γ1", "Board1"))
        # Θύρα κλειστή και Client εκτός: η έγκριση γίνεται κανονικά.
        self.server.stop()
        info = self.server.decide(request_id, queued["code"], approve=True)
        self.assertEqual((info["room"], info["name"]), ("C2627-Γ1", "Board1"))
        record = self.registry.approved_entry(self.client_id)
        self.assertEqual((record["room"], record["name"]), ("C2627-Γ1", "Board1"))
        self.assertEqual(self.trust.context().cert_store_stats()["x509"], 1)
        self.assertFalse(self.server.pending())

    def test_wrong_code_denies_that_request(self):
        queued = self._enroll()
        wrong = f"{(int(queued['code']) + 1) % 1000000:06d}"
        with self.assertRaises(IncorrectCodeError):
            self.server.decide(self.request["request_id"], wrong, approve=True)
        self.assertFalse(self.server.pending())
        self.assertIsNone(self.registry.approved_entry(self.client_id))
        with self.assertRaises(EnrollmentError):  # ο σωστός κωδικός δεν σώζει πια το αίτημα
            self.server.decide(self.request["request_id"], queued["code"], approve=True)

    def test_deny_removes_request(self):
        self._enroll()
        info = self.server.decide(self.request["request_id"], approve=False)
        self.assertEqual(info["name"], "Board1")
        self.assertFalse(self.server.pending())
        self.assertIsNone(self.registry.approved_entry(self.client_id))

    def test_requests_survive_manager_restart(self):
        queued = self._enroll()
        self.server.stop()
        restarted = self._server()  # νέος Manager από το αρχείο, χωρίς άνοιγμα θύρας
        self.assertEqual([item[0] for item in restarted.pending()],
                         [self.request["request_id"]])
        restarted.decide(self.request["request_id"], queued["code"], approve=True)
        self.assertIsNotNone(self.registry.approved_entry(self.client_id))
        self.assertFalse(self._server().pending())  # η απόφαση γράφτηκε στο αρχείο

    def test_request_expires_after_one_day(self):
        queued = self._enroll()
        self.now[0] += REQUEST_TTL - 1
        self.assertEqual(len(self.server.pending()), 1)
        self.now[0] += 1
        self.assertFalse(self.server.pending())
        with self.assertRaises(EnrollmentError):
            self.server.decide(self.request["request_id"], queued["code"], approve=True)
        self.assertIsNone(self.registry.approved_entry(self.client_id))
        self.assertFalse(self._server().pending())  # ούτε μετά από επανεκκίνηση

    def test_new_request_from_same_client_replaces_old(self):
        first = self._enroll()
        second_request = self._request(room="C2627-Γ2", name="Board2")
        second = self._enroll(second_request)
        [(request_id, room, name, _)] = self.server.pending()
        self.assertEqual((request_id, room, name),
                         (second_request["request_id"], "C2627-Γ2", "Board2"))
        with self.assertRaises(EnrollmentError):
            self.server.decide(self.request["request_id"], first["code"], approve=True)
        self.server.decide(request_id, second["code"], approve=True)
        self.assertEqual(self.registry.approved_entry(self.client_id)["room"], "C2627-Γ2")

    def test_already_approved_client_is_rejected_at_submit(self):
        queued = self._enroll()
        self.server.decide(self.request["request_id"], queued["code"], approve=True)
        with self.assertRaises((EOFError, OSError, EnrollmentError)):
            self._enroll(self._request())
        self.assertFalse(self.server.pending())

    def test_tampered_pending_file_entries_are_ignored(self):
        self._enroll()
        self.server.stop()
        data = json.loads(self.pending_file.read_text(encoding="utf-8"))
        data["requests"][0]["request"]["room"] = "C2627-Γ9"  # σπάει την υπογραφή
        self.pending_file.write_text(json.dumps(data), encoding="utf-8")
        self.assertFalse(self._server().pending())

    def test_manager_never_displays_sas(self):
        queued = self._enroll()
        request_id = self.request["request_id"]
        self.assertTrue(any(request_id[:8] in message for message in self.messages))
        self.assertNotIn(queued["code"], " ".join(self.messages))
        self.assertNotIn(queued["code"], repr(self.server.pending()))
        self.assertEqual(self.server._pending[request_id].code, queued["code"])

    def test_resolve_accepts_unique_prefix(self):
        queued = self._enroll()
        request_id = self.request["request_id"]
        self.assertEqual(self.server.resolve(request_id[:8]), request_id)
        with self.assertRaises(EnrollmentError):
            self.server.resolve(request_id[:3])
        with self.assertRaises(EnrollmentError):
            self.server.resolve("zzzz")
        self.server.decide(self.server.resolve(request_id[:6]), queued["code"], approve=True)
        self.assertIsNotNone(self.registry.approved_entry(self.client_id))

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
