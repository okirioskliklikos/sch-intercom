"""Κλίμακα περιβάλλοντος (Ρυθμίσεις → Εμφάνιση, απόφαση χρήστη 24/11/2026).

Ποσοστό 80/90/100/110/125% που πολλαπλασιάζει τη βάση γραμματοσειράς (και άρα
αποστάσεις και εικονίδια), αποθήκευση στο director-gui.json, προεπιλογή 90% στα
Windows και 100% αλλού, εφαρμογή με επανεκκίνηση (όχι σε ζωντανή ανακοίνωση).
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from shared.ui import theme

try:
    from shared.ui.offscreen import use_offscreen
    use_offscreen()
    from director.gui import DirectorMicrophone
    from director import qt_gui
    from director.qt_gui import (DirectorWindow, create_application, load_layout_settings,
                                 load_ui_scale, release_window, save_layout_settings)
    from director import qt_maintenance as qm
    from tests.test_maintenance import Manager, FakeBroadcaster, fake_stream
    HAVE_QT = True
except ImportError:  # χωρίς PySide6
    HAVE_QT = False


class ScaleMetricsTests(unittest.TestCase):
    def test_scale_multiplies_base_after_minimum(self):
        # Windows: Segoe UI 9pt → ελάχιστο 12pt → 90% = 10,8pt.
        self.assertAlmostEqual(theme.metrics_for(9, 90).base_pt, 10.8)
        self.assertAlmostEqual(theme.metrics_for(9).base_pt, 12.0)
        self.assertAlmostEqual(theme.metrics_for(None, 80).base_pt, 9.6)
        self.assertAlmostEqual(theme.metrics_for(14, 125).base_pt, 17.5)

    def test_sizes_scale_together(self):
        full, small = theme.metrics_for(9, 100), theme.metrics_for(9, 80)
        self.assertEqual(full.px(100), 123)
        self.assertEqual(small.px(100), 98)
        self.assertLess(small.px(theme.ICON), full.px(theme.ICON))
        self.assertLess(small.font_pt(theme.FONT_PAGE_TITLE), full.font_pt(theme.FONT_PAGE_TITLE))

    def test_default_is_90_on_windows_and_100_elsewhere(self):
        self.assertEqual(theme.default_ui_scale("win32"), 90)
        self.assertEqual(theme.default_ui_scale("linux"), 100)
        self.assertEqual(theme.default_ui_scale("darwin"), 100)
        self.assertIn(theme.default_ui_scale(), theme.UI_SCALES)
        self.assertEqual(theme.UI_SCALES, (70, 80, 90, 100, 110, 125, 135))


@unittest.skipUnless(HAVE_QT, "χρειάζεται PySide6")
class ScaleSettingsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "director-gui.json"

    def test_missing_file_gives_platform_default(self):
        self.assertEqual(load_ui_scale(self.path), theme.default_ui_scale())
        self.assertEqual(load_ui_scale(None), theme.default_ui_scale())

    def test_saved_scale_is_read_back_and_keeps_other_keys(self):
        save_layout_settings(self.path, {"rooms_extra_width": 40.0})
        save_layout_settings(self.path, {"ui_scale": 80})
        self.assertEqual(load_ui_scale(self.path), 80)
        self.assertEqual(load_layout_settings(self.path)["rooms_extra_width"], 40.0)
        save_layout_settings(self.path, {"security_left_width": 400.0})
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["ui_scale"], 80)

    def test_invalid_values_fall_back_to_default(self):
        for value in (85, "90", 90.0, True, None, 200):
            self.path.write_text(json.dumps({"version": 1, "ui_scale": value}),
                                 encoding="utf-8")
            self.assertEqual(load_ui_scale(self.path), theme.default_ui_scale(), value)

    def test_gui_settings_path_next_to_registry(self):
        args = mock.Mock(registry_file=Path("/x/.school-intercom/clients-registry.json"))
        self.assertEqual(qt_gui.gui_settings_path(args),
                         Path("/x/.school-intercom/director-gui.json"))


@unittest.skipUnless(HAVE_QT, "χρειάζεται PySide6")
class ScaleApplicationTests(unittest.TestCase):
    def tearDown(self):
        create_application(base_pt=12)   # τα υπόλοιπα tests περιμένουν 100%

    def test_create_application_applies_scale(self):
        app = create_application(base_pt=12, ui_scale=90)
        self.assertEqual(qt_gui.current_ui_scale(), 90)
        self.assertAlmostEqual(qt_gui.current_metrics().base_pt, 10.8)
        self.assertAlmostEqual(app.font().pointSizeF(), 10.8, places=1)
        self.assertEqual(qt_gui.px(100), 111)
        create_application(base_pt=12)
        self.assertEqual(qt_gui.current_ui_scale(), 100)
        self.assertEqual(qt_gui.px(100), 123)


@unittest.skipUnless(HAVE_QT, "χρειάζεται PySide6")
class AppearanceTabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_application(base_pt=12)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.manager = Manager(self.root / "pc")
        self.settings = self.root / "director-gui.json"
        save_layout_settings(self.settings, {"ui_scale": 100})   # = εφαρμοσμένη κλίμακα
        self.restarts: list[str] = []
        broadcaster = FakeBroadcaster()
        self.mic = DirectorMicrophone(fake_stream, broadcaster)
        self.window = DirectorWindow(
            broadcaster, self.mic, maintenance=self.manager.model,
            maintenance_options={"restart": lambda: self.restarts.append("restart"),
                                 "layout_settings": self.settings})
        self.window.resize(1366, 900)
        self.window.show()
        self.window.show_page("settings")
        self.page = self.window.settings_page
        self.settle()

    def tearDown(self):
        self.window.close()
        release_window(self.window)
        self.app.processEvents()

    def settle(self):
        for _ in range(4):
            self.app.processEvents()

    def test_settings_open_on_appearance_tab_before_maintenance(self):
        self.assertEqual(self.page.current_tab, qm.TAB_APPEARANCE)
        self.assertTrue(self.page.tab_appearance.isChecked())
        self.assertFalse(self.page.tab_maintenance.isChecked())
        self.assertTrue(self.page.scale_card.isVisible())
        self.assertLess(self.page.tab_appearance.x(), self.page.tab_maintenance.x())
        self.page.tab_maintenance.click()
        self.assertEqual(self.page.current_tab, qm.TAB_MAINTENANCE)
        self.assertTrue(self.page.backup_card.isVisible())
        self.assertFalse(self.page.scale_card.isVisible())

    def test_options_show_current_scale_without_restart_note(self):
        self.assertEqual([b.text() for b in self.page.scale_buttons.values()],
                         ["70%", "80%", "90%", "100%", "110%", "125%", "135%"])
        self.assertTrue(self.page.scale_buttons[100].isChecked())
        self.assertFalse(self.page.scale_note_row.isVisible())

    def test_choosing_scale_saves_and_offers_restart(self):
        self.page.scale_buttons[90].click()
        self.settle()
        self.assertEqual(load_ui_scale(self.settings), 90)
        self.assertTrue(self.page.scale_buttons[90].isChecked())
        self.assertFalse(self.page.scale_buttons[100].isChecked())
        self.assertTrue(self.page.scale_note.isVisible())
        self.assertEqual(self.page.scale_note.text(), qm.SCALE_NOTE)
        self.assertTrue(self.page.restart_button.isVisible())
        # Επιστροφή στην τρέχουσα: χωρίς σημείωση, αποθηκεύεται κι αυτή.
        self.page.scale_buttons[100].click()
        self.settle()
        self.assertFalse(self.page.scale_note_row.isVisible())
        self.assertEqual(load_ui_scale(self.settings), 100)

    def test_restart_now_restarts_manager(self):
        self.page.scale_buttons[80].click()
        self.page.restart_button.click()
        self.assertEqual(self.restarts, ["restart"])

    def test_no_restart_during_live_announcement(self):
        self.page.scale_buttons[80].click()
        with mock.patch.object(type(self.mic), "live", new_callable=mock.PropertyMock,
                               return_value=True):
            self.window.refresh()
            self.assertFalse(self.page.restart_button.isEnabled())
            self.page.restart_button.click()
            self.window.restart_for_scale()
        self.assertEqual(self.restarts, [])
        self.window.refresh()
        self.assertTrue(self.page.restart_button.isEnabled())
        self.page.restart_button.click()
        self.assertEqual(self.restarts, ["restart"])

    def test_registry_import_switches_to_maintenance_tab(self):
        self.page.dialog_hook = lambda dialog: dialog.reject()
        self.window.open_registry_import()
        self.assertEqual(self.page.current_tab, qm.TAB_MAINTENANCE)

    def test_scale_card_respects_scroll_gap(self):
        body = self.page.appearance_scroll.widget()
        self.assertEqual(body.layout().contentsMargins().right(), qt_gui.px(theme.SCROLL_GAP))


if __name__ == "__main__":
    unittest.main()
