"""4.4: υπολογισμοί και κρυφά σημεία μέτρησης της δοκιμής πραγματικού δικτύου."""
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from director.broadcast import AudioBroadcaster
from director.main import MELODY_HZ, stream_test_tone
from shared.audio import BLOCK_BYTES, MESSAGE_STREAM_STOP, encode_control, encode_frame
from shared.jitter import DelayTracker, JitterBuffer
from shared.test_report import TestReport, read_report
from tools import network_metrics as nm


def pubs(first, count, start_us=1_000_000, n=1):
    return [{"ev": "pub", "seq": seq, "t": start_us + (seq - first) * nm.BLOCK_US, "n": n}
            for seq in range(first, first + count)]


def plays(seqs, delay_us, start_us=1_000_000, first=0, offset=0):
    """Αναπαραγωγή κάθε block delay_us μετά την αποστολή, στο ρολόι Client (+offset)."""
    return [{"ev": "play", "seq": seq,
             "t": start_us + (seq - first) * nm.BLOCK_US + delay_us + offset} for seq in seqs]


def clock(offset=0):
    result = nm.ClockMap()
    result.add([(100, 150 + offset, 200)])
    return result


class ClockTests(unittest.TestCase):
    def test_best_offset_uses_smallest_round_trip(self):
        samples = [(0, 1_050, 100), (1_000, 1_000 + 500 + 10_000, 21_000), (2_000, 2_510, 2_020)]
        offset, rtt = nm.best_offset(samples)
        self.assertEqual(rtt, 20)
        self.assertEqual(offset, 500)

    def test_empty_samples_rejected(self):
        with self.assertRaises(ValueError):
            nm.best_offset([])

    def test_clock_map_interpolates_drift(self):
        mapping = nm.ClockMap()
        mapping.add([(0, 1_000, 0)])                 # offset 1000 στο 1000
        mapping.add([(1_000_000, 1_001_100, 1_000_000)])   # offset 1100
        self.assertEqual(mapping.offset(0), 1_000)
        self.assertAlmostEqual(mapping.offset(501_050), 1_050, delta=1)
        self.assertEqual(mapping.offset(5_000_000), 1_100)
        self.assertAlmostEqual(mapping.to_director(501_050), 500_000, delta=1)

    def test_error_is_half_worst_round_trip(self):
        mapping = nm.ClockMap()
        mapping.add([(0, 5, 10_000)])
        mapping.add([(0, 5, 4_000)])
        self.assertEqual(mapping.error_ms, 5.0)


class RunTests(unittest.TestCase):
    def test_runs_split_on_unselected_blocks(self):
        events = pubs(0, 5) + pubs(5, 3, n=0) + pubs(8, 4)
        runs = nm.runs_from_pubs(events)
        self.assertEqual([(run.first, run.last, run.size) for run in runs],
                         [(0, 4, 5), (8, 11, 4)])

    def test_runs_split_on_sequence_gap(self):
        runs = nm.runs_from_pubs(pubs(0, 3) + pubs(10, 2, start_us=5_000_000))
        self.assertEqual([(run.first, run.last) for run in runs], [(0, 2), (10, 11)])


class AnalyseTests(unittest.TestCase):
    def test_latency_with_clock_offset(self):
        runs = nm.runs_from_pubs(pubs(0, 50))
        result = nm.analyse("A", runs, plays(range(50), 200_000, offset=7_000_000),
                            clock(7_000_000))
        self.assertEqual(result.played, 50)
        self.assertEqual(result.lost, 0)
        self.assertTrue(all(abs(value - 200) < 0.01 for value in result.latencies_ms))
        self.assertEqual((result.edges_ok, result.edges_total), (6, 6))

    def test_missing_first_block_fails_edges(self):
        runs = nm.runs_from_pubs(pubs(0, 50))
        result = nm.analyse("A", runs, plays(range(1, 50), 150_000), clock())
        self.assertEqual(result.lost, 1)
        self.assertEqual((result.edges_ok, result.edges_total), (5, 6))
        ok, detail = nm.check(result, "wifi")
        self.assertFalse(ok)
        self.assertIn("άκρα 5/6", detail)

    def test_short_run_counts_each_edge_once(self):
        runs = nm.runs_from_pubs(pubs(0, 4))
        result = nm.analyse("A", runs, plays(range(4), 100_000), clock())
        self.assertEqual(result.edges_total, 4)

    def test_silence_inside_run_counts_as_interruption(self):
        runs = nm.runs_from_pubs(pubs(0, 300))
        silence = [{"ev": "silence", "t": 1_500_000 + index * nm.BLOCK_US} for index in range(6)]
        events = (plays(range(300), 100_000) + [{"ev": "underrun", "t": 1_500_000},
                                                {"ev": "underrun", "t": 99_000_000},
                                                {"ev": "silence", "t": 99_000_000}] + silence)
        result = nm.analyse("A", runs, events, clock())
        self.assertEqual(result.underruns, 1)            # τα άλλα είναι εκτός ανακοίνωσης
        self.assertEqual(result.silence, 6)
        self.assertAlmostEqual(result.interrupt, 6 / 300)
        self.assertTrue(nm.check(result, "wifi")[0])     # 2% ≤ 3%
        self.assertFalse(nm.check(result, "lan")[0])     # 2% > 1%
        self.assertIn("120 ms σιωπή", result.summary())

    def test_jitter_playing_only_after_first_block(self):
        jitter = JitterBuffer(5, 4, prefill_blocks=2)
        jitter.begin_announcement()
        jitter.push(bytes(4), 1)
        jitter.pop()                                     # prefill: σιωπή, όχι κενό
        self.assertFalse(jitter.playing)
        jitter.push(bytes(4), 2)
        jitter.pop()
        self.assertTrue(jitter.playing)
        jitter.pop()
        jitter.pop()                                     # άδεια ουρά: κενό μέσα στην ανακοίνωση
        self.assertIsNone(jitter.last_tag)
        self.assertTrue(jitter.playing)
        jitter.finish_announcement()
        self.assertFalse(jitter.playing)

    def test_latency_limit_depends_on_path(self):
        runs = nm.runs_from_pubs(pubs(0, 100))
        result = nm.analyse("A", runs, plays(range(100), 600_000), clock())
        self.assertFalse(nm.check(result, "lan")[0])
        self.assertTrue(nm.check(result, "wifi")[0])

    def test_nothing_played_fails(self):
        runs = nm.runs_from_pubs(pubs(0, 10))
        result = nm.analyse("A", runs, [], clock())
        ok, detail = nm.check(result, "wifi")
        self.assertFalse(ok)
        self.assertIn("κανένα", detail)

    def test_drift_compares_first_and_last_minute(self):
        runs = nm.runs_from_pubs(pubs(0, 9_000))     # 3 λεπτά
        events = [{"ev": "play", "seq": seq,
                   "t": 1_000_000 + seq * nm.BLOCK_US + 150_000 + (seq // 3_000) * 60_000}
                  for seq in range(9_000)]
        result = nm.analyse("A", runs, events, clock())
        self.assertAlmostEqual(nm.drift_ms(result), 120, delta=1)

    def test_sync_spread_between_clients(self):
        runs = nm.runs_from_pubs(pubs(0, 20))
        first = nm.analyse("A", runs, plays(range(20), 100_000), clock())
        second = nm.analyse("B", runs, plays(range(20), 400_000, offset=-3_000), clock(-3_000))
        biggest, p95, count = nm.sync_spread_ms([first, second])
        self.assertAlmostEqual(biggest, 300, delta=0.1)
        self.assertEqual(count, 20)

    def test_virtual_received(self):
        runs = nm.runs_from_pubs(pubs(0, 10) + pubs(20, 10))
        self.assertEqual(nm.virtual_received(runs, set(range(0, 25))), (15, 20))

    def test_path_kind(self):
        self.assertEqual(nm.path_kind("lan", "lan"), "lan")
        self.assertEqual(nm.path_kind("lan", "wifi"), "wifi")
        self.assertEqual(nm.path_kind("lan", None), "wifi")


class HookTests(unittest.TestCase):
    def test_jitter_buffer_reports_tag_of_played_block(self):
        jitter = JitterBuffer(5, 4, prefill_blocks=1)
        jitter.push(b"\x01\x00\x01\x00", 41)
        jitter.push(b"\x02\x00\x02\x00", 42)
        jitter.pop()
        self.assertEqual(jitter.last_tag, 41)
        jitter.pop()
        self.assertEqual(jitter.last_tag, 42)
        jitter.pop()                       # άδεια ουρά: σιωπή χωρίς ετικέτα
        self.assertIsNone(jitter.last_tag)

    def test_dropped_block_tag_is_never_reported(self):
        jitter = JitterBuffer(2, 4, prefill_blocks=1)
        for tag in (1, 2, 3):
            jitter.push(bytes(4), tag)
        jitter.pop()
        self.assertEqual((jitter.dropped, jitter.last_tag), (1, 2))

    def test_report_file_round_trip_ignores_partial_line(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.jsonl"
            report = TestReport(path)
            report.event("play", t=5, seq=1)
            report.event("lost")
            report.close()
            with path.open("a", encoding="utf-8") as handle:
                handle.write('{"ev": "pla')
            events = read_report(path)
        self.assertEqual([event["ev"] for event in events], ["play", "lost"])
        self.assertEqual(events[0]["t"], 5)

    def test_missing_report_is_empty(self):
        self.assertEqual(read_report(Path(tempfile.gettempdir()) / "δεν-υπάρχει.jsonl"), [])

    def test_pump_frames_records_receive_and_stop(self):
        from client.main import pump_frames
        left, right = socket.socketpair()
        with tempfile.TemporaryDirectory() as folder:
            report = TestReport(Path(folder) / "r.jsonl")
            right.sendall(encode_frame(bytes(BLOCK_BYTES), 7, 1234)
                          + encode_control(MESSAGE_STREAM_STOP))
            right.close()
            left.settimeout(1)
            jitter = JitterBuffer(10, BLOCK_BYTES, prefill_blocks=1)
            with self.assertRaises(EOFError):
                pump_frames(left, jitter, DelayTracker(), clock_us=lambda: 99,
                            test_report=report)
            left.close()
            report.close()
            events = read_report(Path(folder) / "r.jsonl")
        self.assertEqual(events[0], {"ev": "rx", "t": 99, "seq": 7, "ts": 1234})
        self.assertEqual(events[1]["ev"], "stop")
        jitter.pop()
        self.assertEqual(jitter.last_tag, 7)

    def test_publish_returns_sequence_and_receivers(self):
        broadcaster = AudioBroadcaster()
        self.assertEqual(broadcaster.publish(bytes(BLOCK_BYTES), 1), (0, 0))
        self.assertEqual(broadcaster.publish(bytes(BLOCK_BYTES), 2), (1, 0))
        broadcaster.close()
        self.assertIsNone(broadcaster.publish(bytes(BLOCK_BYTES), 3))

    def test_melody_tone_reports_each_block(self):
        class Recorder:
            def __init__(self):
                self.blocks = []

            def publish(self, pcm, timestamp_us):
                self.blocks.append(pcm)
                return len(self.blocks) - 1, 2

        recorder, stop = Recorder(), threading.Event()
        with tempfile.TemporaryDirectory() as folder:
            report = TestReport(Path(folder) / "d.jsonl")
            worker = threading.Thread(target=stream_test_tone, args=(recorder,),
                                      kwargs={"stop": stop, "melody": True, "report": report})
            worker.start()
            time.sleep(0.4)
            stop.set()
            worker.join(2)
            report.close()
            events = read_report(Path(folder) / "d.jsonl")
        self.assertGreater(len(recorder.blocks), 5)
        self.assertEqual([event["seq"] for event in events], list(range(len(events))))
        self.assertTrue(all(event["n"] == 2 for event in events))
        self.assertTrue(all(len(block) == BLOCK_BYTES for block in recorder.blocks))
        self.assertEqual(len(MELODY_HZ), 6)


class DiscoveryHintTests(unittest.TestCase):
    """4.4 Εκτέλεση 3: firewalld στο openSUSE έκοβε το beacon."""

    def test_firewalld_hint_gives_command(self):
        from tools.network_live_test import discovery_hint
        text = discovery_hint(firewalld=True)
        self.assertIn("sudo firewall-cmd --add-port=50568/udp", text)
        self.assertIn("--director-host", text)

    def test_generic_hint(self):
        from tools.network_live_test import discovery_hint
        text = discovery_hint(firewalld=False)
        self.assertNotIn("firewall-cmd", text)
        self.assertIn("50568", text)


class LoadCarrierTests(unittest.TestCase):
    """Σχολείο 26/11/2026: οι εικονικοί Clients μόνο σε βοηθούς με καλώδιο, αν υπάρχουν."""

    def carriers(self, *links):
        from types import SimpleNamespace
        from tools.network_live_test import NetworkTest
        test = NetworkTest.__new__(NetworkTest)
        test.peers = [SimpleNamespace(name=f"P{index}", info={"link": link})
                      for index, link in enumerate(links)]
        return [peer.name for peer in test.load_carriers()]

    def test_only_wired_when_mixed(self):
        self.assertEqual(self.carriers("lan", "wifi"), ["P0"])

    def test_all_when_no_wired(self):
        self.assertEqual(self.carriers("wifi", None), ["P0", "P1"])

    def test_all_wired(self):
        self.assertEqual(self.carriers("lan", "lan"), ["P0", "P1"])


class SkipTests(unittest.TestCase):
    def test_skipped_silence_is_not_lost(self):
        runs = nm.runs_from_pubs(pubs(0, 20))
        events = plays([seq for seq in range(20) if seq not in (8, 9)], 100_000) + [
            {"ev": "skip", "t": 0, "seq": 8}, {"ev": "skip", "t": 0, "seq": 9}]
        result = nm.analyse("A", runs, events, clock())
        self.assertEqual((result.lost, result.skipped, result.interrupt), (0, 2, 0.0))
        self.assertIn("παύσεις κόντυναν 40 ms", result.summary())


class RecoveryTests(unittest.TestCase):
    def test_recovery_after_stall(self):
        runs = nm.runs_from_pubs(pubs(0, 500))
        events = []
        for seq in range(500):
            sent = 1_000_000 + seq * nm.BLOCK_US
            extra = 0
            if 200 <= seq < 300:                  # μετά το κόλλημα: +500 ms που μειώνονται
                extra = 500_000 - (seq - 200) * 5_000
            events.append({"ev": "play", "seq": seq, "t": sent + 150_000 + extra})
        result = nm.analyse("A", runs, events, clock())
        resumed = 1_000_000 + 200 * nm.BLOCK_US
        self.assertAlmostEqual(nm.recovery_s(result, resumed), 1.8, delta=0.05)

    def test_no_recovery(self):
        runs = nm.runs_from_pubs(pubs(0, 100))
        events = plays(range(50), 150_000) + plays(range(50, 100), 900_000)
        result = nm.analyse("A", runs, events, clock())
        self.assertIsNone(nm.recovery_s(result, 1_000_000 + 50 * nm.BLOCK_US))


if __name__ == "__main__":
    unittest.main()
