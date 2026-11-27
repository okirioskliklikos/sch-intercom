"""Φάση 5B3: Ρυθμίσεις → «Ενημερώσεις» (λογική και παράθυρο offscreen) και πακέτα στο backup.

Πραγματικό UpdateStore/UpdateStatus σε προσωρινό φάκελο, πακέτα υπογεγραμμένα με κλειδί
δοκιμής. Τα Qt tests παραλείπονται χωρίς PySide6.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tempfile
import time
import unittest

from shared.ui.offscreen import use_offscreen
use_offscreen()

from director import maintenance as m
from director import updates_view as uv
from director.updates import STATUS_FILE, UpdateError, UpdateStatus, UpdateStore, updates_dir
from tests.test_maintenance import N, PASSWORD, Manager
from tests.test_updates import Packages

try:
    from PySide6.QtWidgets import QDialog
    from director.qt_gui import create_application
    from director.qt_maintenance import TAB_UPDATES, SettingsPage
    from director.qt_updates import ImportPackageDialog, UpdatesPanel
except ImportError as exc:  # pragma: no cover - εξαρτάται από το περιβάλλον
    QT_ERROR = str(exc)
else:
    QT_ERROR = None

NOW = 1_800_000_000.0


@dataclass
class Computer:
    client_id: str
    name: str
    room: str
    online: bool
    last_seen: float | None = None
    duplicate: tuple = ()


@dataclass
class Room:
    name: str
    computers: list = field(default_factory=list)


class Fixture:
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.packages = Packages(self.root / "packages")
        self.packages.root.mkdir()
        self.store = UpdateStore(self.root / "updates", trusted_keys=[self.packages.public])
        self.status = UpdateStatus(self.root / "updates" / STATUS_FILE)
        self.primary = True
        self.room = Room("C2627-Α1")
        self.model = uv.UpdatesModel(self.store, self.status, rooms=lambda: [self.room],
                                     primary=lambda: self.primary, clock=lambda: NOW)

    def add(self, name: str, *, online=True, last_seen=None, **status) -> str:
        client_id = f"id-{name}"
        self.room.computers.append(Computer(client_id, name, self.room.name, online, last_seen))
        if status:
            self.status.update(client_id, **status)
        return client_id

    def row(self, name: str) -> uv.ComputerUpdate:
        return {row.name: row for row in self.model.computers()}[name]

    def import_versions(self, *versions):
        for version in versions:
            self.store.import_package(self.packages.make(version, size=1))


class LogicTests(Fixture, unittest.TestCase):
    def test_package_card_current_previous_and_missing(self):
        info = self.model.package()
        self.assertIsNone(info.current)
        self.assertEqual(info.previous, "—")
        self.import_versions("1.0.5", "1.0.6", "1.0.7")
        info = self.model.package()
        self.assertTrue(info.current.startswith("1.0.7 · "))
        self.assertEqual(info.previous, "1.0.6")
        self.assertIsNone(info.missing)
        self.assertEqual(sorted(p.name for p in self.store.folder.glob("*.sicu")),
                         ["sch-intercom-client-1.0.6.sicu", "sch-intercom-client-1.0.7.sicu"])
        self.store.package_path("1.0.7").unlink()
        info = self.model.package()
        self.assertIn("Λείπει το αρχείο του πακέτου 1.0.7", info.missing)
        self.assertEqual(info.missing_file, "sch-intercom-client-1.0.7.sicu")

    def test_states_follow_reports_and_connection(self):
        self.import_versions("1.0.6")
        self.add("ok", app="1.0.6", state="current")
        self.add("dl", app="1.0.5", state="downloading", available="1.0.6", progress=45)
        self.add("wait", app="1.0.5", state="waiting", available="1.0.6")
        self.add("slot", app="1.0.5", state="available", available="1.0.6",
                 detail="αναμονή σειράς λήψης")
        self.add("switch", app="1.0.5", state="ready", available="1.0.6")
        self.add("fail", app="1.0.5", state="failed", available="1.0.6",
                 detail="Η 1.0.6 δεν συνδέθηκε σε 90 s · ξανά η 1.0.5", updated_at=0)
        self.add("off", online=False, last_seen=NOW - 2 * 86400, app="1.0.4", state="current")
        self.add("never", online=False)
        self.add("new", app="1.0.5", state="available", available="1.0.6")
        expected = {"ok": ("Ενημερωμένος", "ok"), "dl": ("Λήψη 45%", "run"),
                    "wait": (uv.TEXT_WAIT_ANNOUNCEMENT, "wait"),
                    "slot": (uv.TEXT_WAIT_SLOT, "wait"),
                    "switch": ("Εναλλαγή σε 1.0.6…", "run"),
                    "fail": ("⚠ Αποτυχία 1.0.6", "fail"),
                    "off": ("Εκτός σύνδεσης · πριν 2 ημέρες", "off"),
                    "never": (uv.TEXT_NEVER, "off"),
                    "new": ("Διαθέσιμη η 1.0.6", "available")}
        for name, (text, tone) in expected.items():
            with self.subTest(name=name):
                self.assertEqual((self.row(name).text, self.row(name).tone), (text, tone))
        self.assertEqual(self.row("dl").progress, 45)
        self.assertIn("δεν συνδέθηκε σε 90 s", self.row("fail").tooltip)
        self.assertIn(uv.TEXT_NO_RETRY, self.row("fail").tooltip)
        self.assertEqual(self.row("never").version, "—")
        self.assertTrue(self.row("new").can_start)
        self.assertFalse(self.row("ok").can_start)
        self.assertFalse(self.row("fail").can_start)
        self.assertEqual(self.model.counters(self.model.computers()),
                         [(1, "ενημερωμένοι"), (1, "διαθέσιμη"), (4, "σε εξέλιξη"),
                          (1, "αποτυχία"), (2, "εκτός σύνδεσης")])

    def test_versions_for_room_check(self):
        a = self.add("a", app="1.0.6", state="current")
        self.add("never")
        self.assertEqual(self.model.versions(), (None, {a: "1.0.6"}))
        self.import_versions("1.0.6")
        self.assertEqual(self.model.versions(), ("1.0.6", {a: "1.0.6"}))

    def test_without_package_counts_versions(self):
        self.add("a", app="1.0.0", state="current")
        self.add("b", online=False, last_seen=NOW - 600, app="1.0.0", state="current")
        self.add("c", online=False)
        rows = self.model.computers()
        self.assertEqual([r.text for r in rows],
                         ["Online", "Εκτός σύνδεσης · πριν 10 λεπτά", uv.TEXT_NEVER])
        self.assertEqual(self.model.counters(rows, imported=False),
                         [(2, "με την 1.0.0"), (1, "δεν έχει συνδεθεί")])

    def test_actions_only_on_primary(self):
        self.import_versions("1.0.6")
        client = self.add("pc", app="1.0.5", state="available", available="1.0.6")
        self.model.set_auto(False)
        self.model.start(client)
        config = self.store.config()
        self.assertFalse(config["auto"])
        self.assertEqual(config["start"]["clients"], [client])
        self.model.start_all()
        self.assertTrue(self.store.config()["start"]["all"])
        self.primary = False
        self.assertFalse(self.row("pc").can_start)
        for action in (lambda: self.model.set_auto(True), self.model.start_all,
                       lambda: self.model.start(client),
                       lambda: self.model.import_package(self.packages.make("1.0.7"))):
            with self.assertRaisesRegex(UpdateError, "μόνο ο Κύριος"):
                action()
        self.assertEqual(self.store.current()["version"], "1.0.6")

    def test_import_view_shows_changes_or_error(self):
        view = self.model.inspect(self.packages.make("1.0.6", size=1))
        self.assertIsNone(view.error)
        labels = [label for label, _text, _tone in view.rows]
        self.assertEqual(labels, ["Αρχείο", "Έκδοση", "Υπογραφή", "Αλλαγές"])
        self.assertIn("(πρώτο πακέτο)", view.rows[3][1])
        self.assertEqual(view.note, uv.TEXT_AUTO_ON_NOTE)
        self.import_versions("1.0.6")
        view = self.model.inspect(self.packages.make("1.0.7", size=2))
        # Αλλάζουν VERSION και app/blob.bin· τα υπόλοιπα 2 όχι.
        self.assertTrue(view.rows[3][1].startswith("2 από 4 αρχεία σε σχέση με την 1.0.6"),
                        view.rows[3][1])
        view = self.model.inspect(self.packages.make("1.0.5"))
        self.assertEqual(view.error, "Η έκδοση 1.0.5 δεν είναι νεότερη από την τρέχουσα 1.0.6.")
        self.assertIsNone(view.note)
        self.store.set_auto(False)
        self.assertEqual(self.model.inspect(self.packages.make("1.0.8")).note,
                         uv.TEXT_AUTO_OFF_NOTE)

    def test_number_text_uses_greek_thousands(self):
        self.assertEqual(uv.number_text(1912), "1.912")

    def test_restore_row_lists_versions_and_missing(self):
        self.assertIsNone(m.updates_row(None))
        info = {"versions": ["1.0.6", "1.0.5"], "current": "1.0.6", "missing": [],
                "copy_available": []}
        self.assertEqual(m.updates_row(info), ("Ενημερώσεις", "1.0.6 (τρέχουσα), 1.0.5 ✓", None))
        info["missing"] = ["1.0.6", "1.0.5"]
        self.assertEqual(m.updates_row(info),
                         ("Ενημερώσεις", "1.0.6 (τρέχουσα), 1.0.5 · λείπουν εδώ", "err"))
        info["missing"] = ["1.0.5"]
        self.assertEqual(m.updates_row(info)[1], "1.0.6 (τρέχουσα), 1.0.5 · λείπει εδώ η 1.0.5")


@unittest.skipIf(QT_ERROR is not None, f"PySide6 δεν είναι διαθέσιμο: {QT_ERROR}")
class PanelTests(Fixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_application(base_pt=12)

    def setUp(self):
        super().setUp()
        self.informed: list[str] = []
        self.chosen: Path | None = None
        self.dialogs: list[ImportPackageDialog] = []

    def make(self, accept=True) -> UpdatesPanel:
        def hook(dialog):
            self.dialogs.append(dialog)
            if accept and dialog.accept_button is not None:
                dialog.accept_button.click()
            else:
                dialog.reject()
        panel = UpdatesPanel(self.model, choose_open=lambda _w, _p: self.chosen,
                             dialog_hook=hook,
                             inform=lambda _title, text: self.informed.append(text))
        self.addCleanup(panel.deleteLater)
        panel.resize(1300, 800)
        panel.show()
        self.app.processEvents()
        return panel

    def test_empty_state_import_and_actions(self):
        self.add("Board1", app="1.0.0", state="current")
        panel = self.make()
        self.assertEqual(panel.info.values["current"].text(), uv.TEXT_NOT_IMPORTED)
        self.assertFalse(panel.start_button.isVisibleTo(panel))
        self.assertFalse(panel.lines["id-Board1"].menu_button.isVisibleTo(panel))
        self.assertIn("με την 1.0.0", panel.counters.text())
        self.chosen = self.packages.make("1.0.6", size=1)
        panel.import_button.click()
        self.assertEqual(self.store.current()["version"], "1.0.6")
        self.assertEqual(self.dialogs[-1].rows.values["row1"].text().split(" · ")[0], "1.0.6")
        self.assertTrue(panel.info.values["current"].text().startswith("1.0.6 · "))
        self.assertTrue(panel.start_button.isVisibleTo(panel))
        self.assertTrue(panel.signature.isVisibleTo(panel))
        # Αυτόματες ενημερώσεις και «Έναρξη τώρα».
        panel.auto.setChecked(False)
        self.assertFalse(self.store.config()["auto"])
        panel.start_button.click()
        self.assertTrue(self.store.config()["start"]["all"])
        line = panel.lines["id-Board1"]
        self.assertTrue(line.menu_button.isVisibleTo(panel))
        self.status.update("id-Board1", app="1.0.0", state="available", available="1.0.6")
        panel.refresh()
        self.assertTrue(line.start_action.isEnabled())
        line.start_action.trigger()
        self.assertEqual(self.store.config()["start"]["clients"], ["id-Board1"])
        self.assertEqual(self.informed, [])

    def test_rejected_and_bad_import(self):
        self.import_versions("1.0.6")
        panel = self.make(accept=False)
        self.chosen = self.packages.make("1.0.7")
        panel.import_button.click()
        self.assertEqual(self.store.current()["version"], "1.0.6")       # Άκυρο
        self.chosen = self.packages.make("1.0.5")
        panel.import_button.click()
        dialog = self.dialogs[-1]
        self.assertIsNone(dialog.accept_button)
        self.assertIn("δεν είναι νεότερη", dialog.error.text())
        self.assertEqual(self.store.current()["version"], "1.0.6")

    def test_states_tooltip_and_progress(self):
        self.import_versions("1.0.6")
        self.add("dl", app="1.0.5", state="downloading", available="1.0.6", progress=45)
        self.add("fail", app="1.0.5", state="failed", available="1.0.6", detail="λόγος")
        panel = self.make()
        line = panel.lines["id-dl"]
        self.assertTrue(line.progress.isVisibleTo(panel))
        self.assertEqual(line.progress.value(), 45)
        self.assertEqual(line.state.property("tone"), "run")
        fail = panel.lines["id-fail"]
        self.assertEqual(fail.state.text(), "Αποτυχία 1.0.6")
        self.assertTrue(fail.alert.isVisibleTo(panel))
        self.assertIn("λόγος", fail.state.toolTip())
        self.assertEqual(line.name.full_text, "C2627-Α1 · dl")

    def test_backup_role_is_read_only_and_missing_note(self):
        self.import_versions("1.0.6")
        self.add("pc", app="1.0.5", state="available", available="1.0.6")
        self.primary = False
        panel = self.make()
        self.assertTrue(panel.package_lock.isVisibleTo(panel))
        self.assertTrue(panel.backup_note.isVisibleTo(panel))
        for widget in (panel.auto, panel.import_button, panel.start_button):
            self.assertFalse(widget.isEnabled())
        self.assertFalse(panel.lines["id-pc"].menu_button.isVisibleTo(panel))
        self.primary = True
        self.store.package_path("1.0.6").unlink()
        panel.refresh()
        self.assertTrue(panel.missing.isVisibleTo(panel))
        self.assertIn("<b>sch-intercom-client-1.0.6.sicu</b>", panel.missing.text())
        self.assertFalse(panel.start_button.isEnabled())
        self.assertFalse(panel.signature.isVisibleTo(panel))

    def test_layout_side_by_side_only_when_wide(self):
        panel = self.make()
        panel.fit_width(3000)
        self.assertTrue(panel.side_by_side)
        panel.fit_width(900)
        self.assertFalse(panel.side_by_side)


@unittest.skipIf(QT_ERROR is not None, f"PySide6 δεν είναι διαθέσιμο: {QT_ERROR}")
class SettingsDialogsTests(unittest.TestCase):
    """Τρίτο tab και πακέτα στους διαλόγους backup/επαναφοράς (mockup #backup, #restore)."""

    @classmethod
    def setUpClass(cls):
        cls.app = create_application(base_pt=12)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.manager = Manager(self.root / "pc")
        self.packages = Packages(self.root / "packages")
        self.packages.root.mkdir()
        self.store = UpdateStore(updates_dir(self.manager.root),
                                 trusted_keys=[self.packages.public])
        for version in ("1.0.5", "1.0.6"):
            self.store.import_package(self.packages.make(version, size=1))
        self.manager.model.update_trusted_keys = [self.packages.public]
        self.status = UpdateStatus(self.store.folder / STATUS_FILE)
        self.updates = uv.UpdatesModel(self.store, self.status, rooms=lambda: [])

    def page(self, hook) -> SettingsPage:
        page = SettingsPage(self.manager.model, updates=self.updates, dialog_hook=hook,
                            inform=lambda *_: None, start_restore=self.restored.append)
        self.addCleanup(page.deleteLater)
        return page

    restored: list = []

    def test_third_tab_and_backup_copies_packages(self):
        target = self.root / "usb" / "b.sicb"
        target.parent.mkdir()
        seen = {}

        def fill(dialog):
            seen["visible"] = dialog.has_updates
            dialog.path.setText(str(target))
            seen["detail"] = dialog.updates_detail.text()
            dialog.password.setText(PASSWORD)
            dialog.confirm.setText(PASSWORD)
            dialog.create.click()

        page = self.page(fill)
        self.assertEqual(page.tab_names, ["Εμφάνιση", "Συντήρηση", TAB_UPDATES])
        page.show_tab(TAB_UPDATES)
        self.assertEqual(page.current_tab, TAB_UPDATES)
        self.assertIs(page.stack.currentWidget(), page.updates_scroll)
        page.open_backup()
        self.assertTrue(seen["visible"])
        self.assertTrue(seen["detail"].startswith("1.0.6 και 1.0.5 · "), seen["detail"])
        self.assertIn("υπογεγραμμένα, στο b-updates", seen["detail"])
        copy = target.with_name("b-updates")
        self.assertEqual(sorted(p.name for p in copy.iterdir()),
                         ["sch-intercom-client-1.0.5.sicu", "sch-intercom-client-1.0.6.sicu"])

        # Επαναφορά: γραμμή «Ενημερώσεις» και checkbox μόνο όταν λείπουν εδώ.
        for path in self.store.folder.glob("*.sicu"):
            path.unlink()
        seen.clear()

        def restore(dialog):
            dialog.path.setText(str(target))
            dialog.password.setText(PASSWORD)
            dialog.load()
            seen["row"] = dialog.rows.values["updates"].text()
            seen["box"] = dialog.rows.values["updates_copy"].isVisibleTo(dialog)
            seen["detail"] = dialog.updates_detail.text()
            seen["request"] = dialog.request()
            dialog.reject()

        page.dialog_hook = restore
        page.open_restore()
        self.assertEqual(seen["row"], "1.0.6 (τρέχουσα), 1.0.5 · λείπουν εδώ")
        self.assertTrue(seen["box"])
        self.assertIn("από b-updates · ελέγχονται υπογραφή και SHA-256", seen["detail"])
        self.assertTrue(seen["request"].restore_updates)


if __name__ == "__main__":
    unittest.main()
