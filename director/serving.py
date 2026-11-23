"""Ενεργοποίηση/διακοπή εξυπηρέτησης χωρίς εξάρτηση από Qt (4.2D1)."""
from __future__ import annotations

from pathlib import Path
import threading
import time

from director.manager_state import change_serving, load
from shared.discovery import BeaconWatcher


class ServingConflict(ValueError):
    """Υπάρχει beacon δεύτερου Manager της ίδιας εγκατάστασης."""


class ServingController:
    """Ο κύκλος ζωής των sockets ορίζεται από callbacks, όχι από το CLI/GUI.

    start_network(role) πρέπει να ανοίγει TCP/UDP και εγγραφή μόνο για primary.
    stop_network() πρέπει να αποσυνδέει Clients και να κλείνει όλες τις θύρες.
    change_enrollment(enable) ανοίγει/κλείνει τη θύρα εγγραφής σε αλλαγή ρόλου.
    """

    def __init__(self, state_path: Path, watcher: BeaconWatcher, *,
                 start_network, stop_network, change_enrollment=None,
                 check_seconds: float = 3.0, clock=time.monotonic, sleeper=time.sleep,
                 report=print):
        self.state_path = Path(state_path)
        self.watcher = watcher
        self.start_network = start_network
        self.stop_network = stop_network
        self.change_enrollment = change_enrollment
        self.check_seconds = check_seconds
        self.clock, self.sleeper = clock, sleeper
        self.report = report
        self.active = False
        self._lock = threading.RLock()
        self._previous_conflicts: set[str] = set()

    def _other(self):
        return self.watcher.others()

    def start(self, force: bool = False, *, persist: bool = True) -> bool:
        with self._lock:
            if self.active:
                return True
            # Αν ο watcher ξεκίνησε πριν από λιγότερο από 3 s, περιμένουμε
            # μόνο το υπόλοιπο του παραθύρου πριν αποφασίσουμε.
            began = self.watcher.started_at
            elapsed = 0 if began is None else max(0, self.clock() - began)
            waiting = max(0, self.check_seconds - elapsed)
            if waiting:
                self.sleeper(waiting)
            others = self._other()
            if others and not force:
                other = others[0]
                raise ServingConflict(
                    f"Ο υπολογιστής {other.info.host} ({other.ip}) "
                    "εξυπηρετεί ήδη. Σταματήστε τον πρώτα.")
            if others and force:
                self.report("ΠΡΟΣΟΧΗ: Έναρξη παρ' όλα αυτά ενώ "
                            "εξυπηρετεί ήδη άλλος Manager.")
            role = load(self.state_path).role
            self.start_network(role)
            self.active = True
            if persist:
                change_serving(self.state_path, True)
            return True

    def stop(self, *, persist: bool = True) -> None:
        with self._lock:
            if self.active:
                self.stop_network()
                self.active = False
            if persist:
                change_serving(self.state_path, False)

    def role_changed(self) -> None:
        with self._lock:
            if self.active and self.change_enrollment is not None:
                self.change_enrollment(load(self.state_path).role == "primary")

    def poll_conflicts(self) -> list:
        """Ειδοποιεί μία φορά ανά εμφάνιση, ποτέ αυτόματη διακοπή."""
        others = self._other()
        present = {item.info.inst for item in others}
        if self.active:
            for item in others:
                if item.info.inst not in self._previous_conflicts:
                    self.report("ΠΡΟΣΟΧΗ: εξυπηρετεί και ο υπολογιστής "
                                f"{item.info.host} ({item.ip}). "
                                "Σταματήστε τον έναν.")
        self._previous_conflicts = present
        return others

    def startup(self) -> bool:
        """Χρησιμοποιεί την αποθηκευμένη προτίμηση· σε σύγκρουση δεν τη σβήνει."""
        if not load(self.state_path).serving:
            return False
        try:
            return self.start(persist=False)
        except ServingConflict as exc:
            self.report(str(exc))
            return False

    def shutdown(self) -> None:
        """Πλήρης έξοδος Manager, χωρίς αλλαγή της αποθηκευμένης προτίμησης."""
        self.stop(persist=False)


class SocketServingService:
    """Ανοιγοκλείνει κοινές TCP/UDP/registration θύρες για CLI και GUI."""

    def __init__(self, args, *, identifier: str, inst: str, registry,
                 accept_clients, broadcaster, enrollment):
        import socket
        from director.broadcast import MAX_CLIENTS
        self.args, self.identifier, self.inst = args, identifier, inst
        self.registry, self.accept_clients = registry, accept_clients
        self.broadcaster, self.enrollment = broadcaster, enrollment
        self.server = None
        self.stop_event = None
        self.threads: list[threading.Thread] = []

    def start(self, role: str) -> None:
        import socket
        from director.broadcast import MAX_CLIENTS
        from shared.discovery import announce
        args = self.args
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((args.host, args.port))
            server.listen(MAX_CLIENTS)
            stopped = threading.Event()
            school = self.registry.snapshot()["school_id"]
            beacon_kwargs = {
                "udp_port": args.discovery_port, "school": school,
                "inst": self.inst, "registry_version": lambda: self.registry.version,
                "extra_ports": tuple([args.extra_beacon_port])
                if getattr(args, "extra_beacon_port", None) else (),
            }
            beacon = threading.Thread(
                target=announce, args=(stopped, self.identifier, args.port),
                kwargs=beacon_kwargs, daemon=True, name="manager-discovery")
            listener = threading.Thread(
                target=self.accept_clients, args=(server, self.broadcaster, stopped),
                daemon=True, name="manager-accept")
            self.server, self.stop_event = server, stopped
            self.threads = [beacon, listener]
            for worker in self.threads:
                worker.start()
            if role == "primary" and self.enrollment is not None:
                self.enrollment.open()
        except BaseException:
            if self.stop_event is not None:
                self.stop_event.set()
            server.close()
            self.server = None
            self.stop_event = None
            raise

    def change_enrollment(self, enabled: bool) -> None:
        if self.enrollment is None:
            return
        if enabled:
            self.enrollment.open()
        else:
            self.enrollment.stop()

    def stop(self) -> None:
        if self.enrollment is not None:
            self.enrollment.stop()
        if self.stop_event is not None:
            self.stop_event.set()
        if self.server is not None:
            self.server.close()
        # Δεν κλείνουμε τον AudioBroadcaster: μπορεί να ξαναρχίσει να εξυπηρετεί.
        for client_id, _room, active, _age in self.broadcaster.status_snapshot():
            if active:
                self.broadcaster.disconnect_client(client_id)
        for worker in self.threads:
            worker.join(timeout=1.0)
        self.server = None
        self.stop_event = None
        self.threads = []
