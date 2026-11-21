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
    "settings": ('<circle cx="12" cy="12" r="3"/>'
                 '<path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1'
                 'M4.9 19.1L7 17M17 7l2.1-2.1"/>'),
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
         selected_color: str | None = None, size: int = 16) -> QIcon:
    """Εικονίδιο με προαιρετικά χρώματα για ανενεργή/επιλεγμένη κατάσταση."""
    result = QIcon()
    result.addPixmap(pixmap(name, color, size), QIcon.Mode.Normal, QIcon.State.Off)
    result.addPixmap(pixmap(name, selected_color or color, size), QIcon.Mode.Normal,
                     QIcon.State.On)
    result.addPixmap(pixmap(name, disabled_color or color, size), QIcon.Mode.Disabled)
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
