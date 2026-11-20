"""Fake Core Audio / Media Controls: no Windows installation or devices needed."""
import asyncio
from copy import deepcopy
import unittest

from client.windows_apps import ApplicationAudio, restore_record_apps
from client.windows_volume import AnnouncementVolume


class FakeApp:
    def __init__(self, name="chrome.exe", pid=20, session_id="session-A",
                 started=123.0, volume=0.7, mute=False):
        self.name, self.pid = name, pid
        self.session_id, self.started = session_id, started
        self.volume, self.mute = volume, mute
        self.events = []

    def get_volume(self):
        return self.volume

    def set_volume(self, value):
        self.volume = value
        self.events.append(("volume", round(value, 3)))

    def get_mute(self):
        return self.mute


class FakeMedia:
    def __init__(self, app_id="Chrome", playing=True, can_pause=True):
        self.app_id = app_id
        self.playing = playing
        self.supports_pause = can_pause
        self.pauses = self.plays = 0

    def status(self):
        return 4 if self.playing else 5

    def can_pause(self):
        return self.supports_pause

    async def pause(self):
        self.pauses += 1
        self.playing = False
        return True

    async def play(self):
        self.plays += 1
        self.playing = True
        return True


class FakeMaster:
    device_name = "Fake speakers"

    def __init__(self):
        self.volume = 0.2
        self.mute = False
        self.events = []

    def get_volume(self):
        return self.volume

    def set_volume(self, value):
        self.events.append(("master", round(value, 3)))
        self.volume = value

    def get_mute(self):
        return self.mute

    def set_mute(self, value):
        self.mute = value


class ApplicationAudioTests(unittest.TestCase):
    def make_engine(self, apps, media=()):
        async def load_media():
            return list(media)
        return ApplicationAudio(sessions=lambda: list(apps), media=load_media,
                                own_pid=1, sleep=lambda _: None, log=lambda _: None)

    def test_sessions_muted_then_restored_without_setting_mute_flag(self):
        app = FakeApp()
        own = FakeApp(pid=1, session_id="school-intercom", volume=0.5)
        original_muted = FakeApp(name="vlc.exe", pid=22, session_id="muted",
                                 volume=0.6, mute=True)
        engine = self.make_engine([app, own, original_muted])
        saved = []
        record = {}
        def save(r):
            saved.append(deepcopy(r))
        engine.begin(record, save)
        self.assertEqual(app.volume, 0.0)
        self.assertEqual(own.volume, 0.5)
        self.assertEqual(original_muted.volume, 0.6)
        self.assertFalse(app.mute)
        self.assertEqual(saved[0]["apps"][0]["stage"], "pending")
        self.assertTrue(engine.restore(record, save))
        self.assertAlmostEqual(app.volume, 0.7)
        self.assertEqual(record["apps"], [])

    def test_pause_then_play_media_with_volume_at_zero(self):
        app, media = FakeApp(), FakeMedia()
        engine = self.make_engine([app], [media])
        record = {}
        engine.begin(record, lambda _: None)
        self.assertEqual(media.pauses, 1)
        self.assertEqual(app.volume, 0)
        self.assertTrue(engine.restore(record, lambda _: None))
        self.assertEqual(media.plays, 1)
        self.assertAlmostEqual(app.volume, 0.7)

    def test_never_resume_media_we_did_not_pause(self):
        app = FakeApp()
        old_paused = FakeMedia(playing=False)
        engine = self.make_engine([app], [old_paused])
        record = {}
        engine.begin(record, lambda _: None)
        self.assertEqual(record["media"], [])
        self.assertTrue(engine.restore(record, lambda _: None))
        self.assertEqual(old_paused.plays, 0)

    def test_manual_play_is_not_overridden(self):
        app, media = FakeApp(), FakeMedia()
        engine = self.make_engine([app], [media])
        record = {}
        engine.begin(record, lambda _: None)
        media.playing = True  # user pressed play
        self.assertTrue(engine.restore(record, lambda _: None))
        self.assertEqual(media.plays, 0)
        self.assertAlmostEqual(app.volume, 0.7)

    def test_manual_session_volume_change_is_preserved(self):
        app = FakeApp()
        engine = self.make_engine([app])
        record = {}
        engine.begin(record, lambda _: None)
        app.volume = 0.4
        self.assertTrue(engine.restore(record, lambda _: None))
        self.assertEqual(app.volume, 0.4)

    def test_new_session_with_same_pid_not_touched_after_crash(self):
        old = FakeApp()
        record = {}
        self.make_engine([old]).begin(record, lambda _: None)
        new = FakeApp(session_id="new-session", volume=0.3)
        self.assertTrue(self.make_engine([new]).restore(record, lambda _: None))
        self.assertEqual(new.volume, 0.3)

    def test_recovery_same_session_and_media(self):
        app, media = FakeApp(), FakeMedia()
        record = {}
        self.make_engine([app], [media]).begin(record, lambda _: None)
        self.assertTrue(restore_record_apps(record, lambda _: None,
                                           app_audio=self.make_engine([app], [media])))
        self.assertAlmostEqual(app.volume, 0.7)
        self.assertEqual(media.plays, 1)

    def test_single_journal_restores_master_sessions_and_media_after_crash(self):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        app, media = FakeApp(), FakeMedia()
        master = FakeMaster()
        engine = self.make_engine([app], [media])
        with TemporaryDirectory() as folder:
            path = Path(folder) / "volume-recovery.json"
            manager = AnnouncementVolume(
                master, path, target_percent=80, apps=engine,
                sleep=lambda _: None, log=lambda _: None)
            self.assertTrue(manager.begin())
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["stage"], "applied")
            self.assertEqual(len(saved["apps"]), 1)
            self.assertEqual(len(saved["media"]), 1)
            self.assertAlmostEqual(master.volume, 0.8)
            self.assertEqual(app.volume, 0)
            self.assertFalse(media.playing)
            # Προσομοίωση restart: νέο αντικείμενο πάνω στον ΙΔΙΟ journal.
            recovered = AnnouncementVolume(
                master, path, target_percent=80, apps=engine,
                sleep=lambda _: None, log=lambda _: None)
            self.assertTrue(recovered.recover())
            self.assertAlmostEqual(master.volume, 0.2)
            self.assertAlmostEqual(app.volume, 0.7)
            self.assertTrue(media.playing)
            self.assertFalse(path.exists())

    def test_missing_media_backend_keeps_journal(self):
        app, media = FakeApp(), FakeMedia()
        record = {}
        self.make_engine([app], [media]).begin(record, lambda _: None)
        async def unavailable():
            raise OSError("media controls unavailable")
        broken = ApplicationAudio(sessions=lambda: [app], media=unavailable,
                                  own_pid=1, sleep=lambda _: None,
                                  log=lambda _: None)
        self.assertFalse(broken.restore(record, lambda _: None))
        self.assertTrue(record["media"])
        self.assertAlmostEqual(app.volume, 0.7)


if __name__ == "__main__":
    unittest.main()
