"""4.2D2: αυτόματη πραγματική δοκιμή του GUI Κύριου/Εφεδρικού, μόνο loopback.

Δύο «υπολογιστές» Manager της ίδιας εγκατάστασης στον ίδιο υπολογιστή:

- A: ``director.main`` (CLI) ως διεργασία, Κύριος, θύρες 51555/51556/51559.
- B: το πραγματικό παράθυρο Director (PySide6, ``GuiRuntime``) σε αυτή τη διεργασία,
  επαναφερμένο από backup του A, θύρες 51565/51566/51569.

Οι beacons κάθε Manager φτάνουν στον άλλο από ξεχωριστές θύρες παρακολούθησης
(51557/51567), όπως στο tools/manager_role_live_test.py. Η δοκιμή οδηγεί το παράθυρο
του B όπως ο διαχειριστής (μπάρα, ειδοποιήσεις, κάρτα «Ρόλος Manager», διάλογοι,
«Μητρώο υπολογιστών») και ελέγχει πραγματικές θύρες, αρχεία και Clients.

Μόνο προσωρινός φάκελος· η πραγματική εγκατάσταση (``%USERPROFILE%\\.school-intercom``)
δεν αγγίζεται. Δεν χρειάζεται μικρόφωνο. Στιγμιότυπα στο ~/sch-intercom-test-screens.

    Windows:  powershell -ExecutionPolicy Bypass -File tools\\manager_role_gui_live_test.ps1
    Linux:    tools/manager_role_gui_live_test.sh
"""
from __future__ import annotations

from contextlib import ExitStack
from functools import partial
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.manager_role_live_test import (ROOM, TestDevice, TestMachine,  # noqa: E402
                                          check_port)
from tools.mtls_live_test import JOINED  # noqa: E402

TOTAL = 16
HOST = "127.0.0.1"
SCREENS = Path.home() / "sch-intercom-test-screens"


def fake_stream(_gate, *, stop, ready_callback, level_callback, **_kwargs):
    ready_callback()
    while not stop.wait(0.05):
        level_callback(0.0)


class RoleGui:
    """Ο Manager B με GUI, όπως το ``director.main --gui``, στις θύρες του B."""

    def __init__(self, machine: TestMachine, app):
        from director.backup import manager_lock
        from director.main import RegistryTrust, accept_clients
        from director.qt_gui import GuiRuntime
        from director.security import LastSeenStore, last_seen_path
        from shared.client_registry import SignedClientRegistry
        from shared.enrollment import EnrollmentServer

        self.app = app
        self.machine = machine
        self.informed: list[str] = []
        self.dialogs: list = []
        self.answers: list[str] = []
        self.lock = ExitStack()
        self.lock.enter_context(manager_lock(machine.dir))
        registry = SignedClientRegistry(machine.registry, machine.cert, machine.key)
        self.trust = RegistryTrust(registry, machine.cert, machine.key)
        enrollment = EnrollmentServer(machine.cert, machine.key, self.trust, host=HOST,
                                      port=machine.enrollment,
                                      pending_file=machine.dir / "enrollment-pending.json")
        args = SimpleNamespace(host=HOST, port=machine.port, discovery_port=machine.discovery,
                               director_id_file=machine.dir / "director.id", device=None,
                               latency="low", registry_file=machine.registry,
                               tls_cert=machine.cert, tls_key=machine.key,
                               watch_port=machine.watch, extra_beacon_port=machine.extra,
                               start_check_seconds=3.0)
        secure_accept = partial(accept_clients, tls_context=self.trust.context(),
                                registry=registry, context_provider=self.trust.context)
        inform = lambda _title, text: self.informed.append(text)  # noqa: E731
        self.runtime = GuiRuntime(
            args, accept_clients=secure_accept, stream_microphone=fake_stream,
            enrollment=enrollment, trust=self.trust,
            last_seen=LastSeenStore(last_seen_path(machine.registry)),
            release_lock=self.lock.close, quit_app=lambda: None,
            maintenance_options={"inform": inform},
            role_options={"dialog_hook": self.hook, "inform": inform})
        self.window = self.runtime.start()
        self.window.resize(1366, 860)
        self.window.show()
        self.pump()

    def hook(self, dialog) -> None:
        self.dialogs.append(dialog)
        dialog.show()
        self.pump(0.1)
        if self.answers and self.answers.pop(0) == "accept":
            dialog.accept()
        else:
            dialog.reject()

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

    def screenshot(self, name: str, widget=None) -> None:
        SCREENS.mkdir(parents=True, exist_ok=True)
        path = SCREENS / f"role-{name}.png"
        (widget or self.window).grab().save(str(path))
        print(f"    Στιγμιότυπο: {path}", flush=True)

    def header(self) -> tuple[str, str]:
        label = self.window.connection_label
        return label.text(), label.property("state")

    @property
    def card(self):
        return self.window.security_page.role_card

    def state(self) -> dict:
        path = self.machine.dir / "manager-state.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def online(self, client_id: str | None) -> bool:
        return any(cid == client_id and active for cid, _r, active, _a
                   in self.runtime.broadcaster.status_snapshot())

    def close(self) -> None:
        self.runtime.shutdown()
        self.lock.close()
        self.pump()


class LiveTest:
    def __init__(self, root: Path, app):
        self.root = root
        self.app = app
        self.a = TestMachine(root, "pc-a", 51555, 51556, 51559, 51557, 51567)
        self.b = TestMachine(root, "pc-b", 51565, 51566, 51569, 51567, 51557)
        self.one, self.two = TestDevice(root, "Board1"), TestDevice(root, "Board2")
        self.backup_file = root / "transfer.sicb"
        self.export_a = root / "usb" / "export-a.sicr"
        self.export_b = root / "usb" / "export-b.sicr"
        self.gui: RoleGui | None = None
        self.results = []

    def step(self, title, function):
        try:
            answer = function()
            ok, detail = answer if isinstance(answer, tuple) else (bool(answer), "")
        except Exception as exc:  # η δοκιμή αναφέρει, δεν κρύβει
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        self.results.append((title, ok, detail))
        print(f"\n=== {'PASS' if ok else 'FAIL'} {len(self.results):02d}/{TOTAL}: "
              f"{title}" + (f" — {detail}" if detail else "") + "\n", flush=True)

    def version(self, machine: TestMachine) -> int:
        return json.loads(machine.registry.read_text(encoding="utf-8"))["registry"][
            "registry_version"]

    # ------------------------------------------------------------------ βήματα
    def s01_primary_a(self):
        self.a.create_identity()
        director = self.a.start()
        marker = director.mark()
        device = self.one.use(self.a)
        code = device.enroll(self.a.cert)
        line = director.wait_for("Νέο αίτημα", f"| {ROOM} | {device.name}", since=marker,
                                 timeout=10)
        short = line.split("Νέο αίτημα")[1].split("|")[0].strip() if line else ""
        approved = self.a.command(f"approve {short} {code}", "Εγκρίθηκε:")
        marker = director.mark()
        device.start(self.a.cert)
        joined = director.wait_for(device.client_id, JOINED, since=marker, timeout=40)
        return approved and joined is not None, f"έγκριση {approved}, σύνδεση {joined is not None}"

    def s02_restore_b(self):
        rc, _ = self.a.backup("create", "--out", str(self.backup_file), "--media-copy", "no")
        rc2, _ = self.b.backup("restore", str(self.backup_file))
        state = json.loads((self.b.dir / "manager-state.json").read_text(encoding="utf-8"))
        return (rc == 0 and rc2 == 0 and state.get("role") == "backup"
                and state.get("notice") == "restored_as_backup"), f"backup rc={rc}, restore rc={rc2}"

    def s03_gui_restored(self):
        self.gui = RoleGui(self.b, self.app)
        window = self.gui.window
        header = self.gui.header()
        bar = window.restored_bar.isVisible()
        closed = not check_port(self.b.port) and not check_port(self.b.enrollment)
        self.gui.screenshot("01-epanafora")
        return (header == ("Χωρίς εξυπηρέτηση (Εφεδρικός)", "stopped")
                and window.connection_dot.hollow and bar and closed), (
                    f"μπάρα «{header[0]}», μπλε ειδοποίηση {bar}, θύρες κλειστές {closed}")

    def s04_sees_a(self):
        window = self.gui.window
        window.show_page("security")
        window.security_page.show_tab("Πιστοποιητικά")
        other = self.gui.wait(lambda: "εξυπηρετεί" in self.gui.card.rows.values["other"].text(),
                              timeout=15)
        card = self.gui.card
        texts = (card.rows.values["role"].text(), card.rows.values["serving"].text(),
                 card.serve_button.text(), card.role_button.text())
        self.gui.screenshot("02-rolos-efedrikos")
        return (bool(other) and texts == ("Εφεδρικός", "Σταματημένη", "Έναρξη εξυπηρέτησης",
                                          "Αλλαγή σε Κύριο…")), (
                    f"άλλος Manager «{card.rows.values['other'].text()}», κάρτα {texts}")

    def s05_locked_computers(self):
        window = self.gui.window
        page = window.security_page
        page.show_tab("Υπολογιστές")
        self.gui.pump()
        menus = [r.menu_button for r in page.computer_rows.values()]
        menus += [h.menu_button for h in page.room_headers.values()]
        locked = (page.closed_card.isVisible() and page.requests_lock.isVisible()
                  and page.computers_lock.isVisible() and bool(menus)
                  and not any(b.isVisible() for b in menus))
        self.gui.screenshot("03-ypologistes-efedrikos")
        return locked, (f"κλειστή κάρτα «{page.closed_note.label.text()}», "
                        f"{len(menus)} μενού ⋮ όλα κρυφά {not any(b.isVisible() for b in menus)}")

    def s06_stay_backup(self):
        window = self.gui.window
        window.restored_bar.buttons[0].click()
        self.gui.pump()
        state = self.gui.state()
        return (state.get("notice") is None and state.get("role") == "backup"
                and not window.restored_bar.isVisible()), f"κατάσταση {state}"

    def s07_newer_bar(self):
        before = self.version(self.a)
        moved = self.a.command(f"set-room {self.one.client_id} C2627-Γ2", "C2627-Γ2")
        bar = self.gui.wait(lambda: self.gui.window.newer_bar.isVisible(), timeout=15)
        text = self.gui.window.newer_bar.text.text()
        self.gui.screenshot("04-neotero-mitroo")
        return (moved and bool(bar) and f"v{before + 1}" in text
                and f"εδώ v{self.version(self.b)}" in text), f"«{text}»"

    def s08_import_newer(self):
        exported = self.a.command(f"registry export {self.export_a}", "Εξαγωγή:")
        seen = {}

        def run(dialog):
            dialog.open(self.export_a)
            seen["relation"] = dialog.view.relation if dialog.view else None
            seen["changes"] = dict(dialog.view.changes) if dialog.view else {}
            dialog.show()
            self.gui.pump()
            self.gui.screenshot("05-eisagogi", dialog)
            dialog.run()
        self.gui.window.settings_page.dialog_hook = run
        self.gui.window.newer_bar.buttons[0].click()
        self.gui.window.settings_page.dialog_hook = None
        gone = self.gui.wait(lambda: not self.gui.window.newer_bar.isVisible(), timeout=10)
        same = self.version(self.b) == self.version(self.a)
        return (exported and seen.get("relation") == "newer"
                and "Αλλάζουν αίθουσα" in seen.get("changes", {}) and same and bool(gone)), (
                    f"σχέση {seen.get('relation')}, αλλαγές {seen.get('changes')}, "
                    f"ίδια έκδοση {same}, μπάρα έφυγε {bool(gone)}")

    def s09_start_refused(self):
        from director.qt_role import StartConflictDialog
        window = self.gui.window
        window.show_page("security")
        window.security_page.show_tab("Πιστοποιητικά")
        self.gui.answers = ["reject"]
        self.gui.card.serve_button.click()
        self.gui.pump()
        dialog = self.gui.dialogs[-1] if self.gui.dialogs else None
        if dialog is not None:
            self.gui.screenshot("06-enarxi-sygkrousi", dialog)
        return (isinstance(dialog, StartConflictDialog) and not check_port(self.b.port)
                and self.gui.state().get("serving") is False), (
                    f"διάλογος {type(dialog).__name__}, θύρα B {check_port(self.b.port)}")

    def s10_force_conflict(self):
        marker = self.a.director.mark()
        self.gui.answers = ["accept"]
        self.gui.card.serve_button.click()
        conflict = self.gui.wait(lambda: self.gui.window.conflict_bar.isVisible(), timeout=15)
        header = self.gui.header()
        caution = self.a.director.wait_for("ΠΡΟΣΟΧΗ: εξυπηρετεί και", since=marker, timeout=10)
        hidden = not self.gui.window.conflict_stop.isVisible()
        self.gui.screenshot("07-dyo-manager")
        return (bool(conflict) and header == ("Δύο Manager σε λειτουργία (Εφεδρικός)", "conflict")
                and check_port(self.b.port) and caution is not None and hidden), (
                    f"μπάρα «{header[0]}», θύρα B {check_port(self.b.port)}, "
                    f"προειδοποίηση στον A {caution is not None}, "
                    f"κουμπί μπάρας κρυφό στα Πιστοποιητικά {hidden}")

    def s11_stop_from_bar(self):
        window = self.gui.window
        window.show_page("home")
        self.gui.pump()
        visible = window.conflict_stop.isVisible()
        self.gui.answers = ["reject"]
        window.conflict_stop.click()
        self.gui.pump()
        from director.qt_role import StopDialog
        asked = isinstance(self.gui.dialogs[-1], StopDialog) and check_port(self.b.port)
        if asked:
            self.gui.screenshot("07b-diakopi", self.gui.dialogs[-1])
        self.gui.answers = ["accept"]
        window.conflict_stop.click()
        self.gui.pump()
        stopped = not check_port(self.b.port)
        return (visible and asked and stopped and not window.conflict_bar.isVisible()
                and self.gui.state().get("serving") is False), (
                    f"κουμπί στην Κεντρική {visible}, επιβεβαίωση (Άκυρο κράτησε) {asked}, "
                    f"θύρα B κλειστή {stopped}")

    def s12_promote_and_serve(self):
        from director.qt_role import PromoteDialog
        stopped = self.a.command("serve off", "Εξυπηρέτηση: σταματημένη")
        gone = self.gui.wait(lambda: not self.gui.window.role_status.others, timeout=15)
        window = self.gui.window
        window.show_page("security")
        window.security_page.show_tab("Πιστοποιητικά")
        self.gui.answers = ["accept"]
        self.gui.card.role_button.click()
        up = self.gui.wait(lambda: self.gui.header() == ("Συνδεδεμένος (Κύριος)", "up"),
                           timeout=15)
        dialog = self.gui.dialogs[-1]
        self.gui.screenshot("08-kyrios")
        return (stopped and bool(gone) and isinstance(dialog, PromoteDialog)
                and dialog.start.isChecked() and bool(up) and check_port(self.b.port)
                and check_port(self.b.enrollment)), (
                    f"A σταμάτησε {stopped}, B «{self.gui.header()[0]}», "
                    f"θύρες B {check_port(self.b.port)}/{check_port(self.b.enrollment)}")

    def s13_clients_on_b(self):
        self.one.stop()
        self.one.use(self.b)
        self.one.start(self.b.cert)
        joined = self.gui.wait(lambda: self.gui.online(self.one.client_id), timeout=40)
        page = self.gui.window.security_page
        page.show_tab("Υπολογιστές")
        code = self.two.use(self.b).enroll(self.b.cert)
        model = self.gui.window.security
        request = self.gui.wait(lambda: next((r for r in model.requests()
                                              if r.name == self.two.name), None), timeout=10)
        decision = None
        if request is not None and code:
            # Όπως ο διαχειριστής: κωδικός στην κάρτα του αιτήματος και «Έγκριση».
            page.refresh()
            row = page.request_rows.get(request.request_id)
            if row is not None:
                row.code.setText(code)
                row.approve.click()
                decision = SimpleNamespace(text=row.result.text())
        approved = self.gui.wait(lambda: any(
            c.client_id == self.two.client_id for room in model.rooms()
            for c in room.computers), timeout=10)
        return bool(joined) and bool(approved), (
            f"Client 1 online στον B {bool(joined)}, αίτημα {request is not None}, "
            f"κωδικός {bool(code)}, απόφαση «{decision.text if decision else '—'}», "
            f"εγγραφή Client 2 από το GUI {bool(approved)}")

    def s14_export_from_settings(self):
        from director import maintenance as m
        page = self.gui.window.settings_page
        self.gui.window.show_page("settings")
        before = page.registry_rows.values["unexported"].text()
        page.choose_save = lambda _parent, _current: self.export_b
        page.export_button.click()
        after = page.registry_rows.values["unexported"].text()
        self.gui.screenshot("09-mitroo-ypologiston")
        return (self.export_b.is_file() and before != m.TEXT_UNEXPORTED_NONE
                and after == m.TEXT_UNEXPORTED_NONE), f"πριν «{before}», μετά «{after}»"

    def s15_demote(self):
        window = self.gui.window
        window.show_page("security")
        window.security_page.show_tab("Πιστοποιητικά")
        self.gui.answers = ["accept"]
        self.gui.card.role_button.click()
        header = self.gui.wait(lambda: self.gui.header()[0] == "Συνδεδεμένος (Εφεδρικός)",
                               timeout=10)
        closed = self.gui.wait(lambda: not check_port(self.b.enrollment), timeout=10)
        online = self.gui.online(self.one.client_id)
        return (bool(header) and bool(closed) and check_port(self.b.port) and online), (
            f"«{self.gui.header()[0]}», εγγραφή κλειστή {bool(closed)}, "
            f"Client 1 συνδεδεμένος {online}")

    def s16_restart_keeps_state(self):
        self.gui.close()
        self.gui = None
        self.gui = RoleGui(self.b, self.app)
        up = self.gui.wait(lambda: self.gui.header()[0] == "Συνδεδεμένος (Εφεδρικός)",
                           timeout=15)
        state = self.gui.state()
        joined = self.gui.wait(lambda: self.gui.online(self.one.client_id), timeout=40)
        return (bool(up) and state.get("role") == "backup" and state.get("serving") is True
                and bool(joined)), (f"«{self.gui.header()[0]}», κατάσταση {state}, "
                                    f"Client 1 ξανά online {bool(joined)}")

    def run(self) -> int:
        steps = [
            ("A Κύριος (CLI), Client 1 εγγράφεται και συνδέεται", self.s01_primary_a),
            ("Backup A → επαναφορά στον B: Εφεδρικός με ειδοποίηση", self.s02_restore_b),
            ("GUI B: «○ Χωρίς εξυπηρέτηση (Εφεδρικός)», μπλε ειδοποίηση, θύρες κλειστές",
             self.s03_gui_restored),
            ("Κάρτα «Ρόλος Manager»: βλέπει τον A, σωστά κουμπιά", self.s04_sees_a),
            ("Υπολογιστές κλειδωμένοι στον Εφεδρικό, χωρίς μενού ⋮", self.s05_locked_computers),
            ("«Να μείνει Εφεδρικός»: φεύγει μόνο η ειδοποίηση", self.s06_stay_backup),
            ("Αλλαγή στον A → κόκκινη ειδοποίηση «νεότερο μητρώο» στον B", self.s07_newer_bar),
            ("Εισαγωγή μητρώου από την ειδοποίηση: ίδια έκδοση με τον A", self.s08_import_newer),
            ("Έναρξη στον B με ενεργό A: διάλογος σύγκρουσης, Άκυρο", self.s09_start_refused),
            ("«Έναρξη παρ' όλα αυτά»: κόκκινη μπάρα και προειδοποίηση στον A",
             self.s10_force_conflict),
            ("«Διακοπή εξυπηρέτησης εδώ» από την κόκκινη ειδοποίηση, με επιβεβαίωση", self.s11_stop_from_bar),
            ("A σταματά, B «Αλλαγή σε Κύριο» με έναρξη: θύρες ανοιχτές", self.s12_promote_and_serve),
            ("Client 1 συνδέεται στον B, Client 2 εγκρίνεται από το GUI", self.s13_clients_on_b),
            ("Εξαγωγή από «Μητρώο υπολογιστών»: «καμία αλλαγή ✓»", self.s14_export_from_settings),
            ("«Αλλαγή σε Εφεδρικό»: εγγραφή κλειστή, εξυπηρέτηση συνεχίζει", self.s15_demote),
            ("Επανεκκίνηση GUI B: ρόλος και εξυπηρέτηση διατηρούνται", self.s16_restart_keeps_state),
        ]
        try:
            for name, action in steps:
                self.step(name, action)
        finally:
            for device in (self.one, self.two):
                device.stop()
            if self.gui is not None:
                self.gui.close()
            self.a.stop()
        passed = sum(ok for _, ok, _ in self.results)
        print("\n" + "\n".join(f"{'PASS' if ok else 'FAIL'} {i:02d}: {name}"
                               for i, (name, ok, _) in enumerate(self.results, 1)), flush=True)
        print(f"ΣΥΝΟΛΟ: {passed}/{TOTAL} PASS", flush=True)
        return 0 if passed == TOTAL else 1


def main() -> int:
    from director.qt_gui import create_application
    app = create_application()
    root = Path(tempfile.mkdtemp(prefix="sch-role-gui-test-"))
    (root / "usb").mkdir()
    print(f"Προσωρινή δοκιμή μόνο σε loopback: {root}", flush=True)
    try:
        return LiveTest(root, app).run()
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
