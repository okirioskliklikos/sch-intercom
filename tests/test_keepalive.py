"""End-to-end control messages and state transitions over real local sockets."""

import socket
import threading
import time
import unittest
import uuid

from client.main import (ANNOUNCEMENT_PREFILL_MS, DEFAULT_BUFFER_MS, DEFAULT_LATENCY,
                         RECEIVE_TIMEOUT_SECONDS, pump_frames)
from shared.audio import (BLOCK_BYTES, BLOCK_MS, Frame, HEADER_BYTES, MESSAGE_KEEPALIVE,
                          MESSAGE_STREAM_STOP, encode_control, encode_frame, receive_message)
from shared.control import KEEPALIVE_INTERVAL_SECONDS
from shared.identity import (CONNECTION_PROTOCOL, ProtocolMismatch, receive_hello,
                             receive_welcome, send_hello, WELCOME, WELCOME_MAGIC, WELCOME_OK)
from shared.jitter import DelayTracker, JitterBuffer


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

    def test_version_2_and_3_rejected_both_directions(self):
        self.assertEqual(CONNECTION_PROTOCOL, 3)
        sender, receiver = socket.socketpair()
        try:
            send_hello(sender, str(uuid.uuid4()), "C2627-Γ1", protocol=2)
            with self.assertRaisesRegex(ProtocolMismatch, "Client v2, Director v3"):
                receive_hello(receiver)
        finally:
            sender.close()
            receiver.close()
        sender, receiver = socket.socketpair()
        try:
            sender.sendall(WELCOME.pack(WELCOME_MAGIC, 2, WELCOME_OK))
            with self.assertRaisesRegex(ProtocolMismatch, "Client v3, Director v2"):
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
            self.assertTrue(wait_until(lambda: jitter.mode == "ανακοίνωση"))
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
        self.assertEqual(DEFAULT_BUFFER_MS, 250)
        self.assertEqual(DEFAULT_LATENCY, 0.04)
        self.assertEqual(ANNOUNCEMENT_PREFILL_MS, 120)


if __name__ == "__main__":
    unittest.main()
