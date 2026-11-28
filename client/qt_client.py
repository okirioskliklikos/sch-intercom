"""5C1: εικονίδιο tray και παράθυρο του Client (PySide6).

Το Qt τρέχει στο κύριο thread· η σύνδεση και ο ήχος σε thread εργασίας (client.main).
Οι αλλαγές κατάστασης έρχονται με σήμα Qt (queued), όχι με polling· χωρίς timer
animation (docs/development.md §8). Εμφάνιση: docs/ui-style.md §10, λειτουργία:
docs/phase5.md §6.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QColor, QFont, QFontDatabase, QFontMetrics, QIcon,
                           QPainter, QPixmap)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QMenu, QPushButton, QScrollArea, QSizePolicy,
                               QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget)

from client import status as st
from shared.ui import icons, theme

APP_NAME = "School Intercom"
TAB_HISTORY = "Ιστορικό"
TAB_INFO = "Πληροφορίες"
#: Η σειρά των καρτελών· μια τρίτη (π.χ. «Μηνύματα», phase5.md §6) μπαίνει εδώ.
TABS = (TAB_HISTORY, TAB_INFO)
HISTORY_RANGE = "Τελευταίες 30 ημέρες"
OPEN = "Άνοιγμα"
TONE_COLORS = {"ok": theme.TRAY_OK, "wait": theme.PENDING, "bad": theme.LIVE}
TRAY_SIZES = (16, 20, 24, 32, 40, 48, 64)

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
def tray_pixmap(size: int, tone: str | None) -> QPixmap:
    """Εικονίδιο εφαρμογής με κουκκίδα πάνω δεξιά (ui-style.md §10)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(str(theme.APP_ICON_SVG)).render(painter, QRectF(0, 0, size, size))
    if tone is not None:
        radius = size * theme.TRAY_DOT
        ring = max(1.0, size * theme.TRAY_DOT_RING)
        outer = radius + ring
        center = QPointF(size - outer, outer)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.WHITE))
        painter.drawEllipse(center, outer, outer)
        painter.setBrush(QColor(TONE_COLORS[tone]))
        painter.drawEllipse(center, radius, radius)
    painter.end()
    return pm


def tray_icon(tone: str | None) -> QIcon:
    result = QIcon()
    for size in TRAY_SIZES:
        result.addPixmap(tray_pixmap(size, tone))
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
        self.title = QLabel("Αναμονή έγκρισης από τον Manager")
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
    def __init__(self, parent=None):
        super().__init__(parent)
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
        self.labels: list[tuple[QLabel, QLabel]] = []
        self.rows: list[dict] = []

    def render(self, rows: list[dict]) -> None:
        self.rows = rows
        while len(self.labels) < len(rows):
            label, value = QLabel(), QLabel()
            label.setObjectName("kvLabel")
            value.setObjectName("kvValue")
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setWordWrap(True)
            index = len(self.labels)
            self.grid.addWidget(label, index, 0, Qt.AlignmentFlag.AlignTop)
            self.grid.addWidget(value, index, 1)
            self.labels.append((label, value))
        mono = mono_family()
        small = f"{_metrics.font_pt(theme.FONT_SMALL)}pt"
        for index, (label, value) in enumerate(self.labels):
            visible = index < len(rows)
            label.setVisible(visible)
            value.setVisible(visible)
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


# --- Παράθυρο ------------------------------------------------------------------------------
class ClientWindow(QWidget):
    """640×600: τίτλος/κατάσταση, μπάρες, κάρτα κωδικού, καρτέλες Ιστορικό/Πληροφορίες."""

    def __init__(self, *, hide_on_close: bool = True):
        super().__init__()
        self.setObjectName("page")
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(icons.app_icon())
        self.hide_on_close = hide_on_close
        self.resize(px(theme.CLIENT_WINDOW_W), px(theme.CLIENT_WINDOW_H))
        layout = QVBoxLayout(self)
        margin = px(theme.CLIENT_MARGIN)
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(px(12))
        header = QHBoxLayout()
        self.title = QLabel()
        self.title.setObjectName("clientTitle")
        self.status = QLabel()
        self.status.setObjectName("clientStatus")
        header.addWidget(self.title, 1)
        header.addWidget(self.status, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(header)
        self.alarm = _Bar("alarm")
        self.info_bar = _Bar("info")
        self.code_card = _CodeCard()
        layout.addWidget(self.alarm)
        layout.addWidget(self.info_bar)
        layout.addWidget(self.code_card)
        tabs = QHBoxLayout()
        tabs.setSpacing(px(8))
        self.tab_group = QButtonGroup(self)
        self.tab_group.setExclusive(True)
        self.tab_buttons: dict[str, QPushButton] = {}
        for index, name in enumerate(TABS):
            button = QPushButton(name)
            button.setObjectName("tab")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            _fit_tab(button)
            self.tab_group.addButton(button, index)
            self.tab_buttons[name] = button
            tabs.addWidget(button)
        tabs.addStretch(1)
        self.range = QLabel(HISTORY_RANGE)
        self.range.setObjectName("small")
        tabs.addWidget(self.range, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(tabs)
        self.pages = QStackedWidget()
        self.history = _HistoryView()
        self.info = _InfoView()
        self.pages.addWidget(self.history)
        self.pages.addWidget(self.info)
        layout.addWidget(self.pages, 1)
        self.tab_group.idClicked.connect(self.show_tab_index)
        self.show_tab(TAB_HISTORY)

    @property
    def current_tab(self) -> str:
        return TABS[self.pages.currentIndex()]

    def show_tab(self, name: str) -> None:
        self.show_tab_index(TABS.index(name))

    def show_tab_index(self, index: int) -> None:
        self.tab_group.button(index).setChecked(True)
        self.pages.setCurrentIndex(index)
        self.range.setVisible(TABS[index] == TAB_HISTORY)

    def render(self, snap: dict, events: list[dict]) -> None:
        self.title.setText(st.title(snap))
        self.status.setTextFormat(Qt.TextFormat.RichText)
        self.status.setText(_dot(st.tone_name(snap)) + _escape(st.status_label(snap)))
        _repolish(self.status, tone=st.tone_name(snap))
        alarm, info = window_bars(snap)
        for bar, text in ((self.alarm, alarm), (self.info_bar, info)):
            bar.setVisible(text is not None)
            if text is not None and bar.text.text() != text:
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
        """Τι δείχνει το παράθυρο (για το live test 5C1), από τα ίδια τα widgets."""
        return {
            "visible": self.isVisible(),
            "title": self.title.text(),
            "status": "● " + self.status.text().rsplit("</span> ", 1)[-1]
            if "</span>" in self.status.text() else self.status.text(),
            "status_tone": self.status.property("tone"),
            "alarm": self.alarm.text.text() if not self.alarm.isHidden() else None,
            "info": self.info_bar.text.text() if not self.info_bar.isHidden() else None,
            "code": self.code_card.code.text().replace(" ", " ")
            if not self.code_card.isHidden() else None,
            "code_expires": self.code_card.expires.text()
            if not self.code_card.isHidden() else None,
            "tabs": [button.text() for button in self.tab_buttons.values()],
            "tab": self.current_tab,
            "history": self.history.rows,
            "info_rows": [{"label": row["label"], "value": row["value"],
                           "note": row["note"]} for row in self.info.rows],
        }


def _fit_tab(button: QPushButton) -> None:
    """Πλάτος για το έντονο κείμενο της επιλεγμένης καρτέλας (όπως qt_gui.fit_tab_text)."""
    button.ensurePolished()
    bold = QFont(button.font())
    bold.setWeight(QFont.Weight.DemiBold)
    width = QFontMetrics(bold).horizontalAdvance(button.text())
    button.setMinimumWidth(width + 2 * px(16) + 2 + px(4))


def window_bars(snap: dict) -> tuple[str | None, str | None]:
    """(κόκκινη, μπλε) μπάρα του παραθύρου για την κατάσταση."""
    state = snap["state"]
    if state == st.REVOKED:
        return ("Ο υπολογιστής ανακλήθηκε στον Manager και δεν λαμβάνει ανακοινώσεις. "
                "Για να ξανασυνδεθεί χρειάζεται νέα ταυτότητα και νέα έγκριση."), None
    if state == st.UNAPPROVED:
        return ("Ο υπολογιστής δεν έχει εγκριθεί και δεν υπάρχει αίτημα εγγραφής σε αναμονή. "
                "Τρέξτε ξανά την εγκατάσταση για νέο κωδικό."), None
    if state == st.INCOMPATIBLE:
        return ("Η έκδοση δεν είναι συμβατή με τον Manager· αναμονή ενημέρωσης από τον "
                "Manager."), None
    if state == st.ERROR:
        return "Δεν έγινε σύνδεση: " + (snap.get("detail") or "άγνωστο σφάλμα"), None
    if state == st.FULL:
        return None, ("Ο Manager έχει συμπληρώσει το όριο υπολογιστών· νέα προσπάθεια "
                      "κάθε 30 δευτερόλεπτα.")
    return None, None


# --- Tray και εφαρμογή -----------------------------------------------------------------------
class ClientTray(QObject):
    """Συνδέει ClientStatus/History με το tray και το παράθυρο."""

    def __init__(self, status: st.ClientStatus, history: st.History, *,
                 dump_file: Path | None = None, tray_available: bool | None = None):
        super().__init__()
        self.status = status
        self.history = history
        self.dump_file = dump_file
        app = QApplication.instance()
        if tray_available is None:
            tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        # Χωρίς tray (π.χ. επιφάνεια εργασίας Linux χωρίς περιοχή ειδοποιήσεων) το παράθυρο
        # είναι ο μόνος τρόπος εμφάνισης: δεν κρύβεται στο ✕. Στο offscreen (δοκιμές)
        # συμπεριφέρεται σαν να υπάρχει tray.
        offscreen = app is not None and app.platformName() == "offscreen"
        self.tray_available = tray_available or offscreen
        self.window = ClientWindow(hide_on_close=self.tray_available)
        self.tray = QSystemTrayIcon()
        self.menu = QMenu()
        self.menu_title = QAction(self.menu)
        self.menu_state = QAction(self.menu)
        for action in (self.menu_title, self.menu_state):
            action.setEnabled(False)
            self.menu.addAction(action)
        self.menu.addSeparator()
        self.open_action = QAction(OPEN, self.menu)
        bold = QFont(self.open_action.font())
        bold.setBold(True)
        self.open_action.setFont(bold)
        self.open_action.triggered.connect(self.open_window)
        self.menu.addAction(self.open_action)
        self.menu.setDefaultAction(self.open_action)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._activated)
        self._tone: str | None = None
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
            self.open_window()

    def open_window(self) -> None:
        self.window.show()
        self.window.setWindowState(self.window.windowState() & ~Qt.WindowState.WindowMinimized)
        self.window.raise_()
        self.window.activateWindow()
        self.write_dump()

    def refresh(self) -> None:
        self._dirty = False
        snap = self.status.snapshot()
        tone = st.tone(snap)
        if tone != self._tone:
            self._tone = tone
            self.tray.setIcon(tray_icon(tone))
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

    def dump(self) -> dict:
        return {
            "tray_available": self.tray_available,
            "tone": self._tone,
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
             show_window: bool = False) -> int:
    """Qt στο κύριο thread, ``network`` σε thread εργασίας· επιστρέφει όταν τελειώσει."""
    app = setup_application()
    tray = ClientTray(status, history, dump_file=dump_file)
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


__all__ = ["ClientTray", "ClientWindow", "run_tray", "setup_application", "tray_icon",
           "tray_pixmap", "window_bars"]
