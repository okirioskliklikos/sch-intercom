"""Διορθώσεις μετά το PR #25: νήμα ήχου Windows, σειρά, pause/play.

Ψεύτικες εφαρμογές/media με καθυστερήσεις, χωρίς Windows.
"""

import asyncio
import socket
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from client.main import pump_frames
from client.windows_apps import ApplicationAudio
from client.windows_volume import AnnouncementVolume, AudioPolicyWorker
from shared.audio import BLOCK_BYTES, MESSAGE_STREAM_STOP, encode_control, encode_frame
from shared.jitter import DelayTracker, JitterBuffer

PLAYING, PAUSED = 4, 5


class Events(list):
    def add(self, name):
        self.append(name)


class FakeApp:
    def __init__(self, events, pid=20, volume=0.7):
        self.name, self.pid = "chrome.exe", pid
        self.session_id, self.started = f"s{pid}", 100.0 + pid
        self.volume, self.events = volume, events

    def get_volume(self):
        return self.volume

    def set_volume(self, value):
        if value == 0:
            self.events.add("app0")
        self.volume = value

    def get_mute(self):
        return False


class FakeMedia:
    def __init__(self, events, app_id="Chrome", pause_delay=0.0, confirms=True,
                 play_ok=True):
        self.app_id, self.events = app_id, events
        self.playing = True
        self.pause_delay, self.confirms, self.play_ok = pause_delay, confirms, play_ok
        self.plays = 0

    def status(self):
        return PLAYING if self.playing else PAUSED

    def can_pause(self):
        return True

    async def pause(self):
        await asyncio.sleep(self.pause_delay)
        self.events.add("pause")
        if self.confirms:
            self.playing = False
        return True

    async def play(self):
        self.plays += 1
        if self.play_ok:
            self.playing = True
        return self.play_ok


class FakeMaster:
    device_name = "Fake speakers"

    def __init__(self, events):
        self.volume, self.mute, self.events = 0.2, False, events

    def get_volume(self):
        return self.volume

    def set_volume(self, value):
        if abs(value - 0.8) < 1e-9:
            self.events.add("master")
        self.volume = value

    def get_mute(self):
        return self.mute

    def set_mute(self, value):
        self.mute = value


class Base(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "volume-recovery.json"
        self.events = Events()

    def manager(self, apps, media):
        async def load_media():
            return list(media)

        engine = ApplicationAudio(sessions=lambda: list(apps), media=load_media, own_pid=1,
                                  sleep=lambda _: None, log=lambda _: None)
        return AnnouncementVolume(FakeMaster(self.events), self.path, target_percent=80,
                                  apps=engine, sleep=lambda _: None, log=lambda _: None)


class OrderAndPauseTests(Base):
    def test_apps_to_zero_then_master_then_pause(self):
        manager = self.manager([FakeApp(self.events)], [FakeMedia(self.events)])
        self.assertTrue(manager.begin())
        self.assertEqual(self.events, ["app0", "master", "pause"])

    def test_pauses_run_in_parallel_with_total_limit(self):
        slow = [FakeMedia(self.events, pause_delay=0.6) for _ in range(3)]
        never = FakeMedia(self.events, confirms=False)
        manager = self.manager([FakeApp(self.events)], slow + [never])
        started = time.monotonic()
        manager.begin()
        self.assertLess(time.monotonic() - started, 1.3)  # σειριακά θα ήταν > 1.8 s
        self.assertEqual(len(manager._saved["media"]), 3)  # το «never» δεν κρατιέται

    def test_two_media_of_same_app_both_resume(self):
        first, second = FakeMedia(self.events), FakeMedia(self.events)
        manager = self.manager([FakeApp(self.events)], [first, second])
        manager.begin()
        self.assertFalse(first.playing or second.playing)
        manager.end()
        self.assertTrue(first.playing and second.playing)

    def test_refused_play_is_tried_once_and_not_kept(self):
        stubborn = FakeMedia(self.events, play_ok=False)
        app = FakeApp(self.events)
        manager = self.manager([app], [stubborn])
        manager.begin()
        manager.end()
        self.assertEqual(stubborn.plays, 1)
        self.assertAlmostEqual(app.volume, 0.7)
        self.assertFalse(self.path.exists())  # το journal δεν «κολλάει»
        # Επόμενη ανακοίνωση λειτουργεί κανονικά και δεν ξαναπατά play.
        self.assertTrue(manager.begin())
        self.assertEqual(stubborn.plays, 1)

    def test_still_paused_media_is_played_once(self):
        media = FakeMedia(self.events)
        manager = self.manager([FakeApp(self.events)], [media])
        manager.begin()
        manager.end()
        manager.end()  # δεύτερη κλήση: τίποτα
        self.assertEqual(media.plays, 1)

    def test_user_resumed_media_not_touched(self):
        media = FakeMedia(self.events)
        manager = self.manager([FakeApp(self.events)], [media])
        manager.begin()
        media.playing = True  # ο χρήστης πάτησε play μόνος του
        manager.end()
        self.assertEqual(media.plays, 0)


class SlowController:
    def __init__(self, begin_delay=0.0, gate=None):
        self.begin_delay, self.gate = begin_delay, gate
        self.begins = self.ends = 0
        self.thread_ids = set()

    def begin(self):
        self.thread_ids.add(threading.get_ident())
        if self.gate is not None:
            self.gate.wait(5)
        time.sleep(self.begin_delay)
        self.begins += 1
        return True

    def end(self):
        self.ends += 1


class WorkerTests(unittest.TestCase):
    def test_begin_returns_immediately_and_runs_in_other_thread(self):
        controller = SlowController(begin_delay=0.5)
        worker = AudioPolicyWorker(lambda: controller, log=lambda _: None)
        self.addCleanup(worker.close)
        started = time.monotonic()
        worker.begin()
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertTrue(worker.wait_idle(3))
        self.assertEqual(controller.begins, 1)
        self.assertNotIn(threading.get_ident(), controller.thread_ids)

    def test_new_announcement_cancels_pending_restore(self):
        gate = threading.Event()
        controller = SlowController(gate=gate)
        worker = AudioPolicyWorker(lambda: controller, log=lambda _: None)
        self.addCleanup(worker.close)
        worker.begin()  # μπλοκάρει στο gate
        worker.end()
        worker.begin()  # νέα ανακοίνωση πριν εκτελεστεί η επαναφορά
        gate.set()
        self.assertTrue(worker.wait_idle(3))
        self.assertEqual((controller.begins, controller.ends), (1, 0))

    def test_close_restores_and_stops(self):
        controller = SlowController()
        worker = AudioPolicyWorker(lambda: controller, log=lambda _: None)
        worker.begin()
        worker.wait_idle(3)
        worker.close()
        self.assertEqual(controller.ends, 1)

    def test_failing_factory_is_harmless(self):
        def broken():
            raise OSError("no audio")

        worker = AudioPolicyWorker(broken, log=lambda _: None)
        worker.begin()
        self.assertTrue(worker.wait_idle(3))
        worker.close()

    def test_receive_loop_not_blocked_and_no_blocks_lost(self):
        controller = SlowController(begin_delay=1.0)
        worker = AudioPolicyWorker(lambda: controller, log=lambda _: None)
        self.addCleanup(worker.close)
        jitter = JitterBuffer(100, BLOCK_BYTES, prefill_blocks=1)
        jitter.standby()
        sender, receiver = socket.socketpair()
        self.addCleanup(sender.close)
        self.addCleanup(receiver.close)
        frames = 20
        for seq in range(frames):
            sender.sendall(encode_frame(bytes(BLOCK_BYTES), seq, 0))
        sender.sendall(encode_control(MESSAGE_STREAM_STOP))
        sender.close()
        started = time.monotonic()
        with self.assertRaises(EOFError):
            pump_frames(receiver, jitter, DelayTracker(), volume=worker)
        self.assertLess(time.monotonic() - started, 0.5)  # δεν περίμενε το 1 s
        self.assertEqual(len(jitter), frames)  # κανένα block δεν πετάχτηκε


if __name__ == "__main__":
    unittest.main()
