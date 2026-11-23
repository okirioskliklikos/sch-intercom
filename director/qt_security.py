"""Σελίδα «Ασφάλεια» του Director σε PySide6 (docs/ui-style.md §6).

Μόνο εμφάνιση και σύνδεση σημάτων· η λογική είναι στο `director/security.py`.
Tab «Υπολογιστές» (απόφαση χρήστη 22/11/2026): αριστερά «Νέες εγγραφές», μία
κάρτα ανά αίτημα σε μία στήλη· ήπιο διαχωριστικό· δεξιά «Εγγεγραμμένοι υπολογιστές»,
μία κάρτα ανά αίθουσα, όσες χωρούν ανά γραμμή. Το tab «Πιστοποιητικά» δείχνει την
ταυτότητα του Manager (`director/qt_maintenance.py`, ui-style.md §7).
"""
from __future__ import annotations

from pathlib import Path
import time
from collections.abc import Callable

from PySide6.QtCore import QRegularExpression, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFontDatabase, QPalette, QRegularExpressionValidator
from PySide6.QtWidgets import (QApplication, QFrame, QGraphicsDropShadowEffect, QProxyStyle, QStyle, QGridLayout, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMenu,
                               QMessageBox, QPushButton, QScrollArea, QSizePolicy,
                               QStackedWidget, QToolButton, QVBoxLayout, QWidget)

from director.qt_gui import (ElidedLabel, SplitHandle, StatusDot, _repolish,
                             load_layout_settings, px, save_layout_settings)
from director.security import (CODE_LENGTH, TEXT_CHANGES_PRIMARY_ONLY, TEXT_PRIMARY_ONLY,
                               ComputerView, Decision, RequestView, RoleStatus, RoomView,
                               SecurityModel, computers_text)
from shared.ui import icons, theme

PAGE_TITLE = "Ασφάλεια"
TAB_COMPUTERS = "Υπολογιστές"
TAB_CERTIFICATES = "Πιστοποιητικά"
NO_REQUESTS = "Δεν υπάρχουν νέες εγγραφές"
NO_COMPUTERS = "Δεν υπάρχουν εγγεγραμμένοι υπολογιστές"
#: Πόσο μένει ορατό το αποτέλεσμα ενός αιτήματος που έκλεισε.
RESULT_SECONDS = 15.0


def _section_head(title: str) -> tuple[QWidget, QLabel]:
    """Τίτλος ενότητας αριστερά, μικρό κείμενο (πλήθος/όριο) δεξιά."""
    head = QWidget()
    row = QHBoxLayout(head)
    row.setContentsMargins(px(2), 0, px(2), 0)
    row.setSpacing(px(10))
    row.addWidget(_label(title, "cardTitle"))
    row.addStretch(1)
    count = _label("", "sectionCount")
    row.addWidget(count)
    return head, count


class _AllOrNothingLabel(ElidedLabel):
    """Όλο το κείμενο ή τίποτα (ποτέ «Αλλ…»)."""

    def _elide(self) -> None:
        fits = self.fontMetrics().horizontalAdvance(self.full_text) <= self.contentsRect().width()
        text = self.full_text if fits else ""
        if text != self.text():
            self.setText(text)


def _lock_note(text: str, name: str = "lockNote") -> QWidget:
    """Λουκέτο και γκρι κείμενο (Εφεδρικός: «Μόνο στον Κύριο» κ.λπ.)."""
    note = QWidget()
    row = QHBoxLayout(note)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(px(6))
    icon = QLabel()
    # Στην κεφαλίδα μικρό, όπως το κείμενο· στην κενή κάρτα 18px (απόφαση χρήστη 23/11/2026).
    size = px(theme.ICON) if name == "closedNote" else px(theme.LOCK_NOTE_ICON)
    icon.setPixmap(icons.pixmap("lock", theme.TEXT_MUTED, size, stroke_width=2.0))
    row.addWidget(icon, 0, Qt.AlignmentFlag.AlignVCenter)
    if name == "lockNote":
        # Στην κεφαλίδα δεν φαρδαίνει τη στήλη: αν δεν χωρά, μένει μόνο το λουκέτο
        # (το κείμενο στο tooltip).
        label = _AllOrNothingLabel(text)
        label.setObjectName(name)
        note.setToolTip(text)
    else:
        label = _label(text, name)
    row.addWidget(label, 1 if name == "lockNote" else 0, Qt.AlignmentFlag.AlignVCenter)
    note.label = label
    return note


def _menu_icon(name: str, color: str = theme.TEXT_MUTED):
    """Εικονίδιο στοιχείου μενού: γραμμικό 16px όπως τα κουμπιά (ui-style.md §6)."""
    return icons.icon(name, color, size=px(theme.MENU_ICON), stroke_width=1.8)


class _MenuStyle(QProxyStyle):
    """Μεγαλύτερα εικονίδια στα μενού ⋮ (το QSS icon-size δεν ισχύει για QMenu)."""

    def pixelMetric(self, metric, option=None, widget=None):
        if metric == QStyle.PixelMetric.PM_SmallIconSize:
            return px(theme.MENU_ICON)
        return super().pixelMetric(metric, option, widget)


class ActionMenu(QMenu):
    """Μενού ⋮ με ήπια εξωτερική σκιά (απόφαση χρήστη 22/11/2026).

    Η σκιά σχεδιάζεται από την εφαρμογή (διάφανο παράθυρο + QGraphicsDropShadowEffect
    μέσα στο περιθώριο MENU_SHADOW), ώστε να είναι ίδια σε Windows και Linux/Wayland·
    η σκιά του λειτουργικού απενεργοποιείται για να μη φαίνονται δύο.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("actionMenu")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(px(theme.MENU_SHADOW) * 2)
        shadow.setOffset(0, px(2))
        color = QColor(theme.TEXT)
        color.setAlpha(60)
        shadow.setColor(color)
        self.setGraphicsEffect(shadow)


def _menu_button(accessible: str, actions: list[QAction | None]) -> QToolButton:
    """Μικρό τετράγωνο κουμπί με εικονίδιο ⋮ και μενού (None = διαχωριστικό)."""
    button = QToolButton()
    button.setObjectName("rowMenu")
    button.setIcon(icons.icon("more", theme.TEXT_MUTED, size=px(16), stroke_width=3.0))
    button.setIconSize(QSize(px(16), px(16)))
    button.setFixedSize(px(26), px(26))
    button.setAccessibleName(accessible)
    button.setToolTip("Ενέργειες")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    menu = ActionMenu(button)
    style = _MenuStyle(QApplication.style().name())
    style.setParent(menu)
    menu.setStyle(style)
    for action in actions:
        if action is None:
            menu.addSeparator()
        else:
            action.setParent(menu)
            menu.addAction(action)
    button.setMenu(menu)
    return button


def _label(text: str, name: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName(name)
    return label


class RequestRow(QFrame):
    """Κάρτα αιτήματος: «Αίθουσα · Υπολογιστής» με αποτέλεσμα πάνω δεξιά, ID/χρόνοι,
    και πεδίο κωδικού που γεμίζει αριστερά με Έγκριση/Απόρριψη τέρμα δεξιά."""

    def __init__(self, request: RequestView, on_approve: Callable[[str, str], None],
                 on_deny: Callable[[str], None], parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.request = request
        self.final = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(14), px(10), px(14), px(11))
        layout.setSpacing(px(6))
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(px(10))
        self.title = ElidedLabel(request.title)
        self.title.setObjectName("requestTitle")
        self.result = _label("", "result")
        self.result.hide()
        top.addWidget(self.title, 1)
        top.addWidget(self.result, 0, Qt.AlignmentFlag.AlignRight)
        layout.addLayout(top)
        self.meta = _label("", "small")
        layout.addWidget(self.meta)

        self.actions = QWidget()
        actions = QHBoxLayout(self.actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(px(8))
        self.code = QLineEdit()
        self.code.setObjectName("codeInput")
        self.code.setMaxLength(CODE_LENGTH)
        self.code.setValidator(QRegularExpressionValidator(
            QRegularExpression(f"[0-9]{{0,{CODE_LENGTH}}}"), self.code))
        self.code.setPlaceholderText("·" * CODE_LENGTH)
        self.code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code.setAccessibleName(f"Κωδικός για {request.title}")
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        mono.setPointSizeF(self.font().pointSizeF() * 1.05)
        mono.setLetterSpacing(mono.SpacingType.PercentageSpacing, 118)
        self.code.setFont(mono)
        palette = self.code.palette()
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(theme.CONTROL_BORDER))
        self.code.setPalette(palette)
        # Γεμίζει τον χώρο αριστερά, ποτέ στενότερο από 6 ψηφία (απόφαση 22/11/2026).
        digits = self.code.fontMetrics().horizontalAdvance("0" * (CODE_LENGTH + 2))
        self.code.setMinimumWidth(digits + px(22))
        self.code.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        icon_size = QSize(px(theme.ICON), px(theme.ICON))
        self.approve = QPushButton("Έγκριση")
        self.approve.setObjectName("approve")
        self.approve.setIcon(icons.icon("check", theme.WHITE, disabled_color=theme.TEXT_MUTED,
                                        size=px(theme.ICON),
                                        stroke_width=theme.ACTION_ICON_STROKE))
        self.deny = QPushButton("Απόρριψη")
        self.deny.setObjectName("deny")
        self.deny.setIcon(icons.icon("trash", theme.LIVE, size=px(theme.ICON),
                                     stroke_width=theme.TRASH_ICON_STROKE))
        for button in (self.approve, self.deny):
            button.setIconSize(icon_size)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        actions.addWidget(self.code, 1)
        actions.addWidget(self.approve)
        actions.addWidget(self.deny)
        layout.addWidget(self.actions)

        rid = request.request_id
        self.approve.clicked.connect(lambda: on_approve(rid, self.code.text()))
        self.code.returnPressed.connect(lambda: on_approve(rid, self.code.text()))
        self.deny.clicked.connect(lambda: on_deny(rid))

    def set_meta(self, now: float) -> None:
        text = self.request.meta(now)
        if text != self.meta.text():
            self.meta.setText(text)

    def show_decision(self, decision: Decision) -> None:
        self.result.setText(decision.text)
        self.result.setToolTip(decision.hint)
        _repolish(self.result, ok="true" if decision.ok else "false")
        self.result.show()
        if decision.final:
            self.final = True
            self.actions.hide()
            _repolish(self.title, final="true")
        else:
            self.code.clear()
            self.code.setFocus()


class ComputerRow(QWidget):
    """Υπολογιστής με εσοχή: κουκκίδα, όνομα, κατάσταση, μενού «⋮» τέρμα δεξιά."""

    def __init__(self, computer: ComputerView, page: "SecurityPage", parent=None):
        super().__init__(parent)
        self.computer = computer
        layout = QHBoxLayout(self)
        layout.setContentsMargins(px(theme.COMPUTER_INDENT), 0, 0, 0)
        layout.setSpacing(px(9))
        self.setMinimumHeight(px(30))
        self.dot = StatusDot(theme.ROOM_DOT_OFFLINE)
        self.name = ElidedLabel(computer.name)
        self.name.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.state = _label("", "small")
        self.state.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        cid = computer.client_id
        self.actions = {
            "room": QAction(_menu_icon("move"), "Αλλαγή αίθουσας…"),
            "rename": QAction(_menu_icon("edit"), "Μετονομασία…"),
            "revoke": QAction(_menu_icon("trash", theme.LIVE), "Ανάκληση…"),
        }
        self.actions["room"].triggered.connect(lambda: page.change_room(cid))
        self.actions["rename"].triggered.connect(lambda: page.rename(cid))
        self.actions["revoke"].triggered.connect(lambda: page.revoke(cid))
        self.menu_button = _menu_button(
            f"Ενέργειες για {computer.name}",
            [self.actions["room"], self.actions["rename"], None, self.actions["revoke"]])
        layout.addWidget(self.dot)
        # Το όνομα παίρνει τον επιπλέον χώρο· η κατάσταση στοιχίζεται δεξιά, δίπλα στο ⋮
        # (απόφαση χρήστη 22/11/2026)· το όνομα κόβεται με «…» μόνο όταν δεν χωρά.
        layout.addWidget(self.name, 1)
        layout.addWidget(self.state)
        layout.addWidget(self.menu_button)

    def update_state(self, computer: ComputerView, now: float) -> None:
        self.computer = computer
        self.dot.set_color(theme.ROOM_DOT_ONLINE if computer.online else theme.ROOM_DOT_OFFLINE)
        text = computer.state(now)
        if self.state.text() != text:
            self.state.setText(text)


class RoomHeader(QWidget):
    def __init__(self, room: RoomView, page: "SecurityPage | None" = None, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(9))
        self.setMinimumHeight(px(30))
        self.dot = StatusDot(theme.ROOM_DOT_OFFLINE)
        self.name = ElidedLabel(room.room)
        self.name.setObjectName("roomTitle")
        self.count = _label(computers_text(len(room.computers)), "small")
        layout.addWidget(self.dot)
        layout.addWidget(self.name, 1)
        layout.addWidget(self.count)
        # Μενού αίθουσας: μετονομασία για όλους τους υπολογιστές της μαζί.
        name = room.room
        self.actions = {"rename_room": QAction(_menu_icon("edit"), "Μετονομασία αίθουσας…")}
        if page is not None:
            self.actions["rename_room"].triggered.connect(lambda: page.rename_room(name))
        self.menu_button = _menu_button(f"Ενέργειες για την αίθουσα {name}",
                                        [self.actions["rename_room"]])
        layout.addWidget(self.menu_button)

    def update_state(self, room: RoomView, now: float) -> None:
        self.dot.set_color(theme.ROOM_DOT_ONLINE if room.online else theme.ROOM_DOT_OFFLINE)
        # Για την αίθουσα μετρά ο πιο πρόσφατος υπολογιστής της.
        self.setToolTip("Online" if room.online else f"Τελευταία σύνδεση: {room.state(now)}")


class RoomCard(QFrame):
    """Κάρτα αίθουσας: κεφαλίδα (κουκκίδα, όνομα, «N υπολογιστές») και οι υπολογιστές."""

    def __init__(self, room: RoomView, page: "SecurityPage", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(14), px(8), px(10), px(8))
        layout.setSpacing(0)
        self.header = RoomHeader(room, page)
        layout.addWidget(self.header)
        self.rows: dict[str, ComputerRow] = {}
        for computer in room.computers:
            row = ComputerRow(computer, page)
            self.rows[computer.client_id] = row
            layout.addWidget(row)


class SecurityPage(QWidget):
    """Σελίδα «Ασφάλεια». `refresh()` καλείται από το παράθυρο όσο είναι ορατή.

    Οι διάλογοι είναι αντικαταστάσιμοι (`ask_text`, `confirm`, `warn`) για
    δοκιμές χωρίς χρήστη.
    """

    def __init__(self, model: SecurityModel, *, clock: Callable[[], float] = time.time,
                 ask_text: Callable[[str, str, str], str | None] | None = None,
                 confirm: Callable[[str, str, str], bool] | None = None,
                 warn: Callable[[str], None] | None = None,
                 layout_settings: Path | None = None,
                 identity: Callable[[], object] | None = None,
                 role_card: QWidget | None = None, parent=None):
        super().__init__(parent)
        # Κύριος/Εφεδρικός (set_role): στον Εφεδρικό κλειστές εγγραφές, χωρίς μενού ⋮.
        self.role: RoleStatus | None = None
        self.locked = False
        self.closed_reason: str | None = None
        self.role_card = role_card
        #: Καλείται σε αλλαγή tab (το παράθυρο κρύβει τότε το κουμπί της κόκκινης μπάρας).
        self.tab_changed: Callable[[], None] | None = None
        # Πλάτος «Νέες εγγραφές» από τη λαβή, σε px του mockup· None = αυτόματο.
        self._layout_settings = layout_settings
        # 0 στο αρχείο = επαναφορά στην προεπιλογή (διπλό κλικ).
        self._left_user: float | None = load_layout_settings(layout_settings).get(
            "security_left_width") or None
        self._drag_start: int | None = None
        self._relayout_pending = False
        self.model = model
        self.clock = clock
        self.ask_text = ask_text or self._ask_text
        self.confirm = confirm or self._confirm
        self.warn = warn or self._warn
        self.request_rows: dict[str, RequestRow] = {}
        self.finished: dict[str, float] = {}
        self.computer_rows: dict[str, ComputerRow] = {}
        self.room_headers: dict[str, RoomHeader] = {}
        self._rooms_key: tuple | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(theme.CARD_SPACING))

        tabs = QHBoxLayout()
        tabs.setSpacing(px(6))
        self.tab_computers = QPushButton(TAB_COMPUTERS)
        self.tab_certificates = QPushButton(TAB_CERTIFICATES)
        for tab in (self.tab_computers, self.tab_certificates):
            tab.setObjectName("tab")
            tab.setCheckable(True)
            tabs.addWidget(tab)
        tabs.addStretch(1)
        self.tab_computers.setChecked(True)
        self.tab_computers.clicked.connect(lambda: self.show_tab(TAB_COMPUTERS))
        self.tab_certificates.clicked.connect(lambda: self.show_tab(TAB_CERTIFICATES))
        self.certificates = None
        if identity is None:
            self.tab_certificates.setEnabled(False)
            self.tab_certificates.setToolTip("Δεν είναι διαθέσιμο")
        for tab in (self.tab_computers, self.tab_certificates):
            if tab.isEnabled():
                tab.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addLayout(tabs)

        body = QWidget()
        body.setObjectName("securityBody")
        self.columns = QGridLayout(body)
        # Κενό ανάμεσα στις κάρτες και τη γραμμή κύλισης (απόφαση χρήστη 22/11/2026).
        self.columns.setContentsMargins(0, 0, px(theme.SCROLL_GAP), 0)
        # Το οριζόντιο κενό ανάμεσα στις ενότητες το πιάνει η λαβή (στήλη 1).
        self.columns.setHorizontalSpacing(0)
        self.columns.setVerticalSpacing(px(theme.SECTION_GAP))
        self._build_requests_section()
        self._build_computers_section()
        # Λαβή αντί για διαχωριστικό γραμμή (απόφαση χρήστη 22/11/2026).
        self.split_handle = SplitHandle(
            self._drag_left, self._save_left, self._reset_left,
            width=2 * px(theme.SECTION_GAP) + 1,
            tooltip="Σύρετε για πλάτος «Νέες εγγραφές» · διπλό κλικ: προεπιλογή")
        self.side_by_side: bool | None = None
        self.room_columns = 0
        self._room_order: tuple | None = None
        self._left_width = -1
        self._arrange(self._available_width())
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(body)
        self.scroll.verticalScrollBar().valueChanged.connect(
            lambda _v: self.split_handle.update())
        # Tab «Πιστοποιητικά»: μόνο η ταυτότητα του Manager (ui-style.md §7).
        self.tab_pages = QStackedWidget()
        self.tab_pages.addWidget(self.scroll)
        if identity is not None:
            from director.qt_maintenance import CertificatesPanel
            self.certificates = CertificatesPanel(identity, extra=role_card)
            self.tab_pages.addWidget(self.certificates)
        layout.addWidget(self.tab_pages, 1)

    def show_tab(self, name: str) -> None:
        certificates = name == TAB_CERTIFICATES and self.certificates is not None
        self.tab_computers.setChecked(not certificates)
        self.tab_certificates.setChecked(certificates)
        self.tab_pages.setCurrentIndex(1 if certificates else 0)
        if certificates:
            self.certificates.refresh()
        else:
            self.refresh()
        if self.tab_changed is not None:
            self.tab_changed()

    @property
    def current_tab(self) -> str:
        return TAB_CERTIFICATES if self.tab_pages.currentIndex() == 1 else TAB_COMPUTERS

    # --- διάταξη ------------------------------------------------------------
    def _available_width(self) -> int:
        return max(0, self.width() - px(theme.SCROLLBAR) - px(theme.SCROLL_GAP) - px(6))

    def _left_minimum(self) -> int:
        if self.closed_reason is not None:
            # Κλειστές εγγραφές: μόνο η κάρτα με το λουκέτο, οι αίθουσες παίρνουν τον χώρο.
            return self.requests_section.minimumSizeHint().width()
        return max(px(theme.LEFT_COLUMN_MIN), self.requests_section.minimumSizeHint().width())

    def _room_card_minimum(self) -> int:
        widths = [card.minimumSizeHint().width() for card in self.room_cards.values()]
        return max([px(theme.ROOM_CARD_MIN), *widths])

    def _right_minimum(self) -> int:
        """Η δεξιά ενότητα χωρά τουλάχιστον μία κάρτα αίθουσας και την κεφαλίδα της."""
        return max(self._room_card_minimum(), self.computers_head.minimumSizeHint().width(),
                   self.no_computers.minimumSizeHint().width())

    def _gaps(self) -> int:
        return self.split_handle.width()

    def two_columns_fit(self, width: int) -> bool:
        return width - self._left_minimum() - self._gaps() >= self._right_minimum()

    def _left_default(self, width: int) -> int:
        left = self._left_minimum()
        if width - px(theme.LEFT_COLUMN_MAX) - self._gaps() >= self._right_minimum():
            left = max(left, px(theme.LEFT_COLUMN_MAX))
        return left

    def _left_bounds(self, width: int) -> tuple[int, int]:
        low = self._left_minimum()
        return low, max(low, width - self._gaps() - self._right_minimum())

    @property
    def left_width(self) -> int:
        """Πλάτος στήλης «Νέες εγγραφές» στη διάταξη δίπλα-δίπλα."""
        return self.requests_section.width()

    def _arrange(self, width: int) -> None:
        """«Νέες εγγραφές» αριστερά (380–440), διαχωριστικό, αίθουσες δεξιά σε όσες
        στήλες χωρούν· σε στενό παράθυρο η μία ενότητα κάτω από την άλλη."""
        side_by_side = self.two_columns_fit(width)
        gaps = self._gaps()
        if self._left_user is not None:
            low, high = self._left_bounds(width)
            left = max(low, min(px(self._left_user), high))
        else:
            left = self._left_default(width) if side_by_side else self._left_minimum()
        if side_by_side != self.side_by_side:
            self.side_by_side = side_by_side
            top = Qt.AlignmentFlag.AlignTop
            for widget in (self.requests_section, self.split_handle, self.computers_section):
                self.columns.removeWidget(widget)
            if side_by_side:
                self.columns.addWidget(self.requests_section, 0, 0, top)
                self.columns.addWidget(self.split_handle, 0, 1)
                self.columns.addWidget(self.computers_section, 0, 2, top)
                self.split_handle.show()
                for column, stretch in enumerate((0, 0, 1)):
                    self.columns.setColumnStretch(column, stretch)
                for row in range(3):
                    self.columns.setRowStretch(row, 0)
            else:
                # Η μία κάτω από την άλλη: χωρίς λαβή, μόνο κενό ενότητας.
                self.split_handle.hide()
                self.columns.addWidget(self.requests_section, 0, 0, top)
                self.columns.addWidget(self.computers_section, 2, 0, top)
                self.columns.setRowMinimumHeight(1, 0)
                for column, stretch in enumerate((1, 0, 0)):
                    self.columns.setColumnStretch(column, stretch)
                for row, stretch in enumerate((0, 0, 1)):
                    self.columns.setRowStretch(row, stretch)
            self._left_width = -1
        if side_by_side:
            if left != self._left_width:
                self._left_width = left
                self.requests_section.setFixedWidth(left)
            right = width - left - gaps
        else:
            if self._left_width != 0:
                self._left_width = 0
                self.requests_section.setMinimumWidth(0)
                self.requests_section.setMaximumWidth(16777215)
            right = width
        self._place_room_cards(right)

    def _place_room_cards(self, width: int) -> None:
        """Κάρτες αιθουσών σε γραμμές (αλφαβητικά), όσες χωρούν με ελάχιστο ROOM_CARD_MIN."""
        spacing = self.room_grid.horizontalSpacing()
        columns = max(1, (width + spacing) // (self._room_card_minimum() + spacing))
        order = (columns, tuple(self.room_cards))
        if order == self._room_order:
            return
        self._room_order = order
        self.room_columns = columns
        for card in self.room_cards.values():
            self.room_grid.removeWidget(card)
        for index, card in enumerate(self.room_cards.values()):
            self.room_grid.addWidget(card, index // columns, index % columns,
                                     Qt.AlignmentFlag.AlignTop)
        for column in range(max(columns, self.room_grid.columnCount())):
            self.room_grid.setColumnStretch(column, 1 if column < columns else 0)

    def _relayout(self) -> None:
        """Ξανά μετά από αλλαγή περιεχομένου (νέα αίθουσα, αίτημα)."""
        self._arrange(self._available_width())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Στον επόμενο κύκλο γεγονότων, όχι μέσα στο setGeometry του layout
        # (ίδιο πρόβλημα με την Κεντρική: αλλαγή θέσεων grid κατά το resize).
        if not self._relayout_pending:
            self._relayout_pending = True
            # Με το self ως context: ακυρώνεται αν η σελίδα καταστραφεί πριν τρέξει.
            QTimer.singleShot(0, self, self._run_relayout)

    def _run_relayout(self) -> None:
        self._relayout_pending = False
        self._relayout()

    # --- λαβή ---------------------------------------------------------------
    def _drag_left(self, delta: int) -> None:
        if not self.side_by_side:
            return
        if self._drag_start is None:
            self._drag_start = self.left_width
        width = self._available_width()
        low, high = self._left_bounds(width)
        real = max(low, min(self._drag_start + delta, high))
        self._left_user = real / (px(1000) / 1000)
        self._relayout()

    def _save_left(self) -> None:
        self._drag_start = None
        value = None if self._left_user is None else round(self._left_user, 1)
        if value is not None:
            save_layout_settings(self._layout_settings, {"security_left_width": value})

    def _reset_left(self) -> None:
        self._drag_start = None
        self._left_user = None
        self._relayout()
        save_layout_settings(self._layout_settings, {"security_left_width": 0.0})

    # --- κατασκευή ---------------------------------------------------------
    def _build_requests_section(self) -> None:
        self.requests_section = QWidget()
        layout = QVBoxLayout(self.requests_section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(theme.REQUEST_CARD_SPACING))
        head, self.request_count = _section_head("Νέες εγγραφές")
        self.requests_lock = _lock_note(TEXT_PRIMARY_ONLY)
        self.requests_lock.hide()
        head.layout().insertWidget(1, self.requests_lock, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(head)
        # Εφεδρικός (ή χωρίς εξυπηρέτηση): μία κάρτα με λουκέτο αντί για αιτήματα.
        self.closed_card = QFrame()
        self.closed_card.setObjectName("card")
        closed = QHBoxLayout(self.closed_card)
        closed.setContentsMargins(px(14), px(14), px(14), px(14))
        self.closed_note = _lock_note("", "closedNote")
        closed.addStretch(1)
        closed.addWidget(self.closed_note)
        closed.addStretch(1)
        self.closed_card.hide()
        layout.addWidget(self.closed_card)
        # Οι εγγραφές είναι πάντα ανοιχτές· κείμενο μόνο αν η θύρα δεν άνοιξε.
        self.port_notice = _label("", "result")
        self.port_notice.setWordWrap(True)
        self.port_notice.hide()
        layout.addWidget(self.port_notice)
        self.no_requests = _label(NO_REQUESTS, "muted")
        layout.addWidget(self.no_requests)
        self.requests_box = QWidget()
        self.requests_layout = QVBoxLayout(self.requests_box)
        self.requests_layout.setContentsMargins(0, 0, 0, 0)
        self.requests_layout.setSpacing(px(theme.REQUEST_CARD_SPACING))
        layout.addWidget(self.requests_box)
        layout.addStretch(1)

    def _build_computers_section(self) -> None:
        self.computers_section = QWidget()
        layout = QVBoxLayout(self.computers_section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(theme.REQUEST_CARD_SPACING))
        self.computers_head, self.totals = _section_head("Εγγεγραμμένοι υπολογιστές")
        self.computers_lock = _lock_note(TEXT_CHANGES_PRIMARY_ONLY)
        self.computers_lock.hide()
        # Τέρμα δεξιά, μετά το «N υπολογιστές · M online» (mockup 4.2D2).
        self.computers_head.layout().addWidget(self.computers_lock, 0,
                                               Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.computers_head)
        self.no_computers = _label(NO_COMPUTERS, "muted")
        layout.addWidget(self.no_computers)
        grid = QWidget()
        self.room_grid = QGridLayout(grid)
        self.room_grid.setContentsMargins(0, 0, 0, 0)
        self.room_grid.setSpacing(px(theme.CARD_SPACING))
        layout.addWidget(grid)
        layout.addStretch(1)
        self.room_cards: dict[str, RoomCard] = {}

    # --- ενέργειες ---------------------------------------------------------
    def approve(self, request_id: str, code: str) -> None:
        row = self.request_rows.get(request_id)
        if row is None or row.final:
            return
        decision = self.model.approve(request_id, code)
        row.show_decision(decision)
        if decision.final:
            self.finished[request_id] = self.clock()
        self.refresh()

    def deny(self, request_id: str) -> None:
        row = self.request_rows.get(request_id)
        if row is None or row.final:
            return
        decision = self.model.deny(request_id)
        row.show_decision(decision)
        self.finished[request_id] = self.clock()
        self.refresh()

    def _computer(self, client_id: str) -> ComputerView | None:
        row = self.computer_rows.get(client_id)
        return None if row is None else row.computer

    def change_room(self, client_id: str) -> None:
        computer = self._computer(client_id)
        if computer is None:
            return
        room = self.ask_text("Αλλαγή αίθουσας", f"Νέα αίθουσα για «{computer.name}»:",
                             computer.room)
        if room is None or room.strip() == computer.room:
            return
        self._apply(lambda: self.model.change_room(client_id, room))

    def rename(self, client_id: str) -> None:
        computer = self._computer(client_id)
        if computer is None:
            return
        name = self.ask_text("Μετονομασία", f"Νέο όνομα για «{computer.name}» ({computer.room}):",
                             computer.name)
        if name is None or name.strip() == computer.name:
            return
        self._apply(lambda: self.model.rename(client_id, name))

    def rename_room(self, room: str) -> None:
        """Νέο όνομα για όλους τους υπολογιστές της αίθουσας. Αν υπάρχει ήδη αίθουσα
        με αυτό το όνομα, ζητείται επιβεβαίωση και οι υπολογιστές μεταφέρονται εκεί."""
        rooms = {view.room: view for view in self.model.rooms()}
        current = rooms.get(room)
        if current is None:
            return
        new = self.ask_text("Μετονομασία αίθουσας",
                            f"Νέο όνομα για την αίθουσα «{room}» "
                            f"({computers_text(len(current.computers))}):", room)
        if new is None or new.strip() == room:
            return
        new = new.strip()
        if new in rooms:
            question = (f"Η αίθουσα «{new}» υπάρχει ήδη.\n\n"
                        f"Να μεταφερθούν εκεί και οι {computers_text(len(current.computers))} "
                        f"της «{room}»; Οι δύο αίθουσες γίνονται μία.")
            if not self.confirm("Συγχώνευση αιθουσών", question, "Συγχώνευση"):
                return

        def apply() -> None:
            for computer in current.computers:
                self.model.change_room(computer.client_id, new)
        self._apply(apply)

    def revoke(self, client_id: str) -> None:
        computer = self._computer(client_id)
        if computer is None:
            return
        question = (f"Ανάκληση του «{computer.name}» ({computer.room});\n\n"
                    "Ο υπολογιστής αποσυνδέεται αμέσως. Για να ξαναμπεί χρειάζεται "
                    "νέα εγγραφή με νέο κλειδί.")
        if self.confirm("Ανάκληση υπολογιστή", question, "Ανάκληση"):
            self._apply(lambda: self.model.revoke(client_id))

    def _apply(self, action: Callable[[], object]) -> None:
        try:
            action()
        except ValueError as exc:
            self.warn(str(exc))
        self.refresh()

    # --- διάλογοι ----------------------------------------------------------
    def _ask_text(self, title: str, label: str, value: str) -> str | None:
        text, ok = QInputDialog.getText(self, title, label, QLineEdit.EchoMode.Normal, value)
        return text if ok else None

    def _confirm(self, title: str, text: str, action: str) -> bool:
        box = QMessageBox(QMessageBox.Icon.Warning, title, text,
                          QMessageBox.StandardButton.Cancel, self)
        accept = box.addButton(action, QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return box.clickedButton() is accept

    def _warn(self, text: str) -> None:
        QMessageBox.warning(self, PAGE_TITLE, text)

    # --- Κύριος / Εφεδρικός -------------------------------------------------
    def set_role(self, status: RoleStatus) -> None:
        """Από το παράθυρο σε κάθε ανανέωση ρόλου (φθηνό αν δεν άλλαξε κάτι)."""
        self.role = status
        if self.role_card is not None:
            self.role_card.update_status(status)
        locked = not status.primary
        closed = status.enrollment_closed
        if (locked, closed) == (self.locked, self.closed_reason):
            return
        self.locked, self.closed_reason = locked, closed
        self.requests_lock.setVisible(locked)
        self.computers_lock.setVisible(locked)
        self.closed_note.label.setText(closed or "")
        self.closed_card.setVisible(closed is not None)
        self.requests_box.setVisible(closed is None)
        self.request_count.setVisible(closed is None)
        if closed is not None:
            self.no_requests.hide()
            self.port_notice.hide()
        self.side_by_side = None   # νέο ελάχιστο πλάτος αριστερά → ξανά διάταξη
        self._apply_lock()
        self.refresh()

    def _apply_lock(self) -> None:
        """Στον Εφεδρικό κανένα μενού ⋮ σε υπολογιστές και αίθουσες (απόφαση 23/11/2026)."""
        for widget in [*self.computer_rows.values(), *self.room_headers.values()]:
            widget.menu_button.setVisible(not self.locked)

    # --- ανανέωση ----------------------------------------------------------
    def refresh(self) -> None:
        if self.current_tab == TAB_CERTIFICATES:
            return  # η ταυτότητα διαβάζεται όταν ανοίγει το tab, όχι κάθε δευτερόλεπτο
        now = self.clock()
        if self.closed_reason is None:
            notice = self.model.port_notice()
            self.port_notice.setText(notice or "")
            self.port_notice.setVisible(bool(notice))
        self._refresh_requests(now)
        self._refresh_rooms(now)
        self._relayout()

    def _refresh_requests(self, now: float) -> None:
        requests = self.model.requests()
        text, warn = self.model.requests_status(len(requests))
        self.request_count.setText(text)
        _repolish(self.request_count, warn="true" if warn else "false")
        wanted = [r.request_id for r in requests]
        # Τα αιτήματα που απαντήθηκαν μένουν λίγο ορατά με το αποτέλεσμα.
        for rid, at in list(self.finished.items()):
            if now - at > RESULT_SECONDS or rid not in self.request_rows:
                del self.finished[rid]
        keep = wanted + [rid for rid in self.request_rows
                         if rid in self.finished and rid not in wanted]
        if keep != list(self.request_rows):
            views = {r.request_id: r for r in requests}
            old = self.request_rows
            self.request_rows = {}
            while self.requests_layout.count():
                self.requests_layout.takeAt(0)
            for rid in keep:
                row = old.pop(rid, None) or RequestRow(views[rid], self.approve, self.deny)
                self.requests_layout.addWidget(row)
                self.request_rows[rid] = row
            for row in old.values():
                row.deleteLater()
        for row in self.request_rows.values():
            row.set_meta(now)
        self.no_requests.setVisible(not self.request_rows and self.closed_reason is None)

    def _refresh_rooms(self, now: float) -> None:
        rooms = self.model.rooms()
        key = tuple((room.room, tuple((c.client_id, c.name) for c in room.computers))
                    for room in rooms)
        if key != self._rooms_key:
            self._rooms_key = key
            for card in self.room_cards.values():
                self.room_grid.removeWidget(card)
                card.deleteLater()
            self.room_cards, self.room_headers, self.computer_rows = {}, {}, {}
            for room in rooms:
                card = RoomCard(room, self)
                self.room_cards[room.room] = card
                self.room_headers[room.room] = card.header
                self.computer_rows.update(card.rows)
            self._room_order = None
            self._apply_lock()
        for room in rooms:
            self.room_headers[room.room].update_state(room, now)
            for computer in room.computers:
                self.computer_rows[computer.client_id].update_state(computer, now)
        self.totals.setText(self.model.totals(rooms) if rooms else "")
        self.no_computers.setVisible(not rooms)

    def visible_texts(self) -> list[str]:
        """Όλα τα κείμενα της σελίδας (για δοκιμές: ο κωδικός δεν εμφανίζεται ποτέ)."""
        texts = []
        for widget in self.findChildren(QWidget):
            for getter in ("text", "toolTip", "placeholderText"):
                value = getattr(widget, getter, None)
                if callable(value):
                    try:
                        texts.append(str(value()))
                    except TypeError:
                        pass
        return texts
