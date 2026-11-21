"""Πραγματικά TCP loopback sockets, χωρίς μικρόφωνο/ηχεία."""

import socket
import threading
import time
import unittest
import uuid

from director.broadcast import AudioBroadcaster, _ClientSession
from director.main import accept_clients
from shared.audio import (BLOCK_BYTES, Frame, MESSAGE_KEEPALIVE, MESSAGE_STREAM_STOP,
                          receive_frame, receive_message)
from shared.identity import receive_welcome, send_hello
from tests.tls_support import LocalTLSFixture


def pcm(n):
    return bytes([n % 256]) * BLOCK_BYTES


class MultiClientTests(LocalTLSFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.broadcast = AudioBroadcaster(queue_blocks=5)
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(32)
        self.stop = threading.Event()
        self.listener = threading.Thread(target=accept_clients,
                                         args=(self.server, self.broadcast, self.stop),
                                         kwargs={"tls_context": self.tls_server_context},
                                         daemon=True)
        self.listener.start()
        self.clients = []
        self.identities = []

    def tearDown(self):
        self.stop.set()
        self.listener.join(timeout=1)
        self.broadcast.close()
        for s in self.clients:
            s.close()
        self.server.close()

    def connect_clients(self, number):
        for _ in range(number - len(self.clients)):
            s = self.connect_tls_client(self.server.getsockname())
            index = len(self.clients)
            client_id = str(uuid.UUID(int=index + 1))
            room = "C2627-Γ1" if index % 2 == 0 else "C2627-Γ2"
            send_hello(s, client_id, room)
            receive_welcome(s)
            self.clients.append(s)
            self.identities.append((client_id, room))
        end = time.monotonic() + 3
        while self.broadcast.client_count != number and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(self.broadcast.client_count, number)

    def test_silent_connection_does_not_delay_other_clients(self):
        silent = socket.create_connection(self.server.getsockname())
        self.addCleanup(silent.close)
        time.sleep(0.1)   # ο Director έχει δεχτεί τη σιωπηλή σύνδεση
        started = time.monotonic()
        self.connect_clients(1)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_pending_handshakes_are_bounded(self):
        self.stop.set()
        self.listener.join(timeout=1)
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(8)
        self.addCleanup(server.close)
        stop = threading.Event()
        self.addCleanup(stop.set)
        threading.Thread(target=accept_clients, args=(server, self.broadcast, stop),
                         kwargs={"tls_context": self.tls_server_context, "max_pending": 1},
                         daemon=True).start()
        silent = socket.create_connection(server.getsockname())
        self.addCleanup(silent.close)
        time.sleep(0.1)
        extra = socket.create_connection(server.getsockname())
        self.addCleanup(extra.close)
        extra.settimeout(1)
        try:
            self.assertEqual(extra.recv(1), b"")   # κλείνει αμέσως, όχι μετά από 3 s
        except ConnectionResetError:
            pass

    def test_50_simultaneous_clients_all_connect(self):
        """Πρωινό άνοιγμα: 50 Clients συνδέονται ταυτόχρονα, με λίγες θέσεις
        χειραψίας· όσοι απορρίπτονται ξαναδοκιμάζουν και τελικά συνδέονται όλοι."""
        self.stop.set()
        self.listener.join(timeout=1)
        broadcast = AudioBroadcaster(queue_blocks=5, max_clients=64)
        self.addCleanup(broadcast.close)
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(128)
        self.addCleanup(server.close)
        stop = threading.Event()
        self.addCleanup(stop.set)
        threading.Thread(target=accept_clients, args=(server, broadcast, stop),
                         kwargs={"tls_context": self.tls_server_context, "max_pending": 8},
                         daemon=True).start()
        sockets, lock = [], threading.Lock()
        def client(index):
            for _ in range(40):          # επανάληψη όπως ο πραγματικός Client
                try:
                    s = self.connect_tls_client(server.getsockname())
                    send_hello(s, str(uuid.UUID(int=index + 1)), f"Αίθουσα {index}")
                    receive_welcome(s)
                    with lock:
                        sockets.append(s)
                    return
                except (OSError, EOFError, ValueError):
                    time.sleep(0.05)
        threads = [threading.Thread(target=client, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        for s in sockets:
            self.addCleanup(s.close)
        end = time.monotonic() + 3
        while broadcast.client_count != 50 and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(broadcast.client_count, 50)

    def test_accept_requires_tls_context(self):
        with self.assertRaises(ValueError):
            accept_clients(self.server, self.broadcast, threading.Event())

    def test_up_to_70_clients_receive_identical_frames(self):
        self.assertEqual(self.broadcast.max_clients, 70)
        for count in (2, 5, 10, 20, 40, 70):
            with self.subTest(clients=count):
                self.connect_clients(count)
                received = [None] * count
                errors = []

                def read(index):
                    try:
                        received[index] = receive_frame(self.clients[index])
                    except Exception as exc:
                        errors.append(exc)

                threads = [threading.Thread(target=read, args=(i,)) for i in range(count)]
                for thread in threads:
                    thread.start()
                self.broadcast.publish(pcm(count), count * 1000)
                for thread in threads:
                    thread.join(timeout=3)
                self.assertFalse(errors, str(errors))
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertEqual([x.pcm for x in received], [pcm(count)] * count)
                self.assertEqual(len({x.seq for x in received}), 1)

    def test_15_clients_receive_continuous_stream(self):
        self.connect_clients(15)
        received = [[] for _ in self.clients]
        errors = []

        def collect(i):
            try:
                for _ in range(25):
                    received[i].append(receive_frame(self.clients[i]))
            except Exception as exc:
                errors.append(exc)

        readers = [threading.Thread(target=collect, args=(i,)) for i in range(15)]
        for reader in readers:
            reader.start()
        for n in range(25):
            self.broadcast.publish(pcm(n), n * 20_000)
            time.sleep(0.02)
        for reader in readers:
            reader.join(timeout=3)
        self.assertFalse(errors, str(errors))
        self.assertTrue(all(not reader.is_alive() for reader in readers))
        for frames in received:
            self.assertEqual([f.seq for f in frames], list(range(25)))
            self.assertEqual([f.pcm for f in frames], [pcm(n) for n in range(25)])

    def test_disconnect_does_not_block_healthy_client(self):
        self.connect_clients(2)
        self.clients[0].shutdown(socket.SHUT_RDWR)
        self.clients[0].close()
        self.broadcast.publish(pcm(42), 123)
        frame = receive_frame(self.clients[1])
        self.assertEqual(frame.pcm, pcm(42))

    def test_slow_client_queue_bounded(self):
        sender, receiver = socket.socketpair()
        try:
            # Χωρίς εκκίνηση sender thread: προσομοίωση Client που δεν διαβάζει.
            session = _ClientSession(str(uuid.UUID(int=99)), "C2627-Γ1", sender, "slow", 5, lambda _: None)
            dropped = sum(session.enqueue(pcm(n)) for n in range(100))
            self.assertEqual(session.pending.qsize(), 5)
            self.assertEqual(dropped, 95)
            self.assertEqual(list(session.pending.queue), [pcm(n) for n in range(95, 100)])
            session.stop()
        finally:
            receiver.close()

    def test_routing_rooms_individual_and_all(self):
        self.connect_clients(3)
        selected_id = self.identities[1][0]

        def next_non_keepalive(connection):
            until = time.monotonic() + 2
            while time.monotonic() < until:
                message = receive_message(connection)
                if isinstance(message, Frame) or message.kind != MESSAGE_KEEPALIVE:
                    return message
            self.fail("Δεν εμφανίστηκε PCM/STOP")

        self.broadcast.set_targets(rooms={"C2627-Γ1"}, client_ids=set())
        self.broadcast.publish(pcm(1), 1000)
        self.assertEqual(next_non_keepalive(self.clients[0]).pcm, pcm(1))
        self.assertEqual(next_non_keepalive(self.clients[1]).kind, MESSAGE_STREAM_STOP)
        self.assertEqual(next_non_keepalive(self.clients[2]).pcm, pcm(1))

        self.broadcast.set_targets(rooms=set(), client_ids={selected_id})
        self.broadcast.publish(pcm(2), 2000)
        self.assertEqual(next_non_keepalive(self.clients[0]).kind, MESSAGE_STREAM_STOP)
        self.assertEqual(next_non_keepalive(self.clients[1]).pcm, pcm(2))
        self.assertEqual(next_non_keepalive(self.clients[2]).kind, MESSAGE_STREAM_STOP)

        self.broadcast.set_targets(rooms=set(), client_ids=set())
        self.assertEqual(next_non_keepalive(self.clients[1]).kind, MESSAGE_STREAM_STOP)
        # Σε πλήρη αναμονή δεν διακινείται PCM, μόνο keepalive.
        for connection in self.clients:
            message = receive_message(connection)
            self.assertEqual(message.kind, MESSAGE_KEEPALIVE)

        self.broadcast.set_targets()
        self.broadcast.publish(pcm(4), 4000)
        for connection in self.clients:
            self.assertEqual(next_non_keepalive(connection).pcm, pcm(4))

    def test_duplicate_identity_replaces_old_connection(self):
        self.connect_clients(1)
        previous = self.clients[0]
        new = self.connect_tls_client(self.server.getsockname())
        send_hello(new, *self.identities[0])
        receive_welcome(new)
        self.clients.append(new)
        end = time.monotonic() + 2
        while time.monotonic() < end:
            if previous.fileno() >= 0:
                try:
                    if previous.recv(1) == b"":
                        break
                except (OSError, socket.timeout):
                    pass
            time.sleep(0.01)
        self.assertEqual(self.broadcast.client_count, 1)
        self.broadcast.publish(pcm(9), 9)
        self.assertEqual(receive_frame(new).pcm, pcm(9))

    def test_max_clients(self):
        self.broadcast.max_clients = 2
        self.connect_clients(2)
        extra = self.connect_tls_client(self.server.getsockname())
        self.clients.append(extra)
        send_hello(extra, str(uuid.UUID(int=200)), "C2627-Γ3")
        receive_welcome(extra)
        self.assertEqual(extra.recv(1), b"")
        self.assertEqual(self.broadcast.client_count, 2)


if __name__ == "__main__":
    unittest.main()
