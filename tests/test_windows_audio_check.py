"""Έλεγχοι της λογικής επαναφοράς του tools/windows_audio_check.py.

Δεν χρειάζονται Windows: χρησιμοποιούνται ψεύτικες συσκευές, συνεδρίες ήχου και
συνεδρίες media. Ο πραγματικός ήχος δοκιμάζεται μόνο από τον χρήστη σε Windows.
"""

import asyncio
import io
import sys
import unittest
from contextlib import redirect_stdout

from tools import windows_audio_check as wac


class FakeMaster:
    def __init__(self, volume=0.5, muted=False):
        self.volume = volume
        self.muted = muted

    def get_volume(self):
        return self.volume

    def set_volume(self, value):
        self.volume = value

    def get_mute(self):
        return self.muted

    def set_mute(self, muted):
        self.muted = muted


class FakeSession:
    def __init__(self, name, pid, muted=False, mute_works=True):
        self.name = name
        self.pid = pid
        self.muted = muted
        self.mute_works = mute_works
        self.closed = False
        self.volume = 1.0
        self.volume_history = []

    def get_mute(self):
        if self.closed:
            raise OSError("session closed")
        return self.muted

    def set_mute(self, muted):
        if self.closed:
            raise OSError("session closed")
        if self.mute_works:
            self.muted = muted

    def get_volume(self):
        return self.volume

    def set_volume(self, value):
        if self.closed:
            raise OSError("session closed")
        self.volume = value
        self.volume_history.append(value)


class FakeMedia:
    def __init__(self, app_id, status=wac.STATUS_PLAYING, pause_enabled=True, obeys=True):
        self.app_id = app_id
        self._status = status
        self.pause_enabled = pause_enabled
        self.obeys = obeys
        self.play_calls = 0

    def status(self):
        return self._status

    def can_pause(self):
        return self.pause_enabled

    def can_play(self):
        return True

    async def pause(self):
        if self.obeys:
            self._status = wac.STATUS_PAUSED
        return True

    async def play(self):
        self.play_calls += 1
        self._status = wac.STATUS_PLAYING
        return True


class MasterVolumeTests(unittest.TestCase):
    def test_pick_test_volume_differs_from_current(self):
        self.assertEqual(wac.pick_test_volume(0.8, 0.3), 0.3)
        self.assertGreaterEqual(abs(wac.pick_test_volume(0.32, 0.3) - 0.32), 0.10)
        self.assertGreaterEqual(abs(wac.pick_test_volume(0.6, 0.6) - 0.6), 0.10)

    def test_apply_and_restore(self):
        master = FakeMaster(0.8, muted=True)
        change = wac.apply_master(master, 0.3)
        self.assertEqual((master.volume, master.muted), (0.3, False))
        wac.restore_master(master, change)
        self.assertEqual((master.volume, master.muted), (0.8, True))

    def test_unmuted_original_mute_untouched(self):
        master = FakeMaster(0.8, muted=False)
        change = wac.apply_master(master, 0.3)
        self.assertIsNone(change.applied_mute)
        wac.restore_master(master, change)
        self.assertFalse(master.muted)

    def test_manual_volume_change_is_respected(self):
        master = FakeMaster(0.8)
        change = wac.apply_master(master, 0.3)
        master.volume = 0.55  # ο εκπαιδευτικός άλλαξε την ένταση
        notes = wac.restore_master(master, change)
        self.assertEqual(master.volume, 0.55)
        self.assertIn("ΔΕΝ επαναφέρθηκε", notes[0])

    def test_small_rounding_still_restores(self):
        master = FakeMaster(0.8)
        change = wac.apply_master(master, 0.3)
        master.volume = 0.3049  # στρογγυλοποίηση των Windows
        wac.restore_master(master, change)
        self.assertEqual(master.volume, 0.8)


class SessionMuteTests(unittest.TestCase):
    def test_mutes_others_and_restores_only_ours(self):
        own = FakeSession("python.exe", 100)
        chrome = FakeSession("chrome.exe", 200)
        already = FakeSession("spotify.exe", 300, muted=True)
        muted, _ = wac.mute_other_sessions([own, chrome, already], own_pid=100)
        self.assertEqual(muted, [chrome])
        self.assertFalse(own.muted)
        self.assertTrue(chrome.muted)
        wac.unmute_our_sessions(muted)
        self.assertFalse(chrome.muted)
        self.assertTrue(already.muted)  # ήταν σε mute πριν, μένει σε mute

    def test_manual_unmute_is_respected(self):
        chrome = FakeSession("chrome.exe", 200)
        muted, _ = wac.mute_other_sessions([chrome], own_pid=1)
        chrome.muted = False  # ο χρήστης το ξεσίγασε
        chrome.mute_works = False  # αν το αγγίζαμε, θα φαινόταν
        notes = wac.unmute_our_sessions(muted)
        self.assertIn("δεν αγγίζεται", notes[0])

    def test_failed_mute_not_tracked(self):
        stubborn = FakeSession("game.exe", 400, mute_works=False)
        muted, notes = wac.mute_other_sessions([stubborn], own_pid=1)
        self.assertEqual(muted, [])
        self.assertIn("δεν έπιασε", notes[0])

    def test_closed_session_does_not_abort_restore(self):
        a = FakeSession("a.exe", 1)
        b = FakeSession("b.exe", 2)
        muted, _ = wac.mute_other_sessions([a, b], own_pid=99)
        a.closed = True
        notes = wac.unmute_our_sessions(muted)
        self.assertIn("ΣΦΑΛΜΑ", notes[0])
        self.assertFalse(b.muted)


class MediaPauseTests(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def test_pauses_playing_and_resumes_only_ours(self):
        playing = FakeMedia("Spotify.exe")
        paused_before = FakeMedia("Chrome", status=wac.STATUS_PAUSED)
        paused, _ = self.run_async(wac.pause_playing_media([playing, paused_before], settle_seconds=0))
        self.assertEqual(paused, [playing])
        self.run_async(wac.resume_our_media(paused))
        self.assertEqual(playing.status(), wac.STATUS_PLAYING)
        self.assertEqual(paused_before.status(), wac.STATUS_PAUSED)
        self.assertEqual(paused_before.play_calls, 0)

    def test_manual_resume_or_stop_is_respected(self):
        playing = FakeMedia("MSEdge")
        paused, _ = self.run_async(wac.pause_playing_media([playing], settle_seconds=0))
        playing._status = 3  # Stopped
        self.run_async(wac.resume_our_media(paused))
        self.assertEqual(playing.play_calls, 0)

    def test_reports_apps_without_pause(self):
        no_pause = FakeMedia("Radio", pause_enabled=False)
        ignores = FakeMedia("Weird", obeys=False)
        paused, notes = self.run_async(wac.pause_playing_media([no_pause, ignores], settle_seconds=0))
        self.assertEqual(paused, [])
        joined = "\n".join(notes)
        self.assertIn("ΔΕΝ δηλώνει δυνατότητα pause", joined)
        self.assertIn("δέχτηκε το pause αλλά", joined)


class RampTests(unittest.TestCase):
    def test_ramp_is_gradual(self):
        values, sleeps = [], []
        wac.ramp_volume(values.append, 0.5, 0.3, 150, sleep=sleeps.append)
        self.assertEqual(len(values), 15)
        self.assertAlmostEqual(values[-1], 0.3)
        self.assertTrue(all(a > b for a, b in zip(values, values[1:])))
        self.assertLessEqual(max(abs(a - b) for a, b in zip([0.5] + values, values)), 0.02)
        self.assertAlmostEqual(sum(sleeps), 0.14)

    def test_zero_ramp_is_single_step(self):
        values = []
        wac.ramp_volume(values.append, 0.5, 0.3, 0, sleep=lambda _: None)
        self.assertEqual(values, [0.3])

    def test_master_with_ramp_restores(self):
        master = FakeMaster(0.8)
        change = wac.apply_master(master, 0.3, ramp_ms=150, sleep=lambda _: None)
        self.assertAlmostEqual(master.volume, 0.3)
        wac.restore_master(master, change, ramp_ms=150, sleep=lambda _: None)
        self.assertAlmostEqual(master.volume, 0.8)

    def test_fade_mute_keeps_app_volume(self):
        chrome = FakeSession("chrome.exe", 200)
        chrome.volume = 0.7
        muted, _ = wac.mute_other_sessions([chrome], own_pid=1, ramp_ms=150, sleep=lambda _: None)
        self.assertTrue(chrome.muted)
        self.assertEqual(chrome.volume, 0.7)  # μένει η αρχική ένταση
        self.assertEqual(min(chrome.volume_history), 0.0)  # χαμήλωσε σταδιακά πριν το mute
        wac.unmute_our_sessions(muted, ramp_ms=150, sleep=lambda _: None)
        self.assertFalse(chrome.muted)
        self.assertAlmostEqual(chrome.volume, 0.7)

    def test_manual_unmute_with_ramp_not_stuck_at_zero(self):
        chrome = FakeSession("chrome.exe", 200)
        muted, _ = wac.mute_other_sessions([chrome], own_pid=1, ramp_ms=150, sleep=lambda _: None)
        chrome.muted = False  # ο χρήστης ξεσίγασε
        wac.unmute_our_sessions(muted, ramp_ms=150, sleep=lambda _: None)
        self.assertEqual(chrome.volume, 1.0)


class GuardTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "ο έλεγχος αφορά μη Windows")
    def test_exits_cleanly_outside_windows(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = wac.main([])
        self.assertEqual(code, 2)
        self.assertIn("μόνο σε Windows", out.getvalue())


if __name__ == "__main__":
    unittest.main()
