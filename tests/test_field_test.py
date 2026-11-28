"""Φάση 5: βοηθητικά της δοκιμής σε καθαρά Windows (tools/field_test.py, field_test_build.py).

Η πλήρης ροή ελέγχεται στο Linux με ``tools/field_test.sh selfcheck`` (προσομοίωση του
εγκαταστάτη) και πραγματικά μόνο στα Windows (field-test.cmd)."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
import zipfile

from shared import package as pkg
from tools import field_test as ft
from tools import field_test_build as fb

ROOT = Path(__file__).resolve().parents[1]


def small_tree(folder: Path, version: str = ft.BASE) -> Path:
    (folder / "app" / "client").mkdir(parents=True)
    (folder / "app" / "client" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (folder / "app" / "shared").mkdir()
    (folder / "app" / "shared" / "x.py").write_text("X = 1\n", encoding="utf-8")
    site = folder / "python" / "Lib" / "site-packages"
    for name in ("cryptography", "PySide6", "shiboken6", "winrt_runtime", "cffi"):
        (site / name).mkdir(parents=True)
        (site / name / "__init__.py").write_text("", encoding="utf-8")
    (folder / "python" / "python.exe").write_bytes(b"MZ")
    (folder / "VERSION").write_text(version + "\n", encoding="ascii")
    return folder


class FakeManager:
    def __init__(self, lines=()):
        self.lines = list(lines)
        self.sent = []
        self.replies = {}

    def mark(self):
        return len(self.lines)

    def send(self, line):
        self.sent.append(line)
        for prefix, reply in self.replies.items():
            if line.startswith(prefix):
                self.lines.append(reply)

    def wait_for(self, *texts, since=0, timeout=0):
        for line in self.lines[since:]:
            if all(text in line for text in texts):
                return line
        return None


class VariantAndFilesTests(unittest.TestCase):
    def test_variant_changes_only_version_and_main(self):
        with tempfile.TemporaryDirectory() as temp:
            base = small_tree(Path(temp) / "base")
            normal = ft.make_variant(base, Path(temp) / "n", ft.GOOD, None)
            crash = ft.make_variant(base, Path(temp) / "c", ft.CRASHING, ft.CRASH_MAIN)
            self.assertEqual((normal / "VERSION").read_text().strip(), ft.GOOD)
            self.assertEqual((normal / "app/client/main.py").read_text(encoding="utf-8"),
                             "print('ok')\n")
            self.assertIn("sys.exit(7)", (crash / "app/client/main.py").read_text(encoding="utf-8"))
            self.assertEqual((base / "VERSION").read_text().strip(), ft.BASE)

    def test_check_files_reports_missing_and_changed(self):
        with tempfile.TemporaryDirectory() as temp:
            tree = small_tree(Path(temp) / "t")
            out = Path(temp) / "m.json"
            ft.write_base_manifest(tree, out)
            manifest = json.loads(out.read_text(encoding="utf-8"))
            good, total, problems = ft.check_files(tree, manifest)
            self.assertEqual((good, problems), (total, []))
            (tree / "app/shared/x.py").write_text("X = 2\n", encoding="utf-8")
            (tree / "python/python.exe").unlink()
            (tree / "app/__pycache__").mkdir()                 # επιπλέον αρχεία δεν πειράζουν
            good, total, problems = ft.check_files(tree, manifest)
            self.assertEqual(good, total - 2)
            self.assertEqual(sorted(problems), ["άλλαξε app/shared/x.py", "λείπει python/python.exe"])

    def test_test_versions_never_outrank_real_releases(self):
        from shared.version import APP_VERSION, parse_version
        for version in (ft.BASE, *ft.UPDATES):
            self.assertLess(parse_version(version), parse_version(APP_VERSION))
            self.assertEqual(parse_version(version)[:2], (0, 6))


class DirectorOpsTests(unittest.TestCase):
    def service(self, temp: Path, lines=()) -> ft.FieldDirector:
        service = ft.FieldDirector(temp, show=False)
        service.manager = FakeManager(lines)
        service.packages.mkdir(parents=True)
        return service

    def test_approve_enrollment_uses_short_id_and_code(self):
        with tempfile.TemporaryDirectory() as temp:
            service = self.service(Path(temp), [
                f"[Εγγραφή] Νέο αίτημα abcd1234 | {ft.ROOM1} | {ft.PC1} | λήγει ..."])
            service.manager.replies["approve "] = f"Εγκρίθηκε: {ft.ROOM1} | {ft.PC1} | uuid-1"
            reply = service.op_approve_enrollment({"room": ft.ROOM1, "name": ft.PC1,
                                                   "code": "123456", "timeout": 0})
            self.assertEqual(service.manager.sent, ["approve abcd1234 123456"])
            self.assertTrue(reply["ok"])
            self.assertEqual(reply["client_id"], "uuid-1")
            missing = service.op_approve_enrollment({"room": "Άλλη", "name": ft.PC1,
                                                     "code": "1", "timeout": 0})
            self.assertFalse(missing["ok"])

    def test_approve_change_waits_for_exact_request(self):
        with tempfile.TemporaryDirectory() as temp:
            text = f"{ft.ROOM1} · {ft.PC1} → {ft.ROOM2} · {ft.PC2}"
            service = self.service(Path(temp), [f"[Αλλαγές] Αίτημα {text} (uuid-123456)"])
            service.manager.replies["approve-change"] = f"Εγκρίθηκε η αλλαγή: {text}"
            reply = service.op_approve_change({"client_id": "uuid-123456789", "old_room": ft.ROOM1,
                                               "old_name": ft.PC1, "room": ft.ROOM2,
                                               "name": ft.PC2, "timeout": 0})
            self.assertTrue(reply["ok"])
            self.assertEqual(service.manager.sent, ["approve-change uuid-123"])
            seen = service.op_change_seen({"since": 1, "timeout": 0})
            self.assertFalse(seen["seen"])

    def test_joined_can_require_room(self):
        with tempfile.TemporaryDirectory() as temp:
            service = self.service(Path(temp), [f"[Director] uuid-1 ({ft.ROOM1}) συνδέθηκε — σύνολο 1"])
            self.assertTrue(service.op_joined({"client_id": "uuid-1", "timeout": 0})["ok"])
            self.assertTrue(service.op_joined({"client_id": "uuid-1", "room": ft.ROOM1,
                                               "timeout": 0})["ok"])
            self.assertFalse(service.op_joined({"client_id": "uuid-1", "room": ft.ROOM2,
                                                "timeout": 0})["ok"])

    def test_upload_checks_name_size_and_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            service = self.service(Path(temp))
            data = b"x" * 3000
            digest = hashlib.sha256(data).hexdigest()
            name = ft.package_file(ft.GOOD)
            reply = service.receive_upload({"name": name, "size": len(data), "sha256": digest},
                                           io.BytesIO(data))
            self.assertTrue(reply["ok"])
            self.assertTrue(service.op_has_package({"name": name, "sha256": digest})["present"])
            bad = service.receive_upload({"name": ft.package_file(ft.FINAL), "size": 3,
                                          "sha256": "0" * 64}, io.BytesIO(b"abc"))
            self.assertFalse(bad["ok"])
            self.assertFalse((service.packages / ft.package_file(ft.FINAL)).exists())
            for wrong in ("../x.sicu", "evil.exe"):
                with self.assertRaises(ValueError):
                    service.receive_upload({"name": wrong, "size": 1, "sha256": digest},
                                           io.BytesIO(b"x"))
            self.assertFalse(service.op_import({"name": "../x.sicu"})["ok"])

    def test_channel_round_trip_and_upload_over_socket(self):
        with tempfile.TemporaryDirectory() as temp:
            service = self.service(Path(temp))
            service.cert.parent.mkdir(parents=True)
            service.cert.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            service.bind, service.port = "127.0.0.1", port
            ready = threading.Event()
            thread = threading.Thread(target=service.serve, args=(ready,), daemon=True)
            thread.start()
            ready.wait(5)
            channel = ft.Channel("127.0.0.1", port)
            hello = channel.call("hello")
            self.assertEqual(hello["protocol"], ft.FIELD_PROTOCOL)
            self.assertIn("BEGIN CERTIFICATE", hello["cert"])
            self.assertFalse(channel.call("nonsense")["ok"])
            package = Path(temp) / ft.package_file(ft.GOOD)
            package.write_bytes(b"P" * 2_500_000)
            self.assertTrue(channel.upload(package, report=lambda text: None)["ok"])
            self.assertTrue(channel.upload(package, report=lambda text: None).get("skipped"))
            self.assertTrue(channel.call("finish", report="ΣΥΝΟΛΟ")["ok"])
            channel.close()
            thread.join(5)
            self.assertEqual(service.report, "ΣΥΝΟΛΟ")


class SummaryTests(unittest.TestCase):
    def test_abort_after_install_failure_and_counts(self):
        with tempfile.TemporaryDirectory() as temp:
            class Machine:
                sim = True
                layout = ft.Layout(Path(temp) / "h", Path(temp) / "d")
                bundle = Path(temp)
            test = ft.FieldTest(Machine(), ft.Channel("127.0.0.1", 1),
                                state_file=Path(temp) / ft.STATE_FILE, host="127.0.0.1")
            test.s01 = lambda: (True, "")
            test.s02 = lambda: (False, "κόλλησε")
            self.assertFalse(test.run_steps(range(3)))
            summary = test.summary()
            self.assertIn(f"ΣΥΝΟΛΟ: 1/{ft.TOTAL} PASS, 0 SKIP, {ft.TOTAL - 1} FAIL", summary)
            self.assertIn("δεν έγινε το προηγούμενο βήμα", summary)
            saved = json.loads((Path(temp) / ft.STATE_FILE).read_text(encoding="utf-8"))
            self.assertEqual(len(saved["results"]), ft.TOTAL)

    def test_reboot_is_after_update_and_before_rollbacks(self):
        names = [name for _, name in ft.STEPS]
        self.assertEqual(names[ft.REBOOT_AFTER], "s10")
        self.assertEqual(len(names), len(set(names)))


class BuildTests(unittest.TestCase):
    def test_bundle_layout_runner_and_packages(self):
        key = pkg.generate_key()
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            key_path = temp / "key.pem"
            pkg.save_private_key(key, key_path, "κωδικός-δοκιμής-12")

            def tree_builder(work, version, **_):
                return small_tree(Path(work) / "release" / version, version)

            out = fb.build(temp / "dist" / "field-test", key_path, "κωδικός-δοκιμής-12",
                           python_version="3.13.0", report=lambda text: None,
                           tree_builder=tree_builder)
            for version in ft.UPDATES:
                manifest = pkg.read_manifest(out / "packages" / ft.package_file(version),
                                             trusted_keys=[pkg.public_hex(key)])
                self.assertEqual(manifest["version"], version)
            self.assertTrue((out / "manifests" / f"{ft.BASE}.json").is_file())
            site = out / "python" / "Lib" / "site-packages"
            self.assertTrue((site / "cryptography").is_dir())
            self.assertTrue((site / "cffi").is_dir())
            for skipped in ("PySide6", "shiboken6", "winrt_runtime"):
                self.assertFalse((site / skipped).exists(), skipped)
            self.assertTrue((out / "app" / "tools" / "field_test.py").is_file())
            self.assertTrue((out / "app" / "client" / "supervisor.py").is_file())
            cmd = (out / "field-test.cmd").read_bytes()
            cmd.decode("ascii")
            self.assertIn(b"\r\n", cmd)
            self.assertIn(b"field_test.ps1", cmd)
            self.assertTrue((out / "README.txt").read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertTrue((out / "field_test.ps1").read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertFalse((temp / "dist" / "field-test-work").exists())
            with zipfile.ZipFile(temp / "dist" / "sch-intercom-field-test.zip") as archive:
                names = archive.namelist()
            self.assertIn(f"field-test/packages/{ft.package_file(ft.FINAL)}", names)

    def test_key_must_be_the_real_release_key(self):
        key = pkg.generate_key()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "k.pem"
            pkg.save_private_key(key, path, "κωδικός-δοκιμής-12")
            with self.assertRaises(SystemExit):
                fb.check_key(path, "κωδικός-δοκιμής-12")

    def test_build_refuses_to_delete_a_test_in_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "field-test"
            out.mkdir()
            (out / ft.STATE_FILE).write_text('{"phase": "rebooting"}', encoding="utf-8")
            with self.assertRaises(SystemExit):
                fb.build(out, Path(temp) / "k.pem", "x", python_version="3.13.0",
                         report=lambda text: None)
            self.assertTrue((out / ft.STATE_FILE).exists())


class InstallerTests(unittest.TestCase):
    def test_silent_enrollment_does_not_wait_for_enter(self):
        text = (ROOT / "installer" / "sch-intercom-client.iss").read_text(encoding="utf-8-sig")
        silent = text.split("if NeedsDetails and WizardSilent then", 1)[1].split("else if", 1)[0]
        self.assertIn("--enroll", silent)
        self.assertNotIn("--pause", silent)
        self.assertIn("SW_HIDE", silent)


if __name__ == "__main__":
    unittest.main()
