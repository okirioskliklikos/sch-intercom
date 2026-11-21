"""Ταυτότητα TLS εγκατάστασης Manager/Director, χωρίς λήξη.

Χρήση (μία φορά, σε ασφαλή φάκελο εκτός repository):

    python -m director.tls_identity --cert director-tls.crt --key director-tls.key

Απόφαση χρήστη 21/11/2026: το πιστοποιητικό δεν λήγει (RFC 5280 §4.1.2.5,
notAfter 99991231235959Z). Δεν υπάρχει ανανέωση· σε διαρροή κλειδιού ισχύει η
πολιτική ολικής επανέκδοσης της Φάσης 4.2. Ποτέ δεν αντικαθιστά υπάρχοντα αρχεία.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from shared.device_identity import NO_EXPIRY, _write_secret
from shared.transport_security import DEFAULT_SERVER_NAME


def create_director_identity(certfile: Path, keyfile: Path, *,
                             server_name: str = DEFAULT_SERVER_NAME) -> str:
    """Δημιουργεί κλειδί ECDSA P-256 και self-signed πιστοποιητικό με SAN DNS.

    Επιστρέφει το SHA-256 fingerprint για επιβεβαίωση εκτός δικτύου στους Clients.
    """
    certfile, keyfile = Path(certfile), Path(keyfile)
    if certfile.exists() or keyfile.exists():
        raise FileExistsError("Υπάρχει ήδη ταυτότητα TLS· δεν αντικαθίσταται")
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, server_name)])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(NO_EXPIRY)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(server_name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=False,
            crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256()))
    # Το Director φορτώνει σήμερα κλειδί χωρίς κωδικό· η κρυπτογράφηση έρχεται με το backup 4.2C.
    _write_secret(keyfile, key.private_bytes(serialization.Encoding.PEM,
                                             serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
    try:
        _write_secret(certfile, certificate.public_bytes(serialization.Encoding.PEM))
    except BaseException:
        keyfile.unlink(missing_ok=True)
        raise
    return certificate.fingerprint(hashes.SHA256()).hex(":").upper()


def main() -> None:
    parser = argparse.ArgumentParser(description="Ταυτότητα TLS Director χωρίς λήξη")
    parser.add_argument("--cert", type=Path, required=True, help="Νέο πιστοποιητικό (PEM)")
    parser.add_argument("--key", type=Path, required=True,
                        help="Νέο ιδιωτικό κλειδί (PEM, εκτός repository)")
    parser.add_argument("--server-name", default=DEFAULT_SERVER_NAME)
    args = parser.parse_args()
    try:
        fingerprint = create_director_identity(args.cert, args.key, server_name=args.server_name)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Σφάλμα: {exc}\n")
    print(f"Πιστοποιητικό: {args.cert}\nSHA-256: {fingerprint}")
    print("Αντιγράψτε ΜΟΝΟ το πιστοποιητικό στους Clients και επιβεβαιώστε το αποτύπωμα.")


if __name__ == "__main__":
    main()
