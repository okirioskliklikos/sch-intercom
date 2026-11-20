"""Έλεγχοι discovery χωρίς μικρόφωνο, ηχεία ή εξωτερικό LAN."""

from pathlib import Path
from unittest import mock
import socket
import tempfile
import threading
import time
import unittest
import uuid

from shared.discovery import decode_beacon, encode_beacon, director_id, discover
from client.main import run_with_reconnect


class DiscoveryTests(unittest.TestCase):
    def test_stable_director_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "director.id"
            first = director_id(path)
            self.assertEqual(str(uuid.UUID(first)), first)
            self.assertEqual(director_id(path), first)

    def test_beacon_validation(self):
        identifier = str(uuid.uuid4())
        self.assertEqual(decode_beacon(encode_beacon(identifier, 50555)),
                         (identifier, 50555))
        for payload in (b"wrong", b"SID1:{}", b"SID1:[]",
                        b"SID1:" + b"x" * 200,
                        b'SID1:{"id":"x","port":50555}',
                        b'SID1:{"id":"' + identifier.encode() + b'","port":true}'):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    decode_beacon(payload)
        for port in (0, 65536):
            with self.assertRaises(ValueError):
                encode_beacon(identifier, port)

    def test_udp_loopback_ignores_invalid_and_accepts_valid(self):
        identifier = str(uuid.uuid4())
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as test:
            test.bind(("127.0.0.1", 0))
            port = test.getsockname()[1]
        def sender():
            time.sleep(0.06)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.sendto(b"invalid", ("127.0.0.1", port))
                sock.sendto(encode_beacon(identifier, 50555), ("127.0.0.1", port))
        thread = threading.Thread(target=sender)
        thread.start()
        try:
            self.assertEqual(discover(udp_port=port, timeout=0.3),
                             ("127.0.0.1", 50555, identifier))
        finally:
            thread.join()

    def test_ambiguous_directors_rejected(self):
        identifiers = [str(uuid.uuid4()), str(uuid.uuid4())]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as test:
            test.bind(("127.0.0.1", 0))
            port = test.getsockname()[1]
        def sender():
            time.sleep(0.06)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                for identifier in identifiers:
                    sock.sendto(encode_beacon(identifier, 50555), ("127.0.0.1", port))
        thread = threading.Thread(target=sender)
        thread.start()
        try:
            with self.assertRaisesRegex(ValueError, "πολλοί Directors"):
                discover(udp_port=port, timeout=0.3)
        finally:
            thread.join()

    def _send_beacons(self, port, beacons, delay=0.06):
        """Στέλνει (identifier, tcp_port) beacons στο localhost από νέο thread."""
        def sender():
            time.sleep(delay)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                for identifier, tcp_port in beacons:
                    sock.sendto(encode_beacon(identifier, tcp_port), ("127.0.0.1", port))
        thread = threading.Thread(target=sender)
        thread.start()
        return thread

    @staticmethod
    def _free_udp_port():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as test:
            test.bind(("127.0.0.1", 0))
            return test.getsockname()[1]

    def test_preferred_director_returns_after_confirm_window(self):
        identifier = str(uuid.uuid4())
        port = self._free_udp_port()
        thread = self._send_beacons(port, [(identifier, 50555)])
        try:
            start = time.monotonic()
            result = discover(udp_port=port, timeout=2.0, preferred_id=identifier,
                              confirm=0.3)
            elapsed = time.monotonic() - start
        finally:
            thread.join()
        self.assertEqual(result, ("127.0.0.1", 50555, identifier))
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 1.0)

    def test_without_preferred_id_waits_full_window(self):
        identifier = str(uuid.uuid4())
        port = self._free_udp_port()
        thread = self._send_beacons(port, [(identifier, 50555)])
        try:
            start = time.monotonic()
            result = discover(udp_port=port, timeout=0.6, confirm=0.1)
            elapsed = time.monotonic() - start
        finally:
            thread.join()
        self.assertEqual(result, ("127.0.0.1", 50555, identifier))
        self.assertGreaterEqual(elapsed, 0.55)

    def test_default_confirm_window_is_shorter_than_full_window(self):
        from shared.discovery import ANNOUNCE_INTERVAL, CONFIRM_WINDOW, DISCOVERY_WINDOW
        self.assertGreater(CONFIRM_WINDOW, ANNOUNCE_INTERVAL)
        self.assertLess(CONFIRM_WINDOW, DISCOVERY_WINDOW)

    def test_same_id_from_two_addresses_rejected(self):
        identifier = str(uuid.uuid4())
        for preferred in (None, identifier):
            with self.subTest(preferred=preferred):
                port = self._free_udp_port()
                thread = self._send_beacons(port, [(identifier, 50555), (identifier, 50999)])
                try:
                    with self.assertRaisesRegex(ValueError, "πολλές διευθύνσεις"):
                        discover(udp_port=port, timeout=0.4, preferred_id=preferred,
                                 confirm=0.3)
                finally:
                    thread.join()

    def test_conflict_logged_and_retried_by_client(self):
        calls = []
        def discovery(**kwargs):
            calls.append(kwargs)
            raise ValueError("Το ID Director εμφανίστηκε από πολλές διευθύνσεις")
        lines = []
        with mock.patch("client.main.open_connection") as connect:
            run_with_reconnect(None, 50555, lambda _: None, max_sessions=2,
                               discovery=discovery, sleep=lambda _: None, log=lines.append)
        connect.assert_not_called()
        self.assertEqual(len(calls), 2)
        self.assertTrue(any("πολλές διευθύνσεις" in line for line in lines))

    def test_preferred_director_and_ip_change_on_reconnect(self):
        identifier = str(uuid.uuid4())
        discovered = []
        addresses = iter(["127.0.0.1", "127.0.0.2"])
        def discovery(**kwargs):
            discovered.append(kwargs["preferred_id"])
            return next(addresses), 50555, identifier
        sessions = []
        class Connection:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
        def session(connection):
            sessions.append(connection)
            raise EOFError("test disconnect")
        with mock.patch("client.main.open_connection", return_value=Connection()) as open_mock:
            run_with_reconnect(None, 50555, session, max_sessions=2,
                               discovery=discovery, sleep=lambda _: None, log=lambda _: None)
        self.assertEqual(discovered, [None, identifier])
        self.assertEqual([call.args[0] for call in open_mock.call_args_list],
                         ["127.0.0.1", "127.0.0.2"])
        self.assertEqual(len(sessions), 2)

    def test_manual_host_bypasses_discovery(self):
        class Connection:
            def __enter__(self): return self
            def __exit__(self, *args): return False
        with mock.patch("client.main.open_connection", return_value=Connection()) as connect:
            run_with_reconnect("192.0.2.1", 50555, lambda _: None, max_sessions=1,
                               discovery=lambda **_: self.fail("unexpected discovery"),
                               log=lambda _: None)
        self.assertEqual(connect.call_args.args[:2], ("192.0.2.1", 50555))


if __name__ == "__main__":
    unittest.main()
