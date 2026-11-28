"""5C2 στον Director: διόρθωση αιτήματος εγγραφής (ίδιος κωδικός), διόρθωση στην κάρτα,
αιτήματα αλλαγής στο μητρώο και στη σελίδα Ασφάλεια."""
from pathlib import Path
import unittest
import uuid

from shared.ui.offscreen import use_offscreen

use_offscreen()

from director.main import RegistryTrust  # noqa: E402
from director.security import KIND_CHANGE, TAG_CHANGE, TEXT_OFFLINE  # noqa: E402
from shared import change_request as cr  # noqa: E402
from shared.client_registry import SignedClientRegistry  # noqa: E402
from shared.device_identity import create_device_identity  # noqa: E402
from shared.enrollment import (EnrollmentError, EnrollmentServer, amend_enrollment,  # noqa: E402
                               enroll_client, make_amend, make_request)
from tests.test_security_page import SecurityFixture  # noqa: E402
from tests.tls_support import LocalTLSFixture  # noqa: E402

SERVER_NAME = "school-intercom-director"


class AmendTests(LocalTLSFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        root = Path(self._tls_temp.name)
        self.root = root
        self.cert = root / "director.crt"
        self.key = root / "director.key"
        self.registry = SignedClientRegistry(root / "registry.json", self.cert, self.key)
        self.trust = RegistryTrust(self.registry, self.cert, self.key)
        self.pending_file = root / "enrollment-pending.json"
        self.server = self.make_server()
        self.server.start()
        self.client_id = str(uuid.uuid4())
        self.ccert, self.ckey, self.password = create_device_identity(root / "c", self.client_id)
        self.request = make_request(self.client_id, "C2627-Γ1", "Board1",
                                    self.ccert.read_bytes(), self.ckey.read_bytes(),
                                    self.password)
        self.queued = enroll_client("127.0.0.1", self.server.port, self.cert, SERVER_NAME,
                                    self.request)

    def make_server(self):
        server = EnrollmentServer(self.cert, self.key, self.trust, host="127.0.0.1", port=0,
                                  pending_file=self.pending_file, log=lambda _m: None)
        self.addCleanup(server.stop)
        return server

    def amend(self, room, name, seq, key=None, password=None):
        message = make_amend(self.request["request_id"], self.client_id, room, name, seq,
                             (key or self.ckey).read_bytes(), password or self.password)
        amend_enrollment("127.0.0.1", self.server.port, self.cert, SERVER_NAME, message)

    def test_enroll_returns_request_id(self):
        self.assertEqual(self.queued["request_id"], self.request["request_id"])

    def test_amend_keeps_code_and_changes_values(self):
        self.amend("C2627-Γ2", "PC7", 1)
        [(rid, room, name, _exp)] = self.server.pending()
        self.assertEqual((room, name), ("C2627-Γ2", "PC7"))
        # Νέος Manager από τον δίσκο: η διόρθωση μένει.
        self.server.stop()
        again = self.make_server()
        self.assertEqual(again.pending()[0][1:3], ("C2627-Γ2", "PC7"))
        info = again.decide(rid, self.queued["code"], approve=True)
        self.assertEqual((info["room"], info["name"]), ("C2627-Γ2", "PC7"))
        entry = self.registry.approved_entry(self.client_id)
        self.assertEqual((entry["room"], entry["name"]), ("C2627-Γ2", "PC7"))

    def test_replayed_or_foreign_amend_rejected(self):
        self.amend("C2627-Γ2", "PC7", 2)
        with self.assertRaises((EnrollmentError, OSError, EOFError)):
            self.amend("C2627-Γ3", "PC8", 2)                  # ίδιο seq: επανάληψη
        other = str(uuid.uuid4())
        ocert, okey, opass = create_device_identity(self.root / "o", other)
        with self.assertRaises((EnrollmentError, OSError, EOFError)):
            self.amend("C2627-Γ3", "PC8", 3, key=okey, password=opass)   # άλλο κλειδί
        self.assertEqual(self.server.pending()[0][1:3], ("C2627-Γ2", "PC7"))

    def test_unknown_request_rejected(self):
        self.server.decide(self.request["request_id"], approve=False)
        with self.assertRaisesRegex(EnrollmentError, "δεν υπάρχει"):
            self.amend("C2627-Γ2", "PC7", 1)

    def test_manager_edit_and_decide_override(self):
        rid = self.request["request_id"]
        self.server.edit(rid, "C2627-Γ4", "PC4")
        self.assertEqual(self.server.pending()[0][1:3], ("C2627-Γ4", "PC4"))
        info = self.server.decide(rid, self.queued["code"], approve=True,
                                  room="C2627-Γ5", name="PC5")
        self.assertEqual((info["room"], info["name"]), ("C2627-Γ5", "PC5"))


class SecurityChangeTests(SecurityFixture, unittest.TestCase):
    def approved(self):
        client_id, code = self.submit()
        self.assertTrue(self.model.approve(self.request_id(), code).ok)
        self.sent = []
        self.broadcaster.send_change_status = lambda cid, status: self.sent.append(
            (cid, status["state"]))
        self.trust.attach(self.broadcaster)
        return client_id

    def test_change_card_and_approve_updates_registry(self):
        client_id = self.approved()
        self.assertEqual(self.trust.change_request(client_id, "C2627-Γ2", "Board1")["state"],
                         cr.PENDING)
        [view] = self.model.requests()
        self.assertEqual((view.kind, view.old_room, view.room), (KIND_CHANGE, "C2627-Γ1",
                                                                  "C2627-Γ2"))
        self.assertFalse(view.online)
        self.assertIn(TEXT_OFFLINE, view.meta(self.clock()))
        decision = self.model.approve_change(client_id)
        self.assertTrue(decision.ok)
        entry = self.registry.approved_entry(client_id)
        self.assertEqual(entry["room"], "C2627-Γ2")
        self.assertIn((client_id, "C2627-Γ2", "Board1"), self.broadcaster.updates)
        self.assertEqual(self.model.requests(), [])
        self.assertEqual(self.sent, [(client_id, cr.APPROVED)])

    def test_deny_keeps_registry(self):
        client_id = self.approved()
        self.trust.change_request(client_id, "C2627-Γ2", "Board1")
        self.assertFalse(self.model.deny_change(client_id).ok)
        self.assertEqual(self.registry.approved_entry(client_id)["room"], "C2627-Γ1")
        self.assertEqual(self.trust.changes.status_for(client_id)["state"], cr.DENIED)

    def test_unapproved_cannot_request_and_revoke_forgets(self):
        with self.assertRaises(ValueError):
            self.trust.change_request(str(uuid.uuid4()), "C2627-Γ2", "Board1")
        client_id = self.approved()
        self.trust.change_request(client_id, "C2627-Γ2", "Board1")
        self.trust.revoke(client_id, self.broadcaster)
        self.assertEqual(self.trust.changes.pending(), [])

    def test_change_card_in_security_page(self):
        from director.qt_gui import create_application
        from director.qt_security import SecurityPage
        create_application()
        client_id = self.approved()
        self.trust.change_request(client_id, "C2627-Γ2", "Board1")
        page = SecurityPage(self.model, clock=self.clock)
        self.addCleanup(page.deleteLater)
        page.refresh()
        row = page.request_rows[client_id]
        self.assertTrue(row.change)
        self.assertEqual(row.tag.text(), TAG_CHANGE)
        row.approve.click()
        self.assertEqual(self.registry.approved_entry(client_id)["room"], "C2627-Γ2")


if __name__ == "__main__":
    unittest.main()
