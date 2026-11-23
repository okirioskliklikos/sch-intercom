"""Ρύθμιση offscreen Qt: στα Windows και φάκελος γραμματοσειρών (χωρίς Qt)."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from shared.ui import offscreen


class OffscreenTests(unittest.TestCase):
    def test_windows_points_qt_to_system_fonts(self):
        with tempfile.TemporaryDirectory() as windir:
            (Path(windir) / "Fonts").mkdir()
            with mock.patch.dict(os.environ, {"WINDIR": windir}, clear=True):
                offscreen.use_offscreen(windows=True)
                self.assertEqual(os.environ["QT_QPA_PLATFORM"], "offscreen")
                self.assertEqual(os.environ["QT_QPA_FONTDIR"], os.path.join(windir, "Fonts"))

    def test_user_choices_are_kept(self):
        with tempfile.TemporaryDirectory() as windir:
            (Path(windir) / "Fonts").mkdir()
            env = {"WINDIR": windir, "QT_QPA_PLATFORM": "windows"}
            with mock.patch.dict(os.environ, env, clear=True):
                offscreen.use_offscreen(windows=True)
                self.assertEqual(os.environ["QT_QPA_PLATFORM"], "windows")
                self.assertNotIn("QT_QPA_FONTDIR", os.environ)
            env = {"WINDIR": windir, "QT_QPA_FONTDIR": "C:/custom"}
            with mock.patch.dict(os.environ, env, clear=True):
                offscreen.use_offscreen(windows=True)
                self.assertEqual(os.environ["QT_QPA_FONTDIR"], "C:/custom")

    def test_other_systems_unchanged(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            offscreen.use_offscreen(windows=False)
            self.assertEqual(os.environ["QT_QPA_PLATFORM"], "offscreen")
            self.assertNotIn("QT_QPA_FONTDIR", os.environ)

    def test_missing_windows_fonts_folder(self):
        with mock.patch.dict(os.environ, {"WINDIR": "/nonexistent-windir"}, clear=True):
            self.assertIsNone(offscreen.windows_fonts_dir())


if __name__ == "__main__":
    unittest.main()
