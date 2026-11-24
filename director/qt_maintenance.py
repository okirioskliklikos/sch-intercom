"""Ασφάλεια → «Πιστοποιητικά» και Ρυθμίσεις → «Συντήρηση» σε PySide6 (ui-style.md §7–§8).

Μόνο εμφάνιση και σύνδεση σημάτων· η λογική είναι στο `director/maintenance.py` και
το backup/restore στο `director/backup.py` (ίδιες συναρτήσεις με το CLI).

Επαναφορά μέσα από το GUI: ο Manager σταματά να εξυπηρετεί Clients και αφήνει το
`manager.lock`, γίνεται η επαναφορά και η εφαρμογή ξεκινά ξανά από την αρχή, ώστε να
φορτώσει τα νέα αρχεία όπως σε κάθε εκκίνηση. Οι διάλογοι αρχείων και μηνυμάτων είναι
αντικαταστάσιμοι για δοκιμές χωρίς χρήστη.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QFontDatabase, QGuiApplication
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QDialog, QFileDialog,
                               QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout, QLineEdit,
                               QMessageBox, QPushButton, QScrollArea, QStackedWidget, QToolButton,
                               QVBoxLayout, QWidget)

from director import maintenance as m
from director import registry_sync
from director.backup import EXTENSION, BackupError
from director.qt_gui import (ElidedLabel, _repolish, current_ui_scale, fit_tab_text,
                             load_layout_settings, load_ui_scale, px, save_layout_settings)
from shared.ui import icons, theme

SETTINGS_TITLE = "Ρυθμίσεις"
TAB_MAINTENANCE = "Συντήρηση"
TAB_APPEARANCE = "Εμφάνιση"
SCALE_TITLE = "Κλίμακα περιβάλλοντος"
SCALE_NOTE = "Ισχύει μετά από επανεκκίνηση"
RESTART_NOW = "Επανεκκίνηση τώρα"
IDENTITY_TITLE = "Ταυτότητα Manager"
BACKUP_TITLE = "Backup"
RESTORE_TITLE = "Επαναφορά"
CREATE_BACKUP = "Δημιουργία backup…"
OPEN_RESTORE = "Επαναφορά από backup…"
DELETE_PREVIOUS = "Διαγραφή"
COPY = "Αντιγραφή"
COPIED = "Αντιγράφηκε ✓"
BACKUP_FILTER = "Backup School Intercom (*.sicb)"
REGISTRY_TITLE = "Μητρώο υπολογιστών"
IMPORT = "Εισαγωγή…"
EXPORT = "Εξαγωγή…"
IMPORT_TITLE = "Εισαγωγή μητρώου"
PREFER_LOCAL = "Αυτού του υπολογιστή"
PREFER_FILE = "Του αρχείου"


def _label(text: str = "", name: str | None = None, *, tone: str | None = None) -> QLabel:
    label = QLabel(text)
    if name:
        label.setObjectName(name)
    if tone is not None:
        label.setProperty("tone", tone)
    return label


def _button(text: str, kind: str, icon: str | None = None, color: str | None = None,
            stroke: float = 2.0) -> QPushButton:
    """kind: primaryAction (γεμάτο primary), secondaryAction (λευκό), deny (κάδος, live)."""
    button = QPushButton(text)
    button.setObjectName(kind)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if icon:
        button.setIcon(icons.icon(icon, color or theme.TEXT, disabled_color=theme.TEXT_MUTED,
                                  size=px(theme.ICON), stroke_width=stroke))
        button.setIconSize(QSize(px(theme.ICON), px(theme.ICON)))
    return button


def _remove_button(text: str) -> QPushButton:
    return _button(text, "deny", "trash", theme.LIVE, theme.TRASH_ICON_STROKE)


class KeyValues(QGridLayout):
    """Ετικέτα (text_muted) αριστερά, τιμή δεξιά· οι τιμές αλλάζουν με `set`."""

    def __init__(self):
        super().__init__()
        self.setContentsMargins(0, 0, 0, 0)
        self.setHorizontalSpacing(px(theme.KV_GAP))
        self.setVerticalSpacing(px(9))
        self.setColumnStretch(1, 1)
        self.values: dict[str, QWidget] = {}
        self.labels: dict[str, QLabel] = {}

    def add(self, key: str, label: str, widget: QWidget | None = None) -> QWidget:
        row = self.rowCount()
        name = _label(label, "kvLabel")
        self.addWidget(name, row, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        value = widget if widget is not None else _label("", "kvValue")
        self.addWidget(value, row, 1)
        self.labels[key] = name
        self.values[key] = value
        return value

    def set(self, key: str, text: str, tone: str | None = None) -> None:
        value = self.values[key]
        if isinstance(value, ElidedLabel):
            _set_elided(value, text)
        else:
            value.setText(text)
        _repolish(value, tone=tone or "")

    def set_visible(self, key: str, visible: bool) -> None:
        self.labels[key].setVisible(visible)
        self.values[key].setVisible(visible)


def _set_elided(label: ElidedLabel, text: str) -> None:
    """Νέο πλήρες κείμενο: κόβεται με «…» μόνο αν δεν χωρά· ολόκληρο στο tooltip."""
    label.full_text = text
    label.setToolTip(text)
    label.setText(text)
    label._elide()


def _card(title: str) -> tuple[QFrame, QVBoxLayout]:
    card = QFrame()
    card.setObjectName("card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(px(15), px(12), px(15), px(15))
    layout.setSpacing(px(10))
    layout.addWidget(_label(title, "cardTitle"))
    return card, layout


def _scroll(body: QWidget) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(body)
    return scroll


# --- Ασφάλεια → Πιστοποιητικά -------------------------------------------------
class CertificatesPanel(QWidget):
    """Μία κάρτα «Ταυτότητα Manager» (απόφαση χρήστη 22/11/2026: μόνο η ταυτότητα)."""

    def __init__(self, identity: Callable[[], m.ManagerIdentity], *,
                 extra: QWidget | None = None, parent=None):
        super().__init__(parent)
        self._identity = identity
        self.identity: m.ManagerIdentity | None = None
        body = QWidget()
        body.setObjectName("maintenanceBody")
        outer = QVBoxLayout(body)
        outer.setContentsMargins(0, 0, px(theme.SCROLL_GAP), 0)
        card, layout = _card(IDENTITY_TITLE)
        card.setMaximumWidth(px(theme.IDENTITY_CARD_MAX))
        self.card = card
        self.rows = KeyValues()
        self.rows.add("name", "Όνομα")
        fingerprint_row = QWidget()
        row = QHBoxLayout(fingerprint_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(px(12))
        self.fingerprint = _label("", "fingerprint")
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        mono.setPointSizeF(self.font().pointSizeF() * 0.96)
        self.fingerprint.setFont(mono)
        # Πάντα ολόκληρο σε δύο γραμμές των 16 bytes (και κρυμμένο: ίδιο μήκος με «••»).
        self.fingerprint.setMinimumWidth(
            self.fingerprint.fontMetrics().horizontalAdvance("00:" * 15 + "00") + px(4))
        self.fingerprint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.copy_button = QPushButton(COPY)
        self.copy_button.setObjectName("pill")
        self.copy_button.setIcon(icons.icon("copy", theme.TEXT, size=px(theme.ICON)))
        self.copy_button.setIconSize(QSize(px(theme.ICON), px(theme.ICON)))
        self.copy_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_button.clicked.connect(self.copy_fingerprint)
        self.reveal_button = QToolButton()
        self.reveal_button.setObjectName("rowMenu")
        self.reveal_button.setCheckable(True)
        # Μάτι 24px, κάθετα στο κέντρο με την «Αντιγραφή» (απόφαση χρήστη 23/11/2026).
        self.reveal_button.setIconSize(QSize(px(theme.EYE_ICON), px(theme.EYE_ICON)))
        self.reveal_button.setFixedSize(px(theme.EYE_ICON + 8), px(theme.EYE_ICON + 8))
        self.reveal_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reveal_button.toggled.connect(self._show_fingerprint)
        row.addWidget(self.fingerprint, 1)
        row.addWidget(self.reveal_button, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.copy_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.rows.add("fingerprint", "Αποτύπωμα SHA-256", fingerprint_row)
        self.rows.setAlignment(self.rows.labels["fingerprint"],
                               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.rows.add("created", "Δημιουργήθηκε")
        self.rows.add("expires", "Λήξη")
        self.rows.add("key", "Κλειδί", self._pair("key"))
        self.rows.add("registry", "Μητρώο", self._pair("registry"))
        folder = ElidedLabel("")
        folder.setObjectName("kvValue")
        self.rows.add("folder", "Φάκελος", folder)
        layout.addLayout(self.rows)
        outer.setSpacing(px(theme.CARD_SPACING))
        outer.addWidget(card)
        # Κάρτα «Ρόλος Manager» κάτω από την ταυτότητα (4.2D2).
        if extra is not None:
            outer.addWidget(extra)
        outer.addStretch(1)
        page = QVBoxLayout(self)
        page.setContentsMargins(0, 0, 0, 0)
        page.addWidget(_scroll(body))
        self._show_fingerprint(False)
        self._copied_timer = QTimer(self)
        self._copied_timer.setSingleShot(True)
        self._copied_timer.timeout.connect(lambda: self.copy_button.setText(COPY))

    def _pair(self, key: str) -> QWidget:
        """«κείμενο · κατάσταση», η κατάσταση σε ok/err χρώμα, όλα αριστερά (σχόλιο 23/11/2026)."""
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(px(6))
        main = _label("", "kvValue")
        state = _label("", "kvValue")
        dot = _label("·", "kvValue")
        row.addWidget(main)
        row.addWidget(dot)
        row.addWidget(state)
        row.addStretch(1)
        setattr(self, f"{key}_dot", dot)
        setattr(self, f"{key}_text", main)
        setattr(self, f"{key}_state", state)
        return widget

    def refresh(self) -> None:
        try:
            identity = self._identity()
        except (OSError, ValueError) as exc:
            self.rows.set("name", f"Σφάλμα: {exc}", "err")
            return
        self.identity = identity
        self.rows.set("name", identity.name)
        self._show_fingerprint(self.reveal_button.isChecked())
        self.rows.set("created", m.date_text(identity.created))
        self.rows.set("expires", identity.expires_text())
        self.key_text.setText(identity.algorithm)
        self.key_state.setText(identity.key_text())
        _repolish(self.key_state, tone="ok" if identity.key_ok else "err")
        self.registry_text.setText(identity.registry_text())
        self.registry_state.setText(m.TEXT_SIGNATURE_OK if identity.signature_ok
                                    else m.TEXT_SIGNATURE_BAD)
        shown = identity.registry_version is not None or not identity.signature_ok
        self.registry_state.setVisible(shown)
        self.registry_dot.setVisible(shown)
        _repolish(self.registry_state, tone="ok" if identity.signature_ok else "err")
        self.rows.set("folder", str(identity.folder))

    def _show_fingerprint(self, shown: bool) -> None:
        """Μάτι: προεπιλογή κρυμμένο (σχόλιο 23/11/2026)· η Αντιγραφή δίνει πάντα όλο."""
        name = "eye-off" if shown else "eye"
        self.reveal_button.setIcon(icons.icon(name, theme.TEXT_MUTED, size=px(theme.EYE_ICON)))
        self.reveal_button.setToolTip("Απόκρυψη αποτυπώματος" if shown
                                      else "Εμφάνιση αποτυπώματος")
        self.reveal_button.setAccessibleName(self.reveal_button.toolTip())
        if self.identity is None:
            self.fingerprint.setText("")
            return
        fingerprint = self.identity.fingerprint
        self.fingerprint.setText(m.fingerprint_lines(fingerprint) if shown
                                 else m.fingerprint_masked(fingerprint))

    def copy_fingerprint(self) -> None:
        if self.identity is None:
            return
        QGuiApplication.clipboard().setText(self.identity.fingerprint)
        self.copy_button.setText(COPIED)
        self._copied_timer.start(2000)


# --- Διάλογοι ------------------------------------------------------------------
class _Dialog(QDialog):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("maintenanceDialog")
        self.setWindowTitle(title)
        self.setModal(True)
        self.body = QVBoxLayout(self)
        # Το ύψος ακολουθεί το περιεχόμενο (σύνοψη/προειδοποιήσεις που εμφανίζονται).
        self.body.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        self.body.addStrut(px(theme.DIALOG_WIDTH) - 2 * px(15))  # ελάχιστο πλάτος
        self.body.setContentsMargins(px(15), px(14), px(15), px(15))
        self.body.setSpacing(px(11))

    def field(self, label: str, widget: QWidget, button: QWidget | None = None) -> QWidget:
        box = QVBoxLayout()
        box.setSpacing(px(4))
        box.addWidget(_label(label, "fieldLabel"))
        row = QHBoxLayout()
        row.setSpacing(px(8))
        row.addWidget(widget, 1)
        if button is not None:
            row.addWidget(button)
        box.addLayout(row)
        self.body.addLayout(box)
        return widget

    def separator(self) -> QFrame:
        line = QFrame()
        line.setObjectName("dialogSeparator")
        line.setFixedHeight(1)
        self.body.addWidget(line)
        return line

    def footer(self, accept_text: str) -> tuple[QPushButton, QPushButton]:
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = _button("Άκυρο", "secondaryAction")
        accept = _button(accept_text, "primaryAction")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        row.addWidget(accept)
        self.body.addSpacing(px(4))
        self.body.addLayout(row)
        return cancel, accept


def _line_edit(*, password: bool = False) -> QLineEdit:
    edit = QLineEdit()
    edit.setObjectName("field")
    if password:
        edit.setEchoMode(QLineEdit.EchoMode.Password)
    return edit


def _eye_action(edit: QLineEdit) -> QAction:
    """Εμφάνιση/απόκρυψη κωδικού (εικονίδιο ματιού μέσα στο πεδίο)."""
    action = edit.addAction(icons.icon("eye", theme.TEXT_MUTED, size=px(theme.ICON)),
                            QLineEdit.ActionPosition.TrailingPosition)
    action.setToolTip("Εμφάνιση κωδικού")
    action.setCheckable(True)

    def toggle(shown: bool) -> None:
        edit.setEchoMode(QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password)

    action.toggled.connect(toggle)
    return action


class _Busy:
    """Κέρσορας αναμονής όσο τρέχει το scrypt (κάτω από 1 s) ή η αντιγραφή MP3."""

    def __enter__(self):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()

    def __exit__(self, *_exc):
        QApplication.restoreOverrideCursor()


class BackupDialog(_Dialog):
    def __init__(self, model: m.MaintenanceModel, folder: Path, *,
                 choose_path: Callable[[QWidget, Path], Path | None] | None = None,
                 parent=None):
        super().__init__("Δημιουργία backup", parent)
        self.model = model
        self.choose_path = choose_path or self._choose_path
        self.created: tuple[Path, list[str]] | None = None
        self.path = _line_edit()
        self.path.setText(str(Path(folder) / m.default_backup_name()))
        change = _button("Αλλαγή…", "pill")
        change.clicked.connect(self._change)
        self.field("Αρχείο", self.path, change)
        self.password = _line_edit(password=True)
        self.eye = _eye_action(self.password)
        self.field(f"Κωδικός (τουλάχιστον {m.PASSPHRASE_MIN} χαρακτήρες)", self.password)
        self.confirm = _line_edit(password=True)
        self.status = _label("", "fieldStatus")
        self.status.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.field("Επιβεβαίωση κωδικού", self.confirm)
        self.body.addWidget(self.status, 0, Qt.AlignmentFlag.AlignRight)
        count, size = model.media_files()
        self.media = QCheckBox("Αντίγραφο φακέλου MP3 δίπλα στο backup")
        self.media.setChecked(True)
        self.media_detail = _label(
            f"{m._plural(count, 'αρχείο', 'αρχεία')} · {m.size_text(size)} · χωρίς κρυπτογράφηση",
            "small")
        self.media_detail.setIndent(px(23))
        self.media_separator = self.separator()
        self.body.addWidget(self.media)
        self.body.addWidget(self.media_detail)
        self.has_media = count > 0
        for widget in (self.media_separator, self.media, self.media_detail):
            widget.setVisible(self.has_media)
        self.error = _label("", "warnText")
        self.error.setWordWrap(True)
        self.error.hide()
        self.body.addWidget(self.error)
        reminder = _label(m.TEXT_SECRET_REMINDER, "warnText")
        reminder.setWordWrap(True)
        self.body.addWidget(reminder)
        self.cancel, self.create = self.footer("Δημιουργία")
        self.create.clicked.connect(self.run)
        for edit in (self.path, self.password, self.confirm):
            edit.textChanged.connect(self._validate)
        self._validate()

    def _choose_path(self, parent: QWidget, current: Path) -> Path | None:
        path, _ = QFileDialog.getSaveFileName(parent, "Αρχείο backup", str(current), BACKUP_FILTER)
        return Path(path) if path else None

    def _change(self) -> None:
        chosen = self.choose_path(self, Path(self.path.text()))
        if chosen is not None:
            if chosen.suffix.lower() != EXTENSION:
                chosen = chosen.with_name(chosen.name + EXTENSION)
            self.path.setText(str(chosen))

    def _validate(self) -> None:
        first, second = self.password.text(), self.confirm.text()
        problem = m.passphrase_problem(first, second)
        if not first and not second:
            text, tone = "", None
        elif problem == m.TEXT_PASS_SHORT:
            text, tone = problem, "muted"
        elif problem and not second:
            text, tone = "", None
        elif problem:
            text, tone = problem, "err"
        else:
            text, tone = m.TEXT_PASS_MATCH, "ok"
        self.status.setText(text)
        _repolish(self.status, tone=tone or "")
        self.create.setEnabled(problem is None and bool(self.path.text().strip()))

    def run(self) -> None:
        self.error.hide()
        try:
            with _Busy():
                self.created = self.model.create_backup(
                    Path(self.path.text().strip()), self.password.text(), self.confirm.text(),
                    copy_media=self.has_media and self.media.isChecked())
        except (BackupError, OSError, ValueError) as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        self.password.clear()
        self.confirm.clear()
        self.accept()


class RestoreDialog(_Dialog):
    def __init__(self, model: m.MaintenanceModel, folder: Path, *,
                 choose_path: Callable[[QWidget, Path], Path | None] | None = None,
                 parent=None):
        super().__init__("Επαναφορά από backup", parent)
        self.body.addStrut(px(theme.DIALOG_WIDTH + 30) - 2 * px(15))
        self.model = model
        self.folder = Path(folder)
        self.choose_path = choose_path or self._choose_path
        self.info: dict | None = None
        self.path = _line_edit()
        change = _button("Αλλαγή…", "pill")
        change.clicked.connect(self._change)
        self.field("Αρχείο", self.path, change)
        self.password = _line_edit(password=True)
        self.open_button = _button("Άνοιγμα", "pill")
        self.open_button.clicked.connect(self.load)
        self.password.returnPressed.connect(self.load)
        self.field("Κωδικός", self.password, self.open_button)
        # Ίδιο πλάτος κουμπιών → τα δύο πεδία στοιχίζονται δεξιά (σχόλιο 23/11/2026).
        width = max(change.sizeHint().width(), self.open_button.sizeHint().width())
        change.setFixedWidth(width)
        self.open_button.setFixedWidth(width)
        self.error = _label("", "warnText")
        self.error.setWordWrap(True)
        self.error.hide()
        self.body.addWidget(self.error)

        self.summary = QWidget()
        summary = QVBoxLayout(self.summary)
        summary.setContentsMargins(0, 0, 0, 0)
        summary.setSpacing(px(11))
        line = QFrame()
        line.setObjectName("dialogSeparator")
        line.setFixedHeight(1)
        summary.addWidget(line)
        self.rows = KeyValues()
        for key, text in (("created", "Δημιουργήθηκε"), ("registry", "Μητρώο"),
                          ("identity", "Ταυτότητα"), ("mp3", "MP3")):
            self.rows.add(key, text)
        # Checkbox στη στήλη των τιμών, ακριβώς κάτω από το «N αρχεία» (σχόλιο 23/11/2026).
        media = QWidget()
        media_box = QVBoxLayout(media)
        media_box.setContentsMargins(0, 0, 0, 0)
        media_box.setSpacing(px(1))
        self.media = QCheckBox("Αντιγραφή των MP3 που λείπουν")
        self.media.setChecked(True)
        self.media_detail = _label("", "small")
        self.media_detail.setIndent(px(23))
        media_box.addWidget(self.media)
        media_box.addWidget(self.media_detail)
        self.rows.add("media", "", media)
        summary.addLayout(self.rows)
        self.warning_line = QFrame()
        self.warning_line.setObjectName("dialogSeparator")
        self.warning_line.setFixedHeight(1)
        summary.addWidget(self.warning_line)
        self.warning = _label("", "warnText")
        self.warning.setWordWrap(True)
        summary.addWidget(self.warning)
        self.consent = QCheckBox("")
        summary.addWidget(self.consent)
        note = _label(m.TEXT_RESTART_NOTE, "small")
        note.setWordWrap(True)
        summary.addWidget(note)
        self.summary.hide()
        self.body.addWidget(self.summary)

        self.cancel, self.restore = self.footer("Επαναφορά και επανεκκίνηση")
        self.restore.clicked.connect(self._confirm)
        self.consent.toggled.connect(self._validate)
        self.path.textChanged.connect(self._reset)
        self.password.textChanged.connect(self._reset)
        self._validate()

    def _choose_path(self, parent: QWidget, current: Path) -> Path | None:
        path, _ = QFileDialog.getOpenFileName(parent, "Αρχείο backup", str(current), BACKUP_FILTER)
        return Path(path) if path else None

    def _change(self) -> None:
        current = Path(self.path.text()) if self.path.text() else self.folder
        chosen = self.choose_path(self, current)
        if chosen is not None:
            self.path.setText(str(chosen))
            self.password.setFocus()

    def _reset(self) -> None:
        """Νέο αρχείο ή κωδικός: η σύνοψη δεν ισχύει πια."""
        if self.info is not None:
            self.info = None
            self.summary.hide()
            self.adjustSize()
        self.error.hide()
        self._validate()

    def load(self) -> None:
        self.error.hide()
        path = Path(self.path.text().strip())
        try:
            with _Busy():
                info = self.model.preview(path, self.password.text())
        except (BackupError, OSError, ValueError, KeyError) as exc:
            self.info = None
            self.summary.hide()
            self.error.setText(str(exc) if isinstance(exc, BackupError) else f"Σφάλμα: {exc}")
            self.error.show()
            self._validate()
            self.adjustSize()
            return
        self.info = info
        rows = dict(zip(("created", "registry", "identity", "mp3"), m.preview_rows(info)))
        for key, (_label_text, text, tone) in rows.items():
            self.rows.set(key, text, tone)
        self.rows.set_visible("mp3", "mp3" in rows)
        copy = info["media_copy_available"]
        self.rows.set_visible("media", copy)
        self.media_detail.setText("από " + path.stem + "-media")
        warning = m.preview_warning(info)
        for widget in (self.warning_line, self.warning, self.consent):
            widget.setVisible(warning is not None)
        self.consent.setChecked(False)
        if warning:
            self.warning.setText(warning[0])
            self.consent.setText(warning[1])
        self.summary.show()
        self._validate()
        self.adjustSize()

    def _validate(self) -> None:
        ready = self.info is not None
        if ready and m.preview_warning(self.info) is not None:
            ready = self.consent.isChecked()
        self.restore.setEnabled(ready)
        self.open_button.setEnabled(bool(self.path.text().strip()) and bool(self.password.text()))

    def request(self) -> m.RestoreRequest | None:
        if self.info is None:
            return None
        consent = self.consent.isChecked()
        return m.RestoreRequest(
            Path(self.path.text().strip()), self.password.text(),
            replace_identity=consent and self.info["identity"] == "different",
            accept_older=consent and self.info["relation"] in ("older", "conflict"),
            restore_media=bool(self.info["media_copy_available"]) and self.media.isChecked())

    def _confirm(self) -> None:
        if self.request() is not None:
            self.accept()


class ImportDialog(_Dialog):
    """«Εισαγωγή μητρώου» (.sicr): έλεγχος, τι αλλάζει, και σε απόκλιση επιλογή για τις
    διαφορές («Αυτού του υπολογιστή» / «Του αρχείου»)· εγκεκριμένο mockup 23/11/2026."""

    def __init__(self, model: m.MaintenanceModel, folder: Path, *,
                 choose_path: Callable[[QWidget, Path], Path | None] | None = None,
                 parent=None):
        super().__init__(IMPORT_TITLE, parent)
        self.body.addStrut(px(theme.DIALOG_WIDTH + 60) - 2 * px(15))
        self.model = model
        self.folder = Path(folder)
        self.choose_path = choose_path or self._choose_path
        self.view: m.ImportView | None = None
        self.result_info: dict | None = None
        self.prefer = "local"
        self.path = _line_edit()
        self.path.setReadOnly(True)
        change = _button("Αλλαγή…", "pill")
        change.clicked.connect(self._change)
        self.field("Αρχείο", self.path, change)
        self.error = _label("", "warnText")
        self.error.setWordWrap(True)
        self.error.hide()
        self.body.addWidget(self.error)

        self.summary = QWidget()
        summary = QVBoxLayout(self.summary)
        summary.setContentsMargins(0, 0, 0, 0)
        summary.setSpacing(px(11))
        line = QFrame()
        line.setObjectName("dialogSeparator")
        line.setFixedHeight(1)
        summary.addWidget(line)
        self.rows = KeyValues()
        version = QWidget()
        pair = QHBoxLayout(version)
        pair.setContentsMargins(0, 0, 0, 0)
        pair.setSpacing(px(6))
        self.version_text = _label("", "kvValue")
        self.version_state = _label("", "kvValue")
        pair.addWidget(self.version_text)
        pair.addWidget(self.version_state)
        pair.addStretch(1)
        self.rows.add("registry", "Μητρώο", version)
        self.rows.add("signature", "Υπογραφή")
        summary.addLayout(self.rows)
        # Ίδια στήλη ετικετών με τις γραμμές αλλαγών (ξεχωριστό grid που ξαναφτιάχνεται).
        metrics = self.rows.labels["registry"].fontMetrics()
        self.label_width = max(metrics.horizontalAdvance(text) for text in (
            "Μητρώο", "Υπογραφή", "Προστίθενται", "Ανακαλούνται", "Αλλάζουν αίθουσα",
            "Μετονομάζονται", "Αλλάζουν κλειδί", "Διαφορές")) + px(2)
        self.rows.setColumnMinimumWidth(0, self.label_width)
        self.changes_box = QWidget()
        self.changes_layout = QVBoxLayout(self.changes_box)
        self.changes_layout.setContentsMargins(0, 0, 0, 0)
        summary.addWidget(self.changes_box)
        self.current = _label(m.TEXT_ALREADY_CURRENT, "small")
        summary.addWidget(self.current)

        self.prefer_box = QWidget()
        prefer = QVBoxLayout(self.prefer_box)
        prefer.setContentsMargins(0, 0, 0, 0)
        prefer.setSpacing(px(6))
        prefer.addWidget(_label("Σε διαφορές κρατείται", "fieldLabel"))
        toggle = QHBoxLayout()
        toggle.setSpacing(px(6))
        self.prefer_buttons: dict[str, QPushButton] = {}
        for key, text in (("local", PREFER_LOCAL), ("incoming", PREFER_FILE)):
            button = QPushButton(text)
            button.setObjectName("tab")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            fit_tab_text(button)
            button.clicked.connect(lambda _=False, k=key: self.set_prefer(k))
            toggle.addWidget(button)
            self.prefer_buttons[key] = button
        toggle.addStretch(1)
        prefer.addLayout(toggle)
        note = _label(m.TEXT_REVOCATIONS_KEEP, "warnText")
        note.setWordWrap(True)
        prefer.addWidget(note)
        summary.addWidget(self.prefer_box)
        self.summary.hide()
        self.body.addWidget(self.summary)
        self.cancel, self.apply_button = self.footer("Εισαγωγή")
        self.apply_button.clicked.connect(self.run)
        self.set_prefer("local", load=False)
        self._validate()

    def _choose_path(self, parent: QWidget, current: Path) -> Path | None:
        path, _ = QFileDialog.getOpenFileName(parent, IMPORT_TITLE, str(current),
                                              m.REGISTRY_FILTER)
        return Path(path) if path else None

    def _change(self) -> None:
        current = Path(self.path.text()) if self.path.text() else self.folder
        chosen = self.choose_path(self, current)
        if chosen is not None:
            self.open(chosen)

    def open(self, path: Path) -> None:
        self.path.setText(str(path))
        self.load()

    def set_prefer(self, prefer: str, *, load: bool = True) -> None:
        self.prefer = prefer
        for key, button in self.prefer_buttons.items():
            button.setChecked(key == prefer)
        if load and self.view is not None:
            self.load()

    def load(self) -> None:
        self.error.hide()
        try:
            view = self.model.import_view(Path(self.path.text()), prefer=self.prefer)
        except (registry_sync.SyncError, OSError, ValueError, KeyError) as exc:
            self.view = None
            self.summary.hide()
            self.error.setText(str(exc) if isinstance(exc, registry_sync.SyncError)
                               else f"Σφάλμα: {exc}")
            self.error.show()
            self._validate()
            self.adjustSize()
            return
        self.view = view
        main, _sep, state = view.version_text.rpartition(" · ")
        self.version_text.setText(main + " ·" if main else "")
        self.version_text.setVisible(bool(main))
        self.version_state.setText(state)
        _repolish(self.version_state, tone=view.version_tone or "")
        self.rows.set("signature", m.TEXT_SAME_MANAGER, "ok")
        old = self.changes_box
        self.changes_box = QWidget()
        rows = KeyValues()
        rows.setColumnMinimumWidth(0, self.label_width)
        for title, items in view.changes:
            value = _label("\n".join(items), "kvValue")
            value.setWordWrap(True)
            rows.add(title, title, value)
        QVBoxLayout(self.changes_box).setContentsMargins(0, 0, 0, 0)
        self.changes_box.layout().addLayout(rows)
        self.summary.layout().replaceWidget(old, self.changes_box)
        old.deleteLater()
        self.changes_box.setVisible(bool(view.changes))
        self.current.setVisible(view.relation == "same")
        conflict = view.relation == "conflict"
        self.prefer_box.setVisible(conflict)
        self.apply_button.setText("Συγχώνευση" if conflict else "Εισαγωγή")
        self.summary.show()
        self._validate()
        self.adjustSize()

    def _validate(self) -> None:
        self.apply_button.setEnabled(self.view is not None and self.view.relation != "same")

    def run(self) -> None:
        if self.view is None:
            return
        self.error.hide()
        try:
            with _Busy():
                self.result_info = self.model.apply_import(self.view)
        except (registry_sync.SyncError, OSError, ValueError) as exc:
            self.error.setText(str(exc))
            self.error.show()
            self.adjustSize()
            return
        self.accept()


class RestoreProgress(_Dialog):
    """Βήματα επαναφοράς: ✓ ολοκληρώθηκε, ● τρέχει, κενό = δεν ξεκίνησε."""

    def __init__(self, parent=None):
        super().__init__("Επαναφορά από backup", parent)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self.steps: list[tuple[QLabel, QLabel]] = []
        for text in m.RESTORE_STEPS:
            row = QHBoxLayout()
            row.setSpacing(px(10))
            mark = _label("", "kvValue")
            mark.setFixedWidth(px(16))
            label = _label(text)
            row.addWidget(mark)
            row.addWidget(label, 1)
            self.body.addLayout(row)
            self.steps.append((mark, label))

    def set_step(self, index: int) -> None:
        for number, (mark, label) in enumerate(self.steps):
            if number < index:
                mark.setText("✓")
                _repolish(mark, tone="ok")
            elif number == index:
                mark.setText("●")
                _repolish(mark, tone="")
            else:
                mark.setText("")
            font = label.font()
            font.setWeight(font.Weight.DemiBold if number == index else font.Weight.Normal)
            label.setFont(font)
        QApplication.processEvents()


def run_restore(parent: QWidget | None, model: m.MaintenanceModel, request: m.RestoreRequest, *,
                stop_serving: Callable[[], None], restart: Callable[[], None],
                warn: Callable[[str], None], progress: RestoreProgress | None = None) -> bool:
    """Σταματά την εξυπηρέτηση, κάνει την επαναφορά και ζητά επανεκκίνηση.

    Και σε αποτυχία ο Manager ξεκινά ξανά: το restore επαναφέρει μόνο του τα
    προηγούμενα αρχεία (4.2C §6.2), οπότε η επανεκκίνηση συνεχίζει με αυτά.
    Επιστρέφει True αν η επαναφορά πέτυχε.
    """
    progress = progress or RestoreProgress(parent)
    progress.show()
    progress.set_step(0)
    ok = False
    try:
        stop_serving()
        progress.set_step(1)
        model.restore(request)
        ok = True
        progress.set_step(2)
    except (BackupError, OSError, ValueError, KeyError) as exc:
        warn(f"Η επαναφορά απέτυχε: {exc}\n\nΟ Manager ξεκινά ξανά με τα προηγούμενα δεδομένα.")
    finally:
        restart()
    return ok


# --- Ρυθμίσεις → Συντήρηση ----------------------------------------------------
class SettingsPage(QWidget):
    """Σελίδα «Ρυθμίσεις»: tabs «Εμφάνιση» (κλίμακα περιβάλλοντος, 24/11/2026) και
    «Συντήρηση» (απόφαση χρήστη 22/11/2026)."""

    def __init__(self, model: m.MaintenanceModel, *,
                 start_restore: Callable[[m.RestoreRequest], None] | None = None,
                 restore_blocked: Callable[[], str | None] = lambda: None,
                 choose_save: Callable[[QWidget, Path], Path | None] | None = None,
                 choose_open: Callable[[QWidget, Path], Path | None] | None = None,
                 confirm: Callable[[str, str, str], bool] | None = None,
                 inform: Callable[[str, str], None] | None = None,
                 dialog_hook: Callable[[QDialog], None] | None = None,
                 layout_settings: Path | None = None,
                 restart_app: Callable[[], None] | None = None,
                 ui_scale: int | None = None, parent=None):
        super().__init__(parent)
        self.model = model
        self.start_restore = start_restore
        self.restore_blocked = restore_blocked
        self.choose_save = choose_save
        self.choose_open = choose_open
        self.confirm = confirm or self._confirm
        self.inform = inform or self._inform
        # Για δοκιμές: συμπληρώνει τον διάλογο αντί για χρήστη (αλλιώς exec()).
        self.dialog_hook = dialog_hook
        self.layout_settings = layout_settings
        self.side_by_side: bool | None = None
        # Κλίμακα με την οποία τρέχει τώρα η εφαρμογή και αυτή που έχει επιλεγεί.
        self.restart_app = restart_app
        self.applied_scale = ui_scale or current_ui_scale()
        self.chosen_scale = (load_ui_scale(layout_settings) if layout_settings is not None
                             else self.applied_scale)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(px(theme.CARD_SPACING))
        tabs = QHBoxLayout()
        self.tab_buttons: dict[str, QPushButton] = {}
        for name in (TAB_APPEARANCE, TAB_MAINTENANCE):
            button = QPushButton(name)
            button.setObjectName("tab")
            button.setCheckable(True)
            fit_tab_text(button)
            button.clicked.connect(lambda _=False, name=name: self.show_tab(name))
            tabs.addWidget(button)
            self.tab_buttons[name] = button
        self.tab_appearance = self.tab_buttons[TAB_APPEARANCE]
        self.tab_maintenance = self.tab_buttons[TAB_MAINTENANCE]
        tabs.addStretch(1)
        layout.addLayout(tabs)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)

        appearance = QWidget()
        appearance.setObjectName("appearanceBody")
        column = QVBoxLayout(appearance)
        column.setContentsMargins(0, 0, px(theme.SCROLL_GAP), 0)
        column.setSpacing(px(theme.CARD_SPACING))
        self.scale_card = self._build_scale_card()
        self.scale_card.setFixedWidth(px(theme.MAINTENANCE_CARD))
        column.addWidget(self.scale_card, 0, Qt.AlignmentFlag.AlignLeft)
        column.addStretch(1)
        self.appearance_scroll = _scroll(appearance)
        self.stack.addWidget(self.appearance_scroll)

        body = QWidget()
        body.setObjectName("maintenanceBody")
        self.grid = QGridLayout(body)
        self.grid.setContentsMargins(0, 0, px(theme.SCROLL_GAP), 0)
        self.grid.setSpacing(px(theme.CARD_SPACING))
        self.backup_card = self._build_backup_card()
        self.restore_card = self._build_restore_card()
        self.registry_card = self._build_registry_card()
        for card in (self.backup_card, self.restore_card, self.registry_card):
            card.setFixedWidth(px(theme.MAINTENANCE_CARD))
        self._arrange(True)
        self.scroll = _scroll(body)
        self.stack.addWidget(self.scroll)
        self.show_tab(TAB_APPEARANCE)

    # --- tabs ----------------------------------------------------------------
    def show_tab(self, name: str) -> None:
        for key, button in self.tab_buttons.items():
            button.setChecked(key == name)
        self.stack.setCurrentIndex(0 if name == TAB_APPEARANCE else 1)

    @property
    def current_tab(self) -> str:
        return TAB_APPEARANCE if self.stack.currentIndex() == 0 else TAB_MAINTENANCE

    # --- Εμφάνιση: κλίμακα περιβάλλοντος -------------------------------------
    def _build_scale_card(self) -> QFrame:
        card, layout = _card(SCALE_TITLE)
        row = QHBoxLayout()
        row.setSpacing(px(8))
        self.scale_group = QButtonGroup(self)
        self.scale_group.setExclusive(True)
        self.scale_buttons: dict[int, QPushButton] = {}
        for value in theme.UI_SCALES:
            button = QPushButton(f"{value}%")
            button.setObjectName("tab")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            fit_tab_text(button)
            button.setMinimumWidth(max(button.minimumWidth(), px(theme.SCALE_OPTION)))
            button.setChecked(value == self.chosen_scale)
            self.scale_group.addButton(button, value)
            self.scale_buttons[value] = button
            row.addWidget(button)
        row.addStretch(1)
        self.scale_group.idClicked.connect(self.choose_scale)
        layout.addLayout(row)
        self.scale_note_row = QWidget()
        note_row = QHBoxLayout(self.scale_note_row)
        note_row.setContentsMargins(0, px(8), 0, 0)
        self.scale_note = _label(SCALE_NOTE, "muted")
        self.restart_button = _button(RESTART_NOW, "primaryAction", "restart", theme.WHITE)
        self.restart_button.clicked.connect(self._restart_now)
        note_row.addWidget(self.scale_note)
        note_row.addStretch(1)
        note_row.addWidget(self.restart_button)
        layout.addWidget(self.scale_note_row)
        self._refresh_scale()
        return card

    def choose_scale(self, value: int) -> None:
        """Αποθηκεύει την κλίμακα στο director-gui.json· ισχύει στην επόμενη εκκίνηση."""
        if value not in theme.UI_SCALES:
            return
        self.chosen_scale = value
        self.scale_buttons[value].setChecked(True)
        save_layout_settings(self.layout_settings, {"ui_scale": value})
        self._refresh_scale()

    def _refresh_scale(self) -> None:
        pending = self.chosen_scale != self.applied_scale
        self.scale_note_row.setVisible(pending)
        self.restart_button.setVisible(pending and self.restart_app is not None)

    def set_restart_allowed(self, allowed: bool) -> None:
        """Όχι επανεκκίνηση όσο υπάρχει ζωντανή ανακοίνωση (DirectorWindow.refresh)."""
        if self.restart_button.isEnabled() != allowed:
            self.restart_button.setEnabled(allowed)

    def _restart_now(self) -> None:
        if self.restart_app is not None and self.restart_button.isEnabled():
            self.restart_app()

    # --- κατασκευή ---------------------------------------------------------
    def _build_backup_card(self) -> QFrame:
        card, layout = _card(BACKUP_TITLE)
        self.backup_rows = KeyValues()
        self.backup_rows.add("when", "Τελευταίο")
        file_name = ElidedLabel("")
        file_name.setObjectName("kvValue")
        self.backup_rows.add("file", "Αρχείο", file_name)
        self.backup_rows.add("changes", "Από τότε")
        layout.addLayout(self.backup_rows)
        layout.addSpacing(px(4))
        layout.addStretch(1)   # κουμπί στο κάτω μέρος: ίδια ευθεία με την «Επαναφορά»
        row = QHBoxLayout()
        self.create_button = _button(CREATE_BACKUP, "primaryAction", "backup", theme.WHITE)
        self.create_button.clicked.connect(self.open_backup)
        row.addStretch(1)   # κουμπιά τέρμα δεξιά (σχόλιο 23/11/2026)
        row.addWidget(self.create_button)
        layout.addLayout(row)
        return card

    def _build_restore_card(self) -> QFrame:
        card, layout = _card(RESTORE_TITLE)
        self.restore_rows = KeyValues()
        self.restore_rows.add("when", "Τελευταία")
        previous = QWidget()
        box = QVBoxLayout(previous)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(px(1))
        self.previous_name = ElidedLabel("")
        self.previous_name.setObjectName("kvValue")
        self.previous_hint = _label(m.TEXT_PREVIOUS_HINT, "small")
        box.addWidget(self.previous_name)
        box.addWidget(self.previous_hint)
        self.restore_rows.add("previous", "Προηγούμενα αρχεία", previous)
        layout.addLayout(self.restore_rows)
        layout.addSpacing(px(4))
        layout.addStretch(1)
        row = QHBoxLayout()
        row.setSpacing(px(8))
        self.restore_button = _button(OPEN_RESTORE, "secondaryAction", "restore")
        self.restore_button.clicked.connect(self.open_restore)
        self.delete_button = _remove_button(DELETE_PREVIOUS)
        self.delete_button.clicked.connect(self.delete_previous)
        row.addStretch(1)
        row.addWidget(self.delete_button)   # αριστερά, για να μη μετακινείται η Επαναφορά
        row.addWidget(self.restore_button)
        layout.addLayout(row)
        return card

    def _build_registry_card(self) -> QFrame:
        """«Μητρώο υπολογιστών»: εξαγωγή/εισαγωγή .sicr ανάμεσα σε Κύριο και Εφεδρικό."""
        card, layout = _card(REGISTRY_TITLE)
        self.registry_rows = KeyValues()
        self.registry_rows.add("registry", "Μητρώο")
        self.registry_rows.add("last", "Τελευταία")
        self.registry_rows.add("unexported", "Από τότε")
        layout.addLayout(self.registry_rows)
        layout.addSpacing(px(4))
        layout.addStretch(1)
        row = QHBoxLayout()
        row.setSpacing(px(8))
        self.import_button = _button(IMPORT, "secondaryAction", "import")
        self.import_button.clicked.connect(self.open_import)
        self.export_button = _button(EXPORT, "primaryAction", "export", theme.WHITE)
        self.export_button.clicked.connect(self.export_registry)
        row.addStretch(1)
        row.addWidget(self.import_button)
        row.addWidget(self.export_button)
        layout.addLayout(row)
        return card

    def _arrange(self, side_by_side: bool) -> None:
        if side_by_side == self.side_by_side:
            return
        self.side_by_side = side_by_side
        for card in (self.backup_card, self.restore_card, self.registry_card):
            self.grid.removeWidget(card)
        # Δίπλα-δίπλα: ίδιο ύψος, κοινή κάτω ευθεία (σχόλιο 23/11/2026).
        align = Qt.AlignmentFlag.AlignLeft if side_by_side \
            else Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        self.grid.addWidget(self.backup_card, 0, 0, align)
        self.grid.addWidget(self.restore_card, 0 if side_by_side else 1,
                            1 if side_by_side else 0, align)
        top = Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        self.grid.addWidget(self.registry_card, 1 if side_by_side else 2, 0, top)
        for column in range(3):
            self.grid.setColumnStretch(column, 0)
        for row in range(4):
            self.grid.setRowStretch(row, 0)
        self.grid.setColumnStretch(2 if side_by_side else 1, 1)
        self.grid.setRowStretch(2 if side_by_side else 3, 1)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        needed = 2 * px(theme.MAINTENANCE_CARD) + px(theme.CARD_SPACING) \
            + px(theme.SCROLL_GAP) + px(theme.SCROLLBAR)
        self._arrange(self.width() >= needed)

    # --- ανανέωση ----------------------------------------------------------
    def refresh(self) -> None:
        last = self.model.last_backup()
        if last is None:
            self.backup_rows.set("when", "—")
            self.backup_rows.set_visible("file", False)
            self.backup_rows.set_visible("changes", False)
        else:
            self.backup_rows.set("when", last["when"])
            self.backup_rows.set("file", last["file_name"])
            self.backup_rows.set_visible("file", True)
            changes = last["changes"]
            self.backup_rows.set_visible("changes", changes is not None)
            if changes is not None:
                self.backup_rows.set("changes", m.changes_text(changes),
                                     "ok" if changes == 0 else "err")
        restored = self.model.last_restore()
        self.restore_rows.set("when", restored["when"] if restored else "—")
        previous = self.model.previous_folders()
        if previous:
            text = previous[0].name + (f" (+{len(previous) - 1})" if len(previous) > 1 else "")
            _repolish(self.previous_name, tone="err")
            _set_elided(self.previous_name, text)
            self.previous_name.setToolTip("\n".join(str(p) for p in previous))
        else:
            _repolish(self.previous_name, tone="muted")
            _set_elided(self.previous_name, "—")
        self.previous_hint.setVisible(bool(previous))
        self.delete_button.setVisible(bool(previous))
        self._refresh_registry()

    def _refresh_registry(self) -> None:
        try:
            info = self.model.registry_card()
        except (OSError, ValueError) as exc:
            self.registry_rows.set("registry", f"Σφάλμα: {exc}", "err")
            return
        self.registry_rows.set("registry", info["registry"])
        self.registry_rows.set("last", info["last_export"] or "—")
        unexported = info["unexported"]
        self.registry_rows.set_visible("unexported", unexported is not None)
        if unexported is not None:
            self.registry_rows.set("unexported", *unexported)

    # --- φάκελος backup ----------------------------------------------------
    def _folder(self) -> Path:
        saved = load_layout_settings(self.layout_settings).get("backup_folder")
        if isinstance(saved, str) and Path(saved).is_dir():
            return Path(saved)
        documents = Path.home() / "Documents"
        return documents if documents.is_dir() else Path.home()

    def _remember_folder(self, path: Path) -> None:
        save_layout_settings(self.layout_settings, {"backup_folder": str(Path(path).parent)})

    def _show(self, dialog: QDialog) -> int:
        if self.dialog_hook is not None:
            self.dialog_hook(dialog)
            return dialog.result()
        return dialog.exec()

    # --- ενέργειες ---------------------------------------------------------
    def open_backup(self) -> None:
        dialog = BackupDialog(self.model, self._folder(), choose_path=self.choose_save,
                              parent=self)
        self.last_dialog = dialog
        if self._show(dialog) == QDialog.DialogCode.Accepted and dialog.created:
            path, warnings = dialog.created
            self._remember_folder(path)
            self.refresh()
            self.inform(BACKUP_TITLE, "\n\n".join([f"Backup: {path}", *warnings,
                                                   m.TEXT_SECRET_REMINDER]))

    def open_restore(self) -> None:
        blocked = self.restore_blocked()
        if blocked:
            self.inform(RESTORE_TITLE, blocked)
            return
        dialog = RestoreDialog(self.model, self._folder(), choose_path=self.choose_open,
                               parent=self)
        self.last_dialog = dialog
        if self._show(dialog) != QDialog.DialogCode.Accepted:
            return
        request = dialog.request()
        dialog.password.clear()
        blocked = self.restore_blocked()
        if blocked:
            self.inform(RESTORE_TITLE, blocked)
            return
        if request is not None and self.start_restore is not None:
            self._remember_folder(request.path)
            self.start_restore(request)

    def open_import(self, path: Path | None = None) -> None:
        self.show_tab(TAB_MAINTENANCE)
        dialog = ImportDialog(self.model, self._folder(), choose_path=self.choose_open,
                              parent=self)
        self.last_dialog = dialog
        if path is not None:
            dialog.open(Path(path))
        if self._show(dialog) == QDialog.DialogCode.Accepted and dialog.view is not None:
            self._remember_folder(dialog.view.path)
            self.refresh()
            info = dialog.result_info or {}
            version = info.get("registry", {}).get("registry_version")
            text = "Το μητρώο ενημερώθηκε" + (f" (v{version})." if version else ".")
            self.inform(IMPORT_TITLE, text)

    def export_registry(self) -> None:
        current = self._folder() / self.model.default_export_name()
        if self.choose_save is not None:
            chosen = self.choose_save(self, current)
        else:
            path, _ = QFileDialog.getSaveFileName(self, "Εξαγωγή μητρώου", str(current),
                                                  m.REGISTRY_FILTER)
            chosen = Path(path) if path else None
        if chosen is None:
            return
        chosen = Path(chosen)
        if chosen.suffix.lower() != ".sicr":
            chosen = chosen.with_name(chosen.name + ".sicr")
        try:
            out = self.model.export_registry(chosen)
        except FileExistsError:
            self.inform("Εξαγωγή μητρώου", f"Υπάρχει ήδη αρχείο με αυτό το όνομα: {chosen}")
            return
        except (registry_sync.SyncError, OSError, ValueError) as exc:
            self.inform("Εξαγωγή μητρώου", f"Η εξαγωγή απέτυχε: {exc}")
            return
        self._remember_folder(out)
        self.refresh()
        self.inform("Εξαγωγή μητρώου", f"Μητρώο: {out}")

    def delete_previous(self) -> None:
        folders = self.model.previous_folders()
        if not folders:
            return
        if not self.confirm("Διαγραφή προηγούμενων αρχείων",
                            f"Σβήνονται {m._plural(len(folders), 'φάκελος', 'φάκελοι')} με τα "
                            "αρχεία πριν από την επαναφορά, μαζί με το προηγούμενο ιδιωτικό "
                            "κλειδί. Δεν αναιρείται.", DELETE_PREVIOUS):
            return
        try:
            self.model.delete_previous()
        except OSError as exc:
            self.inform(RESTORE_TITLE, f"Η διαγραφή απέτυχε: {exc}")
        self.refresh()

    # --- διάλογοι μηνυμάτων --------------------------------------------------
    def _confirm(self, title: str, text: str, action: str) -> bool:
        box = QMessageBox(QMessageBox.Icon.Warning, title, text,
                          QMessageBox.StandardButton.Cancel, self)
        accept = box.addButton(action, QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return box.clickedButton() is accept

    def _inform(self, title: str, text: str) -> None:
        QMessageBox.information(self, title, text)

    def visible_texts(self) -> list[str]:
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


def ask_rollback() -> bool:
    """Στην εκκίνηση με --gui, όταν μια επαναφορά έμεινε μισή (restore-journal.json)."""
    from director.qt_gui import create_application
    create_application()
    box = QMessageBox(QMessageBox.Icon.Warning, "School Intercom",
                      "Η επαναφορά από backup διακόπηκε πριν ολοκληρωθεί. Ο Manager δεν "
                      "ξεκινά μέχρι να επιστρέψουν τα προηγούμενα αρχεία.",
                      QMessageBox.StandardButton.Cancel)
    accept = box.addButton("Επιστροφή στην προηγούμενη κατάσταση",
                           QMessageBox.ButtonRole.AcceptRole)
    box.setDefaultButton(accept)
    box.exec()
    return box.clickedButton() is accept

