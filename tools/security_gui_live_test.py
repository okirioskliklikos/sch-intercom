"""Αυτόματη πραγματική δοκιμή της σελίδας «Ασφάλεια» (GUI Director) σε δύο υπολογιστές.

Στο Linux ανοίγει το πραγματικό παράθυρο Director (PySide6) και η δοκιμή το οδηγεί
όπως ο διαχειριστής: μενού, κωδικός, Έγκριση/Απόρριψη, μενού «⋯», όριο αιτημάτων.
Ο Client στα Windows είναι ο ίδιος με το tools/mtls_live_test (ίδια εντολή).
Χρησιμοποιούνται προσωρινά κλειδιά/μητρώο: η κανονική εγκατάσταση δεν αγγίζεται.
Οι διάλογοι «Αλλαγή αίθουσας»/«Μετονομασία»/«Ανάκληση» απαντώνται από τη δοκιμή.

    Linux:    tools/security_gui_live_test.sh
    Windows:  powershell -ExecutionPolicy Bypass -File tools\\mtls_live_test.ps1

Στιγμιότυπα της σελίδας γράφονται στο ~/sch-intercom-test-screens/.
"""
from __future__ import annotations

import argparse
import json
import os
from functools import partial
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.mtls_live_test import ClientRun, DirectorRun, OTHER_ROOM, RENAMED, ROOM

NAME = "Board1"
SCREENS = Path.home() / "sch-intercom-test-screens"


class Gui:
    """Εκτέλεση συναρτήσεων στο νήμα του Qt από το νήμα της δοκιμής."""

    def __init__(self):
        from PySide6.QtCore import QObject, Qt, Signal

        class Invoker(QObject):
            call = Signal(object)

        self.invoker = Invoker()
        self.invoker.call.connect(lambda job: job(), Qt.ConnectionType.QueuedConnection)

    def __call__(self, fn, timeout: float = 30.0):
        if threading.current_thread() is threading.main_thread():
            return fn()
        done = threading.Event()
        box: dict = {}

        def job():
            try:
                box["value"] = fn()
            except BaseException as exc:  # αναφέρεται στο νήμα της δοκιμής
                box["error"] = exc
            finally:
                done.set()

        self.invoker.call.emit(job)
        if not done.wait(timeout):
            raise TimeoutError("Το παράθυρο δεν απάντησε")
        if "error" in box:
            raise box["error"]
        return box.get("value")


class SecurityGuiRun(DirectorRun):
    def __init__(self, work: Path, gui: Gui, app):
        super().__init__(work)
        self.gui = gui
        self.app = app
        self.runtime = None
        self.answers: list[str | None] = []
        self.exit_code = 1

    # ------------------------------------------------------------ Director GUI
    def start_gui(self) -> None:
        from director.main import RegistryTrust, accept_clients, stream_microphone
        from director.qt_gui import GuiRuntime
        from director.security import LastSeenStore, last_seen_path
        from shared.audio import DEFAULT_PORT
        from shared.client_registry import SignedClientRegistry
        from shared.discovery import DISCOVERY_PORT
        from shared.enrollment import ENROLL_PORT, EnrollmentServer

        def build():
            registry = SignedClientRegistry(self.registry, self.cert, self.key)
            trust = RegistryTrust(registry, self.cert, self.key)
            enrollment = EnrollmentServer(self.cert, self.key, trust, port=ENROLL_PORT,
                                          pending_file=self.registry.with_name(
                                              "enrollment-pending.json"))
            args = SimpleNamespace(host="0.0.0.0", port=DEFAULT_PORT,
                                   discovery_port=DISCOVERY_PORT,
                                   director_id_file=self.director_id, device=None,
                                   latency="low")
            secure_accept = partial(accept_clients, tls_context=trust.context(),
                                    registry=registry, context_provider=trust.context)
            options = {"ask_text": self._ask_text, "confirm": lambda *_: True,
                       "warn": lambda text: print(f"    [Προειδοποίηση] {text}", flush=True)}
            runtime = GuiRuntime(args, accept_clients=secure_accept,
                                 stream_microphone=stream_microphone, enrollment=enrollment,
                                 trust=trust, last_seen=LastSeenStore(
                                     last_seen_path(self.registry)),
                                 security_options=options)
            window = runtime.start()
            window.resize(max(window.width(), 1200), max(window.height(), 720))
            window.show()
            return runtime

        self.runtime = self.gui(build)

    def stop_gui(self) -> None:
        if self.runtime is not None:
            runtime, self.runtime = self.runtime, None
            self.gui(runtime.shutdown)

    def _ask_text(self, title, label, value):
        print(f"    [Διάλογος] {title}: {value!r} → {self.answers[0]!r}", flush=True)
        return self.answers.pop(0)

    @property
    def window(self):
        return self.runtime.window

    @property
    def page(self):
        return self.runtime.window.security_page

    def refreshed(self, fn):
        """Ανανέωση της σελίδας και ανάγνωση κατάστασης στο νήμα του Qt."""
        def job():
            self.page.refresh()
            return fn()
        return self.gui(job)

    def wait(self, fn, timeout: float = 20.0):
        deadline = time.monotonic() + timeout
        while True:
            value = self.refreshed(fn)
            if value or time.monotonic() > deadline:
                return value
            time.sleep(0.25)

    def screenshot(self, name: str) -> None:
        SCREENS.mkdir(parents=True, exist_ok=True)
        path = SCREENS / f"{name}.png"
        self.gui(lambda: self.window.grab().save(str(path)))
        print(f"    Στιγμιότυπο: {path}", flush=True)

    def row_for(self, room: str, name: str):
        for row in self.page.request_rows.values():
            if row.request.room == room and row.request.name == name and not row.final:
                return row
        return None

    def type_code(self, row, code: str) -> str:
        from PySide6.QtTest import QTest

        def job():
            row.code.clear()
            QTest.keyClicks(row.code, code)
            row.approve.click()
            return row.result.text()
        return self.gui(job)

    def _computer_state(self, client_id: str):
        """Στο νήμα του Qt: τι δείχνει η σελίδα για έναν υπολογιστή (ή None)."""
        row = self.page.computer_rows.get(client_id)
        if row is None:
            return None
        return {"room": row.computer.room, "name": row.computer.name,
                "online": row.computer.online, "state": row.state.text(),
                "totals": self.page.totals.text()}

    def computer(self, client_id: str):
        return self.refreshed(lambda: self._computer_state(client_id))

    def enroll_gui(self, name: str = NAME, profile: str = "gui"):
        reply = self.ask_client("enroll", profile=profile, room=ROOM, name=name)
        row = self.wait(lambda: self.row_for(ROOM, name), timeout=10 if reply.get("sas") else 2)
        return reply, row

    # ------------------------------------------------------------ βήματα
    def steps(self) -> None:
        from director.security import (TEXT_APPROVED, TEXT_DENIED, TEXT_JUST_NOW, TEXT_NEVER,
                                       TEXT_REJECTED, full_text, wrong_code_text)

        def ago_like(state):  # «μόλις τώρα» κάτω από 1 λεπτό, μετά «πριν N …»
            return state == TEXT_JUST_NOW or state.startswith("πριν ")

        self.start_gui()

        # 1. Μενού → Ασφάλεια
        def open_page():
            self.window.nav_buttons["security"].click()
            page = self.page
            return (self.window.current_page, self.window.page_title.text(),
                    page.tab_computers.isChecked(), page.tab_certificates.isEnabled(),
                    hasattr(page, "port_button"))
        state = self.gui(open_page)
        self.result("1. Μενού «Ασφάλεια»: σελίδα, tabs, χωρίς κουμπί θύρας",
                    state == ("security", "Ασφάλεια", True, False, False), f"{state}")

        # 2. Η θύρα εγγραφής είναι ανοιχτή από την εκκίνηση (απόφαση 22/11/2026)
        def port_state():
            return self.runtime.enrollment.is_open, self.page.port_notice.isVisible()
        state = self.gui(port_state)
        self.result("2. Θύρα εγγραφής ανοιχτή από την εκκίνηση, χωρίς προειδοποίηση",
                    state == (True, False), f"{state}")

        # 3. Αίτημα από τα Windows εμφανίζεται· ο κωδικός δεν φαίνεται στον Manager
        reply, row = self.enroll_gui()
        code = reply.get("sas")
        texts = self.gui(self.page.visible_texts) if row else []
        self.result("3. Αίτημα στη λίστα «Νέες εγγραφές» χωρίς τον κωδικό",
                    row is not None and code is not None and reply["rc"] == 0
                    and not any(code in text for text in texts),
                    f"κωδικός Client {code}")
        self.screenshot("1-request")

        # 4. Τρεις λάθος κωδικοί → απόρριψη
        outcomes = []
        expected = [wrong_code_text(2), wrong_code_text(1), TEXT_REJECTED]
        for attempt, text in enumerate(expected, 1):
            outcomes.append(row is not None and self.type_code(row, self.wrong(code, attempt))
                            == text)
        gone = self.gui(lambda: row.final and row.actions.isHidden()) if row else False
        first_id = reply["client_id"]
        self.result("4. 3 λάθος κωδικοί από το πεδίο: μετά απόρριψη, καμία έγκριση",
                    all(outcomes) and gone
                    and self.runtime.trust.registry.approved_entry(first_id) is None,
                    f"{outcomes}")

        # 5. Νέο αίτημα, σωστός κωδικός → Έγκριση
        reply, row = self.enroll_gui()
        board_id, code = reply["client_id"], reply.get("sas")
        result = self.type_code(row, code) if row and code else None
        listed = self.computer(board_id)
        self.result("5. Σωστός κωδικός → «Εγκρίθηκε», εμφανίζεται στους εγγεγραμμένους",
                    result == TEXT_APPROVED and listed is not None
                    and listed["room"] == ROOM and listed["state"] == TEXT_NEVER,
                    f"{listed}")
        self.screenshot("2-approved")

        # 6. Ο υπολογιστής συνδέεται → Online
        self.ask_client("start_client", room=ROOM, new_key=False, profile="gui")
        online = self.wait(lambda: (self._computer_state(board_id) or {}).get("online"), timeout=40)
        listed = self.computer(board_id)
        self.result("6. Η εγκεκριμένη συσκευή συνδέεται: «Online», 1 online",
                    bool(online) and listed["state"] == "Online"
                    and listed["totals"] == "1 υπολογιστής · 1 online", f"{listed}")

        # 7. Μενού ⋯ → Αλλαγή αίθουσας
        self.answers = [OTHER_ROOM]
        self.gui(lambda: self.page.computer_rows[board_id].actions["room"].trigger())
        listed = self.computer(board_id)
        hints = self.ask_client("read_hints", profile="gui", room=OTHER_ROOM)["hints"]
        self.result("7. «Αλλαγή αίθουσας»: νέα ομάδα στη σελίδα και στον Client",
                    listed and listed["room"] == OTHER_ROOM and hints.get("room") == OTHER_ROOM
                    and self.gui(lambda: OTHER_ROOM in self.page.room_headers),
                    f"hints {hints}")

        # 8. Μενού ⋯ → Μετονομασία
        self.answers = [RENAMED]
        self.gui(lambda: self.page.computer_rows[board_id].actions["rename"].trigger())
        listed = self.computer(board_id)
        hints = self.ask_client("read_hints", profile="gui", name=RENAMED)["hints"]
        self.result("8. «Μετονομασία»: νέο όνομα στη σελίδα και στον Client",
                    listed and listed["name"] == RENAMED and hints.get("name") == RENAMED,
                    f"hints {hints}")
        self.screenshot("3-online")

        # 9. Αποσύνδεση → «πριν …» και αποθήκευση στο αρχείο
        self.ask_client("stop_client")
        offline = self.wait(lambda: not (self._computer_state(board_id) or {"online": True})["online"],
                            timeout=20)
        listed = self.computer(board_id)
        seen_file = self.registry.with_name("clients-last-seen.json")

        def saved_times():
            try:
                return json.loads(seen_file.read_text(encoding="utf-8"))["clients"]
            except (OSError, ValueError, KeyError):
                return {}
        deadline = time.monotonic() + 5  # το νήμα καταγραφής ενημερώνει ανά 1 s
        while board_id not in saved_times() and time.monotonic() < deadline:
            time.sleep(0.2)
        time.sleep(1.5)
        saved = saved_times()
        self.result("9. Αποσύνδεση: «μόλις τώρα/πριν …» και τελευταία σύνδεση στο αρχείο",
                    bool(offline) and ago_like(listed["state"])
                    and board_id in saved,
                    f"{listed and listed['state']} · στο αρχείο: {board_id in saved}")
        before = saved.get(board_id)

        # 10. Επανεκκίνηση Director: η τελευταία σύνδεση θυμάται
        self.stop_gui()
        self.start_gui()
        self.gui(lambda: self.window.show_page("security"))
        listed = self.computer(board_id)
        self.result("10. Μετά από επανεκκίνηση φαίνεται ακόμη η τελευταία σύνδεση",
                    listed is not None and ago_like(listed["state"])
                    and before is not None
                    and abs((self.runtime.last_seen.get(board_id) or 0) - before) < 2,
                    f"{listed and listed['state']}")
        self.screenshot("4-after-restart")

        # 11. Απόρριψη αιτήματος με το κουμπί
        reply, row = self.enroll_gui(name="PC", profile="gui2")

        def deny():
            row.deny.click()
            return row.result.text()
        text = self.gui(deny) if row else None
        pending = self.gui(self.page.model.requests)
        self.result("11. Κουμπί «Απόρριψη»: το αίτημα φεύγει, καμία έγκριση",
                    text == TEXT_DENIED and not pending
                    and self.runtime.trust.registry.approved_entry(reply["client_id"]) is None)

        # 12. Ανάκληση από το μενού ⋯ με τον υπολογιστή συνδεδεμένο
        self.ask_client("start_client", room=ROOM, new_key=False, profile="gui")
        online = self.wait(lambda: (self._computer_state(board_id) or {}).get("online"), timeout=40)
        self.gui(lambda: self.page.computer_rows[board_id].actions["revoke"].trigger())
        removed = self.computer(board_id) is None

        def is_online():
            return any(cid == board_id and active
                       for cid, _r, active, _a in self.runtime.broadcaster.status_snapshot())
        dropped = self.wait(lambda: not is_online(), timeout=10)
        time.sleep(15)  # ο Client ξαναδοκιμάζει· πρέπει να απορρίπτεται
        stays_out = not self.gui(is_online)
        self.result("12. «Ανάκληση»: φεύγει από τη λίστα, αποσυνδέεται, δεν ξαναμπαίνει",
                    bool(online) and removed and bool(dropped) and stays_out
                    and self.runtime.trust.registry.approved_entry(board_id) is None)
        self.ask_client("stop_client")

        # 13. Όριο αιτημάτων (50 στην κανονική χρήση, 1 εδώ για συντομία)
        self.gui(lambda: setattr(self.runtime.enrollment, "_max_pending", 1))
        _first, first_row = self.enroll_gui(name="Late1", profile="late1")
        full, full_row = self.enroll_gui(name="Late2", profile="late2")

        def notice():
            self.page.refresh()
            count = self.page.request_count
            return count.text(), count.property("warn") == "true"
        shown = self.gui(notice)
        self.screenshot("5-limit")
        text = self.gui(lambda: (first_row.deny.click(), first_row.result.text())[1]) \
            if first_row else None
        _again, again_row = self.enroll_gui(name="Late2", profile="late2")
        self.result("13. Όριο αιτημάτων: το επιπλέον απορρίπτεται με μήνυμα, μετά χωρά νέο",
                    first_row is not None and full.get("sas") is None and full["rc"] != 0
                    and full.get("full") and full_row is None
                    and shown == (full_text(1), True) and text == TEXT_DENIED
                    and again_row is not None, f"{shown}")
        self.screenshot("5-end")

    def run_steps(self) -> None:
        """Νήμα δοκιμής· το Qt τρέχει στο κύριο νήμα."""
        try:
            self.wait_client()
            self.steps()
        except Exception as exc:  # η δοκιμή αναφέρει, δεν κρύβει
            self.result("Απρόσμενο σφάλμα δοκιμής", False, f"{type(exc).__name__}: {exc}")
        finally:
            try:
                self.stop_gui()
            except Exception as exc:
                print(f"Σφάλμα κλεισίματος: {exc}", flush=True)
        passed = sum(ok for _, ok, _ in self.results)
        summary = f"ΣΥΝΟΛΟ: {passed}/{len(self.results)} PASS"
        print("\n" + "\n".join(f"{'PASS' if ok else 'FAIL'}  {name}"
                               for name, ok, _ in self.results), flush=True)
        print(summary, flush=True)
        try:
            self.channel.send(cmd="done", text=summary)
        except (OSError, AttributeError):
            pass
        self.exit_code = 0 if self.results and passed == len(self.results) else 1
        self.gui(self.app.quit)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("role", choices=("director", "client"))
    parser.add_argument("--director-host", default=None,
                        help="IP του Director, αν δεν βρεθεί αυτόματα (μόνο client)")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="sch-security-gui-test-") as directory:
        work = Path(directory)
        print(f"Φάκελος δοκιμής (διαγράφεται στο τέλος): {work}", flush=True)
        if args.role == "client":
            return ClientRun(work, args.director_host).run()
        try:
            from director.qt_gui import create_application
        except ImportError as exc:
            print(f"Χρειάζεται PySide6: pip install -e .[gui] ({exc})", flush=True)
            return 2
        app = create_application()
        # Η επανεκκίνηση Director (βήμα 10) κλείνει προσωρινά το μοναδικό παράθυρο.
        app.setQuitOnLastWindowClosed(False)
        run = SecurityGuiRun(work, Gui(), app)
        threading.Thread(target=run.run_steps, daemon=True, name="security-gui-test").start()
        app.exec()
        return run.exit_code


if __name__ == "__main__":
    sys.exit(main())
