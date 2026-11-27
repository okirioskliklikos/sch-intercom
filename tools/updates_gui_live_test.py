"""Φάση 5B3: αυτόματη πραγματική δοκιμή Ρυθμίσεις → «Ενημερώσεις» (15 βήματα PASS/FAIL).

Ο Manager είναι το πραγματικό παράθυρο Director (PySide6, ``GuiRuntime``) με πραγματική
θύρα ενημερώσεων, σε αυτή τη διεργασία· οι Clients είναι οι πραγματικοί ``client.main``
ως διεργασίες, «εγκατεστημένοι» 1.0.0. Η δοκιμή οδηγεί το tab όπως ο διαχειριστής:
εισαγωγή πακέτου (και απόρριψη παλιού/ξένου), αυτόματες ενημερώσεις, «Έναρξη τώρα» για
έναν και για όλους, κατάσταση ανά υπολογιστή, backup με αντίγραφο πακέτων, επαναφορά
με εισαγωγή των πακέτων που λείπουν, Εφεδρικός (μόνο ανάγνωση) και αποτυχία ενημέρωσης.

Μόνο loopback 127.0.0.1, θύρες 518xx και προσωρινός φάκελος· η πραγματική εγκατάσταση
δεν αγγίζεται. Πακέτα με κλειδί δοκιμής (όχι το κλειδί του συντηρητή). Η εναλλαγή
έκδοσης (supervisor) δεν είναι μέρος αυτής της δοκιμής: οι Clients μένουν στο «έτοιμο».
Στιγμιότυπα στο ~/sch-intercom-test-screens.

    Windows:  powershell -ExecutionPolicy Bypass -File tools\\updates_gui_live_test.ps1
    Linux:    tools/updates_gui_live_test.sh
"""
from __future__ import annotations

from contextlib import ExitStack
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import package as pkg  # noqa: E402
from shared.client_registry import SignedClientRegistry  # noqa: E402
from tools.mtls_live_test import ROOT, child_env  # noqa: E402
from tools.update_live_test import (DISCOVERY_PORT, ENROLL_PORT, EXTRA_BEACON_PORT,  # noqa: E402
                                    HOST, PORT, UPDATE_PORT, WATCH_PORT, Board)

TOTAL = 15
PASSWORD = "ΜόνοΓιαΔοκιμή-2026-upd"
SCREENS = Path.home() / "sch-intercom-test-screens"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fake_stream(_gate, *, stop, ready_callback, level_callback, **_kwargs):
    """Χωρίς μικρόφωνο: η δοκιμή δεν κάνει ανακοίνωση."""
    ready_callback()
    while not stop.wait(0.05):
        level_callback(0.0)


class GuiManager:
    """Ο Manager με GUI και θύρα ενημερώσεων, όπως το ``director.main --gui``."""

    def __init__(self, test: "LiveTest", app):
        from director.backup import manager_lock
        from director.main import RegistryTrust, accept_clients, make_update_server
        from director.qt_gui import GuiRuntime
        from director.security import LastSeenStore, last_seen_path
        from shared.enrollment import EnrollmentServer

        self.app = app
        self.test = test
        self.informed: list[str] = []
        self.dialogs: list = []
        self.lock = ExitStack()
        self.lock.enter_context(manager_lock(test.dir))
        registry = SignedClientRegistry(test.registry, test.cert, test.key)
        self.trust = RegistryTrust(registry, test.cert, test.key)
        enrollment = EnrollmentServer(test.cert, test.key, self.trust, host=HOST,
                                      port=ENROLL_PORT,
                                      pending_file=test.dir / "enrollment-pending.json")
        args = SimpleNamespace(host=HOST, port=PORT, discovery_port=DISCOVERY_PORT,
                               director_id_file=test.dir / "director.id", device=None,
                               latency="low", registry_file=test.registry,
                               tls_cert=test.cert, tls_key=test.key,
                               watch_port=WATCH_PORT, extra_beacon_port=EXTRA_BEACON_PORT,
                               start_check_seconds=1.0, update_port=UPDATE_PORT,
                               update_trusted_key=[test.public])
        secure_accept = partial(accept_clients, tls_context=self.trust.context(),
                                registry=registry, context_provider=self.trust.context)
        inform = lambda _title, text: self.informed.append(text)  # noqa: E731
        self.runtime = GuiRuntime(
            args, accept_clients=secure_accept, stream_microphone=fake_stream,
            enrollment=enrollment, trust=self.trust,
            last_seen=LastSeenStore(last_seen_path(test.registry)),
            release_lock=self.lock.close, quit_app=lambda: None,
            maintenance_options={"inform": inform},
            updates=make_update_server(args, self.trust),
            updates_options={"choose_open": lambda _w, _p: test.chosen,
                             "dialog_hook": self.hook, "inform": inform})
        self.window = self.runtime.start()
        self.window.resize(1600, 900)
        self.window.show()
        self.window.show_page("settings")
        self.settings.show_tab("Ενημερώσεις")
        self.pump()

    @property
    def settings(self):
        return self.window.settings_page

    @property
    def panel(self):
        return self.settings.updates_panel

    def hook(self, dialog) -> None:
        """Διάλογος «Εισαγωγή πακέτου»: Εισαγωγή αν επιτρέπεται, αλλιώς Κλείσιμο."""
        self.dialogs.append(dialog)
        dialog.show()
        self.pump(0.1)
        if dialog.accept_button is not None:
            dialog.accept_button.click()
        else:
            dialog.close_button.click()

    def pump(self, seconds: float = 0.2) -> None:
        end = time.monotonic() + seconds
        while True:
            self.app.processEvents()
            if time.monotonic() >= end:
                return
            time.sleep(0.02)

    def wait(self, fn, timeout: float = 30.0):
        deadline = time.monotonic() + timeout
        while True:
            self.panel.refresh()
            value = fn()
            if value or time.monotonic() > deadline:
                return value
            self.pump(0.3)

    def line(self, board: Board):
        return self.panel.lines.get(board.client_id)

    def state(self, board: Board) -> str:
        line = self.line(board)
        if line is None:
            return "(χωρίς γραμμή)"
        prefix = "⚠ " if line.alert.isVisible() else ""
        return prefix + line.state.text()

    def version(self, board: Board) -> str:
        line = self.line(board)
        return line.version.text() if line is not None else ""

    def online(self, board: Board) -> bool:
        return any(cid == board.client_id and active for cid, _r, active, _a
                   in self.runtime.broadcaster.status_snapshot())

    def screenshot(self, name: str, widget=None) -> None:
        SCREENS.mkdir(parents=True, exist_ok=True)
        path = SCREENS / f"{name}.png"
        self.pump(0.2)
        (widget or self.window).grab().save(str(path))
        print(f"    Στιγμιότυπο: {path}", flush=True)

    def close(self) -> None:
        self.runtime.shutdown()
        self.lock.close()
        self.pump()


class LiveTest:
    def __init__(self, root: Path, app):
        self.root = root
        self.app = app
        self.dir = root / "manager"
        self.dir.mkdir(parents=True)
        self.cert = self.dir / "tls" / "director-tls.crt"
        self.key = self.dir / "tls" / "director-tls.key"
        self.registry = self.dir / "clients-registry.json"
        self.folder = self.dir / "updates"
        self.signing = pkg.generate_key()
        self.public = pkg.public_hex(self.signing)
        self.b1 = Board(root, "Board1")
        self.b2 = Board(root, "Board2")
        self.b3 = Board(root, "Board3")
        self.boards = [self.b1, self.b2, self.b3]
        self.usb = root / "usb"
        self.usb.mkdir()
        self.file = self.usb / "sch-intercom-backup-updates.sicb"
        self.chosen: Path | None = None
        self.gui: GuiManager | None = None
        self.results: list[tuple[str, bool, str]] = []

    # -------------------------------------------------------------- βοηθητικά
    def step(self, name: str, action) -> None:
        try:
            outcome = action()
            ok, detail = outcome if isinstance(outcome, tuple) else (bool(outcome), "")
        except Exception as exc:  # η δοκιμή αναφέρει, δεν κρύβει
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        self.results.append((name, ok, detail))
        text = f"{'PASS' if ok else 'FAIL'} {len(self.results):02d}/{TOTAL}: {name}"
        print(f"\n=== {text}" + (f" — {detail}" if detail else "") + "\n", flush=True)

    def package(self, version: str, *, key=None) -> Path:
        tree = self.root / "trees" / f"{version}-{uuid.uuid4().hex[:6]}"
        (tree / "app" / "client").mkdir(parents=True)
        (tree / "app" / "client" / "main.py").write_text(f"# {version}\n", encoding="utf-8")
        (tree / "app" / "blob.bin").write_bytes(os.urandom(300_000))
        (tree / "app" / "common.txt").write_text("ίδιο σε όλες τις εκδόσεις\n", encoding="utf-8")
        (tree / "VERSION").write_text(version + "\n", encoding="ascii")
        out = self.root / "packages" / f"sch-intercom-client-{version}-{uuid.uuid4().hex[:4]}.sicu"
        out.parent.mkdir(exist_ok=True)
        pkg.write_package(tree, out, key or self.signing, version)
        return out

    def config(self) -> dict:
        return json.loads((self.folder / "updates-config.json").read_text(encoding="utf-8"))

    def gui_import(self, path: Path):
        self.chosen = path
        before = len(self.gui.dialogs)
        self.gui.panel.import_button.click()
        self.gui.pump(0.3)
        return self.gui.dialogs[-1] if len(self.gui.dialogs) > before else None

    def dialog_rows(self, dialog) -> dict[str, str]:
        from PySide6.QtWidgets import QLabel
        rows = {}
        for key, label in dialog.rows.labels.items():
            value = dialog.rows.values[key]
            inner = getattr(value, "text", None)
            rows[label.text()] = (inner if isinstance(inner, QLabel) else value).text()
        return rows

    def port_open(self, port: int) -> bool:
        try:
            socket.create_connection((HOST, port), timeout=1).close()
            return True
        except OSError:
            return False

    def start_board(self, board: Board) -> None:
        board.start(self.cert, self.public)

    # ------------------------------------------------------------------ βήματα
    def s01_start(self):
        self.cert.parent.mkdir(parents=True, exist_ok=True)
        done = subprocess.run([sys.executable, "-m", "director.tls_identity",
                               "--cert", str(self.cert), "--key", str(self.key)],
                              cwd=ROOT, env=child_env(), capture_output=True, timeout=60)
        if done.returncode != 0:
            return False, "δεν δημιουργήθηκε ταυτότητα Manager"
        registry = SignedClientRegistry(self.registry, self.cert, self.key)
        for board in self.boards:
            board.approve(registry)
        del registry
        self.gui = GuiManager(self, self.app)
        panel = self.gui.panel
        from director import updates_view as uv
        current = panel.info.values["current"].text()
        states = [self.gui.state(b) for b in self.boards]
        ok = (self.gui.settings.current_tab == "Ενημερώσεις" and current == uv.TEXT_NOT_IMPORTED
              and states == [uv.TEXT_NEVER] * 3 and not panel.start_button.isVisible()
              and self.gui.wait(lambda: self.port_open(UPDATE_PORT), 15))
        self.gui.screenshot("enimeroseis-01-keno")
        return ok, f"Τρέχουσα «{current}», υπολογιστές {states}"

    def s02_clients_online(self):
        for board in (self.b1, self.b2):
            self.start_board(board)
        ok = self.gui.wait(lambda: all(self.gui.state(b) == "Online"
                                       and self.gui.version(b) == "1.0.0"
                                       for b in (self.b1, self.b2)), 40)
        counters = self.gui.panel.counters.text()
        return bool(ok) and "με την 1.0.0" in counters and "δεν έχει συνδεθεί" in counters, \
            f"{[self.gui.state(b) for b in self.boards]}, μετρητές «{counters}»"

    def s03_import_first(self):
        dialog = self.gui_import(self.package("1.0.1"))
        rows = self.dialog_rows(dialog) if dialog else {}
        current = self.gui.panel.info.values["current"].text()
        previous = self.gui.panel.info.values["previous"].text()
        ok = (rows.get("Έκδοση", "").startswith("1.0.1 · ")
              and rows.get("Υπογραφή") == "έγκυρη (κλειδί έκδοσης)"
              and "(πρώτο πακέτο)" in rows.get("Αλλαγές", "")
              and current.startswith("1.0.1 · ") and previous == "—"
              and self.config()["current"]["version"] == "1.0.1")
        return ok, f"διάλογος {rows}, κάρτα «{current}» / προηγούμενη «{previous}»"

    def s04_auto_download(self):
        ok = self.gui.wait(lambda: all(self.gui.state(b) == "Εναλλαγή σε 1.0.1…"
                                       for b in (self.b1, self.b2)), 60)
        ready = all(b.ready("1.0.1") for b in (self.b1, self.b2))
        self.gui.screenshot("enimeroseis-02-ependysi")
        return bool(ok) and ready, f"{[self.gui.state(b) for b in self.boards]}, έτοιμη {ready}"

    def s05_rejected_imports(self):
        older = self.gui_import(self.package("1.0.0"))
        foreign = self.gui_import(self.package("1.0.5", key=pkg.generate_key()))
        errors = [d.error.text() if d and d.accept_button is None else "" for d in (older, foreign)]
        if older is not None:
            older.show()
            self.gui.screenshot("enimeroseis-03-palaia", older)
        ok = ("δεν είναι νεότερη από την τρέχουσα 1.0.1" in errors[0]
              and "Άκυρη υπογραφή" in errors[1]
              and self.config()["current"]["version"] == "1.0.1"
              and len(list(self.folder.glob("*.sicu"))) == 1)
        return ok, f"μηνύματα {errors}"

    def s06_auto_off(self):
        from director import updates_view as uv
        self.gui.panel.auto.setChecked(False)
        auto = self.config()["auto"]
        dialog = self.gui_import(self.package("1.0.2"))
        note = dialog.note.text() if dialog and dialog.accept_button else ""
        changes = self.dialog_rows(dialog).get("Αλλαγές", "") if dialog else ""
        time.sleep(6)
        available = self.gui.wait(lambda: all(self.gui.state(b) == "Διαθέσιμη η 1.0.2"
                                              for b in (self.b1, self.b2)), 30)
        downloaded = any(b.ready("1.0.2") for b in (self.b1, self.b2))
        previous = self.gui.panel.info.values["previous"].text()
        files = sorted(p.name for p in self.folder.glob("*.sicu"))
        self.gui.screenshot("enimeroseis-04-xoris-aftomates")
        ok = (auto is False and note == uv.TEXT_AUTO_OFF_NOTE
              and "σε σχέση με την 1.0.1" in changes and bool(available) and not downloaded
              and previous == "1.0.1" and len(files) == 2)
        return ok, (f"auto {auto}, αλλαγές «{changes}», {[self.gui.state(b) for b in self.boards]}, "
                    f"λήψη {downloaded}, προηγούμενη {previous}, αρχεία {files}")

    def s07_start_one(self):
        line = self.gui.line(self.b1)
        enabled = line.start_action.isEnabled() and line.menu_button.isVisible()
        line.start_action.trigger()
        one = self.gui.wait(lambda: self.gui.state(self.b1) == "Εναλλαγή σε 1.0.2…", 60)
        other = self.gui.state(self.b2)
        return enabled and bool(one) and self.b1.ready("1.0.2") and \
            other == "Διαθέσιμη η 1.0.2" and not self.b2.ready("1.0.2"), \
            f"Board1 «{self.gui.state(self.b1)}», Board2 «{other}»"

    def s08_start_all(self):
        self.gui.panel.start_button.click()
        ok = self.gui.wait(lambda: self.gui.state(self.b2) == "Εναλλαγή σε 1.0.2…", 60)
        self.gui.screenshot("enimeroseis-05-enarxi-olous")
        return bool(ok) and self.b2.ready("1.0.2"), f"Board2 «{self.gui.state(self.b2)}»"

    def s09_offline_and_never(self):
        from director import updates_view as uv
        self.b2.stop()
        ok = self.gui.wait(lambda: self.gui.state(self.b2).startswith("Εκτός σύνδεσης · "), 30)
        never = self.gui.state(self.b3)
        counters = self.gui.panel.counters.text()
        return bool(ok) and never == uv.TEXT_NEVER and "εκτός σύνδεσης" in counters, \
            f"Board2 «{self.gui.state(self.b2)}», Board3 «{never}», μετρητές «{counters}»"

    def s10_backup_with_packages(self):
        seen = {}

        def fill(dialog):
            seen["visible"] = dialog.has_updates and dialog.updates.isChecked()
            dialog.path.setText(str(self.file))
            seen["detail"] = dialog.updates_detail.text()
            dialog.password.setText(PASSWORD)
            dialog.confirm.setText(PASSWORD)
            dialog.show()
            self.gui.pump()
            self.gui.screenshot("enimeroseis-06-backup", dialog)
            dialog.create.click()

        self.gui.settings.dialog_hook = fill
        self.gui.settings.open_backup()
        copy = self.usb / (self.file.stem + "-updates")
        names = sorted(p.name for p in copy.glob("*.sicu")) if copy.is_dir() else []
        same = names and all(sha256(copy / n) == sha256(self.folder / n) for n in names)
        expected = ["sch-intercom-client-1.0.1.sicu", "sch-intercom-client-1.0.2.sicu"]
        return (seen.get("visible") and seen.get("detail", "").startswith("1.0.2 και 1.0.1 · ")
                and self.file.is_file() and names == expected and bool(same),
                f"checkbox {seen.get('visible')}, «{seen.get('detail')}», αντίγραφο {names}")

    def s11_missing_package(self):
        for path in self.folder.glob("*.sicu"):
            path.unlink()
        panel = self.gui.panel
        shown = self.gui.wait(lambda: panel.missing.isVisible(), 10)
        text = panel.missing.text()
        self.gui.screenshot("enimeroseis-07-leipei")
        return (bool(shown) and "Λείπει το αρχείο του πακέτου 1.0.2" in text
                and not panel.start_button.isEnabled() and not panel.signature.isVisible(),
                f"σημείωση «{text[:80]}…», Έναρξη ενεργή {panel.start_button.isEnabled()}")

    def s12_restore_imports_packages(self):
        seen = {}

        def fill(dialog):
            dialog.path.setText(str(self.file))
            dialog.password.setText(PASSWORD)
            dialog.load()
            seen["row"] = dialog.rows.values["updates"].text()
            seen["box"] = (dialog.rows.values["updates_copy"].isVisibleTo(dialog)
                           and dialog.updates.isChecked())
            dialog.show()
            self.gui.pump()
            self.gui.screenshot("enimeroseis-08-epanafora", dialog)
            dialog.restore.click()

        self.gui.settings.dialog_hook = fill
        self.gui.settings.open_restore()
        restored = self.gui.window.restore_succeeded is True
        names = sorted(p.name for p in self.folder.glob("*.sicu"))
        copy = self.usb / (self.file.stem + "-updates")
        same = names and all(sha256(copy / n) == sha256(self.folder / n) for n in names)
        config = self.config()
        self.gui.close()
        self.gui = None
        return (seen.get("row") == "1.0.2 (τρέχουσα), 1.0.1 · λείπουν εδώ" and seen.get("box")
                and restored and len(names) == 2 and bool(same) and config["auto"] is False
                and config["current"]["version"] == "1.0.2",
                f"γραμμή «{seen.get('row')}», checkbox {seen.get('box')}, επαναφορά {restored}, "
                f"πακέτα {names}, auto {config['auto']}")

    def s13_backup_role_read_only(self):
        self.gui = GuiManager(self, self.app)
        panel = self.gui.panel
        self.gui.pump(1.0)
        panel.refresh()
        menus = any(line.menu_button.isVisible() for line in panel.lines.values())
        ok = (panel.package_lock.isVisible() and panel.backup_note.isVisible()
              and not panel.import_button.isEnabled() and not panel.start_button.isEnabled()
              and not panel.auto.isEnabled() and not menus and not panel.missing.isVisible()
              and panel.info.values["current"].text().startswith("1.0.2 · ")
              and not self.port_open(UPDATE_PORT))
        detail = (f"λουκέτο {panel.package_lock.isVisible()}, κουμπιά "
                  f"{panel.import_button.isEnabled()}/{panel.start_button.isEnabled()}, "
                  f"⋮ {menus}, θύρα ενημερώσεων ανοιχτή {self.port_open(UPDATE_PORT)}")
        self.gui.screenshot("enimeroseis-09-efedrikos")
        self.gui.close()
        self.gui = None
        return ok, detail

    def s14_primary_again(self):
        codes = []
        for command in ("primary", "serve-on"):
            done = subprocess.run(
                [sys.executable, "-m", "director.manager_state", command,
                 "--registry-file", str(self.registry), "--yes"],
                cwd=ROOT, env=child_env(), capture_output=True, timeout=60)
            codes.append(done.returncode)
        self.gui = GuiManager(self, self.app)
        panel = self.gui.panel
        back = self.gui.wait(lambda: self.gui.online(self.b1)
                             and self.gui.state(self.b1) == "Διαθέσιμη η 1.0.2", 60)
        ok = (codes == [0, 0] and not panel.package_lock.isVisible()
              and panel.import_button.isEnabled() and panel.start_button.isEnabled()
              and bool(back) and self.port_open(UPDATE_PORT))
        return ok, f"CLI {codes}, Board1 «{self.gui.state(self.b1)}», κουμπιά {panel.import_button.isEnabled()}"

    def s15_failed_update(self):
        from director import updates_view as uv
        forged = self.package("1.0.3", key=pkg.generate_key())
        self.gui_import(self.package("1.0.3"))
        # «Κακόβουλος» Manager: άλλο περιεχόμενο με ενημερωμένο hash/μέγεθος.
        config = self.config()
        target = self.folder / config["current"]["file"]
        shutil.copyfile(forged, target)
        config["current"]["sha256"] = sha256(target)
        config["current"]["size"] = target.stat().st_size
        (self.folder / "updates-config.json").write_text(json.dumps(config), encoding="utf-8")
        self.gui.pump(0.5)
        self.gui.wait(lambda: self.gui.line(self.b1).start_action.isEnabled(), 20)
        self.gui.line(self.b1).start_action.trigger()
        failed = self.gui.wait(lambda: self.gui.state(self.b1) == "⚠ Αποτυχία 1.0.3", 60)
        tip = self.gui.line(self.b1).state.toolTip()
        counters = self.gui.panel.counters.text()
        self.gui.screenshot("enimeroseis-10-apotyxia")
        return (bool(failed) and "Άκυρη υπογραφή" in tip and uv.TEXT_NO_RETRY in tip
                and "αποτυχία" in counters and not self.b1.ready("1.0.3"),
                f"Board1 «{self.gui.state(self.b1)}», tooltip «{tip[:70]}…»")

    def close(self) -> None:
        for board in self.boards:
            board.stop()
        if self.gui is not None:
            self.gui.close()
            self.gui = None

    def run(self) -> int:
        steps = [
            ("Manager με GUI: tab «Ενημερώσεις» χωρίς πακέτο, 3 υπολογιστές", self.s01_start),
            ("Board1/Board2 συνδέονται: έκδοση 1.0.0 και «Online»", self.s02_clients_online),
            ("Εισαγωγή 1.0.1 από το GUI: διάλογος (έκδοση, υπογραφή, αλλαγές), κάρτα",
             self.s03_import_first),
            ("Αυτόματες: οι συνδεδεμένοι κατεβάζουν, «Εναλλαγή σε 1.0.1…»", self.s04_auto_download),
            ("Εισαγωγή παλαιότερης και ξένης υπογραφής: μήνυμα, τίποτα δεν αλλάζει",
             self.s05_rejected_imports),
            ("Χωρίς αυτόματες: εισαγωγή 1.0.2, «Διαθέσιμη», καμία λήψη, προηγούμενη 1.0.1",
             self.s06_auto_off),
            ("⋮ «Έναρξη τώρα» μόνο στον Board1", self.s07_start_one),
            ("«Έναρξη τώρα σε όλους»", self.s08_start_all),
            ("Εκτός σύνδεσης και «Δεν έχει συνδεθεί»", self.s09_offline_and_never),
            ("Backup με αντίγραφο πακέτων (1.0.2 + 1.0.1, ίδιο SHA-256)",
             self.s10_backup_with_packages),
            ("Λείπει το πακέτο: κόκκινη σημείωση, «Έναρξη τώρα» ανενεργό", self.s11_missing_package),
            ("Επαναφορά: «λείπουν εδώ», εισαγωγή των πακέτων από το -updates",
             self.s12_restore_imports_packages),
            ("Μετά την επαναφορά (Εφεδρικός): μόνο ανάγνωση, θύρα κλειστή",
             self.s13_backup_role_read_only),
            ("Κύριος ξανά: ενεργό tab, ο Board1 ξανασυνδέεται", self.s14_primary_again),
            ("Αποτυχία ενημέρωσης (άκυρη υπογραφή): «⚠ Αποτυχία», tooltip με λόγο",
             self.s15_failed_update),
        ]
        try:
            for index, (name, action) in enumerate(steps):
                self.step(name, action)
                if index == 0 and not self.results[-1][1]:
                    for later, _ in steps[1:]:
                        self.results.append((later, False, "δεν έγινε το προηγούμενο βήμα"))
                    break
        finally:
            self.close()
        passed = sum(ok for _, ok, _ in self.results)
        print("\n" + "\n".join(f"{'PASS' if ok else 'FAIL'} {i:02d}: {name}"
                               for i, (name, ok, _) in enumerate(self.results, 1)), flush=True)
        print(f"ΣΥΝΟΛΟ: {passed}/{TOTAL} PASS", flush=True)
        return 0 if passed == TOTAL else 1


def main() -> int:
    from director.qt_gui import create_application
    app = create_application()
    root = Path(tempfile.mkdtemp(prefix="sch-updates-gui-test-"))
    print(f"Προσωρινή δοκιμή μόνο σε loopback: {root}", flush=True)
    try:
        return LiveTest(root, app).run()
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
