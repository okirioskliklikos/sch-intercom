"""4.4: προσαρμοζόμενη ουρά (κράτημα ριπών έως 1,5 s, επιστροφή στον στόχο στις παύσεις)."""
import array
import unittest

from client.main import make_jitter_buffer
from shared.audio import BLOCK_BYTES
from shared.jitter import JitterBuffer, block_rms

VOICE = array.array("h", [3000, -3000] * (BLOCK_BYTES // 4)).tobytes()
QUIET = bytes(BLOCK_BYTES)


def started(target=3, maximum=20, keep=2):
    jitter = JitterBuffer(maximum, BLOCK_BYTES, prefill_blocks=1, target_blocks=target,
                          keep_quiet_blocks=keep)
    jitter.begin_announcement()
    return jitter


def play(jitter, count):
    """Παίζει count blocks· επιστρέφει (ετικέτες που έπαιξαν, ετικέτες που παραλείφθηκαν)."""
    played, skipped = [], []
    for _ in range(count):
        jitter.pop()
        if jitter.skipped_tag is not None:
            skipped.append(jitter.skipped_tag)
        if jitter.last_tag is not None:
            played.append(jitter.last_tag)
    return played, skipped


class AdaptiveJitterTests(unittest.TestCase):
    def test_rms(self):
        self.assertEqual(block_rms(QUIET), 0)
        self.assertAlmostEqual(block_rms(VOICE), 3000)

    def test_burst_is_kept_not_dropped(self):
        jitter = started(target=3, maximum=20)
        for tag in range(15):                     # ριπή μετά από κόλλημα
            jitter.push(VOICE, tag)
        played, skipped = play(jitter, 15)
        self.assertEqual(played, list(range(15)))  # καμία λέξη δεν χάθηκε
        self.assertEqual((jitter.dropped, skipped), (0, []))

    def test_voice_is_never_skipped(self):
        jitter = started(target=2, maximum=50, keep=0)
        for tag in range(30):
            jitter.push(VOICE, tag)
        played, skipped = play(jitter, 30)
        self.assertEqual(skipped, [])
        self.assertEqual(len(played), 30)

    def test_pause_keeps_start_and_shrinks_at_most_half(self):
        jitter = started(target=2, maximum=50, keep=3)
        blocks = [VOICE] * 4 + [QUIET] * 12 + [VOICE] * 4
        for tag, block in enumerate(blocks):
            jitter.push(block, tag)
        played, skipped = play(jitter, len(blocks))
        quiet_played = [tag for tag in played if 4 <= tag < 16]
        self.assertEqual(quiet_played[:3], [4, 5, 6])          # αρχή της παύσης ακούγεται
        self.assertGreaterEqual(len(quiet_played), len(skipped))  # το πολύ το μισό λείπει
        self.assertTrue(skipped)
        self.assertTrue(all(4 <= tag < 16 for tag in skipped))  # μόνο σιωπή
        self.assertEqual([tag for tag in played if tag >= 16][:1], [16])

    def test_no_skip_at_or_below_target(self):
        jitter = started(target=10, maximum=50, keep=0)
        for tag in range(8):
            jitter.push(QUIET, tag)
        _, skipped = play(jitter, 8)
        self.assertEqual(skipped, [])

    def test_over_maximum_drops_oldest_with_fade(self):
        jitter = JitterBuffer(5, BLOCK_BYTES, prefill_blocks=1, target_blocks=2,
                              resume_fade_samples=BLOCK_BYTES // 2)
        jitter.begin_announcement()
        jitter.push(VOICE, 0)
        jitter.pop()
        for tag in range(1, 9):
            jitter.push(VOICE, tag)
        self.assertEqual(jitter.dropped, 3)
        block = jitter.pop()
        self.assertEqual(jitter.last_tag, 4)
        self.assertEqual(array.array("h", block)[0], 0)     # σύντομο fade-in μετά το πέταμα

    def test_counters_reset_per_announcement(self):
        jitter = started(target=1, maximum=20, keep=0)
        for tag in range(6):
            jitter.push(QUIET, tag)
        play(jitter, 6)
        self.assertGreater(jitter.skipped, 0)
        jitter.begin_announcement()
        self.assertEqual(jitter.skipped, 0)

    def test_client_defaults(self):
        jitter = make_jitter_buffer()
        self.assertEqual((jitter.target_blocks, jitter.max_blocks), (8, 75))
        legacy = make_jitter_buffer(250, max_buffer_ms=0)
        self.assertEqual((legacy.target_blocks, legacy.max_blocks), (None, 12))


if __name__ == "__main__":
    unittest.main()
