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


def pcm(n):
    return bytes([n % 256]) * BLOCK_BYTES


class MultiClientTests(unittest.TestCase):
    def setUp(self):
        self.broadcast = AudioBroadcaster(queue_blocks=5)
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(32)
        self.stop = threading.Event()
        self.listener = threading.Thread(target=accept_clients,
                                         args=(self.server, self.broadcast, self.stop),
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
            s = socket.create_connection(self.server.getsockname(), timeout=2)
            s.settimeout(3)
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

    def test_2_5_10_15_20_clients_receive_identical_frames(self):
        for count in (2, 5, 10, 15, 20):
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
        new = socket.create_connection(self.server.getsockname(), timeout=2)
        new.settimeout(2)
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
        extra = socket.create_connection(self.server.getsockname(), timeout=2)
        extra.settimeout(2)
        self.clients.append(extra)
        send_hello(extra, str(uuid.UUID(int=200)), "C2627-Γ3")
        receive_welcome(extra)
        self.assertEqual(extra.recv(1), b"")
        self.assertEqual(self.broadcast.client_count, 2)


if __name__ == "__main__":
    unittest.main()
