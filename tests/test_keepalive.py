"""End-to-end control messages and state transitions over real local sockets."""

import socket
import threading
import time
import unittest
import uuid

from client.main import (ANNOUNCEMENT_PREFILL_MS, DEFAULT_BUFFER_MS, DEFAULT_LATENCY,
                         RECEIVE_TIMEOUT_SECONDS, pump_frames, run_with_reconnect)
from shared.audio import (BLOCK_BYTES, BLOCK_MS, Frame, HEADER_BYTES, MESSAGE_KEEPALIVE,
                          MESSAGE_STREAM_STOP, encode_control, encode_frame, receive_message)
from shared.control import KEEPALIVE_INTERVAL_SECONDS
from shared.identity import (CONNECTION_PROTOCOL, ProtocolMismatch, receive_hello,
                             receive_welcome, send_hello, WELCOME, WELCOME_MAGIC, WELCOME_OK)
from shared.jitter import DelayTracker, JitterBuffer
from director.broadcast import AudioBroadcaster
from shared.transport_security import accept_tls
from tests.tls_support import LocalTLSFixture


def wait_until(check, timeout=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.005)
    return bool(check())


def pcm(number):
    return bytes([number]) * BLOCK_BYTES


class ControlProtocolTests(unittest.TestCase):
    def test_control_wire_size_and_validation(self):
        self.assertEqual(KEEPALIVE_INTERVAL_SECONDS, 0.5)
        self.assertEqual(len(encode_control(MESSAGE_KEEPALIVE)), HEADER_BYTES)
        self.assertEqual(len(encode_control(MESSAGE_STREAM_STOP)), HEADER_BYTES)
        self.assertEqual(len(encode_frame(pcm(1), 0, 0)), HEADER_BYTES + BLOCK_BYTES)
        with self.assertRaises(ValueError):
            encode_control(99)
        sender, receiver = socket.socketpair()
        try:
            sender.sendall(encode_control(MESSAGE_KEEPALIVE))
            sender.sendall(encode_control(MESSAGE_STREAM_STOP))
            self.assertEqual(receive_message(receiver).kind, MESSAGE_KEEPALIVE)
            self.assertEqual(receive_message(receiver).kind, MESSAGE_STREAM_STOP)
        finally:
            sender.close()
            receiver.close()

    def test_director_idle_bandwidth_and_stop_order(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client = socket.create_connection(server.getsockname(), timeout=2)
        incoming, _ = server.accept()
        server.close()
        client.settimeout(2)
        director = AudioBroadcaster(heartbeat_timeout=4)
        self.addCleanup(director.close)
        self.addCleanup(client.close)
        identity = str(uuid.uuid4())
        self.assertTrue(director.add_client(incoming, identity, "C2627-Γ1"))
        director.publish(pcm(1), 10)
        self.assertEqual(receive_message(client).pcm, pcm(1))
        director.set_targets(rooms=set(), client_ids=set())
        stop = receive_message(client)
        self.assertEqual(stop.kind, MESSAGE_STREAM_STOP)
        start = time.monotonic()
        for _ in range(3):
            self.assertEqual(receive_message(client).kind, MESSAGE_KEEPALIVE)
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.5)
        # Σε αναμονή τα payload είναι 3×20 bytes, όχι 3×660.
        self.assertEqual(3 * len(encode_control(MESSAGE_KEEPALIVE)), 60)
        director.set_targets()
        director.publish(pcm(2), 20)
        # Πιθανό keepalive ήδη στο TCP πριν από τη νέα επιλογή.
        message = receive_message(client)
        while not isinstance(message, Frame):
            self.assertEqual(message.kind, MESSAGE_KEEPALIVE)
            message = receive_message(client)
        self.assertEqual(message.pcm, pcm(2))

    def test_selected_client_gets_keepalive_when_microphone_stalls(self):
        """Επιλεγμένος Client χωρίς PCM (κόλλημα μικροφώνου) δεν μένει χωρίς μηνύματα."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client = socket.create_connection(server.getsockname(), timeout=2)
        incoming, _ = server.accept()
        server.close()
        client.settimeout(2)
        director = AudioBroadcaster(heartbeat_timeout=4)
        self.addCleanup(director.close)
        self.addCleanup(client.close)
        self.assertTrue(director.add_client(incoming, str(uuid.uuid4()), "C2627-Γ1"))
        director.publish(pcm(1), 10)
        self.assertEqual(receive_message(client).pcm, pcm(1))
        # Ο Client παραμένει επιλεγμένος· δεν γίνεται άλλο publish.
        start = time.monotonic()
        for _ in range(2):
            message = receive_message(client, deadline=time.monotonic() + 1.0)
            self.assertNotIsInstance(message, Frame)
            self.assertEqual(message.kind, MESSAGE_KEEPALIVE)
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertLess(elapsed, 1.6)
        # Όταν επανέλθει το μικρόφωνο, ο ήχος συνεχίζει χωρίς STOP ή επανασύνδεση.
        director.publish(pcm(2), 20)
        message = receive_message(client)
        while not isinstance(message, Frame):
            self.assertEqual(message.kind, MESSAGE_KEEPALIVE)
            message = receive_message(client)
        self.assertEqual(message.pcm, pcm(2))

    def test_no_keepalive_while_pcm_flows(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client = socket.create_connection(server.getsockname(), timeout=2)
        incoming, _ = server.accept()
        server.close()
        client.settimeout(2)
        director = AudioBroadcaster(heartbeat_timeout=4)
        self.addCleanup(director.close)
        self.addCleanup(client.close)
        self.assertTrue(director.add_client(incoming, str(uuid.uuid4()), "C2627-Γ1"))
        for n in range(40):  # 0,8 s ροής, 20 ms ανά block
            director.publish(pcm(n % 250), n)
            time.sleep(0.02)
        kinds = []
        for _ in range(40):
            message = receive_message(client)
            kinds.append("pcm" if isinstance(message, Frame) else message.kind)
        self.assertEqual(kinds, ["pcm"] * 40)

    def test_partial_invalid_message_does_not_extend_timeout(self):
        sender, receiver = socket.socketpair()
        try:
            sender.sendall(encode_control(MESSAGE_KEEPALIVE)[:2])
            start = time.monotonic()
            with self.assertRaises(socket.timeout):
                receive_message(receiver, deadline=start + 0.18)
            self.assertLess(time.monotonic() - start, 0.7)
        finally:
            sender.close()
            receiver.close()

    def test_pre_tls_versions_rejected_both_directions(self):
        self.assertEqual(CONNECTION_PROTOCOL, 7)
        for old in (2, 3, 4, 5, 6):
            with self.subTest(old_client=old):
                sender, receiver = socket.socketpair()
                try:
                    send_hello(sender, str(uuid.uuid4()), "C2627-Γ1", protocol=old)
                    with self.assertRaisesRegex(ProtocolMismatch,
                                                f"Client v{old}, Director v7"):
                        receive_hello(receiver)
                finally:
                    sender.close()
                    receiver.close()
            with self.subTest(old_director=old):
                sender, receiver = socket.socketpair()
                try:
                    sender.sendall(WELCOME.pack(WELCOME_MAGIC, old, WELCOME_OK))
                    with self.assertRaisesRegex(ProtocolMismatch,
                                                f"Client v7, Director v{old}"):
                        receive_welcome(receiver)
                finally:
                    sender.close()
                    receiver.close()

    def test_standby_announcement_stop_standby_without_reconnect(self):
        sender, receiver = socket.socketpair()
        receiver.settimeout(3)
        jitter = JitterBuffer(12, BLOCK_BYTES, prefill_blocks=6)
        jitter.standby()
        errors = []
        def listener():
            try:
                pump_frames(receiver, jitter, DelayTracker(), report=None)
            except EOFError:
                pass
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=listener)
        thread.start()
        try:
            sender.sendall(encode_control(MESSAGE_KEEPALIVE))
            self.assertEqual(jitter.mode, "αναμονή")
            for n in range(8):
                sender.sendall(encode_frame(pcm(n), n, n * BLOCK_MS * 1000))
            self.assertTrue(wait_until(lambda: len(jitter) == 8))
            self.assertEqual(jitter.mode, "ανακοίνωση")
            sender.sendall(encode_control(MESSAGE_STREAM_STOP))
            # Ακόμη κι αν έρθουν πολλά keepalive, δεν μετρούν ως ήχος.
            for _ in range(3):
                sender.sendall(encode_control(MESSAGE_KEEPALIVE))
            self.assertTrue(wait_until(lambda: jitter._draining), "Δεν παραλήφθηκε STREAM_STOP")
            self.assertEqual([jitter.pop() for _ in range(8)], [pcm(n) for n in range(8)])
            self.assertEqual(jitter.pop(), bytes(BLOCK_BYTES))
            self.assertEqual(jitter.mode, "αναμονή")
            self.assertEqual(jitter.underruns, 0)
            self.assertEqual(jitter.dropped, 0)
            sender.sendall(encode_frame(pcm(99), 99, 0))
            self.assertTrue(wait_until(lambda: len(jitter) == 1))
            # Δεν θα παίξει πριν γεμίσει η προφόρτωση των 120 ms.
            self.assertEqual(jitter.pop(), bytes(BLOCK_BYTES))
            for n in range(5):
                sender.sendall(encode_frame(pcm(n), 100+n, 0))
            self.assertTrue(wait_until(lambda: len(jitter) == 6))
            self.assertEqual(jitter.pop(), pcm(99))
        finally:
            sender.close()
            thread.join(timeout=1)
            receiver.close()
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors, repr(errors))

    def test_keepalive_more_than_ten_seconds_no_timeout(self):
        sender, receiver = socket.socketpair()
        receiver.settimeout(RECEIVE_TIMEOUT_SECONDS)
        jitter = JitterBuffer(12, BLOCK_BYTES)
        jitter.standby()
        errors = []
        def listener():
            try:
                pump_frames(receiver, jitter, DelayTracker(), report=None)
            except EOFError:
                return
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=listener)
        thread.start()
        begin = time.monotonic()
        try:
            for _ in range(22):  # 11 s σε πραγματικό monotonic χρόνο
                sender.sendall(encode_control(MESSAGE_KEEPALIVE))
                time.sleep(0.5)
        finally:
            sender.close()
            thread.join(timeout=2)
            receiver.close()
        self.assertGreaterEqual(time.monotonic() - begin, 10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(jitter.mode, "αναμονή")
        self.assertEqual(jitter.underruns, 0)

    def test_keepalive_stops_timeout_in_around_three_seconds(self):
        sender, receiver = socket.socketpair()
        receiver.settimeout(RECEIVE_TIMEOUT_SECONDS)
        start = time.monotonic()
        try:
            sender.sendall(encode_control(MESSAGE_KEEPALIVE))
            with self.assertRaises(socket.timeout):
                pump_frames(receiver, JitterBuffer(12, BLOCK_BYTES), DelayTracker(),
                            report=None)
            elapsed = time.monotonic() - start
            self.assertGreaterEqual(elapsed, 2.8)
            self.assertLess(elapsed, 4.0)
        finally:
            sender.close()
            receiver.close()

    def test_playback_defaults_remain(self):
        self.assertEqual(DEFAULT_BUFFER_MS, 160)       # στόχος· κράτημα έως 1,5 s (4.4)
        self.assertEqual(DEFAULT_LATENCY, 0.04)
        self.assertEqual(ANNOUNCEMENT_PREFILL_MS, 120)


class ReconnectOverTLSTests(LocalTLSFixture, unittest.TestCase):
    def test_missing_keepalive_causes_reconnect_at_three_seconds(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(2)
        server.settimeout(8)
        connections = []
        seen_second = threading.Event()
        closed_first = threading.Event()
        def director():
            try:
                first, _ = server.accept()
                first = accept_tls(first, self.tls_server_context)
                connections.append(first)
                first.sendall(encode_control(MESSAGE_KEEPALIVE))
                # Η σύνδεση παραμένει ανοιχτή, αλλά σιωπούν όλα τα μηνύματα.
                second, _ = server.accept()
                second = accept_tls(second, self.tls_server_context)
                seen_second.set()
                connections.append(second)
                first.settimeout(1)
                if first.recv(1) == b"":
                    closed_first.set()
                second.sendall(encode_frame(pcm(31), 31, 0))
                second.close()
            except OSError:
                pass
        background = threading.Thread(target=director, daemon=True)
        background.start()
        sessions = []
        start = time.monotonic()
        def session(connection):
            jitter = JitterBuffer(12, BLOCK_BYTES)
            jitter.standby()
            try:
                pump_frames(connection, jitter, DelayTracker(), report=None)
            finally:
                sessions.append(len(jitter))
        try:
            run_with_reconnect(*server.getsockname(), session, reconnect_delay=0,
                               tls_context=self.tls_client_context, max_sessions=2, log=lambda _: None, sleep=lambda _: None)
            elapsed = time.monotonic() - start
            self.assertTrue(seen_second.is_set())
            self.assertTrue(closed_first.is_set())
            self.assertEqual(sessions, [0, 1])
            self.assertGreaterEqual(elapsed, 2.8)
            self.assertLess(elapsed, 5.0)
        finally:
            server.close()
            for connection in connections:
                connection.close()
            background.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
