"""4.2D3: ο Client θυμάται την έκδοση μητρώου και ο Manager δείχνει νεότερο μητρώο.

Πρωτόκολλο v7 (HELLO «seen», WELCOME με έκδοση/hash), αρχείο του Client,
αναφορές στον Manager με πραγματικό mTLS σε loopback, «Συνέχεια με αυτό το μητρώο».
"""
import json
import socket
import struct
import threading
import time
from pathlib import Path
import tempfile
import unittest
import uuid

from director import manager_state
from director.broadcast import AudioBroadcaster
from director.main import RegistryTrust, accept_clients
from director.manager_state import RoleError
from director.registry_reports import RegistryReports
from shared import registry_seen
from shared.client_registry import (RegistryError, SignedClientRegistry, digest, merge,
                                    relation, verify_document)
from shared.device_identity import create_device_identity
from shared.identity import (CONNECTION_PROTOCOL, HELLO_MAGIC, WELCOME, WELCOME_MAGIC,
                             WELCOME_OK, ProtocolMismatch, receive_hello_details,
                             receive_welcome, send_hello, send_welcome)
from shared.transport_security import client_context, connect_tls
from tests.tls_support import LocalTLSFixture

HASH_A = "a" * 64
HASH_B = "b" * 64


def raw_hello(sender, data: dict) -> None:
    payload = json.dumps(data).encode("utf-8")
    sender.sendall(HELLO_MAGIC + struct.pack("!H", len(payload)) + payload)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.left, self.right = socket.socketpair()
        self.addCleanup(self.left.close)
        self.addCleanup(self.right.close)
        self.client_id = str(uuid.uuid4())

    def test_version_is_9(self):
        # 7 = 4.2D3 (seen/rv+hash)· 8 = 5C2 (αίτημα αλλαγής αίθουσας/ονόματος).
        self.assertEqual(CONNECTION_PROTOCOL, 9)

    def test_hello_round_trip_with_and_without_seen(self):
        send_hello(self.left, self.client_id, "Γ1", seen={"rv": 20, "hash": HASH_A})
        self.assertEqual(receive_hello_details(self.right),
                         (self.client_id, "Γ1", {"rv": 20, "hash": HASH_A}))
        send_hello(self.left, self.client_id, "Γ1")
        self.assertEqual(receive_hello_details(self.right), (self.client_id, "Γ1", None))

    def test_invalid_seen_rejected(self):
        for seen in ({"rv": 0, "hash": HASH_A}, {"rv": 3, "hash": "XYZ"},
                     {"rv": "3", "hash": HASH_A}, {"rv": 3}, [3, HASH_A],
                     {"rv": 3, "hash": HASH_A, "extra": 1}, {"rv": True, "hash": HASH_A}):
            with self.subTest(seen=seen):
                raw_hello(self.left, {"client_id": self.client_id, "room": "Γ1",
                                      "protocol": CONNECTION_PROTOCOL, "seen": seen})
                with self.assertRaises(ValueError):
                    receive_hello_details(self.right)

    def test_v7_without_seen_field_rejected(self):
        raw_hello(self.left, {"client_id": self.client_id, "room": "Γ1",
                              "protocol": CONNECTION_PROTOCOL})
        with self.assertRaises(ValueError):
            receive_hello_details(self.right)

    def test_v6_client_gets_version_mismatch(self):
        send_hello(self.left, self.client_id, "Γ1", protocol=6)
        with self.assertRaises(ProtocolMismatch) as caught:
            receive_hello_details(self.right)
        self.assertEqual(caught.exception.client_version, 6)

    def test_welcome_carries_version_and_hash(self):
        send_welcome(self.left, accepted=True, registry_version=21, registry_hash=HASH_B)
        self.assertEqual(receive_welcome(self.right), {"rv": 21, "hash": HASH_B})

    def test_welcome_without_registry_returns_none(self):
        send_welcome(self.left, accepted=True)
        self.assertIsNone(receive_welcome(self.right))

    def test_rejection_is_header_only(self):
        send_welcome(self.left, accepted=False)
        self.left.close()
        data = b""
        while chunk := self.right.recv(64):
            data += chunk
        self.assertEqual(len(data), WELCOME.size)

    def test_v6_director_welcome_rejected_before_extra_bytes(self):
        self.left.sendall(WELCOME.pack(WELCOME_MAGIC, 6, WELCOME_OK))
        with self.assertRaisesRegex(ProtocolMismatch, "Director v6"):
            receive_welcome(self.right)

    def test_invalid_registry_info_refused(self):
        with self.assertRaises(ValueError):
            send_welcome(self.left, accepted=True, registry_version=3, registry_hash="zz")


class ClientStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "sub" / "registry-seen.json"
        self.key = registry_seen.manager_key(b"manager-cert")

    def test_keeps_largest_version(self):
        self.assertIsNone(registry_seen.load_seen(self.path, self.key))
        self.assertTrue(registry_seen.record_seen(self.path, self.key, {"rv": 5, "hash": HASH_A}))
        self.assertFalse(registry_seen.record_seen(self.path, self.key, {"rv": 3, "hash": HASH_B}))
        self.assertFalse(registry_seen.record_seen(self.path, self.key, {"rv": 5, "hash": HASH_B}))
        self.assertEqual(registry_seen.load_seen(self.path, self.key), {"rv": 5, "hash": HASH_A})
        self.assertTrue(registry_seen.record_seen(self.path, self.key, {"rv": 9, "hash": HASH_B}))
        self.assertEqual(registry_seen.load_seen(self.path, self.key), {"rv": 9, "hash": HASH_B})

    def test_separate_per_manager(self):
        other = registry_seen.manager_key(b"other-installation")
        registry_seen.record_seen(self.path, self.key, {"rv": 30, "hash": HASH_A})
        self.assertIsNone(registry_seen.load_seen(self.path, other))
        registry_seen.record_seen(self.path, other, {"rv": 2, "hash": HASH_B})
        self.assertEqual(registry_seen.load_seen(self.path, self.key)["rv"], 30)
        self.assertEqual(registry_seen.load_seen(self.path, other)["rv"], 2)

    def test_corrupt_file_means_nothing_seen(self):
        self.path.parent.mkdir(parents=True)
        for content in ("{", "[]", '{"format": 99, "managers": {}}',
                        json.dumps({"format": 1, "managers": {self.key: {"rv": -1}}})):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                self.assertIsNone(registry_seen.load_seen(self.path, self.key))
        self.assertTrue(registry_seen.record_seen(self.path, self.key, {"rv": 4, "hash": HASH_A}))
        self.assertEqual(registry_seen.load_seen(self.path, self.key)["rv"], 4)

    def test_no_registry_in_welcome_changes_nothing(self):
        self.assertFalse(registry_seen.record_seen(self.path, self.key, None))
        self.assertFalse(self.path.exists())

    def test_bounded_number_of_managers(self):
        for index in range(registry_seen.MAX_MANAGERS + 4):
            registry_seen.record_seen(self.path, registry_seen.manager_key(bytes([index])),
                                      {"rv": index + 1, "hash": HASH_A})
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["managers"]), registry_seen.MAX_MANAGERS)
        self.assertIsNone(registry_seen.load_seen(self.path, registry_seen.manager_key(b"\0")))


class ReportsTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.reports = RegistryReports(on_event=self.events.append)

    def record(self, client, rv, hash_, room="Γ1", name="Board1"):
        return self.reports.record(client, {"rv": rv, "hash": hash_}, room=room, name=name,
                                   local_rv=15, local_hash=HASH_A)

    def test_newer_and_divergent_alarm(self):
        self.assertFalse(self.record("c1", 15, HASH_A))     # ίδιο μητρώο
        self.assertFalse(self.record("c2", 9, HASH_B))      # παλαιότερο
        self.assertIsNone(self.reports.reported(15, HASH_A))
        self.assertTrue(self.record("c3", 15, HASH_B, room="Γ2"))
        divergent = self.reports.reported(15, HASH_A)
        self.assertTrue(divergent.divergent)
        self.assertIn("διαφορετικό μητρώο v15", divergent.bar_text())
        self.assertTrue(self.record("c4", 20, HASH_B, room="Γ3", name=""))
        self.assertTrue(self.record("c5", 20, HASH_B, room="Γ1"))
        newer = self.reports.reported(15, HASH_A)
        self.assertEqual((newer.rv, newer.divergent), (20, False))
        self.assertEqual(newer.reporters, ("Γ1 · Board1", "Γ3"))
        self.assertEqual(newer.target_version, 21)
        self.assertEqual(newer.bar_text(), "Υπάρχει νεότερο μητρώο (v20). Οι ανακλήσεις "
                         "μπορεί να μην ισχύουν. Εισαγάγετε το ενημερωμένο μητρώο.")
        self.assertEqual(newer.status_line(), "[Manager] Clients αναφέρουν νεότερο μητρώο: "
                         "v20, εδώ v15 (Γ1 · Board1, Γ3)")
        self.assertEqual(len(self.events), 3)

    def test_newer_local_registry_silences(self):
        self.record("c1", 20, HASH_B)
        self.assertIsNone(self.reports.reported(21, HASH_A))

    def test_only_approved_and_forget(self):
        self.record("c1", 20, HASH_B)
        self.assertIsNone(self.reports.reported(15, HASH_A, approved={"c2"}))
        self.assertIsNotNone(self.reports.reported(15, HASH_A, approved={"c1"}))
        self.reports.forget("c1")
        self.assertIsNone(self.reports.reported(15, HASH_A))

    def test_later_report_without_seen_replaces(self):
        self.record("c1", 20, HASH_B)
        self.reports.record("c1", None, room="Γ1", name="", local_rv=15, local_hash=HASH_A)
        self.assertIsNone(self.reports.reported(15, HASH_A))


class AdvanceVersionTests(LocalTLSFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.directory = Path(self._tls_temp.name)
        self.cert = self.directory / "director.crt"
        self.key = self.directory / "director.key"
        self.registry = SignedClientRegistry(self.directory / "registry.json",
                                             self.cert, self.key)
        self.client_id = str(uuid.uuid4())
        device, _key, _pw = create_device_identity(self.directory / "d1", self.client_id)
        self.registry.approve(self.client_id, "Γ1", device.read_bytes())

    def test_jump_keeps_content_and_signature(self):
        before = self.registry.snapshot()
        self.assertEqual(self.registry.advance_version(21), 21)
        after = self.registry.snapshot()
        self.assertEqual(after["clients"], before["clients"])
        self.assertEqual(len(after["history"]), 20)
        self.assertEqual(relation(before, after), "newer")
        document = json.loads(self.registry.path.read_text(encoding="utf-8"))
        verify_document(document, self.cert.read_bytes())
        self.assertEqual(self.registry.advance_version(22), 22)

    def test_not_lower_or_equal(self):
        for target in (1, self.registry.version, -5, "9"):
            with self.subTest(target=target):
                with self.assertRaises(RegistryError):
                    self.registry.advance_version(target)

    def test_lost_registry_found_later_is_a_conflict_and_merges(self):
        base = self.registry.snapshot()
        lost_path = self.directory / "lost.json"
        lost_path.write_bytes(self.registry.path.read_bytes())
        lost = SignedClientRegistry(lost_path, self.cert, self.key)
        lost.revoke(self.client_id)                      # ανάκληση στο «χαμένο» μητρώο
        for _ in range(3):
            lost.advance_version(lost.version + 1)
        self.registry.advance_version(lost.version + 1)  # συνέχεια με αυτό το μητρώο
        local, incoming = self.registry.snapshot(), lost.snapshot()
        self.assertEqual(relation(local, incoming), "conflict")
        self.assertNotEqual(relation(base, local), "conflict")
        merged, _info = merge(local, incoming)
        self.assertGreater(merged["registry_version"], local["registry_version"])
        self.assertEqual(merged["clients"][0]["status"], "revoked")  # η ανάκληση υπερισχύει


class AdmissionTests(LocalTLSFixture, unittest.TestCase):
    """Πραγματικό mTLS: αναφορά μόνο από εγκεκριμένο Client, WELCOME με έκδοση."""

    def setUp(self):
        super().setUp()
        directory = Path(self._tls_temp.name)
        self.cert = directory / "director.crt"
        self.key = directory / "director.key"
        self.registry = SignedClientRegistry(directory / "registry.json", self.cert, self.key)
        self.devices = {}
        for label in ("one", "two"):
            client_id = str(uuid.uuid4())
            cert, key, password = create_device_identity(directory / label, client_id)
            self.devices[label] = (client_id, cert, client_context(
                self.cert, certfile=cert, keyfile=key, password=password))
        one_id, one_cert, _ = self.devices["one"]
        self.registry.approve(one_id, "Γ1", one_cert.read_bytes(), name="Board1")
        self.trust = RegistryTrust(self.registry, self.cert, self.key)
        self.trust.reports._on_event = lambda _text: None
        self.broadcast = AudioBroadcaster()
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.stop = threading.Event()
        self.listener = threading.Thread(
            target=accept_clients, args=(self.server, self.broadcast, self.stop),
            kwargs={"tls_context": self.trust.context(), "registry": self.registry,
                    "context_provider": self.trust.context, "reports": self.trust.reports},
            daemon=True)
        self.listener.start()

    def tearDown(self):
        self.stop.set()
        self.listener.join(timeout=2)
        self.broadcast.close()
        self.server.close()

    def hello(self, label, seen):
        client_id, _cert, context = self.devices[label]
        connection = socket.create_connection(self.server.getsockname(), timeout=3)
        connection.settimeout(3)
        connection = connect_tls(connection, context)
        try:
            send_hello(connection, client_id, "Γ1", seen=seen)
            return receive_welcome(connection), registry_seen.manager_key(
                connection.getpeercert(binary_form=True))
        finally:
            connection.close()

    def wait_reported(self):
        deadline = time.monotonic() + 2
        while self.trust.reported() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.trust.reported()

    def test_welcome_and_report_from_approved_client(self):
        welcome, key = self.hello("one", None)
        version, registry_hash = self.registry.welcome_info()
        self.assertEqual(welcome, {"rv": version, "hash": registry_hash})
        self.assertEqual(key, registry_seen.manager_key(
            __import__("ssl").PEM_cert_to_DER_cert(self.cert.read_text())))
        self.assertIsNone(self.trust.reported())
        self.hello("one", {"rv": version + 5, "hash": HASH_B})
        reported = self.wait_reported()
        self.assertEqual((reported.rv, reported.reporters), (version + 5, ("Γ1 · Board1",)))

    def test_unapproved_client_is_not_recorded(self):
        version = self.registry.version
        try:
            self.hello("two", {"rv": version + 5, "hash": HASH_B})
        except (OSError, EOFError, ValueError):
            pass
        time.sleep(0.2)
        self.assertIsNone(self.trust.reported())

    def test_revoked_client_report_no_longer_counts(self):
        version = self.registry.version
        self.hello("one", {"rv": version + 5, "hash": HASH_B})
        self.assertIsNotNone(self.wait_reported())
        self.trust.revoke(self.devices["one"][0], self.broadcast)
        self.assertIsNone(self.trust.reported())

    def test_accept_current_raises_version_and_silences(self):
        version = self.registry.version
        self.hello("one", {"rv": version + 5, "hash": HASH_B})
        self.assertIsNotNone(self.wait_reported())
        with self.assertRaises(RegistryError):
            self.trust.accept_current(newer_hosts=("ΓΡΑΜΜΑΤΕΙΑ-2",))
        self.assertEqual(self.trust.accept_current(), version + 6)
        self.assertIsNone(self.trust.reported())
        # Ο Client αναφέρει ξανά την παλιά έκδοση στην επόμενη σύνδεση: χωρίς ειδοποίηση.
        welcome, _key = self.hello("one", {"rv": version + 5, "hash": HASH_B})
        self.assertEqual(welcome["rv"], version + 6)
        time.sleep(0.1)
        self.assertIsNone(self.trust.reported())

    def test_accept_current_needs_report_and_primary(self):
        with self.assertRaises(RegistryError):
            self.trust.accept_current()
        version = self.registry.version
        self.hello("one", {"rv": version + 2, "hash": HASH_B})
        self.assertIsNotNone(self.wait_reported())
        state = manager_state.path_for(self.registry.path)
        manager_state.save(state, manager_state.ManagerState(role="backup", serving=True))
        with self.assertRaises(RoleError):
            self.trust.accept_current()
        self.assertEqual(self.registry.version, version)


if __name__ == "__main__":
    unittest.main()
