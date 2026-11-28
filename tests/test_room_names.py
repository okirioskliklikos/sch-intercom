"""5C2: «C2627-Γ1» → «Γ1» στην τρέχουσα σχολική χρονιά (από 1η Σεπτεμβρίου)."""
from datetime import date
import unittest
from unittest import mock

from shared import room_names
from shared.room_names import room_title, school_year, short_room
from client import status as st

NOV_2026 = date(2026, 11, 28)


class RoomNameTests(unittest.TestCase):
    def test_school_year_starts_first_of_september(self):
        self.assertEqual(school_year(date(2026, 8, 31)), "2526")
        self.assertEqual(school_year(date(2026, 9, 1)), "2627")
        self.assertEqual(school_year(NOV_2026), "2627")
        self.assertEqual(school_year(date(2027, 8, 31)), "2627")
        self.assertEqual(school_year(date(2099, 9, 1)), "9900")

    def test_short_only_for_current_year_pattern(self):
        self.assertEqual(short_room("C2627-Γ1", NOV_2026), "Γ1")
        self.assertEqual(short_room("C2627-Αίθουσα Μουσικής", NOV_2026), "Αίθουσα Μουσικής")
        self.assertEqual(short_room("C2526-Γ1", NOV_2026), "C2526-Γ1")    # πέρσι: ολόκληρο
        self.assertEqual(short_room("Γυμναστήριο", NOV_2026), "Γυμναστήριο")
        self.assertEqual(short_room("C2627-", NOV_2026), "C2627-")
        self.assertEqual(short_room("c2627-Γ1", NOV_2026), "c2627-Γ1")
        self.assertEqual(short_room("", NOV_2026), "")
        self.assertEqual(room_title("C2627-Γ1", "Board1", NOV_2026), "Γ1 · Board1")
        self.assertEqual(room_title("C2627-Γ1", "", NOV_2026), "Γ1")

    def test_client_texts_short_but_change_dialog_full(self):
        with mock.patch.object(room_names, "_today", lambda: NOV_2026):
            status = st.ClientStatus(room="C2627-Γ1", name="Board1")
            status.update(change={"room": "C2627-Γ2", "name": "Board1", "state": "pending"})
            snap = status.snapshot()
            self.assertEqual(st.title(snap), "Γ1 · Board1")
            rows = {row["label"]: row["value"] for row in st.info_rows(snap)}
            self.assertEqual(rows["Αίθουσα"], "Γ1")
            _alarm, info = st.window_bars(snap)
            self.assertIn("<b>Γ2 · Board1</b>", info)
            self.assertEqual(snap["room"], "C2627-Γ1")     # η τιμή μένει πλήρης

    def test_director_texts(self):
        from director.notices import _label
        from director.security import RequestView
        with mock.patch.object(room_names, "_today", lambda: NOV_2026):
            self.assertEqual(_label("C2627-Γ3", "Board1"), "Γ3 · Board1")
            view = RequestView("id", "C2627-Γ2", "PC", 0, 1, "change", "C2627-Γ1", "PC")
            self.assertEqual((view.title, view.old_title), ("Γ2 · PC", "Γ1 · PC"))


if __name__ == "__main__":
    unittest.main()
