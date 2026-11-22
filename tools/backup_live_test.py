"""Self-contained 4.2C backup smoke test. Never touches a real school profile.

These 18 checks exercise the real backup CLI and file persistence. They do NOT
replace the separate Windows Manager/Client live network/audio acceptance test.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from director.backup import (BackupError, ensure_no_interrupted_restore,
                             inspect, manager_lock, restore, rollback)
from director.tls_identity import create_director_identity
from shared.client_registry import SignedClientRegistry
from shared.device_identity import create_device_identity

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "ΜόνοΔοκιμαστικόςΚωδικός2026!"
ENV_NAME = "SCH_TEST_BACKUP_PASSPHRASE"


def command(*args: str):
    env = dict(os.environ)
    env[ENV_NAME] = PASSWORD
    return subprocess.run([sys.executable, "-m", "director.backup", *args,
                           "--passphrase-env", ENV_NAME],
                          cwd=ROOT, env=env, encoding="utf-8",
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)


class Checks:
    def __init__(self):
        self.total = 0
        self.passed = 0
        self.details = []

    def check(self, name: str, action):
        self.total += 1
        try:
            outcome = action()
            if not outcome:
                raise AssertionError("Η συνθήκη δεν επιβεβαιώθηκε")
            self.passed += 1
            print(f"PASS {self.total:02d}: {name}", flush=True)
        except Exception as exc:
            print(f"FAIL {self.total:02d}: {name} ({type(exc).__name__}: {exc})", flush=True)
        return self.passed == self.total

    def summary(self):
        print(f"ΣΥΝΟΛΟ {self.passed}/{self.total} PASS", flush=True)
        return 0 if self.passed == self.total else 1


def main() -> int:
    checks = Checks()
    try:
        with tempfile.TemporaryDirectory(prefix="sch-intercom-backup-test-") as work:
            base = Path(work)
            old = base / "pc-old"
            new = base / "pc-new"
            old.mkdir()
            new.mkdir()
            cert = old / "tls" / "director-tls.crt"
            key = old / "tls" / "director-tls.key"
            cert.parent.mkdir()
            registry_path = old / "clients-registry.json"
            backup = base / "portable.sib"
            media = old / "media"
            media.mkdir()
            state = {}

            def init_identity():
                state["fingerprint"] = create_director_identity(cert, key)
                state["registry"] = SignedClientRegistry(registry_path, cert, key)
                return cert.is_file() and key.is_file()

            checks.check("Δοκιμαστική ταυτότητα Manager", init_identity)
            checks.check("Υπογεγραμμένο μητρώο Clients",
                         lambda: registry_path.exists() and state["registry"].version == 1)

            def client_a():
                identifier = str(uuid.uuid4())
                public, _, _ = create_device_identity(base / "client-A", identifier)
                state["registry"].approve(identifier, "C2627-Γ1", public.read_bytes())
                state["client_a"] = identifier
                return state["registry"].version == 2

            checks.check("Έγκριση Client A", client_a)

            def client_b():
                identifier = str(uuid.uuid4())
                public, _, _ = create_device_identity(base / "client-B", identifier)
                state["registry"].approve(identifier, "C2627-Γ2", public.read_bytes())
                state["registry"].revoke(identifier)
                state["client_b"] = identifier
                return state["registry"].version == 4

            checks.check("Ανάκληση Client B", client_b)

            def pending():
                (old / "enrollment-pending.json").write_text('{"requests":[{"id":"dummy"}]}', encoding="utf-8")
                (old / "clients-last-seen.json").write_text('{"version":1}', encoding="utf-8")
                (old / "director-gui.json").write_text('{"width":440}', encoding="utf-8")
                (old / "director.id").write_text("development-only", encoding="utf-8")
                return all((old / n).exists() for n in ("director.id", "director-gui.json",
                                      "clients-last-seen.json", "enrollment-pending.json"))

            checks.check("Εκκρεμές αίτημα και προαιρετικά δεδομένα", pending)
            checks.check("Δοκιμαστικός φάκελος MP3",
                         lambda: all((media / n).write_bytes(b"demo-" + n.encode()) > 0
                                     for n in ("start.mp3", "end.mp3")))
            common = ["--registry-file", str(registry_path), "--tls-cert", str(cert),
                      "--tls-key", str(key), "--media-dir", str(media)]
            def backup_create():
                state["create_result"] = command("create", "--out", str(backup),
                                                  "--media-copy", "yes", *common)
                return state["create_result"].returncode == 0 and backup.is_file()
            checks.check("CLI create ενώ η ταυτότητα Manager είναι διαθέσιμη", backup_create)
            checks.check("Εξωτερικά αντίγραφα MP3",
                         lambda: (base / "portable-media" / "start.mp3").exists() and
                                 (base / "portable-media" / "end.mp3").exists())
            checks.check("Inspect κεφαλίδας χωρίς κωδικό",
                         lambda: command("inspect", str(backup)).returncode == 0)
            def inspect_full():
                result = command("inspect", str(backup), "--decrypt")
                state["inspect"] = result
                return result.returncode == 0 and '"clients": 2' in result.stdout and '"mp3": 2' in result.stdout
            checks.check("Inspect κρυπτογραφημένου manifest", inspect_full)
            checks.check("Δεν αποκαλύπτεται ιδιωτικό κλειδί",
                         lambda: "PRIVATE KEY" not in state["inspect"].stdout and
                                 "CERTIFICATE" not in state["inspect"].stdout)
            new_cert = new / "tls" / "director-tls.crt"
            new_key = new / "tls" / "director-tls.key"
            new_registry = new / "clients-registry.json"
            new_common = ["--registry-file", str(new_registry), "--tls-cert", str(new_cert),
                          "--tls-key", str(new_key), "--media-dir", str(new / "media")]
            def wrong():
                env = dict(os.environ)
                env[ENV_NAME] = "wrong"
                result = subprocess.run([sys.executable, "-m", "director.backup", "restore",
                                         str(backup), *new_common,
                                         "--passphrase-env", ENV_NAME],
                                        cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, encoding="utf-8", timeout=90)
                return result.returncode == 3 and not new_cert.exists()
            checks.check("Λάθος κωδικός → απόρριψη χωρίς εγγραφή", wrong)
            def restore_first():
                output = command("restore", str(backup), *new_common)
                return output.returncode == 0 and new_cert.read_bytes() == cert.read_bytes()
            checks.check("Επαναφορά σε δεύτερο προσωρινό Manager", restore_first)
            def identity_check():
                reg = SignedClientRegistry(new_registry, new_cert, new_key)
                return (reg.version == 4 and
                        {c["client_id"]: c["status"] for c in reg.snapshot()["clients"]} ==
                        {state["client_a"]: "approved", state["client_b"]: "revoked"})
            checks.check("Ταυτότητα, εγκρίσεις και ανακλήσεις διατηρούνται", identity_check)
            checks.check("Εκκρεμές αίτημα διατηρήθηκε",
                         lambda: new_registry.with_name("enrollment-pending.json").exists())
            checks.check("Restore MP3 μόνο αν λείπουν",
                         lambda: command("restore", str(backup), "--restore-media",
                                         *new_common).returncode == 0 and
                                 (new / "media" / "start.mp3").exists())
            def lock_check():
                with manager_lock(new):
                    try:
                        restore(backup, PASSWORD, registry_file=new_registry,
                                tls_cert=new_cert, tls_key=new_key, media_dir=new / "media")
                    except BackupError as exc:
                        return exc.status == 4
                return False
            checks.check("Κλείδωμα Manager αποτρέπει restore", lock_check)
            def recovery():
                try:
                    restore(backup, PASSWORD, registry_file=new_registry,
                            tls_cert=new_cert, tls_key=new_key, media_dir=new / "media",
                            fail_after=1)
                except KeyboardInterrupt:
                    pass
                if not (new / "restore-journal.json").exists():
                    return False
                try:
                    ensure_no_interrupted_restore(new)
                except BackupError:
                    pass
                else:
                    return False
                return rollback(new) and not rollback(new) and new_cert.exists()
            checks.check("Διακοπή restore, άρνηση εκκίνησης και idempotent rollback", recovery)
    except Exception as exc:
        print(f"FAIL: μη αναμενόμενο σφάλμα αρχικοποίησης ({exc})")
    return checks.summary()


if __name__ == "__main__":
    raise SystemExit(main())
