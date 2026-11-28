"""5C1: εικονίδιο tray και παράθυρο του Client (PySide6).

Το Qt τρέχει στο κύριο thread· η σύνδεση και ο ήχος σε thread εργασίας (client.main).
Οι αλλαγές κατάστασης έρχονται με σήμα Qt (queued), όχι με polling· χωρίς timer
animation (docs/development.md §8). Εμφάνιση: docs/ui-style.md §10, λειτουργία:
docs/phase5.md §6. 5C2: αριστερή μπάρα όπως ο Director, «Αλλαγή…» για αίθουσα/όνομα και
κλίμακα που ισχύει αμέσως (χωρίς διακοπή της σύνδεσης).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import threading
from collections.abc import Callable

from PySide6.QtCore import QByteArray, QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QColor, QFont, QFontDatabase, QFontMetrics, QIcon,
                           QPainter, QPixmap)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QApplication, QButtonGroup, QDialog, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QLayout, QLineEdit, QMenu, QPushButton,
                               QScrollArea, QSizePolicy, QStackedWidget, QSystemTrayIcon,
                               QVBoxLayout, QWidget)

from client import status as st
from shared.ui import icons, theme

APP_NAME = "School Intercom"
TAB_HISTORY = "Ιστορικό"
TAB_INFO = "Πληροφορίες"
#: Η σειρά των καρτελών· μια τρίτη (π.χ. «Μηνύματα», phase5.md §6) μπαίνει εδώ.
TABS = (TAB_HISTORY, TAB_INFO)
NAV_ICONS = ("logs", "info")
#: Υπότιτλος κάτω από το «School Intercom» (ο Director γράφει «Director»).
BRAND_SUB = "Client"
HISTORY_RANGE = "Τελευταίες 30 ημέρες"
#: Μενού tray (5C2, απόφαση χρήστη 28/11/2026): κατευθείαν στην καρτέλα· το πρώτο (έντονο)
#: είναι και αυτό που ανοίγει με κλικ/διπλό κλικ στο εικονίδιο.
MENU_HISTORY = "Ιστορικό…"
MENU_INFO = "Πληροφορίες…"
TONE_COLORS = {"ok": theme.ONLINE, "wait": theme.PENDING, "bad": theme.LIVE}
TRAY_SIZES = (16, 20, 24, 32, 40, 48, 64)
#: Πάχος των εξωτερικών κύκλων του tray (μονάδες του SVG των 24· απόφαση χρήστη 28/11/2026).
TRAY_RING_WIDTH = 1.3

_metrics = theme.metrics_for(None)


def px(value: float) -> int:
    return _metrics.px(value)


def setup_application(argv: list[str] | None = None, *, base_pt: float | None = None,
                      ui_scale: int | None = None) -> QApplication:
    """QApplication με το κοινό στυλ (όπως ο Director, χωρίς εξάρτηση από αυτόν)."""
    global _metrics
    app = QApplication.instance() or QApplication(argv or [APP_NAME])
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setQuitOnLastWindowClosed(False)   # το ✕ κρύβει στο tray
    scale = ui_scale or theme.default_ui_scale()
    system_pt = app.property("schSystemFontPt")
    if system_pt is None:
        font = app.font()
        system_pt = font.pointSizeF() if font.pointSizeF() > 0 else 0.0
        app.setProperty("schSystemFontPt", system_pt)
    _metrics = (theme.Metrics(base_pt * scale / 100) if base_pt else
                theme.metrics_for(system_pt or None, scale))
    font = app.font()
    font.setPointSizeF(_metrics.font_pt())
    app.setFont(font)
    app.setStyleSheet(theme.stylesheet(_metrics))
    app.setWindowIcon(icons.app_icon())
    return app


def mono_family() -> str:
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()


def _repolish(widget: QWidget, **properties) -> None:
    changed = False
    for name, value in properties.items():
        if widget.property(name) != value:
            widget.setProperty(name, value)
            changed = True
    if changed:
        widget.style().unpolish(widget)
        widget.style().polish(widget)


# --- Εικονίδιο tray ---------------------------------------------------------------------
def tray_svg(color: str) -> str:
    """Τα κύματα του εικονιδίου εφαρμογής (§3) μονόχρωμα, χωρίς φόντο, ίδια ένταση."""
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="2.8 2.8 18.4 18.4">'
            f'<circle cx="12" cy="12" r="2.4" fill="{color}"/>'
            f'<circle cx="12" cy="12" r="5.2" fill="none" stroke="{color}" stroke-width="{TRAY_RING_WIDTH}"/>'
            f'<circle cx="12" cy="12" r="8.2" fill="none" stroke="{color}" stroke-width="{TRAY_RING_WIDTH}"/>'
            '</svg>')


def taskbar_is_dark() -> bool:
    """Σκούρα γραμμή εργασιών → λευκά κύματα. Windows: ρύθμιση θέματος της γραμμής
    εργασιών (SystemUsesLightTheme)· αλλού: χρωματικό σχήμα/παλέτα του Qt."""
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows"
                                r"\CurrentVersion\Themes\Personalize") as key:
                return winreg.QueryValueEx(key, "SystemUsesLightTheme")[0] == 0
        except OSError:
            return True     # Windows 10 πριν από το φωτεινό θέμα: σκούρα γραμμή
    app = QApplication.instance()
    if app is None:
        return False
    scheme = app.styleHints().colorScheme()
    if scheme != Qt.ColorScheme.Unknown:
        return scheme == Qt.ColorScheme.Dark
    return app.palette().window().color().lightness() < 128


def tray_pixmap(size: int, tone: str | None, dark: bool = False) -> QPixmap:
    """Μονόχρωμα κύματα στο 85% κάτω αριστερά (μαύρα, λευκά σε σκούρα γραμμή) και
    κουκκίδα πάνω δεξιά με διάφανο κενό γύρω της (ui-style.md §10)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = theme.TRAY_GLYPH_DARK if dark else theme.TRAY_GLYPH_LIGHT
    side = size * theme.TRAY_APP_SCALE
    QSvgRenderer(QByteArray(tray_svg(color).encode())).render(
        painter, QRectF(0, size - side, side, side))
    if tone is not None:
        radius = size * theme.TRAY_DOT
        ring = max(1.0, size * theme.TRAY_DOT_RING)
        outer = radius + ring
        center = QPointF(size - outer, outer)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.setBrush(QColor(0, 0, 0))
        painter.drawEllipse(center, outer, outer)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setBrush(QColor(TONE_COLORS[tone]))
        painter.drawEllipse(center, radius, radius)
    painter.end()
    return pm


def tray_icon(tone: str | None, dark: bool = False) -> QIcon:
    result = QIcon()
    for size in TRAY_SIZES:
        result.addPixmap(tray_pixmap(size, tone, dark))
    return result


# --- Γέφυρα thread εργασίας → Qt ----------------------------------------------------------
class _Bridge(QObject):
    changed = Signal()
    finished = Signal()


# --- Μέρη του παραθύρου ---------------------------------------------------------------------
class _Bar(QFrame):
    """Μπάρα ειδοποίησης (κόκκινη: ανάκληση/πρόβλημα, μπλε: αναμονή), όπως ui-style §4."""

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.setObjectName("alarmBar" if kind == "alarm" else "infoBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(px(16), px(9), px(14), px(9))
        layout.setSpacing(px(14))
        color = theme.LIVE if kind == "alarm" else theme.PRIMARY
        icon = QLabel()
        icon.setPixmap(icons.pixmap("alert" if kind == "alarm" else "info", color, px(20),
                                    stroke_width=2.0))
        icon.setStyleSheet("background: transparent;")
        self.text = QLabel()
        self.text.setObjectName("barText")
        self.text.setWordWrap(True)
        layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.text, 1)
        self.hide()


class _CodeCard(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("codeCard")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(px(20), px(12), px(20), px(14))
        layout.setSpacing(px(30))
        left = QVBoxLayout()
        left.setSpacing(0)
        label = QLabel("Κωδικός εγγραφής")
        label.setObjectName("muted")
        self.code = QLabel()
        self.code.setObjectName("enrollCode")
        self.code.setStyleSheet(f'font-family: "{mono_family()}";')
        self.code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        left.addWidget(label)
        left.addWidget(self.code)
        right = QVBoxLayout()
        right.setSpacing(px(2))
        right.addStretch(1)
        self.title = QLabel("Αναμονή έγκρισης από τον Director")
        self.expires = QLabel()
        self.expires.setObjectName("muted")
        right.addWidget(self.title)
        right.addWidget(self.expires)
        right.addStretch(1)
        layout.addLayout(left)
        layout.addLayout(right, 1)
        self.hide()

    def set_code(self, enrollment: dict) -> None:
        self.code.setText(st.format_code(enrollment["code"]).replace(" ", " "))
        self.expires.setText(st.expiry_text(enrollment["expires_at"]))


class _HistoryView(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet("QScrollArea { background: transparent; }")
        self.signature = None
        self.rows: list[dict] = []
        self.render([])

    def render(self, groups: list[dict]) -> None:
        signature = json.dumps(groups, ensure_ascii=False, sort_keys=True)
        if signature == self.signature:
            return
        self.signature = signature
        holder = QWidget()
        holder.setObjectName("page")
        outer = QVBoxLayout(holder)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(px(18), px(12), px(18), px(12))
        layout.setSpacing(0)
        self.rows = []
        if not groups:
            empty = QLabel(st.EMPTY_HISTORY)
            empty.setObjectName("muted")
            layout.addWidget(empty)
            self.rows.append({"empty": st.EMPTY_HISTORY})
        for index, group in enumerate(groups):
            day = QLabel(group["day"])
            day.setObjectName("historyDay")
            day.setContentsMargins(0, px(10) if index else 0, 0, px(4))
            layout.addWidget(day)
            for row in group["rows"]:
                layout.addWidget(self._row(row))
                self.rows.append({"day": group["day"], **row})
        outer.addWidget(card)
        outer.addStretch(1)
        self.setWidget(holder)

    def _row(self, row: dict) -> QWidget:
        widget = QWidget()
        line = QHBoxLayout(widget)
        line.setContentsMargins(0, px(4), 0, px(4))
        line.setSpacing(px(12))
        when = QLabel(row["time"])
        when.setMinimumWidth(px(46))
        icon = QLabel()
        color = theme.LIVE if row["live"] else theme.TEXT_MUTED
        icon.setPixmap(icons.pixmap("announcements" if row["known"] else "info", color,
                                    px(theme.ICON)))
        kind = QLabel(row["kind"])
        kind.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        line.addWidget(when)
        line.addWidget(icon)
        line.addWidget(kind, 1)
        if row["live"]:
            right = QLabel("● σε εξέλιξη")
            right.setObjectName("historyLive")
        else:
            right = QLabel(row["duration"])
            right.setObjectName("muted")
        line.addWidget(right, 0, Qt.AlignmentFlag.AlignRight)
        if not row.get("text"):
            return widget
        # Μελλοντικά γεγονότα με κείμενο (phase5.md §6.3): δεύτερη γραμμή κάτω από το είδος.
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(widget)
        text = QLabel(row["text"])
        text.setObjectName("historyText")
        text.setWordWrap(True)
        text.setContentsMargins(px(46) + px(12) * 2 + px(theme.ICON), 0, 0, px(4))
        column.addWidget(text)
        return box


class _InfoView(QWidget):
    """Πίνακας ετικέτα/τιμή· 5C2: τρίτη στήλη με «Αλλαγή…» τέρμα δεξιά (row["action"])."""

    def __init__(self, on_action: Callable[[str], None] | None = None, parent=None):
        super().__init__(parent)
        self.on_action = on_action
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.card = QFrame()
        self.card.setObjectName("card")
        self.grid = QGridLayout(self.card)
        self.grid.setContentsMargins(px(18), px(12), px(18), px(12))
        self.grid.setHorizontalSpacing(px(theme.KV_GAP) + px(12))
        self.grid.setVerticalSpacing(px(8))
        self.grid.setColumnStretch(1, 1)
        outer.addWidget(self.card)
        outer.addStretch(1)
        self.labels: list[tuple[QLabel, QLabel, QPushButton]] = []
        self.rows: list[dict] = []

    def _button(self, index: int) -> QPushButton:
        button = QPushButton(st.CHANGE_BUTTON)
        button.setObjectName("pillSmall")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setIcon(icons.icon("edit", theme.TEXT, size=px(theme.ICON)))
        button.setIconSize(QSize(px(theme.ICON), px(theme.ICON)))
        button.clicked.connect(lambda _=False, i=index: self._clicked(i))
        return button

    def _clicked(self, index: int) -> None:
        if index < len(self.rows) and self.rows[index]["action"] and self.on_action:
            self.on_action(self.rows[index]["action"])

    def action_button(self, action: str) -> QPushButton | None:
        for row, (_, _, button) in zip(self.rows, self.labels):
            if row["action"] == action:
                return button
        return None

    def render(self, rows: list[dict]) -> None:
        self.rows = rows
        while len(self.labels) < len(rows):
            label, value = QLabel(), QLabel()
            label.setObjectName("kvLabel")
            value.setObjectName("kvValue")
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setWordWrap(True)
            index = len(self.labels)
            button = self._button(index)
            self.grid.addWidget(label, index, 0, Qt.AlignmentFlag.AlignVCenter)
            self.grid.addWidget(value, index, 1)
            self.grid.addWidget(button, index, 2, Qt.AlignmentFlag.AlignRight |
                                Qt.AlignmentFlag.AlignVCenter)
            self.labels.append((label, value, button))
        mono = mono_family()
        small = f"{_metrics.font_pt(theme.FONT_SMALL)}pt"
        for index, (label, value, button) in enumerate(self.labels):
            visible = index < len(rows)
            label.setVisible(visible)
            value.setVisible(visible)
            button.setVisible(visible and bool(rows[index]["action"]) if visible else False)
            if not visible:
                continue
            row = rows[index]
            label.setText(row["label"])
            text = (_dot(row["tone"]) if row["dot"] else "") + _escape(row["value"])
            if row["note"]:
                text += (f' <span style="color:{theme.TEXT_MUTED}; font-size:{small}">'
                         f'{_escape(row["note"])}</span>')
            value.setTextFormat(Qt.TextFormat.RichText)
            value.setText(text)
            value.setStyleSheet(f'font-family: "{mono}"; font-size: {small};'
                                if row["mono"] else "")
            value.setWordWrap(not row["mono"])   # ID/αποτύπωμα σε μία γραμμή (mockup)
            tone = row["tone"]
            if row["dot"]:
                tone = "state_" + tone
            _repolish(value, tone=tone or "")


#: Η κουκκίδα κατάστασης· η κίτρινη πιο φωτεινή από το κείμενο (mockup #c-pending).
DOT_COLORS = {"ok": theme.CONNECTED, "wait": theme.PENDING, "bad": theme.LIVE}


def _dot(tone: str) -> str:
    color = DOT_COLORS.get(tone)
    return f'<span style="color:{color}">●</span> ' if color else "● "


def _escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# --- Διάλογοι (5C2) ------------------------------------------------------------------------
class _Dialog(QDialog):
    """Βάση των διαλόγων του Client: εικονίδιο εφαρμογής στη γραμμή τίτλου (απόφαση
    χρήστη 28/11/2026), ίδιο φόντο/κουμπιά με τους διαλόγους του Director."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("maintenanceDialog")
        self.setWindowTitle(title)
        self.setWindowIcon(icons.app_icon())
        self.setModal(True)
        self.body = QVBoxLayout(self)
        self.body.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        self.body.addStrut(px(theme.DIALOG_WIDTH) - 2 * px(15))
        self.body.setContentsMargins(px(15), px(14), px(15), px(15))
        self.body.setSpacing(px(11))

    def text(self, text: str, name: str | None = None) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        if name:
            label.setObjectName(name)
        self.body.addWidget(label)
        return label

    def footer(self, accept_text: str) -> tuple[QPushButton, QPushButton]:
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Άκυρο")
        cancel.setObjectName("secondaryAction")
        accept = QPushButton(accept_text)
        accept.setObjectName("primaryAction")
        for button in (cancel, accept):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(button)
        cancel.clicked.connect(self.reject)
        self.body.addLayout(row)
        return cancel, accept


class ChangeDialog(_Dialog):
    """«Αλλαγή…» στον Υπολογιστή: αίθουσα και όνομα· το κείμενο ανάλογα με την κατάσταση."""

    def __init__(self, mode: str, room: str, name: str, parent=None):
        super().__init__(st.CHANGE_TITLE, parent)
        self.mode = mode
        intro, accept_text = st.CHANGE_DIALOG[mode]
        self.intro = self.text(intro)
        fields = QHBoxLayout()
        fields.setSpacing(px(12))
        self.room = self._field(fields, "Αίθουσα", room)
        self.name = self._field(fields, "Υπολογιστής", name)
        self.body.addLayout(fields)
        self.error = self.text("", "warnText")
        self.error.hide()
        self.cancel, self.accept_button = self.footer(accept_text)
        self.accept_button.setDefault(True)
        self.accept_button.clicked.connect(self._accept)
        self.room.setFocus()

    def _field(self, row: QHBoxLayout, label: str, value: str) -> QLineEdit:
        box = QVBoxLayout()
        box.setSpacing(px(4))
        caption = QLabel(label)
        caption.setObjectName("fieldLabel")
        edit = QLineEdit(value)
        edit.setObjectName("field")
        edit.setMaxLength(64)
        box.addWidget(caption)
        box.addWidget(edit)
        row.addLayout(box, 1)
        return edit

    def values(self) -> tuple[str, str]:
        return clean_values(self.room.text(), self.name.text())

    def _accept(self) -> None:
        try:
            self.values()
        except ValueError as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        self.accept()


def clean_values(room: str, name: str) -> tuple[str, str]:
    from shared.change_request import clean
    return clean(room, name)


class ScaleDialog(_Dialog):
    """Κλίμακα του παραθύρου: 70–135%, επιλογές στο κέντρο· ισχύει αμέσως."""

    def __init__(self, current: int | None, parent=None):
        super().__init__(st.SCALE_TITLE, parent)
        self.text(st.SCALE_INTRO)
        row = QHBoxLayout()
        row.setSpacing(px(8))
        row.addStretch(1)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[int, QPushButton] = {}
        self.chosen = current if current in theme.UI_SCALES else theme.default_ui_scale()
        for value in theme.UI_SCALES:
            button = QPushButton(f"{value}%")
            button.setObjectName("tab")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            _fit_tab(button)
            button.setChecked(value == self.chosen)
            self.group.addButton(button, value)
            self.buttons[value] = button
            row.addWidget(button)
        row.addStretch(1)
        self.group.idClicked.connect(self._choose)
        self.body.addLayout(row)
        self.text(st.SCALE_NOTE, "muted")
        self.cancel, self.accept_button = self.footer(st.SCALE_APPLY)
        self.accept_button.setDefault(True)
        self.accept_button.clicked.connect(self.accept)

    def _choose(self, value: int) -> None:
        self.chosen = value


# --- Παράθυρο ------------------------------------------------------------------------------
class ClientWindow(QWidget):
    """Όπως ο Director (5C2): αριστερή μπάρα με εικονίδιο, «School Intercom»/«Client»,
    Ιστορικό/Πληροφορίες και «αίθουσα · PC» κάτω· δεξιά τίτλος ενότητας και κατάσταση."""

    def __init__(self, *, hide_on_close: bool = True,
                 on_action: Callable[[str], None] | None = None):
        super().__init__()
        self.setObjectName("page")
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(icons.app_icon())
        self.hide_on_close = hide_on_close
        self.resize(px(theme.CLIENT_WINDOW_W), px(theme.CLIENT_WINDOW_H))
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())
        content = QWidget()
        content.setObjectName("page")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(px(18), px(14), px(18), px(16))
        layout.setSpacing(px(12))
        header = QHBoxLayout()
        self.page_title = QLabel()
        self.page_title.setObjectName("pageTitle")
        self.status = QLabel()
        self.status.setObjectName("clientStatus")
        header.addWidget(self.page_title, 1)
        header.addWidget(self.status, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(header)
        self.alarm = _Bar("alarm")
        self.info_bar = _Bar("info")
        self.code_card = _CodeCard()
        layout.addWidget(self.alarm)
        layout.addWidget(self.info_bar)
        layout.addWidget(self.code_card)
        self.range = QLabel(HISTORY_RANGE)
        self.range.setObjectName("small")
        self.range.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.range)
        self.pages = QStackedWidget()
        self.history = _HistoryView()
        self.info = _InfoView(on_action)
        self.pages.addWidget(self.history)
        self.pages.addWidget(self.info)
        layout.addWidget(self.pages, 1)
        root.addWidget(content, 1)
        self.show_tab(TAB_HISTORY)

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(px(theme.SIDEBAR_WIDTH))
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(px(10), px(14), px(10), px(14))
        layout.setSpacing(px(theme.NAV_SPACING))
        brand = QHBoxLayout()
        brand.setContentsMargins(px(4), px(2), px(4), px(14))
        brand.setSpacing(px(8))
        logo = QLabel()
        logo.setPixmap(icons.app_pixmap(px(theme.BRAND_ICON)))
        names = QVBoxLayout()
        names.setSpacing(0)
        name = QLabel(APP_NAME)
        name.setObjectName("brand")
        self.brand_sub = QLabel(BRAND_SUB)
        self.brand_sub.setObjectName("brandSub")
        names.addWidget(name)
        names.addWidget(self.brand_sub)
        brand.addWidget(logo, 0, Qt.AlignmentFlag.AlignVCenter)
        brand.addLayout(names, 1)
        layout.addLayout(brand)
        self.tab_group = QButtonGroup(self)
        self.tab_group.setExclusive(True)
        self.tab_buttons: dict[str, QPushButton] = {}
        for index, (name_text, icon) in enumerate(zip(TABS, NAV_ICONS)):
            button = QPushButton(name_text)
            button.setObjectName("navItem")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setIcon(icons.icon(icon, theme.TEXT, selected_color=theme.PRIMARY,
                                      disabled_color=theme.TEXT_MUTED, size=px(theme.ICON)))
            button.setIconSize(QSize(px(theme.ICON), px(theme.ICON)))
            self.tab_group.addButton(button, index)
            self.tab_buttons[name_text] = button
            layout.addWidget(button)
        self.tab_group.idClicked.connect(self.show_tab_index)
        layout.addStretch(1)
        rule = QFrame()
        rule.setObjectName("sidebarRule")
        rule.setFixedHeight(1)
        layout.addWidget(rule)
        self.who = QLabel()
        self.who.setObjectName("sidebarWho")
        self.who.setContentsMargins(px(6), px(8), px(4), 0)
        layout.addWidget(self.who)
        return sidebar

    @property
    def current_tab(self) -> str:
        return TABS[self.pages.currentIndex()]

    def show_tab(self, name: str) -> None:
        self.show_tab_index(TABS.index(name))

    def show_tab_index(self, index: int) -> None:
        self.tab_group.button(index).setChecked(True)
        self.pages.setCurrentIndex(index)
        self.page_title.setText(TABS[index])
        self.range.setVisible(TABS[index] == TAB_HISTORY)

    def render(self, snap: dict, events: list[dict]) -> None:
        who = st.title(snap)
        self.who.setText(who)
        self.setWindowTitle(window_title(snap))
        self.status.setTextFormat(Qt.TextFormat.RichText)
        self.status.setText(_dot(st.tone_name(snap)) + _escape(st.status_label(snap)))
        _repolish(self.status, tone=st.tone_name(snap))
        alarm, info = st.window_bars(snap)
        for bar, text in ((self.alarm, alarm), (self.info_bar, info)):
            bar.setVisible(text is not None)
            if text is not None and bar.text.text() != text:
                bar.text.setTextFormat(Qt.TextFormat.RichText)
                bar.text.setText(text)
        enrollment = snap.get("enrollment")
        show_code = snap["state"] == st.PENDING and bool(enrollment)
        self.code_card.setVisible(show_code)
        if show_code:
            self.code_card.set_code(enrollment)
        self.history.render(st.history_groups(events))
        self.info.render(st.info_rows(snap))

    def closeEvent(self, event) -> None:   # noqa: N802 (Qt)
        if self.hide_on_close:
            event.ignore()
            self.hide()
        else:
            super().closeEvent(event)

    def dump(self) -> dict:
        """Τι δείχνει το παράθυρο (για τα live tests 5C1/5C2), από τα ίδια τα widgets."""
        return {
            "visible": self.isVisible(),
            "title": self.who.text(),
            "window_title": self.windowTitle(),
            "page_title": self.page_title.text(),
            "brand_sub": self.brand_sub.text(),
            "status": "● " + self.status.text().rsplit("</span> ", 1)[-1]
            if "</span>" in self.status.text() else self.status.text(),
            "status_tone": self.status.property("tone"),
            "alarm": _plain(self.alarm.text.text()) if not self.alarm.isHidden() else None,
            "info": _plain(self.info_bar.text.text()) if not self.info_bar.isHidden() else None,
            "code": self.code_card.code.text().replace("\u2002", " ")
            if not self.code_card.isHidden() else None,
            "code_expires": self.code_card.expires.text()
            if not self.code_card.isHidden() else None,
            "tabs": [button.text() for button in self.tab_buttons.values()],
            "tab": self.current_tab,
            "history": self.history.rows,
            "info_rows": [{"label": row["label"], "value": row["value"],
                           "note": row["note"], "action": row["action"]}
                          for row in self.info.rows],
        }


def window_title(snap: dict) -> str:
    """«School Intercom — C2627-Γ1 · Board1» (απόφαση χρήστη 28/11/2026)."""
    if snap.get("room") and snap.get("name"):
        return f"{APP_NAME} — {st.title(snap)}"
    return APP_NAME


def _plain(text: str) -> str:
    return text.replace("<b>", "").replace("</b>", "").replace("&amp;", "&") \
        .replace("&lt;", "<").replace("&gt;", ">")


def _fit_tab(button: QPushButton) -> None:
    """Πλάτος για το έντονο κείμενο της επιλεγμένης καρτέλας (όπως qt_gui.fit_tab_text)."""
    button.ensurePolished()
    bold = QFont(button.font())
    bold.setWeight(QFont.Weight.DemiBold)
    width = QFontMetrics(bold).horizontalAdvance(button.text())
    button.setMinimumWidth(width + 2 * px(16) + 2 + px(4))


def window_bars(snap: dict) -> tuple[str | None, str | None]:
    """Συμβατότητα: οι μπάρες είναι πλέον στο client.status (χωρίς Qt)."""
    return st.window_bars(snap)


# --- Tray και εφαρμογή -----------------------------------------------------------------------
class ClientTray(QObject):
    """Συνδέει ClientStatus/History με το tray και το παράθυρο."""

    def __init__(self, status: st.ClientStatus, history: st.History, *,
                 dump_file: Path | None = None, tray_available: bool | None = None,
                 on_change: Callable[[str, str], object] | None = None,
                 on_scale: Callable[[int], None] | None = None,
                 dialog_hook: Callable[[QDialog], int] | None = None):
        super().__init__()
        self.status = status
        self.history = history
        self.dump_file = dump_file
        self.on_change = on_change
        self.on_scale = on_scale
        #: Δοκιμές: αντί για exec() του διαλόγου (όπως dialog_hook στη «Συντήρηση»).
        self.dialog_hook = dialog_hook
        self.dialog: QDialog | None = None
        app = QApplication.instance()
        if tray_available is None:
            tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        # Χωρίς tray (π.χ. επιφάνεια εργασίας Linux χωρίς περιοχή ειδοποιήσεων) το παράθυρο
        # είναι ο μόνος τρόπος εμφάνισης: δεν κρύβεται στο ✕. Στο offscreen (δοκιμές)
        # συμπεριφέρεται σαν να υπάρχει tray.
        offscreen = app is not None and app.platformName() == "offscreen"
        self.tray_available = tray_available or offscreen
        self.window = ClientWindow(hide_on_close=self.tray_available, on_action=self.action)
        self.tray = QSystemTrayIcon()
        self.menu = QMenu()
        self.menu_title = QAction(self.menu)
        self.menu_state = QAction(self.menu)
        for action in (self.menu_title, self.menu_state):
            action.setEnabled(False)
            self.menu.addAction(action)
        self.menu.addSeparator()
        self.open_action = QAction(MENU_HISTORY, self.menu)
        bold = QFont(self.open_action.font())
        bold.setBold(True)
        self.open_action.setFont(bold)
        self.open_action.triggered.connect(lambda: self.open_window(TAB_HISTORY))
        self.menu.addAction(self.open_action)
        self.menu.setDefaultAction(self.open_action)
        self.info_action = QAction(MENU_INFO, self.menu)
        self.info_action.triggered.connect(lambda: self.open_window(TAB_INFO))
        self.menu.addAction(self.info_action)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._activated)
        self._icon: tuple[str | None, bool] | None = None
        # Θέμα γραμμής εργασιών: αλλαγή στα Windows χωρίς σήμα του Qt → ανάγνωση της
        # ρύθμισης κάθε λίγα δευτερόλεπτα (μία τιμή μητρώου, όχι animation).
        self.taskbar_dark = taskbar_is_dark
        self._theme_timer = QTimer()
        self._theme_timer.timeout.connect(self.notify)
        self._theme_timer.start(theme.TRAY_THEME_POLL_MS)
        if app is not None:
            app.styleHints().colorSchemeChanged.connect(lambda *_: self.notify())
        self._opened_for: str | None = None
        self._dirty = False
        self.bridge = _Bridge()
        self.bridge.changed.connect(self.refresh, Qt.ConnectionType.QueuedConnection)
        status.add_listener(self.notify)
        self.refresh()
        if tray_available:
            self.tray.show()
        elif not offscreen:
            self.window.show()

    def notify(self) -> None:
        """Από οποιοδήποτε thread· πολλές αλλαγές μαζί → ένα refresh."""
        if not self._dirty:
            self._dirty = True
            self.bridge.changed.emit()

    def _activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.open_window(TAB_HISTORY)

    def open_window(self, tab: str | None = None) -> None:
        """``tab``: από το μενού/εικονίδιο tray· None = όπου ήταν (π.χ. κωδικός εγγραφής)."""
        if tab is not None:
            self.window.show_tab(tab)
        self.window.show()
        self.window.setWindowState(self.window.windowState() & ~Qt.WindowState.WindowMinimized)
        self.window.raise_()
        self.window.activateWindow()
        self.write_dump()

    def refresh(self) -> None:
        self._dirty = False
        snap = self.status.snapshot()
        tone = st.tone(snap)
        icon = (tone, bool(self.taskbar_dark()))
        if icon != self._icon:
            self._icon = icon
            self.tray.setIcon(tray_icon(*icon))
        self.tray.setToolTip(st.tooltip(snap))
        first, second = st.menu_lines(snap)
        self.menu_title.setText(first)
        self.menu_state.setText(second)
        self.window.render(snap, self.history.all())
        # Πρώτη εγκατάσταση: το παράθυρο ανοίγει μόνο του μία φορά ανά νέο κωδικό.
        enrollment = snap.get("enrollment")
        if snap["state"] == st.PENDING and enrollment and \
                enrollment.get("code") != self._opened_for:
            self._opened_for = enrollment.get("code")
            self.open_window()
        self.write_dump()

    # --- 5C2: «Αλλαγή…» -------------------------------------------------------------
    def action(self, action: str) -> None:
        if action == st.ACTION_CHANGE:
            self.change_dialog()
        elif action == st.ACTION_SCALE:
            self.scale_dialog()

    def _run(self, dialog: QDialog) -> int:
        self.dialog = dialog
        try:
            if self.dialog_hook is not None:
                return self.dialog_hook(dialog)
            return dialog.exec()
        finally:
            self.dialog = None

    def change_dialog(self) -> str | None:
        snap = self.status.snapshot()
        change = snap.get("change") or {}
        room = change.get("room") or snap.get("room") or ""
        name = change.get("name") or snap.get("name") or ""
        dialog = ChangeDialog(st.change_mode(snap), room, name, self.window)
        if self._run(dialog) != QDialog.DialogCode.Accepted:
            return None
        room, name = dialog.values()
        if self.on_change is None:
            return None
        return self.on_change(room, name)

    def scale_dialog(self) -> int | None:
        current = self.status.snapshot().get("scale")
        dialog = ScaleDialog(current, self.window)
        if self._run(dialog) != QDialog.DialogCode.Accepted or dialog.chosen == current:
            return None
        self.apply_scale(dialog.chosen)
        return dialog.chosen

    def apply_scale(self, scale: int) -> None:
        """Ισχύει αμέσως: νέες μετρήσεις/QSS και νέο παράθυρο· η σύνδεση δεν διακόπτεται."""
        if self.on_scale is not None:
            self.on_scale(scale)
        else:
            self.status.update(scale=scale)
        setup_application(ui_scale=scale)
        old = self.window
        visible, tab, position = old.isVisible(), old.current_tab, old.pos()
        self.window = ClientWindow(hide_on_close=self.tray_available, on_action=self.action)
        self.window.show_tab(tab)
        self.window.render(self.status.snapshot(), self.history.all())
        if visible:
            self.window.move(position)
            self.window.show()
        old.hide_on_close = False
        old.close()
        old.deleteLater()
        self.write_dump()

    def dump(self) -> dict:
        return {
            "tray_available": self.tray_available,
            "tone": self._icon[0] if self._icon else None,
            "tray_dark": self._icon[1] if self._icon else None,
            "tooltip": self.tray.toolTip(),
            "menu": [{"text": action.text(), "enabled": action.isEnabled()}
                     for action in self.menu.actions() if not action.isSeparator()],
            "window": self.window.dump(),
            "state": self.status.snapshot()["state"],
        }

    def write_dump(self) -> None:
        if self.dump_file is None:
            return
        try:
            data = json.dumps(self.dump(), ensure_ascii=False, indent=1)
            temp = self.dump_file.with_name(self.dump_file.name + ".tmp")
            temp.write_text(data, encoding="utf-8")
            os.replace(temp, self.dump_file)
        except OSError:
            pass


def run_tray(status: st.ClientStatus, history: st.History, network: Callable[[], None], *,
             stop: Callable[[], None], dump_file: Path | None = None,
             show_window: bool = False,
             on_change: Callable[[str, str], object] | None = None,
             on_scale: Callable[[int], None] | None = None) -> int:
    """Qt στο κύριο thread, ``network`` σε thread εργασίας· επιστρέφει όταν τελειώσει."""
    app = setup_application(ui_scale=status.snapshot().get("scale"))
    tray = ClientTray(status, history, dump_file=dump_file, on_change=on_change,
                      on_scale=on_scale)
    if show_window:
        tray.open_window()
    bridge = tray.bridge
    bridge.finished.connect(app.quit, Qt.ConnectionType.QueuedConnection)

    def work() -> None:
        try:
            network()
        finally:
            bridge.finished.emit()

    worker = threading.Thread(target=work, name="client-network", daemon=True)
    worker.start()
    # Ctrl+C στην κονσόλα (ανάπτυξη): το Qt δεν αφήνει την Python να δει το σήμα χωρίς
    # περιοδική επιστροφή στον interpreter· ένας αραιός timer, όχι animation.
    previous = signal.signal(signal.SIGINT, lambda *_: app.quit())
    keepalive = QTimer()
    keepalive.timeout.connect(lambda: None)
    keepalive.start(1000)
    try:
        code = app.exec()
    finally:
        signal.signal(signal.SIGINT, previous)
        keepalive.stop()
        stop()
        worker.join(timeout=3)
        tray.tray.hide()
    return code


__all__ = ["ChangeDialog", "ClientTray", "ClientWindow", "ScaleDialog", "run_tray",
           "setup_application", "taskbar_is_dark", "tray_icon", "tray_pixmap", "tray_svg", "window_bars", "window_title"]
