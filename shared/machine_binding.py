"""4.3C1: ανίχνευση κλωνοποιημένου υπολογιστή τάξης.

Όταν δημιουργείται η ταυτότητα του Client, σημειώνεται και το «αποτύπωμα» του
υπολογιστή: MachineGuid των Windows (Linux: /etc/machine-id) και οι διευθύνσεις MAC
των φυσικών καρτών δικτύου. Αποθηκεύονται μόνο hashes, όχι οι ίδιες τιμές.

Κλώνος = άλλαξε το MachineGuid (κλώνος με sysprep) ή δεν υπάρχει πια καμία από τις
αποθηκευμένες MAC (κλώνος δίσκου χωρίς sysprep, ίδιο MachineGuid). Αλλαγή κάρτας
δικτύου στον ίδιο υπολογιστή μοιάζει επίσης με κλώνο και οδηγεί απλώς σε νέα εγγραφή.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

BINDING_FILE = "machine-binding.json"
FORMAT = 1
_MAC = re.compile(r"([0-9A-Fa-f]{2})[:-]([0-9A-Fa-f]{2})[:-]([0-9A-Fa-f]{2})[:-]"
                  r"([0-9A-Fa-f]{2})[:-]([0-9A-Fa-f]{2})[:-]([0-9A-Fa-f]{2})")
#: Εικονικές κάρτες που αντιγράφονται ίδιες μαζί με το image (VPN/TAP, VM, Hyper-V)
#: δεν ξεχωρίζουν υπολογιστές· δεν μετρούν.
_VIRTUAL_PREFIXES = ("00:ff:", "00:15:5d:", "00:50:56:", "00:0c:29:", "00:05:69:",
                     "00:1c:14:", "08:00:27:", "0a:00:27:", "00:03:ff:", "02:00:4c:")


@dataclass(frozen=True)
class Machine:
    machine_id: str | None     # hash του MachineGuid / machine-id, ή None αν δεν διαβάζεται
    macs: frozenset[str]       # hashes των MAC φυσικών καρτών


def _hash(value: str) -> str:
    return hashlib.sha256(("sch-intercom:" + value).encode("utf-8")).hexdigest()[:32]


def normalize_mac(text: str) -> str | None:
    match = _MAC.fullmatch(text.strip())
    if match is None:
        return None
    mac = ":".join(part.lower() for part in match.groups())
    first = int(mac[:2], 16)
    if mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff") or first & 0x01:
        return None   # κενή ή multicast
    if first & 0x02:
        return None   # «τοπικά διαχειριζόμενη»: τυχαία/εικονική, όχι της κάρτας
    if mac.startswith(_VIRTUAL_PREFIXES):
        return None
    return mac


def _windows_machine_id() -> str | None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value).strip() or None
    except OSError:
        return None


def _windows_macs() -> list[str]:
    try:
        output = subprocess.run(["getmac", "/fo", "csv", "/nh"], capture_output=True,
                                timeout=10, creationflags=getattr(subprocess,
                                                                  "CREATE_NO_WINDOW", 0)
                                ).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return []
    return [match.group(0) for match in _MAC.finditer(output)]


def _linux_machine_id() -> str | None:
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(path).read_text(encoding="ascii").strip()
            if value:
                return value
        except OSError:
            continue
    return None


def _linux_macs() -> list[str]:
    result = []
    base = Path("/sys/class/net")
    try:
        interfaces = list(base.iterdir())
    except OSError:
        return result
    for interface in interfaces:
        if interface.name == "lo" or not (interface / "device").exists():
            continue   # μόνο φυσικές συσκευές (όχι bridge/veth/docker)
        try:
            result.append((interface / "address").read_text(encoding="ascii").strip())
        except OSError:
            continue
    return result


def current_machine() -> Machine:
    if sys.platform == "win32":
        machine_id, macs = _windows_machine_id(), _windows_macs()
    else:
        machine_id, macs = _linux_machine_id(), _linux_macs()
    normalized = {mac for mac in (normalize_mac(item) for item in macs) if mac}
    return Machine(_hash(machine_id) if machine_id else None,
                   frozenset(_hash(mac) for mac in normalized))


def binding_path(credentials_dir: Path) -> Path:
    return Path(credentials_dir) / BINDING_FILE


def load_binding(path: Path) -> Machine | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("format") != FORMAT:
            return None
        machine_id = data.get("machine_id")
        macs = data.get("macs", [])
        if machine_id is not None and not isinstance(machine_id, str):
            return None
        if not isinstance(macs, list) or not all(isinstance(item, str) for item in macs):
            return None
        return Machine(machine_id, frozenset(macs))
    except (OSError, ValueError, AttributeError):
        return None


def save_binding(path: Path, machine: Machine) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps({"format": FORMAT, "machine_id": machine.machine_id,
                                         "macs": sorted(machine.macs)}, indent=2) + "\n",
                             encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def clone_reason(stored: Machine, current: Machine) -> str | None:
    """Γιατί ο υπολογιστής μοιάζει κλώνος, ή None αν είναι ο ίδιος."""
    if (stored.machine_id is not None and current.machine_id is not None
            and stored.machine_id != current.machine_id):
        return "άλλο MachineGuid"
    if stored.macs and current.macs and not (stored.macs & current.macs):
        return "καμία από τις κάρτες δικτύου του αρχικού υπολογιστή"
    return None


def merged(stored: Machine, current: Machine) -> Machine:
    """Ίδιος υπολογιστής: κρατά και τις νέες κάρτες (π.χ. USB Wi-Fi που προστέθηκε)."""
    return Machine(stored.machine_id or current.machine_id, stored.macs | current.macs)
