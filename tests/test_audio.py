"""Έλεγχοι πρωτοκόλλου χωρίς εξωτερικές συσκευές ήχου."""

import socket
import struct
import unittest

from shared.audio import (
    BLOCK_BYTES,
    BLOCK_SAMPLES,
    HEADER_BYTES,
    PROTOCOL_MAGIC,
    PROTOCOL_VERSION,
    SAMPLE_RATE,
    Frame,
    read_exact,
    receive_frame,
    send_frame,
)


class AudioProtocolTests(unittest.TestCase):
    def test_pcm_block_settings(self):
        self.assertEqual(SAMPLE_RATE, 16000)
        self.assertEqual(BLOCK_SAMPLES, 320)
        self.assertEqual(BLOCK_BYTES, 640)

    def test_loopback_frame(self):
        left, right = socket.socketpair()
        with left, right:
            pcm = bytes(range(256)) * 2 + bytes(range(128))
            send_frame(left, pcm, 7, 123456789)
            self.assertEqual(receive_frame(right), Frame(7, 123456789, pcm))

    def test_consecutive_frames(self):
        left, right = socket.socketpair()
        with left, right:
            for n in range(3):
                send_frame(left, bytes([n]) * BLOCK_BYTES, n, 1000 * n)
            for n in range(3):
                frame = receive_frame(right)
                self.assertEqual(frame.seq, n)
                self.assertEqual(frame.timestamp_us, 1000 * n)
                self.assertEqual(frame.pcm, bytes([n]) * BLOCK_BYTES)

    def test_invalid_send_size(self):
        left, right = socket.socketpair()
        with left, right:
            with self.assertRaises(ValueError):
                send_frame(left, b"short")

    def test_reject_invalid_frame_length(self):
        left, right = socket.socketpair()
        with left, right:
            left.sendall(struct.pack("!2sBxIIQ", PROTOCOL_MAGIC, PROTOCOL_VERSION, 999999, 0, 0))
            with self.assertRaises(ValueError):
                receive_frame(right)

    def test_reject_old_protocol(self):
        # Ο παλιός Director στέλνει μόνο μήκος 4 bytes + PCM, χωρίς magic.
        left, right = socket.socketpair()
        with left, right:
            left.sendall((struct.pack("!I", BLOCK_BYTES) + bytes(BLOCK_BYTES))[:HEADER_BYTES])
            with self.assertRaisesRegex(ValueError, "πρωτόκολλο"):
                receive_frame(right)

    def test_reject_unknown_version(self):
        left, right = socket.socketpair()
        with left, right:
            left.sendall(struct.pack("!2sBxIIQ", PROTOCOL_MAGIC, 99, BLOCK_BYTES, 0, 0))
            with self.assertRaisesRegex(ValueError, "έκδοση"):
                receive_frame(right)

    def test_eof(self):
        left, right = socket.socketpair()
        with left, right:
            left.close()
            with self.assertRaises(EOFError):
                receive_frame(right)

    def test_partial_read(self):
        left, right = socket.socketpair()
        with left, right:
            left.sendall(b"abc")
            left.sendall(b"def")
            self.assertEqual(read_exact(right, 6), b"abcdef")


if __name__ == "__main__":
    unittest.main()
