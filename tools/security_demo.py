"""Επίδειξη της σελίδας «Ασφάλεια» γεμάτης, χωρίς να αγγίζει το πραγματικό μητρώο.

Χρήση (Linux ή Windows, χρειάζεται PySide6):
    python -m tools.security_demo

Ανοίγει το παράθυρο Director στη σελίδα «Ασφάλεια» με 2 αιτήματα εγγραφής και
26 εγγεγραμμένους υπολογιστές σε 15 αίθουσες (κάποιοι online, κάποιοι «πριν …»,
κάποιοι «Δεν έχει συνδεθεί»). Όλα γίνονται σε προσωρινό φάκελο με δικό του
πιστοποιητικό Director, μητρώο και θύρα εγγραφής σε 127.0.0.1 (τυχαία θύρα),
και σβήνονται όταν κλείσει το παράθυρο. Ο φάκελος ~/.school-intercom δεν
διαβάζεται ούτε γράφεται. Το μικρόφωνο και οι Clients είναι ψεύτικοι.

Οι κουκκίδες online/offline είναι σταθερές· έγκριση, απόρριψη, αλλαγή αίθουσας,
μετονομασία και ανάκληση δουλεύουν κανονικά στο προσωρινό μητρώο.

Κύριος/Εφεδρικός (4.2D2): --role primary | restored | conflict | newer | backup | stopped.
Ο «άλλος Manager» είναι ψεύτικος (κανένα δίκτυο)· αλλαγή ρόλου και Έναρξη/Διακοπή
δουλεύουν πάνω στο προσωρινό manager-state.json.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile
import uuid

# Ενδεικτικές αίθουσες και υπολογιστές: 15 αίθουσες, 26 υπολογιστές.
ROOMS = {
    "C2627-Α1": ["Board1", "PC"],
    "C2627-Α2": ["Board1", "PC"],
    "C2627-Β1": ["Board1", "PC"],
    "C2627-Β2": ["Board1", "PC"],
    "C2627-Γ1": ["Board1", "PC", "Projector-PC"],
    "C2627-Γ2": ["Board1", "PC"],
    "C2627-Δ1": ["Board1"],
    "C2627-Δ2": ["Board1", "PC"],
    "C2627-Ε1": ["Board1"],
    "C2627-Ε2": ["Board1"],
    "Τμήμα Ένταξης": ["PC"],
    "Τμήμα Υποδοχής": ["PC"],
    "Εργαστήριο Πληροφορικής": ["PC01", "PC02", "PC03", "PC-Καθηγητή"],
    "Γραφείο Διευθυντή": ["Laptop-Διευθυντή-Παλιό-2019"],
    "Γραφείο Καθηγητών": ["PC"],
}
REQUESTS = [("C2627-Ζ1", "Board1"), ("Αίθουσα Μουσικής", "Laptop")]
# Κατάσταση με τη σειρά: online, «πριν …» (δευτερόλεπτα), ή None = δεν συνδέθηκε ποτέ.
STATES = [True, 600, True, 7200, True, 3 * 86400, True, 30, None, True, 2400]
ROLE_SCENARIOS = ("primary", "restored", "conflict", "newer", "backup", "stopped")
DEMO_HOST = "ΓΡΑΜΜΑΤΕΙΑ-PC"
OTHER_HOST, OTHER_IP = "ΓΡΑΜΜΑΤΕΙΑ-2", "192.168.1.35"


class DemoBroadcaster:
    """Σταθερή κατάσταση online/offline· οι στόχοι και οι ενημερώσεις αγνοούνται."""

    def __init__(self):
        self.status: dict[str, tuple[str, bool, float]] = {}

    def status_snapshot(self):
        return sorted((cid, room, online, age)
                      for cid, (room, online, age) in self.status.items())

    def set_targets(self, **_kwargs):
        pass

    def publish(self, *_args):
        pass

    def update_client_info(self, client_id, room, _name):
        if client_id in self.status:
            _old, online, age = self.status[client_id]
            self.status[client_id] = (room, online, age)

    def disconnect_client(self, client_id):
        self.status.pop(client_id, None)


class DemoWatcher:
    """Ψεύτικος BeaconWatcher: οι «άλλοι Managers» ορίζονται από το σενάριο."""
    started_at = None

    def __init__(self):
        self.seen = []

    def others(self):
        return list(self.seen)

    def add(self, host: str, ip: str, rv: int) -> None:
        from shared.discovery import BeaconInfo, SeenManager
        import time
        info = BeaconInfo(str(uuid.uuid4()), 50555, "demo", str(uuid.uuid4()), rv, host)
        self.seen.append(SeenManager(info, ip, time.monotonic()))


def build_role(root: Path, scenario: str, registry_version):
    """RoleModel με τον πραγματικό ServingController, χωρίς δίκτυο."""
    from director import manager_state
    from director.security import RoleModel
    from director.serving import ServingController
    state = manager_state.path_for(root / "clients-registry.json")
    watcher = DemoWatcher()
    version = registry_version()
    if scenario == "restored":
        manager_state.restored(state)
    else:
        manager_state.save(state, manager_state.ManagerState(
            role="backup" if scenario in ("newer", "backup") else "primary",
            serving=scenario not in ("newer", "stopped")))
    if scenario == "conflict":
        watcher.add(OTHER_HOST, OTHER_IP, version)
    elif scenario == "newer":
        watcher.add(OTHER_HOST, OTHER_IP, version + 2)
    controller = ServingController(state, watcher, start_network=lambda _role: None,
                                   stop_network=lambda: None, check_seconds=0,
                                   report=lambda _m: None)
    controller.startup() if scenario != "conflict" else controller.start(force=True)
    return RoleModel(state, controller, watcher, registry_version=registry_version,
                     host=DEMO_HOST)


def fake_stream(_gate, *, stop, ready_callback, level_callback, **_kwargs):
    ready_callback()
    while not stop.wait(0.05):
        level_callback(0.0)


def build(root: Path, log=print):
    from director.main import RegistryTrust
    from director.security import LastSeenStore, SecurityModel, last_seen_path
    from director.tls_identity import create_director_identity
    from shared import enrollment as enrollment_module
    from shared.client_registry import SignedClientRegistry
    from shared.device_identity import create_device_identity
    from shared.enrollment import EnrollmentServer, enroll_client, make_request

    cert, key = root / "director.crt", root / "director.key"
    create_director_identity(cert, key)
    registry_file = root / "clients-registry.json"
    registry = SignedClientRegistry(registry_file, cert, key)
    trust = RegistryTrust(registry, cert, key)
    # Μόνο σε αυτή τη διεργασία: ~30 αιτήματα από 127.0.0.1 μέσα σε ένα λεπτό.
    enrollment_module.MAX_ATTEMPTS_PER_MINUTE = 1000
    enrollment = EnrollmentServer(cert, key, trust, host="127.0.0.1", port=0,
                                  pending_file=root / "enrollment-pending.json",
                                  log=lambda _m: None)
    if not enrollment.open():
        raise SystemExit("Η προσωρινή θύρα εγγραφής δεν άνοιξε")
    broadcaster = DemoBroadcaster()
    last_seen = LastSeenStore(last_seen_path(registry_file), log=lambda _m: None)
    model = SecurityModel(enrollment, trust, broadcaster, last_seen)

    def submit(room, name):
        client_id = str(uuid.uuid4())
        device_cert, device_key, password = create_device_identity(
            root / "devices" / client_id, client_id)
        request = make_request(client_id, room, name, device_cert.read_bytes(),
                               device_key.read_bytes(), password)
        queued = enroll_client("127.0.0.1", enrollment.port, cert,
                               "school-intercom-director", request)
        return client_id, queued["code"]

    index = 0
    for room, names in ROOMS.items():
        for name in names:
            client_id, code = submit(room, name)
            [request_id] = [r.request_id for r in model.requests()
                            if (r.room, r.name) == (room, name)]
            if not model.approve(request_id, code).ok:
                raise SystemExit(f"Η έγκριση {room} · {name} απέτυχε")
            state = STATES[index % len(STATES)]
            index += 1
            if state is True:
                broadcaster.status[client_id] = (room, True, 0.1)
            elif state is not None:
                broadcaster.status[client_id] = (room, False, float(state))
    last_seen.update(broadcaster.status_snapshot())
    codes = [(room, name, submit(room, name)[1]) for room, name in REQUESTS]
    log(f"{index} υπολογιστές σε {len(ROOMS)} αίθουσες, {len(codes)} αιτήματα.")
    for room, name, code in codes:
        log(f"  Κωδικός για έγκριση {room} · {name}: {code}")
    return broadcaster, model, enrollment


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--width", type=int, default=None, help="πλάτος παραθύρου")
    parser.add_argument("--role", choices=ROLE_SCENARIOS, default="primary",
                        help="σενάριο Κύριου/Εφεδρικού")
    parser.add_argument("--tab", choices=("computers", "certificates"), default="computers")
    parser.add_argument("--screenshot", type=Path, default=None,
                        help="μόνο στιγμιότυπο (offscreen) και έξοδος")
    args = parser.parse_args()
    if args.screenshot:
        from shared.ui.offscreen import use_offscreen
        use_offscreen()  # offscreen· στα Windows και γραμματοσειρές του συστήματος

    from director.gui import DirectorMicrophone
    from director.qt_gui import DirectorWindow, create_application, release_window

    app = create_application()
    with tempfile.TemporaryDirectory(prefix="sch-intercom-demo-") as temp:
        print(f"Προσωρινός φάκελος: {temp} (σβήνεται στο κλείσιμο)")
        broadcaster, model, enrollment = build(Path(temp))
        role = build_role(Path(temp), args.role, lambda: model.trust.registry.version)
        try:
            from director.maintenance import MaintenanceModel
            root = Path(temp)
            maintenance = MaintenanceModel(root / "clients-registry.json", root / "director.crt",
                                           root / "director.key",
                                           registry_version=lambda: model.trust.registry.version)
            window = DirectorWindow(broadcaster, DirectorMicrophone(fake_stream, broadcaster),
                                    security=model, maintenance=maintenance, role=role,
                                    layout_settings=root / "director-gui.json")
            if args.width:
                window.resize(args.width, 900)
            window.show()
            window.show_page("security")
            if args.tab == "certificates":
                window.security_page.show_tab("Πιστοποιητικά")
            if args.screenshot:
                for _ in range(5):
                    app.processEvents()
                window.grab().save(str(args.screenshot))
                print(f"Στιγμιότυπο: {args.screenshot}")
            else:
                print("Κλείστε το παράθυρο για τέλος· όλα τα δεδομένα επίδειξης σβήνονται.")
                app.exec()
            window.close()
            release_window(window)
            app.processEvents()
        finally:
            enrollment.stop()
    print("Τέλος: ο προσωρινός φάκελος σβήστηκε.")


if __name__ == "__main__":
    main()
