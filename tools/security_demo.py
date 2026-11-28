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
4.2D3: --role reported (Clients ανέφεραν νεότερο μητρώο) | divergent (ίδια έκδοση, άλλο hash).
Ο «άλλος Manager» είναι ψεύτικος (κανένα δίκτυο)· αλλαγή ρόλου και Έναρξη/Διακοπή
δουλεύουν πάνω στο προσωρινό manager-state.json.

4.3C2: --notices full | duplicate | both (ψεύτικα: «γεμάτος» Manager, ίδια ταυτότητα
στον C2627-Γ1 · Board1) και --page home | security.

5B3: --page settings --tab updates [--updates normal | off | missing | empty]
(Ρυθμίσεις → «Ενημερώσεις» με πακέτα 1.0.6/1.0.5 υπογεγραμμένα με κλειδί επίδειξης και
ψεύτικη κατάσταση ανά υπολογιστή). Με --role backup δείχνει την κλειδωμένη μορφή.

5C3: --requests sponsored (κάρτες «Κλώνος του C2627-Γ1 · PC» και «Νέα ταυτότητα» του
C2627-Γ2 · Board1, χωρίς κωδικό· ο κλώνος «PC05» του Εργαστηρίου · PC01 εγκεκριμένος, ώστε
η «Ανάκληση…» του PC01 να προτείνει και τον κλώνο). Με --notices duplicate το ⋮ του
C2627-Γ1 · Board1 έχει «Νέα ταυτότητα στο <IP>…» για κάθε IP.

Κλίμακα περιβάλλοντος: --scale 80 | 90 | 100 | 110 | 125 (προεπιλογή όπως η εφαρμογή).
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile
import time
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
ROLE_SCENARIOS = ("primary", "restored", "conflict", "newer", "backup", "stopped",
                  "reported", "divergent")
DEMO_HOST = "ΓΡΑΜΜΑΤΕΙΑ-PC"
OTHER_HOST, OTHER_IP = "ΓΡΑΜΜΑΤΕΙΑ-2", "192.168.1.35"


class DemoBroadcaster:
    """Σταθερή κατάσταση online/offline· οι στόχοι και οι ενημερώσεις αγνοούνται."""

    max_clients = 70

    def __init__(self):
        self.status: dict[str, tuple[str, bool, float]] = {}
        self.names: dict[str, str] = {}
        self.rooms: dict[str, str] = {}
        self.waiting: list[tuple[str, str, str]] = []
        self.duplicates: list[tuple[str, str, tuple[str, ...]]] = []

    def full_waiting(self):
        return list(self.waiting)

    def duplicate_identities(self):
        return list(self.duplicates)

    def client_names(self):
        return dict(self.names)

    def set_notices(self, scenario: str) -> None:
        """4.3C2: ψεύτικοι «σε αναμονή» και ίδια ταυτότητα στον C2627-Γ1 · Board1."""
        if scenario in ("full", "both"):
            self.waiting = [(str(uuid.uuid4()), "C2627-Γ3", "Board1"),
                            (str(uuid.uuid4()), "C2627-Δ2", "PC")]
        if scenario in ("duplicate", "both"):
            [client_id] = [cid for cid, name in self.names.items()
                           if name == "Board1" and self.rooms[cid] == "C2627-Γ1"]
            self.status[client_id] = ("C2627-Γ1", True, 0.1)
            self.duplicates = [(client_id, "C2627-Γ1", ("192.168.1.21", "192.168.1.47"))]

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

    def send_new_identity(self, *_args):
        return False  # κανένας πραγματικός Client: «στέλνεται στην επόμενη σύνδεση»


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


def inject_reports(trust, scenario: str) -> None:
    """4.2D3: αναφορές «έχω δει vN» από δύο εγκεκριμένους Clients της επίδειξης."""
    version, registry_hash = trust.registry.welcome_info()
    approved = [e for e in trust.registry.snapshot()["clients"] if e["status"] == "approved"]
    seen = ({"rv": version, "hash": "f" * 64} if scenario == "divergent"
            else {"rv": version + 3, "hash": "e" * 64})
    for entry in approved[:2]:
        trust.reports.record(entry["client_id"], seen, room=entry["room"],
                             name=entry.get("name") or "", local_rv=version,
                             local_hash=registry_hash)


def build_role(root: Path, scenario: str, registry_version, trust=None):
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
    extra = {}
    if trust is not None:
        if scenario in ("reported", "divergent"):
            inject_reports(trust, scenario)
        extra = {"reported": trust.reported, "accept_current": trust.accept_current}
    return RoleModel(state, controller, watcher, registry_version=registry_version,
                     host=DEMO_HOST, **extra)


def add_sponsored(model, broadcaster) -> None:
    """5C3: αιτήματα με σύσταση (κλώνος, νέα ταυτότητα) και ένας εγκεκριμένος κλώνος."""
    from shared.enrollment import SPONSOR_CLONE, SPONSOR_RESET

    def origin(room, name):
        [client_id] = [cid for cid, n in broadcaster.names.items()
                       if n == name and broadcaster.rooms[cid] == room]
        return client_id

    lab = origin("Εργαστήριο Πληροφορικής", "PC01")
    clone_id, _code = model.demo_submit("Εργαστήριο Πληροφορικής", "PC01",
                                        (SPONSOR_CLONE, lab, ""))
    [request] = [r for r in model.requests() if r.sponsored]
    if not model.approve_sponsored(request.request_id, "Εργαστήριο Πληροφορικής", "PC05").ok:
        raise SystemExit("Η έγκριση του κλώνου απέτυχε")
    broadcaster.names[clone_id] = "PC05"
    broadcaster.rooms[clone_id] = "Εργαστήριο Πληροφορικής"
    broadcaster.status[clone_id] = ("Εργαστήριο Πληροφορικής", True, 0.1)
    model.demo_submit("C2627-Γ1", "PC", (SPONSOR_CLONE, origin("C2627-Γ1", "PC"), ""))
    board = origin("C2627-Γ2", "Board1")
    model.trust.new_identity(board, "192.168.1.47", "C2627-Γ2", "Board1", broadcaster)
    token = model.trust._identity_tokens[board][0]
    model.demo_submit("C2627-Γ2", "Board1", (SPONSOR_RESET, board, token))


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
    from shared.enrollment import EnrollmentServer, enroll_client, make_request, make_sponsor

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

    def submit(room, name, sponsor=None):
        client_id = str(uuid.uuid4())
        device_cert, device_key, password = create_device_identity(
            root / "devices" / client_id, client_id)
        request = make_request(client_id, room, name, device_cert.read_bytes(),
                               device_key.read_bytes(), password)
        if sponsor is not None:
            kind, origin, token = sponsor
            old_key, old_password = model.demo_keys[origin]
            request = make_sponsor(request, kind, origin, token, old_key, old_password)
        model.demo_keys[client_id] = (device_key.read_bytes(), password)
        queued = enroll_client("127.0.0.1", enrollment.port, cert,
                               "school-intercom-director", request)
        return client_id, queued["code"]

    model.demo_keys = {}
    model.demo_submit = submit

    index = 0
    for room, names in ROOMS.items():
        for name in names:
            client_id, code = submit(room, name)
            [request_id] = [r.request_id for r in model.requests()
                            if (r.room, r.name) == (room, name)]
            if not model.approve(request_id, code).ok:
                raise SystemExit(f"Η έγκριση {room} · {name} απέτυχε")
            broadcaster.names[client_id] = name
            broadcaster.rooms[client_id] = room
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


# 5B3: κατάσταση ενημέρωσης με τη σειρά των υπολογιστών (όσοι είναι online).
UPDATE_STATES = [
    {"app": "1.0.6", "state": "current"},
    {"app": "1.0.5", "state": "downloading", "available": "1.0.6", "progress": 45},
    {"app": "1.0.6", "state": "installed", "available": "1.0.6"},
    {"app": "1.0.5", "state": "waiting", "available": "1.0.6", "detail": "ανακοίνωση σε εξέλιξη"},
    {"app": "1.0.5", "state": "failed", "available": "1.0.6",
     "detail": "Η 1.0.6 δεν συνδέθηκε σε 90 s · ξανά η 1.0.5"},
    {"app": "1.0.5", "state": "ready", "available": "1.0.6"},
    {"app": "1.0.6", "state": "current"},
    {"app": "1.0.5", "state": "available", "available": "1.0.6",
     "detail": "αναμονή σειράς λήψης"},
    {"app": "1.0.6", "state": "current"},
    {"app": "1.0.4", "state": "available", "available": "1.0.6"},
]


def _demo_package(root: Path, key, version: str) -> Path:
    from shared import package as pkg
    tree = root / "release" / version
    (tree / "python").mkdir(parents=True)
    (tree / "python" / "python.exe").write_bytes(b"MZ" + os.urandom(1_500_000))
    (tree / "app" / "client").mkdir(parents=True)
    (tree / "app" / "client" / "main.py").write_text(f"VERSION = {version!r}\n",
                                                     encoding="utf-8")
    (tree / "VERSION").write_text(version + "\n", encoding="ascii")
    out = root / f"sch-intercom-client-{version}.sicu"
    pkg.write_package(tree, out, key, version)
    return out


def build_updates(root: Path, model, broadcaster, scenario: str):
    """Ρυθμίσεις → «Ενημερώσεις»: πακέτα επίδειξης και κατάσταση ανά υπολογιστή."""
    from director.manager_state import load as load_state, path_for
    from director.updates import STATUS_FILE, UpdateStatus, UpdateStore, updates_dir
    from director.updates_view import UpdatesModel
    from shared import package as pkg
    key = pkg.generate_key()
    folder = updates_dir(root)
    store = UpdateStore(folder, trusted_keys=[pkg.public_hex(key)])
    status = UpdateStatus(folder / STATUS_FILE)
    if scenario != "empty":
        for version in ("1.0.5", "1.0.6"):
            store.import_package(_demo_package(root / "demo-packages", key, version))
        store.set_auto(scenario != "off")
        if scenario == "missing":
            store.package_path("1.0.6").unlink()
    index = 0
    for client_id, (_room, online, age) in broadcaster.status.items():
        if online:
            entry = dict(UPDATE_STATES[index % len(UPDATE_STATES)])
            index += 1
        else:
            entry = {"app": "1.0.5" if index % 2 else "1.0.6", "state": "current"}
        if scenario == "empty":
            entry = {"app": "1.0.0", "state": "current"}
        elif scenario == "off" and entry["state"] not in ("current", "installed"):
            entry = {"app": entry["app"], "state": "available", "available": "1.0.6"}
            if index == 2 and online:
                entry.update(state="downloading", progress=45)
        if not online:
            entry["seen_at"] = int(time.time() - age)
        status.update(client_id, **entry)
    state_file = path_for(model.trust.registry.path)
    updates = UpdatesModel(store, status, rooms=model.rooms,
                           primary=lambda: load_state(state_file).role == "primary")
    model.update_versions = updates.versions  # ✓ στις αίθουσες (Υπολογιστές)
    return updates


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--width", type=int, default=None, help="πλάτος παραθύρου")
    parser.add_argument("--role", choices=ROLE_SCENARIOS, default="primary",
                        help="σενάριο Κύριου/Εφεδρικού")
    parser.add_argument("--tab", choices=("computers", "certificates", "updates",
                                          "maintenance", "appearance"), default=None)
    parser.add_argument("--updates", choices=("normal", "off", "missing", "empty"),
                        default="normal", help="5B3: σενάριο Ρυθμίσεις → «Ενημερώσεις»")
    parser.add_argument("--notices", choices=("none", "full", "duplicate", "both"),
                        default="none", help="4.3C2: ειδοποιήσεις «γεμάτος» / ίδια ταυτότητα")
    parser.add_argument("--requests", choices=("normal", "sponsored"), default="normal",
                        help="5C3: και αιτήματα κλώνου / νέας ταυτότητας χωρίς κωδικό")
    parser.add_argument("--page", choices=("home", "security", "settings"), default="security")
    parser.add_argument("--scale", type=int, choices=(70, 80, 90, 100, 110, 125, 135), default=None,
                        help="κλίμακα περιβάλλοντος %% (προεπιλογή: όπως η εφαρμογή, 90 στα Windows)")
    parser.add_argument("--screenshot", type=Path, default=None,
                        help="μόνο στιγμιότυπο (offscreen) και έξοδος")
    args = parser.parse_args()
    if args.screenshot:
        from shared.ui.offscreen import use_offscreen
        use_offscreen()  # offscreen· στα Windows και γραμματοσειρές του συστήματος

    from director.gui import DirectorMicrophone
    from director.qt_gui import (DirectorWindow, create_application, release_window,
                                 save_layout_settings)
    from shared.ui import theme

    scale = args.scale or theme.default_ui_scale()
    app = create_application(ui_scale=scale)
    with tempfile.TemporaryDirectory(prefix="sch-intercom-demo-") as temp:
        print(f"Προσωρινός φάκελος: {temp} (σβήνεται στο κλείσιμο)")
        save_layout_settings(Path(temp) / "director-gui.json", {"ui_scale": scale})
        broadcaster, model, enrollment = build(Path(temp))
        broadcaster.set_notices(args.notices)
        role = build_role(Path(temp), args.role, lambda: model.trust.registry.version,
                          trust=model.trust)
        if args.requests == "sponsored":
            add_sponsored(model, broadcaster)
        try:
            from director.maintenance import MaintenanceModel
            root = Path(temp)
            maintenance = MaintenanceModel(root / "clients-registry.json", root / "director.crt",
                                           root / "director.key",
                                           registry_version=lambda: model.trust.registry.version)
            updates = build_updates(root, model, broadcaster, args.updates)
            window = DirectorWindow(broadcaster, DirectorMicrophone(fake_stream, broadcaster),
                                    security=model, maintenance=maintenance, role=role,
                                    layout_settings=root / "director-gui.json",
                                    updates=updates)
            if args.width:
                window.resize(args.width, 900)
            window.show()
            window.show_page(args.page)
            if args.page == "security" and args.tab == "certificates":
                window.security_page.show_tab("Πιστοποιητικά")
            if args.page == "settings":
                window.settings_page.show_tab({"maintenance": "Συντήρηση",
                                               "appearance": "Εμφάνιση"}.get(
                                                   args.tab, "Ενημερώσεις"))
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
