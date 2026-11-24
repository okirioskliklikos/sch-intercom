"""4.3B: σενάρια σφαλμάτων από docs/phase4-3.md §4.1, με πραγματικά loopback sockets.

Κανένα μικρόφωνο/ηχείο: η κάρτα ήχου και ο έλεγχος έντασης Windows είναι ψεύτικα,
ό,τι άλλο (TLS, broadcaster, accept_clients, pump_frames, AudioPolicyWorker) είναι
ο πραγματικός κώδικας.
"""
from __future__ import annotations

import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock
import uuid

from client.main import pump_frames, play_stream
from client.windows_volume import AudioPolicyWorker
from director.backup import BackupLocked, manager_lock
from director.broadcast import AudioBroadcaster
from director.main import RegistryTrust, accept_clients, stream_test_tone
from shared.audio import BLOCK_BYTES, Frame, encode_frame, receive_message
from shared.client_registry import SignedClientRegistry
from shared.control import heartbeat_sender
from shared.device_identity import create_device_identity
from shared.identity import receive_welcome, send_hello
from shared.jitter import DelayTracker, JitterBuffer
from shared.transport_security import DEFAULT_SERVER_NAME, client_context
from tests.tls_support import LocalTLSFixture

ROOT = Path(__file__).resolve().parents[1]
ROOM = "C2627-Γ1"


def wait_until(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class CountingController:
    """Ψεύτικος έλεγχος έντασης Windows: θυμάται την ένταση και τις κλήσεις."""

    def __init__(self, original=0.3, announcement=0.8):
        self.original, self.announcement = original, announcement
        self.volume = original
        self.begins = self.ends = 0
        self.lock = threading.Lock()

    def begin(self):
        with self.lock:
            self.begins += 1
            self.volume = self.announcement
        return True

    def end(self):
        with self.lock:
            self.ends += 1
            self.volume = self.original


class FakeOutputStream:
    """Αντί για sounddevice.RawOutputStream: καλεί το callback ανά 20 ms σαν κάρτα ήχου."""

    def __init__(self, *, callback, **_kwargs):
        self.callback = callback
        self.latency = 0.02
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        out = bytearray(BLOCK_BYTES)
        while not self.stop.wait(0.02):
            self.callback(out, BLOCK_BYTES // 2, None, None)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(1)
        return False


FAKE_SOUNDDEVICE = types.SimpleNamespace(RawOutputStream=FakeOutputStream)


class LossDuringAnnouncementTests(unittest.TestCase):
    """Σ3: ο Manager χάνεται στη μέση ανακοίνωσης ενώ η ένταση είναι ανεβασμένη."""

    def run_client(self, connection):
        controller = CountingController()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with mock.patch.dict(sys.modules, {"sounddevice": FAKE_SOUNDDEVICE}), \
                mock.patch("client.main.create_volume_controller",
                           return_value=controller), \
                mock.patch("builtins.print"):
            started = time.monotonic()
            with self.assertRaises((OSError, EOFError)) as caught:
                play_stream(connection, announcement_volume=80,
                            recovery_path=Path(temp.name) / "volume-recovery.json")
        return controller, time.monotonic() - started, caught.exception

    def send_announcement(self, sender, blocks=25):
        for seq in range(blocks):
            sender.sendall(encode_frame(bytes([seq % 256]) * BLOCK_BYTES, seq, 0))

    def test_connection_closed_mid_announcement_restores_volume_once(self):
        sender, receiver = socket.socketpair()
        self.addCleanup(receiver.close)
        receiver.settimeout(3.0)

        def manager():
            self.send_announcement(sender)
            time.sleep(0.3)           # η ανακοίνωση παίζει…
            sender.close()            # …και ο Manager «πεθαίνει» χωρίς STREAM_STOP
        threading.Thread(target=manager, daemon=True).start()
        controller, elapsed, error = self.run_client(receiver)
        self.assertIsInstance(error, EOFError)
        self.assertEqual((controller.begins, controller.ends), (1, 1))
        self.assertEqual(controller.volume, controller.original)
        self.assertLess(elapsed, 2.0)

    def test_silent_manager_mid_announcement_restores_within_timeout(self):
        sender, receiver = socket.socketpair()
        self.addCleanup(receiver.close)
        self.addCleanup(sender.close)
        receiver.settimeout(0.8)      # στην πράξη 3 s (RECEIVE_TIMEOUT_SECONDS)

        def manager():
            self.send_announcement(sender)
            # Μετά κανένα byte, ούτε KEEPALIVE, και η σύνδεση μένει ανοιχτή (καλώδιο).
        threading.Thread(target=manager, daemon=True).start()
        controller, elapsed, error = self.run_client(receiver)
        self.assertIsInstance(error, (socket.timeout, TimeoutError))
        self.assertEqual((controller.begins, controller.ends), (1, 1))
        self.assertEqual(controller.volume, controller.original)
        self.assertLess(elapsed, 0.8 + 1.5)


LOCK_HOLDER = """
import sys, time
from pathlib import Path
from director.backup import manager_lock
with manager_lock(Path(sys.argv[1])):
    print("locked", flush=True)
    time.sleep(60)
"""


class ManagerKillTests(unittest.TestCase):
    """Σ3: μετά από kill του Manager το manager.lock ελευθερώνεται χωρίς παρέμβαση."""

    def test_killed_process_releases_manager_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            holder = subprocess.Popen([sys.executable, "-c", LOCK_HOLDER, folder],
                                      cwd=ROOT, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(), "locked")
                with self.assertRaises(BackupLocked):
                    with manager_lock(Path(folder)):
                        pass
                holder.kill()             # SIGKILL / TerminateProcess, όχι κανονικό κλείσιμο
                holder.wait(10)
                with manager_lock(Path(folder)):
                    pass                  # ο νέος Manager ξεκινά
            finally:
                if holder.poll() is None:
                    holder.kill()
                    holder.wait(10)
                holder.stdout.close()


class RegistryFixture(LocalTLSFixture):
    """Πραγματικό mTLS με υπογεγραμμένο μητρώο και RegistryTrust, όπως στην παραγωγή."""

    def setUp(self):
        super().setUp()
        directory = Path(self._tls_temp.name)
        self.director_cert = directory / "director.crt"
        self.director_key = directory / "director.key"
        self.registry = SignedClientRegistry(directory / "registry.json",
                                             self.director_cert, self.director_key)
        self.broadcast = AudioBroadcaster()
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(16)
        self.stop = threading.Event()
        self.listener = None
        self.sockets = []

    def tearDown(self):
        self.stop.set()
        if self.listener is not None:
            self.listener.join(timeout=2)
        for item in self.sockets:
            item.close()
        self.broadcast.close()
        self.server.close()
        super().tearDown()

    def device(self, label):
        """Εγκεκριμένη συσκευή· πριν από το listen(), όπως ένας ήδη εγγεγραμμένος Client."""
        client_id = str(uuid.uuid4())
        cert, key, password = create_device_identity(
            Path(self._tls_temp.name) / label, client_id)
        self.registry.approve(client_id, ROOM, cert.read_bytes())
        return client_id, client_context(self.director_cert, certfile=cert,
                                         keyfile=key, password=password)

    def listen(self):
        self.trust = RegistryTrust(self.registry, self.director_cert, self.director_key)
        self.listener = threading.Thread(
            target=accept_clients, args=(self.server, self.broadcast, self.stop),
            kwargs={"tls_context": self.trust.context(),
                    "context_provider": self.trust.context, "registry": self.registry},
            daemon=True)
        self.listener.start()

    def join(self, client_id, context, *, session=None):
        plain = socket.create_connection(self.server.getsockname(), timeout=3)
        try:
            connection = context.wrap_socket(plain, server_hostname=DEFAULT_SERVER_NAME,
                                             session=session)
        except BaseException:
            plain.close()
            raise
        connection.settimeout(3)
        self.sockets.append(connection)
        send_hello(connection, client_id, ROOM)
        receive_welcome(connection)
        return connection


class RevokeDuringAnnouncementTests(RegistryFixture, unittest.TestCase):
    """Σ5: ανάκληση στη μέση ανακοίνωσης με δύο Clients."""

    def test_revoked_client_is_cut_and_other_has_no_gap(self):
        revoked_id, revoked_ctx = self.device("revoked")
        other_id, other_ctx = self.device("other")
        self.listen()
        revoked = self.join(revoked_id, revoked_ctx)
        other = self.join(other_id, other_ctx)
        self.assertTrue(wait_until(lambda: self.broadcast.client_count == 2))
        stop = threading.Event()

        def microphone():
            while not stop.is_set():
                self.broadcast.publish(bytes(BLOCK_BYTES), 0)
                time.sleep(0.005)
        mic = threading.Thread(target=microphone, daemon=True)
        mic.start()
        self.addCleanup(stop.set)
        received = []
        revoked_at = None
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            message = receive_message(other)
            if isinstance(message, Frame):
                received.append(message.seq)
            if len(received) == 50 and revoked_at is None:
                self.trust.revoke(revoked_id, self.broadcast)
                revoked_at = time.monotonic()
            if len(received) >= 200:
                break
        stop.set()
        mic.join(1)
        self.assertIsNotNone(revoked_at)
        self.assertGreaterEqual(len(received), 200)
        # Η σειρά είναι κοινή για όλους: κανένα κενό = κανένα χαμένο block στον άλλον.
        self.assertEqual(received, list(range(received[0], received[0] + len(received))))
        self.assertEqual(self.broadcast.clients_snapshot(), [(other_id, ROOM)])
        # Ο ανακλημένος βλέπει τέλος σύνδεσης σε ≤1 s, όχι απλώς σιωπή.
        revoked.settimeout(1.0)
        with self.assertRaises((OSError, EOFError, ValueError)):
            while True:
                receive_message(revoked)

    def test_tls_session_from_before_revoke_cannot_resume(self):
        client_id, context = self.device("resume")
        self.listen()
        first = self.join(client_id, context)
        self.assertTrue(wait_until(lambda: self.broadcast.client_count == 1))
        # Τα TLS 1.3 tickets έρχονται μετά το handshake· το WELCOME τα έχει ήδη φέρει.
        session = first.session
        self.assertIsNotNone(session)
        second = self.join(client_id, context, session=session)
        # Πριν την ανάκληση η επανάληψη συνόδου λειτουργεί, άρα ο έλεγχος έχει νόημα.
        self.assertTrue(second.session_reused)
        self.trust.revoke(client_id, self.broadcast)
        self.assertTrue(wait_until(lambda: self.broadcast.client_count == 0))
        with self.assertRaises((OSError, EOFError, ValueError, ssl.SSLError)):
            self.join(client_id, context, session=session)
        time.sleep(0.1)
        self.assertEqual(self.broadcast.client_count, 0)


class RapidAnnouncementTests(LocalTLSFixture, unittest.TestCase):
    """Σ8: 20 γρήγορες ανακοινώσεις broadcaster→TLS→pump_frames με πραγματικό worker."""

    def test_twenty_fast_cycles_keep_connection_and_restore_once_at_end(self):
        broadcast = AudioBroadcaster()
        self.addCleanup(broadcast.close)
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        self.addCleanup(server.close)
        stop = threading.Event()
        self.addCleanup(stop.set)
        threading.Thread(target=accept_clients, args=(server, broadcast, stop),
                         kwargs={"tls_context": self.tls_server_context},
                         daemon=True).start()
        broadcast.set_targets(rooms=set())        # αρχικά κανείς («none»)
        client = self.connect_tls_client(server.getsockname())
        self.addCleanup(client.close)
        send_hello(client, str(uuid.uuid4()), ROOM)
        receive_welcome(client)
        self.assertTrue(wait_until(lambda: broadcast.client_count == 1))
        beats = threading.Event()
        threading.Thread(target=heartbeat_sender, args=(client, beats), daemon=True).start()
        self.addCleanup(beats.set)

        controller = CountingController()
        worker = AudioPolicyWorker(lambda: controller, log=lambda _: None)
        self.addCleanup(worker.close)
        jitter = JitterBuffer(50, BLOCK_BYTES, prefill_blocks=3)
        jitter.standby()
        card = threading.Event()

        def sound_card():
            while not card.wait(0.02):
                jitter.pop()
        threading.Thread(target=sound_card, daemon=True).start()
        self.addCleanup(card.set)
        failure = []

        def receive():
            try:
                pump_frames(client, jitter, DelayTracker(), volume=worker)
            except Exception as exc:  # noqa: BLE001 - καταγράφεται για το assert
                failure.append(exc)
        receiver = threading.Thread(target=receive, daemon=True)
        receiver.start()

        def publish_for(seconds):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                broadcast.publish(bytes(BLOCK_BYTES), 0)
                time.sleep(0.02)
        for _ in range(20):
            broadcast.set_targets()               # «all»: έναρξη
            publish_for(0.2)
            broadcast.set_targets(rooms=set())    # «none»: STREAM_STOP
            publish_for(0.3)                      # < 1,5 s επαναφοράς
        self.assertTrue(worker.active)            # η επαναφορά δεν έγινε ενδιάμεσα
        publish_for(0.2)                          # τελευταίο STOP φεύγει
        # Αναμονή για drain + 1,5 s + λίγο. Τα KEEPALIVE κρατούν ζωντανό το pump.
        self.assertTrue(wait_until(lambda: not worker.active, timeout=4))
        self.assertTrue(worker.wait_idle(2))
        self.assertEqual(failure, [])
        self.assertEqual(broadcast.client_count, 1)
        self.assertEqual(controller.volume, controller.original)
        # Ο worker ενώνει ενδιάμεσα begin/end· στο τέλος ισορροπούν.
        self.assertEqual(controller.begins, controller.ends)
        self.assertGreaterEqual(controller.begins, 1)


class ConnectionLimitTests(RegistryFixture, unittest.TestCase):
    """Σ7: ο επιπλέον Client μπαίνει μόλις φύγει κάποιος· Σ6: ίδιο UUID εναλλάξ."""

    def test_extra_client_joins_on_next_attempt_after_one_leaves(self):
        self.broadcast.max_clients = 2
        ids = [self.device(f"c{n}") for n in range(3)]
        self.listen()
        first = self.join(*ids[0])
        self.join(*ids[1])
        self.assertTrue(wait_until(lambda: self.broadcast.client_count == 2))
        extra = self.join(*ids[2])          # WELCOME και μετά κλείσιμο (§5.2)
        extra.settimeout(2)
        self.assertEqual(extra.recv(1), b"")
        self.assertEqual(self.broadcast.client_count, 2)
        first.close()
        self.assertTrue(wait_until(lambda: self.broadcast.client_count == 1, timeout=8))
        self.join(*ids[2])                  # επόμενη προσπάθεια του Client
        self.assertTrue(wait_until(lambda: self.broadcast.client_count == 2))
        self.assertIn(ids[2][0], [cid for cid, _ in self.broadcast.clients_snapshot()])

    def test_same_identity_from_two_connections_keeps_replacing(self):
        """Κλωνοποιημένος δίσκος (§5.3): κάθε σύνδεση πετά έξω την προηγούμενη."""
        client_id, context = self.device("clone")
        self.listen()
        previous = None
        for _ in range(6):
            current = self.join(client_id, context)
            if previous is not None:
                previous.settimeout(2)
                with self.assertRaises((OSError, EOFError, ValueError)):
                    while True:
                        receive_message(previous)
            self.assertTrue(wait_until(lambda: self.broadcast.client_count == 1))
            previous = current
        status = self.broadcast.status_snapshot()
        self.assertEqual([(cid, online) for cid, _, online, _ in status],
                         [(client_id, True)])


class TestToneTests(unittest.TestCase):
    """Η κρυφή πηγή δοκιμής του tools/failure_live_test: πραγματικός χρόνος, έγκυρα blocks."""

    def test_tone_publishes_full_blocks_in_real_time(self):
        blocks = []

        class Recorder:
            def publish(self, pcm, timestamp_us):
                blocks.append(pcm)

        stop = threading.Event()
        threading.Timer(0.5, stop.set).start()
        with mock.patch("builtins.print"):
            stream_test_tone(Recorder(), stop=stop)
        self.assertTrue(18 <= len(blocks) <= 32, len(blocks))   # ~25 blocks των 20 ms
        self.assertTrue(all(len(block) == BLOCK_BYTES for block in blocks))
        samples = memoryview(blocks[3]).cast("h")
        self.assertTrue(0 < max(abs(value) for value in samples) <= 800)


if __name__ == "__main__":
    unittest.main()
