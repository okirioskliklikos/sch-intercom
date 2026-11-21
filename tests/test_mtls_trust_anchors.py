"""Προκαταρκτική δοκιμή Python/OpenSSL: self-signed leaf CA:FALSE ως trust anchor.

Ανεξάρτητη από την παραγωγική ροή v4: προϋπόθεση της Φάσης 4.2A1.
Τα κλειδιά δημιουργούνται αποκλειστικά σε TemporaryDirectory.
"""
import datetime
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import unittest

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
except ImportError:
    x509 = None


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


@unittest.skipIf(x509 is None, "cryptography is required for mTLS CA:FALSE preflight")
class SelfSignedClientTrustTests(unittest.TestCase):
    def _round_trip(self, *, approved=True, use_approved=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            director_cert, director_key = _certificate("school-intercom-director",
                                                        ExtendedKeyUsageOID.SERVER_AUTH)
            trusted_cert, trusted_key = _certificate("client-approved",
                                                      ExtendedKeyUsageOID.CLIENT_AUTH)
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
            listener.settimeout(4)
            result = []

            def serve():
                try:
                    raw, _ = listener.accept()
                    with server_ctx.wrap_socket(raw, server_side=True) as secure:
                        result.append(("ok", bool(secure.getpeercert(binary_form=True))))
                except ssl.SSLError as exc:
                    result.append(("rejected", type(exc).__name__))
                except OSError as exc:
                    result.append(("error", type(exc).__name__))

            worker = threading.Thread(target=serve)
            worker.start()
            try:
                with socket.create_connection(listener.getsockname(), timeout=4) as raw:
                    # Client might see handshake success before server rejects the unknown
                    # certificate: server-side acceptance is the security assertion.
                    try:
                        with client_ctx.wrap_socket(raw, server_hostname="school-intercom-director"):
                            pass
                    except ssl.SSLError:
                        pass
            finally:
                worker.join(timeout=5)
                listener.close()
            self.assertFalse(worker.is_alive(), "TLS server did not terminate")
            self.assertEqual(len(result), 1)
            return result[0]

    def test_approved_self_signed_ca_false(self):
        self.assertEqual(self._round_trip(), ("ok", True))

    def test_unknown_self_signed_ca_false_is_rejected(self):
        self.assertEqual(self._round_trip(use_approved=False)[0], "rejected")


if __name__ == "__main__":
    unittest.main()
