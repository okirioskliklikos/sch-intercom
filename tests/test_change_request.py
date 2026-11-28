"""5C2: αίτημα αλλαγής αίθουσας/ονόματος (πρωτόκολλο v8, Director, Client, διόρθωση εγγραφής)."""
import json
import queue
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import uuid

from shared import change_request as cr
from shared.control import (CHANGE_REQUEST, CHANGE_LENGTH, HEARTBEAT, encode_change_request,
                            heartbeat_receiver, heartbeat_sender)
from shared.identity import CONNECTION_PROTOCOL
from client.changes import ChangeController, INBOX_FILE, watch_inbox
from client import status as st

CID = str(uuid.uuid4())


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class ControlProtocolTests(unittest.TestCase):
    def setUp(self):
        self.left, self.right = socket.socketpair()
        self.right.settimeout(0.2)
        self.addCleanup(self.left.close)
        self.addCleanup(self.right.close)

    def receive(self, data: bytes, *, callback=True):
        beats, changes = [], []
        stop = threading.Event()

        def on_change(room, name):
            changes.append((room, name))

        def run():
            try:
                heartbeat_receiver(self.right, stop, beats.append, timeout=5,
                                   on_change_request=on_change if callback else None)
            except Exception as exc:  # noqa: BLE001
                result.append(exc)
        result = []
        worker = threading.Thread(target=run)
        worker.start()
        for index in range(0, len(data), 3):      # τμηματικά, όπως σε TCP
            self.left.sendall(data[index:index + 3])
            time.sleep(0.005)
        time.sleep(0.2)
        self.left.close()
        worker.join(3)
        return beats, changes, result

    def test_protocol_is_8(self):
        self.assertEqual(CONNECTION_PROTOCOL, 8)

    def test_change_request_between_heartbeats(self):
        frame = encode_change_request("C2627-Γ2", "Board 1")
        self.assertTrue(frame.startswith(CHANGE_REQUEST))
        beats, changes, result = self.receive(HEARTBEAT + frame + HEARTBEAT)
        self.assertEqual(changes, [("C2627-Γ2", "Board 1")])
        self.assertEqual(len(beats), 3)            # το αίτημα μετρά και ως heartbeat
        self.assertIsInstance(result[0], EOFError)

    def test_old_director_without_callback_rejects(self):
        _beats, changes, result = self.receive(encode_change_request("C2627-Γ2", "Board1"),
                                               callback=False)
        self.assertEqual(changes, [])
        self.assertIsInstance(result[0], ValueError)

    def test_invalid_length_or_payload_closes(self):
        for data in (CHANGE_REQUEST + CHANGE_LENGTH.pack(0),
                     CHANGE_REQUEST + CHANGE_LENGTH.pack(cr.MAX_PAYLOAD + 1),
                     CHANGE_REQUEST + CHANGE_LENGTH.pack(4) + b"nope"):
            with self.subTest(data=data[:8]):
                self.setUp()
                _beats, changes, result = self.receive(data)
                self.assertEqual(changes, [])
                self.assertIsInstance(result[0], ValueError)

    def test_sender_writes_outbox_from_heartbeat_thread(self):
        outbox = queue.Queue()
        stop = threading.Event()
        worker = threading.Thread(target=heartbeat_sender, args=(self.left, stop),
                                  kwargs={"interval": 0.05, "outbox": outbox})
        worker.start()
        frame = encode_change_request("C2627-Γ2", "Board1")
        outbox.put(frame)
        time.sleep(0.3)
        stop.set()
        worker.join(2)
        data = b""
        while True:
            try:
                piece = self.right.recv(4096)
            except socket.timeout:
                break
            if not piece:
                break
            data += piece
        self.assertIn(frame, data)
        self.assertGreaterEqual(data.count(HEARTBEAT), 2)

    def test_status_round_trip(self):
        payload = cr.encode_status(cr.PENDING, "C2627-Γ2", "Board1")
        self.assertEqual(cr.decode_status(payload),
                         {"state": "pending", "room": "C2627-Γ2", "name": "Board1"})
        self.assertEqual(cr.decode_status(cr.encode_status(cr.NONE))["state"], "none")
        with self.assertRaises(ValueError):
            cr.encode_status("maybe", "a", "b")


class DirectorStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / cr.DIRECTOR_FILE
        self.clock = Clock()
        self.applied, self.notified = [], []
        self.store = self.make()

    def make(self):
        return cr.ChangeRequests(self.path, apply=lambda *a: self.applied.append(a),
                                 notify=lambda cid, s: self.notified.append((cid, s)),
                                 clock=self.clock, log=lambda _l: None)

    def submit(self, room="C2627-Γ2", name="Board1"):
        return self.store.submit(CID, room, name, current_room="C2627-Γ1",
                                 current_name="Board1")

    def test_submit_persist_and_approve(self):
        self.assertEqual(self.submit()["state"], cr.PENDING)
        self.assertEqual(self.make().status_for(CID),
                         {"state": "pending", "room": "C2627-Γ2", "name": "Board1"})
        [request] = self.store.pending()
        self.assertEqual((request.old_room, request.room), ("C2627-Γ1", "C2627-Γ2"))
        decided = self.store.decide(CID, approve=True)
        self.assertEqual(decided.state, cr.APPROVED)
        self.assertEqual(self.applied, [(CID, "C2627-Γ2", "Board1")])
        self.assertEqual(self.notified[-1][1]["state"], cr.APPROVED)
        self.assertEqual(self.store.pending(), [])
        with self.assertRaises(cr.ChangeRequestError):
            self.store.decide(CID, approve=True)

    def test_one_request_per_pc_and_same_values_cancel(self):
        self.submit("C2627-Γ2")
        self.submit("C2627-Γ3")
        [request] = self.store.pending()
        self.assertEqual(request.room, "C2627-Γ3")
        self.assertEqual(self.submit("C2627-Γ1")["state"], cr.APPROVED)  # ίδια με το μητρώο
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.status_for(CID)["state"], cr.NONE)

    def test_deny_visible_one_day_then_forgotten(self):
        self.submit()
        self.store.decide(CID, approve=False)
        self.assertEqual(self.applied, [])
        self.assertEqual(self.make().status_for(CID)["state"], cr.DENIED)
        self.clock.now += cr.REQUEST_TTL
        self.assertEqual(self.store.status_for(CID)["state"], cr.NONE)

    def test_pending_expires_after_one_day(self):
        self.submit()
        self.clock.now += cr.REQUEST_TTL - 1
        self.assertEqual(len(self.store.pending()), 1)
        self.clock.now += 1
        self.assertEqual(self.store.pending(), [])

    def test_resolve_prefix_and_forget(self):
        self.submit()
        self.assertEqual(self.store.resolve(CID[:8]), CID)
        with self.assertRaises(cr.ChangeRequestError):
            self.store.resolve("ab")
        self.store.forget(CID)
        self.assertEqual(self.make().pending(), [])

    def test_invalid_values_rejected(self):
        for room, name in (("", "Board1"), ("C2627-Γ1", " x"), ("a\x01", "b"),
                           ("C2627-Γ1", "x" * 65)):
            with self.subTest(room=room, name=name), self.assertRaises(ValueError):
                self.submit(room, name)


class ClientControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.path = self.folder / cr.CLIENT_FILE
        self.status = st.ClientStatus(room="C2627-Γ1", name="Board1")
        self.lines = []
        self.controller = ChangeController(self.path, CID, status=self.status,
                                           log=self.lines.append)
        self.controller.set_current("C2627-Γ1", "Board1")

    def sent(self, outbox):
        items = []
        while not outbox.empty():
            items.append(outbox.get_nowait())
        return items

    def test_offline_request_queued_then_sent_on_none(self):
        self.assertEqual(self.controller.request("C2627-Γ2", "Board1"), cr.QUEUED)
        self.assertEqual(self.status.snapshot()["change"]["state"], cr.QUEUED)
        self.assertEqual(cr.load_local(self.path, CID)["state"], cr.QUEUED)
        outbox = queue.Queue()
        self.controller.connected(outbox)
        self.assertEqual(self.sent(outbox), [])          # περιμένει την απάντηση σύνδεσης
        self.controller.on_status({"state": "none", "room": "", "name": ""})
        self.assertEqual(self.sent(outbox), [encode_change_request("C2627-Γ2", "Board1")])
        self.controller.on_status({"state": "pending", "room": "C2627-Γ2", "name": "Board1"})
        self.assertEqual(cr.load_local(self.path, CID)["state"], cr.PENDING)
        _alarm, info = st.window_bars(self.status.snapshot())
        self.assertIn("αναμονή έγκρισης από τον Director", info)

    def test_online_request_sent_immediately_and_approved_clears(self):
        outbox = queue.Queue()
        self.controller.connected(outbox)
        self.controller.request("C2627-Γ2", "Board1")
        self.assertEqual(len(self.sent(outbox)), 1)
        self.controller.on_status({"state": "approved", "room": "C2627-Γ2", "name": "Board1"})
        self.assertIsNone(cr.load_local(self.path, CID))
        self.assertIsNone(self.status.snapshot()["change"])

    def test_denied_shown_until_director_forgets(self):
        self.controller.connected(queue.Queue())
        self.controller.request("C2627-Γ2", "Board1")
        self.controller.on_status({"state": "denied", "room": "C2627-Γ2", "name": "Board1"})
        alarm, _info = st.window_bars(self.status.snapshot())
        self.assertIn("απορρίφθηκε", alarm)
        self.assertEqual(ChangeController(self.path, CID).local["state"], cr.DENIED)
        self.controller.on_status({"state": "none", "room": "", "name": ""})
        self.assertIsNone(self.status.snapshot()["change"])

    def test_same_as_current_is_not_a_request(self):
        self.assertEqual(self.controller.request("C2627-Γ1", "Board1"), cr.APPROVED)
        self.assertIsNone(cr.load_local(self.path, CID))

    def test_failure_only_on_screen(self):
        self.controller.fail("C2627-Γ2", "Board1", "δεν βρέθηκε Director")
        alarm, _info = st.window_bars(self.status.snapshot())
        self.assertIn("δεν στάλθηκε: δεν βρέθηκε Director", alarm)
        self.assertIsNone(cr.load_local(self.path, CID))

    def test_reset_for_new_identity(self):
        self.controller.request("C2627-Γ2", "Board1")
        other = str(uuid.uuid4())
        self.controller.reset(other)
        self.assertIsNone(self.controller.local)
        self.assertIsNone(ChangeController(self.path, other).local)

    def test_inbox_for_command_line(self):
        ChangeController.write_inbox(self.folder, CID, "C2627-Γ2", "Board1")
        self.assertEqual(ChangeController.take_inbox(self.folder, CID), ("C2627-Γ2", "Board1"))
        self.assertFalse((self.folder / INBOX_FILE).exists())
        ChangeController.write_inbox(self.folder, CID, "C2627-Γ2", "Board1")
        self.assertIsNone(ChangeController.take_inbox(self.folder, str(uuid.uuid4())))
        with self.assertRaises(ValueError):
            ChangeController.write_inbox(self.folder, CID, "", "Board1")

    def test_watcher_hands_inbox_to_handler(self):
        handled = []
        stop = threading.Event()
        worker = threading.Thread(target=watch_inbox,
                                  args=(self.folder, lambda: CID,
                                        lambda r, n: handled.append((r, n)), stop),
                                  kwargs={"interval": 0.02})
        worker.start()
        ChangeController.write_inbox(self.folder, CID, "C2627-Γ3", "PC2")
        deadline = time.monotonic() + 3
        while not handled and time.monotonic() < deadline:
            time.sleep(0.02)
        stop.set()
        worker.join(2)
        self.assertEqual(handled, [("C2627-Γ3", "PC2")])


class ChangeModeTests(unittest.TestCase):
    def snap(self, state, **extra):
        status = st.ClientStatus(**extra)
        status.set_state(state)
        return status.snapshot()

    def test_mode_follows_state(self):
        self.assertEqual(st.change_mode(self.snap(st.CONNECTED)), st.MODE_CHANGE)
        self.assertEqual(st.change_mode(self.snap(st.SEARCHING, approved_before=True)),
                         st.MODE_CHANGE)
        self.assertEqual(st.change_mode(self.snap(st.PENDING, enrollment={"code": "1"})),
                         st.MODE_AMEND)
        self.assertEqual(st.change_mode(self.snap(st.REVOKED, approved_before=True)),
                         st.MODE_ENROLL)
        self.assertEqual(st.change_mode(self.snap(st.UNAPPROVED)), st.MODE_ENROLL)

    def test_pending_file_keeps_request_id_and_seq(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / st.PENDING_FILE
            st.save_pending(path, code="482913", room="R", name="N", expires_at=time.time() + 60,
                            client_id=CID, request_id="rid", seq=3)
            data = st.load_pending(path, CID)
            self.assertEqual((data["request_id"], data["seq"]), ("rid", 3))
            raw = json.loads(path.read_text(encoding="utf-8"))
            del raw["request_id"], raw["seq"]               # αρχείο πριν από το 5C2
            path.write_text(json.dumps(raw), encoding="utf-8")
            data = st.load_pending(path, CID)
            self.assertEqual((data["request_id"], data["seq"]), (None, 0))


if __name__ == "__main__":
    unittest.main()
