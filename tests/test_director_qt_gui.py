"""Παράθυρο PySide6 του Director σε offscreen render (χωρίς οθόνη, δίκτυο, ήχο).

Παραλείπεται αν δεν υπάρχει PySide6 (pip install -e .[gui]).
"""
import os
import unittest
from unittest import TestCase

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication, QLabel
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
        # Το offscreen Qt μπορεί να αναφέρει εικονική οθόνη μόλις 800px.
        # Ελέγχουμε ρητά τη συμφωνημένη οριζόντια διάταξη σε landscape viewport.
        window.resize(1366, 768)
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
        self.assertAlmostEqual(announce.height(), window.rooms_card.height(), delta=2)
        self.assertAlmostEqual(
            announce.mapTo(window, announce.rect().bottomLeft()).y(),
            rooms.mapTo(window, rooms.rect().bottomLeft()).y(), delta=2)
        self.assertLess(
            announce.mapTo(window, announce.rect().bottomLeft()).y(),
            selection.mapTo(window, selection.rect().topLeft()).y())

        # Ο κύκλος διατηρεί την εγκεκριμένη διάμετρο και κεντράρεται.
        button = window.announce_button
        # Λίγος επιπλέον χώρος μόνο για το μεγαλύτερο κύμα «Live».
        self.assertGreaterEqual(button.width(), button.RING + 4)
        self.assertLessEqual(button.width(), button.RING + 4 + current_metrics().px(6))
        self.assertAlmostEqual(button.mapTo(window, button.rect().center()).x(),
                               announce.mapTo(window, announce.rect().center()).x(),
                               delta=3)
        self.assertGreater(announce.width(), button.width())

    def test_meter_top_right_and_mute_near_microphone(self):
        window = self.make(self.STATUS)
        window.show()
        QApplication.processEvents()
        button = window.announce_button
        preview = window.preview_button
        meter = window.meter
        mute = window.mute_button
        hint = window.hint

        self.assertLess(preview.mapTo(window, preview.rect().topLeft()).y(),
                        button.mapTo(window, button.rect().topLeft()).y())
        self.assertGreater(preview.mapTo(window, preview.rect().topLeft()).x(),
                           button.mapTo(window, button.rect().center()).x())
        self.assertLess(meter.mapTo(window, meter.rect().bottomLeft()).y(),
                        button.mapTo(window, button.rect().topLeft()).y())
        gap = (mute.mapTo(window, mute.rect().topLeft()).y() -
               hint.mapTo(window, hint.rect().bottomLeft()).y())
        self.assertGreaterEqual(gap, 0)
        self.assertLess(gap, current_metrics().px(20))
        # Λίγος επιπλέον χώρος μόνο για το μεγαλύτερο κύμα «Live».
        self.assertGreaterEqual(button.width(), button.RING + 4)
        self.assertLessEqual(button.width(), button.RING + 4 + current_metrics().px(6))

    def test_room_title_and_first_row_always_at_top(self):
        window = self.make([("id-1", "Γ1", True, 0.1)])
        window.show()
        QApplication.processEvents()
        window._layout_rooms()
        QApplication.processEvents()
        title = window.rooms_title
        row = window.room_rows["Γ1"]
        card = window.rooms_card
        title_y = title.mapTo(window, title.rect().topLeft()).y()
        row_y = row.mapTo(window, row.rect().topLeft()).y()
        card_y = card.mapTo(window, card.rect().topLeft()).y()
        self.assertGreaterEqual(title_y, card_y)
        self.assertLess(title_y - card_y, current_metrics().px(40))
        self.assertGreater(row_y, title_y)
        self.assertLess(row_y - title_y, current_metrics().px(95))
        self.assertLess(row.mapTo(window, row.rect().topLeft()).x(),
                        card.mapTo(window, card.rect().center()).x())

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

    def test_long_room_name_is_elided_at_status_dot(self):
        long_name = "Εργαστήριο Φυσικής και Χημείας Β΄ τάξης"
        names = [long_name, "Αίθουσα Εικαστικών", "Γραφείο Καθηγητών"]
        window = self.make([(f"id-{i}", n, True, 0.1) for i, n in enumerate(names)])
        window.show()
        QApplication.processEvents()
        row = window.room_rows[long_name]
        self.assertTrue(row.label.text().endswith("…"))
        self.assertTrue(long_name.startswith(row.label.text()[:-1]))
        # Κόβεται βάσει πλάτους: το κείμενο γεμίζει τον χώρο ως την κουκκίδα.
        advance = row.label.fontMetrics().horizontalAdvance(row.label.text())
        self.assertLessEqual(advance, row.label.contentsRect().width())
        self.assertGreater(advance, row.label.contentsRect().width() * 0.8)
        self.assertEqual(row.label.toolTip(), long_name)
        self.assertLessEqual(row.label.x() + row.label.width(), row.dot.x())
        for name in names[1:]:
            with self.subTest(name=name):
                self.assertEqual(window.room_rows[name].label.text(), name)

    def sized(self, count, width, height):
        status = [(f"id-{i}", f"Αίθουσα {i:02d}", True, 0.1) for i in range(count)]
        broadcaster = StatusBroadcaster(status)
        window = DirectorWindow(broadcaster, DirectorMicrophone(FakeStream(), broadcaster))
        window.timer.stop()
        self.addCleanup(release_window, window)
        window.setMinimumSize(100, 100)  # η offscreen «οθόνη» είναι μικρή
        window.resize(width, height)
        window.show()
        for _ in range(3):
            QApplication.processEvents()
            window._layout_rooms()
        QApplication.processEvents()
        return window

    def test_page_scrollbar_styled_when_rooms_stacked(self):
        window = self.sized(60, 1000, 600)
        self.assertTrue(window.stacked)
        bar = window.page_scroll.verticalScrollBar()
        self.assertTrue(bar.isVisible())
        self.assertGreater(bar.maximum(), 0)
        self.assertEqual(bar.width(), current_metrics().px(theme.SCROLLBAR))
        # Οι αίθουσες δεν έχουν δική τους κύλιση στη διάταξη «η μία κάτω από την άλλη».
        self.assertFalse(window.room_scroll.verticalScrollBar().isVisible())
        qss = theme.stylesheet(current_metrics())
        self.assertIn("QScrollBar::handle:vertical", qss)
        self.assertIn(theme.PRIMARY_DARK, qss)

    def test_third_column_opens_and_announcement_narrows(self):
        m = current_metrics()
        window = self.sized(30, m.px(1350), m.px(640))
        self.assertFalse(window.stacked)
        self.assertGreaterEqual(window._room_columns, 3)
        bar = window.room_scroll.verticalScrollBar()
        self.assertFalse(bar.isVisible())         # χωρά σε στήλες, χωρίς κύλιση
        self.assertGreaterEqual(window.announcement_card.width(),
                                m.px(theme.ANNOUNCE_MIN_WIDTH))

        window = self.make(self.STATUS)
        window.show()
        QApplication.processEvents()
        width = current_metrics().px(theme.ROOM_COLUMN_WIDTH)
        self.assertEqual({r.width() for r in window.room_rows.values()}, {width})

    def assert_not_clipped(self, window):
        page = window.page
        announce = window.announcement_card
        self.assertGreaterEqual(announce.width(), announce.minimumSizeHint().width())
        body = window.page_scroll.widget()
        self.assertLessEqual(body.width(), window.page_scroll.viewport().width())
        for card in (announce, window.rooms_card, window.selection_card):
            right = card.mapTo(page, card.rect().topRight()).x()
            self.assertLessEqual(right, page.width())
        grid = window.room_grid_widget
        self.assertLessEqual(grid.mapTo(window.rooms_card, grid.rect().topRight()).x(),
                             window.rooms_card.width())

    def test_rooms_go_below_announcement_when_they_do_not_fit(self):
        # Πραγματική χρήση 22/11/2026: με 2-3 στήλες αιθουσών οι κάρτες κόβονταν δεξιά.
        # Απόφαση χρήστη: τότε «Ανακοίνωση» πάνω, «Αίθουσες» και «Επιλογή» από κάτω.
        for width in (1000, 1200, 1366):
            for count in (17, 30, 40):
                with self.subTest(width=width, rooms=count):
                    window = self.sized(count, width, 700)
                    self.assert_not_clipped(window)
                    announce = window.announcement_card
                    rooms = window.rooms_card
                    selection = window.selection_card
                    page = window.page
                    if window.stacked:
                        self.assertLessEqual(
                            announce.mapTo(page, announce.rect().bottomLeft()).y(),
                            rooms.mapTo(page, rooms.rect().topLeft()).y())
                        self.assertLessEqual(
                            rooms.mapTo(page, rooms.rect().bottomLeft()).y(),
                            selection.mapTo(page, selection.rect().topLeft()).y())
                        # Όλες οι αίθουσες ορατές με κύλιση σελίδας, όχι μέσα στην κάρτα.
                        self.assertFalse(window.room_scroll.verticalScrollBar().isVisible())
                        self.assertEqual(window.room_scroll.height(),
                                         window.room_grid_widget.height())
                    else:
                        self.assertLess(
                            announce.mapTo(page, announce.rect().topRight()).x(),
                            rooms.mapTo(page, rooms.rect().topLeft()).x())
                        self.assertFalse(window.room_scroll.verticalScrollBar().isVisible())
                    window.hide()
        # 1366×700: 17 αίθουσες χωρούν δίπλα-δίπλα, 40 όχι.
        self.assertFalse(self.sized(17, 1366, 700).stacked)
        self.assertTrue(self.sized(40, 1366, 700).stacked)

    def test_layout_switches_back_and_forth_with_window_size(self):
        window = self.sized(30, 1700, 900)
        self.assertFalse(window.stacked)
        for width, stacked in ((1100, True), (1700, False), (1100, True)):
            window.resize(width, 900)
            for _ in range(3):
                QApplication.processEvents()
                window._layout_rooms()
            with self.subTest(width=width):
                self.assertEqual(window.stacked, stacked)
                self.assert_not_clipped(window)

    def test_preview_label_never_clipped(self):
        window = self.make(self.STATUS)
        window.show()
        QApplication.processEvents()
        labels = [w for w in window.announcement_card.findChildren(QLabel)
                  if w.text() == "Στάθμη μικροφώνου"]
        self.assertEqual(len(labels), 1)
        self.assertGreaterEqual(labels[0].width(), labels[0].sizeHint().width())

    def test_start_and_stop_from_round_button(self):
        window = self.make(self.STATUS)
        window.announce_button.click()
        self.assertTrue(self.mic.live)
        self.assertTrue(window.announce_button.live)
        self.assertEqual(self.broadcaster.targets[-1], (None, None))
        self.assertEqual(window.hint.text(), HINT_LIVE)
        self.assertNotIn("ΖΩΝΤΑΝΑ", window.hint.text())
        self.assertTrue(window.live_badge.isVisibleTo(window))
        self.assertEqual(window.live_label.text(), "Live")
        self.assertFalse(window.radio_rooms.isEnabled())
        self.assertFalse(window.room_rows["Γ1"].checkbox.isEnabled())
        self.assertFalse(window.preview_button.isEnabled())
        window.announce_button.click()
        self.assertFalse(self.mic.live)
        self.assertEqual(self.broadcaster.targets[-1], (set(), set()))
        self.assertEqual(window.hint.text(), HINT_IDLE)
        self.assertFalse(window.live_badge.isVisibleTo(window))

    def test_live_badge_keeps_circle_in_place(self):
        window = self.make(self.STATUS)
        window.show()
        QApplication.processEvents()
        button = window.announce_button
        before = button.mapTo(window, button.rect().topLeft())
        self.assertFalse(window.live_badge.isVisible())
        window.announce_button.click()
        QApplication.processEvents()
        self.assertTrue(window.live_badge.isVisible())
        self.assertEqual(button.mapTo(window, button.rect().topLeft()), before)
        badge = window.live_badge
        self.assertLess(badge.mapTo(window, badge.rect().bottomLeft()).y(), before.y() + 1)
        self.assertEqual(window.live_label.font().weight(), QFont.Weight.DemiBold)

    def settle(self, button):
        """Περιμένει να τελειώσει η μετάβαση δαχτυλιδιού ↔ κυμάτων."""
        wait_until(lambda: (QApplication.processEvents() or True)
                   and button._mix_anim.state() != button._mix_anim.State.Running)
        QApplication.processEvents()

    def test_live_waves_replace_ring_and_stop_on_end(self):
        window = self.make(self.STATUS)
        window.show()
        button = window.announce_button
        self.assertFalse(button.waves_running())
        self.assertEqual(button.waves_mix, 0.0)
        window.announce_button.click()
        self.assertTrue(button.waves_running())
        self.settle(button)
        self.assertEqual(button.waves_mix, 1.0)     # σταθερό δαχτυλίδι κρυμμένο
        window.announce_button.click()
        self.assertTrue(button.waves_running())     # ομαλή επιστροφή δαχτυλιδιού
        self.settle(button)
        self.assertEqual(button.waves_mix, 0.0)
        self.assertFalse(button.waves_running())

    def test_mute_during_live_stops_waves(self):
        window = self.make(self.STATUS)
        window.show()
        button = window.announce_button
        window.announce_button.click()
        self.settle(button)
        window.mute_button.click()
        self.settle(button)
        self.assertFalse(button.waves_running())
        window.mute_button.click()
        self.settle(button)
        self.assertTrue(button.waves_running())
        window.announce_button.click()

    def test_wave_geometry(self):
        window = self.make(self.STATUS)
        button = window.announce_button
        self.assertEqual(len(button.wave_states(0)), 1)
        self.assertEqual(len(button.wave_states(theme.WAVE_DELAY_MS * 2)), 3)
        radius, opacity = button.wave_states(0)[0]
        self.assertAlmostEqual(radius, button.CIRCLE / 2)
        self.assertAlmostEqual(opacity, theme.WAVE_OPACITY)
        radius, opacity = button.wave_states(theme.WAVE_PERIOD_MS - 1)[0]
        self.assertAlmostEqual(radius, button.CIRCLE * theme.WAVE_SCALE / 2, delta=0.1)
        self.assertLess(opacity, 0.01)
        # Το μεγαλύτερο κύμα χωρά στο widget.
        self.assertLessEqual(button.CIRCLE * theme.WAVE_SCALE + button.WAVE_PEN,
                             button.width())

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
        self.assertTrue(window.announce_button.muted)   # μικρόφωνο με γραμμή
        self.assertTrue(window.live_badge.isVisibleTo(window))
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
