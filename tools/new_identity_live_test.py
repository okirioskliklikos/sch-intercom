"""5C3: πραγματική δοκιμή «Νέα ταυτότητα στο <IP>» και κλώνων με σύσταση, 16 βήματα PASS/FAIL.

Μόνο loopback. Ένας προσωρινός Manager (δοκιμαστικός τόνος) και Clients με tray (offscreen):

- Board1 εγκρίνεται κανονικά με κωδικό.
- Ένα αντίγραφο του φακέλου του Board1 χωρίς ανίχνευση κλώνου (ίδιος υπολογιστής) συνδέεται
  από 127.0.0.2 μέσω TCP proxy: διπλή ταυτότητα. Ο Director στέλνει «Νέα ταυτότητα» μόνο στη
  σύνδεση της 127.0.0.2, και εγκρίνει το νέο αίτημα χωρίς κωδικό.
- Ένα αντίγραφο «από άλλο υπολογιστή» (machine-binding αλλαγμένο) καταλαβαίνει ότι είναι
  κλώνος, ανοίγει το παράθυρο για όνομα («Αποστολή» = ``--request-change``) και στέλνει αίτημα
  με σύσταση: έγκριση χωρίς κωδικό, με αίθουσα/όνομα που αλλάζει ο Director.
- Ανάκληση του Board1 μαζί με όσους εγκρίθηκαν με σύστασή του· κλώνος ανακληθείσας
  ταυτότητας χρειάζεται πάλι κωδικό.

Ό,τι δείχνουν οι Clients διαβάζεται από το κρυφό ``--status-dump``.

    python tools/new_identity_live_test.py [--keep]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from client import status as st  # noqa: E402
from shared.room_names import room_title  # noqa: E402
from tools.client_tray_live_test import ToneMachine  # noqa: E402
from tools.failure_live_test import SECOND_IP, FreezableProxy  # noqa: E402
from tools.manager_role_live_test import ROOM, TestDevice  # noqa: E402
from tools.mtls_live_test import JOINED, Process, child_env  # noqa: E402
from shared.ui.offscreen import windows_fonts_dir  # noqa: E402

TOTAL = 16
NAME = "Board1"
TWIN_NAME = "Board2"             # όνομα της νέας ταυτότητας του διπλού
CLONE_NAME = "PC-07"             # όνομα από το παράθυρο του κλώνου («Αποστολή»)
LAB_ROOM, LAB_NAME = "C2627-Εργ", "PC05"   # ο Director τα αλλάζει στην έγκριση
PORTS = (51855, 51856, 51859, 51857, 51867)
PROXY_PORT = 51860


class Client:
    """Client με tray (offscreen) και ``--status-dump``, σε δικό του φάκελο."""

    def __init__(self, test: "LiveTest", device: TestDevice, *, port: int | None = None):
        self.test = test
        self.device = device
        self.port = port
        self.dump_file = test.root / f"dump-{device.name}.json"
        self.process: Process | None = None

    def args(self) -> list[str]:
        args = self.device.args(self.test.m.cert)
        if self.port is not None:
            args[args.index("--port") + 1] = str(self.port)
        return [*args, "--status-dump", str(self.dump_file)]

    def start(self, *extra: str) -> Process:
        self.stop()
        self.dump_file.unlink(missing_ok=True)
        self.process = Process(["-m", "client.main", *self.args(), *extra],
                               label=f"Client {self.device.name}", env=self.test.env())
        self.process.wait_for("Client ID:", timeout=40)
        return self.process

    def stop(self) -> None:
        if self.process is not None:
            self.process.stop()
            self.process = None

    def client_id(self) -> str | None:
        """None όσο το client.json λείπει ή γράφεται (π.χ. στη μετακίνηση της παλιάς ταυτότητας)."""
        try:
            return json.loads((self.device.dir / "client.json").read_text(
                encoding="utf-8"))["client_id"]
        except (OSError, ValueError, KeyError):
            return None

    def request_change(self, room: str, name: str) -> bool:
        """Όπως το «Αποστολή»/«Αλλαγή…» του παραθύρου (ίδιος δρόμος, inbox)."""
        done = subprocess.run([sys.executable, "-m", "client.main",
                               *self.device.args(self.test.m.cert), "--request-change",
                               "--room", room, "--computer-name", name],
                              cwd=ROOT, env=child_env(), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
        return done.returncode == 0

    def dump(self) -> dict | None:
        try:
            return json.loads(self.dump_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def wait_dump(self, check, timeout: float = 40.0) -> dict | None:
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
        print(f"    τελευταίο dump {self.device.name}: "
              f"{json.dumps(last, ensure_ascii=False)[:700]}", flush=True)
        return None


class LiveTest:
    def __init__(self, root: Path):
        self.root = root
        self.m = ToneMachine(root, "manager", *PORTS)
        self.board = Client(self, TestDevice(root, NAME).use(self.m))
        self.twin: Client | None = None
        self.lab: Client | None = None
        self.late: Client | None = None
        self.proxy: FreezableProxy | None = None
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
        env["QT_QPA_PLATFORM"] = "offscreen"
        fonts = windows_fonts_dir()
        if fonts:
            env.setdefault("QT_QPA_FONTDIR", fonts)
        return env

    def copy(self, name: str, *, other_machine: bool) -> TestDevice:
        """Αντίγραφο του φακέλου του Board1 (όπως image δίσκου)."""
        device = TestDevice(self.root, name).use(self.m)
        shutil.rmtree(device.dir, ignore_errors=True)
        shutil.copytree(self.board.device.dir, device.dir,
                        ignore=shutil.ignore_patterns("*.tmp", "*.json.*", "history.jsonl"))
        if other_machine:
            binding = device.dir / "credentials" / "machine-binding.json"
            data = json.loads(binding.read_text(encoding="utf-8"))
            data["machine_id"] = "0" * 32   # η ταυτότητα φτιάχτηκε σε άλλον υπολογιστή
            binding.write_text(json.dumps(data), encoding="utf-8")
        return device

    def short(self, line: str | None) -> str | None:
        return line.split("Νέο αίτημα")[1].split("|")[0].strip() if line else None

    def registry_entry(self, client_id: str) -> dict | None:
        data = json.loads(self.m.registry.read_text(encoding="utf-8"))
        return next((e for e in data["registry"]["clients"] if e["client_id"] == client_id),
                    None)

    # -------------------------------------------------------------- βήματα
    def s01(self):
        ok = self.m.create_identity()
        self.m.start()
        return ok, f"Manager σε 127.0.0.1:{PORTS[0]} με δοκιμαστικό τόνο (πρωτόκολλο v9)"

    def s02(self):
        mark = self.m.director.mark()
        code = self.board.device.enroll(self.m.cert)
        line = self.m.director.wait_for("Νέο αίτημα", f"| {ROOM} | {NAME}", since=mark,
                                        timeout=15)
        approved = bool(code) and self.m.command(f"approve {self.short(line)} {code}",
                                                 "Εγκρίθηκε:")
        self.board.start()
        data = self.board.wait_dump(lambda d: d["state"] == st.CONNECTED)
        self.origin = self.board.client_id()
        return approved and data is not None, "Board1 εγκρίθηκε με κωδικό και συνδέθηκε"

    def s03(self):
        self.proxy = FreezableProxy(PROXY_PORT, PORTS[0], listen="127.0.0.1", source=SECOND_IP)
        self.twin = Client(self, self.copy("Twin", other_machine=False), port=PROXY_PORT)
        mark = self.m.director.mark()
        self.twin.start()
        warning = self.m.director.wait_for("ΠΡΟΣΟΧΗ", self.origin, since=mark, timeout=90)
        same = self.twin.client_id() == self.origin
        return warning is not None and same, (f"ίδιο UUID {same}, προειδοποίηση του Director "
                                              f"για 127.0.0.1 και {SECOND_IP}: "
                                              f"{warning is not None}")

    def s04(self):
        ok = self.m.command("status", "Ίδια ταυτότητα σε δύο υπολογιστές")
        return ok, "η εντολή status δείχνει τη διπλή ταυτότητα"

    def s05(self):
        mark_d, mark_t = self.m.director.mark(), self.twin.process.mark()
        sent = self.m.command(f"new-identity {self.origin} {SECOND_IP} {ROOM} {TWIN_NAME}",
                              f"Νέα ταυτότητα στο {SECOND_IP}:", timeout=10)
        asked = self.twin.process.wait_for("Ο Director ζήτησε νέα ταυτότητα", since=mark_t,
                                           timeout=90)
        self.mark_new = mark_d
        return sent and asked is not None, (f"εντολή μόνο στη σύνδεση της {SECOND_IP}: "
                                            f"ο Client τη δέχτηκε {asked is not None}")

    def s06(self):
        deadline = time.monotonic() + 30
        # Στα Windows το βήμα μπορεί να τρέξει ενώ ο Client μετακινεί τα παλιά αρχεία.
        self.twin_id = ""
        while time.monotonic() < deadline:
            self.twin_id = self.twin.client_id() or ""
            if self.twin_id and self.twin_id != self.origin:
                break
            time.sleep(0.3)
        archived = list(self.twin.device.dir.glob("previous-identity-*"))
        board_same = self.board.client_id() == self.origin
        ok = self.twin_id != self.origin and bool(archived) and board_same
        return ok, (f"νέο UUID {self.twin_id[:8]}, παλιά αρχεία σε {len(archived)} φάκελο, "
                    f"ο Board1 κράτησε την ταυτότητά του {board_same}")

    def s07(self):
        line = self.m.director.wait_for("Νέο αίτημα", f"| {ROOM} | {TWIN_NAME}",
                                        "νέα ταυτότητα του", since=self.mark_new, timeout=30)
        self.twin_short = self.short(line)
        listed = self.m.command("pending", "(χωρίς κωδικό)")
        return line is not None and listed, "το αίτημα φέρει τη σύσταση: «χωρίς κωδικό»"

    def s08(self):
        data = self.twin.wait_dump(lambda d: d["state"] == st.PENDING
                                   and d["window"]["info"] ==
                                   "Νέα ταυτότητα από τον Director: αναμονή έγκρισης")
        ok = data is not None and data["window"]["code"] is None
        return ok, "Client: μπλε μπάρα «Νέα ταυτότητα από τον Director», χωρίς κωδικό"

    def s09(self):
        mark = self.m.director.mark()
        approved = self.m.command(f"approve {self.twin_short}", "Εγκρίθηκε χωρίς κωδικό")
        data = self.twin.wait_dump(lambda d: d["state"] == st.CONNECTED
                                   and d["window"]["title"] == room_title(ROOM, TWIN_NAME),
                                   timeout=60)
        joined = self.m.director.wait_for(self.twin_id, JOINED, since=mark, timeout=30)
        return approved and data is not None and joined is not None, \
            f"έγκριση χωρίς κωδικό, ο Client συνδέθηκε ως {room_title(ROOM, TWIN_NAME)}"

    def s10(self):
        time.sleep(12)    # δύο κύκλοι επανασύνδεσης: καμία νέα «κλοπή» σύνδεσης
        mark = self.m.director.mark()
        self.m.director.send("status")
        time.sleep(2)
        duplicate = self.m.director.seen("Ίδια ταυτότητα σε δύο υπολογιστές", since=mark)
        both = (self.m.director.seen(self.origin, "Online", since=mark)
                and self.m.director.seen(self.twin_id, "Online", since=mark))
        entry = self.registry_entry(self.twin_id) or {}
        ok = not duplicate and both and entry.get("origin") == self.origin
        return ok, (f"χωρίς διπλή ταυτότητα {not duplicate}, και οι δύο Online {both}, "
                    f"μητρώο: origin = Board1 {entry.get('origin') == self.origin}")

    def s11(self):
        self.lab = Client(self, self.copy("Lab", other_machine=True))
        mark = self.m.director.mark()
        process = self.lab.start()
        detected = process.wait_for("μοιάζει αντίγραφο", timeout=10) is not None
        hostname = socket.gethostname()[:64]
        data = self.lab.wait_dump(lambda d: d["state"] == st.NAMING
                                  and d["window"]["naming"] is not None)
        window = (data or {}).get("window") or {}
        naming = window.get("naming") or {}
        time.sleep(2)
        no_request = not self.m.director.seen("Νέο αίτημα", since=mark)
        ok = (detected and data is not None and window.get("visible")
              and window.get("tab") == "Πληροφορίες"
              and (naming.get("room"), naming.get("name")) == (ROOM, hostname)
              and "Αποστέλλεται αυτόματα σε" in naming.get("countdown", "")
              and window.get("code") is None and no_request)
        return ok, (f"ανίχνευση {detected}, παράθυρο ανοιχτό στις Πληροφορίες "
                    f"{window.get('visible')}, πεδία {naming.get('room')} · {naming.get('name')}, "
                    f"«{naming.get('countdown')}», κανένα αίτημα ακόμη {no_request}")

    def s12(self):
        mark = self.m.director.mark()
        sent = self.lab.request_change(ROOM, CLONE_NAME)
        line = self.m.director.wait_for("Νέο αίτημα", f"| {ROOM} | {CLONE_NAME}", "κλώνος του",
                                        since=mark, timeout=30)
        self.lab_short = self.short(line)
        self.lab_id = self.lab.client_id()
        expected = f"ως {room_title(ROOM, CLONE_NAME)}: αναμονή έγκρισης από τον Director"
        data = self.lab.wait_dump(lambda d: d["state"] == st.PENDING
                                  and expected in (d["window"]["info"] or ""))
        ok = (sent and line is not None and data is not None
              and data["window"]["code"] is None and data["window"]["naming"] is None
              and self.lab_id not in (None, self.origin))
        return ok, (f"«Αποστολή» {sent}, αίτημα με σύσταση {ROOM} · {CLONE_NAME} στον Director "
                    f"{line is not None}, Client: «{data and data['window']['info']}»")

    def s13(self):
        mark = self.m.director.mark()
        approved = self.m.command(f"approve {self.lab_short} {LAB_ROOM} {LAB_NAME}",
                                  "Εγκρίθηκε χωρίς κωδικό")
        data = self.lab.wait_dump(lambda d: d["state"] == st.CONNECTED
                                  and d["window"]["title"] == room_title(LAB_ROOM, LAB_NAME),
                                  timeout=60)
        joined = self.m.director.wait_for(self.lab_id, JOINED, since=mark, timeout=30)
        entry = self.registry_entry(self.lab_id) or {}
        ok = (approved and data is not None and joined is not None
              and entry.get("origin") == self.origin)
        return ok, (f"όνομα από τον Director ({LAB_ROOM} · {LAB_NAME}), συνδέθηκε, "
                    f"origin στο μητρώο {entry.get('origin') == self.origin}")

    def s14(self):
        mark = self.m.director.mark()
        self.m.director.send(f"revoke {self.origin} with-clones")
        revoked = self.m.director.wait_for(f"Ανακλήθηκε ο Client {self.origin}", since=mark,
                                           timeout=10) is not None
        time.sleep(1)
        clones = sum(self.m.director.seen("Ανακλήθηκε και ο κλώνος", cid, since=mark)
                     for cid in (self.twin_id, self.lab_id))
        return revoked and clones == 2, f"ανάκληση Board1 και των {clones} που σύστησε"

    def s15(self):
        states = [client.wait_dump(lambda d: d["state"] == st.REVOKED, timeout=40)
                  for client in (self.board, self.twin, self.lab)]
        ok = all(state is not None for state in states)
        return ok, "Board1, νέα ταυτότητα και κλώνος: «ανακλήθηκε» στο παράθυρο"

    def s16(self):
        for client in (self.twin, self.lab):
            client.stop()
        self.late = Client(self, self.copy("Late", other_machine=True))
        mark = self.m.director.mark()
        process = self.late.start("--clone-name-seconds", "4")
        naming = self.late.wait_dump(lambda d: d["state"] == st.NAMING, timeout=20)
        auto = process.wait_for("αυτόματα (λήξη αναμονής)", timeout=30)
        code_line = process.wait_for("Κωδικός:", timeout=30)
        refused = self.m.director.wait_for("η σύσταση δεν", since=mark, timeout=10)
        data = self.late.wait_dump(lambda d: d["state"] == st.PENDING and d["window"]["code"])
        ok = (naming is not None and auto is not None and code_line is not None
              and refused is not None and data is not None)
        return ok, (f"αναμονή ονόματος {naming is not None}, αυτόματη αποστολή στη λήξη "
                    f"{auto is not None}, χωρίς σύσταση, με κωδικό "
                    f"{data and data['window']['code']}")

    def run(self) -> int:
        steps = [
            ("Manager με δοκιμαστικό τόνο", self.s01),
            ("Board1: έγκριση με κωδικό και σύνδεση", self.s02),
            (f"Αντίγραφο χωρίς ανίχνευση από {SECOND_IP}: προειδοποίηση διπλής ταυτότητας",
             self.s03),
            ("Director: status δείχνει τη διπλή ταυτότητα", self.s04),
            (f"«Νέα ταυτότητα στο {SECOND_IP}» μόνο σε εκείνη τη σύνδεση", self.s05),
            ("Ο υπολογιστής της IP πήρε νέα ταυτότητα, ο Board1 όχι", self.s06),
            ("Director: νέο αίτημα με σύσταση, «χωρίς κωδικό»", self.s07),
            ("Client: μπάρα «Νέα ταυτότητα από τον Director», χωρίς κωδικό", self.s08),
            ("Έγκριση χωρίς κωδικό: σύνδεση με το νέο όνομα", self.s09),
            ("Τέλος διπλής ταυτότητας, origin στο μητρώο", self.s10),
            ("Κλώνος από άλλον υπολογιστή: παράθυρο για όνομα, κανένα αίτημα ακόμη", self.s11),
            ("«Αποστολή»: αίτημα με σύσταση, χωρίς κωδικό", self.s12),
            ("Έγκριση κλώνου με όνομα από τον Director", self.s13),
            ("Ανάκληση Board1 μαζί με όσους σύστησε", self.s14),
            ("Οι τρεις υπολογιστές βλέπουν την ανάκληση", self.s15),
            ("Κλώνος ανακληθείσας ταυτότητας: αυτόματη αποστολή, χρειάζεται κωδικό", self.s16),
        ]
        try:
            for title, function in steps:
                self.step(title, function)
        finally:
            for client in (self.board, self.twin, self.lab, self.late):
                if client is not None:
                    client.stop()
            if self.proxy is not None:
                self.proxy.close()
            self.m.stop()
        passed = sum(1 for _, ok, _ in self.results if ok)
        print(f"\nΣΥΝΟΛΟ: {passed}/{TOTAL} PASS", flush=True)
        return 0 if passed == TOTAL else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="κράτα τον προσωρινό φάκελο")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="sch-intercom-identity-"))
    print(f"Προσωρινός φάκελος: {root}", flush=True)
    try:
        return LiveTest(root).run()
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
