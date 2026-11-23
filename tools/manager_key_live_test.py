"""Αυτόματη πραγματική δοκιμή 4.2C2: κλειδί TLS του Manager κρυπτογραφημένο (DPAPI).

Τρέχει στον υπολογιστή του Manager (κύριο σενάριο: Windows) τα πραγματικά
``director.main``, ``client.main`` και ``director.backup`` σε loopback, σε
προσωρινό φάκελο και θύρες 51555/51556/51559 (όπως το backup_live_test). Η κανονική
εγκατάσταση δεν αγγίζεται. Τυπώνει PASS/FAIL ανά βήμα και σύνολο.

    Windows:  powershell -ExecutionPolicy Bypass -File tools\\manager_key_live_test.ps1
    Linux:    tools/manager_key_live_test.sh
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.device_identity import _write_secret  # noqa: E402
from shared.manager_key import (is_protected, key_password, password_path,  # noqa: E402
                                plaintext_pem)
from tools.backup_live_test import Device, Machine, fingerprint  # noqa: E402
from tools.mtls_live_test import JOINED, child_env  # noqa: E402

TOTAL = 8
MIGRATED = "κρυπτογραφήθηκε στον δίσκο"


class KeyTest:
    def __init__(self, base: Path):
        self.base = base
        self.results: list[tuple[str, bool]] = []
        self.pc = Machine(base, "pc-main")
        self.new = Machine(base, "pc-new")
        self.board = Device(base, "Board1")
        self.backup = base / "usb" / "key-test.sicb"

    def step(self, name: str, action) -> None:
        try:
            outcome = action()
            ok, detail = outcome if isinstance(outcome, tuple) else (bool(outcome), "")
        except Exception as exc:  # η δοκιμή αναφέρει, δεν κρύβει
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        self.results.append((name, ok))
        print(f"\n=== {'PASS' if ok else 'FAIL'} {len(self.results):02d}/{TOTAL}: {name}"
              + (f" — {detail}" if detail else "") + "\n", flush=True)

    def no_plain_key(self, folder: Path, secret: bytes) -> bool:
        return all(secret not in p.read_bytes() and b"BEGIN PRIVATE KEY" not in p.read_bytes()
                   for p in folder.rglob("*") if p.is_file())

    def connect(self, machine: Machine) -> tuple[bool, bool]:
        """(ο Client συνδέθηκε, ο Manager ζήτησε νέα εγγραφή)."""
        director = machine.director
        mark = director.mark()
        self.board.start(machine.cert)
        joined = director.wait_for(self.board.client_id, JOINED, since=mark, timeout=40)
        enrolled = director.seen("Νέο αίτημα", since=mark)
        self.board.stop()
        return joined is not None, enrolled

    # ---------------------------------------------------------------- βήματα
    def s1_encrypted(self):
        self.pc.create_identity()
        stored = password_path(self.pc.key).read_bytes()
        password = key_password(self.pc.key)
        dpapi = (password not in stored) if os.name == "nt" else True
        ok = (is_protected(self.pc.key.read_bytes()) and len(password) == 32 and dpapi
              and self.no_plain_key(self.pc.dir, plaintext_pem(self.pc.key)))
        return ok, f"κρυπτογραφημένο PKCS#8, .pass {'DPAPI' if os.name == 'nt' else '0600'}"

    def s2_start_and_enroll(self):
        director = self.pc.start()
        if not self.pc.running():
            return False, "ο Manager δεν ξεκίνησε"
        mark = director.mark()
        code = self.board.enroll(self.pc.cert)
        line = director.wait_for("Νέο αίτημα", f"| Board1", since=mark, timeout=10)
        short = line.split("Νέο αίτημα")[1].split("|")[0].strip() if line else "?"
        mark = director.mark()
        director.send(f"approve {short} {code}")
        approved = director.wait_for("Εγκρίθηκε:", since=mark, timeout=10) is not None
        joined, _ = self.connect(self.pc)
        return approved and joined, f"έγκριση {approved}, σύνδεση mTLS {joined}"

    def s3_migration(self):
        self.pc.stop()
        before = fingerprint(self.pc.cert)
        plain = plaintext_pem(self.pc.key)
        # Εγκατάσταση όπως πριν από την 4.2C2: ίδιο κλειδί, ακρυπτογράφητο, χωρίς .pass.
        self.pc.key.unlink()
        password_path(self.pc.key).unlink()
        _write_secret(self.pc.key, plain)
        director = self.pc.start()
        migrated = director.seen(MIGRATED)
        protected = is_protected(self.pc.key.read_bytes()) and plaintext_pem(self.pc.key) == plain
        joined, enrolled = self.connect(self.pc)
        return (migrated and protected and fingerprint(self.pc.cert) == before and joined
                and not enrolled,
                f"μήνυμα {migrated}, κρυπτογραφημένο {protected}, σύνδεση χωρίς νέα εγγραφή "
                f"{joined and not enrolled}")

    def s4_missing_password(self):
        self.pc.stop()
        hidden = self.base / "pass-backup"
        shutil.move(password_path(self.pc.key), hidden)
        try:
            director = self.pc.start()
            director.proc.wait(timeout=20)
            refused = (director.proc.returncode != 0 and director.seen("backup")
                       and not director.seen("Director ακούει"))
        finally:
            self.pc.director = None
            shutil.move(hidden, password_path(self.pc.key))
        return refused, "ο Manager αρνείται με οδηγία για restore από backup"

    def s5_wrong_password(self):
        stored_path = password_path(self.pc.key)
        original = stored_path.read_bytes()
        stored_path.write_bytes(bytes(b ^ 0x5A for b in original))  # σαν άλλος χρήστης/PC
        try:
            director = self.pc.start()
            director.proc.wait(timeout=20)
            refused = director.proc.returncode != 0 and director.seen("δεν ανοίγει")
        finally:
            self.pc.director = None
            stored_path.write_bytes(original)
        return refused, "αλλοιωμένο .pass → σαφές σφάλμα"

    def s6_backup_restore(self):
        self.pc.start()
        self.backup.parent.mkdir()
        rc1, _ = self.pc.backup("create", "--out", str(self.backup), "--media-copy", "no")
        self.pc.stop()
        rc2, _ = self.new.backup("restore", str(self.backup))
        secret = plaintext_pem(self.pc.key)
        ok = (rc1 == 0 and rc2 == 0 and is_protected(self.new.key.read_bytes())
              and plaintext_pem(self.new.key) == secret
              and password_path(self.new.key).read_bytes() != password_path(self.pc.key).read_bytes()
              and b"PRIVATE KEY" not in self.backup.read_bytes()
              and self.no_plain_key(self.new.dir, secret))
        return ok, f"create rc {rc1}, restore rc {rc2}, νέος κωδικός, χωρίς ξεκλείδωτο κλειδί"

    def s7_new_computer_connects(self):
        # 4.2D1: μετά από restore Εφεδρικός χωρίς εξυπηρέτηση· ο χρήστης τον κάνει Κύριο.
        promoted = self.new.promote()
        self.new.start()
        if not self.new.running():
            return False, "ο Manager του pc-new δεν ξεκίνησε"
        joined, enrolled = self.connect(self.new)
        self.new.stop()
        return (promoted and joined and not enrolled,
                f"έγινε Κύριος {promoted}, σύνδεση {joined}, νέα εγγραφή {enrolled}")

    def s8_offline_cli(self):
        done = subprocess.run([sys.executable, "-m", "director.main", *self.new.paths(),
                               "--list-clients"], cwd=Path(__file__).resolve().parents[1],
                              env=child_env(), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
        return (done.returncode == 0 and (self.board.client_id or "?") in done.stdout,
                f"rc {done.returncode}")

    def run(self) -> int:
        steps = [
            ("Νέα ταυτότητα: κλειδί κρυπτογραφημένο στον δίσκο", self.s1_encrypted),
            ("Manager ξεκινά, εγγραφή και σύνδεση Client (mTLS)", self.s2_start_and_enroll),
            ("Παλιό ακρυπτογράφητο κλειδί μετατρέπεται, ίδιο αποτύπωμα", self.s3_migration),
            ("Λείπει το .pass: ο Manager αρνείται με οδηγία restore", self.s4_missing_password),
            ("Λάθος .pass (άλλος χρήστης/PC): σαφές σφάλμα", self.s5_wrong_password),
            ("Backup/restore: νέο κλείδωμα για τον νέο υπολογιστή", self.s6_backup_restore),
            ("Νέος υπολογιστής: ο Client συνδέεται χωρίς νέα εγγραφή", self.s7_new_computer_connects),
            ("Offline εντολή --list-clients με κρυπτογραφημένο κλειδί", self.s8_offline_cli),
        ]
        try:
            for name, action in steps:
                self.step(name, action)
        finally:
            self.board.stop()
            self.pc.stop()
            self.new.stop()
        passed = sum(ok for _, ok in self.results)
        print("\n" + "\n".join(f"{'PASS' if ok else 'FAIL'} {i:02d}: {name}"
                               for i, (name, ok) in enumerate(self.results, 1)), flush=True)
        print(f"ΣΥΝΟΛΟ: {passed}/{TOTAL} PASS", flush=True)
        return 0 if passed == TOTAL else 1


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="sch-key-test-"))
    print(f"Φάκελος δοκιμής (σβήνεται στο τέλος): {base}", flush=True)
    try:
        return KeyTest(base).run()
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
