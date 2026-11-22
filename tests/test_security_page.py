"""Σελίδα «Ασφάλεια»: λογική (χωρίς Qt) και παράθυρο PySide6 offscreen.

Χρησιμοποιούνται πραγματικά EnrollmentServer, υπογεγραμμένο μητρώο και RegistryTrust
σε προσωρινό φάκελο· ψεύτικος είναι μόνο ο broadcaster (ποιος είναι online).
Τα Qt tests παραλείπονται αν δεν υπάρχει PySide6.
"""
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from director.main import RegistryTrust
from director.security import (LAST_SEEN_SAVE_SECONDS, TEXT_APPROVED, TEXT_DENIED,
                               TEXT_EXPIRED, TEXT_NEVER, TEXT_REJECTED, LastSeenStore,
                               SecurityModel, ago_text, expires_text, last_seen_path)
from shared.client_registry import SignedClientRegistry
from shared.device_identity import create_device_identity
from shared.enrollment import (REQUEST_TTL, EnrollmentServer, enroll_client, make_request)
from tests.tls_support import LocalTLSFixture

try:
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from director.qt_gui import DirectorWindow, create_application, release_window
    from director.qt_security import RESULT_SECONDS, PORT_CLOSED, PORT_OPEN
except ImportError as exc:  # pragma: no cover - εξαρτάται από το περιβάλλον
    QT_ERROR = str(exc)
else:
    QT_ERROR = None


class FakeBroadcaster:
    """Μόνο ό,τι χρειάζεται η σελίδα: κατάσταση online και ενημερώσεις Manager."""

    def __init__(self):
        self.online: dict[str, tuple[str, float]] = {}
        self.offline: dict[str, tuple[str, float]] = {}
        self.updates = []
        self.disconnected = []

    def status_snapshot(self):
        rows = [(cid, room, True, age) for cid, (room, age) in self.online.items()]
        rows += [(cid, room, False, age) for cid, (room, age) in self.offline.items()]
        return sorted(rows)

    def update_client_info(self, client_id, room, name):
        self.updates.append((client_id, room, name))

    def disconnect_client(self, client_id):
        self.disconnected.append(client_id)
        if client_id in self.online:
            self.offline[client_id] = self.online.pop(client_id)


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class TextTests(unittest.TestCase):
    def test_ago_matches_mockup_wording(self):
        self.assertEqual(ago_text(20), "πριν 20″")
        self.assertEqual(ago_text(60), "πριν 1 λεπτό")
        self.assertEqual(ago_text(180), "πριν 3 λεπτά")
        self.assertEqual(ago_text(3600), "πριν 1 ώρα")
        self.assertEqual(ago_text(7200), "πριν 2 ώρες")
        self.assertEqual(ago_text(86400), "πριν 1 ημέρα")
        self.assertEqual(ago_text(3 * 86400 + 5), "πριν 3 ημέρες")
        self.assertEqual(ago_text(-5), "πριν 0″")

    def test_expiry_says_today_tomorrow_or_date(self):
        now = time.mktime((2026, 11, 22, 7, 12, 0, 0, 0, -1))
        self.assertEqual(expires_text(now + 3600, now), "λήγει σήμερα 08:12")
        self.assertEqual(expires_text(now + REQUEST_TTL, now), "λήγει αύριο 07:12")
        self.assertEqual(expires_text(now + 2 * REQUEST_TTL, now), "λήγει 24/11 07:12")


class LastSeenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = last_seen_path(Path(self.temp.name) / "clients-registry.json")
        self.clock = Clock()
        self.logs = []

    def store(self):
        return LastSeenStore(self.path, clock=self.clock, log=self.logs.append)

    def saved(self):
        return json.loads(self.path.read_text(encoding="utf-8"))["clients"]

    def test_file_lives_next_to_registry(self):
        self.assertEqual(self.path.name, "clients-last-seen.json")
        self.assertEqual(self.path.parent, Path(self.temp.name))

    def test_connect_and_disconnect_are_saved_immediately_and_survive_restart(self):
        store = self.store()
        store.update([("a", "Γ1", True, 0.5)])
        self.assertEqual(self.saved(), {"a": self.clock.now - 0.5})
        self.clock.now += 30
        # Αποσύνδεση: κρατιέται η ώρα του τελευταίου heartbeat, όχι το «τώρα».
        store.update([("a", "Γ1", False, 4.0)])
        self.assertEqual(self.saved(), {"a": self.clock.now - 4.0})
        again = self.store()
        self.assertEqual(again.get("a"), self.clock.now - 4.0)
        self.assertIsNone(again.get("b"))

    def test_short_connection_seen_only_as_offline_is_saved(self):
        # Σύνδεση μικρότερη από έναν γύρο ελέγχου: φαίνεται μόνο ως αποσύνδεση.
        store = self.store()
        store.update([("a", "Γ1", False, 0.4)])
        self.assertEqual(self.saved(), {"a": self.clock.now - 0.4})

    def test_steady_online_saves_at_most_once_per_interval(self):
        store = self.store()
        store.update([("a", "Γ1", True, 0.0)])
        first = self.saved()["a"]
        self.clock.now += 10
        store.update([("a", "Γ1", True, 0.0)])
        self.assertEqual(self.saved()["a"], first)
        self.assertEqual(store.get("a"), self.clock.now)  # στη μνήμη ενημερωμένο
        self.clock.now += LAST_SEEN_SAVE_SECONDS
        store.update([("a", "Γ1", True, 0.0)])
        self.assertEqual(self.saved()["a"], self.clock.now)

    def test_flush_writes_pending_changes(self):
        store = self.store()
        store.update([("a", "Γ1", True, 0.0)])
        self.clock.now += 5
        store.update([("a", "Γ1", True, 0.0)])
        store.flush()
        self.assertEqual(self.saved()["a"], self.clock.now)

    def test_never_moves_backwards(self):
        store = self.store()
        store.update([("a", "Γ1", True, 0.0)])
        store.update([("a", "Γ1", False, 100.0)])
        self.assertEqual(store.get("a"), self.clock.now)

    def test_corrupt_or_unknown_file_is_ignored(self):
        for content in ("{", "[]", json.dumps({"version": 99, "clients": {"a": 1}}),
                        json.dumps({"version": 1, "clients": {"a": "x", "b": True}})):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                self.assertIsNone(self.store().get("a"))
                self.assertIsNone(self.store().get("b"))


class SecurityFixture(LocalTLSFixture):
    def setUp(self):
        super().setUp()
        self.root = Path(self._tls_temp.name)
        self.cert = self.root / "director.crt"
        self.key = self.root / "director.key"
        self.registry_file = self.root / "clients-registry.json"
        self.registry = SignedClientRegistry(self.registry_file, self.cert, self.key)
        self.trust = RegistryTrust(self.registry, self.cert, self.key)
        self.clock = Clock(time.time())
        self.enrollment = EnrollmentServer(self.cert, self.key, self.trust, host="127.0.0.1",
                                           port=0, pending_file=self.root / "pending.json",
                                           log=lambda _m: None, clock=self.clock)
        self.addCleanup(self.enrollment.stop)
        self.broadcaster = FakeBroadcaster()
        self.last_seen = LastSeenStore(last_seen_path(self.registry_file), clock=self.clock,
                                       log=lambda _m: None)
        self.model = SecurityModel(self.enrollment, self.trust, self.broadcaster,
                                   self.last_seen, clock=self.clock)
        self.devices = {}

    def submit(self, room="C2627-Γ1", name="Board1", device="d1"):
        """Πραγματική υποβολή από «συσκευή» (TLS, υπογραφή)· επιστρέφει (UUID, κωδικό)."""
        if device not in self.devices:
            client_id = str(uuid.uuid4())
            self.devices[device] = (client_id, *create_device_identity(
                self.root / device, client_id))
        client_id, cert, key, password = self.devices[device]
        request = make_request(client_id, room, name, cert.read_bytes(), key.read_bytes(),
                               password)
        opened = self.enrollment.is_open
        if not opened:
            self.enrollment.start()
        try:
            queued = enroll_client("127.0.0.1", self.enrollment.port, self.cert,
                                   "school-intercom-director", request)
        finally:
            if not opened:
                self.enrollment.stop()
        return client_id, queued["code"]

    def request_id(self, room="C2627-Γ1", name="Board1"):
        [rid] = [r.request_id for r in self.model.requests() if (r.room, r.name) == (room, name)]
        return rid

    def wrong(self, code, offset=1):
        return f"{(int(code) + offset) % 1000000:06d}"


class SecurityModelTests(SecurityFixture, unittest.TestCase):
    def test_port_starts_closed_and_toggles(self):
        self.assertFalse(self.model.port_open)
        self.assertIsNone(self.model.set_port_open(True))
        self.assertTrue(self.model.port_open)
        self.assertIsNone(self.model.set_port_open(False))
        self.assertFalse(self.model.port_open)

    def test_busy_port_reports_error(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen(1)
            self.enrollment.port = busy.getsockname()[1]
            error = self.model.set_port_open(True)
        self.assertIn("δεν άνοιξε", error)
        self.assertFalse(self.model.port_open)

    def test_requests_newest_first_without_code(self):
        self.submit(name="Board1", device="d1")
        self.clock.now += 120
        _cid, code = self.submit(name="PC", device="d2")
        requests = self.model.requests()
        self.assertEqual([r.title for r in requests], ["C2627-Γ1 · PC", "C2627-Γ1 · Board1"])
        meta = requests[1].meta(self.clock.now)
        self.assertTrue(meta.startswith(f"ID {requests[1].short_id} · πριν 2 λεπτά · λήγει"))
        self.assertNotIn(code, repr(requests))

    def test_three_wrong_codes_then_rejected(self):
        client_id, code = self.submit()
        rid = self.request_id()
        first = self.model.approve(rid, self.wrong(code, 1))
        second = self.model.approve(rid, self.wrong(code, 2))
        third = self.model.approve(rid, self.wrong(code, 3))
        self.assertEqual((first.text, first.final), ("Λάθος κωδικός. Απομένουν 2 προσπάθειες.",
                                                     False))
        self.assertEqual(second.text, "Λάθος κωδικός. Απομένει 1 προσπάθεια.")
        self.assertEqual((third.text, third.final, third.ok), (TEXT_REJECTED, True, False))
        self.assertEqual(self.model.requests(), [])
        self.assertIsNone(self.registry.approved_entry(client_id))

    def test_short_code_is_not_counted_as_attempt(self):
        _client_id, code = self.submit()
        rid = self.request_id()
        for typed in ("", "123", "12a456", "1234567"):
            result = self.model.approve(rid, typed)
            self.assertEqual((result.text, result.final), ("Ο κωδικός έχει 6 ψηφία", False))
        for offset in (1, 2):
            self.assertFalse(self.model.approve(rid, self.wrong(code, offset)).final)
        self.assertEqual(self.model.approve(rid, code).text, TEXT_APPROVED)

    def test_correct_code_approves_even_with_port_closed(self):
        client_id, code = self.submit()
        self.assertFalse(self.model.port_open)
        result = self.model.approve(self.request_id(), code)
        self.assertEqual((result.text, result.ok, result.final), (TEXT_APPROVED, True, True))
        entry = self.registry.approved_entry(client_id)
        self.assertEqual((entry["room"], entry["name"]), ("C2627-Γ1", "Board1"))

    def test_deny_and_answered_request(self):
        client_id, _code = self.submit()
        rid = self.request_id()
        self.assertEqual(self.model.deny(rid).text, TEXT_DENIED)
        self.assertEqual(self.model.deny(rid).text, TEXT_EXPIRED)
        self.assertEqual(self.model.approve(rid, "123456").text, TEXT_EXPIRED)
        self.assertIsNone(self.registry.approved_entry(client_id))

    def approve(self, room, name, device):
        client_id, code = self.submit(room=room, name=name, device=device)
        self.assertTrue(self.model.approve(self.request_id(room, name), code).ok)
        return client_id

    def test_rooms_grouped_sorted_with_online_and_last_seen(self):
        a = self.approve("C2627-Γ1", "PC", "a")
        b = self.approve("C2627-Γ1", "Board1", "b")
        c = self.approve("C2627-Δ1", "Board1", "c")
        d = self.approve("Γραφείο", "Διευθυντής-PC", "d")
        self.broadcaster.online[b] = ("C2627-Γ1", 0.2)
        self.broadcaster.offline[a] = ("C2627-Γ1", 7200.0)
        self.last_seen.update(self.broadcaster.status_snapshot())
        self.last_seen.update([(c, "C2627-Δ1", False, 3 * 86400)])
        rooms = self.model.rooms()
        self.assertEqual([r.room for r in rooms], ["C2627-Γ1", "C2627-Δ1", "Γραφείο"])
        self.assertEqual([c.name for c in rooms[0].computers], ["Board1", "PC"])
        now = self.clock.now
        self.assertEqual([c.state(now) for c in rooms[0].computers], ["Online", "πριν 2 ώρες"])
        self.assertTrue(rooms[0].online)
        self.assertEqual(rooms[1].state(now), "πριν 3 ημέρες")
        self.assertFalse(rooms[1].online)
        self.assertEqual(rooms[2].computers[0].client_id, d)
        self.assertEqual(rooms[2].state(now), TEXT_NEVER)
        self.assertEqual(self.model.totals(rooms), "4 υπολογιστές · 1 online")

    def test_live_status_counts_before_store_catches_up(self):
        a = self.approve("Γ1", "PC", "a")
        self.broadcaster.offline[a] = ("Γ1", 30.0)  # ο LastSeenStore δεν το έχει δει ακόμη
        [room] = self.model.rooms()
        self.assertEqual(room.computers[0].state(self.clock.now), "πριν 30″")

    def test_room_last_seen_is_most_recent_computer(self):
        a = self.approve("Γ1", "PC", "a")
        b = self.approve("Γ1", "Board1", "b")
        self.last_seen.update([(a, "Γ1", False, 3600.0), (b, "Γ1", False, 600.0)])
        [room] = self.model.rooms()
        self.assertEqual(room.state(self.clock.now), "πριν 10 λεπτά")

    def test_change_room_rename_and_revoke(self):
        client_id = self.approve("C2627-Γ1", "Board1", "a")
        self.model.change_room(client_id, " C2627-Γ2 ")
        self.model.rename(client_id, "Board 2")
        self.assertEqual(self.broadcaster.updates[-1], (client_id, "C2627-Γ2", "Board 2"))
        [room] = self.model.rooms()
        self.assertEqual((room.room, room.computers[0].name), ("C2627-Γ2", "Board 2"))
        self.broadcaster.online[client_id] = ("C2627-Γ2", 0.1)
        self.model.revoke(client_id)
        self.assertEqual(self.broadcaster.disconnected, [client_id])
        self.assertEqual(self.model.rooms(), [])
        self.assertIsNone(self.registry.approved_entry(client_id))

    def test_invalid_metadata_gives_greek_error(self):
        client_id = self.approve("C2627-Γ1", "Board1", "a")
        with self.assertRaisesRegex(ValueError, "Μη έγκυρη αίθουσα"):
            self.model.change_room(client_id, "   ")
        with self.assertRaisesRegex(ValueError, "Μη έγκυρο όνομα"):
            self.model.rename(client_id, "x" * 65)
        with self.assertRaisesRegex(ValueError, "Η μετονομασία απέτυχε"):
            self.model.rename(str(uuid.uuid4()), "Board")


@unittest.skipIf(QT_ERROR is not None, f"PySide6 μη διαθέσιμο: {QT_ERROR}")
class SecurityPageTests(SecurityFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_application()

    def setUp(self):
        super().setUp()
        from director.gui import DirectorMicrophone
        from tests.test_director_microphone import FakeStream
        self.asked = []
        self.answers = []
        self.warnings = []
        self.confirm_answer = True
        options = {"clock": self.clock, "ask_text": self.ask, "confirm": self.confirm,
                   "warn": self.warnings.append}
        mic_broadcaster = _MicBroadcaster(self.broadcaster)
        self.window = DirectorWindow(mic_broadcaster, DirectorMicrophone(FakeStream(),
                                                                         mic_broadcaster),
                                     security=self.model, security_options=options)
        self.window.timer.stop()
        self.addCleanup(lambda: (self.window.close(), release_window(self.window)))
        self.window.resize(1400, 800)
        self.window.show()
        self.window.show_page("security")
        self.page = self.window.security_page

    def ask(self, title, label, value):
        self.asked.append((title, value))
        return self.answers.pop(0)

    def confirm(self, title, text):
        self.asked.append((title, text))
        return self.confirm_answer

    def test_menu_switches_page_and_title(self):
        window = self.window
        self.assertTrue(window.nav_buttons["security"].isEnabled())
        self.assertTrue(window.nav_buttons["security"].isChecked())
        self.assertFalse(window.nav_buttons["home"].isChecked())
        self.assertEqual(window.page_title.text(), "Ασφάλεια")
        self.assertIs(window.pages.currentWidget(), self.page)
        window.nav_buttons["home"].click()
        self.assertEqual((window.current_page, window.page_title.text()),
                         ("home", "Κεντρική λειτουργία"))
        self.assertFalse(window.nav_buttons["security"].isChecked())
        for key in ("rooms", "announcements", "scenarios", "logs", "settings"):
            self.assertFalse(window.nav_buttons[key].isEnabled())
        keys = list(window.nav_buttons)
        self.assertEqual(keys.index("security") + 1, keys.index("settings"))

    def test_tabs_and_port_button(self):
        page = self.page
        self.assertTrue(page.tab_computers.isChecked())
        self.assertFalse(page.tab_certificates.isEnabled())
        self.assertEqual((page.port_label.text(), page.port_button.text()),
                         (PORT_CLOSED, "Άνοιγμα"))
        page.port_button.click()
        self.assertTrue(self.model.port_open)
        self.assertEqual((page.port_label.text(), page.port_button.text()),
                         (PORT_OPEN, "Κλείσιμο"))
        page.port_button.click()
        self.assertFalse(self.model.port_open)

    def test_request_row_wrong_codes_and_code_never_shown(self):
        client_id, code = self.submit()
        self.page.refresh()
        [row] = self.page.request_rows.values()
        self.assertEqual(row.title.text(), "C2627-Γ1 · Board1")
        self.assertIn("λήγει αύριο", row.meta.text())
        self.assertFalse(any(code in text for text in self.page.visible_texts()))
        QTest.keyClicks(row.code, "12ab3")  # δεκτά μόνο ψηφία
        self.assertEqual(row.code.text(), "123")
        row.code.setText(self.wrong(code))
        row.approve.click()
        self.assertEqual(row.result.text(), "Λάθος κωδικός. Απομένουν 2 προσπάθειες.")
        self.assertFalse(row.actions.isHidden())
        self.assertEqual(row.code.text(), "")
        for offset in (2, 3):
            row.code.setText(self.wrong(code, offset))
            row.approve.click()
        self.assertEqual(row.result.text(), TEXT_REJECTED)
        self.assertTrue(row.actions.isHidden())
        self.assertIsNone(self.registry.approved_entry(client_id))

    def test_approve_then_row_disappears_and_computer_listed(self):
        client_id, code = self.submit()
        self.page.refresh()
        [row] = self.page.request_rows.values()
        row.code.setText(code)
        row.code.returnPressed.emit()
        self.assertEqual(row.result.text(), TEXT_APPROVED)
        self.assertEqual(row.result.property("ok"), "true")
        self.assertIn(client_id, self.page.computer_rows)
        self.assertEqual(self.page.totals.text(), "1 υπολογιστής · 0 online")
        self.page.refresh()
        self.assertIn(row, self.page.request_rows.values())  # το αποτέλεσμα μένει ορατό
        self.clock.now += RESULT_SECONDS + 1
        self.page.refresh()
        self.assertEqual(self.page.request_rows, {})
        self.assertFalse(self.page.no_requests.isHidden())

    def test_deny_button(self):
        self.submit()
        self.page.refresh()
        [row] = self.page.request_rows.values()
        row.deny.click()
        self.assertEqual(row.result.text(), TEXT_DENIED)
        self.assertEqual(self.model.requests(), [])

    def approved(self, room, name, device):
        client_id, code = self.submit(room=room, name=name, device=device)
        self.model.approve(self.request_id(room, name), code)
        return client_id

    def test_computers_grouped_with_indent_and_state(self):
        a = self.approved("C2627-Γ1", "Board1", "a")
        b = self.approved("C2627-Γ1", "PC", "b")
        self.broadcaster.online[a] = ("C2627-Γ1", 0.1)
        self.broadcaster.offline[b] = ("C2627-Γ1", 7200.0)
        self.last_seen.update(self.broadcaster.status_snapshot())
        self.page.refresh()
        header = self.page.room_headers["C2627-Γ1"]
        self.assertEqual(header.count.text(), "2 υπολογιστές")
        rows = self.page.computer_rows
        QApplication.processEvents()
        self.assertEqual((rows[a].state.text(), rows[b].state.text()), ("Online", "πριν 2 ώρες"))
        self.assertGreater(rows[a].dot.mapTo(self.page, rows[a].dot.rect().topLeft()).x(),
                           header.dot.mapTo(self.page, header.dot.rect().topLeft()).x() + 15)
        self.assertEqual(self.page.totals.text(), "2 υπολογιστές · 1 online")

    def test_menu_actions_change_room_rename_and_revoke(self):
        client_id = self.approved("C2627-Γ1", "Board1", "a")
        self.page.refresh()
        self.answers = ["C2627-Γ2"]
        self.page.computer_rows[client_id].actions["room"].trigger()
        self.assertIn("C2627-Γ2", self.page.room_headers)
        self.answers = ["Board 2"]
        self.page.computer_rows[client_id].actions["rename"].trigger()
        self.assertEqual(self.page.computer_rows[client_id].name.full_text, "Board 2")
        self.answers = [None]  # Ακύρωση
        self.page.computer_rows[client_id].actions["rename"].trigger()
        self.confirm_answer = False
        self.page.computer_rows[client_id].actions["revoke"].trigger()
        self.assertIsNotNone(self.registry.approved_entry(client_id))
        self.confirm_answer = True
        self.page.computer_rows[client_id].actions["revoke"].trigger()
        self.assertIsNone(self.registry.approved_entry(client_id))
        self.assertEqual(self.page.computer_rows, {})
        self.assertEqual(self.warnings, [])

    def test_cards_side_by_side_or_stacked_on_narrow_window(self):
        self.submit()
        self.page.refresh()
        for width, expected in ((1400, True), (900, False), (1400, True)):
            self.window.resize(width, 800)
            QApplication.processEvents()
            with self.subTest(width=width):
                self.assertEqual(self.page.side_by_side, expected)
                body = self.page.scroll.widget()
                self.assertLessEqual(body.width(), self.page.scroll.viewport().width())

    def test_cards_equal_width_and_never_clipped_when_content_grows(self):
        # Πραγματική δοκιμή 22/11/2026 (1200px): η δεξιά κάρτα κοβόταν, γιατί το
        # περιεχόμενο που μπήκε μετά το resize δεν ξαναέλεγχε τη διάταξη.
        self.window.resize(1200, 800)
        QApplication.processEvents()
        body, viewport = self.page.scroll.widget(), self.page.scroll.viewport()
        widths = set()
        for step in range(3):
            self.approved("C2627-Γ1", f"Board{step}", f"d{step}")
            self.submit(name=f"PC{step}", device=f"p{step}")
            self.page.refresh()
            QApplication.processEvents()
            with self.subTest(step=step):
                self.assertLessEqual(body.width(), viewport.width())
                for card in (self.page.requests_card, self.page.computers_card):
                    self.assertGreaterEqual(card.width(), card.minimumSizeHint().width())
                if self.page.side_by_side:
                    self.assertLessEqual(abs(self.page.requests_card.width()
                                             - self.page.computers_card.width()), 1)
                    widths.add(self.page.requests_card.width())
        self.assertLessEqual(len(widths), 1)  # σταθερό πλάτος όσο αλλάζει το περιεχόμενο

    def test_invalid_rename_shows_warning(self):
        client_id = self.approved("C2627-Γ1", "Board1", "a")
        self.page.refresh()
        self.answers = ["x" * 70]
        self.page.computer_rows[client_id].actions["rename"].trigger()
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("Μη έγκυρο όνομα", self.warnings[0])


class _MicBroadcaster:
    """Ο broadcaster της κεντρικής σελίδας: κατάσταση από τον ψεύτικο, στόχοι αγνοούνται."""

    def __init__(self, fake):
        self.fake = fake

    def status_snapshot(self):
        return self.fake.status_snapshot()

    def set_targets(self, **_kwargs):
        pass

    def publish(self, *_args):
        pass


if __name__ == "__main__":
    unittest.main()
