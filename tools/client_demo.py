"""5C1: επίδειξη παραθύρου/tray του Client με ψεύτικα δεδομένα (χωρίς Manager).

    python -m tools.client_demo [--state connected|pending|revoked|full|live] [--tab history|info]
    python -m tools.client_demo --screenshot out.png [--tray tray.png]

Χωρίς --screenshot ανοίγει το παράθυρο (και το εικονίδιο tray, αν υπάρχει)· τα δεδομένα
είναι προσωρινά και σβήνονται στο κλείσιμο.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client import status as st  # noqa: E402

STATES = ("connected", "pending", "revoked", "full", "live", "update-failed")
FINGERPRINT = "84285ca1" + "3f" * 24 + "7e419d33"


def build(folder: Path, scenario: str, now: float) -> tuple[st.ClientStatus, st.History]:
    history_file = folder / st.HISTORY_FILE
    day = 86400
    today = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    events = [
        (today - 3 * day + 11 * 3600 + 31 * 60, 21),
        (today - day + 9 * 3600, 47),
        (today - day + 13 * 3600 + 20 * 60, 125),
        (today + 8 * 3600 + 5 * 60, 38),
        (today + 10 * 3600 + 42 * 60, 72),
    ]
    if scenario != "pending":
        history_file.write_text("".join(
            json.dumps({"v": 1, "kind": "live", "start": start, "end": start + length}) + "\n"
            for start, length in events), encoding="utf-8")
    history = st.History(history_file).load()
    status = st.ClientStatus(room="C2627-Γ1", name="Board1", version="1.0.6",
                             client_id="8c572527-3f1e-4b0a-9d21-6e0c41a0e2b7")
    if scenario == "pending":
        status.update(client_id="5e0b19c4-8d27-4c11-b6f0-2a9c3e7d4410", manager="192.168.1.10",
                      enrollment={"code": "482913", "expires_at": now + 86400 - 600,
                                  "room": "C2627-Γ1", "name": "Board1"})
        status.set_state(st.PENDING)
        return status, history
    status.update(manager="192.168.1.10", fingerprint=FINGERPRINT, volume=60,
                  mute_others=True, device="Ηχεία (Realtek High Definition Audio)")
    status.set_state(st.CONNECTED)
    status.set_update("current")
    if scenario == "revoked":
        status.set_state(st.REVOKED)
    elif scenario == "full":
        status.set_state(st.FULL)
    elif scenario == "update-failed":
        status.set_update("failed", "1.0.7", "Η νέα έκδοση δεν συνδέθηκε (επιστροφή στην 1.0.6)")
    elif scenario == "live":
        history.begin()
        status.update(live_since=time.time())
    return status, history


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", choices=STATES, default="connected")
    parser.add_argument("--tab", choices=("history", "info"), default=None)
    parser.add_argument("--scale", type=int, choices=(80, 90, 100, 110, 125), default=None)
    parser.add_argument("--screenshot", type=Path, default=None,
                        help="μόνο στιγμιότυπο παραθύρου (offscreen) και έξοδος")
    parser.add_argument("--tray", type=Path, default=None,
                        help="και στιγμιότυπο των εικονιδίων tray (πράσινο/κίτρινο/κόκκινο)")
    args = parser.parse_args()
    if args.screenshot or args.tray:
        from shared.ui.offscreen import use_offscreen
        use_offscreen()
    from client.qt_client import ClientTray, setup_application, tray_pixmap
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QPainter, QPixmap
    from shared.ui import theme

    app = setup_application(ui_scale=args.scale)
    with tempfile.TemporaryDirectory(prefix="sch-intercom-client-demo-") as temp:
        status, history = build(Path(temp), args.state, time.time())
        tray = ClientTray(status, history)
        window = tray.window
        tab = args.tab or ("info" if args.state in ("pending", "update-failed") else "history")
        window.show_tab("Πληροφορίες" if tab == "info" else "Ιστορικό")
        if args.tray:
            sizes = (16, 24, 32, 64)
            sheet = QPixmap(sum(sizes) * 3 + 40 * 3, 80)
            sheet.fill(QColor(theme.WINDOW_BG))
            painter = QPainter(sheet)
            x = 10
            for tone in ("ok", "wait", "bad"):
                for size in sizes:
                    painter.drawPixmap(x, 70 - size, tray_pixmap(size, tone))
                    x += size + 4
                x += 28
            painter.end()
            sheet.save(str(args.tray))
            print(f"Εικονίδια tray: {args.tray}")
        if args.screenshot:
            window.show()
            for _ in range(5):
                app.processEvents()
            window.grab().save(str(args.screenshot))
            print(f"Στιγμιότυπο: {args.screenshot}")
        elif not args.tray:
            window.show()
            window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
            window.hide_on_close = False
            window.destroyed.connect(app.quit)
            print("Κλείστε το παράθυρο για τέλος.")
            app.lastWindowClosed.connect(app.quit)
            app.setQuitOnLastWindowClosed(True)
            app.exec()
        tray.tray.hide()
    return 0


if __name__ == "__main__":
    sys.exit(main())
