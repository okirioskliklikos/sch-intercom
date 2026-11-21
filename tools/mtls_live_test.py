"""Αυτόματη πραγματική δοκιμή 4.2A1 (mTLS, έγκριση, ανάκληση, νέο κλειδί) σε δύο υπολογιστές.

Ο Director (Linux) οδηγεί τη δοκιμή και ο Client (Windows) εκτελεί όσα του ζητούνται.
Τρέχουν τα πραγματικά ``director.main`` και ``client.main`` ως διεργασίες, με
προσωρινά κλειδιά/μητρώο σε φάκελο δοκιμής: η κανονική εγκατάσταση δεν αγγίζεται.

ΜΟΝΟ ΓΙΑ ΔΟΚΙΜΕΣ: το κανάλι συντονισμού (TCP 50557, UDP beacon 50558) είναι χωρίς
κρυπτογράφηση και μεταφέρει μόνο δημόσια πιστοποιητικά και εντολές δοκιμής. Δεν είναι
μηχανισμός εγγραφής· η πραγματική εγγραφή με SAS είναι η Φάση 4.2A2.

    Linux:    tools/mtls_live_test.sh
    Windows:  powershell -ExecutionPolicy Bypass -File tools\\mtls_live_test.ps1
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
CONTROL_PORT = 50557
BEACON_PORT = 50558
ROOM = "C2627-Γ1"
OTHER_ROOM = "C2627-Γ2"
# «αποσυνδέθηκε» περιέχει το «συνδέθηκε»: ελέγχουμε το «(αίθουσα) συνδέθηκε».
JOINED = ") συνδέθηκε"
NO_EXPIRY = datetime.datetime(9999, 12, 31, 23, 59, 59, tzinfo=datetime.timezone.utc)


def child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class Process:
    """Διεργασία με συλλογή εξόδου, για αναζήτηση μηνυμάτων με timeout."""

    def __init__(self, args: list[str], *, label: str, show: bool = True):
        self.label = label
        self.show = show
        self.lines: list[str] = []
        self._cond = threading.Condition()
        self.proc = subprocess.Popen(
            [sys.executable, *args], cwd=ROOT, env=child_env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            if self.show:
                print(f"    [{self.label}] {line}", flush=True)
            with self._cond:
                self.lines.append(line)
                self._cond.notify_all()
        with self._cond:
            self._cond.notify_all()

    def mark(self) -> int:
        with self._cond:
            return len(self.lines)

    def wait_for(self, *texts: str, since: int = 0, timeout: float = 20.0) -> str | None:
        """Πρώτη γραμμή μετά το since που περιέχει όλα τα texts."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for line in self.lines[since:]:
                    if all(text in line for text in texts):
                        return line
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def seen(self, *texts: str, since: int = 0) -> bool:
        with self._cond:
            return any(all(t in line for t in texts) for line in self.lines[since:])

    def send(self, line: str) -> None:
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def stop(self, *, quit_command: str | None = None) -> None:
        if self.proc.poll() is None and quit_command:
            try:
                self.send(quit_command)
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)


class Channel:
    """JSON ανά γραμμή πάνω σε TCP."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.file = sock.makefile("rw", encoding="utf-8", newline="\n")

    def send(self, **message) -> None:
        self.file.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.file.flush()

    def receive(self) -> dict:
        line = self.file.readline()
        if not line:
            raise ConnectionError("Το κανάλι δοκιμής έκλεισε")
        return json.loads(line)


# ---------------------------------------------------------------- Director

class DirectorRun:
    def __init__(self, work: Path):
        from director.tls_identity import create_director_identity
        self.work = work
        self.cert = work / "director-tls.crt"
        self.key = work / "director-tls.key"
        self.registry = work / "clients-registry.json"
        self.director_id = work / "director.id"
        create_director_identity(self.cert, self.key)
        self.results: list[tuple[str, bool, str]] = []
        self.channel: Channel | None = None
        self.director: Process | None = None

    def tls(self) -> list[str]:
        return ["--tls-cert", str(self.cert), "--tls-key", str(self.key),
                "--registry-file", str(self.registry)]

    def result(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, ok, detail))
        text = f"{'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else "")
        print(f"\n=== {text}\n", flush=True)
        if self.channel is not None:
            self.channel.send(cmd="report", text=text)

    def ask_client(self, cmd: str, **args) -> dict:
        self.channel.send(cmd=cmd, **args)
        reply = self.channel.receive()
        if reply.get("error"):
            raise RuntimeError(f"Client: {reply['error']}")
        return reply

    def start_director(self) -> Process:
        self.director = Process(["-m", "director.main", *self.tls(),
                                 "--director-id-file", str(self.director_id)],
                                label="Director")
        if self.director.wait_for("Director ακούει", timeout=15) is None:
            raise RuntimeError("Ο Director δεν ξεκίνησε (θύρα 50555 κατειλημμένη;)")
        return self.director

    def stop_director(self) -> None:
        if self.director is not None:
            self.director.stop(quit_command="quit")
            self.director = None

    def cli(self, *args: str) -> tuple[int, str]:
        done = subprocess.run([sys.executable, "-m", "director.main", *self.tls(), *args],
                              cwd=ROOT, env=child_env(), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
        output = (done.stdout + done.stderr).strip()
        for line in output.splitlines():
            print(f"    [CLI] {line}", flush=True)
        return done.returncode, output

    def wait_client(self) -> None:
        from shared.discovery import announce
        stop = threading.Event()
        threading.Thread(target=announce, args=(stop, str(uuid.uuid4()), CONTROL_PORT),
                         kwargs={"udp_port": BEACON_PORT, "interval": 1.0},
                         daemon=True).start()
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("0.0.0.0", CONTROL_PORT))
            server.listen(1)
            print("Αναμονή για τον Client δοκιμής. Στα Windows τρέξτε:\n"
                  "    powershell -ExecutionPolicy Bypass -File tools\\mtls_live_test.ps1\n"
                  f"(Αν δεν βρεθεί αυτόματα: ... -DirectorHost <IP αυτού του υπολογιστή>.\n"
                  f" Χρειάζονται ανοιχτές οι θύρες TCP 50555, {CONTROL_PORT} και UDP 50556, {BEACON_PORT}.)",
                  flush=True)
            connection, address = server.accept()
        stop.set()
        print(f"Ο Client δοκιμής συνδέθηκε από {address[0]}", flush=True)
        self.channel = Channel(connection)
        self.channel.send(cmd="hello", director_cert=self.cert.read_text(encoding="ascii"))
        self.channel.receive()

    def run(self) -> int:
        from cryptography import x509
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
        from shared.client_registry import public_key_fingerprint

        self.wait_client()
        try:
            # 1. Δημιουργία κλειδιού Client
            reply = self.ask_client("start_client", room=ROOM, new_key=False)
            client_id, old_pem = reply["client_id"], reply["cert_pem"].encode("ascii")
            old_file = self.work / "client-old.pem"
            old_file.write_bytes(old_pem)
            cert = x509.load_pem_x509_certificate(old_pem)
            cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
            eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
            self.result("1. Κλειδί Client (CN=UUID, χωρίς λήξη, clientAuth)",
                        cn == client_id and cert.not_valid_after_utc == NO_EXPIRY
                        and ExtendedKeyUsageOID.CLIENT_AUTH in eku,
                        f"UUID {client_id}")

            # 2. Μη εγκεκριμένος Client απορρίπτεται στο TLS
            director = self.start_director()
            mark = director.mark()
            rejected = director.wait_for("CERTIFICATE_VERIFY_FAILED", since=mark, timeout=30)
            self.result("2. Μη εγκεκριμένος Client απορρίπτεται",
                        rejected is not None and not director.seen(JOINED, since=mark))
            self.stop_director()

            # 3. Έγκριση από το CLI
            code, output = self.cli("--approve-client-cert", str(old_file), "--approve-room", ROOM)
            self.result("3. Έγκριση Client από το CLI",
                        code == 0 and f"Εγκρίθηκε Client {client_id} (v2)" in output)

            # 4. Σύνδεση εγκεκριμένου Client
            director = self.start_director()
            joined = director.wait_for(client_id, JOINED, timeout=40)
            mark = director.mark()
            director.send("status")
            online = director.wait_for(client_id, "Online", since=mark, timeout=5)
            self.result("4. Ο εγκεκριμένος Client συνδέεται (status: Online)",
                        joined is not None and online is not None)

            # 5. Ήχος (ερώτηση στον χρήστη στα Windows)
            director.send("all")
            print("\n>>> Μιλήστε στο μικρόφωνο του Director. Η απάντηση δίνεται στα Windows.\n",
                  flush=True)
            heard = self.ask_client("ask", text="Μιλήστε στο μικρόφωνο του Director. "
                                    "Ακούγεται η φωνή από τα ηχεία των Windows;")["yes"]
            self.result("5. Ήχος Director → Client", heard)

            # 6. Ανάκληση εν λειτουργία
            mark = director.mark()
            director.send(f"revoke {client_id}")
            revoked = director.wait_for(f"Ανακλήθηκε ο Client {client_id}", since=mark, timeout=5)
            dropped = director.wait_for(client_id, "αποσυνδέθηκε", since=mark, timeout=5)
            retry = director.wait_for("CERTIFICATE_VERIFY_FAILED", since=mark, timeout=30)
            mark2 = director.mark()
            director.send("status")
            time.sleep(1)
            still = director.seen(client_id, "Online", since=mark2)
            self.result("6. Ανάκληση: άμεση αποσύνδεση και απόρριψη νέας σύνδεσης",
                        bool(revoked and dropped and retry) and not still
                        and not director.seen(client_id, JOINED, since=mark))
            self.stop_director()
            self.ask_client("stop_client")

            # 7. Νέο κλειδί για τον ίδιο UUID
            reply = self.ask_client("start_client", room=ROOM, new_key=True)
            new_pem = reply["cert_pem"].encode("ascii")
            new_file = self.work / "client-new.pem"
            new_file.write_bytes(new_pem)
            self.result("7. --new-client-key: ίδιο UUID, νέο κλειδί",
                        reply["client_id"] == client_id and reply["new_key_message"]
                        and public_key_fingerprint(new_pem) != public_key_fingerprint(old_pem))

            # 8. Το παλιό κλειδί απορρίπτεται, το νέο εγκρίνεται
            code_old, out_old = self.cli("--approve-client-cert", str(old_file),
                                         "--approve-room", ROOM)
            code_new, out_new = self.cli("--approve-client-cert", str(new_file),
                                         "--approve-room", ROOM)
            _, listing = self.cli("--list-clients")
            rows = [line for line in listing.splitlines() if client_id in line]
            self.result("8. Παλιό κλειδί απορρίπτεται, νέο εγκρίνεται (v4)",
                        code_old == 2 and "Key already used" in out_old
                        and code_new == 0 and "(v4)" in out_new
                        and len(rows) == 1 and rows[0].endswith("approved"))

            # 9. Σύνδεση με το νέο κλειδί
            director = self.start_director()
            joined = director.wait_for(client_id, JOINED, timeout=40)
            self.result("9. Σύνδεση με το νέο κλειδί", joined is not None)

            # 10. Λάθος αίθουσα: απόρριψη με εξήγηση στο log
            self.ask_client("stop_client")
            mark = director.mark()
            self.ask_client("start_client", room=OTHER_ROOM, new_key=False)
            reason = director.wait_for(client_id, f"δηλώθηκε {OTHER_ROOM}", since=mark,
                                       timeout=40)
            self.result("10. Λάθος αίθουσα απορρίπτεται με λόγο στο log",
                        reason is not None and not director.seen(JOINED, since=mark))
            self.ask_client("stop_client")
        except Exception as exc:  # η δοκιμή αναφέρει, δεν κρύβει
            self.result("Απρόσμενο σφάλμα δοκιμής", False, f"{type(exc).__name__}: {exc}")
        finally:
            self.stop_director()
        passed = sum(ok for _, ok, _ in self.results)
        summary = f"ΣΥΝΟΛΟ: {passed}/{len(self.results)} PASS"
        print("\n" + "\n".join(f"{'PASS' if ok else 'FAIL'}  {name}"
                               for name, ok, _ in self.results), flush=True)
        print(summary, flush=True)
        try:
            self.channel.send(cmd="done", text=summary)
        except OSError:
            pass
        return 0 if passed == len(self.results) else 1


# ---------------------------------------------------------------- Client

class ClientRun:
    def __init__(self, work: Path, director_host: str | None):
        self.work = work
        self.director_host = director_host
        self.cert = work / "director-tls.crt"
        self.identity = work / "client-identity.json"
        self.credentials = work / "credentials"
        self.client: Process | None = None

    def connect(self) -> Channel:
        host = self.director_host
        while host is None:
            from shared.discovery import discover
            print("Αναζήτηση του Director δοκιμής στο LAN...", flush=True)
            found = discover(udp_port=BEACON_PORT, timeout=3.0)
            if found is not None:
                host = found[0]
        print(f"Σύνδεση στον Director δοκιμής {host}:{CONTROL_PORT}", flush=True)
        return Channel(socket.create_connection((host, CONTROL_PORT), timeout=None))

    def start_client(self, room: str, new_key: bool) -> dict:
        self.stop_client()
        args = ["-m", "client.main", "--tls-ca", str(self.cert),
                "--identity-file", str(self.identity),
                "--client-credentials-dir", str(self.credentials), "--room", room]
        if self.director_host:
            args += ["--host", self.director_host]
        if new_key:
            args.append("--new-client-key")
        self.client = Process(args, label="Client")
        line = self.client.wait_for("Client ID:", timeout=30)
        if line is None:
            raise RuntimeError("Ο Client δεν ξεκίνησε")
        client_id = line.split("Client ID:")[1].split("|")[0].strip()
        return {"client_id": client_id,
                "cert_pem": (self.credentials / "client-cert.pem").read_text(encoding="ascii"),
                "new_key_message": self.client.seen("Δημιουργήθηκε νέο κλειδί")}

    def stop_client(self) -> None:
        if self.client is not None:
            self.client.stop()
            self.client = None

    def run(self) -> int:
        channel = self.connect()
        summary = "Η δοκιμή διακόπηκε"
        try:
            while True:
                message = channel.receive()
                cmd = message["cmd"]
                try:
                    if cmd == "hello":
                        self.cert.write_text(message["director_cert"], encoding="ascii")
                        channel.send(ok=True)
                    elif cmd == "start_client":
                        channel.send(**self.start_client(message["room"], message["new_key"]))
                    elif cmd == "stop_client":
                        self.stop_client()
                        channel.send(ok=True)
                    elif cmd == "ask":
                        answer = input(f"\n>>> {message['text']} [y/n]: ").strip().lower()
                        channel.send(yes=answer in ("y", "yes", "ν", "ναι"))
                    elif cmd == "report":
                        print(f"\n=== {message['text']}\n", flush=True)
                    elif cmd == "done":
                        summary = message["text"]
                        break
                except Exception as exc:
                    channel.send(error=f"{type(exc).__name__}: {exc}")
        finally:
            self.stop_client()
        print(summary, flush=True)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("role", choices=("director", "client"))
    parser.add_argument("--director-host", default=None,
                        help="IP του Director, αν δεν βρεθεί αυτόματα (μόνο client)")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="sch-4.2a1-test-") as directory:
        work = Path(directory)
        print(f"Φάκελος δοκιμής (διαγράφεται στο τέλος): {work}", flush=True)
        if args.role == "director":
            return DirectorRun(work).run()
        return ClientRun(work, args.director_host).run()


if __name__ == "__main__":
    sys.exit(main())
