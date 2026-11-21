"""Μικρόφωνο Director: ανοίγει μόνο όταν χρειάζεται, προεπισκόπηση, Mute, σφάλματα.

Χωρίς κάρτα ήχου: το ψεύτικο stream κρατά τη θέση του sounddevice και ο
έλεγχος «στέλνει» μπλοκ καλώντας κατευθείαν την πύλη, όπως το
director.main.stream_microphone.
"""
from unittest import TestCase
import threading
import time

from director.gui import (MODE_ALL, MODE_ROOMS, DirectorMicrophone, VoiceGate,
                          choose_targets, meter_fraction,
                          room_grid_shape, room_states)

WAIT = 2.0


class FakeBroadcaster:
    def __init__(self):
        self.targets = []
        self.published = []

    def set_targets(self, *, rooms=None, client_ids=None):
        self.targets.append((rooms, client_ids))

    def publish(self, pcm, timestamp_us):
        self.published.append((pcm, timestamp_us))


class FakeStream:
    """Ένα «άνοιγμα συσκευής» ανά κλήση· τρέχει μέχρι το stop."""

    def __init__(self):
        self.lock = threading.Lock()
        self.opened = threading.Event()
        self.calls = 0
        self.running = 0
        self.max_running = 0
        self.fail_with = None
        self.gate = None
        self.level_callback = None
        self.closed = threading.Event()
        self.kwargs = None

    def __call__(self, gate, *, stop, ready_callback, level_callback, **kwargs):
        with self.lock:
            self.calls += 1
            self.running += 1
            self.max_running = max(self.max_running, self.running)
            self.kwargs = kwargs
        self.gate, self.level_callback = gate, level_callback
        self.closed.clear()
        try:
            if self.fail_with is not None:
                raise self.fail_with
            ready_callback()
            self.opened.set()
            stop.wait()
        finally:
            with self.lock:
                self.running -= 1
            self.closed.set()

    def send(self, captured_us=None):
        """Μπλοκ που μόλις καταγράφηκε (ή με δοσμένο timestamp)."""
        if captured_us is None:
            captured_us = time.monotonic_ns() // 1000
        self.gate.publish(b"pcm", captured_us)


def wait_until(predicate, timeout=WAIT):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class DirectorMicrophoneTests(TestCase):
    def setUp(self):
        self.broadcaster = FakeBroadcaster()
        self.stream = FakeStream()
        self.mic = DirectorMicrophone(self.stream, self.broadcaster, device=3, latency="low")

    def tearDown(self):
        self.mic.shutdown()

    def open_and_wait(self):
        self.assertTrue(self.stream.opened.wait(WAIT))
        self.assertTrue(wait_until(lambda: self.mic.ready))

    def test_idle_keeps_device_closed_and_targets_none(self):
        self.assertFalse(self.mic.device_open)
        self.assertEqual(self.stream.calls, 0)
        self.assertEqual(self.broadcaster.targets, [(set(), set())])
        self.assertEqual(self.mic.level(), 0.0)

    def test_preview_measures_without_broadcast_and_toggles_off(self):
        self.mic.set_preview(True)
        self.open_and_wait()
        self.assertEqual(self.stream.kwargs, {"device": 3, "latency": "low"})
        self.stream.level_callback(0.5)
        self.stream.send()
        self.assertAlmostEqual(self.mic.level(), 0.5)
        self.assertEqual(self.broadcaster.published, [])
        self.assertEqual(self.broadcaster.targets, [(set(), set())])
        self.mic.set_preview(False)
        self.assertFalse(self.mic.device_open)
        self.assertTrue(self.stream.closed.wait(WAIT))
        self.assertEqual(self.mic.level(), 0.0)

    def test_mute_while_idle_blocks_start_and_preview_and_closes_device(self):
        self.mic.set_preview(True)
        self.open_and_wait()
        self.mic.set_muted(True)
        self.assertFalse(self.mic.preview)
        self.assertFalse(self.mic.device_open)
        self.assertTrue(self.stream.closed.wait(WAIT))
        self.assertFalse(self.mic.can_start)
        with self.assertRaises(ValueError):
            self.mic.start_announcement(None)
        self.mic.set_preview(True)
        self.assertFalse(self.mic.preview)
        self.assertFalse(self.mic.live)
        self.assertEqual(self.broadcaster.targets, [(set(), set())])
        self.assertEqual(self.stream.calls, 1)

    def test_announcement_opens_device_and_sends_only_new_voice(self):
        before = time.monotonic_ns() // 1000 - 50_000
        self.mic.start_announcement({"C2627-Γ1"})
        self.assertEqual(self.broadcaster.targets[-1], ({"C2627-Γ1"}, set()))
        self.open_and_wait()
        self.stream.send(captured_us=before)   # παλιό μπλοκ στην ουρά: όχι
        self.stream.send()
        self.assertEqual(len(self.broadcaster.published), 1)
        self.mic.stop_announcement()
        self.stream.send()
        self.assertEqual(len(self.broadcaster.published), 1)
        self.assertEqual(self.broadcaster.targets[-1], (set(), set()))
        self.assertFalse(self.mic.device_open)

    def test_preview_audio_never_leaks_into_announcement(self):
        self.mic.set_preview(True)
        self.open_and_wait()
        captured_during_preview = time.monotonic_ns() // 1000
        time.sleep(0.002)
        self.mic.start_announcement(None)
        self.assertFalse(self.mic.preview)
        # Ίδιο άνοιγμα συσκευής: χωρίς καθυστέρηση νέου ανοίγματος.
        self.assertTrue(self.mic.device_open)
        self.assertEqual(self.stream.calls, 1)
        self.stream.send(captured_us=captured_during_preview)
        self.assertEqual(self.broadcaster.published, [])
        self.stream.send()
        self.assertEqual(len(self.broadcaster.published), 1)

    def test_mute_during_announcement_cuts_voice_only(self):
        self.mic.start_announcement(None)
        self.open_and_wait()
        targets_before = list(self.broadcaster.targets)
        self.mic.set_muted(True)
        self.assertTrue(self.mic.live)
        self.assertTrue(self.mic.device_open)       # η συσκευή μένει ανοιχτή
        self.assertFalse(self.stream.closed.is_set())
        self.assertEqual(self.broadcaster.targets, targets_before)  # χωρίς STREAM_STOP
        captured_while_muted = time.monotonic_ns() // 1000
        self.stream.level_callback(0.8)
        self.stream.send()
        self.assertEqual(self.broadcaster.published, [])
        self.assertEqual(self.mic.level(), 0.0)
        time.sleep(0.002)
        self.mic.set_muted(False)
        # Ήχος από το διάστημα της σίγασης που περίμενε στην ουρά δεν στέλνεται.
        self.stream.send(captured_us=captured_while_muted)
        self.assertEqual(self.broadcaster.published, [])
        self.stream.send()
        self.assertEqual(len(self.broadcaster.published), 1)
        self.assertEqual(self.stream.calls, 1)      # άρση χωρίς νέα έναρξη
        self.assertEqual(self.broadcaster.targets, targets_before)

    def test_red_button_stops_even_when_muted(self):
        self.mic.start_announcement(None)
        self.open_and_wait()
        self.mic.set_muted(True)
        self.mic.stop_announcement()
        self.assertFalse(self.mic.live)
        self.assertEqual(self.broadcaster.targets[-1], (set(), set()))
        self.assertFalse(self.mic.device_open)
        self.assertTrue(self.stream.closed.wait(WAIT))
        self.assertTrue(self.mic.muted)
        self.assertFalse(self.mic.can_start)

    def test_open_error_stops_announcement_and_is_reported_once(self):
        self.stream.fail_with = OSError("η συσκευή αποσυνδέθηκε")
        self.mic.start_announcement(None)
        self.assertTrue(self.stream.closed.wait(WAIT))
        self.assertTrue(wait_until(lambda: self.mic.poll() == "η συσκευή αποσυνδέθηκε"))
        self.assertFalse(self.mic.live)
        self.assertEqual(self.broadcaster.targets[-1], (set(), set()))
        self.assertFalse(self.mic.device_open)
        self.assertEqual(self.mic.error, "η συσκευή αποσυνδέθηκε")
        self.assertIsNone(self.mic.poll())
        # Νέα προσπάθεια που πετυχαίνει σβήνει το σφάλμα.
        self.stream.fail_with = None
        self.mic.set_preview(True)
        self.open_and_wait()
        self.mic.clear_error_if_ready()
        self.assertIsNone(self.mic.error)

    def test_preview_error_turns_preview_off(self):
        self.stream.fail_with = RuntimeError()
        self.mic.set_preview(True)
        self.assertTrue(wait_until(lambda: self.mic.poll() == "RuntimeError"))
        self.assertFalse(self.mic.preview)
        self.assertFalse(self.mic.device_open)

    def test_reopen_waits_for_previous_device_to_close(self):
        for _ in range(5):
            self.mic.set_preview(True)
            self.mic.set_preview(False)
        self.mic.set_preview(True)
        self.open_and_wait()
        self.assertEqual(self.stream.max_running, 1)

    def test_invalid_targets_do_not_open_device(self):
        with self.assertRaises(ValueError):
            self.mic.start_announcement(set())
        self.assertFalse(self.mic.live)
        self.assertFalse(self.mic.device_open)
        self.assertFalse(self.mic.gate.is_open)


class VoiceGateTests(TestCase):
    def test_closed_gate_drops_everything(self):
        broadcaster = FakeBroadcaster()
        clock = [100]
        gate = VoiceGate(broadcaster, clock_us=lambda: clock[0])
        gate.publish(b"a", 200)
        gate.open()
        gate.publish(b"b", 99)
        gate.publish(b"c", 100)
        gate.close()
        gate.publish(b"d", 300)
        self.assertEqual(broadcaster.published, [(b"c", 100)])


class TargetChoiceTests(TestCase):
    STATUS = [("id-1", "Γ1", True, 0.1), ("id-2", "Γ1", False, 9.0),
              ("id-3", "Γ2", False, 9.0)]

    def test_room_online_if_any_client_online(self):
        self.assertEqual(room_states(self.STATUS), [("Γ1", True), ("Γ2", False)])

    def test_all_rooms(self):
        self.assertIsNone(choose_targets(MODE_ALL, set(), self.STATUS))

    def test_specific_rooms(self):
        self.assertEqual(choose_targets(MODE_ROOMS, {"Γ1", "Γ2"}, self.STATUS), {"Γ1", "Γ2"})

    def test_rejections(self):
        offline = [("id-3", "Γ2", False, 9.0)]
        for mode, checked, status in [
            (MODE_ALL, set(), []),
            (MODE_ALL, set(), offline),
            (MODE_ROOMS, set(), self.STATUS),
            (MODE_ROOMS, {"Γ2"}, self.STATUS),
            ("group", set(), self.STATUS),
        ]:
            with self.subTest(mode=mode, checked=checked):
                with self.assertRaises(ValueError):
                    choose_targets(mode, checked, status)

    def test_room_grid_fills_columns_in_order(self):
        self.assertEqual(room_grid_shape(0, 10, 2), (0, 0))
        self.assertEqual(room_grid_shape(17, 20, 2), (17, 1))   # όλες στην πρώτη
        self.assertEqual(room_grid_shape(17, 15, 2), (15, 2))   # 15 + 2
        self.assertEqual(room_grid_shape(30, 15, 2), (15, 2))   # δύο γεμάτες
        self.assertEqual(room_grid_shape(31, 15, 3), (15, 3))   # τρίτη στήλη
        self.assertEqual(room_grid_shape(40, 15, 2), (20, 2))   # δεν χωρά τρίτη: κύλιση
        self.assertEqual(room_grid_shape(50, 15, 3), (17, 3))   # κύλιση, ισόποσα
        self.assertEqual(room_grid_shape(3, 0, 2), (2, 2))      # σχεδόν τίποτα δεν χωρά: κύλιση

    def test_meter_scale(self):
        self.assertEqual(meter_fraction(0.0), 0.0)
        self.assertEqual(meter_fraction(1.0), 1.0)
        self.assertAlmostEqual(meter_fraction(10 ** (-24 / 20)), 0.5)
        self.assertEqual(meter_fraction(1e-6), 0.0)


class LoopbackAnnouncementTests(TestCase):
    """Πραγματικός AudioBroadcaster και TCP Client στο loopback, ψεύτικο μικρόφωνο."""

    def setUp(self):
        import socket
        import uuid
        from director.broadcast import AudioBroadcaster
        from director.main import accept_clients
        from shared.audio import BLOCK_BYTES
        from shared.identity import receive_welcome, send_hello

        self.broadcaster = AudioBroadcaster()
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.stop = threading.Event()
        self.listener = threading.Thread(target=accept_clients,
                                         args=(self.server, self.broadcaster, self.stop),
                                         daemon=True)
        self.listener.start()

        def speaking_stream(gate, *, stop, ready_callback, level_callback, **_kwargs):
            ready_callback()
            while not stop.wait(0.02):
                gate.publish(bytes([7]) * BLOCK_BYTES, time.monotonic_ns() // 1000)

        self.mic = DirectorMicrophone(speaking_stream, self.broadcaster)
        self.client = socket.create_connection(self.server.getsockname(), timeout=2)
        self.client.settimeout(3)
        send_hello(self.client, str(uuid.UUID(int=1)), "C2627-Γ1")
        receive_welcome(self.client)
        self.assertTrue(wait_until(lambda: self.broadcaster.client_count == 1))

    def tearDown(self):
        self.mic.shutdown()
        self.stop.set()
        self.listener.join(timeout=1)
        self.broadcaster.close()
        self.client.close()
        self.server.close()

    def messages(self, seconds):
        from shared.audio import Frame, receive_message
        kinds = []
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            message = receive_message(self.client, deadline=time.monotonic() + 2)
            kinds.append("pcm" if isinstance(message, Frame) else message.kind)
        return kinds

    def test_idle_mute_and_stop_sequence_on_the_wire(self):
        from shared.audio import MESSAGE_KEEPALIVE, MESSAGE_STREAM_STOP
        idle = self.messages(0.7)
        self.assertNotIn("pcm", idle)
        self.assertNotIn(MESSAGE_STREAM_STOP, idle)

        self.mic.start_announcement(None)
        live = self.messages(0.3)
        self.assertIn("pcm", live)

        self.mic.set_muted(True)
        self.messages(0.1)          # ό,τι ήταν ήδη στην ουρά αποστολής
        muted = self.messages(1.2)
        self.assertNotIn("pcm", muted)
        self.assertNotIn(MESSAGE_STREAM_STOP, muted)   # η ανακοίνωση συνεχίζει
        self.assertIn(MESSAGE_KEEPALIVE, muted)        # και η σύνδεση ζει

        self.mic.set_muted(False)
        self.assertIn("pcm", self.messages(0.3))

        self.mic.stop_announcement()
        after = self.messages(0.8)
        self.assertIn(MESSAGE_STREAM_STOP, after)
        self.assertNotIn("pcm", after[after.index(MESSAGE_STREAM_STOP):])
