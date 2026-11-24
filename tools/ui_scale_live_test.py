"""Αυτόματη πραγματική δοκιμή: Ρυθμίσεις → «Εμφάνιση» (κλίμακα περιβάλλοντος).

Τρέχει στον υπολογιστή του Manager (κύριο σενάριο: Windows). Ανοίγει το πραγματικό
παράθυρο Director (PySide6) με πραγματικό Manager (TLS, μητρώο, θύρα εγγραφής,
manager.lock) και πραγματικό ``client.main`` ως διεργασία, και ελέγχει:

- την προεπιλογή (90% στα Windows, 100% αλλού) με τη γραμματοσειρά του συστήματος,
- την επιλογή από το GUI, την αποθήκευση στο ``director-gui.json`` και το
  «Ισχύει μετά από επανεκκίνηση» / «Επανεκκίνηση τώρα»,
- ότι η επανεκκίνηση δεν γίνεται όσο υπάρχει ζωντανή ανακοίνωση,
- ότι μετά την επανεκκίνηση γράμματα, αποστάσεις και εικονίδια άλλαξαν με το ίδιο
  ποσοστό και ο Client ξανασυνδέεται χωρίς νέα εγγραφή.

Η «επανεκκίνηση» γίνεται όπως στο ``run_qt_gui``: το παράθυρο ζητά επανεκκίνηση, ο
Manager σταματά όλα (Clients, sockets, κλείδωμα) και ξεκινά ξανά με
``create_application(ui_scale=…)`` από το αρχείο ρυθμίσεων. Εδώ αυτό γίνεται στην ίδια
διεργασία (ένα QApplication)· το ξεκίνημα νέας διεργασίας ``director.main`` το ελέγχει
ήδη η ``maintenance_gui_live_test`` (επανεκκίνηση μετά από επαναφορά).

Μόνο προσωρινός φάκελος (``%TEMP%\\sch-scale-test-*``), loopback 127.0.0.1 και θύρες
51555/51556/51559: η πραγματική εγκατάσταση (``%USERPROFILE%\\.school-intercom``) δεν
αγγίζεται. Στιγμιότυπα στο ``~/sch-intercom-test-screens/klimaka-*.png``.

    Windows:  powershell -ExecutionPolicy Bypass -File tools\\ui_scale_live_test.ps1
    Linux:    tools/ui_scale_live_test.sh
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.backup_live_test import Device, Machine  # noqa: E402
from tools.maintenance_gui_live_test import SCREENS, GuiManager  # noqa: E402

TOTAL = 12


class LiveTest:
    def __init__(self, base: Path, app):
        from shared.ui import theme
        self.base = base
        self.app = app
        self.results: list[tuple[str, bool, str]] = []
        self.pc = Machine(base, "pc")
        self.a = Device(base, "Board1")
        self.gui: GuiManager | None = None
        self.settings_file = self.pc.registry.with_name("director-gui.json")
        self.default = theme.default_ui_scale()
        self.system_pt: float | None = None
        self.sizes: dict[int, dict[str, float]] = {}

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

    # ------------------------------------------------------------ βοηθητικά
    def start_manager(self) -> int:
        """Όπως το run_qt_gui: κλίμακα από το director-gui.json, μετά το παράθυρο."""
        from director.qt_gui import create_application, load_ui_scale
        scale = load_ui_scale(self.settings_file)
        create_application(ui_scale=scale)
        self.gui = GuiManager(self.pc, self.app)
        return scale

    def restart_manager(self) -> int:
        self.gui.close()
        self.gui = None
        return self.start_manager()

    def measure(self) -> dict[str, float]:
        """Πραγματικά μεγέθη στο παράθυρο: γράμματα, στοιχείο μενού, εικονίδιο κουμπιού."""
        from director.qt_gui import current_metrics
        window = self.gui.window
        page = window.settings_page
        return {"base_pt": current_metrics().base_pt,
                "font_pt": self.app.font().pointSizeF(),
                "title_pt": window.page_title.font().pointSizeF(),
                "nav_h": window.nav_buttons["home"].sizeHint().height(),
                "icon": page.restart_button.iconSize().width(),
                "card_w": page.scale_card.width() or page.scale_card.maximumWidth(),
                "gap": page.layout().spacing()}

    def open_appearance(self):
        window = self.gui.window
        window.nav_buttons["settings"].click()
        window.settings_page.tab_appearance.click()
        self.gui.pump()
        return window.settings_page

    def saved(self) -> int | None:
        try:
            return json.loads(self.settings_file.read_text(encoding="utf-8")).get("ui_scale")
        except (OSError, ValueError):
            return None

    def screenshot(self, name: str) -> None:
        self.gui.screenshot(f"klimaka-{name}")

    def ratio_ok(self, low: int, high: int) -> tuple[bool, str]:
        """Όλα τα μεγέθη αλλάζουν με το ίδιο ποσοστό (±1px στρογγυλοποίηση)."""
        a, b = self.sizes[low], self.sizes[high]
        expected = low / high
        bad = []
        for key in ("base_pt", "font_pt", "title_pt"):
            if abs(a[key] / b[key] - expected) > 0.01:
                bad.append(f"{key} {a[key]:.2f}/{b[key]:.2f}")
        for key in ("icon", "card_w", "gap"):
            if abs(a[key] - b[key] * expected) > 1.5:
                bad.append(f"{key} {a[key]}/{b[key]}")
        # Ύψος στοιχείου μενού: περιθώρια + ύψος γραμμής γραμματοσειράς (όχι ακριβώς
        # αναλογικό λόγω στρογγυλοποίησης της γραμματοσειράς)· μόνο φορά και ±3px.
        if not (a["nav_h"] < b["nav_h"] and abs(a["nav_h"] - b["nav_h"] * expected) <= 3):
            bad.append(f"nav_h {a['nav_h']}/{b['nav_h']}")
        detail = ", ".join(f"{k} {a[k]:g}→{b[k]:g}" for k in a)
        return not bad, (f"λάθος: {bad}" if bad else f"{low}%→{high}%: {detail}")

    # ------------------------------------------------------------------ βήματα
    def s01_default(self):
        from director.qt_gui import load_ui_scale, system_font_pt
        self.system_pt = system_font_pt(self.app)
        default = load_ui_scale(self.settings_file)
        windows = sys.platform.startswith("win")
        ok = default == (90 if windows else 100) and not self.settings_file.exists()
        return ok, (f"γραμματοσειρά συστήματος {self.system_pt}pt, προεπιλογή {default}% "
                    f"({'Windows' if windows else sys.platform})")

    def s02_start_default(self):
        from director.qt_gui import current_ui_scale
        from shared.ui import theme
        if not self.pc.create_identity():
            return False, "η ταυτότητα TLS δεν δημιουργήθηκε"
        scale = self.start_manager()
        self.open_appearance()
        self.sizes[scale] = self.measure()
        expected = theme.metrics_for(self.system_pt, self.default).base_pt
        base = self.sizes[scale]["base_pt"]
        self.gui.window.nav_buttons["home"].click()
        self.gui.pump()
        self.screenshot(f"kentriki-{scale}")
        return (scale == self.default and current_ui_scale() == scale
                and abs(base - expected) < 0.01), f"κλίμακα {scale}%, βάση {base:.2f}pt"

    def s03_enroll(self):
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

    def s04_appearance_tab(self):
        page = self.open_appearance()
        checked = [v for v, b in page.scale_buttons.items() if b.isChecked()]
        self.screenshot(f"emfanisi-{self.default}")
        ok = (page.current_tab == "Εμφάνιση" and checked == [self.default]
              and not page.scale_note_row.isVisible()
              and page.tab_appearance.x() < page.tab_maintenance.x())
        return ok, f"tab «{page.current_tab}», επιλεγμένο {checked}, χωρίς σημείωση"

    def s05_choose_80(self):
        page = self.open_appearance()
        page.scale_buttons[80].click()
        self.gui.pump()
        self.screenshot("emfanisi-allagi")
        ok = (self.saved() == 80 and page.scale_note.isVisible()
              and page.restart_button.isVisible() and page.restart_button.isEnabled())
        return ok, (f"αρχείο ui_scale={self.saved()}, «{page.scale_note.text()}», "
                    f"κουμπί «{page.restart_button.text()}»")

    def s06_blocked_while_live(self):
        window = self.gui.window
        page = window.settings_page
        window.nav_buttons["home"].click()
        window.toggle_announcement()   # όπως το κουμπί ανακοίνωσης
        live = self.gui.wait(lambda: window.mic.live, timeout=10)
        window.nav_buttons["settings"].click()
        self.gui.pump(1.2)   # τουλάχιστον ένα refresh του παραθύρου
        disabled = not page.restart_button.isEnabled()
        page.restart_button.click()
        window.restart_for_scale()
        self.gui.pump()
        blocked = not self.gui.runtime.restart_requested
        window.toggle_announcement()
        self.gui.wait(lambda: not window.mic.live, timeout=10)
        enabled = self.gui.wait(lambda: page.restart_button.isEnabled(), timeout=5)
        return (live and disabled and blocked and enabled,
                f"ζωντανή {live}, κουμπί ανενεργό {disabled}, χωρίς επανεκκίνηση {blocked}, "
                f"ενεργό ξανά μετά {enabled}")

    def s07_restart_now(self):
        page = self.open_appearance()
        page.restart_button.click()
        self.gui.pump()
        return self.gui.runtime.restart_requested, \
            f"ζητήθηκε επανεκκίνηση {self.gui.runtime.restart_requested}"

    def s08_restarted_at_80(self):
        from director.qt_gui import current_ui_scale
        scale = self.restart_manager()
        self.open_appearance()
        self.sizes[scale] = self.measure()
        self.gui.window.nav_buttons["home"].click()
        self.gui.pump()
        self.screenshot("kentriki-80")
        ok, detail = self.ratio_ok(80, self.default)
        return ok and scale == 80 and current_ui_scale() == 80, detail

    def s09_client_reconnects(self):
        online = self.gui.wait(lambda: self.gui.online(self.a.client_id), timeout=40)
        return online, f"Client online χωρίς νέα εγγραφή: {online}"

    def s10_settings_after(self):
        page = self.open_appearance()
        checked = [v for v, b in page.scale_buttons.items() if b.isChecked()]
        return checked == [80] and not page.scale_note_row.isVisible(), \
            f"επιλεγμένο {checked}, χωρίς σημείωση {not page.scale_note_row.isVisible()}"

    def s11_largest_fits(self):
        page = self.open_appearance()
        page.scale_buttons[125].click()
        page.restart_button.click()
        self.gui.pump()
        scale = self.restart_manager()
        self.open_appearance()
        self.sizes[scale] = self.measure()
        window = self.gui.window
        screen = window.screen().availableGeometry()
        fits = (window.minimumWidth() <= screen.width()
                and window.minimumHeight() <= screen.height())
        self.screenshot("emfanisi-125")
        ok, detail = self.ratio_ok(self.default, 125)
        return ok and fits and scale == 125, \
            f"{detail}; ελάχιστο παράθυρο {window.minimumWidth()}×{window.minimumHeight()} " \
            f"σε οθόνη {screen.width()}×{screen.height()}"

    def s12_back_to_default(self):
        from director.qt_gui import current_ui_scale
        page = self.open_appearance()
        page.scale_buttons[self.default].click()
        page.restart_button.click()
        self.gui.pump()
        scale = self.restart_manager()
        sizes = self.measure()
        online = self.gui.wait(lambda: self.gui.online(self.a.client_id), timeout=40)
        same = abs(sizes["base_pt"] - self.sizes[self.default]["base_pt"]) < 0.01
        self.gui.close()
        self.gui = None
        return (scale == self.default and current_ui_scale() == scale and same and online,
                f"ξανά {scale}%, βάση {sizes['base_pt']:.2f}pt, Client online {online}")

    def run(self) -> int:
        steps = [
            ("Προεπιλογή κλίμακας για αυτό το σύστημα", self.s01_default),
            ("Manager με GUI στην προεπιλεγμένη κλίμακα", self.s02_start_default),
            ("Εγγραφή και σύνδεση Client A", self.s03_enroll),
            ("Ρυθμίσεις → «Εμφάνιση» πρώτο tab, τρέχουσα επιλογή, χωρίς σημείωση",
             self.s04_appearance_tab),
            ("Επιλογή 80%: αποθήκευση, «Ισχύει μετά από επανεκκίνηση»", self.s05_choose_80),
            ("Όχι επανεκκίνηση σε ζωντανή ανακοίνωση", self.s06_blocked_while_live),
            ("«Επανεκκίνηση τώρα» ζητά επανεκκίνηση", self.s07_restart_now),
            ("Μετά την επανεκκίνηση: όλα στο 80%", self.s08_restarted_at_80),
            ("Ο Client ξανασυνδέεται χωρίς νέα εγγραφή", self.s09_client_reconnects),
            ("Εμφάνιση μετά: 80% επιλεγμένο, χωρίς σημείωση", self.s10_settings_after),
            ("125%: μεγαλύτερα μεγέθη και το παράθυρο χωρά στην οθόνη", self.s11_largest_fits),
            ("Επιστροφή στην προεπιλογή", self.s12_back_to_default),
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
        print(f"Στιγμιότυπα: {SCREENS}", flush=True)
        print(f"ΣΥΝΟΛΟ: {passed}/{TOTAL} PASS", flush=True)
        return 0 if passed == TOTAL else 1


def main() -> int:
    from director.qt_gui import create_application
    app = create_application()
    base = Path(tempfile.mkdtemp(prefix="sch-scale-test-"))
    print(f"Φάκελος δοκιμής (σβήνεται στο τέλος): {base}", flush=True)
    try:
        return LiveTest(base, app).run()
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
