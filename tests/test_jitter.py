"""Έλεγχοι ουράς αναπαραγωγής, μετρήσεων και προσομοίωση δικτύου με διακοπές.

Δεν χρησιμοποιούνται συσκευές ήχου: η «κάρτα ήχου» είναι ένα νήμα που παίρνει
ένα block κάθε 20 ms, όπως θα έκανε το callback του sounddevice.
"""

import socket
import threading
import time
import unittest

from client.main import format_report, pump_frames
from shared.audio import BLOCK_BYTES, BLOCK_MS, send_frame
from shared.jitter import DelayTracker, JitterBuffer


def block(n: int) -> bytes:
    return bytes([n % 256]) * BLOCK_BYTES


class JitterBufferTests(unittest.TestCase):
    def test_drops_oldest_when_full(self):
        jitter = JitterBuffer(4, BLOCK_BYTES, prefill_blocks=1)
        for n in range(10):
            jitter.push(block(n))
        self.assertEqual(len(jitter), 4)
        self.assertEqual(jitter.dropped, 6)
        self.assertEqual([jitter.pop() for _ in range(4)], [block(n) for n in range(6, 10)])

    def test_silence_until_prefill(self):
        jitter = JitterBuffer(4, BLOCK_BYTES, prefill_blocks=2)
        jitter.push(block(1))
        self.assertEqual(jitter.pop(), bytes(BLOCK_BYTES))
        jitter.push(block(2))
        self.assertEqual(jitter.pop(), block(1))
        self.assertEqual(jitter.pop(), block(2))

    def test_underrun_counts_and_refills(self):
        jitter = JitterBuffer(4, BLOCK_BYTES, prefill_blocks=2)
        jitter.push(block(1))
        jitter.push(block(2))
        jitter.pop()
        jitter.pop()
        self.assertEqual(jitter.pop(), bytes(BLOCK_BYTES))
        self.assertEqual(jitter.underruns, 1)
        jitter.push(block(3))
        self.assertEqual(jitter.pop(), bytes(BLOCK_BYTES))  # περιμένει πάλι 2 blocks
        jitter.push(block(4))
        self.assertEqual(jitter.pop(), block(3))

    def test_default_prefill_is_half(self):
        self.assertEqual(JitterBuffer(4, BLOCK_BYTES).prefill_blocks, 2)
        self.assertEqual(JitterBuffer(1, BLOCK_BYTES).prefill_blocks, 1)

    def test_level_stats(self):
        jitter = JitterBuffer(4, BLOCK_BYTES, prefill_blocks=1)
        for n in range(3):
            jitter.push(block(n))
        jitter.pop()
        jitter.pop()
        self.assertEqual(jitter.take_level_stats(), (2, 2.5, 3))
        self.assertIsNone(jitter.take_level_stats())

    def test_idle_is_silent_without_underruns(self):
        jitter = JitterBuffer(12, BLOCK_BYTES, prefill_blocks=6)
        jitter.standby()
        for _ in range(80):
            self.assertEqual(jitter.pop(), bytes(BLOCK_BYTES))
        self.assertEqual(jitter.mode, "αναμονή")
        self.assertEqual(jitter.underruns, 0)
        self.assertIsNone(jitter.take_level_stats())
        self.assertIn("κατάσταση: αναμονή", format_report(jitter, DelayTracker(), 0,
                                                        mode="αναμονή"))

    def test_stop_drains_even_short_announcement_without_underrun(self):
        jitter = JitterBuffer(12, BLOCK_BYTES, prefill_blocks=6)
        jitter.standby()
        jitter.begin_announcement()
        jitter.push(block(17))
        self.assertEqual(jitter.pop(), bytes(BLOCK_BYTES))  # ακόμη prefill
        jitter.finish_announcement()
        self.assertEqual(jitter.pop(), block(17))
        self.assertEqual(jitter.pop(), bytes(BLOCK_BYTES))
        self.assertEqual(jitter.mode, "αναμονή")
        self.assertEqual(jitter.underruns, 0)

    def test_invalid_size(self):
        with self.assertRaises(ValueError):
            JitterBuffer(0, BLOCK_BYTES)


class DelayTrackerTests(unittest.TestCase):
    def test_extra_delay_relative_to_best(self):
        delay = DelayTracker()
        # Τα ρολόγια διαφέρουν κατά 5 s· μετράει μόνο η διαφορά από το καλύτερο.
        delay.record(0, 0, 5_000_000 + 10_000)
        delay.record(1, 20_000, 5_020_000 + 10_000)
        delay.record(2, 40_000, 5_040_000 + 310_000)
        self.assertEqual(delay.take_stats(), (100.0, 300.0))
        self.assertIsNone(delay.take_stats())

    def test_sequence_gaps(self):
        delay = DelayTracker()
        for seq in (0, 1, 3, 4):
            delay.record(seq, 0, 0)
        self.assertEqual(delay.gaps, 1)

    def test_report_text(self):
        jitter = JitterBuffer(4, BLOCK_BYTES, prefill_blocks=1)
        jitter.push(block(0))
        jitter.pop()
        delay = DelayTracker()
        delay.record(0, 0, 1000)
        line = format_report(jitter, delay, 1)
        self.assertIn("ουρά min/μέση/max 20/20/20 ms", line)
        self.assertIn("επιπλέον καθυστέρηση δικτύου μέση/max 0/0 ms", line)


class JitteryNetworkSimulation(unittest.TestCase):
    """Director στέλνει κάθε 20 ms, το δίκτυο «κολλάει» 1 s και μετά παραδίδει όλα μαζί."""

    STALL_SECONDS = 1.0

    def run_simulation(self, max_blocks: int) -> dict:
        sender, receiver = socket.socketpair()
        jitter = JitterBuffer(max_blocks, BLOCK_BYTES)
        delay = DelayTracker()
        stop = threading.Event()
        levels_after_stall: list[int] = []
        stall_over = threading.Event()

        def director():
            # 0.5 s κανονικά, μετά 1 s που τα blocks μένουν «στο δίκτυο», μετά 0.5 s κανονικά.
            start = time.monotonic()
            held: list[tuple[int, int]] = []
            seq = 0
            while time.monotonic() - start < 2.0:
                due = start + seq * BLOCK_MS / 1000
                time.sleep(max(0.0, due - time.monotonic()))
                captured_us = time.monotonic_ns() // 1000
                elapsed = time.monotonic() - start
                if 0.5 <= elapsed < 0.5 + self.STALL_SECONDS:
                    held.append((seq, captured_us))
                else:
                    for old_seq, old_us in held:
                        send_frame(sender, block(old_seq), old_seq, old_us)
                    if held:
                        stall_over.set()
                    held.clear()
                    send_frame(sender, block(seq), seq, captured_us)
                seq += 1
            sender.close()

        def sound_card():
            next_tick = time.monotonic()
            while not stop.is_set():
                jitter.pop()
                if stall_over.is_set():
                    levels_after_stall.append(len(jitter))
                next_tick += BLOCK_MS / 1000
                time.sleep(max(0.0, next_tick - time.monotonic()))

        threads = [threading.Thread(target=director), threading.Thread(target=sound_card)]
        for thread in threads:
            thread.start()
        try:
            pump_frames(receiver, jitter, delay, report=None)
        except EOFError:
            pass
        finally:
            stop.set()
            for thread in threads:
                thread.join()
            receiver.close()
        _, max_extra_ms = delay.take_stats()
        return {
            "dropped": jitter.dropped,
            "max_after_stall_ms": max(levels_after_stall) * BLOCK_MS,
            "end_level_ms": levels_after_stall[-1] * BLOCK_MS,
            "max_extra_ms": max_extra_ms,
        }

    def test_queue_returns_below_limit_after_stall(self):
        result = self.run_simulation(max_blocks=4)  # 80 ms
        # Η προσομοίωση πράγματι δημιούργησε ~1 s καθυστέρηση δικτύου.
        self.assertGreater(result["max_extra_ms"], 800)
        # Τα blocks του κολλήματος πετάχτηκαν αντί να συσσωρευτούν.
        self.assertGreaterEqual(result["dropped"], 40)
        # Η ουρά δεν ξεπέρασε ποτέ το όριο των 80 ms.
        self.assertLessEqual(result["max_after_stall_ms"], 80)
        self.assertLessEqual(result["end_level_ms"], 80)

    def test_unbounded_queue_keeps_the_delay(self):
        # Συμπεριφορά παρόμοια με την προηγούμενη έκδοση: χωρίς όριο, η καθυστέρηση του κολλήματος μένει.
        result = self.run_simulation(max_blocks=10_000)
        self.assertEqual(result["dropped"], 0)
        self.assertGreater(result["end_level_ms"], 800)


if __name__ == "__main__":
    unittest.main()
