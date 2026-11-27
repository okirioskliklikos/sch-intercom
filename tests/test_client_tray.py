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
        self.assertEqual(rows["Αποτύπωμα Manager"]["value"], "84:28:5C:A1 … 7E:41:9D:33")
        self.assertTrue(rows["Αποτύπωμα Manager"]["mono"])
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
            self.assertEqual(center.name(), QColor(theme.TRAY_OK).name(), size)

    def test_menu_has_no_exit_and_window_hides_on_close(self):
        status, _history, tray = self.make(room="C2627-Γ1", name="Board1")
        status.set_state(st.CONNECTED)
        self.flush()
        dump = tray.dump()
        self.assertEqual([item["text"] for item in dump["menu"]],
                         ["C2627-Γ1 · Board1", "Συνδεδεμένος", "Άνοιγμα"])
        self.assertEqual([item["enabled"] for item in dump["menu"]], [False, False, True])
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
