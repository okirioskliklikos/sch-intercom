"""5C2: πραγματική δοκιμή «Αλλαγή…» αίθουσας/ονόματος, 16 βήματα PASS/FAIL, μόνο loopback.

Ένας προσωρινός Manager (δοκιμαστικός τόνος) και ένας Client με tray (offscreen). Τα
αιτήματα γίνονται με ``client.main --request-change``, που περνά από τον ίδιο δρόμο με το
κουμπί «Αλλαγή…» του παραθύρου (ανάλογα με την κατάσταση: αίτημα στον Director χωρίς
κωδικό, διόρθωση αιτήματος εγγραφής με τον ίδιο κωδικό, ή νέα αίτηση εγγραφής μετά από
ανάκληση). Ό,τι δείχνει ο Client διαβάζεται από το κρυφό ``--status-dump``.

    python tools/change_request_live_test.py [--visible] [--keep]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client import status as st  # noqa: E402
from client.changes import INBOX_FILE  # noqa: E402
from shared import change_request as cr  # noqa: E402
from shared.room_names import room_title, short_room  # noqa: E402
from tools.client_tray_live_test import ToneMachine  # noqa: E402
from tools.manager_role_live_test import ROOM, TestDevice  # noqa: E402
from tools.mtls_live_test import JOINED, ROOT, Process, child_env  # noqa: E402
from shared.ui.offscreen import windows_fonts_dir  # noqa: E402

TOTAL = 16
NAME = "Board1"
ROOM2, NAME2 = "C2627-Γ2", "Board9"      # διόρθωση πριν από την πρώτη έγκριση
ROOM3 = "C2627-Γ3"                       # αίτημα που απορρίπτεται
ROOM4 = "C2627-Γ4"                       # αίτημα εκτός σύνδεσης, έγκριση με κλειστό Client
ROOM5, NAME5 = "C2627-Γ5", "Board5"      # νέα αίτηση εγγραφής μετά από ανάκληση


class LiveTest:
    def __init__(self, root: Path, visible: bool):
        self.root = root
        self.visible = visible
        self.m = ToneMachine(root, "manager", 51755, 51756, 51759, 51757, 51767)
        self.device = TestDevice(root, NAME).use(self.m)
        self.dump_file = root / "status-dump.json"
        self.client: Process | None = None
        self.code: str | None = None
        self.results: list[tuple[str, bool, str]] = []

    # -------------------------------------------------------------- βοηθητικά
    def step(self, title, function) -> None:
        try:
            answer = function()
            ok, detail = answer if isinstance(answer, tuple) else (bool(answer), "")
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        self.results.append((title, ok, detail))
        print(f"\n{'PASS' if ok else 'FAIL'} {len(self.results):02d}/{TOTAL}: {title}"
              + (f" — {detail}" if detail else ""), flush=True)

    def env(self) -> dict:
        env = child_env()
        env.pop(st.NO_TRAY_ENV, None)
        if not self.visible:
            env["QT_QPA_PLATFORM"] = "offscreen"
            fonts = windows_fonts_dir()
            if fonts:
                env.setdefault("QT_QPA_FONTDIR", fonts)
        return env

    def start_client(self) -> Process:
        self.stop_client()
        self.dump_file.unlink(missing_ok=True)
        self.client = Process(["-m", "client.main", *self.device.args(self.m.cert),
                               "--status-dump", str(self.dump_file)],
                              label=f"Client {NAME}", env=self.env())
        self.client.wait_for("Client ID:", timeout=30)
        return self.client

    def stop_client(self) -> None:
        if self.client is not None:
            self.client.stop()
            self.client = None

    def request_change(self, room: str, name: str) -> tuple[int, str]:
        done = subprocess.run([sys.executable, "-m", "client.main",
                               *self.device.args(self.m.cert), "--request-change",
                               "--room", room, "--computer-name", name],
                              cwd=ROOT, env=child_env(), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
        return done.returncode, (done.stdout + done.stderr).strip()

    def dump(self) -> dict | None:
        try:
            return json.loads(self.dump_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def wait_dump(self, check, timeout: float = 30.0) -> dict | None:
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            data = self.dump()
            if data is not None:
                last = data
                try:
                    if check(data):
                        return data
                except (KeyError, TypeError, IndexError):
                    pass
            time.sleep(0.2)
        print(f"    τελευταίο dump: {json.dumps(last, ensure_ascii=False)[:700]}", flush=True)
        return None

    def rows(self, data: dict) -> dict:
        return {row["label"]: row["value"] for row in data["window"]["info_rows"]}

    def client_id(self) -> str:
        return json.loads((self.device.dir / "client.json").read_text(
            encoding="utf-8"))["client_id"]

    def local_change(self) -> dict | None:
        return cr.load_local(self.device.dir / cr.CLIENT_FILE, self.client_id())

    def approve_enrollment(self, room: str, name: str, code: str, mark: int) -> bool:
        director = self.m.director
        line = director.wait_for("Νέο αίτημα", f"| {room} | {name}", since=mark, timeout=15)
        if line is None:
            return False
        short = line.split("Νέο αίτημα")[1].split("|")[0].strip()
        return self.m.command(f"approve {short} {code}", "Εγκρίθηκε:")

    # -------------------------------------------------------------- βήματα
    def s01(self):
        ok = self.m.create_identity()
        self.m.start()
        return ok, "Manager σε 127.0.0.1:51755 με δοκιμαστικό τόνο (πρωτόκολλο v8)"

    def s02(self):
        self.mark = self.m.director.mark()
        self.code = self.device.enroll(self.m.cert)
        saved = st.load_pending(self.device.dir / st.PENDING_FILE, self.client_id())
        ok = bool(self.code) and saved is not None and bool(saved["request_id"])
        return ok, f"κωδικός {self.code}, request_id στο {st.PENDING_FILE}: {bool(saved)}"

    def s03(self):
        self.start_client()
        data = self.wait_dump(lambda d: d["state"] == st.PENDING)
        ok = data is not None and data["window"]["code"] == st.format_code(self.code)
        return ok, "Client σε αναμονή έγκρισης με τον κωδικό"

    def s04(self):
        mark = self.m.director.mark()
        code, output = self.request_change(ROOM2, NAME2)
        amended = self.m.director.wait_for("Διόρθωση αιτήματος", f"| {ROOM2} | {NAME2}",
                                           since=mark, timeout=20) is not None
        data = self.wait_dump(lambda d: d["window"]["title"] == room_title(ROOM2, NAME2))
        same = st.load_pending(self.device.dir / st.PENDING_FILE, self.client_id())
        ok = (code == 0 and amended and data is not None
              and data["window"]["code"] == st.format_code(self.code)
              and same is not None and same["code"] == self.code and same["seq"] == 1)
        return ok, (f"Director: διόρθωση {amended}, Client: {data and data['window']['title']}, "
                    f"ίδιος κωδικός {data and data['window']['code']}")

    def s05(self):
        # Η γραμμή «Νέο αίτημα» του Director έχει τις αρχικές τιμές· εγκρίνεται με τις νέες.
        approved = self.approve_enrollment(ROOM, NAME, self.code, self.mark)
        data = self.wait_dump(lambda d: d["state"] == st.CONNECTED, timeout=40)
        ok = (approved and data is not None
              and data["window"]["window_title"] == "School Intercom — " + room_title(ROOM2, NAME2)
              and data["window"]["brand_sub"] == "Client")
        return ok, (f"έγκριση με τον ίδιο κωδικό: {approved}, τίτλος παραθύρου "
                    f"«{data and data['window']['window_title']}»")

    def s06(self):
        data = self.dump()
        rows = data["window"]["info_rows"]
        labels = [row["label"] for row in rows]
        actions = {row["label"]: row["action"] for row in rows}
        ok = (labels[:5] == ["Κατάσταση", "Αίθουσα", "Υπολογιστής", "Κλίμακα", "Director"]
              and actions["Υπολογιστής"] == st.ACTION_CHANGE
              and actions["Κλίμακα"] == st.ACTION_SCALE
              and "Αποτύπωμα Director" in labels
              and not any("Manager" in label for label in labels))
        return ok, f"γραμμές {labels[:6]}"

    def s07(self):
        mark = self.m.director.mark()
        code, _output = self.request_change(ROOM3, NAME2)
        logged = self.m.director.wait_for("[Αλλαγές] Αίτημα", f"→ {ROOM3} · {NAME2}",
                                          since=mark, timeout=20) is not None
        data = self.wait_dump(lambda d: "αναμονή έγκρισης από τον Director"
                              in (d["window"]["info"] or ""))
        local = self.local_change()
        ok = (code == 0 and logged and data is not None and local is not None
              and local["state"] == cr.PENDING)
        return ok, (f"χωρίς κωδικό· μπλε μπάρα «{data and data['window']['info']}», "
                    f"{cr.CLIENT_FILE}: {local and local['state']}")

    def s08(self):
        ok = self.m.command("changes", f"→ {ROOM3} · {NAME2}")
        return ok, "η εντολή changes δείχνει το αίτημα με παλιές και νέες τιμές"

    def s09(self):
        cid = self.client_id()
        denied = self.m.command(f"deny-change {cid[:8]}", "Απορρίφθηκε η αλλαγή")
        data = self.wait_dump(lambda d: "απορρίφθηκε" in (d["window"]["alarm"] or ""))
        ok = (denied and data is not None and self.rows(data)["Αίθουσα"] == short_room(ROOM2)
              and (self.local_change() or {}).get("state") == cr.DENIED)
        return ok, f"κόκκινη μπάρα «{data and data['window']['alarm']}», αίθουσα ίδια"

    def s10(self):
        self.stop_client()
        mark = self.m.director.mark()
        code, output = self.request_change(ROOM4, NAME2)
        inbox = (self.device.dir / INBOX_FILE).exists()
        time.sleep(1.5)
        quiet = self.m.director.wait_for("[Αλλαγές] Αίτημα", since=mark, timeout=0.5) is None
        ok = code == 0 and inbox and quiet and "επόμενη εκκίνηση" in output
        return ok, f"Client κλειστός: αίτημα στο {INBOX_FILE} ({inbox}), τίποτα στον Director"

    def s11(self):
        mark = self.m.director.mark()
        self.start_client()
        logged = self.m.director.wait_for("[Αλλαγές] Αίτημα", f"→ {ROOM4} · {NAME2}",
                                          since=mark, timeout=40) is not None
        data = self.wait_dump(lambda d: "αναμονή έγκρισης από τον Director"
                              in (d["window"]["info"] or ""), timeout=40)
        ok = logged and data is not None and not (self.device.dir / INBOX_FILE).exists()
        return ok, "στην εκκίνηση ο Client έστειλε το αίτημα· μπλε μπάρα αναμονής"

    def s12(self):
        self.stop_client()
        time.sleep(1)
        cid = self.client_id()
        approved = self.m.command(f"approve-change {cid[:8]}", "Εγκρίθηκε η αλλαγή")
        ok = approved and self.m.command("changes", "Δεν υπάρχουν αιτήματα αλλαγής")
        return ok, "έγκριση με τον Client κλειστό, χωρίς κωδικό"

    def s13(self):
        self.start_client()
        data = self.wait_dump(lambda d: d["state"] == st.CONNECTED
                              and d["window"]["title"] == room_title(ROOM4, NAME2), timeout=40)
        ok = (data is not None and data["window"]["info"] is None
              and data["window"]["alarm"] is None and self.local_change() is None)
        return ok, (f"μετά την επανασύνδεση: {data and data['window']['title']}, "
                    f"χωρίς μπάρες, {cr.CLIENT_FILE} σβήστηκε")

    def s14(self):
        mark = self.m.director.mark()
        code, _output = self.request_change(ROOM4, NAME2)       # ίδιες τιμές
        time.sleep(4)
        quiet = self.m.director.wait_for("[Αλλαγές] Αίτημα", since=mark, timeout=0.5) is None
        self.m.director.wait_for(self.client_id(), JOINED, timeout=1)
        ok = code == 0 and quiet and self.local_change() is None
        return ok, "ίδιες τιμές με το μητρώο: κανένα αίτημα στον Director"

    def s15(self):
        old_id = self.client_id()
        self.m.director.send(f"revoke {old_id}")
        revoked = self.wait_dump(lambda d: d["state"] == st.REVOKED, timeout=40) is not None
        mark = self.m.director.mark()
        code, _output = self.request_change(ROOM5, NAME5)
        line = self.m.director.wait_for("Νέο αίτημα", f"| {ROOM5} | {NAME5}", since=mark,
                                        timeout=40)
        data = self.wait_dump(lambda d: d["state"] == st.PENDING and d["window"]["code"],
                              timeout=40)
        self.new_code = data and data["window"]["code"].replace(" ", "")
        archived = list(self.device.dir.glob("previous-identity-*"))
        ok = (revoked and code == 0 and line is not None and data is not None
              and self.client_id() != old_id and archived)
        self.mark5 = mark
        return ok, (f"ανάκληση → «Αλλαγή…»: νέα ταυτότητα ({self.client_id()[:8]}), "
                    f"νέος κωδικός {self.new_code}, παλιά αρχεία σε {len(archived)} φάκελο")

    def s16(self):
        approved = self.approve_enrollment(ROOM5, NAME5, self.new_code or "", self.mark5)
        data = self.wait_dump(lambda d: d["state"] == st.CONNECTED, timeout=40)
        ok = (approved and data is not None
              and data["window"]["title"] == room_title(ROOM5, NAME5)
              and self.rows(data)["ID υπολογιστή"] == self.client_id())
        return ok, "η νέα ταυτότητα συνδέθηκε χωρίς επανεκκίνηση του Client"

    def run(self) -> int:
        steps = [
            ("Manager με δοκιμαστικό τόνο", self.s01),
            ("Αίτηση εγγραφής: κωδικός και request_id στο αρχείο αιτήματος", self.s02),
            ("Client σε αναμονή έγκρισης", self.s03),
            ("Πριν από την έγκριση: διόρθωση αιτήματος, ίδιος κωδικός", self.s04),
            ("Έγκριση με τον ίδιο κωδικό: νέες τιμές, τίτλος παραθύρου", self.s05),
            ("Πληροφορίες: Υπολογιστής/Κλίμακα με «Αλλαγή…», Director αντί για Manager",
             self.s06),
            ("Εγκεκριμένος: αίτημα αλλαγής χωρίς κωδικό, μπλε μπάρα", self.s07),
            ("Director: η εντολή changes δείχνει το αίτημα", self.s08),
            ("Απόρριψη: κόκκινη μπάρα, στοιχεία ίδια", self.s09),
            ("Client κλειστός: αίτημα μένει τοπικά", self.s10),
            ("Εκκίνηση: το αίτημα στέλνεται στον Director", self.s11),
            ("Έγκριση με τον Client κλειστό", self.s12),
            ("Επανασύνδεση: νέα αίθουσα, χωρίς μπάρες", self.s13),
            ("Ίδιες τιμές: κανένα αίτημα", self.s14),
            ("Μετά από ανάκληση: νέα ταυτότητα και νέος κωδικός", self.s15),
            ("Έγκριση νέας ταυτότητας: σύνδεση χωρίς επανεκκίνηση", self.s16),
        ]
        try:
            for title, function in steps:
                self.step(title, function)
        finally:
            self.stop_client()
            self.m.stop()
        passed = sum(1 for _, ok, _ in self.results if ok)
        print(f"\nΣΥΝΟΛΟ: {passed}/{TOTAL} PASS", flush=True)
        return 0 if passed == TOTAL else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--visible", action="store_true",
                        help="πραγματικό tray/παράθυρο αντί για offscreen")
    parser.add_argument("--keep", action="store_true", help="κράτα τον προσωρινό φάκελο")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="sch-intercom-change-"))
    print(f"Προσωρινός φάκελος: {root}", flush=True)
    try:
        return LiveTest(root, args.visible).run()
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
