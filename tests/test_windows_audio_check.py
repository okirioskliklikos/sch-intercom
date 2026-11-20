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
    def __init__(self, name, pid, muted=False, mute_works=True, volume=1.0, volume_works=True,
                 active=True):
        self.name = name
        self.pid = pid
        self.muted = muted
        self.mute_works = mute_works
        self.closed = False
        self.volume = volume
        self.volume_works = volume_works
        self.active = active
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

    def is_active(self):
        return self.active

    def set_volume(self, value):
        if self.closed:
            raise OSError("session closed")
        if self.volume_works:
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


NO_SLEEP = {"sleep": lambda _: None}


class SessionVolumeTests(unittest.TestCase):
    def test_ramps_others_to_zero_and_back(self):
        own = FakeSession("python.exe", 100)
        chrome = FakeSession("chrome.exe", 200, volume=0.7)
        already = FakeSession("spotify.exe", 300, muted=True)
        holds, _ = wac.mute_other_sessions([own, chrome, already], own_pid=100, ramp_ms=150, **NO_SLEEP)
        self.assertEqual([h.session for h in holds], [chrome])
        self.assertEqual(chrome.volume, 0.0)
        self.assertFalse(chrome.muted)  # δεν χρησιμοποιείται η σημαία mute
        self.assertEqual(own.volume_history, [])
        self.assertEqual(already.volume_history, [])
        self.assertGreater(len(chrome.volume_history), 10)  # σταδιακά, όχι με ένα βήμα
        wac.unmute_our_sessions(holds, ramp_ms=150, **NO_SLEEP)
        self.assertAlmostEqual(chrome.volume, 0.7)
        self.assertTrue(already.muted)

    def test_already_zero_is_untouched(self):
        silent = FakeSession("game.exe", 1, volume=0.0)
        holds, notes = wac.mute_other_sessions([silent], own_pid=99, **NO_SLEEP)
        self.assertEqual(holds, [])
        self.assertIn("ήδη στο 0%", notes[0])

    def test_manual_volume_change_is_respected(self):
        chrome = FakeSession("chrome.exe", 200, volume=0.7)
        holds, _ = wac.mute_other_sessions([chrome], own_pid=1, **NO_SLEEP)
        chrome.volume = 0.4  # ο χρήστης ανέβασε την ένταση της εφαρμογής
        notes = wac.unmute_our_sessions(holds, **NO_SLEEP)
        self.assertEqual(chrome.volume, 0.4)
        self.assertIn("δεν αγγίζεται", notes[0])

    def test_failed_volume_change_not_tracked(self):
        stubborn = FakeSession("game.exe", 400, volume_works=False)
        holds, notes = wac.mute_other_sessions([stubborn], own_pid=1, **NO_SLEEP)
        self.assertEqual(holds, [])
        self.assertIn("δεν έπιασε", notes[0])

    def test_closed_session_does_not_abort_restore(self):
        a = FakeSession("a.exe", 1, volume=0.5)
        b = FakeSession("b.exe", 2, volume=0.5)
        holds, _ = wac.mute_other_sessions([a, b], own_pid=99, **NO_SLEEP)
        a.closed = True
        notes = wac.unmute_our_sessions(holds, **NO_SLEEP)
        self.assertIn("ΣΦΑΛΜΑ", notes[0])
        self.assertAlmostEqual(b.volume, 0.5)


class MatchTests(unittest.TestCase):
    def test_matches_by_process_name(self):
        chrome = FakeSession("chrome.exe", 10)
        edge = FakeSession("msedge.exe", 11)
        spotify = FakeSession("Spotify.exe", 12)
        system = FakeSession("Ήχοι συστήματος", 0)
        audio = [chrome, edge, spotify, system]
        self.assertEqual(wac.match_audio_sessions("Chrome", audio), [chrome])
        self.assertEqual(wac.match_audio_sessions("MSEdge", audio), [edge])
        self.assertEqual(wac.match_audio_sessions("Spotify.exe", audio), [spotify])
        self.assertEqual(
            wac.match_audio_sessions("SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify", audio), [spotify])
        self.assertEqual(wac.match_audio_sessions("308046B0AF4A39CB", audio), [])


class MediaPauseTests(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def pause(self, media, audio=(), **kw):
        return self.run_async(wac.pause_playing_media(
            media, audio, ramp_ms=150, settle_seconds=0, **NO_SLEEP, **kw))

    def resume(self, paused):
        return self.run_async(wac.resume_our_media(paused, ramp_ms=150, **NO_SLEEP))

    def test_volume_to_zero_then_pause_then_play_then_volume(self):
        events = []
        chrome_audio = FakeSession("chrome.exe", 10, volume=0.8)
        chrome_audio.set_volume = lambda v: (events.append(("vol", round(v, 3))),
                                             setattr(chrome_audio, "volume", v))
        media = FakeMedia("Chrome")
        orig_pause, orig_play = media.pause, media.play

        async def pause():
            events.append(("pause", chrome_audio.volume))
            return await orig_pause()

        async def play():
            events.append(("play", chrome_audio.volume))
            return await orig_play()

        media.pause, media.play = pause, play
        paused, _ = self.pause([media], [chrome_audio])
        self.assertEqual([p.session for p in paused], [media])
        self.assertEqual(events[-1], ("pause", 0.0))  # pause μόνο αφού έφτασε στο 0
        self.assertGreater(len(events), 10)  # σταδιακή μείωση πριν το pause
        self.assertEqual(chrome_audio.volume, 0.0)  # μένει στο 0 όσο είναι σε pause
        events.clear()
        self.resume(paused)
        self.assertEqual(events[0], ("play", 0.0))  # play αμέσως, με ένταση 0
        self.assertEqual(events[-1], ("vol", 0.8))  # μετά σταδιακά στην αρχική
        self.assertEqual(media.status(), wac.STATUS_PLAYING)

    def test_pauses_playing_and_resumes_only_ours(self):
        playing = FakeMedia("Spotify.exe")
        paused_before = FakeMedia("Chrome", status=wac.STATUS_PAUSED)
        paused, _ = self.pause([playing, paused_before])
        self.assertEqual([p.session for p in paused], [playing])
        self.resume(paused)
        self.assertEqual(playing.status(), wac.STATUS_PLAYING)
        self.assertEqual(paused_before.play_calls, 0)

    def test_manual_play_still_restores_volume(self):
        audio = FakeSession("msedge.exe", 10, volume=0.6)
        media = FakeMedia("MSEdge")
        paused, _ = self.pause([media], [audio])
        media._status = wac.STATUS_PLAYING  # ο χρήστης πάτησε play μόνος του
        notes = self.resume(paused)
        self.assertEqual(media.play_calls, 0)
        self.assertAlmostEqual(audio.volume, 0.6)  # αλλιώς θα έπαιζε χωρίς ήχο
        self.assertIn("χωρίς play", "\n".join(notes))

    def test_manual_stop_is_respected(self):
        media = FakeMedia("MSEdge")
        paused, _ = self.pause([media])
        media._status = 3  # Stopped
        self.resume(paused)
        self.assertEqual(media.play_calls, 0)

    def test_failed_pause_restores_volume(self):
        audio = FakeSession("weird.exe", 10, volume=0.9)
        ignores = FakeMedia("Weird", obeys=False)
        no_pause = FakeMedia("Radio", pause_enabled=False)
        paused, notes = self.pause([no_pause, ignores], [audio])
        self.assertEqual(paused, [])
        self.assertAlmostEqual(audio.volume, 0.9)
        joined = "\n".join(notes)
        self.assertIn("ΔΕΝ δηλώνει δυνατότητα pause", joined)
        self.assertIn("δέχτηκε το pause αλλά", joined)

    def test_unmatched_unknown_app_is_reported(self):
        media = FakeMedia("A1B2C3D4E5F60718")
        paused, notes = self.pause([media], [FakeSession("vlc.exe", 10)])
        self.assertEqual(len(paused), 1)
        self.assertIn("χωρίς σταδιακή μείωση", "\n".join(notes))

    def test_firefox_fallback_fades_its_sessions(self):
        ff1 = FakeSession("firefox.exe", 10, volume=0.7)
        ff2 = FakeSession("firefox.exe", 11, volume=0.5)
        chrome = FakeSession("chrome.exe", 20, volume=0.9)
        firefox = FakeMedia("308046B0AF4A39CB")
        paused, notes = self.pause([firefox], [ff1, ff2, chrome])
        self.assertEqual(len(paused), 1)
        self.assertEqual((ff1.volume, ff2.volume), (0.0, 0.0))
        self.assertEqual(chrome.volume, 0.9)  # δεν αγγίζεται
        self.assertIn("εφεδρική αντιστοίχιση", "\n".join(notes))
        self.resume(paused)
        self.assertAlmostEqual(ff1.volume, 0.7)
        self.assertAlmostEqual(ff2.volume, 0.5)

    def test_no_fallback_when_two_unmatched_media(self):
        ff = FakeSession("firefox.exe", 10, volume=0.7)
        paused, notes = self.pause([FakeMedia("308046B0AF4A39CB"), FakeMedia("ZZZZ1111")], [ff])
        self.assertEqual(len(paused), 2)
        self.assertEqual(ff.volume, 0.7)  # αμφίβολο: απλό pause
        self.assertIn("δεν γίνεται εικασία", "\n".join(notes))

    def test_no_fallback_for_inactive_firefox(self):
        ff = FakeSession("firefox.exe", 10, volume=0.7, active=False)
        _, notes = self.pause([FakeMedia("308046B0AF4A39CB")], [ff])
        self.assertEqual(ff.volume, 0.7)
        self.assertIn("καμία ενεργή συνεδρία", "\n".join(notes))

    def test_named_match_not_used_as_fallback(self):
        # Ο Chrome αντιστοιχίζεται κανονικά· ο firefox.exe μόνο στο άγνωστο media.
        chrome_audio = FakeSession("chrome.exe", 20, volume=0.9)
        ff = FakeSession("firefox.exe", 10, volume=0.7)
        paused, _ = self.pause([FakeMedia("Chrome"), FakeMedia("308046B0AF4A39CB")],
                               [chrome_audio, ff])
        self.assertEqual(len(paused), 2)
        self.assertEqual((chrome_audio.volume, ff.volume), (0.0, 0.0))
        self.resume(paused)
        self.assertAlmostEqual(chrome_audio.volume, 0.9)
        self.assertAlmostEqual(ff.volume, 0.7)


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
        change = wac.apply_master(master, 0.3, ramp_ms=150, **NO_SLEEP)
        self.assertAlmostEqual(master.volume, 0.3)
        wac.restore_master(master, change, ramp_ms=150, **NO_SLEEP)
        self.assertAlmostEqual(master.volume, 0.8)


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
