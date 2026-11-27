"""Γραμμή εντολών του εγκατεστημένου Client (Φάση 5A, docs/phase5.md §5.5).

    %LOCALAPPDATA%\\sch-intercom\\sch-intercom-client.cmd --status

Ο Client δεν έχει «Έξοδο» ή ρυθμίσεις στο tray (απόφαση 27/11/2026)· όλα γίνονται
από εδώ. Κωδικοί εξόδου: 0 = εντάξει, 1 = σφάλμα, 3 = ο Client δεν εκτελείται.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from client.install_paths import (CLI_NAME, DEFAULT_RUN_NAME, MANAGER_CERT, Layout,
                                  atomic_write_json, load_settings, save_settings)
from client.single_instance import is_locked
from client.supervisor import send_command
from shared.version import parse_version

EXIT_NOT_RUNNING = 3
STOP_WAIT_SECONDS = 15
START_WAIT_SECONDS = 15
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _say(text: str) -> None:
    print(text, flush=True)


def running(layout: Layout) -> bool:
    return is_locked(layout.lock_file)


def status(layout: Layout) -> dict | None:
    try:
        return send_command(layout, "status")
    except (OSError, TimeoutError):
        return None


def wait_until(predicate, timeout: float, step: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def start(layout: Layout, *, wait: bool = True) -> bool:
    """Ξεκινά τον supervisor σε ξεχωριστή διεργασία, χωρίς παράθυρο."""
    if running(layout):
        return True
    command = layout.module_command("client.supervisor", "--home", str(layout.home),
                                    windowed=True)
    if layout.data != Layout(layout.home).data:
        command += ["--data-dir", str(layout.data)]
    env = layout.child_env(layout.current_version())
    options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, env=env, cwd=str(layout.home), close_fds=True)
    if os.name == "nt":
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(command, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **options)
        except OSError:
            subprocess.Popen(command, creationflags=flags, **options)
    else:
        subprocess.Popen(command, start_new_session=True, **options)
    return not wait or wait_until(lambda: status(layout) is not None, START_WAIT_SECONDS)


def stop(layout: Layout) -> bool:
    if not running(layout):
        return True
    try:
        send_command(layout, "stop")
    except (OSError, TimeoutError):
        pass
    return wait_until(lambda: not running(layout), STOP_WAIT_SECONDS)


def restart(layout: Layout) -> bool:
    """Επανεκκίνηση του Client· αν δεν εκτελείται ο supervisor, τον ξεκινά."""
    if not running(layout):
        return start(layout)
    before = (status(layout) or {}).get("client_pid")
    send_command(layout, "restart")
    return wait_until(lambda: (status(layout) or {}).get("client_pid") not in (None, before),
                      START_WAIT_SECONDS)


def format_status(info: dict | None) -> str:
    if info is None:
        return "Ο Client δεν εκτελείται."
    client = (f"PID {info['client_pid']}, σε λειτουργία {info['client_uptime']} s"
              if info.get("client_pid") else "δεν τρέχει αυτή τη στιγμή")
    return (f"Εκτελείται | έκδοση {info.get('version') or '-'} | "
            f"supervisor PID {info.get('supervisor_pid')} | Client {client} | "
            f"επανεκκινήσεις {info.get('restarts', 0)}")


def _host_value(text: str) -> str | None:
    if text == "auto":
        return None
    try:
        ipaddress.ip_address(text)
    except ValueError:
        if not text or len(text) > 253 or any(c.isspace() for c in text):
            raise argparse.ArgumentTypeError(f"Μη έγκυρη διεύθυνση: {text}")
    return text


def _device_value(text: str) -> int | None:
    if text == "auto":
        return None
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("Δώστε αριθμό συσκευής ή auto") from None
    if value < 0:
        raise argparse.ArgumentTypeError("Δώστε αριθμό συσκευής ή auto")
    return value


CLI_TEMPLATE = (
    "@echo off\r\n"
    "rem sch-intercom Client: command line (sch-intercom-client --help)\r\n"
    "set PYTHONUTF8=1\r\n"
    "set PYTHONIOENCODING=utf-8\r\n"
    "chcp 65001 >nul\r\n"
    "\"%~dp0versions\\{version}\\python\\python.exe\" -m client.ctl --home \"%~dp0.\" %*\r\n"
)


def finish_install(layout: Layout, version: str, *, manager_cert: Path | None,
                   director_host: str | None, run_name: str) -> None:
    """Τελευταίο βήμα του εγκαταστάτη: current.json, install.json, .cmd, πιστοποιητικό."""
    parse_version(version)
    data = layout.data
    data.mkdir(parents=True, exist_ok=True)
    atomic_write_json(layout.install_file, {"data_dir": str(data), "run_name": run_name,
                                            "installed_version": version})
    layout.set_installed(version)
    layout.cli_file.write_bytes(CLI_TEMPLATE.format(version=version).encode("ascii"))
    if manager_cert is not None:
        text = Path(manager_cert).read_text(encoding="utf-8")
        if "-----BEGIN CERTIFICATE-----" not in text:
            raise ValueError(f"Το {manager_cert} δεν είναι πιστοποιητικό PEM")
        shutil.copyfile(manager_cert, data / MANAGER_CERT)
    elif not (data / MANAGER_CERT).exists():
        raise ValueError("Λείπει το πιστοποιητικό του Manager (manager-ca.pem)")
    if director_host is not None:
        save_settings(data, director_host=director_host or None)
    for folder in (layout.logs, layout.home / "staging"):
        folder.mkdir(parents=True, exist_ok=True)


def enroll(layout: Layout, room: str | None, name: str | None, *, pause: bool) -> int:
    args = ["--enroll"]
    if room:
        args += ["--room", room]
    if name:
        args += ["--computer-name", name]
    command = layout.module_command("client.main", *args)
    code = subprocess.call(command, env=layout.child_env(layout.current_version()))
    if pause:
        try:
            input("\nΠατήστε Enter για να κλείσει το παράθυρο...")
        except EOFError:
            pass
    return code


def purge(layout: Layout) -> None:
    """«Πλήρης αφαίρεση»: σβήνει ταυτότητα, κλειδιά και ρυθμίσεις του Client."""
    stop(layout)
    data = layout.data.resolve()
    if data in (Path.home().resolve(), Path(data.anchor)) or len(data.parts) < 3:
        raise ValueError(f"Δεν διαγράφεται ο φάκελος {data}")
    if data.exists():
        shutil.rmtree(data)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sch-intercom-client",
        description="School Intercom — Classroom Client: γραμμή εντολών")
    parser.add_argument("--home", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, default=None, help=argparse.SUPPRESS)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--status", action="store_true", help="Κατάσταση Client")
    actions.add_argument("--start", action="store_true", help="Εκκίνηση")
    actions.add_argument("--stop", action="store_true",
                         help="Διακοπή μέχρι την επόμενη σύνδεση στα Windows ή --start")
    actions.add_argument("--restart", action="store_true", help="Επανεκκίνηση")
    actions.add_argument("--set-director-host", type=_host_value, metavar="IP|auto",
                         default=argparse.SUPPRESS,
                         help="Διεύθυνση Manager (auto = αυτόματος εντοπισμός)")
    actions.add_argument("--set-device", type=_device_value, metavar="N|auto",
                         default=argparse.SUPPRESS, help="Συσκευή εξόδου ήχου (auto = προεπιλογή)")
    actions.add_argument("--show-settings", action="store_true", help="Τοπικές ρυθμίσεις")
    actions.add_argument("--enroll", action="store_true",
                         help="Νέα αίτηση εγγραφής (δείχνει τον κωδικό για τον Manager)")
    actions.add_argument("--purge", action="store_true",
                         help="Πλήρης αφαίρεση ταυτότητας και ρυθμίσεων (με --yes)")
    actions.add_argument("--install-finish", metavar="ΕΚΔΟΣΗ", default=None,
                         help=argparse.SUPPRESS)
    parser.add_argument("--room", default=None, help="Με --enroll: αίθουσα")
    parser.add_argument("--computer-name", default=None, help="Με --enroll: όνομα υπολογιστή")
    parser.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--yes", action="store_true", help="Επιβεβαίωση για --purge")
    parser.add_argument("--no-restart", action="store_true",
                        help="Με --set-*: χωρίς επανεκκίνηση του Client")
    # Μόνο για τον εγκαταστάτη.
    parser.add_argument("--manager-cert", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--director-host", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--run-name", default=DEFAULT_RUN_NAME, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    layout = Layout(args.home, args.data_dir)
    if args.status:
        info = status(layout)
        _say(format_status(info))
        return 0 if info is not None else EXIT_NOT_RUNNING
    if args.start:
        ok = start(layout)
        _say(format_status(status(layout)) if ok else "Ο Client δεν ξεκίνησε· δείτε logs\\supervisor.log")
        return 0 if ok else 1
    if args.stop:
        ok = stop(layout)
        _say("Ο Client σταμάτησε." if ok else "Ο Client δεν σταμάτησε εγκαίρως.")
        return 0 if ok else 1
    if args.restart:
        ok = restart(layout)
        _say(format_status(status(layout)) if ok else "Η επανεκκίνηση απέτυχε· δείτε logs")
        return 0 if ok else 1
    if args.show_settings:
        settings = load_settings(layout.data)
        _say(f"Manager: {settings['director_host'] or 'αυτόματα'} | "
             f"Συσκευή ήχου: {'προεπιλογή' if settings['device'] is None else settings['device']} | "
             f"Δεδομένα: {layout.data}")
        return 0
    if hasattr(args, "set_director_host") or hasattr(args, "set_device"):
        if hasattr(args, "set_director_host"):
            save_settings(layout.data, director_host=args.set_director_host)
            _say(f"Manager: {args.set_director_host or 'αυτόματα'}")
        else:
            save_settings(layout.data, device=args.set_device)
            _say(f"Συσκευή ήχου: {'προεπιλογή' if args.set_device is None else args.set_device}")
        if not args.no_restart and running(layout):
            restart(layout)
            _say("Ο Client επανεκκινήθηκε με τη νέα ρύθμιση.")
        return 0
    if args.enroll:
        return enroll(layout, args.room, args.computer_name, pause=args.pause)
    if args.purge:
        if not args.yes:
            parser.error("Η πλήρης αφαίρεση σβήνει την ταυτότητα (χρειάζεται νέα έγκριση)· "
                         "προσθέστε --yes")
        try:
            purge(layout)
        except (OSError, ValueError) as exc:
            _say(f"Σφάλμα: {exc}")
            return 1
        _say(f"Διαγράφηκε: {layout.data}")
        return 0
    if args.install_finish:
        try:
            finish_install(layout, args.install_finish, manager_cert=args.manager_cert,
                           director_host=args.director_host, run_name=args.run_name)
        except (OSError, ValueError) as exc:
            _say(f"Σφάλμα εγκατάστασης: {exc}")
            return 1
        _say(f"Εγκαταστάθηκε η έκδοση {args.install_finish} στο {layout.home}")
        return 0
    parser.error("Καμία εντολή")
    return 1


if __name__ == "__main__":
    sys.exit(main())
