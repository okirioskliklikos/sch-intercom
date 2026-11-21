"""Έλεγχοι λογικής της οθόνης Director χωρίς οθόνη ή ήχο."""
from unittest import TestCase
from unittest.mock import patch
import sys
import threading
from types import SimpleNamespace

from director.gui import AnnouncementControls, summarize_rooms
from director.main import stream_microphone
from shared.audio import BLOCK_BYTES, BLOCK_SAMPLES


class FakeBroadcaster:
    def __init__(self):
        self.calls = []

    def set_targets(self, *, rooms=None, client_ids=None):
        self.calls.append((rooms, client_ids))


class DirectorGuiControlTests(TestCase):
    def setUp(self):
        self.broadcaster = FakeBroadcaster()
        self.controls = AnnouncementControls(self.broadcaster)

    def test_startup_does_not_broadcast_to_any_client(self):
        self.assertEqual(self.broadcaster.calls, [(set(), set())])
        self.assertFalse(self.controls.active)

    def test_start_all_then_stop(self):
        self.controls.start()
        self.assertTrue(self.controls.active)
        self.assertEqual(self.broadcaster.calls[-1], (None, None))
        self.controls.stop()
        self.assertEqual(self.broadcaster.calls[-1], (set(), set()))
        self.assertFalse(self.controls.active)

    def test_start_selected_rooms(self):
        rooms = {"C2627-Γ1", "C2627-PcLab"}
        self.controls.start(rooms)
        self.assertEqual(self.broadcaster.calls[-1], (rooms, set()))
        rooms.clear()
        self.assertEqual(self.controls.target_rooms, {"C2627-Γ1", "C2627-PcLab"})

    def test_empty_selection_is_rejected_without_audio(self):
        with self.assertRaises(ValueError):
            self.controls.start(set())
        self.assertEqual(self.broadcaster.calls, [(set(), set())])

    def test_repeated_start_and_stop_are_safe(self):
        self.controls.start({"C2627-Γ1"})
        self.controls.start({"C2627-Γ2"})
        self.assertEqual(len(self.broadcaster.calls), 2)
        self.controls.stop()
        self.controls.stop()
        self.assertEqual(self.broadcaster.calls[-1], (set(), set()))

    def test_room_counts_include_multiple_clients_and_offline(self):
        status = [("id-1", "C2627-Γ1", True, 0.1),
                  ("id-2", "C2627-Γ1", True, 0.1),
                  ("id-3", "C2627-Γ1", False, 7.0),
                  ("id-4", "C2627-Γ2", False, 8.0)]
        self.assertEqual(summarize_rooms(status),
                         [("C2627-Γ1", 2, 1), ("C2627-Γ2", 0, 1)])

    def test_real_broadcaster_stays_silent_until_toggle(self):
        from director.broadcast import AudioBroadcaster
        broadcaster = AudioBroadcaster()
        try:
            controls = AnnouncementControls(broadcaster)
            client = SimpleNamespace(room="C2627-Γ1", client_id="test-client")
            other = SimpleNamespace(room="C2627-Γ2", client_id="other-client")
            self.assertFalse(broadcaster._is_selected(client))
            self.assertFalse(broadcaster._is_selected(other))
            controls.start({"C2627-Γ1"})
            self.assertTrue(broadcaster._is_selected(client))
            self.assertFalse(broadcaster._is_selected(other))
            controls.stop()
            self.assertFalse(broadcaster._is_selected(client))
        finally:
            broadcaster.close()

    def test_invalid_room_does_not_enable_announcement(self):
        from director.broadcast import AudioBroadcaster
        broadcaster = AudioBroadcaster()
        try:
            controls = AnnouncementControls(broadcaster)
            with self.assertRaises(ValueError):
                controls.start({"Αίθουσα\nΓ1"})
            self.assertFalse(controls.active)
        finally:
            broadcaster.close()


class MicrophoneMeterTests(TestCase):
    def test_input_stream_reports_ready_and_peak_without_audio_hardware(self):
        stop = threading.Event()
        levels = []
        ready = []
        publishes = []

        class FakeInput:
            latency = 0.04

            def __init__(self, **kwargs):
                self.callback = kwargs["callback"]

            def __enter__(self):
                # Mono signed 16-bit: silence except for one half-scale sample.
                block = (b"\x00\x40" + bytes(BLOCK_BYTES - 2))
                self.callback(block, BLOCK_SAMPLES, None, None)
                stop.set()
                return self

            def __exit__(self, *_args):
                return False

        fake_sounddevice = SimpleNamespace(RawInputStream=FakeInput)
        broadcaster = SimpleNamespace(publish=lambda *args: publishes.append(args))
        with patch.dict(sys.modules, {"sounddevice": fake_sounddevice}):
            stream_microphone(broadcaster, stop=stop, level_callback=levels.append,
                              ready_callback=lambda: ready.append(True))
        self.assertEqual(ready, [True])
        self.assertEqual(len(levels), 1)
        self.assertAlmostEqual(levels[0], 0.5)
        self.assertEqual(publishes, [])


if __name__ == "__main__":
    import unittest
    unittest.main()
