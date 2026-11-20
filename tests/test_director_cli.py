"""Έλεγχοι εντολών Director χωρίς μικρόφωνο ή πραγματικό δίκτυο."""

import io
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from director.main import DirectorStats, command_loop, periodic_stats, show_help


class FakeBroadcaster:
    dropped = 3
    client_count = 2

    def __init__(self):
        self.targets = []

    def clients_snapshot(self):
        return [("uuid-1", "C2627-Γ1")]

    def set_targets(self, **kwargs):
        self.targets.append(kwargs)


class DirectorCliTests(unittest.TestCase):
    def test_help_lists_all_commands(self):
        help_text = show_help()
        for line in ("help, ?", "status", "stats", "stats on", "stats off",
                     "all", "none", "room <όνομα>", "client <UUID>", "quit"):
            with self.subTest(line=line):
                self.assertIn(line, help_text)

    def test_stats_snapshot(self):
        stats = DirectorStats()
        stats.update(250, 4)
        report = stats.report(FakeBroadcaster())
        self.assertIn("250 blocks", report)
        self.assertIn("χαμένα μικροφώνου: 4", report)
        self.assertIn("χαμένα ουρών Clients: 3", report)
        self.assertIn("Clients: 2", report)
        self.assertFalse(stats.periodic_enabled())

    def test_commands_stats_help_and_quit(self):
        stats = DirectorStats()
        stop = threading.Event()
        broadcaster = FakeBroadcaster()
        output = io.StringIO()
        with patch("builtins.input", side_effect=[
            "help", "?", "stats", "stats on", "stats off",
            "room C2627-Γ1", "none", "all", "quit"
        ]), redirect_stdout(output):
            command_loop(broadcaster, stop, stats)
        self.assertTrue(stop.is_set())
        self.assertFalse(stats.periodic_enabled())
        text = output.getvalue()
        self.assertIn("Διαθέσιμες εντολές", text)
        self.assertIn("Περιοδικά στατιστικά ενεργά", text)
        self.assertIn("Περιοδικά στατιστικά ανενεργά", text)
        self.assertIn("Τερματισμός Director", text)
        self.assertEqual(broadcaster.targets, [
            {"rooms": {"C2627-Γ1"}, "client_ids": set()},
            {"rooms": set(), "client_ids": set()},
            {},
        ])

    def test_reporter_stays_silent_when_off(self):
        stats = DirectorStats()
        stop = threading.Event()
        output = io.StringIO()
        with patch("director.main.STATS_INTERVAL_SECONDS", 0.01), redirect_stdout(output):
            thread = threading.Thread(target=periodic_stats,
                                      args=(stats, FakeBroadcaster(), stop))
            thread.start()
            self.assertFalse(stop.wait(0.05))
            stop.set()
            thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
