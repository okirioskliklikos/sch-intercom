"""4.3C2: ειδοποιήσεις «γεμάτος» Manager και ίδια ταυτότητα σε 2 υπολογιστές στο GUI."""
from __future__ import annotations

import unittest

from shared.ui.offscreen import use_offscreen

use_offscreen()

from director.broadcast import AudioBroadcaster, MAX_CLIENTS
from director.notices import (DUPLICATE_COMMAND, TEXT_DUPLICATE_STATE, TEXT_SHOW,
                              duplicate_html, duplicate_tooltip, full_html)
from director.security import ComputerView
from tests.test_security_page import FakeBroadcaster, SecurityFixture, _MicBroadcaster

try:
    from director.qt_gui import DirectorWindow, create_application, release_window
    from director.qt_security import TAB_COMPUTERS
except ImportError as exc:   # χωρίς PySide6
    QT_ERROR = str(exc)
else:
    QT_ERROR = None

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"


class TextTests(unittest.TestCase):
    def test_full_lists_room_and_name_like_mockup(self):
        text = full_html([(A, "C2627-Γ3", "Board1"), (B, "C2627-Δ2", "PC")], 70)
        self.assertEqual(text, "Συμπληρώθηκε το όριο των <b>70</b> υπολογιστών. Δεν συνδέονται: "
                               "<b>C2627-Γ3 · Board1</b>, <b>C2627-Δ2 · PC</b>.")
        self.assertIsNone(full_html([], 70))

    def test_full_names_at_most_three(self):
        waiting = [(f"id{i}", f"Γ{i}", "PC") for i in range(5)]
        text = full_html(waiting, 70)
        self.assertIn("<b>Γ2 · PC</b> και 2 ακόμη.", text)
        self.assertNotIn("Γ3", text)

    def test_full_without_name_shows_room_and_escapes(self):
        self.assertIn("<b>Γ1&lt;x&gt;</b>", full_html([(A, "Γ1<x>", "")], 70))

    def test_duplicate_has_ips_and_what_to_do(self):
        text = duplicate_html([(A, "C2627-Γ1", ("192.168.1.21", "192.168.1.47"))],
                              {A: "Board1"})
        self.assertTrue(text.startswith("Ίδια ταυτότητα σε 2 υπολογιστές: <b>C2627-Γ1 · Board1</b> "
                                        "(192.168.1.21, 192.168.1.47)<br>"))
        self.assertIn("Στον έναν από τους δύο:", text)
        self.assertIn(DUPLICATE_COMMAND, text)
        self.assertIn("και νέα έγκριση", text)
        self.assertIsNone(duplicate_html([]))

    def test_tooltip_and_state(self):
        self.assertEqual(duplicate_tooltip(("192.168.1.21", "192.168.1.47")),
                         "Ίδια ταυτότητα από <b>192.168.1.21</b> και <b>192.168.1.47</b>")
        computer = ComputerView(A, "Board1", "Γ1", True, None, ("1.1.1.1", "2.2.2.2"))
        self.assertEqual(computer.state(0), TEXT_DUPLICATE_STATE)
        self.assertEqual(ComputerView(A, "Board1", "Γ1", True, None).state(0), "Online")


class BroadcasterNamesTests(unittest.TestCase):
    def test_client_names_of_connected(self):
        import socket
        broadcaster = AudioBroadcaster()
        self.addCleanup(broadcaster.close)
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        client = socket.create_connection(listener.getsockname())
        self.addCleanup(client.close)
        server, _ = listener.accept()
        self.assertTrue(broadcaster.add_client(server, A, "Γ1", ("127.0.0.1", 5000),
                                               name="Board1"))
        self.assertEqual(broadcaster.client_names(), {A: "Board1"})


class NoticeBroadcaster(FakeBroadcaster):
    max_clients = MAX_CLIENTS

    def __init__(self):
        super().__init__()
        self.waiting = []
        self.duplicates = []
        self.names = {}

    def full_waiting(self):
        return list(self.waiting)

    def duplicate_identities(self):
        return list(self.duplicates)

    def client_names(self):
        return dict(self.names)


class _NoticeMic(_MicBroadcaster):
    max_clients = MAX_CLIENTS

    def full_waiting(self):
        return self.fake.full_waiting()

    def duplicate_identities(self):
        return self.fake.duplicate_identities()

    def client_names(self):
        return self.fake.client_names()


@unittest.skipIf(QT_ERROR, f"PySide6: {QT_ERROR}")
class WindowTests(SecurityFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_application()

    def setUp(self):
        super().setUp()
        from director.gui import DirectorMicrophone
        from tests.test_director_microphone import FakeStream
        from director.security import SecurityModel
        self.broadcaster = NoticeBroadcaster()
        self.model = SecurityModel(self.enrollment, self.trust, self.broadcaster,
                                   self.last_seen, clock=self.clock)
        mic = _NoticeMic(self.broadcaster)
        self.window = DirectorWindow(mic, DirectorMicrophone(FakeStream(), mic),
                                     security=self.model,
                                     security_options={"clock": self.clock})
        self.window.timer.stop()
        self.addCleanup(lambda: (self.window.close(), release_window(self.window)))
        self.window.resize(1366, 900)
        self.window.show()

    def approved(self, room, name, device):
        client_id, code = self.submit(room=room, name=name, device=device)
        self.model.approve(self.request_id(room, name), code)
        return client_id

    def test_no_bars_normally(self):
        self.window.refresh_capacity()
        self.assertTrue(self.window.full_bar.isHidden())
        self.assertTrue(self.window.duplicate_bar.isHidden())

    def test_full_bar_on_every_page_and_disappears(self):
        self.broadcaster.waiting = [(A, "C2627-Γ3", "Board1")]
        self.window.refresh_capacity()
        for page in ("home", "security"):
            self.window.show_page(page)
            self.assertTrue(self.window.full_bar.isVisible(), page)
        self.assertIn("Δεν συνδέονται: <b>C2627-Γ3 · Board1</b>", self.window.full_bar.text.text())
        self.assertEqual(self.window.full_bar.buttons, [])   # τίποτα να πατήσει
        self.broadcaster.waiting = []
        self.window.refresh_capacity()
        self.assertFalse(self.window.full_bar.isVisible())

    def test_full_bar_updates_through_timer_refresh(self):
        self.broadcaster.waiting = [(A, "C2627-Γ3", "Board1")]
        self.window._capacity_refreshed = float("-inf")
        self.window.refresh()
        self.assertTrue(self.window.full_bar.isVisible())

    def test_duplicate_bar_show_opens_computers_and_marks_row(self):
        client_id = self.approved("C2627-Γ1", "Board1", "a")
        other = self.approved("C2627-Γ1", "PC", "b")
        self.broadcaster.online = {client_id: ("C2627-Γ1", 0.1), other: ("C2627-Γ1", 0.1)}
        self.broadcaster.names = {client_id: "Board1"}
        self.broadcaster.duplicates = [(client_id, "C2627-Γ1", ("192.168.1.21", "192.168.1.47"))]
        self.window.show_page("home")
        self.window.refresh_capacity()
        bar = self.window.duplicate_bar
        self.assertTrue(bar.isVisible())
        self.assertIn("<b>C2627-Γ1 · Board1</b> (192.168.1.21, 192.168.1.47)", bar.text.text())
        self.assertEqual(self.window.duplicate_show.text(), TEXT_SHOW)
        self.assertTrue(self.window.duplicate_show.isVisible())
        self.window.duplicate_show.click()
        self.assertEqual(self.window.current_page, "security")
        self.assertEqual(self.window.security_page.current_tab, TAB_COMPUTERS)
        self.assertFalse(self.window.duplicate_show.isVisible())   # ήδη εκεί
        self.assertTrue(bar.isVisible())
        rows = self.window.security_page.computer_rows
        row = rows[client_id]
        self.assertEqual(row.state.text(), TEXT_DUPLICATE_STATE)
        self.assertFalse(row.alert.isHidden())
        self.assertIn("192.168.1.47", row.state.toolTip())
        self.assertEqual(rows[other].state.text(), "Online")
        self.assertTrue(rows[other].alert.isHidden())
        # Όταν σταματήσει η διπλή χρήση: κανονική γραμμή, χωρίς μπάρα.
        self.broadcaster.duplicates = []
        self.window.security_page.refresh()
        self.window.refresh_capacity()
        self.assertEqual(row.state.text(), "Online")
        self.assertTrue(row.alert.isHidden())
        self.assertEqual(row.state.toolTip(), "")
        self.assertFalse(bar.isVisible())

    def test_both_bars_stack(self):
        self.broadcaster.waiting = [(A, "C2627-Γ3", "Board1")]
        self.broadcaster.duplicates = [(B, "C2627-Γ1", ("10.0.0.1", "10.0.0.2"))]
        self.window.refresh_capacity()
        full, dup = self.window.full_bar, self.window.duplicate_bar
        self.assertTrue(full.isVisible() and dup.isVisible())
        self.assertLess(full.geometry().top(), dup.geometry().top())


@unittest.skipIf(QT_ERROR, f"PySide6: {QT_ERROR}")
class RealBroadcasterTests(unittest.TestCase):
    """Το παράθυρο με τον πραγματικό AudioBroadcaster: «γεμάτος» και δύο IP."""

    @classmethod
    def setUpClass(cls):
        cls.app = create_application()

    def add(self, broadcaster, client_id, ip, name="Board1"):
        import socket
        with socket.create_server(("127.0.0.1", 0)) as server:
            theirs = socket.create_connection(server.getsockname())
            ours, _ = server.accept()
        self.addCleanup(theirs.close)
        self.assertTrue(broadcaster.add_client(ours, client_id, "C2627-Γ1", (ip, 40000),
                                               name=name))

    def test_bars_follow_broadcaster(self):
        from director.gui import DirectorMicrophone
        from tests.test_director_microphone import FakeStream
        broadcaster = AudioBroadcaster(max_clients=1, on_event=lambda _e: None)
        self.addCleanup(broadcaster.close)
        now = [0.0]
        broadcaster.clock = lambda: now[0]
        window = DirectorWindow(broadcaster, DirectorMicrophone(FakeStream(), broadcaster))
        window.timer.stop()
        self.addCleanup(lambda: (window.close(), release_window(window)))
        window.show()
        for index in range(5):
            now[0] += 5
            self.add(broadcaster, A, "10.0.0.11" if index % 2 else "10.0.0.12")
        self.assertFalse(broadcaster.has_room(B))
        broadcaster.note_full(B, "C2627-Γ3", "PC")
        window.refresh_capacity()
        self.assertIn("<b>C2627-Γ3 · PC</b>", window.full_bar.text.text())
        self.assertIn("<b>C2627-Γ1 · Board1</b> (10.0.0.11, 10.0.0.12)",
                      window.duplicate_bar.text.text())
        self.assertTrue(window.full_bar.isVisible() and window.duplicate_bar.isVisible())
        now[0] += 400   # ο επιπλέον σταμάτησε να ξαναδοκιμάζει, η διπλή χρήση έληξε
        window.refresh_capacity()
        self.assertFalse(window.full_bar.isVisible())
        self.assertFalse(window.duplicate_bar.isVisible())


@unittest.skipIf(QT_ERROR, f"PySide6: {QT_ERROR}")
class WindowWithoutSecurityTests(unittest.TestCase):
    """Χωρίς σελίδα Ασφάλεια (δοκιμές/legacy): η μπάρα εμφανίζεται, χωρίς «Προβολή»."""

    @classmethod
    def setUpClass(cls):
        cls.app = create_application()

    def test_duplicate_without_security_page(self):
        from director.gui import DirectorMicrophone
        from tests.test_director_microphone import FakeStream
        fake = NoticeBroadcaster()
        fake.duplicates = [(A, "Γ1", ("10.0.0.1", "10.0.0.2"))]
        mic = _NoticeMic(fake)
        window = DirectorWindow(mic, DirectorMicrophone(FakeStream(), mic))
        window.timer.stop()
        self.addCleanup(lambda: (window.close(), release_window(window)))
        window.show()
        window.refresh_capacity()
        self.assertTrue(window.duplicate_bar.isVisible())
        self.assertFalse(window.duplicate_show.isVisible())
        window.show_computers()   # δεν σκάει


if __name__ == "__main__":
    unittest.main()
