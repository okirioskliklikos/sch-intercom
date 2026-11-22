"""Σελίδα «Ασφάλεια» του Director σε PySide6 (docs/ui-style.md §6).

Μόνο εμφάνιση και σύνδεση σημάτων· η λογική είναι στο `director/security.py`.
Tab «Υπολογιστές»: «Νέες εγγραφές» (θύρα, αιτήματα, κωδικός, Έγκριση/Απόρριψη)
και «Εγγεγραμμένοι υπολογιστές» (ανά αίθουσα, μενού ⋯). Το tab «Πιστοποιητικά»
φαίνεται ανενεργό μέχρι να σχεδιαστεί.
"""
from __future__ import annotations

import time
from collections.abc import Callable

from PySide6.QtCore import QRegularExpression, QSize, Qt
from PySide6.QtGui import QAction, QColor, QFontDatabase, QPalette, QRegularExpressionValidator
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMenu,
                               QMessageBox, QPushButton, QScrollArea, QSizePolicy,
                               QToolButton, QVBoxLayout, QWidget)

from director.qt_gui import ElidedLabel, StatusDot, _card, _repolish, px
from director.security import (CODE_LENGTH, ComputerView, Decision, RequestView,
                               RoomView, SecurityModel, computers_text)
from shared.ui import icons, theme

PAGE_TITLE = "Ασφάλεια"
TAB_COMPUTERS = "Υπολογιστές"
TAB_CERTIFICATES = "Πιστοποιητικά"
PORT_OPEN = "Θύρα εγγραφής ανοιχτή"
PORT_CLOSED = "Θύρα εγγραφής κλειστή"
NO_REQUESTS = "Δεν υπάρχουν νέες εγγραφές"
NO_COMPUTERS = "Δεν υπάρχουν εγγεγραμμένοι υπολογιστές"
#: Πόσο μένει ορατό το αποτέλεσμα ενός αιτήματος που έκλεισε.
RESULT_SECONDS = 15.0


def _separator() -> QFrame:
    line = QFrame()
    line.setObjectName("separator")
    line.setFixedHeight(1)
    return line


def _label(text: str, name: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName(name)
    return label


class RequestRow(QWidget):
    """Αίτημα σε τρεις γραμμές: «Αίθουσα · Υπολογιστής», ID/χρόνοι, κωδικός + κουμπιά."""

    def __init__(self, request: RequestView, on_approve: Callable[[str, str], None],
                 on_deny: Callable[[str], None], parent=None):
        super().__init__(parent)
        self.request = request
        self.final = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, px(9), 0, px(10))
        layout.setSpacing(px(6))
        self.title = _label(request.title, "requestTitle")
        self.title.setWordWrap(True)
        self.meta = _label("", "small")
        layout.addWidget(self.title)
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
        digits = self.code.fontMetrics().horizontalAdvance("0" * (CODE_LENGTH + 1))
        self.code.setFixedWidth(digits + px(22))

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
        actions.addWidget(self.code)
        actions.addWidget(self.approve)
        actions.addWidget(self.deny)
        actions.addStretch(1)
        layout.addWidget(self.actions)

        self.result = _label("", "result")
        self.result.setWordWrap(True)
        self.result.hide()
        layout.addWidget(self.result)

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
        _repolish(self.result, ok="true" if decision.ok else "false")
        self.result.show()
        if decision.final:
            self.final = True
            self.actions.hide()
            self.title.setEnabled(False)
        else:
            self.code.clear()
            self.code.setFocus()


class ComputerRow(QWidget):
    """Υπολογιστής με εσοχή: κουκκίδα, όνομα, κατάσταση, μενού «⋯» τέρμα δεξιά."""

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
        self.menu_button = QToolButton()
        self.menu_button.setObjectName("rowMenu")
        self.menu_button.setText("⋯")
        self.menu_button.setAccessibleName(f"Ενέργειες για {computer.name}")
        self.menu_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.menu_button)
        cid = computer.client_id
        self.actions = {
            "room": QAction("Αλλαγή αίθουσας…", menu),
            "rename": QAction("Μετονομασία…", menu),
            "revoke": QAction("Ανάκληση…", menu),
        }
        self.actions["room"].triggered.connect(lambda: page.change_room(cid))
        self.actions["rename"].triggered.connect(lambda: page.rename(cid))
        self.actions["revoke"].triggered.connect(lambda: page.revoke(cid))
        menu.addAction(self.actions["room"])
        menu.addAction(self.actions["rename"])
        menu.addSeparator()
        menu.addAction(self.actions["revoke"])
        self.menu_button.setMenu(menu)
        layout.addWidget(self.dot)
        layout.addWidget(self.name)
        layout.addWidget(self.state)
        layout.addStretch(1)
        layout.addWidget(self.menu_button)

    def update_state(self, computer: ComputerView, now: float) -> None:
        self.computer = computer
        self.dot.set_color(theme.ROOM_DOT_ONLINE if computer.online else theme.ROOM_DOT_OFFLINE)
        text = computer.state(now)
        if self.state.text() != text:
            self.state.setText(text)


class RoomHeader(QWidget):
    def __init__(self, room: RoomView, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, px(4), 0, 0)
        layout.setSpacing(px(9))
        self.setMinimumHeight(px(32))
        self.dot = StatusDot(theme.ROOM_DOT_OFFLINE)
        self.name = ElidedLabel(room.room)
        self.name.setObjectName("roomTitle")
        self.count = _label(computers_text(len(room.computers)), "small")
        layout.addWidget(self.dot)
        layout.addWidget(self.name, 1)
        layout.addWidget(self.count)

    def update_state(self, room: RoomView, now: float) -> None:
        self.dot.set_color(theme.ROOM_DOT_ONLINE if room.online else theme.ROOM_DOT_OFFLINE)
        # Για την αίθουσα μετρά ο πιο πρόσφατος υπολογιστής της.
        self.setToolTip("Online" if room.online else f"Τελευταία σύνδεση: {room.state(now)}")


class SecurityPage(QWidget):
    """Σελίδα «Ασφάλεια». `refresh()` καλείται από το παράθυρο όσο είναι ορατή.

    Οι διάλογοι είναι αντικαταστάσιμοι (`ask_text`, `confirm`, `warn`) για
    δοκιμές χωρίς χρήστη.
    """

    def __init__(self, model: SecurityModel, *, clock: Callable[[], float] = time.time,
                 ask_text: Callable[[str, str, str], str | None] | None = None,
                 confirm: Callable[[str, str], bool] | None = None,
                 warn: Callable[[str], None] | None = None, parent=None):
        super().__init__(parent)
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
        self.tab_computers.clicked.connect(lambda: self.tab_computers.setChecked(True))
        self.tab_certificates.setEnabled(False)
        self.tab_certificates.setToolTip("Σχεδιάζεται σε επόμενο βήμα")
        layout.addLayout(tabs)

        body = QWidget()
        body.setObjectName("securityBody")
        self.columns = QGridLayout(body)
        self.columns.setContentsMargins(0, 0, 0, 0)
        self.columns.setSpacing(px(theme.CARD_SPACING))
        self._build_requests_card()
        self._build_computers_card()
        self.side_by_side: bool | None = None
        self._column_minimum = -1
        self._arrange(True)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(body)
        layout.addWidget(self.scroll, 1)

    # --- διάταξη ------------------------------------------------------------
    def _card_minimum(self) -> int:
        """Το ελάχιστο πλάτος της πιο απαιτητικής κάρτας (κεφαλίδες, κουμπιά)."""
        return max(self.requests_card.minimumSizeHint().width(),
                   self.computers_card.minimumSizeHint().width())

    def _arrange(self, side_by_side: bool) -> None:
        """Δύο κάρτες δίπλα-δίπλα με ίσο πλάτος· σε στενό παράθυρο η μία κάτω από την άλλη.

        Το ίσο πλάτος στηρίζεται στο ελάχιστο της πιο απαιτητικής κάρτας, ώστε οι
        κάρτες να μην αλλάζουν πλάτος όταν μπαίνουν/φεύγουν αιτήματα ή υπολογιστές.
        """
        minimum = self._card_minimum() if side_by_side else 0
        if side_by_side == self.side_by_side and minimum == self._column_minimum:
            return
        self._column_minimum = minimum
        for column in (0, 1):
            self.columns.setColumnMinimumWidth(column, minimum)
        if side_by_side == self.side_by_side:
            return
        self.side_by_side = side_by_side
        top = Qt.AlignmentFlag.AlignTop
        for card in (self.requests_card, self.computers_card):
            self.columns.removeWidget(card)
        self.columns.addWidget(self.requests_card, 0, 0, top)
        if side_by_side:
            self.columns.addWidget(self.computers_card, 0, 1, top)
            self.columns.setColumnStretch(0, 1)
            self.columns.setColumnStretch(1, 1)
            self.columns.setRowStretch(0, 1)
            self.columns.setRowStretch(1, 0)
        else:
            self.columns.addWidget(self.computers_card, 1, 0, top)
            self.columns.setColumnStretch(0, 1)
            self.columns.setColumnStretch(1, 0)
            self.columns.setRowStretch(0, 0)
            self.columns.setRowStretch(1, 1)

    def two_columns_fit(self, width: int) -> bool:
        needed = (2 * self._card_minimum() + self.columns.horizontalSpacing()
                  + px(theme.SCROLLBAR) + px(6))
        return width >= needed

    def _relayout(self) -> None:
        """Ξανά μετά από αλλαγή περιεχομένου: νέο αίτημα/υπολογιστής αλλάζει το ελάχιστο."""
        self._arrange(self.two_columns_fit(self.width()))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._arrange(self.two_columns_fit(event.size().width()))

    # --- κατασκευή ---------------------------------------------------------
    def _build_requests_card(self) -> QWidget:
        head = QWidget()
        row = QHBoxLayout(head)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(px(8))
        row.addWidget(_label("Νέες εγγραφές", "cardTitle"))
        row.addStretch(1)
        self.port_dot = StatusDot(theme.ROOM_DOT_OFFLINE)
        self.port_label = _label(PORT_CLOSED, "portState")
        self.port_button = QPushButton()
        self.port_button.setObjectName("pillSmall")
        self.port_button.setIconSize(QSize(px(theme.ICON), px(theme.ICON)))
        self.port_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.port_button.clicked.connect(self.toggle_port)
        row.addWidget(self.port_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.port_label)
        row.addSpacing(px(2))
        row.addWidget(self.port_button)
        card, layout = _card(head)
        self.requests_card = card
        self.requests_layout = QVBoxLayout()
        self.requests_layout.setContentsMargins(0, 0, 0, 0)
        self.requests_layout.setSpacing(0)
        self.no_requests = _label(NO_REQUESTS, "muted")
        layout.addWidget(self.no_requests)
        layout.addLayout(self.requests_layout)
        return card

    def _build_computers_card(self) -> QWidget:
        head = QWidget()
        row = QHBoxLayout(head)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(_label("Εγγεγραμμένοι υπολογιστές", "cardTitle"))
        row.addStretch(1)
        self.totals = _label("", "small")
        row.addWidget(self.totals)
        card, layout = _card(head)
        self.computers_card = card
        self.rooms_layout = QVBoxLayout()
        self.rooms_layout.setContentsMargins(0, 0, 0, 0)
        self.rooms_layout.setSpacing(0)
        self.no_computers = _label(NO_COMPUTERS, "muted")
        layout.addWidget(self.no_computers)
        layout.addLayout(self.rooms_layout)
        return card

    # --- ενέργειες ---------------------------------------------------------
    def toggle_port(self) -> None:
        error = self.model.set_port_open(not self.model.port_open)
        if error:
            self.warn(error)
        self.refresh()

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

    def revoke(self, client_id: str) -> None:
        computer = self._computer(client_id)
        if computer is None:
            return
        question = (f"Ανάκληση του «{computer.name}» ({computer.room});\n\n"
                    "Ο υπολογιστής αποσυνδέεται αμέσως. Για να ξαναμπεί χρειάζεται "
                    "νέα εγγραφή με νέο κλειδί.")
        if self.confirm("Ανάκληση υπολογιστή", question):
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

    def _confirm(self, title: str, text: str) -> bool:
        box = QMessageBox(QMessageBox.Icon.Warning, title, text,
                          QMessageBox.StandardButton.Cancel, self)
        revoke = box.addButton("Ανάκληση", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return box.clickedButton() is revoke

    def _warn(self, text: str) -> None:
        QMessageBox.warning(self, PAGE_TITLE, text)

    # --- ανανέωση ----------------------------------------------------------
    def refresh(self) -> None:
        now = self.clock()
        self._refresh_port()
        self._refresh_requests(now)
        self._refresh_rooms(now)
        self._relayout()

    def _refresh_port(self) -> None:
        is_open = self.model.port_open
        self.port_dot.set_color(theme.ONLINE if is_open else theme.ROOM_DOT_OFFLINE)
        self.port_label.setText(PORT_OPEN if is_open else PORT_CLOSED)
        self.port_button.setText("Κλείσιμο" if is_open else "Άνοιγμα")
        self.port_button.setIcon(icons.icon("lock" if is_open else "unlock", theme.TEXT,
                                            size=px(theme.ICON),
                                            stroke_width=theme.ACTION_ICON_STROKE))

    def _refresh_requests(self, now: float) -> None:
        requests = self.model.requests()
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
                item = self.requests_layout.takeAt(0)
                widget = item.widget()
                if widget is not None and not isinstance(widget, RequestRow):
                    widget.deleteLater()
            for index, rid in enumerate(keep):
                row = old.pop(rid, None) or RequestRow(views[rid], self.approve, self.deny)
                if index:
                    self.requests_layout.addWidget(_separator())
                self.requests_layout.addWidget(row)
                self.request_rows[rid] = row
            for row in old.values():
                row.deleteLater()
        for row in self.request_rows.values():
            row.set_meta(now)
        self.no_requests.setVisible(not self.request_rows)

    def _refresh_rooms(self, now: float) -> None:
        rooms = self.model.rooms()
        key = tuple((room.room, tuple((c.client_id, c.name) for c in room.computers))
                    for room in rooms)
        if key != self._rooms_key:
            self._rooms_key = key
            while self.rooms_layout.count():
                widget = self.rooms_layout.takeAt(0).widget()
                if widget is not None:
                    widget.deleteLater()
            self.room_headers, self.computer_rows = {}, {}
            for room in rooms:
                header = RoomHeader(room)
                self.room_headers[room.room] = header
                self.rooms_layout.addWidget(header)
                for computer in room.computers:
                    row = ComputerRow(computer, self)
                    self.computer_rows[computer.client_id] = row
                    self.rooms_layout.addWidget(row)
        for room in rooms:
            self.room_headers[room.room].update_state(room, now)
            for computer in room.computers:
                self.computer_rows[computer.client_id].update_state(computer, now)
        self.totals.setText(self.model.totals(rooms))
        self.totals.setVisible(bool(rooms))
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
