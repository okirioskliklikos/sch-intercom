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
