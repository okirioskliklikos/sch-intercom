"""Παράθυρο PySide6 του Director σε offscreen render (χωρίς οθόνη, δίκτυο, ήχο).

Παραλείπεται αν δεν υπάρχει PySide6 (pip install -e .[gui]).
"""
import os
import unittest
from unittest import TestCase

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    from director.qt_gui import (HINT_IDLE, HINT_LIVE, HINT_LIVE_MUTED, HINT_MUTED,
                                 DirectorWindow, create_application, current_metrics,
                                 release_window)
except ImportError as exc:  # pragma: no cover - εξαρτάται από το περιβάλλον
    QT_ERROR = str(exc)
else:
    QT_ERROR = None

from director.gui import MODE_ALL, MODE_ROOMS, DirectorMicrophone
from shared.ui import theme
from tests.test_director_microphone import FakeBroadcaster, FakeStream, wait_until


class StatusBroadcaster(FakeBroadcaster):
    def __init__(self, status):
        super().__init__()
        self.status = status

    def status_snapshot(self):
        return list(self.status)


@unittest.skipIf(QT_ERROR is not None, f"PySide6 μη διαθέσιμο: {QT_ERROR}")
class DirectorWindowTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_application()

    def make(self, status, stream=None):
        self.broadcaster = StatusBroadcaster(status)
        self.stream = stream or FakeStream()
        self.mic = DirectorMicrophone(self.stream, self.broadcaster)
        self.window = DirectorWindow(self.broadcaster, self.mic)
        self.window.timer.stop()  # οι έλεγχοι καλούν refresh() ρητά
        self.addCleanup(self.close)
        return self.window

    def close(self):
        self.window.close()
        release_window(self.window)

    STATUS = [("id-1", "Γ1", True, 0.1), ("id-2", "Γ2", False, 8.0),
              ("id-3", "Α1", True, 0.2), ("id-4", "Α1", False, 9.0)]

    def test_idle_layout_and_safe_state(self):
        window = self.make(self.STATUS)
        self.assertEqual(window.windowTitle(), "School Intercom — Διεύθυνση")
        self.assertEqual(self.broadcaster.targets, [(set(), set())])
        self.assertFalse(self.mic.device_open)
        self.assertEqual(window.online_count.text(), "(2/3 online)")
        self.assertEqual(window.radio_all.text(), "Όλες οι αίθουσες (2)")
        self.assertEqual(list(window.room_rows), ["Α1", "Γ1", "Γ2"])
        self.assertEqual(window.room_rows["Γ2"].dot.color, theme.ROOM_DOT_OFFLINE)
        self.assertEqual(window.room_rows["Α1"].dot.color, theme.ROOM_DOT_ONLINE)
        self.assertTrue(all(row.checkbox.isChecked() for row in window.room_rows.values()))
        self.assertEqual(window.hint.text(), HINT_IDLE)
        self.assertTrue(window.nav_buttons["home"].isChecked())
        self.assertFalse(any(b.isEnabled() for k, b in window.nav_buttons.items() if k != "home"))
        self.assertFalse(window.radio_group.isEnabled())

    def positions(self, window):
        return {room: window.room_grid.getItemPosition(window.room_grid.indexOf(row))[:2]
                for room, row in window.room_rows.items()}

    def test_landscape_layout_expands_announcement_and_stacks_right_cards(self):
        window = self.make(self.STATUS)
        window.show()
        QApplication.processEvents()
        window._layout_rooms()
        QApplication.processEvents()

        announce = window.announcement_card
        rooms = window.rooms_card
        selection = window.selection_card
        self.assertLess(announce.mapTo(window, announce.rect().topRight()).x(),
                        rooms.mapTo(window, rooms.rect().topLeft()).x())
        self.assertLess(rooms.mapTo(window, rooms.rect().bottomLeft()).y(),
                        selection.mapTo(window, selection.rect().topLeft()).y())
        self.assertAlmostEqual(announce.height(), window.rooms_area.height(), delta=2)
        self.assertAlmostEqual(
            announce.mapTo(window, announce.rect().bottomLeft()).y(),
            selection.mapTo(window, selection.rect().bottomLeft()).y(), delta=2)

        # Ο κύκλος διατηρεί την εγκεκριμένη διάμετρο και κεντράρεται.
        button = window.announce_button
        self.assertEqual(button.width(), button.RING + 4)
        self.assertAlmostEqual(button.mapTo(window, button.rect().center()).x(),
                               announce.mapTo(window, announce.rect().center()).x(),
                               delta=3)
        self.assertGreater(announce.width(), button.width())

    def test_status_dot_at_far_right_even_with_short_room_name(self):
        window = self.make([("id-1", "Γ1", True, 0.1)])
        window.show()
        QApplication.processEvents()
        row = window.room_rows["Γ1"]
        self.assertGreater(row.dot.x() - (row.label.x() + row.label.width()), 8)
        self.assertLessEqual(row.width() - (row.dot.x() + row.dot.width()),
                             current_metrics().px(10))

    def test_first_column_fills_before_second(self):
        status = [(f"id-{i}", f"Αίθουσα {i:02d}", True, 0.1) for i in range(12)]
        window = self.make(status)
        row = current_metrics().px(theme.ROW_HEIGHT)
        window.show()
        for height in (row * 30, row * 9):
            window.rooms_area.resize(window.rooms_area.width(), height)
            QApplication.processEvents()
            window._layout_rooms()
            rows_fit = window._room_rows_per_column
            with self.subTest(rows_per_column=rows_fit):
                columns = {pos[1] for pos in self.positions(window).values()}
                if rows_fit >= 12:
                    self.assertEqual(columns, {0})
                else:
                    self.assertEqual(columns, {0, 1})
                    self.assertEqual(self.positions(window)["Αίθουσα 00"], (0, 0))
                    self.assertEqual(self.positions(window)[f"Αίθουσα {rows_fit:02d}"], (0, 1))
        widths = {r.width() for r in window.room_rows.values()}
        self.assertEqual(len(widths), 1)  # ίσες στήλες

    def test_long_room_name_is_cut_after_18_characters(self):
        long_name = "Εργαστήριο Φυσικής Β΄ τάξης"
        window = self.make([("id-1", long_name, True, 0.1)])
        label = window.room_rows[long_name].label
        self.assertEqual(label.text(), "Εργαστήριο Φυσικής…")
        self.assertEqual(label.toolTip(), long_name)

    def test_start_and_stop_from_round_button(self):
        window = self.make(self.STATUS)
        window.announce_button.click()
        self.assertTrue(self.mic.live)
        self.assertTrue(window.announce_button.live)
        self.assertEqual(self.broadcaster.targets[-1], (None, None))
        self.assertEqual(window.hint.text(), HINT_LIVE)
        self.assertEqual(window.hint.property("state"), "live")
        self.assertFalse(window.radio_rooms.isEnabled())
        self.assertFalse(window.room_rows["Γ1"].checkbox.isEnabled())
        self.assertFalse(window.preview_button.isEnabled())
        window.announce_button.click()
        self.assertFalse(self.mic.live)
        self.assertEqual(self.broadcaster.targets[-1], (set(), set()))
        self.assertEqual(window.hint.text(), HINT_IDLE)

    def test_unchecking_room_switches_to_specific_rooms(self):
        window = self.make(self.STATUS)
        window.room_rows["Α1"].checkbox.click()
        self.assertEqual(window.mode, MODE_ROOMS)
        self.assertTrue(window.radio_rooms.isChecked())
        self.assertEqual(window.checked, {"Γ1", "Γ2"})
        window.announce_button.click()
        self.assertEqual(self.broadcaster.targets[-1], ({"Γ1", "Γ2"}, set()))
        window.announce_button.click()
        window.radio_all.click()
        self.assertEqual(window.mode, MODE_ALL)

    def test_no_online_client_shows_notice_without_audio(self):
        window = self.make([("id-2", "Γ2", False, 8.0)])
        window.announce_button.click()
        self.assertFalse(self.mic.live)
        self.assertFalse(self.mic.device_open)
        self.assertEqual(window.hint.text(), "Δεν υπάρχει συνδεδεμένος Client.")
        self.assertEqual(window.hint.property("state"), "error")

    def test_mute_states(self):
        window = self.make(self.STATUS)
        window.mute_button.click()
        self.assertTrue(self.mic.muted)
        self.assertFalse(window.announce_button.isEnabled())
        self.assertFalse(window.preview_button.isEnabled())
        self.assertEqual(window.hint.text(), HINT_MUTED)
        self.assertEqual(window.mute_button.text(), "Άρση σίγασης")
        window.mute_button.click()
        window.announce_button.click()
        window.mute_button.click()
        self.assertTrue(self.mic.live)
        self.assertTrue(window.announce_button.isEnabled())  # λήξη πάντα διαθέσιμη
        self.assertEqual(window.hint.text(), HINT_LIVE_MUTED)
        window.announce_button.click()
        self.assertFalse(self.mic.live)
        self.assertFalse(window.announce_button.isEnabled())

    def test_preview_toggle(self):
        window = self.make(self.STATUS)
        window.preview_button.click()
        self.assertTrue(self.mic.preview)
        self.assertTrue(self.stream.opened.wait(2))
        self.stream.level_callback(1.0)
        self.assertTrue(wait_until(lambda: self.mic.level() > 0))
        window.refresh()
        self.assertEqual(window.meter.lit_segments(), window.meter.SEGMENTS)
        self.assertEqual(self.broadcaster.published, [])
        window.preview_button.click()
        self.assertFalse(self.mic.device_open)
        window.refresh()
        self.assertEqual(window.meter.lit_segments(), 0)

    def test_microphone_error_stops_announcement_and_is_shown(self):
        stream = FakeStream()
        stream.fail_with = OSError("Δεν βρέθηκε συσκευή")
        window = self.make(self.STATUS, stream)
        window.announce_button.click()
        self.assertTrue(stream.closed.wait(2))
        self.assertTrue(wait_until(lambda: (window.refresh(), not self.mic.live)[1]))
        self.assertEqual(window.hint.text(), "Σφάλμα μικροφώνου: Δεν βρέθηκε συσκευή")
        self.assertEqual(self.broadcaster.targets[-1], (set(), set()))
        self.assertFalse(window.announce_button.live)

    def test_empty_room_list_and_connection_state(self):
        window = self.make([])
        self.assertTrue(window.empty_rooms.isVisibleTo(window))
        self.assertFalse(window.room_scroll.isVisibleTo(window))
        window.server_up = lambda: False
        window.refresh()
        self.assertEqual(window.connection_label.text(), "Εκτός λειτουργίας")

    def test_sizes_follow_font_metrics(self):
        window = self.make(self.STATUS)
        m = current_metrics()
        self.assertGreaterEqual(m.base_pt, theme.FONT_MIN_PT)
        self.assertEqual(window.announce_button.RING, m.px(theme.ANNOUNCE_RING))
        self.assertEqual(window.room_rows["Α1"].height(), m.px(theme.ROW_HEIGHT))
        self.assertEqual(QApplication.font().pointSizeF(), m.font_pt())
        available = window.screen().availableGeometry().size()
        self.assertLessEqual(window.minimumWidth(), available.width())
        self.assertLessEqual(window.minimumHeight(), available.height())

    def test_offscreen_render(self):
        window = self.make(self.STATUS)
        window.show()
        QApplication.processEvents()
        image = window.grab().toImage()
        self.assertFalse(image.isNull())
        self.assertGreaterEqual(image.width(), window.minimumWidth())
        self.assertGreaterEqual(image.height(), window.minimumHeight())


if __name__ == "__main__":
    unittest.main()
