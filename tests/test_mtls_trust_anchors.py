"""Προκαταρκτική δοκιμή Python/OpenSSL: self-signed leaf CA:FALSE ως trust anchor.

Ανεξάρτητη από την παραγωγική ροή v4: προϋπόθεση της Φάσης 4.2A1.
Τα κλειδιά δημιουργούνται αποκλειστικά σε TemporaryDirectory.

Η βιβλιοθήκη `cryptography` είναι υποχρεωτική εξάρτηση (pyproject.toml). Αν λείπει,
το test αποτυγχάνει αντί να παραλείπεται, ώστε η δοκιμή στα Windows να μη φαίνεται
επιτυχής χωρίς να έχει τρέξει:

    python -m unittest tests.test_mtls_trust_anchors -v
"""
import datetime
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

TIMEOUT = 4
# Ο server το στέλνει μόνο αφού δεχτεί το πιστοποιητικό του Client. Ο Client το διαβάζει
# πριν κλείσει: με TLS 1.3 το handshake του Client τελειώνει πριν από την επαλήθευση στον
# server, και αν ο Client έκλεινε αμέσως, στα Windows ο server έπαιρνε
# ConnectionAbortedError στέλνοντας τα session tickets.
ACK = b"A"


def _certificate(name, role):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
            key_encipherment=False, data_encipherment=False, key_agreement=False,
            key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False),
            critical=True)
        .add_extension(x509.ExtendedKeyUsage([role]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256()))
    return (certificate.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(serialization.Encoding.PEM,
                          serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()))


class SelfSignedClientTrustTests(unittest.TestCase):
    def _round_trip(self, *, approved=True, use_approved=True,
                    client_role=ExtendedKeyUsageOID.CLIENT_AUTH):
        """approved=False: κανένα εγκεκριμένο πιστοποιητικό (άδειο μητρώο).

        Επιστρέφει (αποτέλεσμα server, αν ο Client έλαβε δεδομένα εφαρμογής).
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            director_cert, director_key = _certificate("school-intercom-director",
                                                        ExtendedKeyUsageOID.SERVER_AUTH)
            trusted_cert, trusted_key = _certificate("client-approved", client_role)
            unknown_cert, unknown_key = _certificate("client-unknown",
                                                      ExtendedKeyUsageOID.CLIENT_AUTH)
            cert, key = ((trusted_cert, trusted_key) if use_approved
                         else (unknown_cert, unknown_key))
            for name, data in (("director.pem", director_cert), ("director.key", director_key),
                               ("client.pem", cert), ("client.key", key)):
                (root / name).write_bytes(data)

            server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            server_ctx.load_cert_chain(str(root / "director.pem"), str(root / "director.key"))
            server_ctx.verify_mode = ssl.CERT_REQUIRED
            if approved:
                server_ctx.load_verify_locations(cadata=trusted_cert.decode("ascii"))

            client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            client_ctx.check_hostname = False  # Preflight only: trusted Director cert is pinned
            client_ctx.load_verify_locations(cadata=director_cert.decode("ascii"))
            client_ctx.load_cert_chain(str(root / "client.pem"), str(root / "client.key"))

            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(TIMEOUT)
            result = []

            def serve():
                try:
                    raw, _ = listener.accept()
                    # Το accept() επιστρέφει blocking socket: χωρίς timeout ένα κολλημένο
                    # handshake θα κρατούσε το νήμα ζωντανό μετά το τέλος του test.
                    raw.settimeout(TIMEOUT)
                    with server_ctx.wrap_socket(raw, server_side=True) as secure:
                        result.append(("ok", bool(secure.getpeercert(binary_form=True))))
                        secure.sendall(ACK)
                except ssl.SSLError as exc:
                    result.append(("rejected", type(exc).__name__))
                except OSError as exc:
                    result.append(("error", type(exc).__name__))

            worker = threading.Thread(target=serve)
            worker.start()
            received = b""
            try:
                with socket.create_connection(listener.getsockname(), timeout=TIMEOUT) as raw:
                    # Ο Client μπορεί να δει επιτυχές handshake πριν ο server απορρίψει το
                    # πιστοποιητικό του: η απόφαση του server είναι ο έλεγχος ασφάλειας, και
                    # ο απορριφθείς Client δεν πρέπει να λάβει δεδομένα εφαρμογής.
                    try:
                        with client_ctx.wrap_socket(
                                raw, server_hostname="school-intercom-director") as secure:
                            received = secure.recv(len(ACK))
                    except (ssl.SSLError, OSError):
                        pass
            finally:
                worker.join(timeout=5)
                listener.close()
            self.assertFalse(worker.is_alive(), "TLS server did not terminate")
            self.assertEqual(len(result), 1)
            return result[0], received == ACK

    def test_approved_self_signed_ca_false(self):
        self.assertEqual(self._round_trip(), (("ok", True), True))

    def _assert_rejected(self, **options):
        (status, _), client_got_data = self._round_trip(**options)
        self.assertEqual(status, "rejected")
        self.assertFalse(client_got_data, "rejected Client received application data")

    def test_unknown_self_signed_ca_false_is_rejected(self):
        self._assert_rejected(use_approved=False)

    def test_empty_registry_rejects_every_client(self):
        self._assert_rejected(approved=False)

    def test_approved_certificate_without_client_auth_eku_is_rejected(self):
        # Ίδιο εγκεκριμένο πιστοποιητικό, αλλά μόνο με EKU serverAuth.
        self._assert_rejected(client_role=ExtendedKeyUsageOID.SERVER_AUTH)


if __name__ == "__main__":
    unittest.main()
