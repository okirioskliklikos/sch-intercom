"""Στιγμιότυπα της κεντρικής οθόνης Director χωρίς οθόνη, δίκτυο ή μικρόφωνο.

Χρήση (Linux/Windows, χρειάζεται PySide6):
    python -m tools.render_director_gui --out-dir screenshots

Γράφει director-main-idle.png και director-main-live.png με ψεύτικους
Clients (τα 17 ενδεικτικά ονόματα του ui-design.md) και ψεύτικο μικρόφωνο.
Χρησιμεύει για σύγκριση με το docs/mockups/ και για τα PR. Δεν είναι δοκιμή
πραγματικού ήχου ή εμφάνισης στα Windows.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

MOCKUP_ROOMS = ["Α1", "Α2", "Β1", "Β2", "Γ1", "Γ2", "Δ1", "Δ2", "Ε1", "Ε2", "Στ1", "Στ2",
                "Τμ. Ένταξης 1", "Τμ. Ένταξης 2", "Τμήμα Υποδοχής", "Εργ. Υπολογιστών",
                "Κεντρική Αίθουσα"]


class FakeBroadcaster:
    def __init__(self, rooms):
        self.status = [(f"id-{i}", room, True, 0.1) for i, room in enumerate(rooms)]

    def status_snapshot(self):
        return list(self.status)

    def set_targets(self, *, rooms=None, client_ids=None):
        pass

    def publish(self, *_args):
        pass


def fake_stream(level: float):
    def stream(_gate, *, stop, ready_callback, level_callback, **_kwargs):
        ready_callback()
        while not stop.wait(0.02):
            level_callback(level)
    return stream


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", type=Path, default=Path("screenshots"))
    parser.add_argument("--scale", type=float, default=2.0, help="device pixel ratio")
    parser.add_argument("--base-pt", type=float, default=None,
                        help="βάση γραμματοσειράς σε pt (προεπιλογή: του συστήματος, ελάχιστο 12)")
    args = parser.parse_args()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ["QT_SCALE_FACTOR"] = str(args.scale)

    from PySide6.QtCore import QEventLoop, QTimer
    from director import gui
    from director.qt_gui import (DirectorWindow, create_application, current_metrics,
                                 release_window)

    app = create_application(base_pt=args.base_pt)
    broadcaster = FakeBroadcaster(MOCKUP_ROOMS)
    microphone = gui.DirectorMicrophone(fake_stream(0.12), broadcaster)
    window = DirectorWindow(broadcaster, microphone)
    # Η offscreen «οθόνη» είναι 800×800· χωρίς περιορισμό, όπως σε κανονική οθόνη.
    m = current_metrics()
    window.setMinimumSize(m.px(860), m.px(600))
    window.resize(m.px(960), m.px(680))
    window.show()

    def settle(ms: int = 300) -> None:
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    settle()
    window.grab().save(str(args.out_dir / "director-main-idle.png"))
    window.toggle_announcement()
    settle()
    window.grab().save(str(args.out_dir / "director-main-live.png"))
    window.close()
    release_window(window)
    app.processEvents()
    print(f"Αποθηκεύτηκαν στο {args.out_dir}")


if __name__ == "__main__":
    main()
