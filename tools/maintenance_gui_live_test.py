"""Αυτόματη πραγματική δοκιμή: Ασφάλεια → «Πιστοποιητικά» και Ρυθμίσεις → «Συντήρηση».

Τρέχει στον υπολογιστή του Manager (κύριο σενάριο: Windows). Ανοίγει το πραγματικό
παράθυρο Director (PySide6) με πραγματικό Manager (TLS, μητρώο, θύρα εγγραφής,
manager.lock) και η δοκιμή το οδηγεί όπως ο διαχειριστής: tab «Πιστοποιητικά»,
διάλογος backup, διάλογος επαναφοράς, επαναφορά με διακοπή εξυπηρέτησης και
επανεκκίνηση. Ο Client είναι ο πραγματικός ``client.main`` ως διεργασία.

Μόνο προσωρινός φάκελος (``%TEMP%\\sch-maint-test-*``), loopback 127.0.0.1 και θύρες
51555/51556/51559: η πραγματική εγκατάσταση (``%USERPROFILE%\\.school-intercom``) δεν
αγγίζεται. Ο φάκελος σβήνεται στο τέλος. Δεν χρειάζεται μικρόφωνο.

Η «επανεκκίνηση» μετά την επαναφορά ελέγχεται όπως γίνεται στην εφαρμογή: το παράθυρο
ζητά επανεκκίνηση και κλείνει, και ο Manager ξεκινά ξανά από την αρχή (εδώ ως
``director.main`` διεργασία) πάνω στα αρχεία που επανήλθαν.

    Windows:  powershell -ExecutionPolicy Bypass -File tools\\maintenance_gui_live_test.ps1
    Linux:    tools/maintenance_gui_live_test.sh
"""
from __future__ import annotations

from contextlib import ExitStack
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.backup_live_test import (DISCOVERY_PORT, ENROLL_PORT, HOST, PORT,  # noqa: E402
                                    Device, Machine, fingerprint)
from tools.mtls_live_test import JOINED  # noqa: E402

PASSWORD = "ΜόνοΓιαΔοκιμή-2026-gui"
TOTAL = 14
SCREENS = Path.home() / "sch-intercom-test-screens"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fake_stream(_gate, *, stop, ready_callback, level_callback, **_kwargs):
    """Χωρίς μικρόφωνο: η δοκιμή δεν κάνει ανακοίνωση."""
    ready_callback()
    while not stop.wait(0.05):
        level_callback(0.0)


class GuiManager:
    """Ο Manager με GUI στη διεργασία της δοκιμής, όπως το ``director.main --gui``."""

    def __init__(self, machine: Machine, app):
        from director.backup import manager_lock
        from director.main import RegistryTrust, accept_clients
        from director.qt_gui import GuiRuntime
        from director.security import LastSeenStore, last_seen_path
        from shared.client_registry import SignedClientRegistry
        from shared.enrollment import EnrollmentServer

        self.app = app
        self.machine = machine
        self.informed: list[str] = []
        self.warnings: list[str] = []
        self.answers: list[bool] = []
        # Όπως στο main: το κλείδωμα κρατιέται όσο τρέχει ο Manager.
        self.lock = ExitStack()
        self.lock.enter_context(manager_lock(machine.dir))
        registry = SignedClientRegistry(machine.registry, machine.cert, machine.key)
        self.trust = RegistryTrust(registry, machine.cert, machine.key)
        enrollment = EnrollmentServer(machine.cert, machine.key, self.trust, host=HOST,
                                      port=ENROLL_PORT,
                                      pending_file=machine.dir / "enrollment-pending.json")
        args = SimpleNamespace(host=HOST, port=PORT, discovery_port=DISCOVERY_PORT,
                               director_id_file=machine.dir / "director.id", device=None,
                               latency="low", registry_file=machine.registry,
                               tls_cert=machine.cert, tls_key=machine.key)
        secure_accept = partial(accept_clients, tls_context=self.trust.context(),
                                registry=registry, context_provider=self.trust.context)
        options = {"warn": self.warnings.append,
                   "inform": lambda _title, text: self.informed.append(text),
                   "confirm": lambda *_args: self.answers.pop(0)}
        self.runtime = GuiRuntime(args, accept_clients=secure_accept,
                                  stream_microphone=fake_stream, enrollment=enrollment,
                                  trust=self.trust,
                                  last_seen=LastSeenStore(last_seen_path(machine.registry)),
                                  release_lock=self.lock.close, maintenance_options=options,
                                  quit_app=lambda: None)
        self.window = self.runtime.start()
        self.window.resize(1366, 860)
        self.window.show()
        self.pump()

    def pump(self, seconds: float = 0.2) -> None:
        end = time.monotonic() + seconds
        while True:
            self.app.processEvents()
            if time.monotonic() >= end:
                return
            time.sleep(0.02)

    def wait(self, fn, timeout: float = 20.0):
        deadline = time.monotonic() + timeout
        while True:
            value = fn()
            if value or time.monotonic() > deadline:
                return value
            self.pump(0.2)

    def online(self, client_id: str | None) -> bool:
        return any(cid == client_id and active for cid, _r, active, _a
                   in self.runtime.broadcaster.status_snapshot())

    def screenshot(self, name: str, widget=None) -> None:
        SCREENS.mkdir(parents=True, exist_ok=True)
        path = SCREENS / f"{name}.png"
        (widget or self.window).grab().save(str(path))
        print(f"    Στιγμιότυπο: {path}", flush=True)

    @property
    def settings(self):
        return self.window.settings_page

    def texts(self) -> str:
        pages = [self.window.settings_page, self.window.security_page]
        return " ".join(t for page in pages for t in page.visible_texts())

    def close(self) -> None:
        self.runtime.shutdown()
        self.lock.close()
        self.pump()


class LiveTest:
    def __init__(self, base: Path, app):
        self.base = base
        self.app = app
        self.results: list[tuple[str, bool, str]] = []
        self.pc = Machine(base, "pc")
        self.a = Device(base, "Board1")
        self.usb = base / "usb"
        self.usb.mkdir()
        self.file = self.usb / "sch-intercom-backup-gui.sicb"
        self.gui: GuiManager | None = None
        self.backup_version: int | None = None

    def result(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, ok, detail))
        text = f"{'PASS' if ok else 'FAIL'} {len(self.results):02d}/{TOTAL}: {name}"
        print(f"\n=== {text}" + (f" — {detail}" if detail else "") + "\n", flush=True)

    def step(self, name: str, action) -> None:
        try:
            outcome = action()
            ok, detail = outcome if isinstance(outcome, tuple) else (bool(outcome), "")
        except Exception as exc:  # η δοκιμή αναφέρει, δεν κρύβει
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        self.result(name, ok, detail)

    def registry_version(self) -> int:
        return json.loads(self.pc.registry.read_text(encoding="utf-8"))["registry"][
            "registry_version"]

    # ------------------------------------------------------------------ βήματα
    def s01_start(self):
        if not self.pc.create_identity():
            return False, "η ταυτότητα TLS δεν δημιουργήθηκε"
        self.pc.media.mkdir()
        for name in ("κουδούνι.mp3", "διάλειμμα.mp3"):
            (self.pc.media / name).write_bytes(os.urandom(4096))
        self.gui = GuiManager(self.pc, self.app)
        window = self.gui.window
        return (window.nav_buttons["settings"].isEnabled()
                and window.security_page.tab_certificates.isEnabled(),
                "μενού Ρυθμίσεις και tab Πιστοποιητικά ενεργά")

    def s02_enroll(self):
        model = self.gui.window.security
        code = self.a.enroll(self.pc.cert)
        request = self.gui.wait(lambda: next((r for r in model.requests()
                                              if r.name == self.a.name), None), timeout=10)
        if request is None or code is None:
            return False, "δεν βρέθηκε το αίτημα"
        decision = model.approve(request.request_id, code)
        self.a.start(self.pc.cert)
        online = self.gui.wait(lambda: self.gui.online(self.a.client_id), timeout=40)
        return decision.ok and online, f"έγκριση {decision.ok}, online {online}"

    def s03_certificates(self):
        from director import maintenance as m
        window = self.gui.window
        window.nav_buttons["security"].click()
        window.security_page.tab_certificates.click()
        self.gui.pump()
        panel = window.security_page.certificates
        expected = fingerprint(self.pc.cert)
        shown = panel.fingerprint.text().replace("\n", ":")
        key = panel.key_state.text()
        wanted_key = m.TEXT_KEY_DPAPI if os.name == "nt" else m.TEXT_KEY_PASSFILE
        panel.copy_button.click()
        from PySide6.QtWidgets import QApplication
        copied = QApplication.clipboard().text()
        self.gui.screenshot("pistopoiitika")
        ok = (shown == expected and wanted_key in key and copied == expected
              and panel.registry_state.text() == m.TEXT_SIGNATURE_OK
              and panel.rows.values["expires"].text() == m.TEXT_NO_EXPIRY)
        return ok, f"αποτύπωμα ίδιο {shown == expected}, κλειδί «{key}», πρόχειρο {copied == expected}"

    def s04_backup_dialog(self):
        from director import maintenance as m
        window = self.gui.window
        window.nav_buttons["settings"].click()
        self.gui.pump()
        seen = {}

        def fill(dialog):
            dialog.path.setText(str(self.file))
            dialog.password.setText(PASSWORD)
            dialog.confirm.setText(PASSWORD[:-1])
            seen["mismatch"] = (dialog.status.text(), dialog.create.isEnabled())
            dialog.confirm.setText(PASSWORD)
            seen["media"] = dialog.has_media and dialog.media.isChecked()
            dialog.show()
            self.gui.pump()
            self.gui.screenshot("dialogos-backup", dialog)
            dialog.create.click()

        self.gui.settings.dialog_hook = fill
        self.gui.settings.open_backup()
        self.gui.settings.dialog_hook = None
        copy = self.usb / (self.file.stem + "-media")
        copies = all((copy / p.name).is_file() and sha256(copy / p.name) == sha256(p)
                     for p in self.pc.media.glob("*.mp3"))
        self.backup_version = self.registry_version()
        changes = self.gui.settings.backup_rows.values["changes"].text()
        ok = (seen["mismatch"] == (m.TEXT_PASS_MISMATCH, False) and seen["media"]
              and self.file.is_file() and copies and changes == m.TEXT_NO_CHANGES
              and self.gui.online(self.a.client_id))
        return ok, (f"αρχείο {self.file.is_file()}, αντίγραφα MP3 {copies}, «{changes}», "
                    f"Client online {self.gui.online(self.a.client_id)}")

    def s05_changes_since(self):
        model = self.gui.window.security
        model.rename(self.a.client_id, "Board1-νέο")
        self.gui.settings.refresh()
        text = self.gui.settings.backup_rows.values["changes"].text()
        self.gui.screenshot("syntirisi")
        return text == "1 αλλαγή μητρώου", f"«{text}»"

    def s06_cli_locked(self):
        rc, _ = self.pc.backup("restore", str(self.file), "--accept-older", password=PASSWORD)
        return rc == 4, f"python -m director.backup restore: κωδικός {rc} (αναμενόμενο 4)"

    def s07_wrong_password(self):
        from director.backup import ERROR_SECRET
        before = self.pc.registry.read_bytes()
        seen = {}

        def fill(dialog):
            dialog.path.setText(str(self.file))
            dialog.password.setText("ΛάθοςΚωδικός-12345")
            dialog.load()
            seen["error"] = dialog.error.text()
            seen["enabled"] = dialog.restore.isEnabled()
            dialog.reject()

        self.gui.settings.dialog_hook = fill
        self.gui.settings.open_restore()
        self.gui.settings.dialog_hook = None
        same = self.pc.registry.read_bytes() == before
        return (seen["error"] == ERROR_SECRET and not seen["enabled"] and same
                and self.gui.online(self.a.client_id), f"«{seen['error']}», ίδιο μητρώο {same}")

    def s08_preview_and_restore(self):
        from director import maintenance as m
        (self.pc.media / "διάλειμμα.mp3").unlink()
        seen = {}

        def fill(dialog):
            dialog.path.setText(str(self.file))
            dialog.password.setText(PASSWORD)
            dialog.load()
            seen["rows"] = {k: dialog.rows.values[k].text() for k in dialog.rows.values}
            seen["before_consent"] = dialog.restore.isEnabled()
            seen["consent"] = dialog.consent.text()
            seen["media"] = dialog.media.isChecked() and dialog.info["media_copy_available"]
            dialog.consent.setChecked(True)
            dialog.show()
            self.gui.pump()
            self.gui.screenshot("dialogos-epanafora", dialog)
            dialog.restore.click()

        self.gui.settings.dialog_hook = fill
        self.gui.settings.open_restore()
        rows = seen.get("rows", {})
        ok = (rows.get("identity") == m.TEXT_SAME_IDENTITY
              and rows.get("mp3") == "2 αρχεία · λείπει 1"
              and not seen["before_consent"] and seen["consent"] == m.TEXT_ACCEPT_OLDER
              and seen["media"] and self.gui.window.restore_succeeded is True
              and self.gui.runtime.restart_requested and not self.gui.warnings)
        return ok, (f"σύνοψη {rows}, επαναφορά {self.gui.window.restore_succeeded}, "
                    f"επανεκκίνηση {self.gui.runtime.restart_requested}, "
                    f"προειδοποιήσεις {self.gui.warnings}")

    def s09_stopped_serving(self):
        from director.backup import manager_lock
        offline = self.gui.wait(lambda: not self.gui.online(self.a.client_id), timeout=10)
        try:
            with manager_lock(self.pc.dir):
                free = True
        except Exception:
            free = False
        self.gui.close()
        self.gui = None
        return offline and free, f"Client αποσυνδέθηκε {offline}, κλείδωμα ελεύθερο {free}"

    def s10_restored_files(self):
        restored = self.registry_version() == self.backup_version
        media = all((self.pc.media / n).is_file() for n in ("κουδούνι.mp3", "διάλειμμα.mp3"))
        copy = self.usb / (self.file.stem + "-media") / "διάλειμμα.mp3"
        same_hash = media and sha256(self.pc.media / "διάλειμμα.mp3") == sha256(copy)
        previous = list(self.pc.dir.glob("pre-restore-*"))
        last = (self.pc.dir / "last-restore.json").is_file()
        name = json.loads(self.pc.registry.read_text(encoding="utf-8"))["registry"]["clients"][0]
        undone = name.get("name") == self.a.name
        return (restored and same_hash and bool(previous) and last and undone,
                f"μητρώο v{self.registry_version()} (backup v{self.backup_version}), "
                f"MP3 {same_hash}, pre-restore {bool(previous)}, last-restore {last}, "
                f"μετονομασία αναιρέθηκε {undone}")

    def s11_restart_reconnect(self):
        from director.main import restart_command
        command = restart_command()
        director = self.pc.start()
        mark = director.mark()
        self.a.start(self.pc.cert)
        joined = director.wait_for(self.a.client_id, JOINED, since=mark, timeout=40)
        enrolled_again = director.seen("Νέο αίτημα", since=mark)
        self.a.stop()
        self.pc.stop()
        return (joined is not None and not enrolled_again and command[0] == sys.executable,
                f"σύνδεση χωρίς νέα εγγραφή {joined is not None and not enrolled_again}")

    def s12_settings_after_restore(self):
        self.gui = GuiManager(self.pc, self.app)
        window = self.gui.window
        window.nav_buttons["settings"].click()
        self.gui.pump()
        page = self.gui.settings
        when = page.restore_rows.values["when"].text()
        previous = page.previous_name.full_text
        self.gui.screenshot("syntirisi-meta-epanafora")
        return (when != "—" and previous.startswith("pre-restore-")
                and page.delete_button.isVisible(), f"«{when}», «{previous}»")

    def s13_delete_previous(self):
        page = self.gui.settings
        self.gui.answers.extend([False, True])
        page.delete_button.click()
        kept = bool(list(self.pc.dir.glob("pre-restore-*")))
        page.delete_button.click()
        gone = not list(self.pc.dir.glob("pre-restore-*"))
        return kept and gone and not page.delete_button.isVisible(), \
            f"Άκυρο κράτησε {kept}, Διαγραφή έσβησε {gone}"

    def s14_no_secrets(self):
        texts = self.gui.texts() + " ".join(self.gui.informed)
        bad = [s for s in (PASSWORD, "PRIVATE KEY", "BEGIN CERTIFICATE") if s in texts]
        self.gui.close()
        self.gui = None
        return not bad, f"βρέθηκαν: {bad}" if bad else "κανένας κωδικός ή κλειδί σε κείμενο"

    def run(self) -> int:
        steps = [
            ("Manager με GUI: Ρυθμίσεις και Πιστοποιητικά ενεργά", self.s01_start),
            ("Εγγραφή και σύνδεση Client A", self.s02_enroll),
            ("Πιστοποιητικά: αποτύπωμα, κλειδί, υπογραφή, αντιγραφή", self.s03_certificates),
            ("Διάλογος backup: έλεγχος κωδικού, .sicb, αντίγραφο MP3", self.s04_backup_dialog),
            ("«Από τότε»: 1 αλλαγή μητρώου", self.s05_changes_since),
            ("Restore από CLI όσο τρέχει το GUI: κωδικός 4", self.s06_cli_locked),
            ("Επαναφορά με λάθος κωδικό: μήνυμα, τίποτα δεν αλλάζει", self.s07_wrong_password),
            ("Σύνοψη, συναίνεση παλαιότερου μητρώου, επαναφορά", self.s08_preview_and_restore),
            ("Η εξυπηρέτηση σταμάτησε και το κλείδωμα αφέθηκε", self.s09_stopped_serving),
            ("Τα αρχεία επανήλθαν (μητρώο, MP3, pre-restore)", self.s10_restored_files),
            ("Επανεκκίνηση: ο Client συνδέεται χωρίς νέα εγγραφή", self.s11_restart_reconnect),
            ("Συντήρηση μετά: τελευταία επαναφορά, προηγούμενα αρχεία", self.s12_settings_after_restore),
            ("Διαγραφή προηγούμενων μόνο μετά από επιβεβαίωση", self.s13_delete_previous),
            ("Κανένας κωδικός ή κλειδί στα κείμενα του GUI", self.s14_no_secrets),
        ]
        try:
            for name, action in steps:
                self.step(name, action)
        finally:
            self.a.stop()
            self.pc.stop()
            if self.gui is not None:
                self.gui.close()
        passed = sum(ok for _, ok, _ in self.results)
        print("\n" + "\n".join(f"{'PASS' if ok else 'FAIL'} {i:02d}: {name}"
                               for i, (name, ok, _) in enumerate(self.results, 1)), flush=True)
        print(f"ΣΥΝΟΛΟ: {passed}/{TOTAL} PASS", flush=True)
        return 0 if passed == TOTAL else 1


def main() -> int:
    from director.qt_gui import create_application
    app = create_application()
    base = Path(tempfile.mkdtemp(prefix="sch-maint-test-"))
    print(f"Φάκελος δοκιμής (σβήνεται στο τέλος): {base}", flush=True)
    try:
        return LiveTest(base, app).run()
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
