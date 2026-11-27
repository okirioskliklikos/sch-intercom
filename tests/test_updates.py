"""Φάση 5B2a/5B2c: διανομή ενημερώσεων από τον Manager και λήψη στον Client (πραγματικό mTLS)."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid
import zipfile

from client.install_paths import Layout
from client.main import pump_frames
from client.updater import AnnouncementState, Retry, Updater
from director.serving import SocketServingService
from director.updates import (STATUS_FILE, UpdateError, UpdateServer, UpdateStatus,
                              UpdateStore, main as updates_main)
from shared import package as pkg
from shared.audio import (HEADER_BYTES, MESSAGE_KEEPALIVE, MESSAGE_UPDATE_CHECK, ControlMessage,
                          _HEADER, PROTOCOL_MAGIC, PROTOCOL_VERSION, encode_control,
                          receive_message)
from shared.device_identity import create_device_identity
from shared.jitter import DelayTracker
from shared.transport_security import DEFAULT_SERVER_NAME, client_context
from shared.update_protocol import check_message, read_message, send_message
from tests.test_failure_scenarios import ROOM, RegistryFixture, wait_until
from tests.test_release_package import make_tree, rewrite


class Packages:
    """Πακέτα δοκιμής υπογεγραμμένα με κλειδί δοκιμής (όχι το κλειδί του συντηρητή)."""

    def __init__(self, root: Path):
        self.root = root
        self.key = pkg.generate_key()
        self.public = pkg.public_hex(self.key)

    def make(self, version: str, *, key=None, min_updater: int = 1, size: int = 10) -> Path:
        tree = make_tree(self.root / f"tree-{version}-{uuid.uuid4().hex[:6]}", version)
        (tree / "app" / "blob.bin").write_bytes(bytes(range(256)) * 1024 * size)
        out = self.root / f"in-{version}-{uuid.uuid4().hex[:6]}.sicu"
        pkg.write_package(tree, out, key or self.key, version, min_updater=min_updater)
        return out


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.packages = Packages(self.root)
        self.store = UpdateStore(self.root / "updates", trusted_keys=[self.packages.public])

    def check(self, app="1.0.0", client_id="a", failed=(), updater=1):
        return {"client_id": client_id, "app": app, "failed": list(failed),
                "updater": updater, "protocol": 7}

    def test_import_keeps_current_and_previous(self):
        first = self.store.import_package(self.packages.make("1.0.1"))
        self.assertEqual(first["version"], "1.0.1")
        for bad in ("1.0.1", "1.0.0"):
            with self.subTest(version=bad), self.assertRaisesRegex(UpdateError, "δεν είναι νεότερη"):
                self.store.import_package(self.packages.make(bad))
        self.store.import_package(self.packages.make("1.0.2"))
        self.store.import_package(self.packages.make("1.0.3"))
        config = self.store.config()
        self.assertEqual((config["current"]["version"], config["previous"]["version"]),
                         ("1.0.3", "1.0.2"))
        names = sorted(p.name for p in self.store.folder.glob("*.sicu"))
        self.assertEqual(names, ["sch-intercom-client-1.0.2.sicu", "sch-intercom-client-1.0.3.sicu"])
        self.assertFalse(self.store.package_missing())
        self.store.package_path("1.0.3").unlink()
        self.assertTrue(self.store.package_missing())

    def test_import_rejects_wrong_key(self):
        with self.assertRaisesRegex(UpdateError, "Άκυρη υπογραφή"):
            self.store.import_package(self.packages.make("1.0.1", key=pkg.generate_key()))
        self.assertIsNone(self.store.current())

    def test_offer_rules(self):
        self.assertIsNone(self.store.offer_for(self.check()))
        self.store.import_package(self.packages.make("1.0.1"))
        self.assertEqual(self.store.offer_for(self.check())["version"], "1.0.1")
        self.assertIsNone(self.store.offer_for(self.check(app="1.0.1")))
        self.assertIsNone(self.store.offer_for(self.check(app="2.0.0")))
        self.assertIsNone(self.store.offer_for(self.check(failed=["1.0.1"])))
        self.assertIsNone(self.store.offer_for(self.check(updater=0)))
        self.store.set_auto(False)
        self.assertIsNone(self.store.offer_for(self.check()))
        self.store.start_now(["a"])
        self.assertIsNotNone(self.store.offer_for(self.check()))
        self.assertIsNone(self.store.offer_for(self.check(client_id="b")))
        self.store.start_now(None)
        self.assertIsNotNone(self.store.offer_for(self.check(client_id="b")))
        # Νέο πακέτο: το «Έναρξη τώρα» δεν μεταφέρεται σε άλλη έκδοση.
        self.store.import_package(self.packages.make("1.0.2"))
        self.assertIsNone(self.store.offer_for(self.check()))

    def test_config_is_reread_when_cli_changes_it(self):
        running = UpdateStore(self.store.folder, trusted_keys=[self.packages.public])
        self.assertTrue(running.config()["auto"])
        with mock.patch("sys.stdout"):
            self.assertEqual(updates_main(["--registry-file", str(self.root / "r.json"),
                                           "auto", "off"]), 0)
        self.assertFalse(running.config()["auto"])
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(updates_main(["--registry-file", str(self.root / "r.json"),
                                           "start", "--all"]), 2)   # χωρίς πακέτο


class ProtocolMessageTests(unittest.TestCase):
    def test_unknown_small_control_is_returned_not_fatal(self):
        left, right = socket.socketpair()
        with left, right:
            left.sendall(_HEADER.pack(PROTOCOL_MAGIC, PROTOCOL_VERSION, 9, 3, 0, 0) + b"abc")
            left.sendall(encode_control(MESSAGE_UPDATE_CHECK))
            self.assertEqual(receive_message(right), ControlMessage(9, b"abc"))
            self.assertEqual(receive_message(right), ControlMessage(MESSAGE_UPDATE_CHECK))
            left.sendall(_HEADER.pack(PROTOCOL_MAGIC, PROTOCOL_VERSION, 9, 4000, 0, 0))
            with self.assertRaises(ValueError):
                receive_message(right)
        self.assertEqual(len(encode_control(MESSAGE_UPDATE_CHECK)), HEADER_BYTES)

    def test_pump_frames_calls_update_check_and_ignores_unknown(self):
        left, right = socket.socketpair()
        calls = []
        right.settimeout(0.5)
        with left, right:
            left.sendall(_HEADER.pack(PROTOCOL_MAGIC, PROTOCOL_VERSION, 9, 2, 0, 0) + b"{}")
            left.sendall(encode_control(MESSAGE_UPDATE_CHECK))
            left.sendall(encode_control(MESSAGE_KEEPALIVE))
            left.shutdown(socket.SHUT_WR)
            jitter = mock.Mock(mode="αναμονή")
            with self.assertRaises(EOFError):
                pump_frames(right, jitter, DelayTracker(), update_check=lambda: calls.append(1))
        self.assertEqual(calls, [1])

    def test_broadcaster_pushes_check_to_selected_clients(self):
        from director.broadcast import AudioBroadcaster
        broadcaster = AudioBroadcaster()
        self.addCleanup(broadcaster.close)
        sessions = {cid: mock.Mock() for cid in ("a", "b")}
        broadcaster._sessions.update(sessions)
        self.assertEqual(broadcaster.request_update_check(["a"]), 1)
        sessions["a"].enqueue.assert_called_once_with(encode_control(MESSAGE_UPDATE_CHECK))
        sessions["b"].enqueue.assert_not_called()
        self.assertEqual(broadcaster.request_update_check(), 2)
        broadcaster._sessions.clear()

    def test_serving_opens_updates_only_for_primary(self):
        updates = mock.Mock()
        service = SocketServingService(mock.Mock(), identifier="x", inst="y",
                                       registry=mock.Mock(), accept_clients=None,
                                       broadcaster=mock.Mock(status_snapshot=lambda: []),
                                       enrollment=None, updates=updates)
        service.change_enrollment(False)
        updates.stop.assert_called_once()
        service.change_enrollment(True)
        updates.open.assert_called_once()
        service.stop()
        self.assertEqual(updates.stop.call_count, 2)


class ServerFixture(RegistryFixture):
    def setUp(self):
        super().setUp()
        root = Path(self._tls_temp.name)
        self.packages = Packages(root / "packages")
        self.packages.root.mkdir()
        self.store = UpdateStore(root / "updates", trusted_keys=[self.packages.public])
        self.status = UpdateStatus(root / "updates" / STATUS_FILE)
        self.logs: list[str] = []
        self.pushed: list = []
        self.trust = None

    def open_server(self, **options):
        from director.main import RegistryTrust
        self.trust = RegistryTrust(self.registry, self.director_cert, self.director_key)
        self.updates = UpdateServer(self.trust, self.store, self.status, host="127.0.0.1",
                                    port=0, log=self.logs.append, watch_seconds=0.05,
                                    **options)
        self.assertTrue(self.updates.open(push=self.pushed.append))
        self.addCleanup(self.updates.stop)

    def client(self, label="pc", installed="1.0.0", approve=True):
        client_id = str(uuid.uuid4())
        folder = Path(self._tls_temp.name) / label
        cert, key, password = create_device_identity(folder / "credentials", client_id)
        if approve and self.trust is not None:
            self.trust.approve(client_id, ROOM, cert.read_bytes())   # νέο mTLS context
        elif approve:
            self.registry.approve(client_id, ROOM, cert.read_bytes())
        layout = Layout(folder / "home", folder / "data")
        layout.set_installed(installed)
        context = client_context(self.director_cert, certfile=cert, keyfile=key,
                                 password=password)
        port = self.updates.port if self.trust is not None else 0
        updater = Updater(layout, client_id, context, port=port,
                          trusted_keys=[self.packages.public], log=self.logs.append,
                          pause_poll=0.02)
        return updater


class ServerTests(ServerFixture, unittest.TestCase):
    def test_download_verify_extract_and_report_ready(self):
        self.store.import_package(self.packages.make("1.0.1"))
        self.open_server()
        updater = self.client()
        self.assertEqual(updater.run_once("127.0.0.1"), "ready")
        target = updater.layout.version_dir("1.0.1")
        self.assertEqual((target / "VERSION").read_text().strip(), "1.0.1")
        self.assertEqual(list(updater.staging.iterdir()), [])
        entry = self.status.snapshot()[updater.client_id]
        self.assertEqual((entry["state"], entry["available"]), ("ready", "1.0.1"))
        # Δεύτερος έλεγχος: ήδη έτοιμη, καμία λήψη.
        self.assertEqual(updater.run_once("127.0.0.1"), "ready")
        self.assertEqual(updater.layout.current_version(), "1.0.0")   # εναλλαγή στο 5B2b

    def test_up_to_date_and_auto_off_report_state(self):
        self.store.import_package(self.packages.make("1.0.1"))
        self.open_server()
        current = self.client("new", installed="1.0.1")
        self.assertEqual(current.run_once("127.0.0.1"), "none")
        self.assertEqual(self.status.snapshot()[current.client_id]["state"], "current")
        self.store.set_auto(False)
        old = self.client("old")
        self.assertEqual(old.run_once("127.0.0.1"), "none")
        entry = self.status.snapshot()[old.client_id]
        self.assertEqual((entry["state"], entry["app"], entry["available"]),
                         ("available", "1.0.0", "1.0.1"))
        self.store.start_now([old.client_id])
        self.assertEqual(old.run_once("127.0.0.1"), "ready")

    def test_resume_from_partial_download(self):
        self.store.import_package(self.packages.make("1.0.1", size=20))
        self.open_server()
        updater = self.client()
        index = self.updates.index(self.store.current())
        work = updater.staging / "1.0.1"
        work.mkdir(parents=True)
        (updater.staging / "1.0.1.json").write_text(json.dumps({"sha256": index.sha256}))
        item = index.files["app/blob.bin"]
        with zipfile.ZipFile(index.path) as archive:
            data = archive.read(pkg.FILES_PREFIX + "app/blob.bin")
        (work / "partial").mkdir()
        (work / "partial" / f"{item['sha256']}.part").write_bytes(data[:300_000])
        self.assertEqual(updater.run_once("127.0.0.1"), "ready")
        self.assertTrue(any("Συνέχιση λήψης 1.0.1: app/blob.bin" in line for line in self.logs))

    def test_partial_for_other_package_is_discarded(self):
        self.store.import_package(self.packages.make("1.0.1"))
        self.open_server()
        updater = self.client()
        work = updater.staging / "1.0.1"
        work.mkdir(parents=True)
        (work / "partial").mkdir()
        (work / "partial" / ("0" * 64 + ".part")).write_bytes(b"x" * 1000)
        (updater.staging / "1.0.1.json").write_text('{"sha256": "00"}')
        (updater.staging / "0.9.9.part").write_bytes(b"old")
        self.assertEqual(updater.run_once("127.0.0.1"), "ready")
        self.assertFalse(any("Συνέχιση" in line for line in self.logs))
        self.assertEqual(list(updater.staging.iterdir()), [])

    def test_tampered_package_rejected_by_client_and_not_retried(self):
        self.store.import_package(self.packages.make("1.0.1"))
        # Ο Manager (ή το δίκτυο) αλλοιώνει το πακέτο και δηλώνει σωστό hash γι' αυτό.
        forged = self.packages.make("1.0.1", key=pkg.generate_key())
        path = self.store.package_path("1.0.1")
        path.write_bytes(forged.read_bytes())
        config = json.loads(self.store.config_file.read_text())
        config["current"]["size"] = path.stat().st_size
        from director.updates import _sha256
        config["current"]["sha256"] = _sha256(path)
        self.store.config_file.write_text(json.dumps(config))
        self.open_server()
        updater = self.client()
        self.assertEqual(updater.run_once("127.0.0.1"), "failed")
        self.assertFalse(updater.layout.version_dir("1.0.1").exists())
        self.assertEqual(updater.layout.read_current()["failed"], ["1.0.1"])
        entry = self.status.snapshot()[updater.client_id]
        self.assertEqual(entry["state"], "failed")
        self.assertIn("Άκυρη υπογραφή", entry["detail"])
        self.assertEqual(updater.run_once("127.0.0.1"), "none")

    def test_wrong_file_from_manager_is_failed_download(self):
        self.store.import_package(self.packages.make("1.0.1"))
        path = self.store.package_path("1.0.1")
        changed = path.with_name("changed.sicu")
        # Σωστή υπογραφή/manifest, αλλά ο Manager (ή το δίσκος του) στέλνει άλλα bytes.
        rewrite(path, changed, lambda name, data:
                bytes(len(data)) if name.endswith("blob.bin") else data)
        changed.replace(path)
        self.open_server()
        updater = self.client()
        self.assertEqual(updater.run_once("127.0.0.1"), "failed")
        self.assertEqual(updater.layout.read_current()["failed"], [])   # ξαναδοκιμάζεται
        self.assertIn("Λάθος SHA-256 λήψης: app/blob.bin",
                      self.status.snapshot()[updater.client_id]["detail"])
        self.assertFalse(updater.layout.version_dir("1.0.1").exists())

    def test_downgrade_offer_ignored_by_client(self):
        self.store.import_package(self.packages.make("1.0.1"))
        self.open_server()
        updater = self.client(installed="1.0.0")
        updater.layout.set_installed("1.0.5")
        with mock.patch.object(self.store, "offer_for", return_value=self.store.current()):
            self.assertEqual(updater.run_once("127.0.0.1"), "none")
        self.assertFalse(updater.layout.version_dir("1.0.1").exists())

    def test_pause_during_announcement_then_continue(self):
        self.store.import_package(self.packages.make("1.0.1"))
        self.open_server()
        announcement = AnnouncementState()
        updater = self.client()
        updater.announcement = announcement
        announcement.set(True)
        threading.Timer(0.3, announcement.set, args=(False,)).start()
        with self.assertRaises(Retry) as caught:
            updater.run_once("127.0.0.1")
        self.assertEqual(caught.exception.seconds, 0)
        self.assertEqual(self.status.snapshot()[updater.client_id]["state"], "waiting")
        self.assertEqual(updater.run_once("127.0.0.1"), "ready")

    def test_download_limit_gives_busy(self):
        self.store.import_package(self.packages.make("1.0.1"))
        self.open_server(max_downloads=1)
        holder = self.client("holder")
        stream = holder._connect("127.0.0.1")
        self.addCleanup(stream.close)
        send_message(stream, check_message(holder.client_id, "1.0.0", updater=1, protocol=7))
        offer = read_message(stream)
        send_message(stream, {"type": "get-file", "version": "1.0.1", "path": "app/blob.bin",
                              "offset": 0})
        reply = read_message(stream)
        self.assertEqual(reply["type"], "data")
        waiting = self.client("waiting")
        with self.assertRaises(Retry) as caught:
            waiting.run_once("127.0.0.1")
        self.assertTrue(20 <= caught.exception.seconds <= 40, caught.exception.seconds)
        self.assertEqual(offer["type"], "offer")
        stream.close()
        self.assertTrue(wait_until(lambda: self.updates.active_downloads == 0))
        self.assertEqual(waiting.run_once("127.0.0.1"), "ready")

    def test_unapproved_client_gets_nothing(self):
        self.store.import_package(self.packages.make("1.0.1"))
        unknown = self.client("unknown", approve=False)
        approved = self.client("approved")   # ώστε το mTLS context να έχει πιστοποιητικά
        self.open_server()
        unknown.port = approved.port = self.updates.port
        with self.assertRaises((OSError, EOFError)):
            unknown.run_once("127.0.0.1")
        self.assertNotIn(unknown.client_id, self.status.snapshot())

    def test_push_on_new_package_and_start_now(self):
        self.open_server()
        self.store.import_package(self.packages.make("1.0.1"))
        self.assertTrue(wait_until(lambda: self.pushed == [None]))
        self.store.start_now(["a", "b"])
        self.assertTrue(wait_until(lambda: self.pushed == [None, ["a", "b"]]))

    def test_closed_port_distributes_nothing(self):
        self.store.import_package(self.packages.make("1.0.1"))
        self.open_server()
        updater = self.client()
        self.updates.stop()
        self.assertFalse(self.updates.is_open)
        with self.assertRaises(OSError):
            updater.run_once("127.0.0.1")

    def test_trigger_runs_in_background_and_retries_busy(self):
        self.store.import_package(self.packages.make("1.0.1"))
        self.open_server()
        updater = self.client()
        updater.start()
        self.addCleanup(updater.stop)
        updater.trigger("127.0.0.1")
        self.assertTrue(wait_until(lambda: updater.last_result == "ready", timeout=10))


if __name__ == "__main__":
    unittest.main()
