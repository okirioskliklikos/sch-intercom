"""Έλεγχοι discovery χωρίς μικρόφωνο, ηχεία ή εξωτερικό LAN."""

from pathlib import Path
from unittest import mock
import socket
import ssl
import tempfile
import threading
import time
import unittest
import uuid

from shared.discovery import (BeaconWatcher, decode_beacon, decode_beacon_info,
                              encode_beacon, director_id, discover)
from client.main import open_connection, run_with_reconnect
from tests.fake_responder import FakeResponder

# Τα open_connection είναι mock· αρκεί έγκυρο SSLContext για τον έλεγχο «TLS υποχρεωτικό».
TLS = ssl.create_default_context()


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

    def test_beacon_v2_complete_fields_and_validation(self):
        identifier, school, instance = (str(uuid.uuid4()) for _ in range(3))
        packet = encode_beacon(identifier, 50555, school=school, inst=instance,
                               rv=9, host="Manager-A")
        info = decode_beacon_info(packet)
        self.assertEqual((info.id, info.port, info.school, info.inst, info.rv, info.host),
                         (identifier, 50555, school, instance, 9, "Manager-A"))
        self.assertEqual(decode_beacon(packet), (identifier, 50555))
        self.assertLessEqual(len(packet), 512)
        import json
        from shared.discovery import MAGIC
        value = json.loads(packet[len(MAGIC):])
        for bad in ({"rv": True}, {"rv": 0}, {"host": "bad\nname"}, {"host": "tab\there"},
                    {"port": "50555"}, {"school": "other"}, {"unknown": 1}):
            copy = dict(value)
            copy.update(bad)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                decode_beacon_info(MAGIC + json.dumps(copy).encode())
        copy = dict(value)
        copy.pop("inst")
        with self.assertRaises(ValueError):
            decode_beacon_info(MAGIC + json.dumps(copy).encode())
        with self.assertRaises(ValueError):
            decode_beacon_info(b"SID1:" + packet[len(MAGIC):])
        with self.assertRaises(ValueError):
            decode_beacon_info(packet + b" " * 513)

    def test_watcher_filters_instances_and_expires(self):
        school, self_id, other_id = (str(uuid.uuid4()) for _ in range(3))
        now = [100.0]
        watcher = BeaconWatcher(school, self_id, clock=lambda: now[0])
        uid = str(uuid.uuid4())
        watcher.receive(encode_beacon(uid, 50555, school=school,
                                      inst=self_id, rv=1, host="self"), "127.0.0.1")
        watcher.receive(encode_beacon(uid, 50555, school=str(uuid.uuid4()),
                                      inst=other_id, rv=5, host="other-school"), "127.0.0.2")
        self.assertEqual(watcher.others(), [])
        watcher.receive(encode_beacon(uid, 50555, school=school,
                                      inst=other_id, rv=5, host="other"), "127.0.0.2")
        self.assertEqual(len(watcher.newer(2)), 1)
        now[0] += 5.1
        self.assertEqual(watcher.others(), [])

    def test_udp_loopback_ignores_invalid_and_accepts_valid(self):
        identifier = str(uuid.uuid4())
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as test:
            test.bind(("127.0.0.1", 0))
            port = test.getsockname()[1]
        thread = FakeResponder(port, [b"invalid", encode_beacon(identifier, 50555)])
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
        thread = FakeResponder(port, [encode_beacon(i, 50555) for i in identifiers])
        try:
            with self.assertRaisesRegex(ValueError, "πολλοί Directors"):
                discover(udp_port=port, timeout=0.3)
        finally:
            thread.join()

    def _send_beacons(self, port, beacons, delay=0.06):
        """Ψεύτικος Manager που απαντά με (identifier, tcp_port) beacons."""
        return FakeResponder(port, [encode_beacon(identifier, tcp_port)
                                    for identifier, tcp_port in beacons])

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
            run_with_reconnect(None, 50555, lambda _: None, max_sessions=2, tls_context=TLS,
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
            run_with_reconnect(None, 50555, session, max_sessions=2, tls_context=TLS,
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
            run_with_reconnect("192.0.2.1", 50555, lambda _: None, max_sessions=1, tls_context=TLS,
                               discovery=lambda **_: self.fail("unexpected discovery"),
                               log=lambda _: None)
        self.assertEqual(connect.call_args.args[:2], ("192.0.2.1", 50555))


    def test_fake_director_failing_tls_does_not_lock_client(self):
        fake, real = str(uuid.uuid4()), str(uuid.uuid4())
        found = iter([("192.0.2.66", 50555, fake), ("192.0.2.10", 50555, real),
                      ("192.0.2.11", 50555, real)])
        preferred = []
        def discovery(**kwargs):
            preferred.append(kwargs["preferred_id"])
            return next(found)
        class Connection:
            def __enter__(self): return self
            def __exit__(self, *args): return False
        def connect(host, *args, **kwargs):
            if host == "192.0.2.66":
                raise ssl.SSLCertVerificationError("untrusted Director")
            return Connection()
        def session(connection):
            raise EOFError("test disconnect")
        with mock.patch("client.main.open_connection", side_effect=connect) as open_mock:
            run_with_reconnect(None, 50555, session, max_sessions=3, tls_context=TLS,
                               discovery=discovery, sleep=lambda _: None, log=lambda _: None)
        # Μετά την αποτυχία TLS δεν προτιμά τον ψεύτικο· κλειδώνει στον πραγματικό.
        self.assertEqual(preferred, [None, None, real])
        self.assertEqual([c.args[0] for c in open_mock.call_args_list],
                         ["192.0.2.66", "192.0.2.10", "192.0.2.11"])

    def test_client_refuses_to_connect_without_tls(self):
        with mock.patch("client.main.open_connection") as open_mock:
            with self.assertRaises(ValueError):
                run_with_reconnect("192.0.2.1", 50555, lambda _: None, max_sessions=1,
                                   log=lambda _: None)
        open_mock.assert_not_called()
        with self.assertRaises(ValueError):
            open_connection("127.0.0.1", 9)


class WindowsLikeSocket(socket.socket):
    """Όπως στα Windows: datagram μεγαλύτερο από το buffer δίνει WSAEMSGSIZE, δεν κόβεται."""

    def recvfrom(self, size, *args):
        data, address = super().recvfrom(65535, *args)
        if len(data) > size:
            raise OSError(10040, "WSAEMSGSIZE: message too long")
        return data, address


class OversizedPacketTests(unittest.TestCase):
    """4.3B, εύρημα της πραγματικής δοκιμής Windows: ένα υπερμεγέθες UDP πακέτο
    έκανε το discovery να αποτυγχάνει και σταματούσε για πάντα τον BeaconWatcher."""

    def free_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as test:
            test.bind(("127.0.0.1", 0))
            return test.getsockname()[1]

    def test_discover_ignores_oversized_packet(self):
        identifier, port = str(uuid.uuid4()), self.free_port()
        with mock.patch("shared.discovery.socket.socket", WindowsLikeSocket):
            thread = FakeResponder(port, [b"\x00" * 600, encode_beacon(identifier, 50555)])
            try:
                self.assertEqual(discover(udp_port=port, timeout=0.5),
                                 ("127.0.0.1", 50555, identifier))
            finally:
                thread.join()

    def test_watcher_keeps_running_after_oversized_packet(self):
        school, inst, other = (str(uuid.uuid4()) for _ in range(3))
        port = self.free_port()
        with mock.patch("shared.discovery.socket.socket", WindowsLikeSocket):
            watcher = BeaconWatcher(school, inst, port=port)
            watcher.start()
            self.addCleanup(watcher.close)
            time.sleep(0.1)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.sendto(b"\x00" * 600, ("127.0.0.1", port))
                time.sleep(0.1)
                sock.sendto(encode_beacon(str(uuid.uuid4()), 50555, school=school,
                                          inst=other, host="pc-b"), ("127.0.0.1", port))
            deadline = time.monotonic() + 2
            while not watcher.others() and time.monotonic() < deadline:
                time.sleep(0.02)
        self.assertEqual([item.info.inst for item in watcher.others()], [other])


if __name__ == "__main__":
    unittest.main()


class FakeSender:
    def __init__(self, fail=False):
        self.sent, self.fail, self.closed = [], fail, False

    def sendto(self, packet, target):
        if self.fail:
            raise OSError("down")
        self.sent.append(target)

    def close(self):
        self.closed = True


class BroadcastSendersTests(unittest.TestCase):
    """4.4 Εκτέλεση 3: στα Windows broadcast από κάθε κάρτα δικτύου."""

    def make(self, addresses, *, per_interface=True):
        from shared import discovery
        now = [0.0]
        opened = {}

        def fake_open(address=None):
            opened[address] = FakeSender(fail=address == "10.0.0.9")
            return opened[address]

        with mock.patch.object(discovery.BroadcastSenders, "_open", staticmethod(fake_open)):
            senders = discovery.BroadcastSenders(50556, per_interface=per_interface,
                                                 addresses=lambda: list(addresses),
                                                 clock=lambda: now[0])
            senders.refresh()
        senders._open = fake_open
        return senders, opened, now

    def test_sends_from_every_interface(self):
        senders, opened, _ = self.make(["192.168.2.58", "172.20.0.1"])
        self.assertEqual(senders.send(b"x"), 2)
        for address in ("192.168.2.58", "172.20.0.1"):
            self.assertEqual(opened[address].sent, [("255.255.255.255", 50556)])
        self.assertEqual(opened[None].sent, [])

    def test_linux_keeps_single_socket(self):
        senders, opened, _ = self.make(["192.168.2.58"], per_interface=False)
        self.assertEqual(senders.send(b"x"), 1)
        self.assertEqual(list(opened), [None])

    def test_no_addresses_falls_back_to_default(self):
        senders, opened, _ = self.make([])
        self.assertEqual(senders.send(b"x"), 1)
        self.assertEqual(len(opened[None].sent), 1)

    def test_one_failing_interface_does_not_stop_others(self):
        senders, opened, _ = self.make(["10.0.0.9", "192.168.2.58"])
        self.assertEqual(senders.send(b"x"), 1)

    def test_all_failing_raises(self):
        senders, _, _ = self.make(["10.0.0.9"])
        with self.assertRaises(OSError):
            senders.send(b"x")

    def test_addresses_refreshed_after_interval(self):
        from shared import discovery
        addresses = ["192.168.2.58"]
        senders, opened, now = self.make(addresses)
        senders._addresses = lambda: list(addresses)
        addresses[:] = ["192.168.2.77"]
        senders.send(b"x")
        self.assertEqual(set(senders.bound), {"192.168.2.58"})   # ακόμη μέσα στο διάστημα
        now[0] = discovery.INTERFACE_REFRESH_SECONDS + 1
        senders.send(b"x")
        self.assertEqual(set(senders.bound), {"192.168.2.77"})
        self.assertTrue(opened["192.168.2.58"].closed)

    def test_local_addresses_skip_loopback(self):
        from shared import discovery
        fake = [(socket.AF_INET, 0, 0, "", ("127.0.1.1", 0)),
                (socket.AF_INET, 0, 0, "", ("192.168.2.58", 0)),
                (socket.AF_INET, 0, 0, "", ("192.168.2.58", 0))]
        with mock.patch.object(discovery.socket, "getaddrinfo", return_value=fake):
            self.assertEqual(discovery.local_ipv4_addresses(), ["192.168.2.58"])

    def test_real_announce_answers_queries_and_feeds_watchers(self):
        """Ο Client ρωτά και ο Manager απαντά· τα beacons φτάνουν ακόμη σε test watcher port."""
        from shared.discovery import announce, decode_beacon, discover
        identifier = str(uuid.uuid4())
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as watcher:
            watcher.bind(("127.0.0.1", 50597))
            watcher.settimeout(2.0)
            stop = threading.Event()
            worker = threading.Thread(target=announce, args=(stop, identifier, 50599),
                                      kwargs={"udp_port": 50598, "interval": 0.2,
                                              "extra_ports": (50597,),
                                              "report": lambda *_: None},
                                      daemon=True)
            worker.start()
            try:
                found = discover(udp_port=50598, timeout=2.0)
                beacon, _ = watcher.recvfrom(1024)
            finally:
                stop.set()
                worker.join(2)
        self.assertIsNotNone(found)
        self.assertEqual(found[1:], (50599, identifier))
        self.assertEqual(decode_beacon(beacon), (identifier, 50599))

    def test_client_does_not_listen_on_discovery_port(self):
        """Firewall (VM Windows 10, 28/11/2026): ο Client δεν δένεται στη θύρα discovery."""
        from shared.discovery import discover_candidates
        port = self._free_port()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as other:
            other.bind(("", port))   # χωρίς SO_REUSEADDR: θα αποτύγχανε αν ο Client την έπιανε
            self.assertEqual(discover_candidates(udp_port=port, timeout=0.3), [])

    def test_reply_limiter(self):
        from shared.discovery import ReplyLimiter
        now = [0.0]
        limiter = ReplyLimiter(per_ip=2, total=3, clock=lambda: now[0])
        self.assertEqual([limiter.allow("a") for _ in range(3)], [True, True, False])
        self.assertTrue(limiter.allow("b"))
        self.assertFalse(limiter.allow("c"))
        now[0] = 1.0
        self.assertTrue(limiter.allow("a"))

    def test_query_format(self):
        from shared.discovery import QUERY_BYTES, encode_query, is_query
        self.assertEqual(len(encode_query()), QUERY_BYTES)
        self.assertTrue(is_query(encode_query()))
        self.assertFalse(is_query(encode_query()[:20]))
        self.assertFalse(is_query(encode_beacon(str(uuid.uuid4()), 50555)))

    @staticmethod
    def _free_port():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as test:
            test.bind(("", 0))
            return test.getsockname()[1]
