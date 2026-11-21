"""Κεντρική οθόνη Director σε PySide6 (docs/ui-design.md, docs/ui-style.md).

Μόνο εμφάνιση και σύνδεση σημάτων. Η συμπεριφορά βρίσκεται στο
`director/gui.py` (χωρίς Qt) και τα χρώματα στο `shared/ui/theme.py`.
Τα widgets ενημερώνονται μόνο από το GUI thread, με QTimer.
"""
from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import (QAbstractButton, QApplication, QButtonGroup, QCheckBox,
                               QFrame, QGridLayout, QHBoxLayout, QLabel, QMainWindow,
                               QPushButton, QRadioButton, QScrollArea, QSizePolicy, QStyle,
                               QVBoxLayout, QWidget)

from director.broadcast import AudioBroadcaster, MAX_CLIENTS
from director.gui import (MODE_ALL, MODE_ROOMS, DirectorMicrophone, choose_targets,
                          meter_fraction, room_display_name, room_rows_per_column,
                          room_states)
from shared.discovery import announce, director_id
from shared.ui import icons, theme

WINDOW_TITLE = "School Intercom — Διεύθυνση"
PAGE_TITLE = "Κεντρική λειτουργία"
REFRESH_MS = 100
NOTICE_SECONDS = 4.0

HINT_IDLE = "Πατήστε για έναρξη"
HINT_LIVE = "ΖΩΝΤΑΝΑ · πατήστε για λήξη"
HINT_LIVE_MUTED = "ΖΩΝΤΑΝΑ · σίγαση · πατήστε για λήξη"
HINT_MUTED = "Σίγαση ενεργή · άρση σίγασης για έναρξη"
HINT_PREVIEW = "Προεπισκόπηση · δεν μεταδίδεται"

# Μενού: (κλειδί εικονιδίου, κείμενο). Μόνο η Κεντρική υλοποιείται ακόμη.
NAV_ITEMS = [
    ("home", "Κεντρική"),
    ("rooms", "Αίθουσες"),
    ("announcements", "Ανακοινώσεις"),
    ("scenarios", "Σενάρια"),
    ("logs", "Καταγραφές"),
    ("settings", "Ρυθμίσεις"),
]


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
        self.live = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(self.RING + 4, self.RING + 4)
        self.setAccessibleName("Ανακοίνωση")

    def set_live(self, live: bool) -> None:
        if live != self.live:
            self.live = live
            self.update()

    def sizeHint(self) -> QSize:
        return QSize(self.RING + 4, self.RING + 4)

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
        painter.setBrush(QColor(ring))
        painter.drawEllipse(center, self.RING / 2, self.RING / 2)
        painter.setBrush(QColor(fill))
        painter.drawEllipse(center, self.CIRCLE / 2, self.CIRCLE / 2)
        size = px(theme.ANNOUNCE_ICON)
        icons.render(painter, "mic", theme.WHITE,
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
        self.label = QLabel(room_display_name(room, theme.ROOM_NAME_MAX_CHARS))
        self.label.setObjectName("roomName")
        self.label.setToolTip(room)
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
                 clock: Callable[[], float] = time.monotonic):
        super().__init__()
        self.broadcaster = broadcaster
        self.mic = microphone
        self.server_up = server_up
        self.clock = clock
        self.mode = MODE_ALL
        self.checked: set[str] = set()
        self.rooms: list[tuple[str, bool]] = []
        self.room_rows: dict[str, RoomRow] = {}
        self._room_rows_per_column: int | None = None
        self._room_columns = 0
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
        outer.addWidget(self._build_page(), 1)

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
            if key == "home":
                button.setChecked(True)
                # Η τρέχουσα σελίδα μένει επιλεγμένη.
                button.clicked.connect(lambda _=False, b=button: b.setChecked(True))
            else:
                # Χωρίς προσποίηση μη διαθέσιμων λειτουργιών (ui-design.md).
                button.setEnabled(False)
                button.setToolTip("Δεν είναι ακόμη διαθέσιμο")
            self.nav_buttons[key] = button
            layout.addWidget(button)
        layout.addStretch(1)
        return sidebar

    def _build_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("page")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(px(18), px(14), px(18), px(16))
        layout.setSpacing(px(theme.CARD_SPACING))

        header = QHBoxLayout()
        title = QLabel(PAGE_TITLE)
        title.setObjectName("pageTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.connection_dot = StatusDot(theme.CONNECTED)
        self.connection_label = QLabel("Συνδεδεμένος")
        self.connection_label.setObjectName("connection")
        header.setSpacing(px(6))
        header.addWidget(self.connection_dot)
        header.addWidget(self.connection_label)
        layout.addLayout(header)

        # Επάνω σειρά: κάρτες ανακοίνωσης και αιθουσών με ίδιο ύψος.
        # Δεύτερη σειρά: επιλογή αιθουσών μόνο δεξιά.
        columns = QGridLayout()
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setHorizontalSpacing(px(theme.CARD_SPACING))
        columns.setVerticalSpacing(px(theme.CARD_SPACING))
        columns.setColumnStretch(0, 1)
        columns.setColumnStretch(1, 0)
        columns.setRowStretch(0, 1)
        columns.setRowStretch(1, 0)

        self.announcement_card = self._build_announcement_card()
        self.announcement_card.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        columns.addWidget(self.announcement_card, 0, 0)

        self.rooms_area = _ResizeNotifier(self._layout_rooms)
        self.rooms_area.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        right_top = QVBoxLayout(self.rooms_area)
        right_top.setContentsMargins(0, 0, 0, 0)
        right_top.setSpacing(0)
        self.rooms_card = self._build_rooms_card()
        self.rooms_card.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        right_top.addWidget(self.rooms_card)
        columns.addWidget(self.rooms_area, 0, 1)

        self.selection_card = self._build_selection_card()
        self.selection_card.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        columns.addWidget(self.selection_card, 1, 1)

        layout.addLayout(columns, 1)
        return page

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
        up = self.server_up()
        self.connection_dot.set_color(theme.CONNECTED if up else theme.LIVE)
        self.connection_label.setText("Συνδεδεμένος" if up else "Εκτός λειτουργίας")
        _repolish(self.connection_label, state="up" if up else "down")

    def _refresh_rooms(self) -> None:
        rooms = room_states(self.broadcaster.status_snapshot())
        names = [room for room, _ in rooms]
        if names != [room for room, _ in self.rooms]:
            self._rebuild_room_rows(names)
        self.rooms = rooms
        online = sum(1 for _, is_online in rooms if is_online)
        self.online_count.setText(f"({online}/{len(rooms)} online)")
        self.online_count.setVisible(bool(rooms))
        self.empty_rooms.setVisible(not rooms)
        self.room_scroll.setVisible(bool(rooms))
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
        self._layout_rooms()

    def _room_column_width(self) -> int:
        """Ίσες στήλες, όσο το μεγαλύτερο όνομα (με όριο), checkbox και κουκκίδα."""
        rows = list(self.room_rows.values())
        rows[0].label.ensurePolished()
        metrics = QFontMetrics(rows[0].label.font())
        name = max(metrics.horizontalAdvance(r.label.text()) for r in rows) + px(2)
        spacing = px(9)
        natural = px(2) * 2 + px(18) + spacing + name + px(theme.ROOM_DOT_GAP) + px(theme.DOT)
        # Ελάχιστο πλάτος ώστε η κουκκίδα να φτάνει στο δεξί άκρο
        # και σε γραμμή με μικρό όνομα αίθουσας.
        return max(natural, px(theme.LEFT_COLUMN_WIDTH) -
                   2 * (px(theme.CARD_PADDING) + px(2)))

    def _layout_rooms(self) -> None:
        """Πρώτη στήλη ως το διαθέσιμο ύψος, μετά δεύτερη (ui-style.md §5)."""
        if not self.room_rows:
            return
        row_height = px(theme.ROW_HEIGHT)
        margins = self.rooms_card_layout.contentsMargins()
        chrome = (margins.top() + margins.bottom() + self.rooms_card_layout.spacing()
                  + self.rooms_title.sizeHint().height() + 2)
        available = self.rooms_area.height() - chrome
        rows_fit = max(1, available // row_height)
        names = list(self.room_rows)
        rows = room_rows_per_column(len(names), rows_fit)
        width = self._room_column_width()
        columns = 1 if len(names) <= rows else 2
        self._room_columns = columns
        if rows != self._room_rows_per_column:
            self._room_rows_per_column = rows
            for row in self.room_rows.values():
                self.room_grid.removeWidget(row)
            for index, room in enumerate(names):
                self.room_grid.addWidget(self.room_rows[room], index % rows, index // rows)
        for row in self.room_rows.values():
            row.setFixedWidth(width)
        scrolls = rows > rows_fit
        scrollbar = (self.room_scroll.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
                     + px(6)) if scrolls else 0
        self.room_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded if scrolls else
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        grid_width = width * columns + self.room_grid.horizontalSpacing() * (columns - 1)
        self.room_grid_widget.setFixedSize(grid_width, rows * row_height)
        self.room_scroll.setFixedSize(grid_width + scrollbar, min(rows, rows_fit) * row_height)
        # Η δεξιά στήλη παίρνει μόνο όσο πλάτος χρειάζεται.
        column_width = max(
            self.rooms_card.sizeHint().width(),
            self.selection_card.sizeHint().width())
        if self.rooms_area.width() != column_width:
            self.rooms_area.setFixedWidth(column_width)
        if self.selection_card.width() != column_width:
            self.selection_card.setFixedWidth(column_width)

    def _refresh_announcement(self) -> None:
        mic = self.mic
        self.announce_button.set_live(mic.live)
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
            text, state = (HINT_LIVE_MUTED if mic.muted else HINT_LIVE), "live"
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


def run_qt_gui(args, *, accept_clients, stream_microphone) -> None:
    app = create_application()
    stop = threading.Event()
    broadcaster = AudioBroadcaster(max_clients=MAX_CLIENTS,
                                   on_event=lambda s: print(s, flush=True))
    microphone = DirectorMicrophone(stream_microphone, broadcaster,
                                    device=args.device, latency=args.latency)
    identifier = director_id(args.director_id_file)
    threads: list[threading.Thread] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((args.host, args.port))
            server.listen(MAX_CLIENTS)
            for name, target, kwargs in [
                ("discovery", announce, {"args": (stop, identifier, args.port),
                                         "kwargs": {"udp_port": args.discovery_port}}),
                ("accept", accept_clients, {"args": (server, broadcaster, stop)}),
            ]:
                thread = threading.Thread(target=target, daemon=True,
                                          name=f"director-gui-{name}", **kwargs)
                thread.start()
                threads.append(thread)
            window = DirectorWindow(
                broadcaster, microphone,
                server_up=lambda: all(thread.is_alive() for thread in threads))
            window.show()
            try:
                app.exec()
            finally:
                release_window(window)
    finally:
        # Πρώτα STOP στους Clients, μετά κλείσιμο sockets και worker threads.
        microphone.shutdown()
        stop.set()
        broadcaster.close()
        for thread in threads:
            thread.join(timeout=1)
