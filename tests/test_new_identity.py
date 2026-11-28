"""5C3: «Νέα ταυτότητα στο <IP>» από τον Director και κλώνοι με σύσταση (phase5.md §6.6, §6.7)."""
from pathlib import Path
import socket
import tempfile
import threading
import unittest
import uuid

from shared.ui.offscreen import use_offscreen

use_offscreen()

from client import status as st  # noqa: E402
from client.identity_reset import old_identity  # noqa: E402
from director.broadcast import AudioBroadcaster  # noqa: E402
from director.security import (KIND_CLONE, KIND_ENROLL, KIND_RESET, TAG_CLONE,  # noqa: E402
                               TAG_RESET, TEXT_NAME_NEEDED, new_identity_action)
from shared.audio import (MESSAGE_NEW_IDENTITY, decode_new_identity,  # noqa: E402
                          encode_new_identity, receive_message)
from shared.client_registry import RegistryError  # noqa: E402
from shared.device_identity import create_device_identity  # noqa: E402
from shared.enrollment import (SPONSOR_CLONE, SPONSOR_RESET, IncorrectCodeError,  # noqa: E402
                               enroll_client, make_request, make_sponsor)
from tests.test_security_page import SecurityFixture  # noqa: E402

SERVER_NAME = "school-intercom-director"
TOKEN = "0123456789abcdef0123456789abcdef"


class SponsorFixture(SecurityFixture):
    """Μία εγκεκριμένη ταυτότητα «Πρότυπο» και υποβολή νέων αιτημάτων με σύσταση."""

    def setUp(self):
        super().setUp()
        self.origin_id, code = self.submit("C2627-Εργ", "Πρότυπο", device="origin")
        self.assertTrue(self.model.approve(self.request_id("C2627-Εργ", "Πρότυπο"), code).ok)
        _cid, self.origin_cert, self.origin_key, self.origin_password = self.devices["origin"]

    def sponsored(self, kind=SPONSOR_CLONE, *, token="", room="C2627-Εργ", name="HOST-7",
                  key=None, password=None, origin=None):
        client_id = str(uuid.uuid4())
        cert, keyfile, pw = create_device_identity(self.root / client_id[:8], client_id)
        request = make_request(client_id, room, name, cert.read_bytes(), keyfile.read_bytes(), pw)
        request = make_sponsor(request, kind, origin or self.origin_id, token,
                               (key or self.origin_key).read_bytes(),
                               password or self.origin_password)
        queued = enroll_client("127.0.0.1", self.enrollment.port, self.cert, SERVER_NAME,
                               request)
        return client_id, request["request_id"], queued


class EncodingTests(unittest.TestCase):
    def test_new_identity_message_roundtrip_and_validation(self):
        frame = encode_new_identity("C2627-Γ1", "Board2", TOKEN)
        a, b = socket.socketpair()
        try:
            a.sendall(frame)
            message = receive_message(b)
        finally:
            a.close()
            b.close()
        self.assertEqual(message.kind, MESSAGE_NEW_IDENTITY)
        self.assertEqual(decode_new_identity(message.payload),
                         {"room": "C2627-Γ1", "name": "Board2", "token": TOKEN})
        for room, name, token in (("C2627-Γ1", "", TOKEN), ("C2627-Γ1", "B", "short"),
                                  ("", "B", TOKEN)):
            with self.assertRaises(ValueError):
                encode_new_identity(room, name, token)

    def test_pump_frames_calls_new_identity(self):
        from client.main import pump_frames
        from shared.audio import BLOCK_BYTES
        from shared.jitter import DelayTracker, JitterBuffer
        a, b = socket.socketpair()
        received = []
        try:
            a.sendall(encode_new_identity("C2627-Γ1", "Board2", TOKEN))
            a.close()
            with self.assertRaises(EOFError):
                pump_frames(b, JitterBuffer(12, BLOCK_BYTES, prefill_blocks=6), DelayTracker(),
                            report=None, new_identity=received.append)
        finally:
            b.close()
        self.assertEqual(received, [{"room": "C2627-Γ1", "name": "Board2", "token": TOKEN}])


class CloneSponsorTests(SponsorFixture, unittest.TestCase):
    def test_clone_approved_without_code_and_origin_recorded(self):
        client_id, rid, queued = self.sponsored()
        self.assertTrue(queued["sponsored"])
        [view] = [r for r in self.model.requests() if r.request_id == rid]
        self.assertEqual((view.kind, view.origin_room, view.origin_name),
                         (KIND_CLONE, "C2627-Εργ", "Πρότυπο"))
        self.assertEqual(self.model.approve_sponsored(rid, "C2627-Εργ", "").text,
                         TEXT_NAME_NEEDED)
        self.assertTrue(self.model.approve_sponsored(rid, "C2627-Εργ", "PC05").ok)
        entry = self.registry.approved_entry(client_id)
        self.assertEqual((entry["room"], entry["name"], entry["origin"]),
                         ("C2627-Εργ", "PC05", self.origin_id))
        self.assertEqual([e["client_id"] for e in self.registry.derived(self.origin_id)],
                         [client_id])
        self.assertEqual([c.name for c in self.model.derived(self.origin_id)], ["PC05"])

    def test_sponsor_survives_restart_of_pending_file(self):
        _client_id, rid, _queued = self.sponsored()
        from shared.enrollment import EnrollmentServer
        again = EnrollmentServer(self.cert, self.key, self.trust, host="127.0.0.1", port=0,
                                 pending_file=self.root / "pending.json", log=lambda _m: None)
        [item] = [i for i in again.pending_details() if i["request_id"] == rid]
        self.assertTrue(item["sponsored"])
        self.assertEqual(item["sponsor"]["kind"], SPONSOR_CLONE)

    def test_revoked_origin_needs_code(self):
        self.model.revoke(self.origin_id)
        _client_id, rid, queued = self.sponsored()
        self.assertFalse(queued["sponsored"])
        [view] = [r for r in self.model.requests() if r.request_id == rid]
        self.assertEqual(view.kind, KIND_ENROLL)
        with self.assertRaises(IncorrectCodeError):
            self.enrollment.decide(rid, None, approve=True)

    def test_origin_revoked_while_waiting_falls_back_to_code(self):
        _client_id, rid, _queued = self.sponsored()
        self.model.revoke(self.origin_id)
        decision = self.model.approve_sponsored(rid, "C2627-Εργ", "PC05")
        self.assertFalse(decision.ok)

    def test_forged_sponsor_signature_ignored(self):
        other = str(uuid.uuid4())
        _cert, okey, opass = create_device_identity(self.root / "other", other)
        _client_id, rid, queued = self.sponsored(key=okey, password=opass)
        self.assertFalse(queued["sponsored"])
        self.assertEqual([r.kind for r in self.model.requests() if r.request_id == rid],
                         [KIND_ENROLL])

    def test_revoke_with_derived(self):
        client_id, rid, _queued = self.sponsored()
        self.assertTrue(self.model.approve_sponsored(rid, "C2627-Εργ", "PC05").ok)
        self.model.revoke(self.origin_id, derived=(client_id,))
        self.assertIsNone(self.registry.approved_entry(client_id))
        self.assertEqual(self.registry.derived(self.origin_id), [])


class ResetTests(SponsorFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.broadcaster.send_new_identity = lambda *args: self.sent.append(args) or True
        self.sent = []

    def test_reset_needs_token_from_this_manager(self):
        _cid, _rid, queued = self.sponsored(SPONSOR_RESET, token=TOKEN)
        self.assertFalse(queued["sponsored"])
        self.assertTrue(self.model.new_identity(self.origin_id, "192.168.1.47",
                                                "C2627-Γ1", "Board2"))
        [(cid, ip, room, name, token)] = self.sent
        self.assertEqual((cid, ip, room, name), (self.origin_id, "192.168.1.47",
                                                 "C2627-Γ1", "Board2"))
        client_id, rid, queued = self.sponsored(SPONSOR_RESET, token=token,
                                                room="C2627-Γ1", name="Board2")
        self.assertTrue(queued["sponsored"])
        [view] = [r for r in self.model.requests() if r.request_id == rid]
        self.assertEqual((view.kind, view.room, view.name), (KIND_RESET, "C2627-Γ1", "Board2"))
        self.assertTrue(self.model.approve_sponsored(rid, view.room, view.name).ok)
        self.assertEqual(self.registry.approved_entry(client_id)["origin"], self.origin_id)
        # Το token δεν ξαναχρησιμοποιείται μετά την έγκριση.
        _cid, _rid, queued = self.sponsored(SPONSOR_RESET, token=token)
        self.assertFalse(queued["sponsored"])

    def test_new_identity_only_for_approved(self):
        with self.assertRaises(ValueError):
            self.model.new_identity(str(uuid.uuid4()), "10.0.0.1", "C2627-Γ1", "B")


class BroadcasterTests(unittest.TestCase):
    def make_session(self, broadcaster, client_id, ip):
        # Πραγματικό TCP (το socketpair δεν δέχεται TCP_NODELAY)· η IP του Client δηλώνεται.
        with socket.create_server(("127.0.0.1", 0)) as server:
            client = socket.create_connection(server.getsockname())
            director, _address = server.accept()
        self.addCleanup(client.close)
        broadcaster.add_client(director, client_id, "C2627-Γ1", (ip, 50000), name="Board1")
        return client

    def read_kinds(self, connection, wanted, timeout=3.0):
        connection.settimeout(timeout)
        kinds = []
        while wanted not in kinds:
            kinds.append(receive_message(connection).kind)
        return kinds

    def test_command_goes_only_to_that_ip(self):
        broadcaster = AudioBroadcaster()
        self.addCleanup(broadcaster.close)
        client_id = str(uuid.uuid4())
        first = self.make_session(broadcaster, client_id, "192.168.1.21")
        self.assertFalse(broadcaster.send_new_identity(client_id, "192.168.1.47", "C2627-Γ1",
                                                       "Board2", TOKEN))
        # Η 192.168.1.47 «κλέβει» τη σύνδεση: τότε παίρνει την εντολή.
        second = self.make_session(broadcaster, client_id, "192.168.1.47")
        self.assertIn(MESSAGE_NEW_IDENTITY, self.read_kinds(second, MESSAGE_NEW_IDENTITY))
        first.settimeout(0.3)
        kinds = []
        try:
            while True:
                kinds.append(receive_message(first).kind)
        except (OSError, EOFError, ValueError):
            pass
        self.assertNotIn(MESSAGE_NEW_IDENTITY, kinds)
        self.assertEqual(broadcaster.duplicate_identities(), [])

    def test_command_sent_now_when_ip_connected(self):
        broadcaster = AudioBroadcaster()
        self.addCleanup(broadcaster.close)
        client_id = str(uuid.uuid4())
        connection = self.make_session(broadcaster, client_id, "192.168.1.47")
        self.assertTrue(broadcaster.send_new_identity(client_id, "192.168.1.47", "C2627-Γ1",
                                                      "Board2", TOKEN))
        self.assertIn(MESSAGE_NEW_IDENTITY, self.read_kinds(connection, MESSAGE_NEW_IDENTITY))


class ClientStatusTests(unittest.TestCase):
    def test_pending_sponsor_roundtrip_and_bars(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / st.PENDING_FILE
            client_id = str(uuid.uuid4())
            st.save_pending(path, code="123456", room="C2627-Εργ", name="HOST", expires_at=9e9,
                            client_id=client_id, request_id="r",
                            sponsor={"kind": "clone", "room": "C2627-Εργ", "name": "Πρότυπο"})
            pending = st.load_pending(path, client_id)
        self.assertEqual(pending["sponsor"], {"kind": "clone", "room": "C2627-Εργ",
                                              "name": "Πρότυπο"})
        snap = st.ClientStatus(enrollment=pending).snapshot()
        snap["state"] = st.PENDING
        _alarm, info = st.window_bars(snap)
        self.assertIn("Κλώνος του", info)
        self.assertIn("αναμονή ονόματος και έγκρισης από τον Director", info)
        self.assertNotIn("123", st.tooltip(snap))
        snap["enrollment"]["sponsor"]["kind"] = "reset"
        self.assertEqual(st.window_bars(snap)[1],
                         "Νέα ταυτότητα από τον Director: αναμονή έγκρισης")

    def test_old_identity_reads_key_before_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            client_id = str(uuid.uuid4())
            (folder / "client.json").write_text(
                f'{{"client_id": "{client_id}", "room": "C2627-Εργ"}}', encoding="utf-8")
            create_device_identity(folder / "credentials", client_id)
            info = old_identity(folder / "client.json", folder / "credentials",
                                folder / "hints.json", SPONSOR_CLONE)
            self.assertEqual((info["origin"], info["room"], info["kind"]),
                             (client_id, "C2627-Εργ", SPONSOR_CLONE))
            self.assertTrue(info["key"].startswith(b"-----BEGIN"))
            self.assertIsNone(old_identity(folder / "missing.json", folder / "credentials",
                                           folder / "hints.json", SPONSOR_CLONE))


class GuiTests(SponsorFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from director.qt_gui import create_application
        create_application()
        self.dialogs = []

    def page(self, answer=True):
        from director.qt_security import SecurityPage

        def hook(dialog):
            self.dialogs.append(dialog)
            return answer(dialog) if callable(answer) else answer
        page = SecurityPage(self.model, clock=self.clock, dialog_hook=hook)
        self.addCleanup(page.deleteLater)
        page.refresh()
        return page

    def test_clone_card_has_tag_origin_and_empty_name(self):
        _client_id, rid, _queued = self.sponsored()
        page = self.page()
        row = page.request_rows[rid]
        self.assertTrue(row.sponsored)
        self.assertEqual(row.tag.text(), TAG_CLONE)
        self.assertIn("του C2627-Εργ · Πρότυπο", row.origin.text())
        self.assertEqual(row.name_edit.text(), "")
        self.assertEqual(row.name_edit.placeholderText(), "π.χ. PC05")
        self.assertTrue(row.code.isHidden())
        row.approve.click()
        self.assertEqual(row.result.text(), TEXT_NAME_NEEDED)
        row.name_edit.setText("PC05")
        row.approve.click()
        self.assertTrue(row.final)

    def test_identity_menu_only_on_duplicate(self):
        page = self.page()
        row = page.computer_rows[self.origin_id]
        self.assertEqual(row.identity_actions, {})
        self.broadcaster.duplicate_identities = lambda: [
            (self.origin_id, "C2627-Εργ", ("192.168.1.21", "192.168.1.47"))]
        page.refresh()
        self.assertEqual([a.text() for a in row.identity_actions.values()],
                         [new_identity_action("192.168.1.21"),
                          new_identity_action("192.168.1.47")])
        # Όπως στο mockup: Αλλαγή αίθουσας, Μετονομασία | Νέα ταυτότητα ×2 | Ανάκληση.
        texts = [a.text() or "|" for a in row.menu_button.menu().actions()]
        self.assertEqual(texts, ["Αλλαγή αίθουσας…", "Μετονομασία…", "|",
                                 new_identity_action("192.168.1.21"),
                                 new_identity_action("192.168.1.47"), "|", "Ανάκληση…"])
        sent = []
        self.broadcaster.send_new_identity = lambda *args: sent.append(args) or True

        def edit(dialog):
            dialog.name.setText("Board2")
            dialog._accept()
            return dialog.result() == dialog.DialogCode.Accepted
        page.dialog_hook = lambda dialog: (self.dialogs.append(dialog), edit(dialog))[1]
        row.identity_actions["192.168.1.47"].trigger()
        [(cid, ip, room, name, _token)] = sent
        self.assertEqual((cid, ip, room, name),
                         (self.origin_id, "192.168.1.47", "C2627-Εργ", "Board2"))
        self.assertIn("192.168.1.21", " ".join(
            w.text() for w in self.dialogs[-1].findChildren(type(row.state))))
        self.broadcaster.duplicate_identities = lambda: []
        page.refresh()
        self.assertEqual(row.identity_actions, {})
        self.assertEqual([a.text() or "|" for a in row.menu_button.menu().actions()],
                         ["Αλλαγή αίθουσας…", "Μετονομασία…", "|", "Ανάκληση…"])

    def test_revoke_offers_clones_unchecked(self):
        client_id, rid, _queued = self.sponsored()
        self.assertTrue(self.model.approve_sponsored(rid, "C2627-Εργ", "PC05").ok)
        page = self.page(answer=True)
        page.revoke(self.origin_id)
        dialog = self.dialogs[-1]
        self.assertFalse(dialog.also.isChecked())
        self.assertIsNone(self.registry.approved_entry(self.origin_id))
        self.assertIsNotNone(self.registry.approved_entry(client_id))

    def test_revoke_with_clones_checked(self):
        client_id, rid, _queued = self.sponsored()
        self.assertTrue(self.model.approve_sponsored(rid, "C2627-Εργ", "PC05").ok)

        def check(dialog):
            dialog.also.setChecked(True)
            return True
        page = self.page(answer=check)
        page.revoke(self.origin_id)
        self.assertIsNone(self.registry.approved_entry(client_id))

    def test_reset_card_prefilled(self):
        self.broadcaster.send_new_identity = lambda *args: self.tokens.append(args[-1]) or True
        self.tokens = []
        self.model.new_identity(self.origin_id, "10.0.0.5", "C2627-Γ1", "Board2")
        _cid, rid, _queued = self.sponsored(SPONSOR_RESET, token=self.tokens[0],
                                            room="C2627-Γ1", name="Board2")
        row = self.page().request_rows[rid]
        self.assertEqual(row.tag.text(), TAG_RESET)
        self.assertEqual(row.values(), ("C2627-Γ1", "Board2"))


class RegistryOriginTests(SponsorFixture, unittest.TestCase):
    def test_invalid_origin_rejected(self):
        client_id = str(uuid.uuid4())
        cert, _key, _pw = create_device_identity(self.root / "x", client_id)
        with self.assertRaises((RegistryError, ValueError)):
            self.registry.approve(client_id, "C2627-Γ1", cert.read_bytes(), name="X",
                                  origin=client_id)


if __name__ == "__main__":
    unittest.main()
