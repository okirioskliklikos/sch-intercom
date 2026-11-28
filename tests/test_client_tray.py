"""5C1: κατάσταση Client, ιστορικό (history.jsonl), tray και παράθυρο."""
from __future__ import annotations

import json
from pathlib import Path
import ssl
import tempfile
import threading
import time
from unittest import TestCase, skipUnless

from client import status as st
from client.main import REJECTED, NotAdmitted, classify_failure, run_with_reconnect
from shared.identity import ManagerFull, ProtocolMismatch

try:
    from shared.ui.offscreen import use_offscreen
    use_offscreen()
    from PySide6.QtWidgets import QApplication  # noqa: F401
    HAVE_QT = True
except ImportError:  # pragma: no cover — χωρίς PySide6
    HAVE_QT = False

DAY = 86400
# Παρασκευή 27/11/2026 12:00 τοπική ώρα.
NOW = time.mktime((2026, 11, 27, 12, 0, 0, 0, 0, -1))


class Clock:
    def __init__(self, now: float = NOW):
        self.now = now

    def __call__(self) -> float:
        return self.now


class StatusTests(TestCase):
    def test_listeners_fire_only_on_change(self):
        status = st.ClientStatus(clock=Clock())
        calls = []
        status.add_listener(lambda: calls.append(1))
        status.update(room="C2627-Γ1")
        status.update(room="C2627-Γ1")
        status.set_state(st.CONNECTED)
        self.assertEqual(len(calls), 2)
        self.assertTrue(status.snapshot()["approved_before"])

    def test_since_changes_only_with_state(self):
        clock = Clock()
        status = st.ClientStatus(clock=clock)
        status.set_state(st.CONNECTED)
        clock.now += 60
        status.set_state(st.CONNECTED)
        self.assertEqual(status.snapshot()["since"], NOW)
        status.set_state(st.CONNECTING)
        self.assertEqual(status.snapshot()["since"], NOW + 60)

    def test_texts(self):
        snap = st.ClientStatus(room="C2627-Γ1", name="Board1",
                               enrollment={"code": "482913", "expires_at": NOW + DAY}).snapshot()
        snap["state"] = st.PENDING
        self.assertEqual(st.tooltip(snap),
                         "C2627-Γ1 · Board1 — Αναμονή έγκρισης, κωδικός 482 913")
        self.assertEqual(st.menu_lines(snap), ["C2627-Γ1 · Board1", "Αναμονή έγκρισης"])
        self.assertEqual(st.tone(snap), "wait")
        snap["state"] = st.CONNECTED
        self.assertEqual(st.tooltip(snap), "C2627-Γ1 · Board1 — Συνδεδεμένος")
        self.assertEqual(st.tone(snap), "ok")
        snap["update"] = {"state": "failed", "version": "1.0.7", "detail": "x"}
        self.assertEqual(st.tone(snap), "bad")
        self.assertIn("αποτυχία ενημέρωσης", st.tooltip(snap))
        snap["state"] = st.REVOKED
        self.assertEqual(st.tone(snap), "bad")
        self.assertEqual(st.title({"room": None, "name": None}), st.NEW_COMPUTER)

    def test_failed_update_keeps_reason(self):
        status = st.ClientStatus()
        status.set_update("failed", "1.0.7", "Η νέα έκδοση δεν συνδέθηκε")
        status.set_update("failed", "1.0.7")
        self.assertEqual(status.snapshot()["update"]["detail"], "Η νέα έκδοση δεν συνδέθηκε")

    def test_info_rows(self):
        status = st.ClientStatus(clock=Clock(), room="C2627-Γ1", name="Board1",
                                 manager="192.168.1.10", fingerprint="84285ca1" + "00" * 24
                                 + "7e419d33", version="1.0.6", volume=60, mute_others=True,
                                 device="Ηχεία", client_id="8c57")
        status.set_state(st.CONNECTED)
        status.set_update("current")
        rows = {row["label"]: row for row in st.info_rows(status.snapshot(), now=NOW)}
        self.assertEqual(rows["Κατάσταση"]["value"], "Συνδεδεμένος")
        self.assertEqual(rows["Κατάσταση"]["note"], "από 12:00")
        self.assertEqual(rows["Αποτύπωμα Director"]["value"], "84:28:5C:A1 … 7E:41:9D:33")
        self.assertTrue(rows["Αποτύπωμα Director"]["mono"])
        self.assertEqual(rows["Ενημέρωση"]["value"], "Ενημερωμένη")
        self.assertEqual(rows["Ενημέρωση"]["tone"], "ok")
        self.assertEqual(rows["Ένταση ανακοίνωσης"]["value"], "60% · σίγαση άλλων ήχων")
        self.assertEqual(rows["Συσκευή ήχου"]["value"], "Ηχεία")
        self.assertEqual(rows["ID υπολογιστή"]["value"], "8c57")

    def test_pending_info_has_no_audio_rows(self):
        status = st.ClientStatus(room="C2627-Γ1", name="Board1", client_id="x")
        status.set_state(st.PENDING)
        labels = [row["label"] for row in st.info_rows(status.snapshot())]
        self.assertNotIn("Ένταση ανακοίνωσης", labels)
        self.assertNotIn("Ενημέρωση", labels)

    def test_expiry_text(self):
        self.assertTrue(st.expiry_text(NOW + DAY, now=NOW).startswith("Λήγει αύριο "))
        self.assertTrue(st.expiry_text(NOW + 3600, now=NOW).startswith("Λήγει σήμερα "))
        self.assertTrue(st.expiry_text(NOW + 3 * DAY, now=NOW).startswith("Λήγει 30/11 "))

    def test_tray_supported(self):
        self.assertTrue(st.tray_supported({}, "win32"))
        self.assertFalse(st.tray_supported({st.NO_TRAY_ENV: "1"}, "win32"))
        self.assertFalse(st.tray_supported({}, "linux"))
        self.assertTrue(st.tray_supported({"DISPLAY": ":0"}, "linux"))


class PendingFileTests(TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.file = self.dir / st.PENDING_FILE

    def test_round_trip_and_ownership(self):
        st.save_pending(self.file, code="482913", room="C2627-Γ1", name="Board1",
                        expires_at=NOW + DAY, client_id="A")
        self.assertEqual(st.load_pending(self.file, "A", now=NOW)["code"], "482913")
        self.assertIsNone(st.load_pending(self.file, "B", now=NOW))       # άλλη ταυτότητα
        self.assertIsNone(st.load_pending(self.file, "A", now=NOW + 2 * DAY))  # έληξε
        st.clear_pending(self.file)
        st.clear_pending(self.file)
        self.assertIsNone(st.load_pending(self.file, "A", now=NOW))

    def test_garbage_is_ignored(self):
        self.file.write_text("{", encoding="utf-8")
        self.assertIsNone(st.load_pending(self.file, "A"))
        self.file.write_text(json.dumps({"client_id": "A", "code": "12ab", "expires_at": 9e12}),
                             encoding="utf-8")
        self.assertIsNone(st.load_pending(self.file, "A"))


class HistoryTests(TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.file = self.dir / st.HISTORY_FILE
        self.clock = Clock()

    def test_begin_end_appends_line(self):
        history = st.History(self.file, clock=self.clock).load()
        history.begin()
        self.assertEqual(len(history.all()), 1)
        self.clock.now += 72
        event = history.end()
        self.assertEqual(event, {"v": 1, "kind": "live", "start": NOW, "end": NOW + 72})
        lines = self.file.read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line) for line in lines], [event])
        self.assertIsNone(history.end())

    def test_prune_keeps_unknown_fields_and_kinds(self):
        old = {"v": 1, "kind": "live", "start": NOW - 31 * DAY, "end": NOW - 31 * DAY + 5}
        future = {"v": 2, "kind": "text", "start": NOW - DAY, "end": NOW - DAY + 3,
                  "title": "Μήνυμα", "text": "Λήξη μαθημάτων 13:00", "extra": {"a": 1}}
        self.file.write_text(json.dumps(old) + "\nnot json\n" + json.dumps(future, ensure_ascii=False)
                             + "\n", encoding="utf-8")
        history = st.History(self.file, clock=self.clock).load()
        self.assertEqual(history.all(), [future])
        kept = [json.loads(line) for line in self.file.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(kept, [future])     # άγνωστα πεδία μένουν ανέγγιχτα
        groups = st.history_groups(history.all(), now=NOW)
        row = groups[0]["rows"][0]
        self.assertEqual((row["kind"], row["known"], row["text"]),
                         ("Μήνυμα", False, "Λήξη μαθημάτων 13:00"))

    def test_groups_by_day(self):
        events = [
            {"kind": "live", "start": NOW - 3600, "end": NOW - 3600 + 72},
            {"kind": "live", "start": NOW},
            {"kind": "live", "start": NOW - DAY, "end": NOW - DAY + 125},
            {"kind": "live", "start": NOW - 3 * DAY, "end": NOW - 3 * DAY + 21},
        ]
        groups = st.history_groups(events, now=NOW)
        self.assertEqual([g["day"] for g in groups],
                         ["Σήμερα, Παρασκευή 27/11", "Χθες, Πέμπτη 26/11", "Τρίτη 24/11"])
        today = groups[0]["rows"]
        self.assertTrue(today[0]["live"])
        self.assertEqual(today[1]["duration"], "1:12")
        self.assertEqual(groups[1]["rows"][0]["duration"], "2:05")
        self.assertEqual(st.history_groups([], now=NOW), [])


class FailureClassificationTests(TestCase):
    def test_kinds(self):
        self.assertEqual(classify_failure(ManagerFull())[0], st.FULL)
        self.assertEqual(classify_failure(ProtocolMismatch("v9"))[0], st.INCOMPATIBLE)
        alert = ssl.SSLError(1, "[SSL: TLSV1_ALERT_UNKNOWN_CA] tlsv1 alert unknown ca")
        self.assertEqual(classify_failure(alert)[0], REJECTED)
        wrong = ssl.SSLCertVerificationError(1, "certificate verify failed")
        self.assertEqual(classify_failure(wrong)[0], st.ERROR)
        self.assertEqual(classify_failure(ConnectionRefusedError())[0], st.CONNECTING)
        # TLS 1.3: η απόρριψη μπορεί να φανεί ως reset πριν από το WELCOME (χρονισμός/Windows).
        self.assertEqual(classify_failure(NotAdmitted("reset"))[0], REJECTED)

    def test_reconnect_reports_states_without_flapping(self):
        states = []
        attempts = iter([ssl.SSLError(1, "TLSV1_ALERT_UNKNOWN_CA")] * 3)

        def session(_connection):
            raise next(attempts)

        import client.main as main_module
        original = main_module._verified_connection
        main_module._verified_connection = lambda *a, **k: (_Fake(), None)
        try:
            run_with_reconnect("127.0.0.1", 1, session, max_sessions=3,
                               tls_context=ssl.create_default_context(),
                               on_state=lambda kind, detail: states.append(kind),
                               sleep=lambda _s: None, pin_file=None)
        finally:
            main_module._verified_connection = original
        # Μία φορά «connecting» και μετά μόνο το αποτέλεσμα (όχι connecting ανά προσπάθεια).
        self.assertEqual(states, [st.CONNECTING, REJECTED, REJECTED, REJECTED])

    def test_stop_event_ends_loop(self):
        stop = threading.Event()
        stop.set()
        calls = []
        run_with_reconnect("127.0.0.1", 1, calls.append, tls_context=ssl.create_default_context(),
                           stop=stop, pin_file=None)
        self.assertEqual(calls, [])


class _Fake:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@skipUnless(HAVE_QT, "PySide6")
class TrayTests(TestCase):
    @classmethod
    def setUpClass(cls):
        from client.qt_client import setup_application
        cls.app = setup_application()

    def make(self, **initial):
        from client.qt_client import ClientTray
        self.dir = Path(tempfile.mkdtemp())
        status = st.ClientStatus(**initial)
        history = st.History(self.dir / st.HISTORY_FILE).load()
        tray = ClientTray(status, history, dump_file=self.dir / "dump.json",
                          tray_available=True)
        return status, history, tray

    def flush(self):
        self.app.processEvents()
        self.app.processEvents()

    def test_tray_pixmap_dot_top_right(self):
        from PySide6.QtGui import QColor
        from client.qt_client import tray_pixmap
        from shared.ui import theme
        for size in (16, 32, 48):
            image = tray_pixmap(size, "ok").toImage()
            outer = size * (theme.TRAY_DOT + theme.TRAY_DOT_RING)
            center = image.pixelColor(round(size - outer), round(outer))
            self.assertEqual(center.name(), QColor(theme.ONLINE).name(), size)

    def test_tray_pixmap_monochrome_by_taskbar(self):
        from client.qt_client import tray_pixmap
        from shared.ui import theme
        size = 64
        side = size * theme.TRAY_APP_SCALE
        middle = (round(side / 2), round(size - side / 2))     # κεντρική κουκκίδα κυμάτων
        for dark, color in ((False, theme.TRAY_GLYPH_LIGHT), (True, theme.TRAY_GLYPH_DARK)):
            image = tray_pixmap(size, "ok", dark).toImage()
            self.assertEqual(image.pixelColor(*middle).name(), color, dark)
            # Καμία απόχρωση teal: μόνο μαύρο/λευκό, διάφανο ή το χρώμα της κουκκίδας.
            allowed = {color, theme.ONLINE.lower()}
            for y in range(size):
                for x in range(size):
                    pixel = image.pixelColor(x, y)
                    if pixel.alpha() == 255:
                        self.assertIn(pixel.name(), allowed, (x, y))
            # Διάφανο κενό ανάμεσα στην κουκκίδα και στα κύματα, και πάνω αριστερά.
            radius = size * theme.TRAY_DOT
            outer = radius + size * theme.TRAY_DOT_RING
            self.assertEqual(image.pixelColor(1, 1).alpha(), 0)
            gap = image.pixelColor(round(size - outer), round(outer + (radius + outer) / 2))
            self.assertEqual(gap.alpha(), 0)

    def test_tray_icon_follows_taskbar_theme(self):
        status, _history, tray = self.make(room="C2627-Γ1", name="Board1")
        status.set_state(st.CONNECTED)
        tray.taskbar_dark = lambda: False
        tray.refresh()
        self.assertFalse(tray.dump()["tray_dark"])
        tray.taskbar_dark = lambda: True
        tray.refresh()
        self.assertTrue(tray.dump()["tray_dark"])
        self.assertEqual(tray.dump()["tone"], "ok")

    def test_menu_has_no_exit_and_window_hides_on_close(self):
        status, _history, tray = self.make(room="C2627-Γ1", name="Board1")
        status.set_state(st.CONNECTED)
        self.flush()
        dump = tray.dump()
        self.assertEqual([item["text"] for item in dump["menu"]],
                         ["C2627-Γ1 · Board1", "Συνδεδεμένος", "Ιστορικό…", "Πληροφορίες…"])
        self.assertEqual([item["enabled"] for item in dump["menu"]], [False, False, True, True])
        self.assertNotIn("Έξοδος", json.dumps(dump, ensure_ascii=False))
        self.assertEqual(dump["tooltip"], "C2627-Γ1 · Board1 — Συνδεδεμένος")
        self.assertEqual(dump["tone"], "ok")
        self.assertFalse(dump["window"]["visible"])
        tray.open_window()
        self.assertTrue(tray.window.isVisible())
        tray.window.close()
        self.assertFalse(tray.window.isVisible())
        self.assertIsNotNone(QApplication.instance())
        saved = json.loads((self.dir / "dump.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["state"], st.CONNECTED)

    def test_pending_opens_window_once_with_code(self):
        enrollment = {"code": "482913", "expires_at": time.time() + DAY, "room": "C2627-Γ1",
                      "name": "Board1"}
        status, _history, tray = self.make(room="C2627-Γ1", name="Board1",
                                           enrollment=enrollment)
        status.set_state(st.PENDING)
        self.flush()
        window = tray.dump()["window"]
        self.assertTrue(window["visible"])
        self.assertEqual(window["code"], "482 913")
        self.assertTrue(window["code_expires"].startswith("Λήγει αύριο"))
        self.assertEqual(window["status_tone"], "wait")
        tray.window.hide()
        status.set_state(st.CONNECTING)
        status.set_state(st.PENDING)
        self.flush()
        self.assertFalse(tray.window.isVisible())     # ίδιος κωδικός: όχι ξανά
        status.set_state(st.CONNECTED)
        self.flush()
        self.assertIsNone(tray.dump()["window"]["code"])

    def test_history_rows_and_tabs(self):
        status, history, tray = self.make(room="C2627-Γ1", name="Board1")
        window = tray.window
        self.assertEqual(window.dump()["tabs"], ["Ιστορικό", "Πληροφορίες"])
        self.assertEqual(window.dump()["history"], [{"empty": st.EMPTY_HISTORY}])
        history.begin()
        status.update(live_since=time.time())
        self.flush()
        rows = window.dump()["history"]
        self.assertTrue(rows[0]["live"])
        self.assertTrue(rows[0]["day"].startswith("Σήμερα"))
        history.end()
        status.update(live_since=None)
        self.flush()
        rows = window.dump()["history"]
        self.assertFalse(rows[0]["live"])
        self.assertEqual(rows[0]["kind"], "Ζωντανή ανακοίνωση")
        self.assertTrue(window.range.isVisibleTo(window) or window.range.isHidden() is False)
        window.show_tab("Πληροφορίες")
        self.assertTrue(window.range.isHidden())
        labels = [row["label"] for row in window.dump()["info_rows"]]
        self.assertIn("ID υπολογιστή", labels)

    def test_bars(self):
        status, _history, tray = self.make(room="C2627-Γ1", name="Board1")
        status.set_state(st.REVOKED)
        self.flush()
        self.assertIn("ανακλήθηκε", tray.window.dump()["alarm"])
        self.assertEqual(tray.dump()["tone"], "bad")
        status.set_state(st.FULL)
        self.flush()
        dump = tray.window.dump()
        self.assertIsNone(dump["alarm"])
        self.assertIn("όριο", dump["info"])

    def test_updates_from_other_thread_are_queued(self):
        status, _history, tray = self.make(room="C2627-Γ1", name="Board1")
        worker = threading.Thread(target=lambda: status.set_state(st.CONNECTED))
        worker.start()
        worker.join()
        self.flush()
        self.assertEqual(tray.dump()["tooltip"], "C2627-Γ1 · Board1 — Συνδεδεμένος")


@skipUnless(HAVE_QT, "PySide6")
class ChangeWindowTests(TestCase):
    """5C2: αριστερή μπάρα, «Αλλαγή…», Κλίμακα, εικονίδιο στους διαλόγους, μενού tray."""

    @classmethod
    def setUpClass(cls):
        from client.qt_client import setup_application
        cls.app = setup_application()

    def flush(self):
        self.app.processEvents()
        self.app.processEvents()

    def make(self, **initial):
        from client.qt_client import ClientTray
        self.dir = Path(tempfile.mkdtemp())
        status = st.ClientStatus(**initial)
        history = st.History(self.dir / st.HISTORY_FILE).load()
        self.changes, self.scales, self.dialogs = [], [], []
        tray = ClientTray(status, history, tray_available=True,
                          on_change=lambda r, n: self.changes.append((r, n)),
                          on_scale=lambda s: (self.scales.append(s), status.update(scale=s)))
        self.addCleanup(tray.window.deleteLater)
        return status, history, tray

    def test_sidebar_titles_and_info_buttons(self):
        status, _history, tray = self.make(room="C2627-Γ1", name="Board1", scale=90)
        status.set_state(st.CONNECTED)
        self.flush()
        window = tray.window.dump()
        self.assertEqual(window["window_title"], "School Intercom — C2627-Γ1 · Board1")
        self.assertEqual(window["brand_sub"], "Client")
        self.assertEqual(window["title"], "C2627-Γ1 · Board1")
        self.assertEqual(window["page_title"], "Ιστορικό")
        tray.window.show_tab("Πληροφορίες")
        self.assertEqual(tray.window.dump()["page_title"], "Πληροφορίες")
        rows = {row["label"]: row for row in tray.window.dump()["info_rows"]}
        labels = list(rows)
        self.assertEqual(labels[:5], ["Κατάσταση", "Αίθουσα", "Υπολογιστής", "Κλίμακα",
                                      "Director"])
        self.assertEqual(rows["Υπολογιστής"]["action"], st.ACTION_CHANGE)
        self.assertEqual(rows["Κλίμακα"]["action"], st.ACTION_SCALE)
        self.assertEqual(rows["Κλίμακα"]["value"], "90%")
        for action in (st.ACTION_CHANGE, st.ACTION_SCALE):
            button = tray.window.info.action_button(action)
            self.assertFalse(button.isHidden())
            self.assertEqual(button.text(), "Αλλαγή…")
        self.assertFalse(any("Manager" in str(row) for row in tray.window.dump()["info_rows"]))

    def test_tray_menu_opens_tabs(self):
        status, _history, tray = self.make(room="C2627-Γ1", name="Board1")
        tray.window.show_tab("Πληροφορίες")
        tray.menu.actions()[-1].trigger()            # «Πληροφορίες…»
        self.assertEqual(tray.window.current_tab, "Πληροφορίες")
        tray.open_action.trigger()                   # «Ιστορικό…»
        self.assertEqual(tray.window.current_tab, "Ιστορικό")
        tray.window.show_tab("Πληροφορίες")
        from PySide6.QtWidgets import QSystemTrayIcon
        tray._activated(QSystemTrayIcon.ActivationReason.DoubleClick)
        self.assertEqual(tray.window.current_tab, "Ιστορικό")
        tray.window.hide()

    def test_change_dialog_by_state_with_app_icon(self):
        from client.qt_client import ChangeDialog
        status, _history, tray = self.make(room="C2627-Γ1", name="Board1")
        status.set_state(st.CONNECTED)
        seen = []

        def hook(dialog):
            seen.append(dialog)
            self.assertIsInstance(dialog, ChangeDialog)
            self.assertFalse(dialog.windowIcon().isNull())     # εικονίδιο στη γραμμή τίτλου
            self.assertEqual(dialog.windowTitle(), st.CHANGE_TITLE)
            self.assertEqual(dialog.accept_button.text(), "Αποστολή αιτήματος")
            self.assertEqual(dialog.room.text(), "C2627-Γ1")
            dialog.room.setText("  C2627-Γ2 ")
            dialog.name.setText("")
            dialog._accept()
            self.assertFalse(dialog.error.isHidden())            # κενό όνομα: μένει ανοιχτός
            dialog.name.setText("Board 2")
            dialog._accept()
            return dialog.result()
        tray.dialog_hook = hook
        tray.action(st.ACTION_CHANGE)
        self.assertEqual(self.changes, [("C2627-Γ2", "Board 2")])
        for state, extra, text in ((st.REVOKED, {}, "Νέα αίτηση εγγραφής"),
                                   (st.PENDING, {"enrollment": {"code": "482913",
                                                                "expires_at": 9e9}},
                                    "Διόρθωση αιτήματος")):
            status.update(**extra)
            status.set_state(state)
            tray.dialog_hook = lambda d, t=text: (self.assertEqual(d.accept_button.text(), t), 0)[1]
            tray.action(st.ACTION_CHANGE)
        self.assertEqual(len(self.changes), 1)                   # Άκυρο: τίποτα

    def test_scale_dialog_applies_without_reconnect(self):
        from client.qt_client import ScaleDialog
        from shared.ui import theme
        status, _history, tray = self.make(room="C2627-Γ1", name="Board1", scale=100)
        tray.window.show_tab("Πληροφορίες")
        old = tray.window

        def hook(dialog):
            self.assertIsInstance(dialog, ScaleDialog)
            self.assertFalse(dialog.windowIcon().isNull())
            self.assertEqual([b.text() for b in dialog.buttons.values()],
                             [f"{v}%" for v in theme.UI_SCALES])
            self.assertTrue(dialog.buttons[100].isChecked())
            dialog.buttons[125].click()
            return 1
        tray.dialog_hook = hook
        try:
            tray.action(st.ACTION_SCALE)
            self.assertEqual(self.scales, [125])
            self.assertIsNot(tray.window, old)
            self.assertEqual(tray.window.current_tab, "Πληροφορίες")
            rows = {r["label"]: r for r in tray.window.dump()["info_rows"]}
            self.assertEqual(rows["Κλίμακα"]["value"], "125%")
        finally:
            from client.qt_client import setup_application
            setup_application(ui_scale=theme.default_ui_scale())
