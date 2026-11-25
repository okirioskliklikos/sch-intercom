"""4.4: αυτόματη δοκιμή σε πραγματικό δίκτυο (LAN/Wi-Fi), 11 βήματα PASS/FAIL/SKIP.

Ο υπολογιστής «director» οδηγεί τη δοκιμή και κάθε υπολογιστής «client» τρέχει έναν
βοηθό που εκτελεί ό,τι του ζητηθεί και στέλνει πίσω τις μετρήσεις του. Τρέχουν τα
πραγματικά ``director.main`` (πηγή δοκιμής αντί για μικρόφωνο) και ``client.main``
(πραγματική αναπαραγωγή στα ηχεία), με προσωρινά κλειδιά/μητρώο σε φάκελο δοκιμής που
διαγράφεται στο τέλος: η κανονική εγκατάσταση δεν αγγίζεται.

Μετρά: καθυστέρηση αποστολή→ηχείο, πρώτα/τελευταία blocks κάθε ανακοίνωσης, κενά,
συγχρονισμό αιθουσών (≤ 1 s), 5 λεπτά ήχο μαζί με heartbeat, φόρτο με εικονικούς
Clients, επανεκκίνηση Director και «πάγωμα» δικτύου (TCP proxy στον Director, χωρίς
admin). Τα ρολόγια ευθυγραμμίζονται μέσα από το κανάλι δοκιμής (μέτρηση τύπου NTP).
Οι μετρήσεις στέλνουν σιωπή (μετρά ο αύξων αριθμός, όχι ο ήχος)· μόνο στο τελευταίο
βήμα παίζει ήπια μελωδία ~10 s και η μόνη ερώτηση είναι αν ακούστηκε καθαρά.

ΜΟΝΟ ΓΙΑ ΔΟΚΙΜΕΣ: το κανάλι συντονισμού (TCP 50567, UDP beacon 50568) είναι χωρίς
κρυπτογράφηση και μεταφέρει μόνο δημόσια πιστοποιητικά, εντολές και μετρήσεις.

    Director (Linux):    tools/network_live_test.sh director --clients 2
    Director (Windows):  powershell -ExecutionPolicy Bypass -File tools\\network_live_test.ps1 -Role director -Clients 2
    Client (Linux):      tools/network_live_test.sh client [--director-host IP]
    Client (Windows):    powershell -ExecutionPolicy Bypass -File tools\\network_live_test.ps1 [-DirectorHost IP]
    Μόνο σε αυτόν τον υπολογιστή (έλεγχος του script): network_live_test.py director --loopback
"""
from __future__ import annotations

import argparse
import datetime
import os
from pathlib import Path
import platform
import socket
import ssl
import subprocess
import sys
import tempfile
import queue
import shutil
import threading
import time
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.audio import Frame, receive_message  # noqa: E402
from shared.client_registry import SignedClientRegistry  # noqa: E402
from shared.control import heartbeat_sender  # noqa: E402
from shared.device_identity import create_device_identity  # noqa: E402
from shared.identity import load_identity, receive_welcome, send_hello  # noqa: E402
from shared.test_report import read_report  # noqa: E402
from shared.transport_security import DEFAULT_SERVER_NAME, client_context  # noqa: E402
from tools import network_metrics as nm  # noqa: E402
from tools.failure_live_test import FreezableProxy  # noqa: E402
from tools.mtls_live_test import Channel, Process  # noqa: E402

TOTAL = 11
AUDIO_PORT, DISCOVERY_PORT, CONTROL_PORT = 50565, 50566, 50567
BEACON_PORT, ENROLL_PORT, PROXY_PORT = 50568, 50569, 50571
VIRTUAL_ROOM = "C2627-Δ9"
JOIN_TIMEOUT = 40.0
CLOCK_SAMPLES = 15
WARMUP_SECONDS = 5


def now_us() -> int:
    return time.monotonic_ns() // 1000


def room_for(index: int) -> str:
    return f"C2627-Α{index + 1}"


# ------------------------------------------------------------------ δίκτυο
def local_ip_towards(host: str) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((host, 9))     # δεν στέλνει τίποτα· μόνο επιλογή διαδρομής
        return probe.getsockname()[0]


def link_type(ip: str) -> str | None:
    """«lan», «wifi» ή None (άγνωστο) για την κάρτα που έχει τη διεύθυνση ip."""
    if ip.startswith("127."):
        return "lan"
    try:
        if os.name == "nt":
            command = ("(Get-NetAdapter -InterfaceIndex (Get-NetIPAddress -IPAddress "
                       f"'{ip}' -ErrorAction Stop).InterfaceIndex).PhysicalMediaType")
            done = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                                  capture_output=True, text=True, timeout=20)
            media = done.stdout.strip()
            if "802.11" in media:
                return "wifi"
            if "802.3" in media:
                return "lan"
            return None
        done = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True,
                              text=True, timeout=10)
        for line in done.stdout.splitlines():
            parts = line.split()
            if len(parts) > 3 and parts[3].split("/")[0] == ip:
                name = parts[1]
                wireless = Path("/sys/class/net") / name
                if (wireless / "wireless").exists() or (wireless / "phy80211").exists():
                    return "wifi"
                return "lan"
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def link_label(link: str | None) -> str:
    return {"lan": "καλώδιο", "wifi": "Wi-Fi"}.get(link, "άγνωστο (κριτήρια Wi-Fi)")


class Inbox:
    """Λήψη από το κανάλι σε δικό της νήμα, ώστε το Ctrl+C να δουλεύει και στα Windows
    (εκεί ένα μπλοκαρισμένο recv/accept δεν διακόπτεται από το Ctrl+C)."""

    def __init__(self, channel: Channel):
        self.channel = channel
        self.items: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True, name="test-inbox").start()

    def _run(self) -> None:
        while True:
            try:
                self.items.put(self.channel.receive())
            except (OSError, ValueError) as exc:
                self.items.put(exc)
                return

    def get(self) -> dict:
        while True:
            try:
                item = self.items.get(timeout=0.5)
            except queue.Empty:
                continue
            if isinstance(item, Exception):
                self.items.put(item)        # και οι επόμενες κλήσεις βλέπουν το κλείσιμο
                raise item if isinstance(item, OSError) else ConnectionError(str(item))
            return item


class TimedProcess(Process):
    """Process που κρατά και τη στιγμή (monotonic μs) κάθε γραμμής."""

    def __init__(self, *args, **kwargs):
        self.times: list[int] = []
        super().__init__(*args, **kwargs)

    def _read(self) -> None:
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            if self.show:
                print(f"    [{self.label}] {line}", flush=True)
            with self._cond:
                self.lines.append(line)
                self.times.append(now_us())
                self._cond.notify_all()
        with self._cond:
            self.closed = True
            self._cond.notify_all()

    def first_time(self, *texts: str, since: int = 0) -> int | None:
        with self._cond:
            for index in range(since, len(self.lines)):
                if all(text in self.lines[index] for text in texts):
                    return self.times[index]
        return None


# ------------------------------------------------------------------ βοηθός Client
class VirtualClient:
    """Εικονικός Client: πραγματικό mTLS, HELLO, heartbeat, κρατά τους αύξοντες που ήρθαν."""

    def __init__(self, folder: Path, index: int, room: str):
        self.client_id = str(uuid.uuid4())
        self.room = room
        self.cert, self.key, self.password = create_device_identity(folder / f"v{index:02d}",
                                                                    self.client_id)
        self.connection: ssl.SSLSocket | None = None
        self.stop = threading.Event()
        self.seqs: list[int] = []
        self.error: str | None = None

    def connect(self, host: str, port: int, trust: Path) -> None:
        context = client_context(trust, certfile=self.cert, keyfile=self.key,
                                 password=self.password)
        plain = socket.create_connection((host, port), timeout=5)
        try:
            connection = context.wrap_socket(plain, server_hostname=DEFAULT_SERVER_NAME)
            connection.settimeout(5)
            send_hello(connection, self.client_id, self.room)
            receive_welcome(connection)
        except BaseException:
            plain.close()
            raise
        self.connection = connection
        self.stop = threading.Event()
        self.seqs = []
        self.error = None
        threading.Thread(target=heartbeat_sender, args=(connection, self.stop),
                         daemon=True).start()
        threading.Thread(target=self._receive, daemon=True).start()

    def _receive(self) -> None:
        while not self.stop.is_set():
            try:
                # Ίδιος τρόπος ανάγνωσης με τον πραγματικό Client (select με προθεσμία).
                message = receive_message(self.connection, deadline=time.monotonic() + 5)
            except socket.timeout:
                continue
            except (OSError, EOFError, ValueError) as exc:
                if not self.stop.is_set():
                    self.error = f"{type(exc).__name__}: {exc}"
                break
            if isinstance(message, Frame):
                self.seqs.append(message.seq)

    def close(self) -> None:
        self.stop.set()
        if self.connection is not None:
            try:
                self.connection.close()
            except OSError:
                pass
            self.connection = None


def firewalld_running() -> bool:
    """Linux: τρέχει το firewalld (openSUSE, Fedora); ``firewall-cmd --state`` χωρίς root."""
    if os.name == "nt" or shutil.which("firewall-cmd") is None:
        return False
    try:
        result = subprocess.run(["firewall-cmd", "--state"], capture_output=True,
                                text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.stdout.strip() == "running"


def discovery_hint(*, firewalld: bool | None = None) -> str:
    """Τι να κάνει ο χρήστης όταν ο βοηθός δεν βρίσκει τον Director (4.4, Εκτέλεση 3)."""
    if firewalld is None:
        firewalld = firewalld_running()
    if firewalld:
        return (f"Δεν βρέθηκε ακόμη. Το firewalld τρέχει και κόβει το beacon (UDP {BEACON_PORT}).\n"
                f"  Άνοιγμα μέχρι την επανεκκίνηση: sudo firewall-cmd --add-port={BEACON_PORT}/udp\n"
                "  ή ξανά με --director-host <IP του Director>")
    return ("Δεν βρέθηκε ακόμη. Ελέγξτε ότι ο Director τρέχει στο ίδιο δίκτυο και ότι το "
            f"firewall επιτρέπει εισερχόμενο UDP {BEACON_PORT}, ή ξανά με --director-host <IP>")


class Agent:
    def __init__(self, work: Path, director_host: str | None, name: str | None,
                 link: str | None, quiet: bool):
        self.work = work
        self.director_host = director_host
        self.name = name or socket.gethostname()[:24]
        self.forced_link = link
        self.quiet = quiet
        self.trust = work / "director-tls.crt"
        self.report = work / "client-report.jsonl"
        self.client: Process | None = None
        self.virtual: list[VirtualClient] = []
        self.room = ""
        self.host = ""
        self.buffer_ms: int | None = None

    def connect(self) -> Channel:
        host = self.director_host
        attempts = 0
        while host is None:
            from shared.discovery import discover_candidates
            print("Αναζήτηση του Director δοκιμής στο δίκτυο...", flush=True)
            # Ο Director με καλώδιο και Wi-Fi στέλνει από δύο IP· αρκεί η πρώτη.
            found = discover_candidates(udp_port=BEACON_PORT, timeout=3.0)
            if found:
                host = found[0][0]
                break
            attempts += 1
            if attempts == 3:
                print(discovery_hint(), flush=True)
        print(f"Σύνδεση στον Director δοκιμής {host}:{CONTROL_PORT}", flush=True)
        sock = socket.create_connection((host, CONTROL_PORT), timeout=30)
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.host = host
        return Channel(sock)

    def hello(self, message: dict) -> dict:
        self.trust.write_text(message["director_cert"], encoding="ascii")
        self.room = message["room"]
        self.buffer_ms = message.get("buffer_ms")
        folder = self.work / "real"
        client_id, _ = load_identity(folder / "client.json", room_override=self.room)
        cert, _, _ = create_device_identity(folder / "credentials", client_id)
        self.virtual = [VirtualClient(self.work / "virtual", index, VIRTUAL_ROOM)
                        for index in range(message["virtual"])]
        ip = local_ip_towards(self.host)
        link = self.forced_link or link_type(ip)
        print(f"Αυτός ο υπολογιστής: {self.name} ({ip}), σύνδεση: {link_label(link)}",
              flush=True)
        return {"name": self.name, "ip": ip, "link": link, "os": platform.system(),
                "real": {"id": client_id, "pem": cert.read_text(encoding="ascii")},
                "virtual": [{"id": item.client_id,
                             "pem": item.cert.read_text(encoding="ascii")}
                            for item in self.virtual]}

    def start_real(self, port: int) -> dict:
        self.stop_real()
        folder = self.work / "real"
        self.client = Process(
            ["-m", "client.main", "--tls-ca", str(self.trust),
             "--identity-file", str(folder / "client.json"),
             "--client-credentials-dir", str(folder / "credentials"),
             "--client-hints-file", str(folder / "client-hints.json"),
             "--registry-seen-file", str(folder / "registry-seen.json"),
             "--volume-recovery-file", str(folder / "volume-recovery.json"),
             "--room", self.room, "--host", self.host, "--port", str(port),
             "--enrollment-port", str(ENROLL_PORT), "--test-report", str(self.report),
             *(["--buffer-ms", str(self.buffer_ms)] if self.buffer_ms else [])],
            label=f"Client {self.name}", show=not self.quiet)
        if self.client.wait_for("Client ID:", timeout=30) is None:
            raise RuntimeError("Ο Client δεν ξεκίνησε· " + " | ".join(self.client.lines[-5:]))
        return {"ok": True}

    def stop_real(self) -> None:
        if self.client is not None:
            self.client.stop()
            self.client = None

    def events(self, after: int) -> dict:
        wanted = ("play", "underrun", "silence", "connected", "lost", "stop")
        events = [event for event in read_report(self.report)
                  if event.get("t", 0) > after and event.get("ev") in wanted]
        return {"events": events}

    def vstart(self, port: int) -> dict:
        errors = []

        def one(item: VirtualClient) -> None:
            try:
                item.connect(self.host, port, self.trust)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")
        threads = [threading.Thread(target=one, args=(item,)) for item in self.virtual]
        for thread in threads:
            thread.start()
            time.sleep(0.05)
        for thread in threads:
            thread.join(30)
        return {"connected": len(self.virtual) - len(errors), "errors": errors[:3]}

    def vstop(self) -> None:
        for item in self.virtual:
            item.close()

    def run(self) -> int:
        channel = self.connect()
        inbox = Inbox(channel)
        summary = "Η δοκιμή διακόπηκε"
        try:
            while True:
                try:
                    message = inbox.get()
                except OSError:      # ConnectionError/10053: ο Director σταμάτησε
                    summary = ("Η σύνδεση με τον Director έκλεισε πριν από το τέλος "
                               "(διακόπηκε η δοκιμή στον Director;)")
                    break
                cmd = message["cmd"]
                try:
                    if cmd == "clock":
                        channel.send(t=now_us())
                    elif cmd == "hello":
                        channel.send(**self.hello(message))
                    elif cmd == "start_real":
                        channel.send(**self.start_real(message["port"]))
                    elif cmd == "stop_real":
                        self.stop_real()
                        channel.send(ok=True)
                    elif cmd == "events":
                        channel.send(**self.events(message["after"]))
                    elif cmd == "vstart":
                        channel.send(**self.vstart(message["port"]))
                    elif cmd == "vseqs":
                        channel.send(seqs=[item.seqs for item in self.virtual],
                                     errors=[item.error for item in self.virtual if item.error])
                    elif cmd == "vstop":
                        self.vstop()
                        channel.send(ok=True)
                    elif cmd == "report":
                        print(f"\n{message['text']}\n", flush=True)
                    elif cmd == "done":
                        summary = message["text"]
                        break
                    else:
                        channel.send(error=f"άγνωστη εντολή {cmd}")
                except Exception as exc:  # noqa: BLE001
                    channel.send(error=f"{type(exc).__name__}: {exc}")
        finally:
            self.vstop()
            self.stop_real()
        print(summary, flush=True)
        return 0


# ------------------------------------------------------------------ Director
class Peer:
    """Ένας βοηθός Client όπως τον βλέπει ο Director."""

    def __init__(self, index: int, channel: Channel, address: str):
        self.index = index
        self.channel = channel
        self.address = address
        self.room = room_for(index)
        self.clock = nm.ClockMap()
        self.inbox = Inbox(channel)
        self.info: dict = {}
        self.lock = threading.Lock()

    @property
    def name(self) -> str:
        return self.info.get("name", self.address)

    @property
    def client_id(self) -> str:
        return self.info["real"]["id"]

    def ask(self, cmd: str, **args) -> dict:
        with self.lock:
            self.channel.send(cmd=cmd, **args)
            reply = self.inbox.get()
        if reply.get("error"):
            raise RuntimeError(f"{self.name}: {reply['error']}")
        return reply

    def sync(self) -> None:
        samples = []
        for _ in range(CLOCK_SAMPLES):
            before = now_us()
            client = self.ask("clock")["t"]
            samples.append((before, client, now_us()))
        self.clock.add(samples)

    def now(self) -> int:
        """Τρέχων χρόνος στο ρολόι του Client (για φίλτρο γεγονότων)."""
        return int(now_us() + self.clock.offset(now_us()))

    def events(self, after: int) -> list[dict]:
        return self.ask("events", after=after)["events"]


class NetworkTest:
    def __init__(self, work: Path, *, clients: int, virtual: int, ask: bool,
                 quiet: bool, loopback: bool, link: str | None,
                 long_seconds: int, buffer_ms: int | None = None):
        self.work = work
        self.clients = clients
        self.virtual = virtual
        self.ask_user = ask
        self.quiet = quiet
        self.loopback = loopback
        self.forced_link = link
        self.long_seconds = long_seconds
        self.buffer_ms = buffer_ms
        self.cert = work / "tls" / "director-tls.crt"
        self.key = work / "tls" / "director-tls.key"
        self.registry = work / "clients-registry.json"
        self.peers: list[Peer] = []
        self.director: TimedProcess | None = None
        self.report_file: Path | None = None
        self.starts = 0
        self.proxy: FreezableProxy | None = None
        self.results: list[tuple[str, str, str]] = []
        self.helpers: list[Process] = []
        self.link: str | None = None
        self.collected: dict[str, list[nm.ClientResult]] = {}
        self.stamp = time.strftime("%d/%m/%Y %H:%M")

    # ------------------------------------------------------------ βοηθητικά
    def step(self, title: str, function, minutes: float = 0) -> None:
        number = len(self.results) + 1
        start = (f"... {number:02d}/{TOTAL}: {title}"
                 + (f" (περίπου {minutes:g} λεπτ{'ό' if minutes == 1 else 'ά'})" if minutes else "")
                 + time.strftime(" — %H:%M:%S"))
        print("\n" + start, flush=True)
        self.tell_peers(start)
        try:
            answer = function()
            state, detail = answer if isinstance(answer, tuple) else (
                "PASS" if answer else "FAIL", "")
            if state is True or state is False:
                state = "PASS" if state else "FAIL"
        except Exception as exc:  # noqa: BLE001
            state, detail = "FAIL", f"{type(exc).__name__}: {exc}"
        self.results.append((title, state, detail))
        text = f"{state} {len(self.results):02d}/{TOTAL}: {title}" + (
            f"\n       {detail}" if detail else "")
        print(f"\n{text}\n", flush=True)
        self.tell_peers(text)

    def tell_peers(self, text: str) -> None:
        """Ίδια ενημέρωση και στην οθόνη κάθε υπολογιστή Client."""
        for peer in self.peers:
            try:
                with peer.lock:
                    peer.channel.send(cmd="report", text=text)
            except OSError:
                pass

    def kind(self, peer: Peer) -> str:
        return nm.path_kind(self.link, peer.info.get("link"))

    def start_director(self, tone: str = "silent") -> TimedProcess:
        self.starts += 1
        self.report_file = self.work / f"director-report-{self.starts}.jsonl"
        self.director = TimedProcess(
            ["-m", "director.main", "--registry-file", str(self.registry),
             "--tls-cert", str(self.cert), "--tls-key", str(self.key),
             "--port", str(AUDIO_PORT), "--discovery-port", str(DISCOVERY_PORT),
             "--enrollment-port", str(ENROLL_PORT),
             "--director-id-file", str(self.work / "director.id"),
             "--start-check-seconds", "2", "--test-tone", tone,
             "--test-report", str(self.report_file)],
            label="Director", show=not self.quiet)
        if self.director.wait_for("[Manager] Εξυπηρέτηση: ενεργή", timeout=30) is None:
            raise RuntimeError("Ο Director δεν ξεκίνησε: " + " | ".join(self.director.lines[-5:]))
        if self.director.wait_for("Πηγή δοκιμής ενεργή", timeout=10) is None:
            raise RuntimeError("Ο Director δεν ξεκίνησε την πηγή δοκιμής")
        self.command("none")
        return self.director

    def command(self, text: str) -> None:
        self.director.send(text)

    def kill_director(self) -> None:
        if self.director is not None:
            self.director.proc.kill()
            self.director.proc.wait(10)
            self.director = None

    def stop_director(self) -> None:
        if self.director is not None:
            self.director.stop(quit_command="quit")
            self.director = None

    def pubs(self, since_us: int = 0) -> list[dict]:
        time.sleep(0.8)      # το αρχείο γράφεται ανά 0,5 s
        return [event for event in read_report(self.report_file)
                if event.get("ev") == "pub" and event["t"] >= since_us]

    def wait_joined(self, ids: list[str], since: int, timeout: float = JOIN_TIMEOUT
                    ) -> dict[str, int | None]:
        deadline = time.monotonic() + timeout
        found: dict[str, int | None] = {client_id: None for client_id in ids}
        while time.monotonic() < deadline:
            for client_id in ids:
                if found[client_id] is None:
                    found[client_id] = self.director.first_time(client_id, ") συνδέθηκε",
                                                                since=since)
            if all(value is not None for value in found.values()):
                break
            time.sleep(0.1)
        return found

    def announce(self, seconds: float) -> None:
        self.command("all")
        if seconds >= 120:          # μεγάλη ανακοίνωση: ένδειξη ότι η δοκιμή προχωρά
            ended = time.monotonic() + seconds
            while (left := ended - time.monotonic()) > 0:
                time.sleep(min(60.0, left))
                remaining = ended - time.monotonic()
                if remaining > 1:
                    line = (f"    ... η μέτρηση συνεχίζεται (σιωπηλά), απομένουν "
                            f"{remaining / 60:.0f} λεπτ{'ό' if remaining < 90 else 'ά'}")
                    print(line, flush=True)
                    self.tell_peers(line.strip())
        else:
            time.sleep(seconds)
        self.command("none")

    def measure(self, since_us: int, after: dict[int, int], label: str
                ) -> tuple[list[nm.Run], list[nm.ClientResult]]:
        """Ανάλυση των ανακοινώσεων από since_us για κάθε πραγματικό Client."""
        time.sleep(1.5)    # να αδειάσει η ουρά και να γραφτούν τα αρχεία
        runs = nm.runs_from_pubs(self.pubs(since_us))
        results = []
        for peer in self.peers:
            peer.sync()
            events = peer.events(after[peer.index])
            results.append(nm.analyse(peer.name, runs, events, peer.clock))
        self.collected[label] = results
        return runs, results

    def marks(self) -> dict[int, int]:
        return {peer.index: peer.now() for peer in self.peers}

    # ------------------------------------------------------------ προετοιμασία
    def accept_peers(self) -> None:
        from shared.discovery import announce
        stop = threading.Event()
        threading.Thread(target=announce, args=(stop, str(uuid.uuid4()), CONTROL_PORT),
                         kwargs={"udp_port": BEACON_PORT, "interval": 1.0,
                                 "report": lambda *_: None},
                         daemon=True).start()
        with socket.create_server(("0.0.0.0", CONTROL_PORT), reuse_port=False) as server:
            if self.loopback:
                for index in range(self.clients):
                    self.helpers.append(Process(
                        [str(Path(__file__).resolve()), "client", "--director-host",
                         "127.0.0.1", "--name", f"Loop{index + 1}", "--link", "lan"],
                        label=f"Βοηθός {index + 1}", show=False))
            else:
                print(f"\nΑναμονή για {self.clients} υπολογιστή/ές Client. Σε καθένα τρέξτε:\n"
                      "    Linux:   tools/network_live_test.sh client\n"
                      "    Windows: powershell -ExecutionPolicy Bypass -File "
                      "tools\\network_live_test.ps1\n"
                      "(Αν δεν βρεθεί αυτόματα, προσθέστε --director-host / -DirectorHost "
                      "<IP αυτού του υπολογιστή>.)\n", flush=True)
            server.settimeout(0.5)          # Ctrl+C και στα Windows
            while len(self.peers) < self.clients:
                try:
                    connection, address = server.accept()
                except socket.timeout:
                    continue
                connection.settimeout(None)
                connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                peer = Peer(len(self.peers), Channel(connection), address[0])
                self.peers.append(peer)
                print(f"Συνδέθηκε βοηθός {len(self.peers)}/{self.clients} από {address[0]}",
                      flush=True)
        stop.set()

    def prepare(self) -> tuple[str, str]:
        from director.tls_identity import create_director_identity
        self.cert.parent.mkdir(parents=True, exist_ok=True)
        create_director_identity(self.cert, self.key)
        self.accept_peers()
        registry = SignedClientRegistry(self.registry, self.cert, self.key)
        lines = []
        for peer in self.peers:
            peer.info = peer.ask("hello", director_cert=self.cert.read_text(encoding="ascii"),
                                 room=peer.room, virtual=self.virtual,
                                 buffer_ms=self.buffer_ms)
            registry.approve(peer.client_id, peer.room, peer.info["real"]["pem"].encode("ascii"),
                             name=peer.name[:64])
            for index, item in enumerate(peer.info["virtual"]):
                registry.approve(item["id"], VIRTUAL_ROOM, item["pem"].encode("ascii"),
                                 name=f"V{peer.index + 1}-{index + 1:02d}")
        self.link = self.forced_link or link_type(
            self.peers[0].channel.sock.getsockname()[0])
        bad = []
        for peer in self.peers:
            peer.sync()
            lines.append(f"{peer.name} ({peer.address}, {peer.info.get('os')}): "
                         f"{link_label(peer.info.get('link'))}, διαδρομή {self.kind(peer)}, "
                         f"ρολόι ±{peer.clock.error_ms:.1f} ms")
            if peer.clock.error_ms > 50:
                bad.append(peer.name)
        detail = (f"Director: {link_label(self.link)}"
                  + (f", ουρά Client {self.buffer_ms} ms" if self.buffer_ms else "") + " | " + " | ".join(lines)
                  + (f" | ασταθές ρολόι: {', '.join(bad)}" if bad else ""))
        return ("FAIL" if bad else "PASS"), detail

    # ------------------------------------------------------------ βήματα
    def s02_connect(self):
        self.start_director()
        mark = self.director.mark()
        for peer in self.peers:
            peer.ask("start_real", port=AUDIO_PORT)
        found = self.wait_joined([peer.client_id for peer in self.peers], mark)
        missing = [peer.name for peer in self.peers if found[peer.client_id] is None]
        # Στην πράξη ο Client είναι συνδεδεμένος πολύ πριν από την ανακοίνωση· η κάρτα
        # ήχου χρειάζεται λίγα δευτερόλεπτα να σταθεροποιηθεί μετά το άνοιγμα (phase4-4 §5).
        time.sleep(WARMUP_SECONDS)
        return not missing, ("όλοι συνδέθηκαν" if not missing
                             else f"δεν συνδέθηκαν: {', '.join(missing)}")

    def check_all(self, results: list[nm.ClientResult], *, edges: bool) -> tuple[bool, str]:
        oks, details = [], []
        for peer, result in zip(self.peers, results):
            ok, detail = nm.check(result, self.kind(peer), edges=edges)
            oks.append(ok)
            details.append(detail)
        return all(oks), "\n       ".join(details)

    def s03_latency(self):
        after, since = self.marks(), now_us()
        self.announce(60)
        _, results = self.measure(since, after, "latency")
        return self.check_all(results, edges=False)

    def s04_edges(self):
        after, since = self.marks(), now_us()
        for _ in range(10):
            self.announce(2.0)
            time.sleep(1.5)
        for _ in range(5):     # γρήγορες, η μία μετά την άλλη
            self.announce(0.5)
            time.sleep(0.5)
        runs, results = self.measure(since, after, "edges")
        ok, detail = self.check_all(results, edges=True)
        return ok, f"{len(runs)} ανακοινώσεις\n       {detail}"

    def s05_sync(self):
        if len(self.peers) < 2:
            return "SKIP", "χρειάζονται τουλάχιστον 2 υπολογιστές Client"
        worst = []
        for label in ("latency", "edges"):
            results = self.collected.get(label) or []
            if len(results) == len(self.peers):
                worst.append(nm.sync_spread_ms(results))
        if not worst or all(item[2] == 0 for item in worst):
            return False, "κανένα κοινό block για σύγκριση"
        biggest = max(item[0] for item in worst if item[2])
        p95 = max(item[1] for item in worst if item[2])
        error = sum(peer.clock.error_ms for peer in self.peers)
        return biggest <= nm.SYNC_LIMIT_MS, (
            f"μέγιστη διαφορά {biggest:.0f} ms, p95 {p95:.0f} ms "
            f"(σφάλμα ρολογιών έως ±{error:.0f} ms, όριο {nm.SYNC_LIMIT_MS} ms)")

    def s06_long(self):
        after, since = self.marks(), now_us()
        mark = self.director.mark()
        self.announce(self.long_seconds)
        _, results = self.measure(since, after, "long")
        ok, detail = self.check_all(results, edges=False)
        drops = [peer.name for peer in self.peers
                 if self.director.first_time(peer.client_id, "αποσυνδέθηκε", since=mark)]
        lost = [peer.name for peer in self.peers
                if any(event.get("ev") in ("lost", "connected")
                       for event in peer.events(after[peer.index]))]
        drifts = [nm.drift_ms(result) for result in results]
        drift_ok = all(value <= nm.DRIFT_LIMIT_MS for value in drifts)
        return ok and not drops and not lost and drift_ok, (
            f"{self.long_seconds} s, αποσυνδέσεις: "
            f"{', '.join(sorted(set(drops + lost))) or 'καμία'}, αύξηση καθυστέρησης "
            + ", ".join(f"{value:+.0f} ms" for value in drifts)
            + f" (όριο +{nm.DRIFT_LIMIT_MS})\n       {detail}")

    def s07_load(self):
        if self.virtual <= 0:
            return "SKIP", "χωρίς εικονικούς Clients (--virtual 0)"
        mark = self.director.mark()
        connected = sum(peer.ask("vstart", port=AUDIO_PORT)["connected"] for peer in self.peers)
        expected = self.virtual * len(self.peers)
        ids = [item["id"] for peer in self.peers for item in peer.info["virtual"]]
        found = self.wait_joined(ids, mark, timeout=20)
        joined = sum(value is not None for value in found.values())
        after, since = self.marks(), now_us()
        self.announce(30)
        time.sleep(1.0)
        for _ in range(5):
            self.announce(2.0)
            time.sleep(1.5)
        runs, results = self.measure(since, after, "load")
        ok, detail = self.check_all(results, edges=True)
        worst, errors = 1.0, []
        for peer in self.peers:
            reply = peer.ask("vseqs")
            errors += reply.get("errors", [])
            for seqs in reply["seqs"]:
                got, total = nm.virtual_received(runs, set(seqs))
                worst = min(worst, got / total if total else 0.0)
            peer.ask("vstop")
        bitrate = (len(self.peers) + joined) * 0.3
        virtual_ok = joined == expected and worst >= 0.99
        return ok and virtual_ok, (
            f"εικονικοί {joined}/{expected} (συνδέθηκαν {connected}), ο χειρότερος έλαβε "
            f"{worst * 100:.1f}% των blocks, ~{bitrate:.0f} Mbps από τον Director"
            + (f", σφάλματα: {'; '.join(sorted(set(errors))[:3])}" if errors else "")
            + f"\n       {detail}")

    def s08_restart(self):
        self.kill_director()
        killed = now_us()
        self.start_director()
        ready = now_us()
        found = self.wait_joined([peer.client_id for peer in self.peers], 0, timeout=30)
        times = {peer.name: (found[peer.client_id] - ready) / 1e6
                 if found[peer.client_id] else None for peer in self.peers}
        after, since = self.marks(), now_us()
        self.announce(3)
        _, results = self.measure(since, after, "restart")
        heard = all(result.played > 0 for result in results)
        ok = all(value is not None and value <= nm.RECONNECT_LIMIT_S for value in times.values())
        text = ", ".join(f"{name} {'—' if value is None else f'{value:.1f} s'}"
                         for name, value in times.items())
        return ok and heard, (f"Director ξανά έτοιμος σε {(ready - killed) / 1e6:.1f} s· "
                              f"επανασύνδεση μετά από αυτό: {text} (όριο "
                              f"{nm.RECONNECT_LIMIT_S:.0f} s), ήχος μετά: "
                              f"{'ναι' if heard else 'όχι'}")

    def through_proxy(self) -> bool:
        if self.proxy is None:
            self.proxy = FreezableProxy(PROXY_PORT, AUDIO_PORT, listen="0.0.0.0", source=None)
        mark = self.director.mark()
        for peer in self.peers:
            peer.ask("start_real", port=PROXY_PORT)
        found = self.wait_joined([peer.client_id for peer in self.peers], mark)
        return all(value is not None for value in found.values())

    def s09_freeze(self):
        if not self.through_proxy():
            return False, "οι Clients δεν συνδέθηκαν μέσω του proxy"
        for peer in self.peers:
            peer.sync()
        after = self.marks()
        mark = self.director.mark()
        self.command("all")
        time.sleep(2)
        frozen = now_us()
        self.proxy.freeze()
        time.sleep(10)
        thawed = now_us()
        self.proxy.thaw()
        found = self.wait_joined([peer.client_id for peer in self.peers], mark, timeout=25)
        self.command("none")
        details, oks = [], []
        for peer in self.peers:
            dropped = self.director.first_time(peer.client_id, "αποσυνδέθηκε", since=mark)
            director_s = None if dropped is None else (dropped - frozen) / 1e6
            lost = [event for event in peer.events(after[peer.index]) if event["ev"] == "lost"]
            client_s = (None if not lost
                        else (peer.clock.to_director(lost[0]["t"]) - frozen) / 1e6)
            joined = found[peer.client_id]
            back_s = None if joined is None or joined < thawed else (joined - thawed) / 1e6
            oks.append(director_s is not None and director_s <= nm.LOSS_DETECT_DIRECTOR_S
                       and client_s is not None and client_s <= nm.LOSS_DETECT_CLIENT_S
                       and back_s is not None and back_s <= nm.RECONNECT_LIMIT_S)
            fmt = lambda value: "—" if value is None else f"{value:.1f} s"  # noqa: E731
            details.append(f"{peer.name}: Client κατάλαβε σε {fmt(client_s)}, Director σε "
                           f"{fmt(director_s)}, επανασύνδεση {fmt(back_s)} μετά")
        return all(oks), ("όρια: Client ≤ 4,5 s, Director ≤ 7,5 s, επανασύνδεση ≤ 15 s"
                          "\n       " + "\n       ".join(details))

    def s10_short_freeze(self):
        after = self.marks()
        mark = self.director.mark()
        self.command("all")
        time.sleep(2)
        frozen = now_us()
        self.proxy.freeze()
        time.sleep(0.5)
        self.proxy.resume()
        time.sleep(8)
        self.command("none")
        time.sleep(1.5)
        details, oks = [], []
        for peer in self.peers:
            peer.sync()
            events = peer.events(after[peer.index])
            dropped = self.director.first_time(peer.client_id, "αποσυνδέθηκε", since=mark)
            lost = any(event["ev"] == "lost" for event in events)
            later = sum(1 for event in events if event["ev"] == "play"
                        and peer.clock.to_director(event["t"]) > frozen + 2_000_000)
            gaps = sum(1 for event in events if event["ev"] == "underrun")
            oks.append(dropped is None and not lost and later > 100)
            details.append(f"{peer.name}: αποσύνδεση {'ναι' if dropped or lost else 'όχι'}, "
                           f"blocks μετά το κόλλημα {later}, κενά {gaps}")
        return all(oks), "\n       ".join(details)

    def s11_listen(self):
        # Οι μετρήσεις στέλνουν σιωπή (ο αύξων μετρά, όχι ο ήχος)· μελωδία μόνο εδώ.
        self.stop_director()
        mark = 0
        self.start_director("melody")
        found = self.wait_joined([peer.client_id for peer in self.peers], mark, timeout=30)
        if not all(found.values()):
            return False, "οι Clients δεν ξανασυνδέθηκαν για την ακρόαση"
        time.sleep(WARMUP_SECONDS)
        self.command("all")
        time.sleep(1)
        if not self.ask_user:
            time.sleep(7)
            self.command("none")
            return "SKIP", "χωρίς ερώτηση (--no-ask)"
        print("\n>>> Παίζει μελωδία δοκιμής σε όλους τους υπολογιστές Client για ~10 s.",
              flush=True)
        time.sleep(9)
        self.command("none")
        answer = input(">>> Ακούστηκε καθαρά, χωρίς κοψίματα και κλικ, σε όλα τα PC; [ν/ο]: ")
        yes = answer.strip().lower() in ("y", "yes", "ν", "ναι")
        return yes, f"απάντηση: {answer.strip() or '—'}"

    # ------------------------------------------------------------ εκτέλεση
    def run(self) -> int:
        started = time.monotonic()
        try:
            self.step("Βοηθοί Client, είδος δικτύου, ευθυγράμμιση ρολογιών", self.prepare)
            if self.results[-1][1] == "FAIL" and not self.peers:
                raise RuntimeError("χωρίς βοηθούς Client")
            for title, function, minutes in (
                    ("Οι πραγματικοί Clients συνδέονται με mTLS", self.s02_connect, 0),
                    ("Καθυστέρηση αποστολή→ηχείο, 60 s συνεχής ανακοίνωση",
                     self.s03_latency, 1),
                    ("Πρώτα/τελευταία 3 blocks: 10 ανακοινώσεις + 5 γρήγορες",
                     self.s04_edges, 1),
                    ("Συγχρονισμός αιθουσών (≤ 1 s)", self.s05_sync, 0),
                    ("Ήχος και heartbeat μαζί, χωρίς αποσύνδεση και χωρίς αύξηση "
                     "καθυστέρησης", self.s06_long, round(self.long_seconds / 60)),
                    ("Φόρτος: εικονικοί Clients μέσα από το πραγματικό δίκτυο",
                     self.s07_load, 1),
                    ("Επανεκκίνηση Director (τερματισμός διεργασίας)", self.s08_restart, 0),
                    ("Πάγωμα δικτύου 10 s: εντοπισμός και επανασύνδεση", self.s09_freeze, 0),
                    ("Σύντομο κόλλημα 0,5 s: χωρίς αποσύνδεση", self.s10_short_freeze, 0),
                    ("Ακρόαση: καθαρός ήχος (ερώτηση)", self.s11_listen, 0)):
                self.step(title, function, minutes)
        except Exception as exc:  # noqa: BLE001
            self.results.append(("Απρόσμενο σφάλμα δοκιμής", "FAIL",
                                 f"{type(exc).__name__}: {exc}"))
        finally:
            self.close()
        return self.summary(time.monotonic() - started)

    def summary(self, seconds: float) -> int:
        passed = sum(state == "PASS" for _, state, _ in self.results)
        skipped = sum(state == "SKIP" for _, state, _ in self.results)
        failed = sum(state == "FAIL" for _, state, _ in self.results)
        total = (f"ΣΥΝΟΛΟ: {passed}/{TOTAL} PASS" + (f", {skipped} SKIP" if skipped else "")
                 + (f", {failed} FAIL" if failed else "") + f" ({seconds / 60:.1f} λεπτά)")
        lines = [f"Δοκιμή 4.4 σε πραγματικό δίκτυο — {self.stamp} — Director "
                 f"{platform.system()} {platform.release()}, Python {platform.python_version()}",
                 ""]
        for number, (title, state, detail) in enumerate(self.results, 1):
            lines.append(f"{state}  {number:02d}. {title}")
            lines += [f"       {item.strip()}" for item in detail.splitlines() if item.strip()]
        lines += ["", total]
        text = "\n".join(lines)
        print("\n" + "=" * 70 + "\n" + text, flush=True)
        saved = None
        try:
            folder = Path(__file__).resolve().parents[1] / "test-results"
            folder.mkdir(exist_ok=True)
            saved = folder / f"network-{time.strftime('%Y%m%d-%H%M')}.txt"
            saved.write_text(text + "\n", encoding="utf-8")
            print(f"Αποθηκεύτηκε: {saved}", flush=True)
        except OSError as exc:
            print(f"Δεν αποθηκεύτηκε η αναφορά: {exc}", flush=True)
        for peer in self.peers:
            try:
                peer.channel.send(cmd="done", text=text)
            except OSError:
                pass
        for helper in self.helpers:
            try:
                helper.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                helper.stop()
        return 0 if failed == 0 and passed + skipped == TOTAL else 1

    def close(self) -> None:
        if self.proxy is not None:
            self.proxy.close()
        self.stop_director()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("role", choices=("director", "client"))
    parser.add_argument("--clients", type=int, default=1,
                        help="director: πόσοι υπολογιστές Client θα συνδεθούν (default 1)")
    parser.add_argument("--virtual", type=int, default=20,
                        help="director: εικονικοί Clients ανά υπολογιστή Client (default 20)")
    parser.add_argument("--long-seconds", type=int, default=300,
                        help="director: διάρκεια της μεγάλης ανακοίνωσης (default 300)")
    parser.add_argument("--client-buffer-ms", type=int, default=None,
                        help="director: --buffer-ms των Clients (προεπιλογή του Client: 250)")
    parser.add_argument("--no-ask", action="store_true",
                        help="director: χωρίς την τελική ερώτηση (SKIP)")
    parser.add_argument("--loopback", action="store_true",
                        help="director: βοηθοί Client στον ίδιο υπολογιστή (έλεγχος script)")
    parser.add_argument("--director-host", default=None,
                        help="client: IP του Director, αν δεν βρεθεί αυτόματα")
    parser.add_argument("--name", default=None, help="client: όνομα στα αποτελέσματα")
    parser.add_argument("--link", choices=("lan", "wifi"), default=None,
                        help="είδος σύνδεσης, αν δεν αναγνωρίζεται σωστά αυτόματα")
    parser.add_argument("--verbose", action="store_true",
                        help="εμφάνιση και των γραμμών των διεργασιών Director/Client")
    args = parser.parse_args()
    if args.role == "director" and not 1 <= args.clients <= 8:
        parser.error("--clients: 1–8")
    if args.role == "director" and not 0 <= args.virtual * args.clients <= 60:
        parser.error("--virtual × --clients: έως 60 (όριο Manager 70 μαζί με τους πραγματικούς)")
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"Δοκιμή 4.4 σε πραγματικό δίκτυο — {args.role} — {stamp} — "
          f"{platform.system()} {platform.release()}, Python {platform.python_version()}",
          flush=True)
    with tempfile.TemporaryDirectory(prefix="sch-4.4-test-") as directory:
        work = Path(directory)
        if args.role == "director":
            return NetworkTest(work, clients=args.clients, virtual=args.virtual,
                               ask=not (args.no_ask or args.loopback),
                               quiet=not args.verbose,
                               loopback=args.loopback, link=args.link,
                               long_seconds=args.long_seconds,
                               buffer_ms=args.client_buffer_ms).run()
        return Agent(work, args.director_host, args.name, args.link, not args.verbose).run()


if __name__ == "__main__":
    sys.exit(main())
