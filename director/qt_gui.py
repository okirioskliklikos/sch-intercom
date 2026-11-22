"""Κεντρική οθόνη Director σε PySide6 (docs/ui-design.md, docs/ui-style.md).

Μόνο εμφάνιση και σύνδεση σημάτων. Η συμπεριφορά βρίσκεται στο
`director/gui.py` (χωρίς Qt) και τα χρώματα στο `shared/ui/theme.py`.
Τα widgets ενημερώνονται μόνο από το GUI thread, με QTimer.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import socket
import threading
import time
from collections.abc import Callable

from PySide6.QtCore import (QElapsedTimer, QPointF, QRectF, QSize, Qt, QTimer,
                            QVariantAnimation)
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (QAbstractButton, QApplication, QButtonGroup, QCheckBox,
                               QFrame, QGridLayout, QHBoxLayout, QLabel, QMainWindow,
                               QPushButton, QRadioButton, QScrollArea, QSizePolicy,
                               QStackedWidget, QVBoxLayout, QWidget)

from director.broadcast import AudioBroadcaster, MAX_CLIENTS
from director.gui import (MODE_ALL, MODE_ROOMS, DirectorMicrophone, choose_targets,
                          meter_fraction, room_grid_shape,
                          room_states)
from shared.discovery import announce, director_id
from shared.ui import icons, theme

WINDOW_TITLE = "School Intercom — Διεύθυνση"
PAGE_TITLE = "Κεντρική λειτουργία"
REFRESH_MS = 100
NOTICE_SECONDS = 4.0

HINT_IDLE = "Πατήστε για έναρξη"
HINT_LIVE = "Πατήστε για λήξη"
HINT_LIVE_MUTED = "Σίγαση · πατήστε για λήξη"
LIVE_BADGE = "Live"
HINT_MUTED = "Σίγαση ενεργή · άρση σίγασης για έναρξη"
HINT_PREVIEW = "Προεπισκόπηση · δεν μεταδίδεται"

# Μενού: (κλειδί εικονιδίου, κείμενο), σειρά ui-style.md §4.
# Υλοποιημένες: Κεντρική και Ασφάλεια (η δεύτερη μόνο όταν δοθεί μοντέλο ασφάλειας).
NAV_ITEMS = [
    ("home", "Κεντρική"),
    ("rooms", "Αίθουσες"),
    ("announcements", "Ανακοινώσεις"),
    ("scenarios", "Σενάρια"),
    ("logs", "Καταγραφές"),
    ("security", "Ασφάλεια"),
    ("settings", "Ρυθμίσεις"),
]
#: Η σελίδα «Ασφάλεια» διαβάζει μητρώο/αιτήματα το πολύ τόσο συχνά.
SECURITY_REFRESH_SECONDS = 1.0


_metrics = theme.metrics_for(None)


def px(mockup_px: float) -> int:
    """Διάσταση του mockup στην κλίμακα της εφαρμογής (βλ. theme.Metrics)."""
    return _metrics.px(mockup_px)


def _repolish(widget: QWidget, **properties) -> None:
    changed = False
    for name, value in properties.items():
        if widget.property(name) != value:
            widget.setProperty(name, value)
            changed = True
    if changed:
        widget.style().unpolish(widget)
        widget.style().polish(widget)


class AnnounceButton(QAbstractButton):
    """Μεγάλος κυκλικός διακόπτης ανακοίνωσης (ui-style.md §4)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.RING = px(theme.ANNOUNCE_RING)
        self.CIRCLE = px(theme.ANNOUNCE_CIRCLE)
        self.WAVE_PEN = px(theme.WAVE_WIDTH)
        self.live = False
        self.muted = False
        # 0 = σταθερό δαχτυλίδι, 1 = κύματα· ενδιάμεσες τιμές στη μετάβαση.
        self.waves_mix = 0.0
        self._mix_anim = QVariantAnimation(self)
        self._mix_anim.setDuration(theme.WAVE_FADE_MS)
        self._mix_anim.valueChanged.connect(self._set_mix)
        self._mix_anim.finished.connect(self._mix_finished)
        self._clock = QElapsedTimer()
        self._frame = QTimer(self)
        self._frame.setInterval(theme.WAVE_FRAME_MS)
        self._frame.timeout.connect(self.update)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Χώρος δεσμευμένος για το μέγιστο κύμα, ώστε η διάταξη να μη μετακινείται.
        wave_max = math.ceil(self.CIRCLE * theme.WAVE_SCALE) + self.WAVE_PEN
        side = max(self.RING, wave_max) + 4
        self.setFixedSize(side, side)
        self.setAccessibleName("Ανακοίνωση")

    def set_live(self, live: bool) -> None:
        if live != self.live:
            self.live = live
            self._update_waves()
            self.update()

    def set_muted(self, muted: bool) -> None:
        """Σίγαση: λευκό μικρόφωνο με διαγώνια γραμμή (ui-style.md §4)."""
        if muted != self.muted:
            self.muted = muted
            self._update_waves()
            self.update()

    def waves_wanted(self) -> bool:
        """Κύματα μόνο σε ζωντανή ανακοίνωση χωρίς σίγαση (ui-style.md §4)."""
        return self.live and not self.muted and self.isEnabled()

    def waves_running(self) -> bool:
        return self._frame.isActive()

    def _update_waves(self) -> None:
        target = 1.0 if self.waves_wanted() else 0.0
        current_end = self._mix_anim.endValue()
        if self._mix_anim.state() == QVariantAnimation.State.Running:
            if current_end == target:
                return
            self._mix_anim.stop()
        elif self.waves_mix == target:
            return
        if target and not self._frame.isActive():
            self._clock.start()
            self._frame.start()
        self._mix_anim.setStartValue(self.waves_mix)
        self._mix_anim.setEndValue(target)
        self._mix_anim.start()

    def _set_mix(self, value) -> None:
        self.waves_mix = float(value)
        self.update()

    def _mix_finished(self) -> None:
        if self.waves_mix <= 0.0:
            self._frame.stop()
            self.update()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == event.Type.EnabledChange:
            self._update_waves()

    def hideEvent(self, event) -> None:
        self._frame.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.waves_mix > 0.0 and not self._frame.isActive():
            self._clock.start()
            self._frame.start()

    def sizeHint(self) -> QSize:
        return self.size()

    def wave_states(self, elapsed_ms: int) -> list[tuple[float, float]]:
        """(ακτίνα, opacity) κάθε ενεργού κύματος μετά από elapsed_ms."""
        start = self.CIRCLE / 2
        grow = self.CIRCLE * theme.WAVE_SCALE / 2 - start
        waves = []
        for index in range(theme.WAVE_COUNT):
            local = elapsed_ms - index * theme.WAVE_DELAY_MS
            if local < 0:
                continue
            progress = (local % theme.WAVE_PERIOD_MS) / theme.WAVE_PERIOD_MS
            eased = 1 - (1 - progress) ** 3          # ease-out
            waves.append((start + grow * eased, theme.WAVE_OPACITY * (1 - eased)))
        return waves

    def paintEvent(self, _event) -> None:
        if not self.isEnabled():
            fill, ring = theme.DISABLED_FILL, theme.DISABLED_RING
        elif self.live:
            fill, ring = theme.LIVE, theme.LIVE_LIGHT
        else:
            fill, ring = theme.PRIMARY, theme.PRIMARY_LIGHT
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        center = QPointF(self.width() / 2, self.height() / 2)
        mix = self.waves_mix
        if mix < 1.0:
            ring_color = QColor(ring)
            ring_color.setAlphaF(1.0 - mix)
            painter.setBrush(ring_color)
            painter.drawEllipse(center, self.RING / 2, self.RING / 2)
        if mix > 0.0 and self._frame.isActive():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for radius, opacity in self.wave_states(self._clock.elapsed()):
                color = QColor(theme.LIVE)
                color.setAlphaF(opacity * mix)
                painter.setPen(QPen(color, self.WAVE_PEN))
                painter.drawEllipse(center, radius, radius)
            painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(fill))
        painter.drawEllipse(center, self.CIRCLE / 2, self.CIRCLE / 2)
        size = px(theme.ANNOUNCE_ICON)
        icons.render(painter, "mic_off" if self.muted else "mic", theme.WHITE,
                     QRectF(center.x() - size / 2, center.y() - size / 2, size, size), 2.2)
        painter.end()


class LevelMeter(QWidget):
    """Σειρά στρογγυλεμένων τμημάτων· ενεργά σε `online`."""

    SEGMENTS = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self.GAP = px(theme.METER_GAP)
        self.BAR = px(theme.METER_BAR)
        self.fraction = 0.0
        self.setFixedHeight(self.BAR)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_fraction(self, fraction: float) -> None:
        fraction = max(0.0, min(1.0, fraction))
        if abs(fraction - self.fraction) > 1e-3:
            self.fraction = fraction
            self.update()

    def lit_segments(self) -> int:
        return round(self.fraction * self.SEGMENTS)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        width = (self.width() - self.GAP * (self.SEGMENTS - 1)) / self.SEGMENTS
        lit = self.lit_segments()
        for index in range(self.SEGMENTS):
            painter.setBrush(QColor(theme.METER_ON if index < lit else theme.METER_OFF))
            x = index * (width + self.GAP)
            painter.drawRoundedRect(QRectF(x, 0, width, self.BAR), self.BAR / 2, self.BAR / 2)
        painter.end()


class StatusDot(QWidget):
    def __init__(self, color: str, size: int | None = None, parent=None):
        super().__init__(parent)
        size = size or px(theme.DOT)
        self.color = color
        self.setFixedSize(size, size)

    def set_color(self, color: str) -> None:
        if color != self.color:
            self.color = color
            self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self.color))
        painter.drawEllipse(self.rect())
        painter.end()


class ElidedLabel(QLabel):
    """Κείμενο που κόβεται με «…» όταν δεν χωρά στο πλάτος (tooltip το πλήρες)."""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.full_text = text
        self.setToolTip(text)
        self.setMinimumWidth(1)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (event.Type.FontChange, event.Type.StyleChange):
            self._elide()

    def _elide(self) -> None:
        text = self.fontMetrics().elidedText(
            self.full_text, Qt.TextElideMode.ElideRight, self.contentsRect().width())
        if text != self.text():
            self.setText(text)

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        return QSize(self.fontMetrics().horizontalAdvance(self.full_text) + 2, hint.height())


class RoomRow(QWidget):
    """Γραμμή αίθουσας ~30px: checkbox, όνομα, κουκκίδα κατάστασης δεξιά."""

    def __init__(self, room: str, on_toggled: Callable[[str, bool], None], parent=None):
        super().__init__(parent)
        self.room = room
        self.setObjectName("roomRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(px(theme.ROW_HEIGHT))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(px(2), 0, px(2), 0)
        layout.setSpacing(px(9))
        self.checkbox = QCheckBox()
        self.checkbox.setFixedWidth(px(18))  # χωρίς κείμενο: όχι κενό spacing
        self.checkbox.setAccessibleName(room)
        # Το όνομα κόβεται με «…» μόνο όταν φτάνει στην κουκκίδα (ui-style.md §5).
        self.label = ElidedLabel(room)
        self.label.setObjectName("roomName")
        self.dot = StatusDot(theme.ROOM_DOT_OFFLINE)
        layout.addWidget(self.checkbox)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.dot)
        self.checkbox.toggled.connect(lambda checked: on_toggled(room, checked))

    def mousePressEvent(self, event) -> None:
        if self.checkbox.isEnabled() and event.button() == Qt.MouseButton.LeftButton:
            self.checkbox.toggle()
        super().mousePressEvent(event)

    def set_state(self, *, checked: bool, online: bool, enabled: bool) -> None:
        if self.checkbox.isChecked() != checked:
            self.checkbox.blockSignals(True)
            self.checkbox.setChecked(checked)
            self.checkbox.blockSignals(False)
        self.checkbox.setEnabled(enabled)
        self.dot.set_color(theme.ROOM_DOT_ONLINE if online else theme.ROOM_DOT_OFFLINE)
        self.dot.setToolTip("Online" if online else "Κανένας Client online")


class _ResizeNotifier(QWidget):
    def __init__(self, on_resize: Callable[[], None], parent=None):
        super().__init__(parent)
        self._on_resize = on_resize

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._on_resize()


class RoomsSplitHandle(QWidget):
    """Λαβή ανάμεσα στην «Ανακοίνωση» και τις αίθουσες (απόφαση χρήστη 22/11/2026).

    Πιάνει το κενό ανάμεσα στις κάρτες· τρεις κάθετες τελείες στο κέντρο. Σύρσιμο
    αριστερά δίνει πλάτος στις αίθουσες, διπλό κλικ επαναφέρει την προεπιλογή.
    """

    def __init__(self, on_drag: Callable[[int], None], on_release: Callable[[], None],
                 on_reset: Callable[[], None], parent=None):
        super().__init__(parent)
        self.setObjectName("roomsSplitHandle")
        self._on_drag, self._on_release, self._on_reset = on_drag, on_release, on_reset
        self._start: float | None = None
        self._hover = False
        self.setFixedWidth(px(theme.CARD_SPACING))
        self.setCursor(Qt.CursorShape.SplitHCursor)
        self.setToolTip("Σύρετε για πλάτος αιθουσών · διπλό κλικ: προεπιλογή")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._start = event.globalPosition().x()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._start is not None:
            # Προς τα αριστερά = περισσότερο πλάτος για τις αίθουσες.
            self._on_drag(round(self._start - event.globalPosition().x()))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._start is not None:
            self._start = None
            self._on_release()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        self._start = None
        self._on_reset()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.TEXT_MUTED if self._hover else theme.CONTROL_BORDER))
        radius = max(1.5, px(theme.SPLIT_DOT) / 2)
        gap = px(theme.SPLIT_DOT_GAP)
        cx, cy = self.width() / 2, self.height() / 2
        for offset in (-gap, 0, gap):
            painter.drawEllipse(QPointF(cx, cy + offset), radius, radius)
        painter.end()


def load_layout_settings(path: Path | None) -> dict:
    """Ρυθμίσεις εμφάνισης παραθύρου (π.χ. πλάτος αιθουσών)· άγνωστο αρχείο = προεπιλογές."""
    if path is None:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != 1:
        return {}
    extra = data.get("rooms_extra_width")
    if type(extra) not in (int, float) or not 0 <= extra <= 10000:
        return {}
    return {"rooms_extra_width": float(extra)}


def save_layout_settings(path: Path | None, settings: dict) -> None:
    if path is None:
        return
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({"version": 1, **settings}), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # η εμφάνιση δεν σταματά τον Director


def _card(title: QWidget | str) -> tuple[QFrame, QVBoxLayout]:
    card = QFrame()
    card.setObjectName("card")
    layout = QVBoxLayout(card)
    pad = px(theme.CARD_PADDING)
    layout.setContentsMargins(pad + px(2), pad, pad + px(2), pad)
    layout.setSpacing(px(8))
    if isinstance(title, str):
        label = QLabel(title)
        label.setObjectName("cardTitle")
        layout.addWidget(label)
    else:
        layout.addWidget(title)
    return card, layout


class DirectorWindow(QMainWindow):
    def __init__(self, broadcaster, microphone: DirectorMicrophone, *,
                 server_up: Callable[[], bool] = lambda: True,
                 clock: Callable[[], float] = time.monotonic,
                 security=None, security_options: dict | None = None,
                 layout_settings: Path | None = None):
        super().__init__()
        # Επιπλέον πλάτος αιθουσών από τη λαβή, σε px του mockup (ανεξάρτητο κλίμακας).
        self._layout_settings = layout_settings
        self._rooms_extra = load_layout_settings(layout_settings).get("rooms_extra_width", 0.0)
        self._rooms_extra_max = 0
        self._drag_start_extra: int | None = None
        self._layout_pending = False
        self.broadcaster = broadcaster
        self.security = security
        self.security_page = None
        self._security_options = security_options or {}
        self._security_refreshed = -math.inf
        self.mic = microphone
        self.server_up = server_up
        self.clock = clock
        self.mode = MODE_ALL
        self.checked: set[str] = set()
        self.rooms: list[tuple[str, bool]] = []
        self.room_rows: dict[str, RoomRow] = {}
        self._room_rows_per_column: int | None = None
        self._room_columns = 0
        self._room_shape: tuple[int, int] | None = None
        self.page: QWidget | None = None
        self.notice: tuple[str, float] | None = None

        self.setWindowTitle(WINDOW_TITLE)
        self.setWindowIcon(icons.app_icon())
        self._fit_to_screen(QSize(px(960), px(680)), QSize(px(860), px(600)))

        root = QWidget()
        root.setObjectName("page")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_sidebar())
        outer.addWidget(self._build_content(), 1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(REFRESH_MS)
        self.refresh()

    def _fit_to_screen(self, size: QSize, minimum: QSize) -> None:
        """Με μεγάλη γραμματοσειρά σε οθόνη 1366×768 το παράθυρο πρέπει να χωρά."""
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry().size()
            minimum = minimum.boundedTo(available)
            size = size.boundedTo(available)
        self.setMinimumSize(minimum)
        self.resize(size)

    # --- κατασκευή ---------------------------------------------------------
    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(px(theme.SIDEBAR_WIDTH))
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(px(10), px(14), px(10), px(14))
        layout.setSpacing(px(4))

        brand = QHBoxLayout()
        brand.setContentsMargins(px(4), px(2), px(4), px(14))
        brand.setSpacing(px(8))
        logo = QLabel()
        logo.setPixmap(icons.app_pixmap(px(theme.BRAND_ICON)))
        name = QLabel("School Intercom")
        name.setObjectName("brand")
        brand.addWidget(logo)
        brand.addWidget(name, 1)
        layout.addLayout(brand)

        self.nav_buttons: dict[str, QPushButton] = {}
        for key, text in NAV_ITEMS:
            button = QPushButton(text)
            button.setObjectName("navItem")
            button.setCheckable(True)
            button.setIcon(icons.icon(key, theme.TEXT, selected_color=theme.PRIMARY,
                                      disabled_color=theme.TEXT_MUTED, size=px(theme.ICON)))
            button.setIconSize(QSize(px(theme.ICON), px(theme.ICON)))
            if key == "home" or (key == "security" and self.security is not None):
                button.setChecked(key == "home")
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.clicked.connect(lambda _=False, k=key: self.show_page(k))
            else:
                # Χωρίς προσποίηση μη διαθέσιμων λειτουργιών (ui-design.md).
                button.setEnabled(False)
                button.setToolTip("Δεν είναι ακόμη διαθέσιμο")
            self.nav_buttons[key] = button
            layout.addWidget(button)
        layout.addStretch(1)
        return sidebar

    def _build_content(self) -> QWidget:
        """Κοινή κεφαλίδα (τίτλος, «● Συνδεδεμένος») και οι σελίδες του μενού."""
        content = QWidget()
        content.setObjectName("page")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(px(18), px(14), px(18), px(16))
        layout.setSpacing(px(theme.CARD_SPACING))

        header = QHBoxLayout()
        self.page_title = QLabel(PAGE_TITLE)
        self.page_title.setObjectName("pageTitle")
        header.addWidget(self.page_title)
        header.addStretch(1)
        self.connection_dot = StatusDot(theme.CONNECTED)
        self.connection_label = QLabel("Συνδεδεμένος")
        self.connection_label.setObjectName("connection")
        header.setSpacing(px(6))
        # «● Συνδεδεμένος»: η κουκκίδα πριν από τη λέξη (ui-style.md §4, 22/11/2026).
        header.addWidget(self.connection_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.connection_label)
        layout.addLayout(header)

        self.pages = QStackedWidget()
        self.page_keys = ["home"]
        self.pages.addWidget(self._build_page())
        if self.security is not None:
            from director.qt_security import SecurityPage
            self.security_page = SecurityPage(self.security, **self._security_options)
            self.pages.addWidget(self.security_page)
            self.page_keys.append("security")
        layout.addWidget(self.pages, 1)
        return content

    def show_page(self, key: str) -> None:
        """Εναλλαγή σελίδας από το μενού· η τρέχουσα μένει επιλεγμένη."""
        if key not in self.page_keys:
            return
        for name, button in self.nav_buttons.items():
            button.setChecked(name == key)
        self.pages.setCurrentIndex(self.page_keys.index(key))
        if key == "security":
            from director.qt_security import PAGE_TITLE as SECURITY_TITLE
            self.page_title.setText(SECURITY_TITLE)
            self._security_refreshed = -math.inf
        else:
            self.page_title.setText(PAGE_TITLE)
        self.refresh()

    @property
    def current_page(self) -> str:
        return self.page_keys[self.pages.currentIndex()]

    def _build_page(self) -> QWidget:
        # Αλλαγή πλάτους σελίδας μπορεί να χωρά ή να αφαιρεί στήλη αιθουσών.
        page = _ResizeNotifier(self._request_layout)
        page.setObjectName("page")
        self.page = page
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Δίπλα-δίπλα: «Ανακοίνωση» αριστερά, «Αίθουσες» και «Επιλογή αιθουσών» δεξιά,
        # με ίδιο ύψος στην επάνω σειρά. Όταν οι αίθουσες δεν χωρούν δίπλα στην
        # «Ανακοίνωση», η μία κάτω από την άλλη με κύλιση σελίδας (ui-style.md §5).
        body = QWidget()
        body.setObjectName("page")
        columns = QGridLayout(body)
        self.main_grid = columns
        self.stacked = False
        # Κενό ανάμεσα στις κάρτες και τη γραμμή κύλισης (ui-style.md §2).
        columns.setContentsMargins(0, 0, px(theme.SCROLL_GAP), 0)
        # Το οριζόντιο κενό ανάμεσα στις στήλες το πιάνει η λαβή (στήλη 1).
        columns.setHorizontalSpacing(0)
        columns.setVerticalSpacing(px(theme.CARD_SPACING))
        columns.setColumnStretch(0, 1)
        columns.setColumnStretch(1, 0)
        columns.setColumnStretch(2, 0)
        columns.setRowStretch(0, 1)
        columns.setRowStretch(1, 0)

        self.announcement_card = self._build_announcement_card()
        self.announcement_card.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        columns.addWidget(self.announcement_card, 0, 0)

        self.rooms_area = _ResizeNotifier(self._request_layout)
        self.rooms_area.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        right_top = QVBoxLayout(self.rooms_area)
        right_top.setContentsMargins(0, 0, 0, 0)
        right_top.setSpacing(0)
        self.rooms_card = self._build_rooms_card()
        self.rooms_card.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        right_top.addWidget(self.rooms_card)
        columns.addWidget(self.rooms_area, 0, 2)
        self.split_handle = RoomsSplitHandle(self._drag_rooms, self._save_rooms_extra,
                                             self._reset_rooms_extra)
        columns.addWidget(self.split_handle, 0, 1, 2, 1)

        self.selection_card = self._build_selection_card()
        self.selection_card.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        columns.addWidget(self.selection_card, 1, 2)

        self.page_scroll = QScrollArea()
        self.page_scroll.setObjectName("pageScroll")
        self.page_scroll.setWidgetResizable(True)
        self.page_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.page_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.page_scroll.setWidget(body)
        layout.addWidget(self.page_scroll, 1)
        return page

    def _set_stacked(self, stacked: bool) -> None:
        """Αλλαγή διάταξης: δίπλα-δίπλα ή «Ανακοίνωση» πάνω, αίθουσες και επιλογή κάτω."""
        if stacked == self.stacked:
            return
        self.stacked = stacked
        grid = self.main_grid
        cards = (self.announcement_card, self.rooms_area, self.selection_card)
        for widget in cards:
            grid.removeWidget(widget)
        free = 16777215  # QWIDGETSIZE_MAX: χωρίς σταθερό πλάτος
        if stacked:
            grid.setHorizontalSpacing(px(theme.CARD_SPACING))
            self.split_handle.hide()
            grid.addWidget(self.announcement_card, 0, 0)
            grid.addWidget(self.rooms_area, 1, 0)
            grid.addWidget(self.selection_card, 2, 0)
            for row, stretch in enumerate((0, 0, 0, 1)):
                grid.setRowStretch(row, stretch)
            for widget in (self.rooms_area, self.selection_card):
                widget.setMinimumWidth(0)
                widget.setMaximumWidth(free)
            self.rooms_area.setSizePolicy(QSizePolicy.Policy.Preferred,
                                          QSizePolicy.Policy.Preferred)
        else:
            grid.setHorizontalSpacing(0)
            grid.addWidget(self.announcement_card, 0, 0)
            grid.addWidget(self.rooms_area, 0, 2)
            grid.addWidget(self.selection_card, 1, 2)
            for row, stretch in enumerate((1, 0, 0, 0)):
                grid.setRowStretch(row, stretch)
            self.rooms_area.setSizePolicy(QSizePolicy.Policy.Preferred,
                                          QSizePolicy.Policy.Expanding)
        self._room_shape = None

    def _build_announcement_card(self) -> QWidget:
        # Τίτλος αριστερά, στάθμη και προεπισκόπηση πάνω δεξιά.
        title_row = QWidget()
        title_layout = QHBoxLayout(title_row)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(px(8))
        title = QLabel("Ανακοίνωση")
        title.setObjectName("cardTitle")
        title_layout.addWidget(title, 0, Qt.AlignmentFlag.AlignTop)
        title_layout.addStretch(1)

        preview_panel = QWidget()
        preview_panel.setFixedWidth(px(240))
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(px(7))
        level_row = QHBoxLayout()
        level_row.setContentsMargins(0, 0, 0, 0)
        level_label = QLabel("Στάθμη μικροφώνου")
        level_label.setObjectName("muted")
        level_row.addWidget(level_label)
        level_row.addStretch(1)
        self.preview_button = QPushButton("Προεπισκόπηση")
        self.preview_button.setObjectName("pillSmall")
        self.preview_button.setCheckable(True)
        self.preview_button.setToolTip("Τοπική μέτρηση στάθμης, χωρίς μετάδοση")
        self.preview_button.toggled.connect(self.mic_preview_toggled)
        level_row.addWidget(self.preview_button)
        preview_layout.addLayout(level_row)
        self.meter = LevelMeter()
        preview_layout.addWidget(self.meter)
        # Τουλάχιστον 240px του mockup, αλλά ποτέ κομμένο «Στάθμη μικροφώνου» με
        # μεγάλη γραμματοσειρά.
        for widget in (level_label, self.preview_button):
            widget.ensurePolished()
        preview_panel.setFixedWidth(max(
            px(240), level_label.sizeHint().width() + level_row.spacing()
            + self.preview_button.sizeHint().width() + px(4)))
        title_layout.addWidget(preview_panel, 0, Qt.AlignmentFlag.AlignTop)
        card, layout = _card(title_row)
        # Η ομάδα χειριστηρίων διατηρεί το σημερινό μέγεθος και
        # κεντράρεται μέσα στην επεκτεινόμενη κάρτα.
        controls = QWidget()
        controls.setFixedWidth(
            px(theme.LEFT_COLUMN_WIDTH) - 2 * (px(theme.CARD_PADDING) + px(2)))
        group = QVBoxLayout(controls)
        group.setContentsMargins(0, 0, 0, 0)
        group.setSpacing(px(8))

        # «Live ●» πάνω από τον κύκλο μόνο σε ζωντανή ανακοίνωση. Ο χώρος
        # μένει δεσμευμένος και σε αναμονή, ώστε ο κύκλος να μη μετακινείται.
        self.live_badge = QWidget()
        badge = QHBoxLayout(self.live_badge)
        badge.setContentsMargins(0, 0, 0, 0)
        badge.setSpacing(px(6))
        self.live_label = QLabel(LIVE_BADGE)
        self.live_label.setObjectName("liveBadge")
        self.live_dot = StatusDot(theme.LIVE)
        badge.addStretch(1)
        badge.addWidget(self.live_label)
        badge.addWidget(self.live_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        badge.addStretch(1)
        self.live_badge.setFixedHeight(self.live_badge.sizeHint().height())
        policy = self.live_badge.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.live_badge.setSizePolicy(policy)
        self.live_badge.setVisible(False)
        group.addWidget(self.live_badge)

        self.announce_button = AnnounceButton()
        self.announce_button.clicked.connect(self.toggle_announcement)
        group.addWidget(self.announce_button, 0, Qt.AlignmentFlag.AlignHCenter)

        self.hint = QLabel(HINT_IDLE)
        self.hint.setObjectName("announceHint")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setWordWrap(True)
        group.addWidget(self.hint)
        # Η σίγαση παραμένει κάτω από τον κύκλο, λίγο πιο κοντά του.
        group.addSpacing(px(1))

        self.mute_button = QPushButton()
        self.mute_button.setObjectName("pill")
        self.mute_button.setCheckable(True)
        self.mute_button.setIconSize(QSize(px(theme.ICON), px(theme.ICON)))
        self.mute_button.toggled.connect(self.mute_toggled)
        group.addWidget(self.mute_button, 0, Qt.AlignmentFlag.AlignHCenter)
        self._update_mute_button()
        layout.addStretch(1)
        layout.addWidget(controls, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        return card

    def _build_selection_card(self) -> QWidget:
        card, layout = _card("Επιλογή αιθουσών")
        layout.setSpacing(px(6))
        self.radio_all = QRadioButton()
        self.radio_rooms = QRadioButton("Συγκεκριμένες αίθουσες")
        self.radio_group = QRadioButton("Ομάδα / Σενάριο")
        self.radio_group.setEnabled(False)
        self.radio_group.setToolTip("Δεν είναι ακόμη διαθέσιμο")
        self.radio_all.setChecked(True)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.radio_all)
        self.mode_group.addButton(self.radio_rooms)
        self.mode_group.addButton(self.radio_group)
        self.radio_all.toggled.connect(lambda on: on and self.set_mode(MODE_ALL))
        self.radio_rooms.toggled.connect(lambda on: on and self.set_mode(MODE_ROOMS))
        for radio in (self.radio_all, self.radio_rooms, self.radio_group):
            layout.addWidget(radio)
        return card

    def _build_rooms_card(self) -> QWidget:
        title_row = QWidget()
        title_layout = QHBoxLayout(title_row)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(px(5))
        title = QLabel("Αίθουσες")
        title.setObjectName("cardTitle")
        self.online_count = QLabel()
        self.online_count.setObjectName("onlineCount")
        title_layout.addWidget(title)
        title_layout.addWidget(self.online_count)
        title_layout.addStretch(1)
        card, layout = _card(title_row)
        card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.rooms_card, self.rooms_card_layout, self.rooms_title = card, layout, title_row

        self.empty_rooms = QLabel("Δεν έχει συνδεθεί ακόμη κανένας Client.")
        self.empty_rooms.setObjectName("muted")
        layout.addWidget(self.empty_rooms)

        self.room_grid_widget = QWidget()
        self.room_grid_widget.setObjectName("roomGrid")
        self.room_grid = QGridLayout(self.room_grid_widget)
        self.room_grid.setContentsMargins(0, 0, 0, 0)
        self.room_grid.setHorizontalSpacing(px(16))
        self.room_grid.setVerticalSpacing(0)
        self.room_scroll = QScrollArea()
        self.room_scroll.setWidgetResizable(False)
        self.room_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.room_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.room_scroll.setWidget(self.room_grid_widget)
        layout.addWidget(self.room_scroll)
        # Κρατά τον τίτλο και τις αίθουσες πάνω αριστερά,
        # ανεξάρτητα από το ύψος της κάρτας.
        layout.addStretch(1)
        return card

    # --- ενέργειες χρήστη --------------------------------------------------
    def toggle_announcement(self) -> None:
        if self.mic.live:
            self.mic.stop_announcement()
        else:
            try:
                rooms = choose_targets(self.mode, self.checked,
                                       self.broadcaster.status_snapshot())
                self.mic.start_announcement(rooms)
            except ValueError as exc:
                self.show_notice(str(exc))
            else:
                self.notice = None
                self.mic.error = None
        self.refresh()

    def mic_preview_toggled(self, enabled: bool) -> None:
        if enabled:
            self.mic.error = None
        self.mic.set_preview(enabled)
        self.refresh()

    def mute_toggled(self, muted: bool) -> None:
        self.mic.set_muted(muted)
        self.refresh()

    def set_mode(self, mode: str) -> None:
        if self.mic.live or mode == self.mode:
            return
        if mode == MODE_ROOMS and not self.checked:
            # Ξεκινά από τις online αίθουσες, όπως φαίνονταν στο «Όλες».
            self.checked = {room for room, online in self.rooms if online}
        self.mode = mode
        self.refresh()

    def room_toggled(self, room: str, checked: bool) -> None:
        if self.mic.live:
            return
        if self.mode == MODE_ALL:
            # Αλλαγή μίας αίθουσας σημαίνει «Συγκεκριμένες αίθουσες».
            self.checked = {name for name, _ in self.rooms}
            self.mode = MODE_ROOMS
            self.radio_rooms.blockSignals(True)
            self.radio_rooms.setChecked(True)
            self.radio_rooms.blockSignals(False)
        if checked:
            self.checked.add(room)
        else:
            self.checked.discard(room)
        self.refresh()

    def show_notice(self, message: str) -> None:
        self.notice = (message, self.clock())

    # --- ανανέωση ----------------------------------------------------------
    def refresh(self) -> None:
        error = self.mic.poll()
        if error:
            self.notice = None
        self.mic.clear_error_if_ready()
        self._refresh_rooms()
        self._refresh_announcement()
        if self.security_page is not None and self.current_page == "security":
            now = time.monotonic()
            if now - self._security_refreshed >= SECURITY_REFRESH_SECONDS:
                self._security_refreshed = now
                self.security_page.refresh()
        up = self.server_up()
        self.connection_dot.set_color(theme.CONNECTED if up else theme.LIVE)
        self.connection_label.setText("Συνδεδεμένος" if up else "Εκτός λειτουργίας")
        _repolish(self.connection_label, state="up" if up else "down")

    def _refresh_rooms(self) -> None:
        rooms = room_states(self.broadcaster.status_snapshot())
        names = [room for room, _ in rooms]
        online = sum(1 for _, is_online in rooms if is_online)
        self.online_count.setText(f"({online}/{len(rooms)} online)")
        self.online_count.setVisible(bool(rooms))
        self.empty_rooms.setVisible(not rooms)
        self.room_scroll.setVisible(bool(rooms))
        if names != [room for room, _ in self.rooms]:
            self._rebuild_room_rows(names)
        self.rooms = rooms
        self.radio_all.setText(f"Όλες οι αίθουσες ({online})")
        locked = self.mic.live
        for room, is_online in rooms:
            checked = self.mode == MODE_ALL or room in self.checked
            self.room_rows[room].set_state(checked=checked, online=is_online,
                                           enabled=not locked)
        for radio in (self.radio_all, self.radio_rooms):
            radio.setEnabled(not locked)

    def _rebuild_room_rows(self, names: list[str]) -> None:
        for row in self.room_rows.values():
            self.room_grid.removeWidget(row)
            row.deleteLater()
        self.room_rows = {}
        for room in names:
            self.room_rows[room] = RoomRow(room, self.room_toggled)
        # Οι επιλογές αιθουσών που χάθηκαν εντελώς δεν κρατιούνται.
        self.checked &= set(names)
        self._room_rows_per_column = None
        self._room_shape = None
        self._layout_rooms()

    def _room_column_width(self) -> int:
        """Σταθερό πλάτος στήλης αιθουσών, κλιμακωμένο (ui-style.md §5)."""
        return px(theme.ROOM_COLUMN_WIDTH)

    def _max_room_columns(self, column_width: int) -> int:
        """Πόσες στήλες αιθουσών χωρούν δίπλα στην «Ανακοίνωση» (0 = καμία)."""
        if self.page is None or not self.page.isVisible():
            return 2
        margins = self.page.layout().contentsMargins()
        card = self.rooms_card_layout.contentsMargins()
        announce = max(px(theme.ANNOUNCE_MIN_WIDTH),
                       self.announcement_card.minimumSizeHint().width())
        available = (self.page.width() - margins.left() - margins.right()
                     - px(theme.CARD_SPACING) - announce
                     - card.left() - card.right() - 2
                     - self._scroll_reserve())
        spacing = self.room_grid.horizontalSpacing()
        return max(0, (available + spacing) // (column_width + spacing))

    def _request_layout(self) -> None:
        """Αναδιάταξη μετά από αλλαγή μεγέθους, στον επόμενο κύκλο γεγονότων.

        Το resizeEvent έρχεται μέσα από το setGeometry του layout· αν εκεί αλλάξει
        η θέση των καρτών στο ίδιο grid (δίπλα-δίπλα ↔ η μία κάτω από την άλλη), το
        Qt μπορεί να καταρρεύσει (segfault όταν στενεύει και χαμηλώνει μαζί το
        παράθυρο). Γι' αυτό η αναδιάταξη γίνεται αμέσως μετά, μία φορά.
        """
        if not self._layout_pending:
            self._layout_pending = True
            QTimer.singleShot(0, self._run_requested_layout)

    def _run_requested_layout(self) -> None:
        self._layout_pending = False
        if self.page is not None:
            self._layout_rooms()

    @staticmethod
    def _scroll_reserve() -> int:
        """Χώρος για τη γραμμή κύλισης της σελίδας και το κενό πριν από αυτή."""
        return px(theme.SCROLLBAR) + px(6) + px(theme.SCROLL_GAP)

    def _announce_min_width(self) -> int:
        return max(px(theme.ANNOUNCE_MIN_WIDTH),
                   self.announcement_card.minimumSizeHint().width())

    def _empty_side_by_side_fits(self) -> bool:
        """Χωρούν «Ανακοίνωση» και άδεια κάρτα «Αίθουσες» δίπλα-δίπλα;"""
        if self.page is None or not self.page.isVisible():
            return True
        margins = self.page.layout().contentsMargins()
        available = (self.page.width() - margins.left() - margins.right()
                     - self._scroll_reserve())
        needed = (self._announce_min_width() + px(theme.CARD_SPACING)
                  + self._right_column_width())
        return needed <= available

    def _stacked_min_width(self) -> int:
        """Ελάχιστο πλάτος της σελίδας ώστε η διάταξη «η μία κάτω από την άλλη» να χωρά."""
        card = self.rooms_card_layout.contentsMargins()
        one_column = px(theme.ROOM_COLUMN_WIDTH) + card.left() + card.right() + 2
        return (max(self._announce_min_width(), one_column,
                    self.selection_card.minimumSizeHint().width(),
                    self.rooms_title.sizeHint().width() + card.left() + card.right() + 2,
                    self.empty_rooms.sizeHint().width() + card.left() + card.right() + 2)
                + self._scroll_reserve())

    def _update_page_minimum(self) -> None:
        """Το παράθυρο δεν στενεύει τόσο ώστε να κόβονται κάρτες δεξιά."""
        minimum = self._stacked_min_width()
        if self.page_scroll.minimumWidth() != minimum:
            self.page_scroll.setMinimumWidth(minimum)
        # Το ρητό ελάχιστο μέγεθος του παραθύρου υπερισχύει του layout: το ανεβάζουμε.
        needed = self.centralWidget().minimumSizeHint().width()
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            needed = min(needed, screen.availableGeometry().width())
        if self.minimumWidth() < needed:
            self.setMinimumWidth(needed)

    def _side_by_side_fits(self, count: int, column_width: int, chrome: int) -> bool:
        """Χωρούν όλες οι αίθουσες, χωρίς κύλιση, δίπλα στην «Ανακοίνωση»;

        Υπολογίζεται από το μέγεθος της σελίδας και όχι από την τρέχουσα διάταξη,
        ώστε η επιλογή να μην αλλάζει μόνο επειδή άλλαξε η διάταξη.
        """
        if self.page is None or not self.page.isVisible():
            return True
        max_columns = self._max_room_columns(column_width)
        if max_columns < 1:
            return False
        height = (self.page.height() - self.selection_card.sizeHint().height()
                  - px(theme.CARD_SPACING) - chrome)
        rows_fit = max(1, height // px(theme.ROW_HEIGHT))
        return math.ceil(count / rows_fit) <= max_columns

    def _layout_rooms(self) -> None:
        """Κάθε στήλη ως το διαθέσιμο ύψος, μετά η επόμενη όσο χωρά (ui-style.md §5).

        Αν δεν χωρούν δίπλα στην «Ανακοίνωση», η «Ανακοίνωση» πάει πάνω και οι
        «Αίθουσες» με την «Επιλογή αιθουσών» από κάτω, σε όσες στήλες χωρά το πλάτος
        (απόφαση χρήστη 22/11/2026).
        """
        self._update_page_minimum()
        if not self.room_rows:
            # Χωρίς αίθουσες: δίπλα-δίπλα μόνο αν χωρά και το κείμενο «Δεν έχει συνδεθεί…».
            self._set_stacked(not self._empty_side_by_side_fits())
            self.split_handle.hide()  # χωρίς αίθουσες δεν υπάρχει τι να φαρδύνει
            if not self.stacked:
                self._fix_right_column_width()
            return
        row_height = px(theme.ROW_HEIGHT)
        margins = self.rooms_card_layout.contentsMargins()
        chrome = (margins.top() + margins.bottom() + self.rooms_card_layout.spacing()
                  + self.rooms_title.sizeHint().height() + 2)
        names = list(self.room_rows)
        width = self._room_column_width()
        spacing = self.room_grid.horizontalSpacing()
        self._set_stacked(not self._side_by_side_fits(len(names), width, chrome))
        if self.stacked:
            inner = (self.page.width() - self._scroll_reserve()
                     - margins.left() - margins.right() - 2)
            columns = max(1, (inner + spacing) // (width + spacing))
            rows = math.ceil(len(names) / columns)
            rows_fit = rows
            rows, columns = rows, math.ceil(len(names) / rows)
            # Όλο το πλάτος της κάρτας στις στήλες, ώστε τα ονόματα να μην κόβονται άσκοπα.
            width = max(width, (inner - spacing * (columns - 1)) // columns)
        else:
            available = self.rooms_area.height() - chrome
            rows_fit = max(1, available // row_height)
            rows, columns = room_grid_shape(len(names), rows_fit,
                                            max(1, self._max_room_columns(width)))
        self._room_columns = columns
        if (rows, columns) != self._room_shape:
            self._room_shape = (rows, columns)
            self._room_rows_per_column = rows
            for row in self.room_rows.values():
                self.room_grid.removeWidget(row)
            for index, room in enumerate(names):
                self.room_grid.addWidget(self.room_rows[room], index % rows, index // rows)
        scrolls = rows > rows_fit
        if not self.stacked:
            width += self._room_extra_per_column(columns, width, scrolls)
        for row in self.room_rows.values():
            row.setFixedWidth(width)
        scrollbar = px(theme.SCROLLBAR) + px(6) if scrolls else 0
        self.room_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded if scrolls else
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        grid_width = width * columns + self.room_grid.horizontalSpacing() * (columns - 1)
        self.room_grid_widget.setFixedSize(grid_width, rows * row_height)
        self.room_scroll.setFixedSize(grid_width + scrollbar, min(rows, rows_fit) * row_height)
        self.split_handle.setVisible(not self.stacked)
        if self.stacked:
            return
        self._fix_right_column_width()

    def _room_extra_per_column(self, columns: int, width: int, scrolls: bool) -> int:
        """Πλάτος που πρόσθεσε η λαβή, μοιρασμένο στις στήλες αιθουσών.

        Ποτέ τόσο ώστε η «Ανακοίνωση» να πέσει κάτω από το ελάχιστο πλάτος της.
        """
        if self.page is None:
            return 0
        margins = self.page.layout().contentsMargins()
        card = self.rooms_card_layout.contentsMargins()
        spacing = self.room_grid.horizontalSpacing()
        scrollbar = px(theme.SCROLLBAR) + px(6) if scrolls else 0
        auto = (columns * width + spacing * (columns - 1) + scrollbar
                + card.left() + card.right() + 2)
        auto = max(auto, self.selection_card.minimumSizeHint().width())
        available = (self.page.width() - margins.left() - margins.right()
                     - px(theme.CARD_SPACING) - self._announce_min_width()
                     - self._scroll_reserve())
        self._rooms_extra_max = max(0, available - auto)
        extra = min(self._rooms_extra_px(), self._rooms_extra_max)
        return max(0, extra) // max(1, columns)

    @property
    def rooms_extra_width(self) -> int:
        """Επιπλέον πλάτος αιθουσών σε πραγματικά pixel (για δοκιμές)."""
        return min(self._rooms_extra_px(), self._rooms_extra_max)

    def _rooms_extra_px(self) -> int:
        return px(self._rooms_extra) if self._rooms_extra > 0 else 0

    def _scale(self) -> float:
        return px(1000) / 1000

    def _drag_rooms(self, delta: int) -> None:
        if self._drag_start_extra is None:
            self._drag_start_extra = self.rooms_extra_width
        real = max(0, min(self._drag_start_extra + delta, self._rooms_extra_max))
        self._rooms_extra = real / self._scale()
        self._room_shape = None
        self._layout_rooms()

    def _save_rooms_extra(self) -> None:
        self._drag_start_extra = None
        save_layout_settings(self._layout_settings,
                             {"rooms_extra_width": round(self._rooms_extra, 1)})

    def _reset_rooms_extra(self) -> None:
        self._rooms_extra = 0.0
        self._room_shape = None
        self._layout_rooms()
        self._save_rooms_extra()

    def _right_column_width(self) -> int:
        return max(self.rooms_card.sizeHint().width(),
                   self.selection_card.sizeHint().width())

    def _fix_right_column_width(self) -> None:
        """Η δεξιά στήλη παίρνει μόνο όσο πλάτος χρειάζεται."""
        column_width = self._right_column_width()
        if self.rooms_area.width() != column_width:
            self.rooms_area.setFixedWidth(column_width)
        if self.selection_card.width() != column_width:
            self.selection_card.setFixedWidth(column_width)

    def _refresh_announcement(self) -> None:
        mic = self.mic
        self.announce_button.set_live(mic.live)
        self.announce_button.set_muted(mic.muted)
        self.live_badge.setVisible(mic.live)
        # Σε ενεργή ανακοίνωση το κουμπί λήξης είναι πάντα διαθέσιμο.
        self.announce_button.setEnabled(mic.live or mic.can_start)
        self.announce_button.setToolTip(
            "Λήξη ανακοίνωσης" if mic.live else "Έναρξη ανακοίνωσης")

        self.preview_button.blockSignals(True)
        self.preview_button.setChecked(mic.preview)
        self.preview_button.blockSignals(False)
        self.preview_button.setEnabled(mic.preview or mic.can_preview)
        self.mute_button.blockSignals(True)
        self.mute_button.setChecked(mic.muted)
        self.mute_button.blockSignals(False)
        self._update_mute_button()

        self.meter.set_fraction(meter_fraction(mic.level()))

        if self.notice and self.clock() - self.notice[1] > NOTICE_SECONDS:
            self.notice = None
        if mic.error:
            text, state = f"Σφάλμα μικροφώνου: {mic.error}", "error"
        elif self.notice:
            text, state = self.notice[0], "error"
        elif mic.live:
            text, state = (HINT_LIVE_MUTED if mic.muted else HINT_LIVE), "idle"
        elif mic.muted:
            text, state = HINT_MUTED, "idle"
        elif mic.preview:
            text, state = HINT_PREVIEW, "idle"
        else:
            text, state = HINT_IDLE, "idle"
        self.hint.setText(text)
        _repolish(self.hint, state=state)

    def _update_mute_button(self) -> None:
        muted = self.mute_button.isChecked()
        self.mute_button.setText("Άρση σίγασης" if muted else "Σίγαση μικροφώνου")
        name = "mic" if muted else "mic_off"
        self.mute_button.setIcon(icons.icon(name, theme.PRIMARY if muted else theme.TEXT,
                                            disabled_color=theme.TEXT_MUTED,
                                            size=px(theme.ICON)))

    def closeEvent(self, event) -> None:
        self.timer.stop()
        self.mic.shutdown()
        super().closeEvent(event)


def system_font_pt(app: QApplication) -> float | None:
    """Μέγεθος γραμματοσειράς συστήματος σε points (None αν είναι άγνωστο)."""
    font = app.font()
    if font.pointSizeF() > 0:
        return font.pointSizeF()
    screen = app.primaryScreen()
    if font.pixelSize() > 0 and screen is not None:
        return font.pixelSize() * 72 / screen.logicalDotsPerInch()
    return None


def create_application(argv: list[str] | None = None, *,
                       base_pt: float | None = None) -> QApplication:
    """QApplication με το κοινό στυλ. `base_pt` μόνο για δοκιμές/στιγμιότυπα."""
    global _metrics
    app = QApplication.instance() or QApplication(argv or [])
    app.setApplicationName("School Intercom")
    app.setWindowIcon(icons.app_icon())
    app.setStyle("Fusion")  # ίδια σχεδίαση στοιχείων σε Windows/Linux
    _metrics = (theme.Metrics(base_pt) if base_pt else
                theme.metrics_for(system_font_pt(app)))
    font = app.font()
    font.setPointSizeF(_metrics.font_pt())
    app.setFont(font)
    app.setStyleSheet(theme.stylesheet(_metrics))
    return app


def current_metrics() -> theme.Metrics:
    return _metrics


def release_window(window: QWidget) -> None:
    """Καταστρέφει το παράθυρο όσο ζει ακόμη το QApplication.

    Χωρίς αυτό, στην έξοδο της συνάρτησης η Python μπορεί να καταστρέψει
    πρώτα το QApplication και μετά τα widgets, με crash κατά το κλείσιμο.
    """
    import shiboken6
    if shiboken6.isValid(window):
        shiboken6.delete(window)


class GuiRuntime:
    """Τα μέρη του Director με GUI: θύρα Clients, ανακάλυψη, εγγραφή, τελευταία σύνδεση.

    Το χρησιμοποιεί το `run_qt_gui` και η αυτόματη πραγματική δοκιμή
    (tools/security_gui_live_test.py), ώστε να δοκιμάζεται το ίδιο παράθυρο.
    """

    def __init__(self, args, *, accept_clients, stream_microphone,
                 enrollment=None, trust=None, last_seen=None,
                 security_options: dict | None = None):
        self.args = args
        self.accept_clients = accept_clients
        self.enrollment = enrollment
        self.trust = trust
        self.last_seen = last_seen
        self.security_options = security_options
        # Ρυθμίσεις εμφάνισης δίπλα στο μητρώο (π.χ. ~/.school-intercom/director-gui.json).
        registry_file = getattr(args, "registry_file", None)
        self.layout_settings = (Path(registry_file).with_name("director-gui.json")
                                if registry_file is not None else None)
        self.stop = threading.Event()
        self.broadcaster = AudioBroadcaster(max_clients=MAX_CLIENTS,
                                            on_event=lambda s: print(s, flush=True))
        self.microphone = DirectorMicrophone(stream_microphone, self.broadcaster,
                                             device=args.device, latency=args.latency)
        self.threads: list[threading.Thread] = []
        self.server: socket.socket | None = None
        self.window: DirectorWindow | None = None

    def start(self) -> DirectorWindow:
        from director.security import SecurityModel, track_last_seen
        args = self.args
        identifier = director_id(args.director_id_file)
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((args.host, args.port))
            server.listen(MAX_CLIENTS)
        except BaseException:
            server.close()
            raise
        self.server = server
        for name, target, kwargs in [
            ("discovery", announce, {"args": (self.stop, identifier, args.port),
                                     "kwargs": {"udp_port": args.discovery_port}}),
            ("accept", self.accept_clients, {"args": (server, self.broadcaster, self.stop)}),
        ]:
            thread = threading.Thread(target=target, daemon=True,
                                      name=f"director-gui-{name}", **kwargs)
            thread.start()
            self.threads.append(thread)
        security = None
        if self.last_seen is not None:
            self.last_seen_thread = threading.Thread(
                target=track_last_seen, args=(self.last_seen, self.broadcaster, self.stop),
                daemon=True, name="director-gui-last-seen")
            self.last_seen_thread.start()
            if self.enrollment is not None and self.trust is not None:
                self.enrollment.open()
                security = SecurityModel(self.enrollment, self.trust, self.broadcaster,
                                         self.last_seen)
        core = list(self.threads)
        self.window = DirectorWindow(
            self.broadcaster, self.microphone,
            server_up=lambda: all(thread.is_alive() for thread in core),
            security=security, security_options=self.security_options,
            layout_settings=self.layout_settings)
        return self.window

    def shutdown(self) -> None:
        if self.window is not None:
            release_window(self.window)
            self.window = None
        # Πρώτα STOP στους Clients, μετά κλείσιμο sockets και worker threads.
        self.microphone.shutdown()
        if self.enrollment is not None:
            self.enrollment.stop()
        self.stop.set()
        thread = getattr(self, "last_seen_thread", None)
        if thread is not None:
            thread.join(timeout=2)
        self.broadcaster.close()
        if self.server is not None:
            self.server.close()
        for thread in self.threads:
            thread.join(timeout=1)


def run_qt_gui(args, *, accept_clients, stream_microphone, enrollment=None, trust=None,
               last_seen=None) -> None:
    app = create_application()
    runtime = GuiRuntime(args, accept_clients=accept_clients,
                         stream_microphone=stream_microphone, enrollment=enrollment,
                         trust=trust, last_seen=last_seen)
    try:
        window = runtime.start()
        window.show()
        app.exec()
    finally:
        runtime.shutdown()
