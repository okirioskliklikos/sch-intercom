"""4.2D3: 15 πραγματικοί έλεγχοι «ο Client θυμάται την έκδοση μητρώου», μόνο loopback.

Δύο προσωρινοί Managers (A, B) και ένας Client με πραγματικό mTLS και ήχο.
Σενάριο: ο A προχωρά το μητρώο, «χάνεται», ο B επαναφέρεται από παλιό backup και ο
Client αποκαλύπτει το παλιό μητρώο. Μετά «Συνέχεια με αυτό το μητρώο», απόκλιση
ίδιας έκδοσης και συγχώνευση όταν το χαμένο μητρώο βρεθεί. Θύρες TCP/UDP 516xx.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.client_registry import digest  # noqa: E402
from tools.manager_role_live_test import (TestDevice, TestMachine,  # noqa: E402
                                          check_port)
from tools.mtls_live_test import JOINED  # noqa: E402

TOTAL = 15
REPORT = "[Manager] Clients αναφέρουν"


def registry(machine: TestMachine) -> dict:
    return json.loads(machine.registry.read_text(encoding="utf-8"))["registry"]


def version(machine: TestMachine) -> int:
    return registry(machine)["registry_version"]


class LiveTest:
    def __init__(self, root: Path):
        self.root = root
        self.a = TestMachine(root, "pc-a", 51605, 51606, 51609, 51607, 51617)
        self.b = TestMachine(root, "pc-b", 51615, 51616, 51619, 51617, 51607)
        self.one = TestDevice(root, "Board1")
        self.backup_file = root / "old.sicb"
        self.lost = root / "lost-a.sicr"
        self.divergent = root / "divergent-a.sicr"
        self.old_version = 0      # έκδοση του backup
        self.lost_version = 0     # τελευταία έκδοση του A πριν «χαθεί»
        self.accepted = 0         # έκδοση μετά τη «Συνέχεια με αυτό το μητρώο»
        self.results = []

    # ---------------------------------------------------------------- βοηθητικά
    def step(self, title, function):
        try:
            answer = function()
            ok, detail = answer if isinstance(answer, tuple) else (bool(answer), "")
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        self.results.append((title, ok, detail))
        print(f"\n{'PASS' if ok else 'FAIL'} {len(self.results):02d}/{TOTAL}: "
              f"{title}" + (f" — {detail}" if detail else ""), flush=True)

    def seen_file(self) -> dict:
        """Το registry-seen.json του Client: {"rv", "hash"} του μοναδικού Manager."""
        path = self.one.dir / "registry-seen.json"
        if not path.is_file():
            return {}
        managers = json.loads(path.read_text(encoding="utf-8")).get("managers", {})
        return next(iter(managers.values()), {}) if len(managers) == 1 else {}

    def connect(self, machine: TestMachine, *, audio: bool = False) -> tuple[bool, str]:
        """Νέα σύνδεση του Client (επανεκκίνηση) στον machine· προαιρετικά ήχος."""
        director = machine.director
        marker = director.mark()
        self.one.use(machine)
        client = self.one.start(machine.cert)
        joined = director.wait_for(self.one.client_id, JOINED, since=marker, timeout=40)
        if not audio:
            return joined is not None, f"σύνδεση {joined is not None}"
        marker = client.mark()
        director.send("all")
        line = client.wait_for("κατάσταση: ανακοίνωση | blocks", since=marker, timeout=25)
        director.send("none")
        return joined is not None and line is not None, (
            f"σύνδεση {joined is not None}, ήχος {line is not None}")

    def status_has(self, machine: TestMachine, text: str) -> bool:
        marker = machine.director.mark()
        machine.director.send("status")
        if machine.director.wait_for("Clients online:", since=marker, timeout=10) is None:
            return False
        return machine.director.seen(text, since=marker)

    # ------------------------------------------------------------------- βήματα
    def s01(self):
        self.a.create_identity()
        proc = self.a.start()
        marker = proc.mark()
        code = self.one.use(self.a).enroll(self.a.cert)
        line = proc.wait_for("Νέο αίτημα", "| Board1", since=marker, timeout=10)
        short = line.split("Νέο αίτημα")[1].split("|")[0].strip() if line else ""
        approved = bool(code) and self.a.command(f"approve {short} {code}", "Εγκρίθηκε:")
        connected, detail = self.connect(self.a, audio=True)
        stored = self.seen_file()
        expected = {"rv": version(self.a), "hash": digest(registry(self.a))}
        return approved and connected and stored == expected, (
            f"{detail}, ο Client θυμάται v{stored.get('rv')}")

    def s02(self):
        self.old_version = version(self.a)
        rc, _ = self.a.backup("create", "--out", str(self.backup_file), "--media-copy", "no")
        return rc == 0 and self.backup_file.is_file(), f"backup v{self.old_version}, rc={rc}"

    def s03(self):
        changed = all(self.a.command(f"set-name {self.one.client_id} {name}",
                                     "Ενημερώθηκε όνομα")
                      for name in ("Board1-a", "Board1-b", "Board1"))
        connected, _ = self.connect(self.a)
        client_line = self.one.process.wait_for("[Client] Μητρώο Manager v", timeout=10)
        self.lost_version = version(self.a)
        stored = self.seen_file()
        return (changed and connected and client_line is not None and
                stored.get("rv") == self.lost_version > self.old_version), (
            f"A v{self.lost_version}, ο Client θυμάται v{stored.get('rv')}")

    def s04(self):
        exported = self.a.command(f"registry export {self.lost}", "Εξαγωγή:")
        self.one.stop()
        self.a.stop()
        return exported and self.lost.is_file() and not check_port(self.a.port), (
            "ο A «χάνεται» (κλείνει) με την εξαγωγή του")

    def s05(self):
        rc, _ = self.b.backup("restore", str(self.backup_file))
        proc = self.b.start()
        serving = self.b.command("serve on", "Εξυπηρέτηση: ενεργή")
        return (rc == 0 and proc.seen("[Manager] Ρόλος: Εφεδρικός") and serving and
                version(self.b) == self.old_version), f"B v{version(self.b)} (Εφεδρικός)"

    def s06(self):
        marker = self.b.director.mark()
        connected, detail = self.connect(self.b, audio=True)
        manager_log = self.b.director.wait_for(
            "έχει δει νεότερο μητρώο", f"v{self.lost_version}", since=marker, timeout=10)
        client_log = self.one.process.wait_for(f"έχω δει v{self.lost_version}", timeout=10)
        return connected and manager_log is not None and client_log is not None, (
            f"{detail}, Manager log {manager_log is not None}, Client log {client_log is not None}")

    def s07(self):
        line = (f"{REPORT} νεότερο μητρώο: v{self.lost_version}, "
                f"εδώ v{self.old_version} (C2627-Γ1 · Board1)")
        return self.status_has(self.b, line), line

    def s08(self):
        stored = self.seen_file()
        return stored.get("rv") == self.lost_version, (
            f"ο Client κρατά v{stored.get('rv')} (δεν κατέβηκε σε v{self.old_version})")

    def s09(self):
        before = self.b.registry.read_bytes()
        refused = self.b.command("registry accept-current confirm", "Εφεδρικός")
        return refused and self.b.registry.read_bytes() == before, "Εφεδρικός: άρνηση"

    def s10(self):
        promoted = self.b.command("role primary confirm", "Ρόλος: Κύριος")
        before = self.b.registry.read_bytes()
        warned = self.b.command("registry accept-current", "θα θεωρηθεί χαμένο")
        return promoted and warned and self.b.registry.read_bytes() == before, (
            "προειδοποίηση χωρίς αλλαγή")

    def s11(self):
        expected = self.lost_version + 1
        accepted = self.b.command("registry accept-current confirm",
                                  f"νέα έκδοση v{expected}")
        self.accepted = version(self.b)
        silent = not self.status_has(self.b, REPORT)
        return accepted and self.accepted == expected and silent, (
            f"B v{self.accepted}, ειδοποίηση σβηστή {silent}")

    def s12(self):
        marker = self.b.director.mark()
        connected, _ = self.connect(self.b)
        time.sleep(1)
        alarm = self.b.director.seen("έχει δει", since=marker)
        stored = self.seen_file()
        silent = not self.status_has(self.b, REPORT)
        return (connected and not alarm and silent and stored.get("rv") == self.accepted and
                stored.get("hash") == digest(registry(self.b))), (
            f"ο Client θυμάται v{stored.get('rv')}, χωρίς νέα ειδοποίηση {silent}")

    def s13(self):
        self.one.stop()
        off = self.b.command("serve off", "Εξυπηρέτηση: σταματημένη")
        time.sleep(6)   # ο A δεν πρέπει να δει το beacon του B
        self.a.start()
        on = self.a.command("serve on", "Εξυπηρέτηση: ενεργή")
        changed = self.a.command(f"set-name {self.one.client_id} Board1-A", "Ενημερώθηκε όνομα")
        same = version(self.a) == self.accepted
        connected, _ = self.connect(self.a)
        line = (f"{REPORT} διαφορετικό μητρώο: v{self.accepted}, εδώ v{self.accepted}")
        divergent = self.status_has(self.a, line)
        return off and on and changed and same and connected and divergent, (
            f"A v{version(self.a)}, B v{self.accepted}, απόκλιση {divergent}")

    def s14(self):
        exported = self.a.command(f"registry export {self.divergent}", "Εξαγωγή:")
        denied = self.b.command(f"registry import {self.lost}", "χρειάζεται --merge")
        merged = self.b.command(f"registry import {self.lost} merge confirm",
                                "[Manager] Εισαγωγή: conflict")
        merged2 = self.b.command(f"registry import {self.divergent} merge confirm",
                                 "[Manager] Εισαγωγή: conflict")
        return exported and denied and merged and merged2 and version(self.b) > self.accepted, (
            f"συγχώνευση χαμένου και αποκλίνοντος: B v{version(self.b)}")

    def s15(self):
        self.one.stop()
        off = self.a.command("serve off", "Εξυπηρέτηση: σταματημένη")
        time.sleep(6)
        on = self.b.command("serve on", "Εξυπηρέτηση: ενεργή")
        connected, detail = self.connect(self.b, audio=True)
        time.sleep(1)
        stored = self.seen_file()
        silent = not self.status_has(self.b, REPORT)
        return (off and on and connected and silent and stored.get("rv") == version(self.b)), (
            f"{detail}, ο Client θυμάται v{stored.get('rv')}, χωρίς ειδοποίηση {silent}")

    def close(self):
        self.one.stop()
        for machine in (self.a, self.b):
            machine.stop()

    def run(self) -> int:
        actions = [
            ("Εγγραφή Client στον A, ήχος, ο Client θυμάται την έκδοση", self.s01),
            ("Backup του A (παλιό μητρώο)", self.s02),
            ("Αλλαγές στον A, ο Client θυμάται τη νεότερη έκδοση", self.s03),
            ("Εξαγωγή του A και ο A «χάνεται»", self.s04),
            ("Restore του παλιού backup στον B, εξυπηρέτηση", self.s05),
            ("Ο Client συνδέεται στον B, ήχος, αναφέρει νεότερο μητρώο", self.s06),
            ("status του B: «Clients αναφέρουν νεότερο μητρώο»", self.s07),
            ("Ο Client δεν «ξεχνά» τη μεγαλύτερη έκδοση", self.s08),
            ("Εφεδρικός: «Συνέχεια με αυτό το μητρώο» αρνείται", self.s09),
            ("Κύριος: προειδοποίηση χωρίς confirm, καμία αλλαγή", self.s10),
            ("Συνέχεια με αυτό το μητρώο: νέα έκδοση, ειδοποίηση σβήνει", self.s11),
            ("Νέα σύνδεση Client: θυμάται τη νέα έκδοση, χωρίς ειδοποίηση", self.s12),
            ("Ίδια έκδοση με άλλο hash στον A: «διαφορετικό μητρώο»", self.s13),
            ("Το χαμένο μητρώο βρέθηκε: σύγκρουση και συγχώνευση στον B", self.s14),
            ("Μετά τη συγχώνευση: ήχος, ο Client θυμάται, χωρίς ειδοποίηση", self.s15),
        ]
        try:
            for name, action in actions:
                self.step(name, action)
        finally:
            self.close()
        successes = sum(ok for _, ok, _ in self.results)
        print("\n" + "\n".join(
            f"{'PASS' if ok else 'FAIL'} {i:02d}: {name}"
            for i, (name, ok, _) in enumerate(self.results, 1)), flush=True)
        print(f"ΣΥΝΟΛΟ: {successes}/{TOTAL} PASS", flush=True)
        return 0 if successes == TOTAL else 1


def main():
    root = Path(tempfile.mkdtemp(prefix="sch-seen-test-"))
    print(f"Προσωρινή δοκιμή μόνο σε loopback: {root}", flush=True)
    try:
        return LiveTest(root).run()
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
