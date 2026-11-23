"""Μονόχρωμα γραμμικά εικονίδια και εικονίδιο εφαρμογής (απαιτεί PySide6).

Οι διαδρομές SVG είναι ίδιες με το docs/mockups/director-main.html. Το χρώμα
δίνεται πάντα ως token από το `shared.ui.theme`.
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from shared.ui import theme

# Εσωτερικό <svg viewBox="0 0 24 24">, χρώμα μέσω currentColor.
_PATHS = {
    "home": '<path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
    "rooms": ('<rect x="3" y="3" width="7" height="7" rx="1.5"/>'
              '<rect x="14" y="3" width="7" height="7" rx="1.5"/>'
              '<rect x="3" y="14" width="7" height="7" rx="1.5"/>'
              '<rect x="14" y="14" width="7" height="7" rx="1.5"/>'),
    "announcements": '<path d="M3 10v4h4l7 5V5L7 10z"/><path d="M18 8a5 5 0 0 1 0 8"/>',
    "scenarios": '<path d="M4 6h16M4 12h16M4 18h10"/>',
    "logs": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    # Γρανάζι (απόφαση 22/11/2026, ui-style.md §4).
    "settings": ('<circle cx="12" cy="12" r="3"/>'
                 '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83'
                 'l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0'
                 'v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1'
                 '-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3'
                 'a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82'
                 'l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9'
                 'a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51'
                 ' 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06'
                 'a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4'
                 'h-.09a1.65 1.65 0 0 0-1.51 1z"/>'),
    # Ασπίδα με τικ για τη σελίδα «Ασφάλεια» (ui-style.md §4, §6).
    "security": ('<path d="M12 3 5 6v5c0 4.5 3 8 7 10 4-2 7-5.5 7-10V6z"/>'
                 '<path d="m9 12 2 2 4-4"/>'),
    "trash": ('<path d="M5 6.5h14M10 6.5V4.5h4v2M7 6.5v11.5a2 2 0 0 0 2 2h6'
              'a2 2 0 0 0 2-2V6.5M10.5 10v6.5M13.5 10v6.5"/>'),
    "check": '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    # Στοιχεία μενού υπολογιστή/αίθουσας (ui-style.md §6): μεταφορά σε άλλη αίθουσα (πόρτα),
    # μετονομασία (μολύβι)· η Ανάκληση χρησιμοποιεί το "trash".
    "move": ('<path d="M14 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/>'
             '<path d="M9 7l5 5-5 5M14 12H3"/>'),  # βέλος που μπαίνει σε πόρτα
    "edit": ('<path d="M4 20h4L19 9a2.83 2.83 0 0 0-4-4L4 16z"/>'
             '<path d="M13.5 6.5l4 4"/>'),
    # Τρεις κάθετες τελείες: μενού ενεργειών υπολογιστή/αίθουσας (ui-style.md §6).
    # Σχεδιάζεται με stroke_width ~3 (στρογγυλά άκρα = τελείες).
    "more": '<path d="M12 5h.01M12 12h.01M12 19h.01"/>',
    # Πιστοποιητικά / Συντήρηση (ui-style.md §7–§8).
    "copy": ('<rect x="9" y="9" width="11" height="11" rx="2"/>'
             '<path d="M5 15V6a2 2 0 0 1 2-2h9"/>'),
    # Αρχείο με βέλος προς τα μέσα (backup) / προς τα έξω (επαναφορά), σχόλιο 23/11/2026.
    "backup": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8zM14 3v5h5M12 11v6M9.5 14.5 12 17l2.5-2.5"/>',
    "restore": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8zM14 3v5h5M12 17v-6M9.5 13.5 12 11l2.5 2.5"/>',
    "eye": ('<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/>'
            '<circle cx="12" cy="12" r="3"/>'),
    "eye-off": ('<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/>'
                '<circle cx="12" cy="12" r="3"/><path d="M4 20 20 4"/>'),
    "mic": ('<rect x="9" y="3" width="6" height="11" rx="3"/>'
            '<path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7"/>'),
    # Διαγώνια γραμμή από πάνω δεξιά προς κάτω αριστερά (ui-style.md §4).
    "mic_off": ('<rect x="9" y="3" width="6" height="11" rx="3"/>'
                '<path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/><path d="M20 3L4 21"/>'),
}


def svg_source(name: str, color: str, stroke_width: float = 1.8) -> bytes:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        f'stroke="{color}" stroke-width="{stroke_width}" stroke-linecap="round" '
        f'stroke-linejoin="round" color="{color}">{_PATHS[name]}</svg>'
    ).encode()


def render(painter: QPainter, name: str, color: str, rect: QRectF,
           stroke_width: float = 1.8) -> None:
    QSvgRenderer(QByteArray(svg_source(name, color, stroke_width))).render(painter, rect)


def pixmap(name: str, color: str, size: int, *, ratio: float = 2.0,
           stroke_width: float = 1.8) -> QPixmap:
    pm = QPixmap(QSize(round(size * ratio), round(size * ratio)))
    pm.fill(Qt.GlobalColor.transparent)
    pm.setDevicePixelRatio(ratio)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    render(painter, name, color, QRectF(0, 0, size, size), stroke_width)
    painter.end()
    return pm


def icon(name: str, color: str, *, disabled_color: str | None = None,
         selected_color: str | None = None, size: int = 16,
         stroke_width: float = 1.8) -> QIcon:
    """Εικονίδιο με προαιρετικά χρώματα για ανενεργή/επιλεγμένη κατάσταση."""
    result = QIcon()
    sw = {"stroke_width": stroke_width}
    result.addPixmap(pixmap(name, color, size, **sw), QIcon.Mode.Normal, QIcon.State.Off)
    result.addPixmap(pixmap(name, selected_color or color, size, **sw), QIcon.Mode.Normal,
                     QIcon.State.On)
    result.addPixmap(pixmap(name, disabled_color or color, size, **sw), QIcon.Mode.Disabled)
    return result


def app_icon() -> QIcon:
    """Εικονίδιο «κύματα» από το μοναδικό αρχείο πηγής SVG (ui-style.md §3)."""
    renderer = QSvgRenderer(str(theme.APP_ICON_SVG))
    result = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
        result.addPixmap(pm)
    return result


def app_pixmap(size: int, *, ratio: float = 2.0) -> QPixmap:
    pm = QPixmap(round(size * ratio), round(size * ratio))
    pm.fill(Qt.GlobalColor.transparent)
    pm.setDevicePixelRatio(ratio)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(str(theme.APP_ICON_SVG)).render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pm
