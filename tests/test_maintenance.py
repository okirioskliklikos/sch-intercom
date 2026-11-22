"""Ασφάλεια → «Πιστοποιητικά» και Ρυθμίσεις → «Συντήρηση»: λογική και παράθυρο offscreen.

Πραγματικά αρχεία Manager (ταυτότητα, υπογεγραμμένο μητρώο, backup) σε προσωρινό
φάκελο, με χαμηλό scrypt n μόνο εδώ. Τα Qt tests παραλείπονται χωρίς PySide6.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from director import backup, maintenance as m
from director.backup import BackupError, BackupLocked, manager_lock
from director.tls_identity import create_director_identity
from shared.client_registry import SignedClientRegistry
from shared.device_identity import create_device_identity
from shared.manager_key import password_path

try:
    from PySide6.QtWidgets import QApplication, QDialog
    from director.gui import DirectorMicrophone
    from director.qt_gui import DirectorWindow, create_application, release_window
    from director import qt_maintenance as qm
    from director.qt_security import SecurityPage, TAB_CERTIFICATES
except ImportError as exc:  # pragma: no cover - εξαρτάται από το περιβάλλον
    QT_ERROR = str(exc)
else:
    QT_ERROR = None

PASSWORD = "Σχολικό-backup-2026"
N = 1 << 15


class Manager:
    """Προσωρινή εγκατάσταση Manager με N εγκεκριμένους υπολογιστές."""

    def __init__(self, root: Path, computers: int = 2):
        self.root = root
        self.cert = root / "tls" / "director-tls.crt"
        self.key = root / "tls" / "director-tls.key"
        self.cert.parent.mkdir(parents=True)
        create_director_identity(self.cert, self.key)
        self.registry_file = root / "clients-registry.json"
        self.registry = SignedClientRegistry(self.registry_file, self.cert, self.key)
        for index in range(computers):
            self.approve(f"C2627-Γ{index + 1}")
        self.model = m.MaintenanceModel(self.registry_file, self.cert, self.key, kdf_n=N)

    def approve(self, room: str) -> str:
        client_id = str(uuid.uuid4())
        cert, _key, _password = create_device_identity(self.root / "devices" / client_id,
                                                       client_id)
        self.registry.approve(client_id, room, cert.read_bytes(), name="PC")
        return client_id

    def backup(self, out: Path, **kwargs) -> Path:
        out.parent.mkdir(parents=True, exist_ok=True)
        path, _ = self.model.create_backup(out, PASSWORD, PASSWORD,
                                           copy_media=kwargs.get("copy_media", False))
        return path


class LogicTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.manager = Manager(self.root / "pc")
        self.usb = self.root / "usb"

    # --- Πιστοποιητικά ----------------------------------------------------
    def test_identity_shows_manager_certificate_without_secrets(self):
        identity = self.manager.model.identity()
        self.assertEqual(identity.name, "school-intercom-director")
        self.assertRegex(identity.fingerprint, r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
        self.assertIsNone(identity.expires)
        self.assertEqual(identity.expires_text(), m.TEXT_NO_EXPIRY)
        self.assertEqual(identity.algorithm, "ECDSA P-256")
        self.assertEqual(identity.key_state, "dpapi" if os.name == "nt" else "password_file")
        self.assertTrue(identity.key_ok)
        self.assertTrue(identity.signature_ok)
        self.assertEqual(identity.registry_text(),
                         f"v{self.manager.registry.version} · 2 υπολογιστές")
        self.assertEqual(identity.folder, self.manager.cert.parent.resolve())
        self.assertNotIn("PRIVATE KEY", repr(identity))

    def test_fingerprint_in_two_lines_of_sixteen_bytes(self):
        fingerprint = self.manager.model.identity().fingerprint
        first, second = m.fingerprint_lines(fingerprint).split("\n")
        self.assertEqual(len(first.split(":")), 16)
        self.assertEqual(len(second.split(":")), 16)
        self.assertEqual(f"{first}:{second}", fingerprint)

    def test_key_state_plain_and_broken(self):
        password_path(self.manager.key).unlink()
        self.assertEqual(self.manager.model.identity().key_state, "broken")
        self.assertEqual(self.manager.model.identity().key_text(), m.TEXT_KEY_BROKEN)
        other = Manager(self.root / "plain", computers=0)
        from shared.manager_key import plaintext_pem
        plain = plaintext_pem(other.key)
        other.key.write_bytes(plain)
        self.assertEqual(m.key_state(other.key), "plain")

    def test_tampered_registry_shows_invalid_signature(self):
        document = json.loads(self.manager.registry_file.read_text(encoding="utf-8"))
        document["registry"]["clients"][0]["room"] = "Άλλη"
        self.manager.registry_file.write_text(json.dumps(document), encoding="utf-8")
        identity = self.manager.model.identity()
        self.assertFalse(identity.signature_ok)
        self.assertIsNone(identity.registry_version)

    # --- κωδικός ----------------------------------------------------------
    def test_passphrase_rules(self):
        self.assertEqual(m.passphrase_problem("κοντός"), m.TEXT_PASS_SHORT)
        self.assertEqual(m.passphrase_problem("123456789012", "123456789013"),
                         m.TEXT_PASS_MISMATCH)
        self.assertIsNone(m.passphrase_problem("123456789012", "123456789012"))
        import unicodedata
        nfd = unicodedata.normalize("NFD", PASSWORD)
        self.assertIsNone(m.passphrase_problem(PASSWORD, nfd))
        with self.assertRaises(BackupError):
            self.manager.model.create_backup(self.usb / "x.sicb", PASSWORD, PASSWORD + "!",
                                             copy_media=False)
        self.assertFalse((self.usb / "x.sicb").exists())

    def test_backup_extension_is_sicb(self):
        self.assertTrue(m.default_backup_name().endswith(".sicb"))
        self.usb.mkdir()
        path, _ = backup.create(self.usb, PASSWORD, registry_file=self.manager.registry_file,
                                tls_cert=self.manager.cert, tls_key=self.manager.key,
                                media_dir=self.root / "none", kdf_n=N)
        self.assertEqual(path.suffix, ".sicb")

    # --- κατάσταση backup/επαναφοράς ---------------------------------------
    def test_last_backup_counts_registry_changes(self):
        model = self.manager.model
        self.assertIsNone(model.last_backup())
        path = self.manager.backup(self.usb / "a.sicb")
        last = model.last_backup()
        self.assertEqual(last["file_name"], path.name)
        self.assertEqual(last["changes"], 0)
        self.assertIn("μόλις τώρα", last["when"])
        self.manager.approve("C2627-Δ1")
        self.manager.approve("C2627-Δ2")
        self.assertEqual(model.last_backup()["changes"], 2)
        self.assertEqual(m.changes_text(2), "2 αλλαγές μητρώου")
        self.assertEqual(m.changes_text(1), "1 αλλαγή μητρώου")
        self.assertEqual(m.changes_text(0), m.TEXT_NO_CHANGES)

    def test_preview_same_older_and_media(self):
        media = self.manager.model.media_dir
        media.mkdir()
        (media / "κουδούνι.mp3").write_bytes(b"ID3 test")
        (media / "ύμνος.mp3").write_bytes(b"ID3 test 2")
        self.assertEqual(self.manager.model.media_files(), (2, 18))
        path = self.manager.backup(self.usb / "a.sicb", copy_media=True)
        info = self.manager.model.preview(path, PASSWORD)
        self.assertEqual((info["identity"], info["relation"]), ("same", "same"))
        self.assertIsNone(m.preview_warning(info))
        self.assertEqual(info["approved"], 2)
        self.assertFalse(info["media_copy_available"])
        (media / "ύμνος.mp3").unlink()
        self.manager.approve("C2627-Δ1")
        before = sorted(p.name for p in self.manager.root.rglob("*"))
        info = self.manager.model.preview(path, PASSWORD)
        self.assertEqual(info["relation"], "older")
        self.assertTrue(info["media_copy_available"])
        warning, consent = m.preview_warning(info)
        version = info["registry_version"]
        self.assertIn(f"(v{version} < v{version + 1})", warning)
        self.assertEqual(consent, m.TEXT_ACCEPT_OLDER)
        rows = dict((label, (text, tone)) for label, text, tone in m.preview_rows(info))
        self.assertEqual(rows["Ταυτότητα"], (m.TEXT_SAME_IDENTITY, "ok"))
        self.assertEqual(rows["MP3"], ("2 αρχεία · λείπει 1", "err"))
        self.assertIn("2 υπολογιστές", rows["Μητρώο"][0])
        # Η προεπισκόπηση δεν γράφει τίποτα (ούτε last-restore, ούτε staging).
        self.assertEqual(before, sorted(p.name for p in self.manager.root.rglob("*")))

    def test_preview_runs_while_manager_holds_lock(self):
        path = self.manager.backup(self.usb / "a.sicb")
        with manager_lock(self.manager.root):
            self.assertEqual(self.manager.model.preview(path, PASSWORD)["identity"], "same")
            with self.assertRaises(BackupLocked):
                self.manager.model.restore(m.RestoreRequest(path, PASSWORD))

    def test_preview_wrong_password_and_other_identity(self):
        path = self.manager.backup(self.usb / "a.sicb")
        with self.assertRaises(BackupError) as caught:
            self.manager.model.preview(path, "λάθος-κωδικός-123")
        self.assertEqual(caught.exception.status, 3)
        other = Manager(self.root / "other", computers=1)
        info = other.model.preview(path, PASSWORD)
        self.assertEqual(info["identity"], "different")
        warning, consent = m.preview_warning(info)
        self.assertEqual(consent, m.TEXT_REPLACE_IDENTITY)
        self.assertIn("άλλη ταυτότητα Manager", warning)

    def test_conflicting_registry_needs_consent(self):
        path = self.manager.backup(self.usb / "a.sicb")
        restored = Manager.__new__(Manager)
        # Νέος κλάδος από το ίδιο backup: restore σε άλλο φάκελο και αλλαγή εκεί.
        target = self.root / "branch"
        target.mkdir()
        model = m.MaintenanceModel(target / "clients-registry.json",
                                   target / "tls" / "director-tls.crt",
                                   target / "tls" / "director-tls.key", kdf_n=N)
        model.restore(m.RestoreRequest(path, PASSWORD))
        branch = SignedClientRegistry(model.registry_file, model.tls_cert, model.tls_key)
        restored.root = target
        restored.registry = branch
        Manager.approve(restored, "Κλάδος-Α")
        self.manager.approve("Κλάδος-Β")
        branch_backup = model.create_backup(self.usb / "branch.sicb", PASSWORD, PASSWORD,
                                            copy_media=False)[0]
        info = self.manager.model.preview(branch_backup, PASSWORD)
        self.assertEqual(info["relation"], "conflict")
        self.assertIn("δεν προέρχεται από το τρέχον", m.preview_warning(info)[0])
        with self.assertRaises(BackupError) as caught:
            self.manager.model.restore(m.RestoreRequest(branch_backup, PASSWORD))
        self.assertEqual(caught.exception.status, 5)

    def test_restore_records_last_restore_and_previous_folder(self):
        path = self.manager.backup(self.usb / "a.sicb")
        self.manager.approve("C2627-Δ1")
        model = self.manager.model
        self.assertIsNone(model.last_restore())
        self.assertEqual(model.previous_folders(), [])
        with self.assertRaises(BackupError):
            model.restore(m.RestoreRequest(path, PASSWORD))  # παλαιότερο χωρίς συναίνεση
        result = model.restore(m.RestoreRequest(path, PASSWORD, accept_older=True))
        last = model.last_restore()
        self.assertIn(f"v{result['registry_version']}", last["when"])
        self.assertEqual(last["file_name"], "a.sicb")
        folders = model.previous_folders()
        self.assertEqual(len(folders), 1)
        self.assertTrue(folders[0].name.startswith("pre-restore-"))
        self.assertEqual(model.delete_previous(), 1)
        self.assertEqual(model.previous_folders(), [])

    def test_request_repr_hides_password(self):
        request = m.RestoreRequest(Path("a.sicb"), PASSWORD, accept_older=True)
        self.assertNotIn(PASSWORD, repr(request))
        self.assertNotIn(PASSWORD, str(request))

    def test_restart_command_repeats_original_arguments(self):
        from director import main
        with mock.patch.object(sys, "orig_argv",
                               ["python", "-m", "director.main", "--gui", "--tls-cert", "c"]):
            self.assertEqual(main.restart_command(),
                             [sys.executable, "-m", "director.main", "--gui", "--tls-cert", "c"])


class FakeBroadcaster:
    def status_snapshot(self):
        return []

    def set_targets(self, **_kwargs):
        pass

    def publish(self, *_args):
        pass


def fake_stream(_gate, *, stop, ready_callback, level_callback, **_kwargs):
    ready_callback()
    while not stop.wait(0.05):
        level_callback(0.0)


@unittest.skipIf(QT_ERROR is not None, f"PySide6 δεν είναι διαθέσιμο: {QT_ERROR}")
class QtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_application(base_pt=12)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.manager = Manager(self.root / "pc")
        self.usb = self.root / "usb"
        self.usb.mkdir()
        self.events: list[str] = []
        self.warnings: list[str] = []
        self.informed: list[str] = []
        self.window = None

    def tearDown(self):
        if self.window is not None:
            self.window.close()
            release_window(self.window)
        self.app.processEvents()

    def settle(self):
        for _ in range(4):
            self.app.processEvents()

    def make_window(self, **options):
        broadcaster = FakeBroadcaster()
        self.mic = DirectorMicrophone(fake_stream, broadcaster)
        defaults = {"stop_serving": lambda: self.events.append("stop"),
                    "restart": lambda: self.events.append("restart"),
                    "warn": self.warnings.append,
                    "inform": lambda _title, text: self.informed.append(text),
                    "layout_settings": self.root / "director-gui.json"}
        self.window = DirectorWindow(broadcaster, self.mic, maintenance=self.manager.model,
                                     maintenance_options={**defaults, **options})
        self.window.resize(1366, 900)
        self.window.show()
        self.settle()
        return self.window

    # --- Πιστοποιητικά ----------------------------------------------------
    def test_certificates_tab_shows_identity_and_copies_fingerprint(self):
        page = SecurityPage(mock.Mock(), identity=self.manager.model.identity)
        self.addCleanup(page.deleteLater)
        self.assertTrue(page.tab_certificates.isEnabled())
        page.show_tab(TAB_CERTIFICATES)
        self.assertEqual(page.current_tab, TAB_CERTIFICATES)
        self.assertTrue(page.tab_certificates.isChecked())
        self.assertFalse(page.tab_computers.isChecked())
        panel = page.certificates
        identity = self.manager.model.identity()
        self.assertEqual(panel.fingerprint.text(), m.fingerprint_lines(identity.fingerprint))
        self.assertIn(identity.key_text(), panel.key_state.text())
        self.assertEqual(panel.key_state.property("tone"), "ok")
        self.assertEqual(panel.registry_state.text(), m.TEXT_SIGNATURE_OK)
        self.assertEqual(panel.rows.values["expires"].text(), m.TEXT_NO_EXPIRY)
        panel.copy_fingerprint()
        self.assertEqual(QApplication.clipboard().text(), identity.fingerprint)
        self.assertEqual(panel.copy_button.text(), qm.COPIED)
        texts = " ".join(page.visible_texts())
        self.assertNotIn("PRIVATE KEY", texts)

    def test_certificates_tab_disabled_without_identity(self):
        page = SecurityPage(mock.Mock())
        self.addCleanup(page.deleteLater)
        self.assertFalse(page.tab_certificates.isEnabled())
        self.assertIsNone(page.certificates)

    # --- Ρυθμίσεις → Συντήρηση --------------------------------------------
    def test_settings_menu_enabled_with_maintenance_only(self):
        window = self.make_window()
        self.assertTrue(window.nav_buttons["settings"].isEnabled())
        window.show_page("settings")
        self.assertEqual(window.current_page, "settings")
        self.assertEqual(window.page_title.text(), qm.SETTINGS_TITLE)
        page = window.settings_page
        self.assertEqual(page.tab_maintenance.text(), qm.TAB_MAINTENANCE)
        self.assertEqual(page.backup_rows.values["when"].text(), "—")
        self.assertFalse(page.delete_button.isVisible())
        # Ίδιος κανόνας κύλισης με τις άλλες σελίδες (ui-style.md §2).
        self.assertEqual(page.grid.contentsMargins().right(), qm.px(12))
        self.assertTrue(page.side_by_side)
        other = DirectorWindow(FakeBroadcaster(), DirectorMicrophone(fake_stream, FakeBroadcaster()))
        self.addCleanup(lambda: release_window(other))
        self.assertFalse(other.nav_buttons["settings"].isEnabled())

    def test_backup_dialog_validates_and_creates_backup(self):
        window = self.make_window()
        window.show_page("settings")
        page = window.settings_page
        media = self.manager.model.media_dir
        target = self.usb / "μέσα.sicb"
        seen = {}

        def fill(dialog):
            seen["media_visible"] = dialog.has_media
            self.assertFalse(dialog.create.isEnabled())
            dialog.path.setText(str(target))
            dialog.password.setText("κοντός")
            self.assertEqual(dialog.status.text(), m.TEXT_PASS_SHORT)
            dialog.password.setText(PASSWORD)
            dialog.confirm.setText(PASSWORD + "x")
            self.assertEqual(dialog.status.text(), m.TEXT_PASS_MISMATCH)
            self.assertFalse(dialog.create.isEnabled())
            dialog.confirm.setText(PASSWORD)
            self.assertEqual(dialog.status.text(), m.TEXT_PASS_MATCH)
            self.assertTrue(dialog.create.isEnabled())
            dialog.create.click()

        page.dialog_hook = fill
        page.open_backup()
        self.assertFalse(seen["media_visible"])  # χωρίς MP3: καμία ερώτηση αντιγράφου
        self.assertTrue(target.is_file())
        self.assertEqual(page.backup_rows.values["file"].full_text, target.name)
        self.assertEqual(page.backup_rows.values["changes"].text(), m.TEXT_NO_CHANGES)
        self.assertTrue(any(m.TEXT_SECRET_REMINDER in text for text in self.informed))
        self.assertNotIn(PASSWORD, " ".join(page.visible_texts()))
        saved = json.loads((self.root / "director-gui.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["backup_folder"], str(self.usb))
        # Με MP3: η επιλογή αντιγράφου φαίνεται και το αντίγραφο γίνεται δίπλα στο backup.
        media.mkdir()
        (media / "κουδούνι.mp3").write_bytes(b"ID3 test")
        second = self.usb / "με-mp3.sicb"

        def fill_media(dialog):
            seen["media_visible"] = dialog.has_media
            dialog.path.setText(str(second))
            dialog.password.setText(PASSWORD)
            dialog.confirm.setText(PASSWORD)
            dialog.create.click()

        page.dialog_hook = fill_media
        page.open_backup()
        self.assertTrue(seen["media_visible"])
        self.assertTrue((self.usb / "με-mp3-media" / "κουδούνι.mp3").is_file())

    def test_backup_dialog_shows_error_and_stays_open(self):
        window = self.make_window()
        page = window.settings_page
        existing = self.manager.backup(self.usb / "υπάρχει.sicb")
        seen = {}

        def fill(dialog):
            dialog.path.setText(str(existing))
            dialog.password.setText(PASSWORD)
            dialog.confirm.setText(PASSWORD)
            dialog.create.click()
            seen["error"] = dialog.error.text()
            seen["result"] = dialog.result()

        page.dialog_hook = fill
        page.open_backup()
        self.assertIn("δεν αντικαθίσταται", seen["error"])
        self.assertNotEqual(seen["result"], QDialog.DialogCode.Accepted)

    def test_restore_dialog_wrong_password_then_consent_then_restart(self):
        path = self.manager.backup(self.usb / "a.sicb")
        self.manager.approve("C2627-Δ1")  # το backup είναι πλέον παλαιότερο
        window = self.make_window()
        page = window.settings_page
        seen = {}

        def fill(dialog):
            dialog.path.setText(str(path))
            dialog.password.setText("λάθος-κωδικός-99")
            dialog.load()
            seen["error"] = dialog.error.text()
            self.assertFalse(dialog.restore.isEnabled())
            dialog.password.setText(PASSWORD)
            dialog.load()
            self.assertTrue(dialog.summary.isVisibleTo(dialog))
            self.assertTrue(dialog.consent.isVisibleTo(dialog))
            self.assertEqual(dialog.consent.text(), m.TEXT_ACCEPT_OLDER)
            self.assertFalse(dialog.restore.isEnabled())
            dialog.consent.setChecked(True)
            self.assertTrue(dialog.restore.isEnabled())
            seen["request"] = dialog.request()
            dialog.restore.click()

        page.dialog_hook = fill
        page.open_restore()
        self.assertEqual(seen["error"], backup.ERROR_SECRET)
        self.assertTrue(seen["request"].accept_older)
        self.assertFalse(seen["request"].replace_identity)
        self.assertEqual(self.events, ["stop", "restart"])
        self.assertTrue(window.restore_succeeded)
        self.assertEqual(self.warnings, [])
        self.assertIsNotNone(self.manager.model.last_restore())

    def test_restore_failure_warns_and_restarts_with_old_files(self):
        path = self.manager.backup(self.usb / "a.sicb")
        window = self.make_window()
        before = self.manager.registry_file.read_bytes()
        with mock.patch.object(self.manager.model, "restore",
                               side_effect=BackupError("δοκιμαστικό σφάλμα")):
            window.start_restore(m.RestoreRequest(path, PASSWORD))
        self.assertEqual(self.events, ["stop", "restart"])
        self.assertFalse(window.restore_succeeded)
        self.assertIn("δοκιμαστικό σφάλμα", self.warnings[0])
        self.assertEqual(self.manager.registry_file.read_bytes(), before)

    def test_restore_releases_manager_lock_before_restoring(self):
        """Όπως στο main: ο Manager κρατά το κλείδωμα και το αφήνει στο stop_serving."""
        path = self.manager.backup(self.usb / "a.sicb")
        lock = ExitStack()
        lock.enter_context(manager_lock(self.manager.root))
        self.addCleanup(lock.close)
        with self.assertRaises(BackupLocked):
            self.manager.model.restore(m.RestoreRequest(path, PASSWORD))
        window = self.make_window(stop_serving=lock.close)
        window.start_restore(m.RestoreRequest(path, PASSWORD))
        self.assertTrue(window.restore_succeeded, self.warnings)

    def test_restore_blocked_during_live_announcement(self):
        window = self.make_window()
        page = window.settings_page
        opened = []
        page.dialog_hook = opened.append
        with mock.patch.object(type(self.mic), "live", new_callable=mock.PropertyMock,
                               return_value=True):
            page.open_restore()
        self.assertEqual(opened, [])
        self.assertIn("ζωντανή ανακοίνωση", self.informed[-1])

    def test_previous_files_shown_and_deleted_after_confirmation(self):
        path = self.manager.backup(self.usb / "a.sicb")
        self.manager.model.restore(m.RestoreRequest(path, PASSWORD))
        answers = []
        window = self.make_window(confirm=lambda *_a: answers.pop(0))
        window.show_page("settings")
        page = window.settings_page
        self.assertTrue(page.delete_button.isVisible())
        self.assertTrue(page.previous_name.full_text.startswith("pre-restore-"))
        self.assertNotEqual(page.restore_rows.values["when"].text(), "—")
        answers.append(False)
        page.delete_previous()
        self.assertEqual(len(self.manager.model.previous_folders()), 1)
        answers.append(True)
        page.delete_previous()
        self.assertEqual(self.manager.model.previous_folders(), [])
        self.assertFalse(page.delete_button.isVisible())
        self.assertEqual(page.previous_name.full_text, "—")


if __name__ == "__main__":
    unittest.main()
