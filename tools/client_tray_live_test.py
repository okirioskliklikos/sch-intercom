"""5C1: πραγματική δοκιμή tray/παραθύρου του Client, 15 βήματα PASS/FAIL, μόνο loopback.

Ένας προσωρινός Manager και ένας Client με tray. Ο Client γράφει με το κρυφό
``--status-dump`` τι δείχνουν πραγματικά το εικονίδιο tray (κουκκίδα, tooltip, μενού) και
το παράθυρο (κάρτα κωδικού, Ιστορικό, Πληροφορίες)· η δοκιμή το διαβάζει.

Το Qt τρέχει offscreen (και στα Windows), ώστε να μην ανοίγουν παράθυρα στη μέση της
δοκιμής· με ``--visible`` εμφανίζονται κανονικά (Windows: πραγματικό εικονίδιο tray).

    python tools/client_tray_live_test.py [--visible]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import sys
import tempfile
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client import status as st  # noqa: E402
from shared.ui.offscreen import windows_fonts_dir  # noqa: E402
from shared.version import APP_VERSION  # noqa: E402
from shared.room_names import room_title, short_room  # noqa: E402
from tools.manager_role_live_test import ROOM, TestDevice, TestMachine  # noqa: E402
from tools.mtls_live_test import JOINED, Process, child_env  # noqa: E402

TOTAL = 15
NAME = "Board1"
TITLE = room_title(ROOM, NAME)     # «Γ1 · Board1» στη σχολική χρονιά 2026-27


class ToneMachine(TestMachine):
    """Manager με σταθερό δοκιμαστικό τόνο αντί για μικρόφωνο (κρυφό --test-tone)."""

    def start(self) -> Process:
        self.director = Process([
            "-m", "director.main", *self.paths(), "--host", "127.0.0.1",
            "--port", str(self.port), "--discovery-port", str(self.discovery),
            "--enrollment-port", str(self.enrollment),
            "--director-id-file", str(self.dir / "director.id"),
            "--watch-port", str(self.watch), "--extra-beacon-port", str(self.extra),
            "--start-check-seconds", "3", "--test-tone", "440"],
            label=f"Manager {self.dir.name}")
        if self.director.wait_for("[Manager] Εξυπηρέτηση:", timeout=30) is None:
            raise RuntimeError(f"Δεν εκκίνησε ο {self.dir.name}")
        return self.director


class LiveTest:
    def __init__(self, root: Path, visible: bool):
        self.root = root
        self.visible = visible
        self.m = ToneMachine(root, "manager", 51655, 51656, 51659, 51657, 51667)
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

    def env(self, *, tray: bool = True) -> dict:
        env = child_env()
        if tray:
            env.pop(st.NO_TRAY_ENV, None)
            if not self.visible:
                env["QT_QPA_PLATFORM"] = "offscreen"
                fonts = windows_fonts_dir()
                if fonts:
                    env.setdefault("QT_QPA_FONTDIR", fonts)
        return env

    def start_client(self, *, tray: bool = True, show_window: bool = False) -> Process:
        self.stop_client()
        self.dump_file.unlink(missing_ok=True)
        args = ["-m", "client.main", *self.device.args(self.m.cert), "--room", ROOM,
                "--status-dump", str(self.dump_file)]
        if show_window:
            args.append("--show-window")
        self.client = Process(args, label=f"Client {NAME}", env=self.env(tray=tray))
        self.client.wait_for("Client ID:", timeout=30)
        return self.client

    def stop_client(self) -> None:
        if self.client is not None:
            self.client.stop()
            self.client = None

    def dump(self) -> dict | None:
        try:
            return json.loads(self.dump_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def wait_dump(self, check, timeout: float = 20.0) -> dict | None:
        """Περιμένει μέχρι το dump να ικανοποιεί το check (το γράφει ο Client σε κάθε αλλαγή)."""
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
        print(f"    τελευταίο dump: {json.dumps(last, ensure_ascii=False)[:600]}", flush=True)
        return None

    @property
    def pending_file(self) -> Path:
        return self.device.dir / st.PENDING_FILE

    # -------------------------------------------------------------- βήματα
    def s01(self):
        ok = self.m.create_identity()
        self.m.start()
        return ok, "Manager σε 127.0.0.1:51655 με δοκιμαστικό τόνο"

    def s02(self):
        self.code = self.device.enroll(self.m.cert)
        saved = json.loads(self.pending_file.read_text(encoding="utf-8")) \
            if self.pending_file.is_file() else {}
        ok = bool(self.code) and saved.get("code") == self.code and \
            saved.get("client_id") == self.device.client_id
        return ok, f"κωδικός {self.code}, {st.PENDING_FILE}: {bool(saved)}"

    def s03(self):
        self.start_client()
        data = self.wait_dump(lambda d: d["state"] == st.PENDING and d["window"]["visible"])
        if data is None:
            return False, "δεν εμφανίστηκε αναμονή έγκρισης με ανοιχτό παράθυρο"
        window = data["window"]
        ok = (window["code"] == st.format_code(self.code) and data["tone"] == "wait"
              and window["title"] == TITLE and window["status"] == "● Αναμονή έγκρισης"
              and window["code_expires"].startswith("Λήγει"))
        return ok, (f"παράθυρο ανοιχτό μόνο του, κωδικός «{window['code']}», "
                    f"κουκκίδα {data['tone']}, {window['code_expires']}")

    def s04(self):
        data = self.dump()
        menu = data["menu"]
        expected = f"{TITLE} — Αναμονή έγκρισης, κωδικός {st.format_code(self.code)}"
        texts = [item["text"] for item in menu]
        ok = (data["tooltip"] == expected and texts == [TITLE, "Αναμονή έγκρισης", "Ιστορικό…", "Πληροφορίες…"]
              and [item["enabled"] for item in menu] == [False, False, True, True]
              and not any("Έξοδ" in text for text in texts))
        return ok, f"tooltip «{data['tooltip']}», μενού {texts}"

    def s05(self):
        director = self.m.director
        line = director.wait_for("Νέο αίτημα", f"| {ROOM} | {NAME}", timeout=10)
        short = line.split("Νέο αίτημα")[1].split("|")[0].strip()
        mark = director.mark()
        director.send(f"approve {short} {self.code}")
        approved = director.wait_for("Εγκρίθηκε:", since=mark, timeout=10) is not None
        data = self.wait_dump(lambda d: d["state"] == st.CONNECTED, timeout=40)
        ok = (approved and data is not None and data["tone"] == "ok"
              and data["window"]["code"] is None and not self.pending_file.exists())
        return ok, (f"έγκριση {approved}, κατάσταση {data and data['state']}, "
                    f"κουκκίδα {data and data['tone']}, αρχείο αιτήματος σβήστηκε "
                    f"{not self.pending_file.exists()}")

    def s06(self):
        data = self.dump()
        texts = [item["text"] for item in data["menu"]]
        ok = (data["tooltip"] == f"{TITLE} — Συνδεδεμένος"
              and texts == [TITLE, "Συνδεδεμένος", "Ιστορικό…", "Πληροφορίες…"]
              and data["window"]["status"] == "● Συνδεδεμένος"
              and data["window"]["alarm"] is None)
        return ok, f"tooltip «{data['tooltip']}», μενού {texts}"

    def s07(self):
        data = self.dump()
        rows = {row["label"]: row["value"] for row in data["window"]["info_rows"]}
        der = ssl.PEM_cert_to_DER_cert(self.m.cert.read_text(encoding="ascii"))
        fingerprint = st.short_fingerprint(hashlib.sha256(der).hexdigest())
        expected = {"Αίθουσα": short_room(ROOM), "Υπολογιστής": NAME, "Director": "127.0.0.1",
                    "Αποτύπωμα Director": fingerprint, "Έκδοση": APP_VERSION,
                    "ID υπολογιστή": self.device.client_id, "Κατάσταση": "Συνδεδεμένος"}
        wrong = {k: rows.get(k) for k, v in expected.items() if rows.get(k) != v}
        ok = not wrong and "Ένταση ανακοίνωσης" in rows and "Συσκευή ήχου" in rows
        return ok, ("όλες οι τιμές σωστές (" + ", ".join(f"{k}={v}" for k, v in
                                                      list(expected.items())[:5]) + ")"
                    if ok else f"διαφορές: {wrong}")

    def announce_rate(self, client: Process, seconds: float = 12.0) -> tuple[float, str]:
        """Ανακοίνωση με τον δοκιμαστικό τόνο· κενά ανά block ανάμεσα σε δύο αναφορές (5 s)."""
        mark = client.mark()
        self.m.director.send("all")
        deadline = time.monotonic() + seconds + 6
        reports = []
        while time.monotonic() < deadline and len(reports) < 3:
            reports = [line for line in client.lines[mark:]
                       if "κατάσταση: ανακοίνωση | blocks" in line]
            time.sleep(0.3)
        values = [(int(re.search(r"blocks (\d+)", line).group(1)),
                   int(re.search(r"κενά (\d+)", line).group(1))) for line in reports]
        if len(values) < 3:
            raise RuntimeError(f"μόνο {len(values)} αναφορές ήχου")
        # Η πρώτη αναφορά περιέχει την έναρξη· μετράμε από τη 2η στην 3η.
        (b1, g1), (b2, g2) = values[1], values[2]
        rate = (g2 - g1) / max(1, b2 - b1)
        return rate, f"blocks {b2 - b1}, κενά {g2 - g1}"

    def s08(self):
        """Βάση σύγκρισης: ο ίδιος Client χωρίς tray (στο Linux container η «null» κάρτα
        ήχου δίνει κενά και χωρίς GUI· στα Windows αναμένονται 0)."""
        client = self.start_client(tray=False)
        if client.wait_for(JOINED, timeout=1) is None:
            self.m.director.wait_for(self.device.client_id, JOINED, timeout=40)
        time.sleep(2)
        try:
            self.base_rate, detail = self.announce_rate(client)
        finally:
            self.m.director.send("none")
        time.sleep(2)
        self.start_client(show_window=True)    # όπως «Ιστορικό…» από το tray
        data = self.wait_dump(lambda d: d["state"] == st.CONNECTED and d["window"]["visible"],
                              timeout=40)
        self.events_before = len(self.history_events())
        return data is not None, f"χωρίς tray: {detail} ({self.base_rate:.1%})"

    def history_events(self) -> list[dict]:
        path = self.device.dir / st.HISTORY_FILE
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def s09(self):
        self.rate, self.rate_detail = self.announce_rate(self.client)
        data = self.wait_dump(lambda d: d["window"]["history"][0].get("live") is True,
                              timeout=10)
        ok = data is not None and data["window"]["history"][0]["kind"] == "Ζωντανή ανακοίνωση" \
            and data["window"]["visible"]
        return ok, "γραμμή «● σε εξέλιξη» στο Ιστορικό, παράθυρο ανοιχτό"

    def s10(self):
        ok = self.rate <= self.base_rate + 0.01
        return ok, (f"με tray: {self.rate_detail} ({self.rate:.1%}), χωρίς tray "
                    f"{self.base_rate:.1%} (όριο +1%)")

    def s11(self):
        self.m.director.send("none")
        data = self.wait_dump(lambda d: d["window"]["history"][0].get("live") is False
                              and d["window"]["history"][0].get("duration"), timeout=20)
        events = self.history_events()
        last = events[-1] if events else {}
        ok = (data is not None and len(events) == self.events_before + 1 and last.get("v") == 1
              and last.get("kind") == "live" and last["end"] > last["start"])
        duration = data and data["window"]["history"][0]["duration"]
        return ok, f"διάρκεια {duration}, νέα γραμμή history.jsonl: {last}"

    def s12(self):
        self.start_client()
        data = self.wait_dump(lambda d: d["state"] == st.CONNECTED, timeout=40)
        ok = (data is not None and not data["window"]["visible"]
              and len([row for row in data["window"]["history"] if "kind" in row])
              == len(self.history_events()))
        return ok, ("μετά την επανεκκίνηση: συνδεδεμένος, παράθυρο κρυφό στο tray, "
                    "ιστορικό διατηρήθηκε" if ok else f"dump {data and data['window']}")

    def s13(self):
        self.m.director.send(f"revoke {self.device.client_id}")
        data = self.wait_dump(lambda d: d["state"] == st.REVOKED, timeout=40)
        ok = data is not None and data["tone"] == "bad" and \
            "ανακλήθηκε" in (data["window"]["alarm"] or "")
        return ok, f"κόκκινη κουκκίδα και μπάρα: {data and data['window']['alarm']}"

    def s14(self):
        data = self.dump()
        ok = data["tooltip"] == f"{TITLE} — Ανακλήθηκε" and \
            data["menu"][1]["text"] == "Ανακλήθηκε"
        return ok, f"tooltip «{data['tooltip']}»"

    def s15(self):
        client = self.start_client(tray=False)
        attempt = client.wait_for("Σύνδεση στον Director", timeout=20)
        time.sleep(1)
        ok = attempt is not None and not self.dump_file.exists() and \
            client.proc.poll() is None
        return ok, "SCH_INTERCOM_NO_TRAY=1: κονσόλα όπως πριν, χωρίς tray/dump"

    def run(self) -> int:
        steps = [
            ("Manager με δοκιμαστικό τόνο", self.s01),
            ("Αίτηση εγγραφής: κωδικός και αρχείο αιτήματος", self.s02),
            ("Αναμονή έγκρισης: παράθυρο ανοίγει μόνο του με τον κωδικό, κίτρινη κουκκίδα",
             self.s03),
            ("Tooltip με κωδικό, μενού δύο γκρι γραμμές + Ιστορικό…/Πληροφορίες…, χωρίς Έξοδο", self.s04),
            ("Έγκριση στον Manager: πράσινη κουκκίδα, κωδικός φεύγει", self.s05),
            ("Tooltip/μενού «Συνδεδεμένος»", self.s06),
            ("Πληροφορίες: αίθουσα, PC, Director, αποτύπωμα, έκδοση, ID", self.s07),
            ("Βάση σύγκρισης: ανακοίνωση με τον Client χωρίς tray", self.s08),
            ("Ανακοίνωση με tray: γραμμή «σε εξέλιξη» στο Ιστορικό", self.s09),
            ("Ήχος με tray και ανοιχτό παράθυρο όσο καλός όσο χωρίς tray", self.s10),
            ("Τέλος ανακοίνωσης: διάρκεια και γραμμή στο history.jsonl", self.s11),
            ("Επανεκκίνηση Client: tray, κρυφό παράθυρο, ιστορικό μένει", self.s12),
            ("Ανάκληση: κόκκινη κουκκίδα και κόκκινη μπάρα", self.s13),
            ("Tooltip/μενού «Ανακλήθηκε»", self.s14),
            ("Χωρίς tray: κονσόλα όπως πριν", self.s15),
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
    root = Path(tempfile.mkdtemp(prefix="sch-intercom-tray-"))
    print(f"Προσωρινός φάκελος: {root}", flush=True)
    try:
        return LiveTest(root, args.visible).run()
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
