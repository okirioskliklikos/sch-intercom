"""Έλεγχοι σταθερής ταυτότητας και οριοθετημένης δήλωσης TCP."""

import json
from pathlib import Path
import socket
import struct
import tempfile
import unittest
import uuid

from shared.identity import (
    HELLO_MAGIC, MAX_HELLO_BYTES, load_identity, receive_hello, send_hello,
    validate_identity,
)


class IdentityTests(unittest.TestCase):
    def test_persistent_identity_and_room_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "client.json"
            first = load_identity(path, room_override="C2627-Γ1")
            self.assertTrue(path.exists())
            self.assertEqual(first[1], "C2627-Γ1")
            self.assertEqual(load_identity(path), first)
            second = load_identity(path, room_override="C2627-PcLab")
            self.assertEqual(second[0], first[0])
            self.assertEqual(second[1], "C2627-PcLab")
            self.assertEqual(load_identity(path), second)

    def test_handshake_roundtrip_unicode(self):
        left, right = socket.socketpair()
        try:
            client_id = str(uuid.uuid4())
            send_hello(left, client_id, "C2627-Γ1")
            self.assertEqual(receive_hello(right), (client_id, "C2627-Γ1"))
        finally:
            left.close()
            right.close()

    def test_reject_invalid_ids_rooms(self):
        for room in ("", "a" * 65, "invalid\nroom", " bad"):
            with self.subTest(room=room):
                with self.assertRaises(ValueError):
                    validate_identity(str(uuid.uuid4()), room)
        with self.assertRaises(ValueError):
            validate_identity("not-a-uuid", "C2627-Γ1")

    def test_reject_oversized_handshake_before_payload(self):
        left, right = socket.socketpair()
        try:
            left.sendall(HELLO_MAGIC + struct.pack("!H", MAX_HELLO_BYTES + 1))
            with self.assertRaises(ValueError):
                receive_hello(right)
        finally:
            left.close()
            right.close()

    def test_reject_malformed_json(self):
        left, right = socket.socketpair()
        try:
            payload = b"{not-json}"
            left.sendall(HELLO_MAGIC + struct.pack("!H", len(payload)) + payload)
            with self.assertRaises(ValueError):
                receive_hello(right)
        finally:
            left.close()
            right.close()


if __name__ == "__main__":
    unittest.main()
