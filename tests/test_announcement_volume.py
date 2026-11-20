"""Λογικοί έλεγχοι προσωρινής έντασης χωρίς Windows ή φυσική κάρτα ήχου."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from client.windows_volume import AnnouncementVolume


class FakeMaster:
    device_name = "test-output"

    def __init__(self, volume=0.2, mute=False):
        self.volume = volume
        self.mute = mute
        self.writes = []
        self.fail_at = None

    def get_volume(self):
        return self.volume

    def set_volume(self, value):
        if self.fail_at is not None and value >= self.fail_at:
            raise RuntimeError("simulate Core Audio failure")
        self.volume = value
        self.writes.append(value)

    def get_mute(self):
        return self.mute

    def set_mute(self, value):
        self.mute = value


class AnnouncementVolumeTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "recovery.json"
        self.master = FakeMaster()
        self.logs = []

    def controller(self, master=None, target=80):
        return AnnouncementVolume(master or self.master, self.path,
                                  target_percent=target, sleep=lambda _: None,
                                  log=self.logs.append)

    def test_begin_and_end_restore_original_volume(self):
        manager = self.controller()
        self.assertTrue(manager.begin())
        self.assertAlmostEqual(self.master.volume, 0.8)
        self.assertTrue(self.path.exists())
        manager.end()
        self.assertAlmostEqual(self.master.volume, 0.2)
        self.assertFalse(self.path.exists())

    def test_previously_muted_output_is_restored(self):
        self.master.mute = True
        manager = self.controller()
        manager.begin()
        self.assertFalse(self.master.mute)
        manager.end()
        self.assertTrue(self.master.mute)
        self.assertAlmostEqual(self.master.volume, 0.2)

    def test_manual_volume_change_is_preserved(self):
        manager = self.controller()
        manager.begin()
        self.master.volume = 0.4
        manager.end()
        self.assertAlmostEqual(self.master.volume, 0.4)
        self.assertFalse(self.path.exists())

    def test_manual_mute_during_announcement_is_preserved(self):
        manager = self.controller()
        manager.begin()
        self.master.mute = True
        manager.end()
        self.assertTrue(self.master.mute)

    def test_repeated_begin_does_not_overwrite_initial_volume(self):
        manager = self.controller()
        manager.begin()
        manager.begin()
        manager.end()
        self.assertAlmostEqual(self.master.volume, 0.2)

    def test_crash_recovery_restores_still_applied_setting(self):
        first = self.controller()
        first.begin()
        second = self.controller()
        self.assertTrue(second.recover())
        self.assertAlmostEqual(self.master.volume, 0.2)
        self.assertFalse(self.path.exists())

    def test_crash_recovery_does_not_override_manual_change(self):
        self.controller().begin()
        self.master.volume = 0.3
        self.assertTrue(self.controller().recover())
        self.assertAlmostEqual(self.master.volume, 0.3)
        self.assertFalse(self.path.exists())

    def test_crash_during_ramp_restores_partial_change(self):
        self.path.write_text(json.dumps({
            "version": 1, "device": "test-output", "original": 0.2,
            "original_mute": False, "target": 0.8, "stage": "pending",
            "unmuted": False,
        }), encoding="utf-8")
        self.master.volume = 0.5
        self.assertTrue(self.controller().recover())
        self.assertAlmostEqual(self.master.volume, 0.2)

    def test_different_endpoint_remains_untouched(self):
        self.controller().begin()
        other = FakeMaster(0.3)
        other.device_name = "other-output"
        self.assertFalse(self.controller(master=other).recover())
        self.assertAlmostEqual(other.volume, 0.3)
        self.assertTrue(self.path.exists())

    def test_invalid_recovery_record_rejected(self):
        self.path.write_text('{"version": 1}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.controller().recover()

    def test_begin_writes_record_before_first_volume_change(self):
        manager = self.controller()
        original_set = self.master.set_volume
        def check_record(value):
            self.assertTrue(self.path.exists())
            self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["stage"],
                             "pending")
            original_set(value)
        self.master.set_volume = check_record
        try:
            manager.begin()
        finally:
            self.master.set_volume = original_set
        manager.end()

    def test_failure_during_ramp_attempts_restore(self):
        self.master.fail_at = 0.5
        manager = self.controller()
        with self.assertRaises(RuntimeError):
            manager.begin()
        self.assertAlmostEqual(self.master.volume, 0.2)


class PumpIntegrationTests(unittest.TestCase):
    def test_stop_waits_until_jitter_drains_before_restoring(self):
        import time
        from unittest.mock import patch
        from client.main import pump_frames
        from shared.audio import Frame, ControlMessage, MESSAGE_STREAM_STOP, MESSAGE_KEEPALIVE
        from shared.jitter import JitterBuffer, DelayTracker

        class Connection:
            def gettimeout(self):
                return None

        class VolumeSpy:
            def __init__(self):
                self.active = False
                self.events = []

            def begin(self):
                self.active = True
                self.events.append("begin")

            def end(self):
                self.active = False
                self.events.append("end")

        volume = VolumeSpy()
        jitter = JitterBuffer(12, 640, prefill_blocks=1)
        jitter.standby()
        responses = iter([
            Frame(0, 0, bytes(640)),
            ControlMessage(MESSAGE_STREAM_STOP),
            ControlMessage(MESSAGE_KEEPALIVE),
        ])

        def receive(*_args, **_kwargs):
            try:
                message = next(responses)
            except StopIteration:
                raise EOFError("end of synthetic stream")
            if isinstance(message, ControlMessage) and message.kind == MESSAGE_KEEPALIVE:
                self.assertTrue(volume.active)  # STOP alone does not restore
                jitter.pop()  # last PCM
                jitter.pop()  # callback enters standby
                time.sleep(0.11)  # simulated minimum hardware drain allowance
            return message

        with patch("client.main.receive_message", side_effect=receive):
            with self.assertRaises(EOFError):
                pump_frames(Connection(), jitter, DelayTracker(), volume=volume)
        self.assertEqual(volume.events, ["begin", "end"])


if __name__ == "__main__":
    unittest.main()
