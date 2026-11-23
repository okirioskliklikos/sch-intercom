"""4.2D1: 20 πραγματικοί έλεγχοι Κύριου/Εφεδρικού Manager, μόνο loopback.

Αυτοτελής δοκιμή χωρίς δεδομένα σχολείου: τρεις προσωρινοί Managers,
δύο Clients και ξεχωριστές TCP/UDP θύρες 515xx. Ο πραγματικός ήχος
ελέγχεται μόνο όταν το εκτελέσει ο χρήστης με συσκευές ήχου.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from director.registry_sync import SyncError, examine  # noqa: E402
from shared.client_registry import SignedClientRegistry, digest  # noqa: E402
from tools.backup_live_test import (Device as BaseDevice, Machine as BaseMachine,
                                    sha256)  # noqa: E402
from tools.mtls_live_test import JOINED, Process, child_env  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TOTAL = 20
ROOM = "C2627-Γ1"
PASSWORD_ENV = "SCH_ROLE_TEST_PASSPHRASE"
PASSWORD = os.environ.get(PASSWORD_ENV) or secrets.token_urlsafe(24)


def reg_hash(file: Path) -> str:
    return digest(json.loads(file.read_text(encoding="utf-8"))["registry"])


def check_port(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


class TestMachine(BaseMachine):
    def __init__(self, base: Path, name: str, port: int, discovery: int,
                 enrollment: int, watch: int, extra: int):
        super().__init__(base, name)
        self.port, self.discovery = port, discovery
        self.enrollment, self.watch, self.extra = enrollment, watch, extra

    def start(self) -> Process:
        self.director = Process([
            "-m", "director.main", *self.paths(), "--host", "127.0.0.1",
            "--port", str(self.port), "--discovery-port", str(self.discovery),
            "--enrollment-port", str(self.enrollment),
            "--director-id-file", str(self.dir / "director.id"),
            "--watch-port", str(self.watch),
            "--extra-beacon-port", str(self.extra),
            "--start-check-seconds", "3"],
            label=f"Manager {self.dir.name}")
        if self.director.wait_for("[Manager] Ρόλος:", timeout=30) is None:
            raise RuntimeError(f"Δεν εκκίνησε ο {self.dir.name}")
        return self.director

    def command(self, command: str, pattern: str, *, timeout: int = 10) -> bool:
        if self.director is None:
            raise RuntimeError("Manager is not running")
        marker = self.director.mark()
        self.director.send(command)
        return self.director.wait_for(pattern, since=marker, timeout=timeout) is not None

    def offline(self, module: str, *args: str) -> tuple[int, str]:
        result = subprocess.run(
            [sys.executable, "-m", module, *args, "--registry-file",
             str(self.registry), "--tls-cert", str(self.cert),
             "--tls-key", str(self.key)],
            cwd=ROOT, env=child_env(), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=90)
        content = (result.stdout + result.stderr).strip()
        for line in content.splitlines():
            print(f"    [offline {self.dir.name}] {line}", flush=True)
        return result.returncode, content

    def backup(self, *args: str, **kwargs):
        return super().backup(*args, password=PASSWORD, **kwargs)


class TestDevice(BaseDevice):
    def __init__(self, base: Path, name: str):
        super().__init__(base, name)
        self.machine: TestMachine | None = None

    def use(self, machine: TestMachine):
        self.machine = machine
        return self

    def args(self, trust: Path) -> list[str]:
        args = super().args(trust)
        if self.machine is not None:
            for option, value in (("--port", self.machine.port),
                                  ("--enrollment-port", self.machine.enrollment)):
                args[args.index(option) + 1] = str(value)
        return args


class LiveTest:
    def __init__(self, root: Path):
        self.root = root
        self.a = TestMachine(root, "pc-a", 51555, 51556, 51559, 51557, 51567)
        self.b = TestMachine(root, "pc-b", 51565, 51566, 51569, 51567, 51557)
        self.c = TestMachine(root, "pc-c", 51575, 51576, 51579, 51577, 51587)
        self.one, self.two = TestDevice(root, "Board1"), TestDevice(root, "Board2")
        self.file = root / "export-b.sicr"
        self.old_a = root / "export-a-old.sicr"
        self.a_conflict = root / "export-a-conflict.sicr"
        self.b_conflict = root / "export-b-conflict.sicr"
        self.merged = root / "export-b-merged.sicr"
        self.backup_file = root / "transfer.sicb"
        self.results = []

    def step(self, title, function):
        try:
            answer = function()
            ok, detail = answer if isinstance(answer, tuple) else (bool(answer), "")
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        self.results.append((title, ok, detail))
        print(f"\n{'PASS' if ok else 'FAIL'} {len(self.results):02d}/{TOTAL}: "
              f"{title}" + (f" — {detail}" if detail else ""), flush=True)

    def cli(self, machine: TestMachine, command: str, fragment: str) -> bool:
        return machine.command(command, fragment)

    def approve(self, machine: TestMachine, device: TestDevice, code: str | None,
                mark: int) -> bool:
        director = machine.director
        line = director.wait_for("Νέο αίτημα", f"| {ROOM} | {device.name}",
                                 since=mark, timeout=10)
        if line is None or not code:
            return False
        short = line.split("Νέο αίτημα")[1].split("|")[0].strip()
        mark = director.mark()
        director.send(f"approve {short} {code}")
        return director.wait_for("Εγκρίθηκε:", device.client_id, since=mark,
                                 timeout=10) is not None

    def client_audio(self, machine: TestMachine, device: TestDevice):
        director = machine.director
        marker = director.mark()
        device.use(machine)
        client = device.start(machine.cert)
        joined = director.wait_for(device.client_id, JOINED, since=marker, timeout=40)
        marker = client.mark()
        director.send("all")
        line = client.wait_for("κατάσταση: ανακοίνωση | blocks", since=marker, timeout=25)
        director.send("none")
        blocks = int(re.search(r"blocks (\d+)", line).group(1)) if line else 0
        return joined is not None and blocks > 0, f"σύνδεση {joined is not None}, blocks {blocks}"

    def s01(self):
        self.a.create_identity()
        proc = self.a.start()
        return (proc.seen("[Manager] Ρόλος: Κύριος") and
                proc.seen("[Manager] Εξυπηρέτηση: ενεργή") and
                check_port(self.a.port) and check_port(self.a.enrollment))

    def s02(self):
        marker = self.a.director.mark()
        device = self.one.use(self.a)
        code = device.enroll(self.a.cert)
        approved = self.approve(self.a, device, code, marker)
        heard, detail = self.client_audio(self.a, device)
        return approved and heard, detail

    def s03(self):
        rc, _ = self.a.backup("create", "--out", str(self.backup_file),
                              "--media-copy", "no")
        return rc == 0 and self.backup_file.exists() and self.a.running(), f"rc={rc}"

    def s04(self):
        rc, _ = self.b.backup("restore", str(self.backup_file))
        file = self.b.dir / "manager-state.json"
        state = json.loads(file.read_text(encoding="utf-8")) if file.exists() else {}
        return (rc == 0 and state.get("role") == "backup" and
                state.get("serving") is False and
                state.get("notice") == "restored_as_backup"), f"rc={rc}"

    def s05(self):
        proc = self.b.start()
        time.sleep(2)
        other = self.b.command("status", "Άλλος Manager:")
        return (proc.seen("[Manager] Ρόλος: Εφεδρικός") and other and
                not check_port(self.b.port) and not check_port(self.b.enrollment))

    def s06(self):
        previous = self.b.registry.read_bytes()
        p1 = self.b.command(f"revoke {self.one.client_id}", "Εφεδρικός")
        p2 = self.b.command(f"set-room {self.one.client_id} Z1", "Εφεδρικός")
        rc1, _ = self.b.offline(
            "director.main", "--approve-client-cert",
            str(self.one.dir / "credentials" / "client-cert.pem"),
            "--approve-room", ROOM)
        rc2, _ = self.b.offline("director.main",
                               "--revoke-client", str(self.one.client_id))
        return p1 and p2 and rc1 == 6 and rc2 == 6 and self.b.registry.read_bytes() == previous, (
            f"CLI αρνήσεις={p1}/{p2}, offline revoke={rc2}, approve={rc1}")

    def s07(self):
        marker = self.b.director.mark()
        self.b.director.send("serve on")
        denied = self.b.director.wait_for("εξυπηρετεί ήδη", since=marker, timeout=10)
        return denied is not None and not check_port(self.b.port)

    def s08(self):
        marker_a, marker_b = self.a.director.mark(), self.b.director.mark()
        forced = self.b.command("serve on force", "Εξυπηρέτηση: ενεργή")
        caution_a = self.a.director.wait_for("ΠΡΟΣΟΧΗ: εξυπηρετεί και",
                                            since=marker_a, timeout=10)
        caution_b = self.b.director.wait_for("ΠΡΟΣΟΧΗ: εξυπηρετεί και",
                                            since=marker_b, timeout=10)
        self.b.command("serve off", "Εξυπηρέτηση: σταματημένη")
        return (forced and caution_a is not None and caution_b is not None and
                not check_port(self.b.port)), (
                    f"force={forced}, προειδοποίηση A={bool(caution_a)}, B={bool(caution_b)}")

    def s09(self):
        self.one.stop()
        stopped = self.a.command("serve off", "Εξυπηρέτηση: σταματημένη")
        time.sleep(6)
        began = self.b.command("serve on", "Εξυπηρέτηση: ενεργή")
        audio, detail = self.client_audio(self.b, self.one)
        return stopped and began and audio, detail

    def s10(self):
        promoted = self.b.command("role primary confirm", "Ρόλος: Κύριος")
        opened = check_port(self.b.enrollment)
        marker = self.b.director.mark()
        code = self.two.use(self.b).enroll(self.b.cert)
        approved = self.approve(self.b, self.two, code, marker)
        state = json.loads((self.b.dir / "manager-state.json").read_text())
        return promoted and opened and approved and state.get("notice") is None

    def s11(self):
        status = self.b.command("status", "Αλλαγές που δεν έχουν εξαχθεί: 1")
        exported = self.b.command(f"registry export {self.file}", "Εξαγωγή:")
        after = self.b.command("status", "Αλλαγές που δεν έχουν εξαχθεί: 0")
        return status and exported and after and self.file.is_file()

    def s12(self):
        backup_role = self.a.command("role backup confirm", "Ρόλος: Εφεδρικός")
        old_export = self.a.command(f"registry export {self.old_a}", "Εξαγωγή:")
        imported = self.a.command(f"registry import {self.file} confirm",
                                  "[Manager] Εισαγωγή: newer")
        return (backup_role and old_export and imported and
                reg_hash(self.a.registry) == reg_hash(self.b.registry))

    def s13(self):
        unchanged = self.a.command(f"registry import {self.file}",
                                   "ήδη ενημερωμένο")
        self.a.stop()
        rc, _ = self.a.offline("director.registry_sync", "import",
                               str(self.old_a), "--yes")
        self.a.start()
        return unchanged and rc == 5, f"παλαιότερο rc={rc}"

    def s14(self):
        promoted = self.a.command("role primary confirm", "Ρόλος: Κύριος")
        revoke = self.a.command(f"revoke {self.one.client_id}", "Ανακλήθηκε")
        changed = self.b.command(f"set-room {self.two.client_id} C2627-Γ2",
                                 "Ενημερώθηκε αίθουσα")
        exported_a = self.a.command(f"registry export {self.a_conflict}", "Εξαγωγή:")
        exported_b = self.b.command(f"registry export {self.b_conflict}", "Εξαγωγή:")
        return promoted and revoke and changed and exported_a and exported_b

    def s15(self):
        before = self.b.registry.read_bytes()
        try:
            examine(self.b.registry, self.b.cert, self.a_conflict)
        except SyncError as exc:
            expected = exc.status == 5
        else:
            expected = False
        denied = self.b.command(f"registry import {self.a_conflict}", "χρειάζεται --merge")
        return expected and denied and self.b.registry.read_bytes() == before

    def s16(self):
        before = json.loads(self.b.registry.read_text())["registry_version"]
        imported = self.b.command(
            f"registry import {self.a_conflict} merge confirm",
            "[Manager] Εισαγωγή: conflict")
        reg = json.loads(self.b.registry.read_text())["registry"]
        entries = {entry["client_id"]: entry for entry in reg["clients"]}
        c1 = entries[self.one.client_id]["status"] == "revoked"
        c2 = entries[self.two.client_id]["room"] == "C2627-Γ2"
        greater = reg["registry_version"] > before
        marker = self.b.director.mark()
        self.one.use(self.b).start(self.b.cert)
        rejection = self.b.director.wait_for("CERTIFICATE_VERIFY_FAILED",
                                             since=marker, timeout=30)
        self.one.stop()
        return imported and c1 and c2 and greater and rejection is not None, (
            f"ανακλήθηκε={c1}, νέα αίθουσα={c2}, νέα έκδοση={greater}, "
            f"TLS άρνηση={rejection is not None}")

    def s17(self):
        exported = self.b.command(f"registry export {self.merged}", "Εξαγωγή:")
        imported = self.a.command(f"registry import {self.merged} confirm",
                                  "[Manager] Εισαγωγή: newer")
        return exported and imported and reg_hash(self.a.registry) == reg_hash(self.b.registry)

    def s18(self):
        self.two.stop()
        self.b.stop()
        restarted = self.b.start()
        primary = restarted.seen("[Manager] Ρόλος: Κύριος")
        serving = restarted.seen("[Manager] Εξυπηρέτηση: ενεργή")
        off = self.b.command("serve off", "Εξυπηρέτηση: σταματημένη")
        self.b.stop()
        restart_off = self.b.start()
        stopped = (restart_off.seen("[Manager] Εξυπηρέτηση: σταματημένη") and
                   not check_port(self.b.port))
        return primary and serving and off and stopped

    def s19(self):
        before = self.b.registry.read_bytes()
        damaged = self.root / "damaged.sicr"
        data = bytearray(self.merged.read_bytes())
        data[-8] ^= 1
        damaged.write_bytes(data)
        tampered = self.b.command(f"registry import {damaged}", "αλλοιωμένο")
        self.c.create_identity()
        SignedClientRegistry(self.c.registry, self.c.cert, self.c.key)
        other_file = self.root / "other-manager.sicr"
        self.c.offline("director.registry_sync", "export", "--out", str(other_file))
        foreign = self.b.command(f"registry import {other_file}", "άλλον Manager")
        return tampered and foreign and self.b.registry.read_bytes() == before

    def s20(self):
        changed = self.a.command(f"set-room {self.two.client_id} C2627-Γ3",
                                 "Ενημερώθηκε αίθουσα")
        newfile = self.root / "newer-a.sicr"
        exported = self.a.command(f"registry export {newfile}", "Εξαγωγή:")
        rc_import, _ = self.b.offline("director.registry_sync", "import",
                                      str(newfile), "--yes")
        rc_status, _ = self.b.offline("director.registry_sync", "status")
        return changed and exported and rc_import == 4 and rc_status == 0, (
            f"offline import={rc_import}, status={rc_status}")

    def close(self):
        for device in (self.one, self.two):
            device.stop()
        for machine in (self.a, self.b, self.c):
            machine.stop()

    def run(self) -> int:
        actions = [
            ("Νέα εγκατάσταση A: Κύριος, εξυπηρέτηση ενεργή", self.s01),
            ("Εγγραφή Client 1 και λήψη ήχου στον A", self.s02),
            ("Backup A με ενεργό Manager", self.s03),
            ("Restore B: Εφεδρικός, σταματημένος, ειδοποίηση", self.s04),
            ("B χωρίς θύρες, βλέπει τον ενεργό A", self.s05),
            ("B αρνείται έγκριση/ανάκληση/αλλαγή αίθουσας", self.s06),
            ("B serve on αρνείται ενεργό A", self.s07),
            ("B serve on force, προειδοποιήσεις και serve off", self.s08),
            ("A off, B on, Client 1 συνδέεται και λαμβάνει ήχο", self.s09),
            ("B γίνεται Κύριος, ανοίγει εγγραφή Client 2", self.s10),
            ("Μία μη εξαγμένη αλλαγή, export .sicr και μετρητής 0", self.s11),
            ("A Εφεδρικός, εισάγει νεότερο μητρώο του B", self.s12),
            ("Ίδια εισαγωγή no-op, παλιότερο αρχείο rc 5", self.s13),
            ("Δύο αποκλίνοντα μητρώα και εξαγωγές", self.s14),
            ("Εισαγωγή conflict χωρίς merge αρνείται", self.s15),
            ("Συγχώνευση, ανάκληση TLS και διατήρηση νέας αίθουσας", self.s16),
            ("Εισαγωγή συγχωνευμένου στον A και ίδιο digest", self.s17),
            ("Επανεκκινήσεις B διατηρούν ρόλο και κατάσταση", self.s18),
            ("Αλλοιωμένο αρχείο και ξένος Manager απορρίπτονται", self.s19),
            ("Offline import με ενεργό B rc 4, status rc 0", self.s20),
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
    root = Path(tempfile.mkdtemp(prefix="sch-role-test-"))
    print(f"Προσωρινή δοκιμή μόνο σε loopback: {root}", flush=True)
    try:
        return LiveTest(root).run()
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
