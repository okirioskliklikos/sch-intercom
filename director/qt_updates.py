"""Ρυθμίσεις → «Ενημερώσεις» (Φάση 5B3, εγκεκριμένο mockup 27/11/2026, ui-style.md §9).

Αριστερά η κάρτα «Πακέτο Client» (τρέχον/προηγούμενο, εισαγωγή .sicu, αυτόματες
ενημερώσεις, «Έναρξη τώρα σε όλους»), δεξιά η κάρτα «Υπολογιστές» με έκδοση και
κατάσταση ανά υπολογιστή. Η λογική είναι στο `director/updates_view.py`.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QMessageBox, QProgressBar, QSizePolicy,
                               QVBoxLayout, QWidget)

from director import updates_view as uv
from director.qt_gui import ElidedLabel, _repolish, px
from director.qt_maintenance import KeyValues, _button, _card, _Dialog, _label, _Busy
from director.qt_security import _lock_note, _menu_button, _menu_icon
from director.security import TEXT_PRIMARY_ONLY
from director.updates import UpdateError
from shared.ui import icons, theme

REFRESH_MS = 1000


def _signature_value() -> QWidget:
    """«🛡 έγκυρη» σε πράσινο (η υπογραφή ελέγχθηκε στην εισαγωγή)."""
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(px(5))
    icon = QLabel()
    icon.setPixmap(icons.pixmap("security", theme.CONNECTED, px(16), stroke_width=2.0))
    row.addWidget(icon, 0, Qt.AlignmentFlag.AlignVCenter)
    text = _label("", "kvValue", tone="ok")
    row.addWidget(text, 1)
    box.text = text
    return box


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class RoomPcLabel(ElidedLabel):
    """«αίθουσα · υπολογιστής» με την αίθουσα γκρι· κόβεται με «…» όταν δεν χωρά."""

    def __init__(self):
        super().__init__("")
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.prefix = ""

    def set_parts(self, room: str, name: str) -> None:
        self.prefix = f"{room} · "
        self.full_text = self.prefix + name
        self.setToolTip(self.full_text)
        self._elide()

    def _elide(self) -> None:
        text = self.fontMetrics().elidedText(
            self.full_text, Qt.TextElideMode.ElideRight, max(1, self.contentsRect().width()))
        if text.startswith(self.prefix):
            html = (f'<span style="color:{theme.TEXT_MUTED}">{_escape(self.prefix)}</span>'
                    + _escape(text[len(self.prefix):]))
        else:
            html = f'<span style="color:{theme.TEXT_MUTED}">{_escape(text)}</span>'
        if html != self.text():
            self.setText(html)


def _columns(name: QWidget, version: QWidget, state: QWidget, menu: QWidget) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setContentsMargins(0, px(4), 0, px(4))
    layout.setSpacing(px(10))
    version.setFixedWidth(px(theme.UPDATES_VERSION_COLUMN))
    state.setFixedWidth(px(theme.UPDATES_STATE_COLUMN))
    menu.setFixedWidth(px(26))
    layout.addWidget(name, 1)
    layout.addWidget(version)
    layout.addWidget(state)
    layout.addWidget(menu)
    return layout


def header_line() -> QWidget:
    head = QWidget()
    head.setObjectName("updHead")
    head.setLayout(_columns(_label("Υπολογιστής", "updHeadText"),
                            _label("Έκδοση", "updHeadText"),
                            _label("Κατάσταση", "updHeadText"), QWidget()))
    head.layout().setContentsMargins(0, 0, 0, px(4))
    return head


class ComputerLine(QFrame):
    """Γραμμή «αίθουσα · υπολογιστής | έκδοση | κατάσταση | ⋮» (γραμμή πάνω από κάθε μία)."""

    def __init__(self, panel: "UpdatesPanel", client_id: str):
        super().__init__()
        self.setObjectName("updRow")
        self.client_id = client_id
        # Ίδιο ύψος με ή χωρίς ⋮ (ο Εφεδρικός δεν έχει μενού).
        self.setMinimumHeight(px(26) + 2 * px(4) + 1)
        self.name = RoomPcLabel()
        self.version = _label("", "updVersion")
        state = QWidget()
        state_row = QHBoxLayout(state)
        state_row.setContentsMargins(0, 0, 0, 0)
        state_row.setSpacing(px(6))
        self.alert = QLabel()
        self.alert.setPixmap(icons.pixmap("alert", theme.LIVE, px(16), stroke_width=2.0))
        self.alert.hide()
        self.state = _label("", "updState")
        self.progress = QProgressBar()
        self.progress.setObjectName("updProgress")
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setFixedSize(px(theme.UPDATES_PROGRESS), px(6))
        self.progress.hide()
        state_row.addWidget(self.alert, 0, Qt.AlignmentFlag.AlignVCenter)
        state_row.addWidget(self.state, 0, Qt.AlignmentFlag.AlignVCenter)
        state_row.addSpacing(px(4))
        state_row.addWidget(self.progress, 0, Qt.AlignmentFlag.AlignVCenter)
        state_row.addStretch(1)
        self.start_action = QAction(_menu_icon("play", theme.PRIMARY), uv.TEXT_START)
        self.start_action.triggered.connect(lambda: panel.start_one(self.client_id))
        self.menu_button = _menu_button("Ενέργειες", [self.start_action])
        menu = QWidget()
        menu_row = QHBoxLayout(menu)
        menu_row.setContentsMargins(0, 0, 0, 0)
        menu_row.addWidget(self.menu_button)
        self.setLayout(_columns(self.name, self.version, state, menu))
        self._shown: uv.ComputerUpdate | None = None

    def show_row(self, row: uv.ComputerUpdate, *, menus: bool) -> None:
        if row != self._shown:
            self._shown = row
            self.name.set_parts(row.room, row.name)
            self.version.setText(row.version)
            self.state.setText(row.text.removeprefix("⚠ "))
            self.alert.setVisible(row.tone == "fail")
            _repolish(self.state, tone=row.tone)
            self.state.setToolTip(row.tooltip)
            self.alert.setToolTip(row.tooltip)
            self.progress.setVisible(row.progress is not None)
            if row.progress is not None:
                self.progress.setValue(row.progress)
            self.start_action.setEnabled(row.can_start)
            self.menu_button.setAccessibleName(f"Ενέργειες για {row.name}")
        self.menu_button.setVisible(menus)


class ImportPackageDialog(_Dialog):
    """«Εισαγωγή πακέτου»: τι εισάγεται (έκδοση, υπογραφή, αλλαγές) ή γιατί όχι."""

    def __init__(self, view: uv.ImportView, parent=None):
        super().__init__(uv.TEXT_IMPORT_TITLE, parent)
        self.body.addStrut(px(theme.DIALOG_WIDTH + 110) - 2 * px(15))
        self.view = view
        rows = KeyValues()
        for index, (label, text, tone) in enumerate(view.rows):
            key = f"row{index}"
            if label == "Υπογραφή":
                value = rows.add(key, label, _signature_value())
                value.text.setText(text)
            else:
                rows.add(key, label)
                rows.set(key, text, tone if tone != "bold" else None)
                if tone == "bold":
                    rows.values[key].setStyleSheet("font-weight: 600;")
                rows.values[key].setWordWrap(True)
        self.body.addLayout(rows)
        self.rows = rows
        if view.error:
            self.error = _label(view.error, "warnText")
            self.error.setWordWrap(True)
            self.body.addWidget(self.error)
            row = QHBoxLayout()
            row.addStretch(1)
            self.close_button = _button("Κλείσιμο", "primaryAction")
            self.close_button.clicked.connect(self.reject)
            row.addWidget(self.close_button)
            self.body.addSpacing(px(4))
            self.body.addLayout(row)
            self.accept_button = None
            return
        self.separator()
        self.note = _label(view.note or "", "infoNote")
        self.note.setWordWrap(True)
        self.body.addWidget(self.note)
        self.cancel, self.accept_button = self.footer("Εισαγωγή")
        self.accept_button.setIcon(icons.icon("import", theme.WHITE, size=px(theme.ICON),
                                              stroke_width=2.0))
        self.accept_button.setIconSize(QSize(px(theme.ICON), px(theme.ICON)))
        self.accept_button.clicked.connect(self.accept)


class UpdatesPanel(QWidget):
    """Το tab «Ενημερώσεις». ``choose_open``/``dialog_hook``/``inform`` για δοκιμές."""

    def __init__(self, model: uv.UpdatesModel, *,
                 choose_open: Callable[[QWidget, Path], Path | None] | None = None,
                 dialog_hook: Callable[[QDialog], None] | None = None,
                 inform: Callable[[str, str], None] | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("updatesBody")
        self.model = model
        self.choose_open = choose_open or self._choose_open
        self.dialog_hook = dialog_hook
        self.inform = inform or self._inform
        self.folder = Path.home()
        self.lines: dict[str, ComputerLine] = {}
        self.last_dialog: ImportPackageDialog | None = None
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, px(theme.SCROLL_GAP), 0)
        self.grid.setSpacing(px(theme.CARD_SPACING))
        self.package_card = self._build_package_card()
        self.package_card.setFixedWidth(px(theme.UPDATES_PACKAGE_CARD))
        self.computers_card = self._build_computers_card()
        self.side_by_side: bool | None = None
        self._arrange(True)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.refresh()

    # --- διάταξη -------------------------------------------------------------
    def _arrange(self, side: bool) -> None:
        if side == self.side_by_side:
            return
        self.side_by_side = side
        for card in (self.package_card, self.computers_card):
            self.grid.removeWidget(card)
        top = Qt.AlignmentFlag.AlignTop
        if side:
            self.grid.addWidget(self.package_card, 0, 0, top)
            self.grid.addWidget(self.computers_card, 0, 1, top)
            self.grid.setColumnStretch(1, 1)
        else:
            self.grid.addWidget(self.package_card, 0, 0, top | Qt.AlignmentFlag.AlignLeft)
            self.grid.addWidget(self.computers_card, 1, 0, top)
            self.grid.setColumnStretch(1, 0)
        self.grid.setRowStretch(2, 1)

    def fit_width(self, width: int) -> None:
        """Πλάτος της σελίδας Ρυθμίσεις: δίπλα-δίπλα μόνο όταν χωρά ο πίνακας."""
        needed = px(theme.UPDATES_PACKAGE_CARD + theme.CARD_SPACING
                    + theme.UPDATES_COMPUTERS_MIN + theme.SCROLL_GAP + theme.SCROLLBAR)
        self._arrange(width >= needed)

    def showEvent(self, event) -> None:
        self.refresh()
        self.timer.start(REFRESH_MS)
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self.timer.stop()
        super().hideEvent(event)

    # --- κάρτα «Πακέτο Client» -------------------------------------------------------
    def _build_package_card(self) -> QFrame:
        card, layout = _card(uv.TITLE_PACKAGE)
        head = layout.itemAt(0).widget()
        layout.removeWidget(head)
        title_row = QHBoxLayout()
        title_row.setSpacing(px(10))
        title_row.addWidget(head)
        title_row.addStretch(1)
        self.package_lock = _lock_note(TEXT_PRIMARY_ONLY)
        self.package_lock.hide()
        title_row.addWidget(self.package_lock, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.insertLayout(0, title_row)
        self.info = KeyValues()
        self.info.add("current", "Τρέχουσα")
        self.info.add("imported", "Εισαγωγή")
        self.info.add("previous", "Προηγούμενη")
        self.signature = self.info.add("signature", "Υπογραφή", _signature_value())
        self.signature.text.setText(uv.TEXT_SIGNATURE)
        layout.addLayout(self.info)
        self.missing = _label("", "alarmNote")
        self.missing.setTextFormat(Qt.TextFormat.RichText)
        self.missing.setWordWrap(True)
        self.missing.hide()
        layout.addWidget(self.missing)
        layout.addSpacing(px(4))
        self.auto = QCheckBox(uv.TEXT_AUTO)
        self.auto.toggled.connect(self._auto_changed)
        layout.addWidget(self.auto)
        hint = _label(uv.TEXT_AUTO_HINT, "small")
        hint.setIndent(px(23))
        layout.addWidget(hint)
        layout.addSpacing(px(6))
        buttons = QHBoxLayout()
        buttons.setSpacing(px(8))
        buttons.addStretch(1)
        self.import_button = _button(uv.TEXT_IMPORT, "secondaryAction", "import")
        self.import_button.clicked.connect(self.open_import)
        self.start_button = _button(uv.TEXT_START_ALL, "primaryAction", "play", theme.WHITE)
        self.start_button.clicked.connect(self.start_all)
        buttons.addWidget(self.import_button)
        buttons.addWidget(self.start_button)
        layout.addLayout(buttons)
        return card

    # --- κάρτα «Υπολογιστές» --------------------------------------------------------
    def _build_computers_card(self) -> QFrame:
        card, layout = _card(uv.TITLE_COMPUTERS)
        head = layout.itemAt(0).widget()
        layout.removeWidget(head)
        title_row = QHBoxLayout()
        title_row.setSpacing(px(16))
        title_row.addWidget(head)
        self.counters = _label("", "updCounters")
        self.counters.setTextFormat(Qt.TextFormat.RichText)
        self.counters.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.counters.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title_row.addWidget(self.counters, 1, Qt.AlignmentFlag.AlignVCenter)
        layout.insertLayout(0, title_row)
        table = QVBoxLayout()
        table.setSpacing(0)
        table.addWidget(header_line())
        self.table = QVBoxLayout()
        self.table.setSpacing(0)
        table.addLayout(self.table)
        layout.addLayout(table)
        self.empty = _label("Δεν υπάρχουν εγκεκριμένοι υπολογιστές.", "muted")
        self.empty.hide()
        layout.addWidget(self.empty)
        self.backup_note = _label(uv.TEXT_BACKUP_NOTE, "small")
        self.backup_note.setWordWrap(True)
        self.backup_note.hide()
        layout.addWidget(self.backup_note)
        return card

    # --- ανανέωση ------------------------------------------------------------------
    def refresh(self) -> None:
        try:
            info = self.model.package()
            rows = self.model.computers()
            primary = self.model.primary()
        except (OSError, ValueError) as exc:
            print(f"Ενημερώσεις: {exc}", flush=True)
            return
        imported = info.current is not None
        if imported:
            self.info.set("current", info.current)
            self.info.values["current"].setStyleSheet("font-weight: 600;")
            self.info.set("imported", info.imported or "—")
        else:
            self.info.set("current", uv.TEXT_NOT_IMPORTED, "muted")
            self.info.values["current"].setStyleSheet("")
        self.info.set("previous", info.previous)
        self.info.set_visible("imported", imported)
        self.info.set_visible("signature", imported and info.missing is None)
        self.missing.setVisible(info.missing is not None)
        text = _escape(info.missing or "")
        if info.missing_file:
            text = text.replace(_escape(info.missing_file), f"<b>{_escape(info.missing_file)}</b>")
        self.missing.setText(text)
        if self.auto.isChecked() != info.auto:
            self.auto.blockSignals(True)
            self.auto.setChecked(info.auto)
            self.auto.blockSignals(False)
        self.package_lock.setVisible(not primary)
        self.auto.setEnabled(primary)
        self.import_button.setEnabled(primary)
        self.start_button.setVisible(imported)
        self.start_button.setEnabled(primary and imported and info.missing is None)
        self.backup_note.setVisible(not primary)
        self.counters.setText("&nbsp;&nbsp;&nbsp;&nbsp;".join(
            f"<b>{count}</b> {label}"
            for count, label in self.model.counters(rows, imported=imported)))
        self._show_rows(rows, menus=primary and imported)

    def _show_rows(self, rows: list[uv.ComputerUpdate], *, menus: bool) -> None:
        ids = [row.client_id for row in rows]
        if ids != list(self.lines):
            for line in self.lines.values():
                self.table.removeWidget(line)
                line.deleteLater()
            self.lines = {}
            for client_id in ids:
                line = ComputerLine(self, client_id)
                self.table.addWidget(line)
                self.lines[client_id] = line
        for row in rows:
            self.lines[row.client_id].show_row(row, menus=menus)
        self.empty.setVisible(not rows)

    # --- ενέργειες -----------------------------------------------------------------
    def _auto_changed(self, enabled: bool) -> None:
        self._act(lambda: self.model.set_auto(enabled))

    def start_all(self) -> None:
        self._act(self.model.start_all)

    def start_one(self, client_id: str) -> None:
        self._act(lambda: self.model.start(client_id))

    def _act(self, action: Callable[[], None]) -> None:
        try:
            action()
        except (UpdateError, OSError) as exc:
            self.inform(uv.TITLE_PACKAGE, str(exc))
        self.refresh()

    def _choose_open(self, parent: QWidget, current: Path) -> Path | None:
        path, _ = QFileDialog.getOpenFileName(parent, uv.TEXT_IMPORT_TITLE, str(current),
                                              uv.PACKAGE_FILTER)
        return Path(path) if path else None

    def open_import(self) -> None:
        chosen = self.choose_open(self, self.folder)
        if chosen is None:
            return
        self.folder = chosen.parent
        with _Busy():
            view = self.model.inspect(chosen)
        dialog = ImportPackageDialog(view, self)
        self.last_dialog = dialog
        accepted = self._exec(dialog)
        if not accepted or view.error:
            return
        try:
            with _Busy():
                self.model.import_package(view.path)
        except (UpdateError, OSError) as exc:
            self.inform(uv.TEXT_IMPORT_TITLE, str(exc))
        self.refresh()

    def _exec(self, dialog: QDialog) -> bool:
        if self.dialog_hook is not None:
            self.dialog_hook(dialog)
            return dialog.result() == QDialog.DialogCode.Accepted
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _inform(self, title: str, text: str) -> None:
        QMessageBox.information(self, title, text)
