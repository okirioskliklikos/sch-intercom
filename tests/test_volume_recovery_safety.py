"""Το αρχείο ανάκαμψης έντασης δεν πρέπει ποτέ να εμποδίζει τον Client.

Ψεύτικη έξοδος, χωρίς Windows. Αφορά το create_volume_controller και το
recover_pending_volume που καλεί ο Client σε κάθε σύνδεση.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from client.windows_volume import (AnnouncementVolume, create_volume_controller,
                                   recover_pending_volume)


class FakeMaster:
    def __init__(self, name="Speakers", volume=0.2):
        self.device_name = name
        self.volume = volume
        self.mute = False

    def get_volume(self):
        return self.volume

    def set_volume(self, value):
        self.volume = value

    def get_mute(self):
        return self.mute

    def set_mute(self, value):
        self.mute = value


class RecoverySafetyTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "volume-recovery.json"
        self.logs: list[str] = []

    def leave_journal(self, device="Speakers", volume=0.2, target=0.8):
        """Προσομοίωση crash μετά την εφαρμογή της έντασης ανακοίνωσης."""
        master = FakeMaster(device, volume)
        AnnouncementVolume(master, self.path, target_percent=round(target * 100),
                           sleep=lambda _: None, log=self.logs.append).begin()
        self.assertTrue(self.path.exists())

    def test_startup_recovery_on_other_device_warns_and_continues(self):
        self.leave_journal(device="Headphones")
        speakers = FakeMaster("Speakers", 0.5)
        recover_pending_volume(self.path, device=None, master_factory=lambda: speakers,
                               log=self.logs.append)  # δεν εγείρει
        self.assertEqual(speakers.volume, 0.5)  # η άλλη έξοδος δεν αγγίζεται
        self.assertTrue(self.path.exists())  # κρατιέται για την επόμενη φορά
        self.assertIn("συνεχίζει κανονικά", self.logs[-1])

    def test_startup_recovery_restores_same_device(self):
        self.leave_journal()
        speakers = FakeMaster("Speakers", 0.8)
        recover_pending_volume(self.path, device=None, master_factory=lambda: speakers,
                               log=self.logs.append)
        self.assertAlmostEqual(speakers.volume, 0.2)
        self.assertFalse(self.path.exists())

    def test_startup_recovery_with_device_option_does_not_raise(self):
        self.leave_journal()
        recover_pending_volume(self.path, device=3, master_factory=FakeMaster,
                               log=self.logs.append)
        self.assertTrue(self.path.exists())
        self.assertIn("--device", self.logs[-1])

    def test_corrupt_journal_does_not_raise(self):
        self.path.write_text("{not json", encoding="utf-8")
        recover_pending_volume(self.path, device=None, master_factory=FakeMaster,
                               log=self.logs.append)
        self.assertIn("συνεχίζει κανονικά", self.logs[-1])
        controller = create_volume_controller(80, device=None, recovery_path=self.path,
                                              master_factory=FakeMaster, log=self.logs.append)
        self.assertIsNone(controller)

    def test_failing_audio_backend_does_not_raise(self):
        def broken():
            raise OSError("no output device")

        self.leave_journal()
        recover_pending_volume(self.path, device=None, master_factory=broken,
                               log=self.logs.append)
        self.assertIsNone(create_volume_controller(80, device=None, recovery_path=self.path,
                                                   master_factory=broken, log=self.logs.append))

    def test_controller_on_other_device_stays_inactive(self):
        self.leave_journal(device="Headphones")
        speakers = FakeMaster("Speakers", 0.3)
        controller = create_volume_controller(80, device=None, recovery_path=self.path,
                                              master_factory=lambda: speakers,
                                              log=self.logs.append)
        self.assertIsNotNone(controller)
        self.assertFalse(controller.begin())  # δεν αλλάζει ένταση όσο εκκρεμεί ανάκαμψη
        self.assertEqual(speakers.volume, 0.3)

    def test_controller_invalid_options_return_none(self):
        self.assertIsNone(create_volume_controller(120, device=None, recovery_path=self.path,
                                                   master_factory=FakeMaster,
                                                   log=self.logs.append))
        self.assertIsNone(create_volume_controller(80, device=2, recovery_path=self.path,
                                                   master_factory=FakeMaster,
                                                   log=self.logs.append))

    def test_no_journal_is_silent(self):
        recover_pending_volume(self.path, device=None, master_factory=FakeMaster,
                               log=self.logs.append)
        self.assertEqual(self.logs, [])


if __name__ == "__main__":
    unittest.main()
