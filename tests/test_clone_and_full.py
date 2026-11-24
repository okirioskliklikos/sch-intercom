"""4.3C1: discovery με δοκιμή TLS, «γεμάτος» Manager, κλωνοποιημένοι υπολογιστές."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

from client.identity_reset import (CLONED, archive_identity, check_clone, previous_room,
                                   remember_machine)
from client.main import _verified_connection, run_with_reconnect
from director.broadcast import AudioBroadcaster
from shared.discovery import MAX_CANDIDATES, discover_candidates, encode_beacon
from shared.identity import ManagerFull, WELCOME_FULL, receive_welcome, send_welcome
from shared.machine_binding import (Machine, binding_path, clone_reason, load_binding,
                                    normalize_mac)
from shared.manager_pin import load_pin, save_pin
from shared.registry_seen import manager_key

TLS = ssl.create_default_context()
ROOM = "C2627-Γ1"


class FakeTLS:
    """Σύνδεση που «πέρασε» το TLS, με συγκεκριμένο πιστοποιητικό Manager."""

    def __init__(self, cert: bytes):
        self.cert, self.closed = cert, False

    def getpeercert(self, binary_form=False):
        return self.cert

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as test:
        test.bind(("127.0.0.1", 0))
        return test.getsockname()[1]


class MachineBindingTests(unittest.TestCase):
    def test_mac_filter_keeps_only_physical_addresses(self):
        self.assertEqual(normalize_mac("A4-BB-6D-12-34-56"), "a4:bb:6d:12:34:56")
        for virtual in ("00:00:00:00:00:00", "02:42:ac:11:00:02",   # τοπικά διαχειριζόμενη
                        "01:00:5e:00:00:01",                         # multicast
                        "00:15:5D:01:02:03", "00:50:56:aa:bb:cc",    # Hyper-V, VMware
                        "08:00:27:11:22:33", "00:FF:12:34:56:78",    # VirtualBox, TAP
                        "not a mac"):
            self.assertIsNone(normalize_mac(virtual), virtual)

    def test_clone_reasons(self):
        original = Machine("guid-a", frozenset({"m1", "m2"}))
        self.assertIsNone(clone_reason(original, Machine("guid-a", frozenset({"m2", "m9"}))))
        self.assertEqual(clone_reason(original, Machine("guid-b", frozenset({"m1"}))),
                         "άλλο MachineGuid")
        # Κλώνος δίσκου χωρίς sysprep: ίδιο MachineGuid, άλλες κάρτες.
        self.assertIn("κάρτες δικτύου",
                      clone_reason(original, Machine("guid-a", frozenset({"m7"}))))
        # Χωρίς στοιχεία δεν αποφασίζουμε «κλώνος».
        self.assertIsNone(clone_reason(original, Machine(None, frozenset())))
        self.assertIsNone(clone_reason(Machine(None, frozenset()), original))

    def test_real_machine_reading_never_fails(self):
        from shared.machine_binding import current_machine
        machine = current_machine()
        self.assertTrue(machine.machine_id is None or len(machine.machine_id) == 32)


class CloneDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / ".school-intercom"
        self.identity = self.base / "client.json"
        self.credentials = self.base / "credentials"
        self.hints = self.base / "client-hints.json"
        self.credentials.mkdir(parents=True)
        self.identity.write_text(json.dumps({"client_id": str(uuid.uuid4()), "room": ROOM}),
                                 encoding="utf-8")
        (self.credentials / "client-key.pem").write_text("key", encoding="utf-8")
        self.hints.write_text("{}", encoding="utf-8")
        self.original = Machine("guid-a", frozenset({"m1"}))

    def test_identity_without_fingerprint_is_adopted_once(self):
        self.assertIsNone(check_clone(self.identity, self.credentials, machine=self.original))
        self.assertEqual(load_binding(binding_path(self.credentials)), self.original)

    def test_same_machine_learns_new_network_card(self):
        remember_machine(self.credentials, machine=self.original)
        same = Machine("guid-a", frozenset({"m1", "usb-wifi"}))
        self.assertIsNone(check_clone(self.identity, self.credentials, machine=same))
        self.assertEqual(load_binding(binding_path(self.credentials)).macs,
                         frozenset({"m1", "usb-wifi"}))
        # Αργότερα βγαίνει η αρχική κάρτα: μένει η USB, άρα ίδιος υπολογιστής.
        self.assertIsNone(check_clone(self.identity, self.credentials,
                                      machine=Machine("guid-a", frozenset({"usb-wifi"}))))

    def test_cloned_disk_is_detected(self):
        remember_machine(self.credentials, machine=self.original)
        clone = Machine("guid-a", frozenset({"m2"}))
        self.assertIsNotNone(check_clone(self.identity, self.credentials, machine=clone))
        # Ο έλεγχος δεν αλλάζει το αποτύπωμα του αρχικού.
        self.assertEqual(load_binding(binding_path(self.credentials)), self.original)

    def test_archive_moves_everything_and_never_overwrites(self):
        now = datetime(2026, 11, 24, 20, 30, 0)
        self.assertEqual(previous_room(self.identity), ROOM)
        first = archive_identity([self.identity, self.credentials, self.hints], CLONED, now=now)
        self.assertEqual(first.name, "cloned-20261124-203000")
        self.assertFalse(self.identity.exists() or self.credentials.exists()
                         or self.hints.exists())
        self.assertTrue((first / "credentials" / "client-key.pem").exists())
        self.identity.write_text("{}", encoding="utf-8")
        second = archive_identity([self.identity], CLONED, now=now)
        self.assertNotEqual(first, second)
        self.assertTrue((first / "client.json").exists())
        self.assertIsNone(archive_identity([self.identity], CLONED, now=now))


class ManagerPinTests(unittest.TestCase):
    def test_round_trip_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "manager-pin.json"
            self.assertIsNone(load_pin(path))
            director = str(uuid.uuid4())
            self.assertTrue(save_pin(path, director, "a" * 64))
            self.assertFalse(save_pin(path, director, "a" * 64))
            self.assertEqual(load_pin(path), {"director_id": director, "fingerprint": "a" * 64})
            path.write_text("{broken", encoding="utf-8")
            self.assertIsNone(load_pin(path))


class DiscoveryCandidatesTests(unittest.TestCase):
    def send(self, port, packets, delay=0.06):
        def sender():
            time.sleep(delay)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                for packet in packets:
                    sock.sendto(packet, ("127.0.0.1", port))
        thread = threading.Thread(target=sender)
        thread.start()
        return thread

    def test_all_candidates_including_same_id_from_two_ports(self):
        port, real, fake = free_udp_port(), str(uuid.uuid4()), str(uuid.uuid4())
        thread = self.send(port, [encode_beacon(fake, 50999), encode_beacon(real, 50555),
                                  encode_beacon(real, 50998)])
        try:
            found = discover_candidates(udp_port=port, timeout=0.4)
        finally:
            thread.join()
        self.assertEqual(sorted(found), sorted([("127.0.0.1", 50999, fake),
                                               ("127.0.0.1", 50555, real),
                                               ("127.0.0.1", 50998, real)]))

    def test_preferred_filters_others_but_falls_back_when_absent(self):
        port, real, fake = free_udp_port(), str(uuid.uuid4()), str(uuid.uuid4())
        thread = self.send(port, [encode_beacon(fake, 50999), encode_beacon(real, 50555)])
        try:
            found = discover_candidates(udp_port=port, timeout=0.4, confirm=0.2,
                                        preferred_id=real)
        finally:
            thread.join()
        self.assertEqual(found, [("127.0.0.1", 50555, real)])
        # Ο προτιμώμενος δεν εμφανίζεται (π.χ. μεταφέρθηκε): δοκιμάζονται οι άλλοι.
        thread = self.send(port, [encode_beacon(fake, 50999)])
        try:
            found = discover_candidates(udp_port=port, timeout=0.4, preferred_id=real)
        finally:
            thread.join()
        self.assertEqual(found, [("127.0.0.1", 50999, fake)])

    def test_number_of_candidates_is_bounded(self):
        port = free_udp_port()
        thread = self.send(port, [encode_beacon(str(uuid.uuid4()), 50000 + n)
                                  for n in range(20)])
        try:
            found = discover_candidates(udp_port=port, timeout=0.4)
        finally:
            thread.join()
        self.assertEqual(len(found), MAX_CANDIDATES)


class VerifiedConnectionTests(unittest.TestCase):
    def connect_with(self, behaviour):
        def connect(host, port, **_):
            result = behaviour[(host, port)]
            if isinstance(result, Exception):
                raise result
            return result
        return mock.patch("client.main.open_connection", side_effect=connect)

    def test_fake_with_same_id_is_skipped(self):
        real = FakeTLS(b"real")
        real_id = str(uuid.uuid4())
        candidates = [("10.0.0.66", 50555, real_id), ("10.0.0.10", 50555, real_id)]
        behaviour = {("10.0.0.66", 50555): ssl.SSLCertVerificationError("untrusted"),
                     ("10.0.0.10", 50555): real}
        lines = []
        with self.connect_with(behaviour):
            chosen, found = _verified_connection(candidates, port=50555, pin=None,
                                                 log=lines.append)
        self.assertIs(chosen, real)
        self.assertEqual(found, real_id)
        self.assertTrue(any("δεν επαληθεύτηκε" in line for line in lines))

    def test_same_manager_on_two_network_cards_is_fine(self):
        first, second = FakeTLS(b"same"), FakeTLS(b"same")
        identifier = str(uuid.uuid4())
        behaviour = {("10.0.0.1", 50555): first, ("10.0.1.1", 50555): second}
        with self.connect_with(behaviour):
            chosen, _ = _verified_connection(
                [("10.0.0.1", 50555, identifier), ("10.0.1.1", 50555, identifier)],
                port=50555, pin=None, log=lambda _: None)
        self.assertIs(chosen, first)
        self.assertTrue(second.closed)

    def test_two_different_valid_managers_need_pin(self):
        a, b = FakeTLS(b"manager-a"), FakeTLS(b"manager-b")
        candidates = [("10.0.0.1", 50555, str(uuid.uuid4())),
                      ("10.0.0.2", 50555, str(uuid.uuid4()))]
        behaviour = {("10.0.0.1", 50555): a, ("10.0.0.2", 50555): b}
        with self.connect_with(behaviour), self.assertRaises(ValueError):
            _verified_connection(candidates, port=50555, pin=None, log=lambda _: None)
        self.assertTrue(a.closed and b.closed)
        a, b = FakeTLS(b"manager-a"), FakeTLS(b"manager-b")
        behaviour = {("10.0.0.1", 50555): a, ("10.0.0.2", 50555): b}
        pin = {"director_id": candidates[1][2], "fingerprint": manager_key(b"manager-b")}
        with self.connect_with(behaviour):
            chosen, found = _verified_connection(candidates, port=50555, pin=pin,
                                                 log=lambda _: None)
        self.assertIs(chosen, b)
        self.assertTrue(a.closed)

    def test_pin_is_saved_and_used_after_restart(self):
        identifier = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as folder:
            pin_file = Path(folder) / "manager-pin.json"
            preferred = []

            def discovery(**kwargs):
                preferred.append(kwargs["preferred_id"])
                return [("10.0.0.10", 50555, identifier)]

            def session(connection):
                raise EOFError("τέλος")
            for _ in range(2):          # δύο «εκκινήσεις» του Client
                with mock.patch("client.main.open_connection",
                                return_value=FakeTLS(b"manager")):
                    run_with_reconnect(None, 50555, session, max_sessions=1,
                                       tls_context=TLS, discovery=discovery,
                                       pin_file=pin_file, log=lambda _: None,
                                       sleep=lambda _: None)
            self.assertEqual(preferred, [None, identifier])
            self.assertEqual(load_pin(pin_file)["fingerprint"], manager_key(b"manager"))


class ManagerFullTests(unittest.TestCase):
    def test_full_status_round_trip(self):
        a, b = socket.socketpair()
        with a, b:
            send_welcome(a, accepted=False, full=True)
            with self.assertRaises(ManagerFull):
                receive_welcome(b)
        with self.assertRaises(ValueError):
            send_welcome(mock.Mock(), accepted=True, full=True)
        self.assertEqual(WELCOME_FULL, 2)

    def test_client_waits_thirty_seconds_and_says_why(self):
        delays, lines = [], []

        def session(connection):
            raise ManagerFull("γεμάτος")
        with mock.patch("client.main.open_connection", return_value=FakeTLS(b"m")):
            run_with_reconnect("10.0.0.1", 50555, session, max_sessions=2, tls_context=TLS,
                               log=lines.append, sleep=delays.append)
        self.assertEqual(len(delays), 1)
        self.assertTrue(29 <= delays[0] <= 31)
        self.assertTrue(any("όριο υπολογιστών" in line for line in lines))

    def test_manager_logs_once_per_minute_and_counts_waiting(self):
        broadcaster = AudioBroadcaster()
        self.addCleanup(broadcaster.close)
        now = [100.0]
        broadcaster.clock = lambda: now[0]
        one, two = str(uuid.uuid4()), str(uuid.uuid4())
        self.assertTrue(broadcaster.note_full(one, ROOM, "Board1"))
        now[0] += 30
        self.assertFalse(broadcaster.note_full(one, ROOM, "Board1"))
        self.assertTrue(broadcaster.note_full(two, ROOM, "Board2"))
        self.assertEqual(len(broadcaster.full_waiting()), 2)
        now[0] += 31
        self.assertTrue(broadcaster.note_full(one, ROOM, "Board1"))
        now[0] += 80                       # σταμάτησαν να ξαναδοκιμάζουν
        self.assertEqual(broadcaster.full_waiting(), [])


class DuplicateIdentityTests(unittest.TestCase):
    def add(self, broadcaster, client_id, ip):
        """Πραγματικό TCP ζεύγος (ο broadcaster ορίζει TCP_NODELAY)· η IP είναι δηλωμένη."""
        with socket.create_server(("127.0.0.1", 0)) as server:
            theirs = socket.create_connection(server.getsockname())
            ours, _ = server.accept()
        self.addCleanup(theirs.close)
        self.assertTrue(broadcaster.add_client(ours, client_id, ROOM, (ip, 40000)))

    def test_two_computers_with_same_identity_warn_once(self):
        events = []
        broadcaster = AudioBroadcaster(on_event=events.append)
        self.addCleanup(broadcaster.close)
        now = [0.0]
        broadcaster.clock = lambda: now[0]
        client_id = str(uuid.uuid4())
        for index in range(8):
            now[0] += 5
            self.add(broadcaster, client_id, "10.0.0.11" if index % 2 else "10.0.0.12")
        warnings = [event for event in events if "ΠΡΟΣΟΧΗ" in event]
        self.assertEqual(len(warnings), 1)
        self.assertIn("10.0.0.11, 10.0.0.12", warnings[0])
        self.assertEqual(broadcaster.duplicate_identities(),
                         [(client_id, ROOM, ("10.0.0.11", "10.0.0.12"))])

    def test_reconnect_from_same_or_new_address_is_not_a_clone(self):
        events = []
        broadcaster = AudioBroadcaster(on_event=events.append)
        self.addCleanup(broadcaster.close)
        client_id = str(uuid.uuid4())
        for ip in ("10.0.0.11", "10.0.0.11", "10.0.0.11", "10.0.0.20", "10.0.0.20"):
            self.add(broadcaster, client_id, ip)   # π.χ. νέα IP από DHCP μία φορά
        self.assertFalse(any("ΠΡΟΣΟΧΗ" in event for event in events))
        self.assertEqual(broadcaster.duplicate_identities(), [])


if __name__ == "__main__":
    unittest.main()
