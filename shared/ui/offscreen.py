"""Ρύθμιση Qt χωρίς οθόνη (offscreen) για tests και στιγμιότυπα. Δεν εισάγει Qt.

Στα Windows το offscreen plugin του Qt δεν βρίσκει από μόνο του γραμματοσειρές
(«QFontDatabase: Cannot find font directory …/PySide6/lib/fonts»): τα ελληνικά
βγαίνουν με εφεδρικά glyphs πολύ πλατύτερα από το πραγματικό παράθυρο (Segoe UI)
και οι έλεγχοι διάταξης αποτυγχάνουν, ενώ η εφαρμογή είναι σωστή. Γι' αυτό
δείχνουμε στο Qt τον φάκελο γραμματοσειρών του συστήματος.
"""
from __future__ import annotations

import os


def windows_fonts_dir() -> str | None:
    """Ο φάκελος γραμματοσειρών των Windows, αν υπάρχει."""
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if not windir:
        return None
    fonts = os.path.join(windir, "Fonts")
    return fonts if os.path.isdir(fonts) else None


def use_offscreen(*, windows: bool | None = None) -> None:
    """Πριν από το πρώτο import του PySide6: offscreen, και στα Windows γραμματοσειρές.

    Δεν αλλάζει τιμές που έχει ήδη ορίσει ο χρήστης (π.χ. QT_QPA_PLATFORM=windows).
    """
    if windows is None:
        windows = os.name == "nt"
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if windows and os.environ["QT_QPA_PLATFORM"] == "offscreen":
        fonts = windows_fonts_dir()
        if fonts is not None:
            os.environ.setdefault("QT_QPA_FONTDIR", fonts)
