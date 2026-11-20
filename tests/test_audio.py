"""Έλεγχοι πρωτοκόλλου χωρίς εξωτερικές συσκευές ήχου."""

import socket
import struct
import unittest

from shared.audio import BLOCK_BYTES, BLOCK_SAMPLES, SAMPLE_RATE, read_exact, receive_frame, send_frame


class AudioProtocolTests(unittest.TestCase):
    def test_pcm_block_settings(self):
        self.assertEqual(SAMPLE_RATE, 16000)
        self.assertEqual(BLOCK_SAMPLES, 320)
        self.assertEqual(BLOCK_BYTES, 640)

    def test_loopback_frame(self):
        left, right = socket.socketpair()
        with left, right:
            pcm = bytes(range(256)) * 2 + bytes(range(128))
            send_frame(left, pcm)
            self.assertEqual(receive_frame(right), pcm)

    def test_consecutive_frames(self):
        left, right = socket.socketpair()
        with left, right:
            for n in range(3):
                send_frame(left, bytes([n]) * BLOCK_BYTES)
            for n in range(3):
                self.assertEqual(receive_frame(right), bytes([n]) * BLOCK_BYTES)

    def test_invalid_send_size(self):
        left, right = socket.socketpair()
        with left, right:
            with self.assertRaises(ValueError):
                send_frame(left, b"short")

    def test_reject_invalid_frame_length(self):
        left, right = socket.socketpair()
        with left, right:
            left.sendall(struct.pack("!I", 999999))
            with self.assertRaises(ValueError):
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
