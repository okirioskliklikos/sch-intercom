"""4.2D2: Κύριος/Εφεδρικός στο GUI — λογική (RoleStatus, εισαγωγή μητρώου, κρυφό
αποτύπωμα) και παράθυρο offscreen (μπάρα, ειδοποιήσεις, κάρτα ρόλου, κλείδωμα, διάλογοι).

Το παράθυρο χτίζεται όπως στο tools/security_demo.py: πραγματικό μητρώο, εγγραφή σε
127.0.0.1 και ο πραγματικός ServingController, με ψεύτικο δίκτυο και ψεύτικο «άλλο Manager».
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from shared.ui.offscreen import use_offscreen
use_offscreen()  # offscreen· στα Windows και γραμματοσειρές του συστήματος

from director import maintenance as m
from director import manager_state
from director.security import (TEXT_CLOSED_BACKUP, TEXT_CLOSED_STOPPED, TEXT_NO_OTHER,
                               OtherManager, RoleModel, RoleStatus)
from shared import enrollment as enrollment_module

try:
    from PySide6.QtWidgets import QApplication, QLabel
    from director.gui import DirectorMicrophone
    from director.qt_gui import DirectorWindow, create_application, release_window
    from director import qt_maintenance, qt_role
    from tools import maintenance_demo, security_demo
except ImportError as exc:  # pragma: no cover - εξαρτάται από το περιβάλλον
    QT_ERROR = str(exc)
else:
    QT_ERROR = None

FINGERPRINT = ":".join(f"{i:02X}" for i in range(32))
OTHER = OtherManager("ΓΡΑΜΜΑΤΕΙΑ-2", "192.168.1.35", 17, "inst-2")


def status(role="primary", serving=True, notice=None, others=(), version=15):
    return RoleStatus(role, serving, notice, tuple(others), version, "ΓΡΑΜΜΑΤΕΙΑ-PC")


class FingerprintTests(unittest.TestCase):
    def test_masked_keeps_full_length_and_two_lines(self):
        masked = m.fingerprint_masked(FINGERPRINT)
        shown = m.fingerprint_lines(FINGERPRINT)
        first, second = masked.split("\n")
        self.assertEqual(len(masked), len(shown))
        self.assertEqual(first.split(":")[:2], ["00", "01"])
        self.assertEqual(second.split(":")[-2:], ["1E", "1F"])
        self.assertEqual(len(first.split(":")), 16)
        self.assertEqual(len(second.split(":")), 16)
        middle = first.split(":")[2:] + second.split(":")[:-2]
        self.assertEqual(set(middle), {m.MASK})
        self.assertNotIn("…", masked)


class RoleStatusTests(unittest.TestCase):
    def test_header_states(self):
        self.assertEqual(status().header(), ("Συνδεδεμένος (Κύριος)", "up"))
        self.assertEqual(status("backup").header(), ("Συνδεδεμένος (Εφεδρικός)", "up"))
        self.assertEqual(status("backup", False).header(),
                         ("Χωρίς εξυπηρέτηση (Εφεδρικός)", "stopped"))
        self.assertEqual(status(others=[OTHER]).header(),
                         ("Δύο Manager σε λειτουργία (Κύριος)", "conflict"))
        # Όσο δεν εξυπηρετούμε δεν υπάρχει σύγκρουση.
        self.assertFalse(status(serving=False, others=[OTHER]).conflict)

    def test_serving_text_without_dots(self):
        self.assertEqual(status().serving_text(), ("Ενεργή", "ok"))
        self.assertEqual(status(serving=False).serving_text(), ("Σταματημένη", "err"))

    def test_other_manager_texts(self):
        self.assertEqual(status().others_html(), (TEXT_NO_OTHER, "muted"))
        same = OtherManager("ΓΡΑΜΜΑΤΕΙΑ-2", "192.168.1.35", 15, "i")
        text, tone = status(others=[same]).others_html()
        self.assertEqual((text, tone), ("ΓΡΑΜΜΑΤΕΙΑ-2 · 192.168.1.35 · εξυπηρετεί", "err"))
        text, tone = status("backup", False, others=[OTHER]).others_html()
        self.assertIn("μητρώο v17, εδώ v15", text)
        self.assertEqual(tone, "")
        self.assertEqual(status(others=[OTHER]).newer, (OTHER,))
        self.assertEqual(status(others=[same]).newer, ())

    def test_bars_escape_host_names(self):
        evil = OtherManager("<b>x</b>", "10.0.0.1", 15, "i")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", status(others=[evil]).conflict_html())
        self.assertEqual(status("backup", False, "restored_as_backup").restored_html(),
                         "Επαναφορά από backup: ο υπολογιστής είναι <b>Εφεδρικός</b> "
                         "και δεν εξυπηρετεί.")
        self.assertEqual(status("backup", False, others=[OTHER]).newer_html(),
                         "Το μητρώο χρειάζεται ενημέρωση: ο ΓΡΑΜΜΑΤΕΙΑ-2 έχει v17, εδώ v15.")
        self.assertIn("Εισαγάγετέ το πρώτα", status(others=[OTHER]).newer_warning())

    def test_enrollment_closed_reason(self):
        self.assertIsNone(status().enrollment_closed)
        self.assertEqual(status("backup").enrollment_closed, TEXT_CLOSED_BACKUP)
        self.assertEqual(status(serving=False).enrollment_closed, TEXT_CLOSED_STOPPED)


class FakeController:
    def __init__(self):
        self.active = False
        self.calls = []

    def start(self, force=False):
        self.calls.append(("start", force))
        self.active = True

    def stop(self):
        self.calls.append(("stop",))
        self.active = False

    def role_changed(self):
        self.calls.append(("role_changed",))


class RoleModelTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "manager-state.json"
        manager_state.restored(self.path)
        self.controller = FakeController()
        self.watcher = mock.Mock()
        self.watcher.others.return_value = []
        self.model = RoleModel(self.path, self.controller, self.watcher,
                               registry_version=lambda: 3, host="PC")

    def test_status_and_actions(self):
        current = self.model.status()
        self.assertEqual((current.role, current.serving, current.restored), ("backup", False, True))
        self.model.acknowledge()
        self.assertFalse(self.model.status().restored)
        self.model.promote()
        self.assertEqual(manager_state.load(self.path).role, "primary")
        self.assertIn(("role_changed",), self.controller.calls)
        self.model.start()
        self.assertTrue(self.model.status().serving)
        self.model.demote()
        self.assertEqual(self.model.status().role, "backup")
        self.model.stop()
        self.assertEqual(self.controller.calls[-1], ("stop",))


class ImportViewTests(unittest.TestCase):
    @staticmethod
    def entry(cid, room, name="PC", status="approved", spki="a"):
        return {"client_id": cid, "room": room, "name": name, "status": status,
                "spki_sha256": spki}

    def registry(self, version, *clients):
        return {"registry_version": version, "clients": list(clients), "school_id": "s"}

    def test_newer_lists_changes(self):
        local = self.registry(15, self.entry("1", "Α1"), self.entry("2", "Γ2"),
                              self.entry("3", "Δ1"))
        incoming = self.registry(17, self.entry("1", "Α1", "Board"), self.entry("2", "Γ3"),
                                 self.entry("3", "Δ1", status="revoked"),
                                 self.entry("4", "Α3", "Board1"))
        view = m.import_view(Path("x.sicr"), local, incoming, incoming, "newer")
        self.assertEqual(view.version_text, "v17 · εδώ v15 · νεότερο ✓")
        self.assertEqual(view.version_tone, "ok")
        self.assertEqual(dict(view.changes), {
            "Προστίθενται": ("Α3 · Board1",), "Ανακαλούνται": ("Δ1 · PC",),
            "Αλλάζουν αίθουσα": ("Γ2 · PC → Γ3",), "Μετονομάζονται": ("Α1 · PC → Board",)})

    def test_conflict_lists_differences(self):
        local = self.registry(17, self.entry("1", "Γ3"))
        incoming = self.registry(16, self.entry("1", "Γ2"))
        view = m.import_view(Path("x.sicr"), local, incoming, local, "conflict")
        self.assertEqual(view.version_tone, "err")
        self.assertEqual(view.differences, ("Γ3 · PC: αίθουσα Γ3 εδώ, Γ2 στο αρχείο",))


@unittest.skipIf(QT_ERROR, f"PySide6: {QT_ERROR}")
class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_application()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        limit = enrollment_module.MAX_ATTEMPTS_PER_MINUTE
        self.addCleanup(setattr, enrollment_module, "MAX_ATTEMPTS_PER_MINUTE", limit)
        (self.broadcaster, self.model, self.enrollment, self.maintenance,
         _backup) = maintenance_demo.build(self.root, log=lambda *_: None)
        self.addCleanup(self.enrollment.stop)
        self.dialogs = []
        self.informed = []

    def window(self, scenario: str) -> DirectorWindow:
        self.role = security_demo.build_role(self.root, scenario,
                                             lambda: self.model.trust.registry.version,
                                             trust=self.model.trust)
        window = DirectorWindow(
            self.broadcaster, DirectorMicrophone(security_demo.fake_stream, self.broadcaster),
            security=self.model, maintenance=self.maintenance, role=self.role,
            maintenance_options={"inform": lambda *a: self.informed.append(a)},
            role_options={"dialog_hook": self.hook,
                          "inform": lambda *a: self.informed.append(a)},
            layout_settings=self.root / "director-gui.json")
        self.addCleanup(lambda: (window.close(), release_window(window)))
        window.resize(1366, 900)
        window.show()
        self.settle()
        return window

    def settle(self):
        for _ in range(4):
            QApplication.processEvents()

    def hook(self, dialog):
        self.dialogs.append(dialog)
        action = self.answer.pop(0) if getattr(self, "answer", None) else "reject"
        if action == "accept":
            dialog.accept()
        else:
            dialog.reject()

    def state(self):
        return manager_state.load(manager_state.path_for(self.root / "clients-registry.json"))

    def test_primary_header_and_no_bars(self):
        window = self.window("primary")
        self.assertEqual(window.connection_label.text(), "Συνδεδεμένος (Κύριος)")
        self.assertEqual(window.connection_label.property("state"), "up")
        self.assertFalse(window.connection_dot.hollow)
        for bar in (window.restored_bar, window.newer_bar, window.conflict_bar):
            self.assertFalse(bar.isVisible())

    def test_restored_bar_stay_backup(self):
        window = self.window("restored")
        self.assertEqual(window.connection_label.text(), "Χωρίς εξυπηρέτηση (Εφεδρικός)")
        self.assertTrue(window.connection_dot.hollow)
        self.assertTrue(window.restored_bar.isVisible())
        self.assertIn("<b>Εφεδρικός</b>", window.restored_bar.text.text())
        window.restored_bar.buttons[0].click()   # «Να μείνει Εφεδρικός»
        self.settle()
        self.assertIsNone(self.state().notice)
        self.assertEqual(self.state().role, "backup")
        self.assertFalse(window.restored_bar.isVisible())

    def test_restored_bar_promote_and_start(self):
        window = self.window("restored")
        self.answer = ["accept"]
        window.restored_bar.buttons[1].click()   # «Να γίνει Κύριος…»
        self.settle()
        dialog = self.dialogs[0]
        self.assertIsInstance(dialog, qt_role.PromoteDialog)
        self.assertTrue(dialog.start.isChecked())
        self.assertEqual(self.state().role, "primary")
        self.assertTrue(self.state().serving)
        self.assertEqual(window.connection_label.text(), "Συνδεδεμένος (Κύριος)")
        self.assertFalse(window.restored_bar.isVisible())

    def test_conflict_bar_and_stop(self):
        window = self.window("conflict")
        self.assertEqual(window.connection_label.property("state"), "conflict")
        self.assertTrue(window.conflict_bar.isVisible())
        self.assertIn("ΓΡΑΜΜΑΤΕΙΑ-2", window.conflict_bar.text.text())
        self.assertTrue(window.conflict_stop.isVisible())
        self.assertEqual(window.conflict_stop.text(), "Διακοπή εξυπηρέτησης εδώ")
        window.show_page("security")
        window.security_page.show_tab("Πιστοποιητικά")
        self.settle()
        # Και στα «Πιστοποιητικά» (απόφαση χρήστη 23/11/2026), μαζί με το κουμπί της κάρτας.
        self.assertTrue(window.conflict_stop.isVisible())
        card = window.security_page.role_card
        self.assertEqual(card.serve_button.text(), "Διακοπή εξυπηρέτησης εδώ")
        self.assertEqual(card.serve_button.objectName(), "dangerAction")
        window.conflict_stop.click()             # Άκυρο: τίποτα δεν αλλάζει
        self.settle()
        self.assertIsInstance(self.dialogs[-1], qt_role.StopDialog)
        self.assertEqual(self.dialogs[-1].accept_button.objectName(), "dangerAction")
        self.assertIn("ΓΡΑΜΜΑΤΕΙΑ-2", " ".join(
            w.text() for w in self.dialogs[-1].findChildren(QLabel)))
        self.assertTrue(self.state().serving)
        self.answer = ["accept"]
        window.conflict_stop.click()
        self.settle()
        self.assertFalse(self.state().serving)
        self.assertFalse(window.conflict_bar.isVisible())
        self.assertEqual(window.connection_label.text(), "Χωρίς εξυπηρέτηση (Κύριος)")
        # Η κάρτα ενημερώθηκε μαζί με την μπάρα.
        self.assertEqual(card.serve_button.text(), "Έναρξη εξυπηρέτησης")
        self.assertEqual(card.rows.values["serving"].text(), "Σταματημένη")

    def test_start_conflict_dialog_then_force(self):
        window = self.window("newer")
        self.assertTrue(window.newer_bar.isVisible())
        self.assertIn("έχει v", window.newer_bar.text.text())
        card = window.security_page.role_card
        self.assertEqual(card.serve_button.text(), "Έναρξη εξυπηρέτησης")
        card.serve_button.click()            # Άκυρο
        self.settle()
        self.assertIsInstance(self.dialogs[-1], qt_role.StartConflictDialog)
        self.assertEqual(self.dialogs[-1].accept_button.objectName(), "dangerAction")
        self.assertFalse(self.state().serving)
        self.answer = ["accept"]
        card.serve_button.click()            # «Έναρξη παρ' όλα αυτά»
        self.settle()
        self.assertTrue(self.state().serving)
        self.assertTrue(self.role.controller.active)

    def test_backup_locks_computers_tab(self):
        window = self.window("backup")
        window.show_page("security")
        self.settle()
        page = window.security_page
        self.assertTrue(page.closed_card.isVisible())
        self.assertEqual(page.closed_note.label.text(), TEXT_CLOSED_BACKUP)
        self.assertFalse(page.requests_box.isVisible())
        self.assertTrue(page.requests_lock.isVisible())
        self.assertTrue(page.computers_lock.isVisible())
        menus = [r.menu_button for r in page.computer_rows.values()]
        menus += [h.menu_button for h in page.room_headers.values()]
        self.assertTrue(menus)
        self.assertFalse(any(button.isVisible() for button in menus))
        self.assertTrue(page.side_by_side)
        # Αλλαγή σε Κύριο: τα μενού ⋮ και τα αιτήματα επιστρέφουν.
        self.answer = ["accept"]
        page.role_card.role_button.click()
        self.settle()
        window.refresh_role()
        self.settle()
        self.assertEqual(self.state().role, "primary")
        self.assertFalse(page.closed_card.isVisible())
        self.assertTrue(page.requests_box.isVisible())
        self.assertTrue(all(button.isVisible() for button in menus))

    def test_card_stop_asks_first(self):
        window = self.window("primary")
        window.show_page("security")
        window.security_page.show_tab("Πιστοποιητικά")
        card = window.security_page.role_card
        card.serve_button.click()            # Άκυρο
        self.assertIsInstance(self.dialogs[-1], qt_role.StopDialog)
        self.assertTrue(self.state().serving)
        self.answer = ["accept"]
        card.serve_button.click()
        self.settle()
        self.assertFalse(self.state().serving)
        self.assertEqual(card.serve_button.text(), "Έναρξη εξυπηρέτησης")

    def test_demote_dialog(self):
        window = self.window("primary")
        window.show_page("security")
        window.security_page.show_tab("Πιστοποιητικά")
        self.settle()
        card = window.security_page.role_card
        self.assertEqual(card.role_button.text(), "Αλλαγή σε Εφεδρικό…")
        card.role_button.click()             # Άκυρο
        self.assertEqual(self.state().role, "primary")
        self.answer = ["accept"]
        card.role_button.click()
        self.settle()
        self.assertIsInstance(self.dialogs[-1], qt_role.DemoteDialog)
        self.assertEqual(self.state().role, "backup")
        self.assertEqual(window.connection_label.text(), "Συνδεδεμένος (Εφεδρικός)")

    def test_registry_card_export_and_import(self):
        window = self.window("primary")
        window.show_page("settings")
        self.settle()
        page = window.settings_page
        rows = page.registry_rows
        version = self.model.trust.registry.version
        self.assertTrue(rows.values["registry"].text().startswith(f"v{version} · "))
        self.assertEqual(rows.values["unexported"].text(), "1 αλλαγή που δεν έχει εξαχθεί")
        out = self.root / "usb" / "export.sicr"
        page.choose_save = lambda _parent, _current: out
        page.export_button.click()
        self.assertTrue(out.exists())
        self.assertEqual(rows.values["unexported"].text(), m.TEXT_UNEXPORTED_NONE)
        self.assertIn(f"v{version}", rows.values["last"].text())
        # Εισαγωγή νεότερου μητρώου από τον «άλλο υπολογιστή».
        files = self.maintenance.import_files
        seen = []

        def accept(dialog):
            seen.append((dialog.view.relation, dialog.apply_button.text(),
                         dict(dialog.view.changes)))
            dialog.run()
        page.dialog_hook = accept
        page.open_import(files["newer"])
        relation, button, changes = seen[0]
        self.assertEqual((relation, button), ("newer", "Εισαγωγή"))
        self.assertIn("Ανακαλούνται", changes)
        self.assertEqual(self.model.trust.registry.version, version + 2)
        self.assertEqual(self.informed[-1][0], "Εισαγωγή μητρώου")

    def test_import_conflict_merges_with_choice(self):
        window = self.window("primary")
        window.show_page("settings")
        page = window.settings_page
        seen = []

        def merge(dialog):
            dialog.set_prefer("incoming")
            seen.append((dialog.view.relation, dialog.apply_button.text(),
                         dialog.prefer_box.isVisibleTo(dialog), dialog.view.prefer))
            dialog.run()
        page.dialog_hook = merge
        before = self.model.trust.registry.version
        page.open_import(self.maintenance.import_files["conflict"])
        self.assertEqual(seen, [("conflict", "Συγχώνευση", True, "incoming")])
        self.assertGreater(self.model.trust.registry.version, before)

    def test_newer_bar_opens_import(self):
        window = self.window("newer")
        opened = []
        window.settings_page.dialog_hook = lambda dialog: opened.append(dialog) or \
            dialog.reject()
        importing = next(b for b in window.newer_bar.buttons
                         if b.text() == qt_role.IMPORT_REGISTRY)
        self.assertFalse(window.accept_current.isVisibleTo(window))  # beacon, όχι Clients
        importing.click()
        self.assertEqual(window.current_page, "settings")
        self.assertIsInstance(opened[0], qt_maintenance.ImportDialog)

    # --- 4.2D3: νεότερο μητρώο που ανέφεραν Clients ------------------------------
    def test_reported_bar_and_accept_current(self):
        before = self.model.trust.registry.version
        window = self.window("reported")
        self.assertTrue(window.newer_bar.isVisibleTo(window))
        self.assertIn(f"Υπάρχει νεότερο μητρώο (v{before + 3})", window.newer_bar.text.text())
        self.assertTrue(window.accept_current.isVisibleTo(window))
        self.assertEqual(window.accept_current.text(), qt_role.ACCEPT_CURRENT)
        self.answer = ["reject"]
        window.accept_current.click()
        dialog = self.dialogs[-1]
        self.assertIsInstance(dialog, qt_role.AcceptCurrentDialog)
        self.assertEqual(dialog.accept_button.objectName(), "dangerAction")
        self.assertIn("θα θεωρηθεί χαμένο", dialog.warning.text())
        self.assertEqual(self.model.trust.registry.version, before)
        self.answer = ["accept"]
        window.accept_current.click()
        self.settle()
        self.assertEqual(self.model.trust.registry.version, before + 4)
        self.assertFalse(window.newer_bar.isVisibleTo(window))
        self.assertFalse(window.accept_current.isVisibleTo(window))

    def test_divergent_text(self):
        window = self.window("divergent")
        self.assertIn("διαφορετικό μητρώο", window.newer_bar.text.text())
        self.assertTrue(window.accept_current.isVisibleTo(window))

    def test_accept_current_hidden_on_backup(self):
        window = self.window("reported")
        self.role.demote()
        window.refresh_role()
        self.assertTrue(window.newer_bar.isVisibleTo(window))
        self.assertFalse(window.accept_current.isVisibleTo(window))


if __name__ == "__main__":
    unittest.main()
