"""4.2D1: tests for collision-aware serving independent of Qt or real audio."""
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from director.manager_state import change_role, load, restored
from director.serving import ServingConflict, ServingController
from shared.discovery import BeaconInfo, SeenManager


class FakeWatcher:
    def __init__(self):
        self.started_at = 0.0
        self.found = []

    def others(self):
        return list(self.found)


class ServingTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "manager-state.json"
        self.watcher = FakeWatcher()
        self.started = []
        self.stopped = []
        self.enrollment = []
        self.lines = []
        self.ctrl = ServingController(
            self.path, self.watcher,
            start_network=self.started.append,
            stop_network=lambda: self.stopped.append(True),
            change_enrollment=self.enrollment.append,
            check_seconds=3.0, clock=lambda: 10.0,
            sleeper=lambda amount: self.fail("unexpected wait"),
            report=self.lines.append)

    @staticmethod
    def another(version=3):
        return SeenManager(BeaconInfo("00000000-0000-0000-0000-000000000001",
                                      50555, "00000000-0000-0000-0000-000000000002",
                                      "00000000-0000-0000-0000-000000000003",
                                      version, "pc-other"), "127.0.0.2", 10.0)

    def test_start_and_stop_persist(self):
        self.assertTrue(self.ctrl.start())
        self.assertEqual(self.started, ["primary"])
        self.assertTrue(load(self.path).serving)
        self.ctrl.stop()
        self.assertEqual(self.stopped, [True])
        self.assertFalse(load(self.path).serving)

    def test_conflict_rejected_and_force_allowed(self):
        self.watcher.found = [self.another()]
        with self.assertRaises(ServingConflict):
            self.ctrl.start()
        self.assertEqual(self.started, [])
        self.assertTrue(self.ctrl.start(force=True))
        self.assertEqual(self.started, ["primary"])
        self.assertTrue(any("ΠΡΟΣΟΧΗ" in line for line in self.lines))

    def test_startup_conflict_keeps_preference(self):
        self.watcher.found = [self.another()]
        self.assertFalse(self.ctrl.startup())
        self.assertEqual(self.started, [])
        self.assertTrue(load(self.path).serving)

    def test_backup_can_serve_but_does_not_open_enrollment(self):
        restored(self.path)
        self.ctrl.start()
        self.assertEqual(self.started, ["backup"])
        self.ctrl.role_changed()
        self.assertEqual(self.enrollment, [False])
        change_role(self.path, "primary")
        self.ctrl.role_changed()
        self.assertEqual(self.enrollment, [False, True])

    def test_collision_warning_once_until_reappears(self):
        self.ctrl.start()
        self.watcher.found = [self.another()]
        self.ctrl.poll_conflicts()
        self.ctrl.poll_conflicts()
        self.assertEqual(sum("ΠΡΟΣΟΧΗ" in line for line in self.lines), 1)
        self.watcher.found = []
        self.ctrl.poll_conflicts()
        self.watcher.found = [self.another()]
        self.ctrl.poll_conflicts()
        self.assertEqual(sum("ΠΡΟΣΟΧΗ" in line for line in self.lines), 2)

    def test_shutdown_does_not_change_persistent_choice(self):
        self.ctrl.start()
        self.ctrl.shutdown()
        self.assertTrue(load(self.path).serving)

    def test_socket_callback_close_releases_port(self):
        service = {}
        def start(role):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            service["sock"] = sock
            service["address"] = sock.getsockname()
        def stop():
            service["sock"].close()
        ctrl = ServingController(self.path, self.watcher, start_network=start,
                                 stop_network=stop, check_seconds=0)
        ctrl.start()
        with socket.create_connection(service["address"], timeout=1):
            pass
        ctrl.stop()
        with self.assertRaises(OSError):
            socket.create_connection(service["address"], timeout=0.2)


if __name__ == "__main__":
    unittest.main()
