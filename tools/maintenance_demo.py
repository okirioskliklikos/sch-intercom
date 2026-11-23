"""Επίδειξη: Ασφάλεια → «Πιστοποιητικά» και Ρυθμίσεις → «Συντήρηση», χωρίς πραγματικά δεδομένα.

Χρήση (Linux ή Windows, χρειάζεται PySide6):
    python -m tools.maintenance_demo                 # ανοίγει στη «Συντήρηση»
    python -m tools.maintenance_demo --page certificates
    python -m tools.maintenance_demo --screenshots DIR   # μόνο στιγμιότυπα και έξοδος

Όλα γίνονται σε προσωρινό φάκελο (ταυτότητα Manager, μητρώο με 26 υπολογιστές, 2 MP3,
ένα backup με κωδικό «demo-κωδικός-2026» στον υποφάκελο usb/), που σβήνεται στο κλείσιμο.
Ο φάκελος ~/.school-intercom δεν διαβάζεται ούτε γράφεται.

Backup και «Άνοιγμα» στον διάλογο επαναφοράς δουλεύουν κανονικά. Η «Επαναφορά και
επανεκκίνηση» εδώ γίνεται κανονικά στον προσωρινό φάκελο, αλλά αντί για επανεκκίνηση
το παράθυρο απλώς κλείνει.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile

PASSWORD = "demo-κωδικός-2026"


def build(root: Path, log=print):
    from director.maintenance import MaintenanceModel
    from tools import security_demo

    broadcaster, model, enrollment = security_demo.build(root, log=log)
    media = root / "media"
    media.mkdir()
    (media / "κουδούνι.mp3").write_bytes(b"ID3" + os.urandom(48_000))
    (media / "εθνικός-ύμνος.mp3").write_bytes(b"ID3" + os.urandom(96_000))
    maintenance = MaintenanceModel(root / "clients-registry.json", root / "director.crt",
                                   root / "director.key",
                                   registry_version=lambda: model.trust.registry.version)
    usb = root / "usb"
    usb.mkdir()
    path, _ = maintenance.create_backup(usb / "sch-intercom-backup-demo.sicb", PASSWORD,
                                        PASSWORD, copy_media=True)
    # Τρεις αλλαγές μητρώου μετά το backup: «3 αλλαγές μητρώου» και, στην επαναφορά,
    # «παλαιότερο μητρώο».
    rooms = model.rooms()
    for computer in rooms[0].computers[:3]:
        model.rename(computer.client_id, computer.name + "-νέο")
    (media / "εθνικός-ύμνος.mp3").unlink()
    log(f"Backup επίδειξης: {path} (κωδικός: {PASSWORD})")
    return broadcaster, model, enrollment, maintenance, path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--page", choices=("maintenance", "certificates"), default="maintenance")
    parser.add_argument("--width", type=int, default=None, help="πλάτος παραθύρου")
    parser.add_argument("--screenshots", type=Path, default=None,
                        help="φάκελος για στιγμιότυπα (offscreen) και έξοδος")
    args = parser.parse_args()
    if args.screenshots:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from director.gui import DirectorMicrophone
    from director.qt_gui import DirectorWindow, create_application, release_window
    from tools.security_demo import fake_stream

    app = create_application()
    with tempfile.TemporaryDirectory(prefix="sch-intercom-demo-") as temp:
        print(f"Προσωρινός φάκελος: {temp} (σβήνεται στο κλείσιμο)")
        broadcaster, model, enrollment, maintenance, backup_path = build(Path(temp))
        try:
            options = {"restart": lambda: window.close(),
                       "choose_save": None, "choose_open": None}
            window = DirectorWindow(broadcaster, DirectorMicrophone(fake_stream, broadcaster),
                                    security=model, maintenance=maintenance,
                                    maintenance_options=options,
                                    layout_settings=Path(temp) / "director-gui.json")
            window.resize(args.width or 1366, 900)
            window.show()
            if args.screenshots:
                screenshots(app, window, backup_path, args.screenshots)
            else:
                if args.page == "certificates":
                    window.show_page("security")
                    window.security_page.show_tab("Πιστοποιητικά")
                else:
                    window.show_page("settings")
                print("Κλείστε το παράθυρο για τέλος· όλα τα δεδομένα επίδειξης σβήνονται.")
                app.exec()
            window.close()
            release_window(window)
            app.processEvents()
        finally:
            enrollment.stop()
    print("Τέλος: ο προσωρινός φάκελος σβήστηκε.")


def screenshots(app, window, backup_path: Path, out: Path) -> None:
    from PySide6.QtWidgets import QDialog

    out.mkdir(parents=True, exist_ok=True)

    def settle():
        for _ in range(6):
            app.processEvents()

    window.show_page("security")
    window.security_page.show_tab("Πιστοποιητικά")
    settle()
    window.grab().save(str(out / "pistopoiitika.png"))
    window.show_page("settings")
    settle()
    window.grab().save(str(out / "syntirisi.png"))
    page = window.settings_page

    def fill_backup(dialog: QDialog):
        dialog.password.setText(PASSWORD)
        dialog.confirm.setText(PASSWORD)
        dialog.show()
        settle()
        dialog.grab().save(str(out / "dialogos-backup.png"))
        dialog.reject()

    page.dialog_hook = fill_backup
    page.open_backup()

    def fill_restore(dialog: QDialog):
        dialog.path.setText(str(backup_path))
        dialog.password.setText(PASSWORD)
        dialog.show()
        dialog.load()
        settle()
        dialog.grab().save(str(out / "dialogos-epanafora.png"))
        dialog.reject()

    page.dialog_hook = fill_restore
    page.open_restore()
    page.dialog_hook = None
    print(f"Στιγμιότυπα: {out}")


if __name__ == "__main__":
    main()
