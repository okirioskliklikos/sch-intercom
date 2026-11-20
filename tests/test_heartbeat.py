"""Heartbeat, timeout και offline κατάσταση σε τοπικά TCP sockets."""

import socket
import threading
import time
import unittest
import uuid

from director.broadcast import AudioBroadcaster
from shared.audio import BLOCK_BYTES, receive_frame
from shared.control import (
    HEARTBEAT, HEARTBEAT_INTERVAL_SECONDS, HEARTBEAT_TIMEOUT_SECONDS,
    heartbeat_receiver, heartbeat_sender,
)


def wait_until(condition, seconds=2.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return bool(condition())


class HeartbeatTests(unittest.TestCase):
    def test_default_intervals(self):
        self.assertEqual(HEARTBEAT_INTERVAL_SECONDS, 2.0)
        self.assertEqual(HEARTBEAT_TIMEOUT_SECONDS, 6.0)

    def test_sender_immediate_and_periodic(self):
        left, right = socket.socketpair()
        stop = threading.Event()
        try:
            right.settimeout(1)
            sender = threading.Thread(target=heartbeat_sender,
                                      args=(left, stop), kwargs={"interval": 0.04})
            sender.start()
            self.assertEqual(right.recv(4), HEARTBEAT)
            self.assertEqual(right.recv(4), HEARTBEAT)
            stop.set()
            sender.join(timeout=1)
            self.assertFalse(sender.is_alive())
        finally:
            stop.set()
            left.close()
            right.close()

    def test_fragmented_beats_survive_socket_timeouts(self):
        left, right = socket.socketpair()
        stop = threading.Event()
        right.settimeout(0.015)
        received = []
        errors = []
        def listen():
            try:
                heartbeat_receiver(right, stop, received.append, timeout=0.5)
            except (EOFError, OSError, TimeoutError, ValueError) as exc:
                errors.append(exc)
        thread = threading.Thread(target=listen)
        thread.start()
        try:
            left.sendall(HEARTBEAT[:2])
            time.sleep(0.06)
            left.sendall(HEARTBEAT[2:])
            self.assertTrue(wait_until(lambda: len(received) == 1, 1))
            self.assertFalse(errors)
        finally:
            stop.set()
            left.shutdown(socket.SHUT_WR)
            thread.join(timeout=1)
            left.close()
            right.close()

    def test_receiver_rejects_invalid_data(self):
        left, right = socket.socketpair()
        right.settimeout(0.02)
        try:
            left.sendall(b"BAD!")
            with self.assertRaisesRegex(ValueError, "heartbeat"):
                heartbeat_receiver(right, threading.Event(), lambda _: None, timeout=0.4)
        finally:
            left.close()
            right.close()

    def test_receiver_times_out_without_heartbeat(self):
        left, right = socket.socketpair()
        right.settimeout(0.025)
        try:
            with self.assertRaises(TimeoutError):
                heartbeat_receiver(right, threading.Event(), lambda _: None, timeout=0.14)
        finally:
            left.close()
            right.close()

    def _connected_pair(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client = socket.create_connection(server.getsockname(), timeout=2)
        incoming, _ = server.accept()
        server.close()
        client.settimeout(2)
        return incoming, client

    def test_broadcaster_heartbeats_and_audio_are_independent(self):
        broadcaster = AudioBroadcaster(heartbeat_timeout=3)
        incoming, client = self._connected_pair()
        identity = str(uuid.uuid4())
        self.addCleanup(broadcaster.close)
        self.addCleanup(client.close)
        self.assertTrue(broadcaster.add_client(incoming, identity, "C2627-Γ1"))
        client.sendall(HEARTBEAT)
        self.assertTrue(wait_until(lambda: broadcaster.status_snapshot()[0][3] < 0.08))
        broadcaster.publish(bytes([73]) * BLOCK_BYTES, 12345)
        self.assertEqual(receive_frame(client).pcm, bytes([73]) * BLOCK_BYTES)
        self.assertEqual(broadcaster.status_snapshot()[0][2], True)

    def test_broadcaster_marks_closed_client_offline(self):
        broadcaster = AudioBroadcaster(heartbeat_timeout=3)
        incoming, client = self._connected_pair()
        identity = str(uuid.uuid4())
        self.addCleanup(broadcaster.close)
        self.assertTrue(broadcaster.add_client(incoming, identity, "C2627-Γ2"))
        client.close()
        self.assertTrue(wait_until(lambda: broadcaster.client_count == 0))
        snapshot = broadcaster.status_snapshot()
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0][:3], (identity, "C2627-Γ2", False))

    def test_broadcaster_expires_unresponsive_client(self):
        broadcaster = AudioBroadcaster(heartbeat_timeout=0.15)
        incoming, client = self._connected_pair()
        identity = str(uuid.uuid4())
        self.addCleanup(broadcaster.close)
        self.addCleanup(client.close)
        self.assertTrue(broadcaster.add_client(incoming, identity, "C2627-PcLab"))
        self.assertTrue(wait_until(lambda: broadcaster.client_count == 0, 2.0))
        self.assertFalse(broadcaster.status_snapshot()[0][2])

    def test_reconnect_changes_offline_to_online(self):
        broadcaster = AudioBroadcaster(heartbeat_timeout=4)
        self.addCleanup(broadcaster.close)
        identity = str(uuid.uuid4())
        first, client1 = self._connected_pair()
        self.assertTrue(broadcaster.add_client(first, identity, "C2627-Γ1"))
        client1.close()
        self.assertTrue(wait_until(lambda: broadcaster.client_count == 0))
        self.assertFalse(broadcaster.status_snapshot()[0][2])
        second, client2 = self._connected_pair()
        self.addCleanup(client2.close)
        self.assertTrue(broadcaster.add_client(second, identity, "C2627-Γ1"))
        snapshot = broadcaster.status_snapshot()
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0][:3], (identity, "C2627-Γ1", True))

    def test_replacement_does_not_mark_new_session_offline(self):
        broadcaster = AudioBroadcaster(heartbeat_timeout=4)
        self.addCleanup(broadcaster.close)
        identity = str(uuid.uuid4())
        first, client1 = self._connected_pair()
        second, client2 = self._connected_pair()
        self.addCleanup(client1.close)
        self.addCleanup(client2.close)
        self.assertTrue(broadcaster.add_client(first, identity, "C2627-Γ1"))
        self.assertTrue(broadcaster.add_client(second, identity, "C2627-Γ1"))
        time.sleep(0.08)
        self.assertEqual(broadcaster.client_count, 1)
        self.assertTrue(broadcaster.status_snapshot()[0][2])


if __name__ == "__main__":
    unittest.main()
