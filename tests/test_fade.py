"""Έλεγχοι fade-in/fade-out του ήχου ανακοίνωσης στον Client (χωρίς κάρτα ήχου)."""

import unittest
from array import array

from client.main import make_jitter_buffer
from shared.audio import BLOCK_BYTES, BLOCK_SAMPLES, apply_gain_ramp
from shared.jitter import JitterBuffer

LOUD = 10_000


def loud_block() -> bytes:
    return array("h", [LOUD] * BLOCK_SAMPLES).tobytes()


def samples(block: bytes) -> list[int]:
    values = array("h")
    values.frombytes(block)
    return list(values)


def play(jitter: JitterBuffer, count: int) -> list[int]:
    out: list[int] = []
    for _ in range(count):
        out.extend(samples(jitter.pop()))
    return out


def max_step(values: list[int]) -> int:
    return max(abs(a - b) for a, b in zip(values, values[1:]))


class GainRampTests(unittest.TestCase):
    def test_unity_gain_returns_same_block(self):
        block = loud_block()
        self.assertIs(apply_gain_ramp(block, 1.0, 1.0), block)

    def test_linear_ramp(self):
        out = samples(apply_gain_ramp(loud_block(), 0.0, 1.0))
        self.assertEqual(out[0], 0)
        self.assertEqual(out[-1], LOUD)
        self.assertEqual(out, sorted(out))

    def test_negative_samples(self):
        block = array("h", [-LOUD] * BLOCK_SAMPLES).tobytes()
        out = samples(apply_gain_ramp(block, 1.0, 0.0))
        self.assertEqual((out[0], out[-1]), (-LOUD, 0))


class AnnouncementFadeTests(unittest.TestCase):
    def setUp(self):
        self.jitter = make_jitter_buffer(buffer_ms=1000, fade_ms=150, resume_fade_ms=20)
        self.jitter.begin_announcement()

    def test_fade_in_at_start_and_fade_out_at_end(self):
        for _ in range(30):
            self.jitter.push(loud_block())
        self.jitter.finish_announcement()
        out = [s for s in play(self.jitter, 32)]
        voiced = out[: 30 * BLOCK_SAMPLES]
        self.assertEqual(voiced[0], 0)
        self.assertEqual(voiced[-1], 0)
        # 150 ms = 2400 δείγματα· στη μέση ο ήχος είναι πλήρης.
        self.assertEqual(voiced[2400], LOUD)
        self.assertEqual(voiced[-2401], LOUD)
        # Κανένα απότομο βήμα: σε 150 ms το βήμα ανά δείγμα είναι ~LOUD/2400.
        self.assertLessEqual(max_step(out), LOUD // 2400 + 2)

    def test_short_fade_after_network_gap(self):
        for _ in range(12):
            self.jitter.push(loud_block())
        played = play(self.jitter, 12)  # το prefill είναι ήδη γεμάτο: παίζει αμέσως
        self.assertEqual(played[0], 0)
        self.assertEqual(played[-1], LOUD)
        self.jitter.pop()  # η ουρά άδειασε: κενό δικτύου
        for _ in range(10):
            self.jitter.push(loud_block())
        resumed = play(self.jitter, 1)
        self.assertEqual(resumed[0], 0)
        # 20 ms = 320 δείγματα = ένα block: πλήρης ένταση στο τέλος του block.
        self.assertEqual(resumed[-1], LOUD)
        self.assertEqual(play(self.jitter, 1)[0], LOUD)

    def test_stop_before_prefill_still_fades(self):
        for _ in range(3):
            self.jitter.push(loud_block())
        self.jitter.finish_announcement()
        out = play(self.jitter, 3)
        self.assertEqual(out[0], 0)
        self.assertEqual(out[-1], 0)
        self.assertLess(max(out), LOUD)

    def test_new_announcement_fades_in_again(self):
        for _ in range(10):
            self.jitter.push(loud_block())
        self.jitter.finish_announcement()
        play(self.jitter, 11)
        self.jitter.begin_announcement()
        for _ in range(10):
            self.jitter.push(loud_block())
        out = play(self.jitter, 7)
        self.assertEqual(out[0], 0)
        self.assertLess(out[BLOCK_SAMPLES], LOUD)


class NoFadeTests(unittest.TestCase):
    def test_zero_fade_keeps_blocks_unchanged(self):
        jitter = make_jitter_buffer(buffer_ms=1000, fade_ms=0, resume_fade_ms=0)
        jitter.begin_announcement()
        block = loud_block()
        for _ in range(8):
            jitter.push(block)
        jitter.finish_announcement()
        self.assertTrue(all(jitter.pop() is block for _ in range(8)))

    def test_default_buffer_has_no_fade(self):
        jitter = JitterBuffer(4, BLOCK_BYTES, prefill_blocks=1)
        block = loud_block()
        jitter.push(block)
        self.assertIs(jitter.pop(), block)


if __name__ == "__main__":
    unittest.main()
