"""Αυτόματη πραγματική δοκιμή 4.2C (docs/phase4-2c-backup.md §11): 18 βήματα PASS/FAIL.

Τρέχει στον υπολογιστή του Manager (κύριο σενάριο: Windows) τα πραγματικά
``director.main``, ``client.main`` και ``director.backup`` ως διεργασίες:
backup με Manager σε λειτουργία, επαναφορά σε «νέο υπολογιστή», σύνδεση του ίδιου
Client χωρίς νέα εγγραφή, λήψη ήχου ανακοίνωσης, απόρριψη ανακλημένου, κλείδωμα,
διακοπή στη μέση και rollback, αντικατάσταση άλλης ταυτότητας.

Όλα γίνονται σε προσωρινό φάκελο (``%TEMP%\\sch-backup-test-*``), μόνο σε loopback
127.0.0.1 και σε θύρες 51555/51556/51559, ώστε να μην αγγίζεται ούτε να συγκρούεται
με πραγματική εγκατάσταση (``%USERPROFILE%\\.school-intercom``). Ο φάκελος σβήνεται στο τέλος.

Το βήμα 13 χρειάζεται μικρόφωνο στον Manager και κάρτα ήχου για τον Client.

    Windows:  powershell -ExecutionPolicy Bypass -File tools\\backup_live_test.ps1
    Linux:    tools/backup_live_test.sh
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.mtls_live_test import JOINED, Process, child_env  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PORT, DISCOVERY_PORT, ENROLL_PORT = 51555, 51556, 51559
HOST = "127.0.0.1"
ROOM = "C2627-Γ1"
PASSWORD = "ΜόνοΓιαΔοκιμή-2026-backup"
PASSWORD_ENV = "SCH_BACKUP_LIVE_PASSPHRASE"
TOTAL = 18


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint(cert: Path) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    return x509.load_pem_x509_certificate(cert.read_bytes()).fingerprint(
        hashes.SHA256()).hex(":").upper()


class Machine:
    """Ένας «υπολογιστής» Manager: φάκελος δεδομένων όπως ο %USERPROFILE%\\.school-intercom."""

    def __init__(self, base: Path, name: str):
        self.dir = base / name
        self.dir.mkdir(parents=True)
        self.cert = self.dir / "tls" / "director-tls.crt"
        self.key = self.dir / "tls" / "director-tls.key"
        self.registry = self.dir / "clients-registry.json"
        self.media = self.dir / "media"
        self.director: Process | None = None

    def paths(self) -> list[str]:
        return ["--registry-file", str(self.registry), "--tls-cert", str(self.cert),
                "--tls-key", str(self.key)]

    def create_identity(self) -> bool:
        self.cert.parent.mkdir(parents=True, exist_ok=True)
        done = subprocess.run([sys.executable, "-m", "director.tls_identity",
                               "--cert", str(self.cert), "--key", str(self.key)],
                              cwd=ROOT, env=child_env(), capture_output=True, timeout=60)
        return done.returncode == 0 and self.cert.is_file() and self.key.is_file()

    def start(self) -> Process:
        self.director = Process(
            ["-m", "director.main", *self.paths(), "--host", HOST, "--port", str(PORT),
             "--discovery-port", str(DISCOVERY_PORT), "--enrollment-port", str(ENROLL_PORT),
             "--director-id-file", str(self.dir / "director.id")],
            label=f"Manager {self.dir.name}")
        self.director.wait_for("Director ακούει", timeout=20)
        return self.director

    def running(self) -> bool:
        return self.director is not None and self.director.proc.poll() is None

    def stop(self) -> None:
        if self.director is not None:
            self.director.stop(quit_command="quit")
            self.director = None

    def backup(self, *args: str, password: str = PASSWORD,
               extra_env: dict | None = None) -> tuple[int, str]:
        env = child_env()
        env[PASSWORD_ENV] = password
        env.update(extra_env or {})
        done = subprocess.run(
            [sys.executable, "-m", "director.backup", *self.paths(),
             "--media-dir", str(self.media), "--passphrase-env", PASSWORD_ENV, *args],
            cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=180)
        output = (done.stdout + done.stderr).strip()
        for line in output.splitlines():
            print(f"    [backup {self.dir.name}] {line}", flush=True)
        return done.returncode, done.stdout


class Device:
    """Ένας Classroom Client με δική του ταυτότητα (UUID, κλειδί, hints)."""

    def __init__(self, base: Path, name: str):
        self.name = name
        self.dir = base / f"client-{name}"
        self.client_id: str | None = None
        self.process: Process | None = None

    def args(self, trust: Path) -> list[str]:
        return ["--tls-ca", str(trust), "--identity-file", str(self.dir / "client.json"),
                "--client-credentials-dir", str(self.dir / "credentials"),
                "--client-hints-file", str(self.dir / "client-hints.json"),
                "--host", HOST, "--port", str(PORT), "--enrollment-port", str(ENROLL_PORT)]

    def enroll(self, trust: Path) -> str | None:
        """Εγγραφή χωρίς αναμονή: ο Client δείχνει τον κωδικό και κλείνει μόνος του."""
        process = Process(["-m", "client.main", *self.args(trust), "--enroll",
                           "--room", ROOM, "--computer-name", self.name],
                          label=f"Εγγραφή {self.name}")
        try:
            process.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.stop()
        line = process.wait_for("Κωδικός:", timeout=1)
        self.client_id = json.loads((self.dir / "client.json").read_text(
            encoding="utf-8"))["client_id"]
        return line.split("Κωδικός:")[1].split("|")[0].strip() if line else None

    def start(self, trust: Path) -> Process:
        self.stop()
        self.process = Process(["-m", "client.main", *self.args(trust), "--room", ROOM],
                               label=f"Client {self.name}")
        self.process.wait_for("Client ID:", timeout=30)
        return self.process

    def stop(self) -> None:
        if self.process is not None:
            self.process.stop()
            self.process = None


class LiveTest:
    def __init__(self, base: Path):
        self.base = base
        self.results: list[tuple[str, bool, str]] = []
        self.old = Machine(base, "pc-old")
        self.new = Machine(base, "pc-new")
        self.a, self.b, self.c = (Device(base, n) for n in ("Board1", "Board2", "Board3"))
        self.usb = base / "usb"
        self.usb.mkdir()
        self.file = self.usb / "sch-intercom-backup-test.sib"
        self.c_short: str | None = None
        self.c_code: str | None = None
        self.inspected: dict = {}

    # -------------------------------------------------------------- βοηθητικά
    def result(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, ok, detail))
        number = len(self.results)
        text = f"{'PASS' if ok else 'FAIL'} {number:02d}/{TOTAL}: {name}"
        print(f"\n=== {text}" + (f" — {detail}" if detail else "") + "\n", flush=True)

    def step(self, name: str, action) -> None:
        try:
            outcome = action()
            ok, detail = outcome if isinstance(outcome, tuple) else (bool(outcome), "")
        except Exception as exc:  # η δοκιμή αναφέρει, δεν κρύβει
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        self.result(name, ok, detail)

    @staticmethod
    def approve(director: Process, device: Device, code: str | None, since: int) -> str | None:
        """Όπως ο διαχειριστής: βρίσκει το αίτημα από αίθουσα/όνομα και γράφει τον κωδικό."""
        line = director.wait_for("Νέο αίτημα", f"| {ROOM} | {device.name}", since=since,
                                 timeout=10)
        if line is None or code is None:
            return None
        short = line.split("Νέο αίτημα")[1].split("|")[0].strip()
        mark = director.mark()
        director.send(f"approve {short} {code}")
        return short if director.wait_for("Εγκρίθηκε:", device.client_id or "?", since=mark,
                                          timeout=10) else None

    def stop_all(self) -> None:
        for device in (self.a, self.b, self.c):
            device.stop()
        self.old.stop()
        self.new.stop()

    # -------------------------------------------------------------- βήματα
    def s01_identity(self):
        return self.old.create_identity(), fingerprint(self.old.cert)

    def s02_enroll_a(self):
        director = self.old.start()
        if not self.old.running():
            return False, "ο Manager δεν ξεκίνησε"
        mark = director.mark()
        code = self.a.enroll(self.old.cert)
        if self.approve(director, self.a, code, mark) is None:
            return False, "η έγκριση του Board1 απέτυχε"
        mark = director.mark()
        self.a.start(self.old.cert)
        return director.wait_for(self.a.client_id, JOINED, since=mark, timeout=40) is not None

    def s03_revoke_b_pending_c(self):
        director = self.old.director
        mark = director.mark()
        code = self.b.enroll(self.old.cert)
        approved = self.approve(director, self.b, code, mark) is not None
        mark = director.mark()
        director.send(f"revoke {self.b.client_id}")
        revoked = director.wait_for(f"Ανακλήθηκε ο Client {self.b.client_id}", since=mark,
                                    timeout=10) is not None
        mark = director.mark()
        self.c_code = self.c.enroll(self.old.cert)
        line = director.wait_for("Νέο αίτημα", f"| {ROOM} | {self.c.name}", since=mark,
                                 timeout=10)
        self.c_short = line.split("Νέο αίτημα")[1].split("|")[0].strip() if line else None
        return (approved and revoked and self.c_short is not None,
                f"έγκριση B {approved}, ανάκληση B {revoked}, αίτημα C {self.c_short}")

    def s04_media(self):
        self.old.media.mkdir()
        for name in ("κουδούνι.mp3", "διάλειμμα.mp3"):
            (self.old.media / name).write_bytes(os.urandom(4096))
        return len(list(self.old.media.glob("*.mp3"))) == 2

    def s05_create_running(self):
        # Ο Manager γράφει την τελευταία σύνδεση λίγο μετά τη σύνδεση του Client.
        last_seen = self.old.dir / "clients-last-seen.json"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (
                last_seen.is_file() and (self.a.client_id or "?") in last_seen.read_text(
                    encoding="utf-8")):
            time.sleep(0.2)
        rc, out = self.old.backup("create", "--out", str(self.file), "--media-copy", "yes")
        copy = self.usb / (self.file.stem + "-media")
        copies_ok = all((copy / p.name).is_file() and sha256(copy / p.name) == sha256(p)
                        for p in self.old.media.glob("*.mp3"))
        return (rc == 0 and self.old.running() and self.file.is_file() and copies_ok
                and "φάκελος MP3" in out,
                f"rc {rc}, Manager σε λειτουργία {self.old.running()}, αντίγραφα MP3 {copies_ok}")

    def s06_inspect(self):
        rc1, plain = self.old.backup("inspect", str(self.file))
        rc2, full = self.old.backup("inspect", str(self.file), "--decrypt")
        summary = json.loads(full)["summary"] if rc2 == 0 else {}
        self.inspected = summary
        no_secret = all(s not in plain + full for s in ("PRIVATE KEY", "CERTIFICATE", PASSWORD))
        ok = (rc1 == 0 and "summary" not in json.loads(plain) and rc2 == 0
              and summary.get("clients") == 2 and summary.get("pending") == 1
              and summary.get("mp3") == 2
              and summary.get("tls_fingerprint") == fingerprint(self.old.cert) and no_secret)
        return ok, (f"Clients {summary.get('clients')}, αιτήματα {summary.get('pending')}, "
                    f"MP3 {summary.get('mp3')}, χωρίς μυστικά {no_secret}")

    def s07_wrong_password(self):
        rc, _ = self.new.backup("restore", str(self.file), password="ΛάθοςΚωδικός-12345")
        empty = not self.new.cert.exists() and not self.new.registry.exists()
        return rc == 3 and empty, f"rc {rc}, ο pc-new έμεινε άδειος {empty}"

    def s08_tampered(self):
        tampered = self.usb / "tampered.sib"
        data = bytearray(self.file.read_bytes())
        data[len(data) // 2] ^= 0x01
        tampered.write_bytes(bytes(data))
        rc, _ = self.new.backup("restore", str(tampered))
        return rc == 3 and not self.new.registry.exists(), f"rc {rc}"

    def s09_stop_old(self):
        self.a.stop()
        process = self.old.director
        self.old.stop()
        return process is not None and process.proc.poll() is not None

    def s10_restore(self):
        rc, out = self.new.backup("restore", str(self.file))
        media = json.loads(out)["media"] if rc == 0 else {}
        return (rc == 0 and sorted(media.get("missing", [])) == sorted(
                    p.name for p in self.old.media.glob("*.mp3")),
                f"rc {rc}, λείπουν MP3: {media.get('missing')}")

    def s11_restore_media(self):
        rc, out = self.new.backup("restore", str(self.file), "--restore-media")
        media = json.loads(out)["media"] if rc == 0 else {}
        same = all((self.new.media / p.name).is_file()
                   and sha256(self.new.media / p.name) == sha256(p)
                   for p in self.old.media.glob("*.mp3"))
        return (rc == 0 and media.get("missing") == [] and len(media.get("copied", [])) == 2
                and same, f"rc {rc}, αντιγράφηκαν {media.get('copied')}")

    def s12_same_state(self):
        registry = json.loads(self.new.registry.read_text(encoding="utf-8"))["registry"]
        pending = self.new.dir / "enrollment-pending.json"
        last_seen = self.new.dir / "clients-last-seen.json"
        checks = {
            "αποτύπωμα TLS": fingerprint(self.new.cert) == self.inspected.get("tls_fingerprint"),
            "κλειδί": self.new.key.read_bytes() == self.old.key.read_bytes(),
            "έκδοση μητρώου": registry["registry_version"] == self.inspected.get(
                "registry_version"),
            "εκκρεμές αίτημα": pending.is_file() and self.c.name in pending.read_text(
                encoding="utf-8"),
            "τελευταία σύνδεση": last_seen.is_file() and (self.a.client_id or "?") in
            last_seen.read_text(encoding="utf-8"),
        }
        detail = ", ".join(f"{k} {v}" for k, v in checks.items())
        if not checks["τελευταία σύνδεση"]:
            detail += f" | clients-last-seen.json: {last_seen.read_text(encoding='utf-8')[:300] if last_seen.is_file() else 'λείπει'}"
        return all(checks.values()), detail

    def s13_reconnect_and_audio(self):
        director = self.new.start()
        if not self.new.running():
            return False, "ο Manager του pc-new δεν ξεκίνησε"
        mark = director.mark()
        client = self.a.start(self.new.cert)
        joined = director.wait_for(self.a.client_id, JOINED, since=mark, timeout=40)
        enrolled_again = director.seen("Νέο αίτημα", since=mark)
        client_mark = client.mark()
        director.send("all")
        line = client.wait_for("κατάσταση: ανακοίνωση | blocks", since=client_mark, timeout=25)
        director.send("none")
        blocks = int(re.search(r"blocks (\d+)", line).group(1)) if line else 0
        return (joined is not None and not enrolled_again and blocks > 0,
                f"σύνδεση {joined is not None}, νέα εγγραφή {enrolled_again}, "
                f"blocks ήχου {blocks}")

    def s14_revoked_rejected(self):
        director = self.new.director
        mark = director.mark()
        self.b.start(self.new.cert)
        rejected = director.wait_for("CERTIFICATE_VERIFY_FAILED", since=mark, timeout=30)
        time.sleep(2)
        joined = director.seen(self.b.client_id, JOINED, since=mark)
        self.b.stop()
        return rejected is not None and not joined, f"απόρριψη TLS {rejected is not None}"

    def s15_approve_pending(self):
        director = self.new.director
        mark = director.mark()
        director.send(f"approve {self.c_short} {self.c_code}")
        ok = director.wait_for("Εγκρίθηκε:", self.c.client_id or "?", since=mark, timeout=10)
        return ok is not None, f"αίτημα {self.c_short}"

    def s16_locked(self):
        before = self.new.registry.read_bytes()
        rc, _ = self.new.backup("restore", str(self.file), "--accept-older")
        return (rc == 4 and self.new.running() and self.new.registry.read_bytes() == before,
                f"rc {rc}")

    def s17_interrupted_rollback(self):
        self.a.stop()
        self.new.stop()
        before = self.new.registry.read_bytes()
        rc, _ = self.new.backup("restore", str(self.file), "--accept-older",
                                extra_env={"SCH_BACKUP_TEST_FAIL_AFTER": "3"})
        journal = (self.new.dir / "restore-journal.json").exists()
        refused = self.new.start()
        refused.proc.wait(timeout=20)
        blocked = refused.seen("Η επαναφορά διακόπηκε") and not refused.seen("Director ακούει")
        self.new.director = None
        rb, _ = self.new.backup("rollback")
        restored = self.new.registry.read_bytes() == before
        director = self.new.start()
        mark = director.mark()
        self.a.start(self.new.cert)
        joined = director.wait_for(self.a.client_id, JOINED, since=mark, timeout=40)
        self.a.stop()
        self.new.stop()
        return (rc != 0 and journal and blocked and rb == 0 and restored and joined is not None,
                f"διακοπή rc {rc}, journal {journal}, άρνηση εκκίνησης {blocked}, "
                f"rollback rc {rb}, ίδιο μητρώο {restored}, σύνδεση {joined is not None}")

    def s18_replace_identity(self):
        other = Machine(self.base, "pc-other")
        other.create_identity()
        rc1, _ = other.backup("restore", str(self.file))
        rc2, _ = other.backup("restore", str(self.file), "--replace-identity")
        previous = list(other.dir.glob("pre-restore-*"))
        same = other.cert.read_bytes() == self.old.cert.read_bytes()
        return (rc1 == 5 and rc2 == 0 and bool(previous) and same,
                f"χωρίς επιβεβαίωση rc {rc1}, με --replace-identity rc {rc2}, "
                f"pre-restore {bool(previous)}")

    def run(self) -> int:
        steps = [
            ("Ταυτότητα TLS στον pc-old", self.s01_identity),
            ("Εγγραφή και σύνδεση Client A στον pc-old", self.s02_enroll_a),
            ("Client B εγκρίνεται και ανακαλείται, αίτημα C σε αναμονή", self.s03_revoke_b_pending_c),
            ("Φάκελος MP3 με 2 αρχεία", self.s04_media),
            ("create με Manager σε λειτουργία και αντίγραφο MP3", self.s05_create_running),
            ("inspect χωρίς και με κωδικό, χωρίς μυστικά", self.s06_inspect),
            ("Λάθος κωδικός: κωδικός 3, τίποτα στον pc-new", self.s07_wrong_password),
            ("Αλλοιωμένο αρχείο: κωδικός 3", self.s08_tampered),
            ("Διακοπή Manager pc-old", self.s09_stop_old),
            ("restore στον pc-new, αναφορά MP3 που λείπουν", self.s10_restore),
            ("restore --restore-media: MP3 με σωστό hash", self.s11_restore_media),
            ("Ίδια ταυτότητα, μητρώο, αίτημα, τελευταία σύνδεση", self.s12_same_state),
            ("Client A συνδέεται χωρίς νέα εγγραφή και λαμβάνει ήχο", self.s13_reconnect_and_audio),
            ("Ο ανακλημένος Client B απορρίπτεται", self.s14_revoked_rejected),
            ("Το εκκρεμές αίτημα C εγκρίνεται στον pc-new", self.s15_approve_pending),
            ("restore ενώ τρέχει ο Manager: κωδικός 4", self.s16_locked),
            ("Διακοπή restore, άρνηση εκκίνησης, rollback, σύνδεση", self.s17_interrupted_rollback),
            ("Άλλη ταυτότητα: κωδικός 5, μετά --replace-identity", self.s18_replace_identity),
        ]
        try:
            for name, action in steps:
                self.step(name, action)
        finally:
            self.stop_all()
        passed = sum(ok for _, ok, _ in self.results)
        print("\n" + "\n".join(f"{'PASS' if ok else 'FAIL'} {i:02d}: {name}"
                               for i, (name, ok, _) in enumerate(self.results, 1)), flush=True)
        print(f"ΣΥΝΟΛΟ: {passed}/{TOTAL} PASS", flush=True)
        return 0 if passed == TOTAL else 1


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="sch-backup-test-"))
    print(f"Φάκελος δοκιμής (σβήνεται στο τέλος): {base}", flush=True)
    try:
        return LiveTest(base).run()
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
