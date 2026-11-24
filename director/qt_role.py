"""Κύριος/Εφεδρικός στο GUI (4.2D2, docs/ui-style.md §7, phase4-2d.md §4–§5).

Μόνο εμφάνιση και σύνδεση σημάτων· η λογική είναι στο `director/security.py`
(`RoleModel`, `RoleStatus`) και πίσω της ο `ServingController` του 4.2D1.

- `RoleCard`: κάρτα «Ρόλος Manager» στο tab «Πιστοποιητικά».
- `RoleActions`: οι διάλογοι (Αλλαγή σε Κύριο/Εφεδρικό, Έναρξη σε σύγκρουση) και οι
  ενέργειες, κοινές για κάρτα και ειδοποιήσεις. Οι διάλογοι περνούν από `dialog_hook`
  στις δοκιμές, όπως στη «Συντήρηση».
"""
from __future__ import annotations

import html
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QVBoxLayout, QWidget)

from director.qt_gui import action_button, px
from director.qt_maintenance import KeyValues, _Dialog, _label
from director.security import (TEXT_DEMOTE, TEXT_PROMOTE, TEXT_TWO_MANAGERS, RoleModel,
                               RoleStatus)
from director.serving import ServingConflict
from shared.ui import icons, theme

ROLE_TITLE = "Ρόλος Manager"
TO_BACKUP = "Αλλαγή σε Εφεδρικό…"
TO_PRIMARY = "Αλλαγή σε Κύριο…"
STOP_HERE = "Διακοπή εξυπηρέτησης εδώ"   # πάντα αυτό το κείμενο (απόφαση χρήστη 23/11/2026)
START = "Έναρξη εξυπηρέτησης"
STAY_BACKUP = "Να μείνει Εφεδρικός"
MAKE_PRIMARY = "Να γίνει Κύριος…"
IMPORT_REGISTRY = "Εισαγωγή μητρώου…"
ACCEPT_CURRENT = "Συνέχεια με αυτό το μητρώο…"
FORCE_START = "Έναρξη παρ' όλα αυτά"
TEXT_STOP = ("Οι Clients αποσυνδέονται από αυτόν τον υπολογιστή και δεν συνδέονται ξανά "
             "εδώ μέχρι την «Έναρξη εξυπηρέτησης».")
TEXT_STOP_ALONE = "Κανένας άλλος Manager δεν εξυπηρετεί: οι ανακοινώσεις σταματούν."


def _restyle(widget: QWidget) -> None:
    """Νέο objectName → ξανά εφαρμογή του QSS."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def stop_button() -> QPushButton:
    return action_button(STOP_HERE, "dangerAction", "stop", theme.WHITE)


def _text(text: str, *, rich: bool = False, name: str | None = None) -> QLabel:
    label = _label(text, name)
    label.setWordWrap(True)
    if rich:
        label.setTextFormat(Qt.TextFormat.RichText)
    return label


class PromoteDialog(_Dialog):
    """«Αλλαγή σε Κύριο»: προειδοποίηση απόκλισης, νεότερο μητρώο, «Έναρξη εξυπηρέτησης»."""

    def __init__(self, status: RoleStatus, parent=None):
        super().__init__("Αλλαγή σε Κύριο", parent)
        self.body.addWidget(_text(TEXT_PROMOTE))
        self.newer = None
        warning = status.newer_warning()
        if warning:
            self.newer = _text(warning, name="warnText")
            self.body.addWidget(self.newer)
        self.start = QCheckBox(START)
        self.start.setChecked(not status.serving)
        # Αν ήδη εξυπηρετεί, η αλλαγή ρόλου απλώς ανοίγει τις εγγραφές.
        self.start.setVisible(not status.serving)
        self.body.addWidget(self.start)
        self.cancel, self.accept_button = self.footer("Να γίνει Κύριος")
        self.accept_button.clicked.connect(self.accept)


class DemoteDialog(_Dialog):
    def __init__(self, parent=None):
        super().__init__("Αλλαγή σε Εφεδρικό", parent)
        self.body.addWidget(_text(TEXT_DEMOTE))
        self.cancel, self.accept_button = self.footer("Να γίνει Εφεδρικός")
        self.accept_button.clicked.connect(self.accept)


class StopDialog(_Dialog):
    """Επιβεβαίωση πριν από «Διακοπή εξυπηρέτησης εδώ» (απόφαση χρήστη 23/11/2026).
    Προεπιλογή «Άκυρο», κόκκινο κουμπί με το ίδιο κείμενο."""

    def __init__(self, status: RoleStatus, parent=None):
        super().__init__("Διακοπή εξυπηρέτησης", parent)
        self.body.addWidget(_text(TEXT_STOP))
        if status.others:
            names = ", ".join(f"<b>{html.escape(o.host)}</b> ({html.escape(o.ip)})"
                              for o in status.others)
            self.body.addWidget(_text(f"Συνεχίζει να εξυπηρετεί ο υπολογιστής {names}.",
                                      rich=True))
        else:
            self.body.addWidget(_text(TEXT_STOP_ALONE, name="warnText"))
        self.cancel, self.accept_button = self.footer(STOP_HERE)
        self.accept_button.setObjectName("dangerAction")
        _restyle(self.accept_button)
        self.cancel.setDefault(True)
        self.cancel.setFocus()
        self.accept_button.clicked.connect(self.accept)


class StartConflictDialog(_Dialog):
    """Άλλος Manager εξυπηρετεί ήδη: Άκυρο (προεπιλογή) ή κόκκινο «Έναρξη παρ' όλα αυτά»."""

    def __init__(self, status: RoleStatus, parent=None):
        super().__init__(START, parent)
        names = ", ".join(f"<b>{html.escape(o.host)}</b> ({html.escape(o.ip)})"
                          for o in status.others) or "άλλος Manager"
        verb = "εξυπηρετούν" if len(status.others) > 1 else "εξυπηρετεί"
        self.message = _text(f"Ο υπολογιστής {names} {verb} ήδη. Σταματήστε τον πρώτα.",
                             rich=True)
        self.body.addWidget(self.message)
        self.body.addWidget(_text(TEXT_TWO_MANAGERS, name="warnText"))
        self.cancel, self.accept_button = self.footer(FORCE_START)
        self.accept_button.setObjectName("dangerAction")
        _restyle(self.accept_button)
        self.cancel.setDefault(True)
        self.cancel.setFocus()
        self.accept_button.clicked.connect(self.accept)


class AcceptCurrentDialog(_Dialog):
    """4.2D3: το νεότερο μητρώο που ανέφεραν οι Clients θεωρείται χαμένο.
    Προεπιλογή «Άκυρο», κόκκινο κουμπί (η ενέργεια χάνει ανακλήσεις)."""

    def __init__(self, status: RoleStatus, parent=None):
        super().__init__("Συνέχεια με αυτό το μητρώο", parent)
        reported = status.reported
        self.body.addWidget(_text(reported.bar_text()))
        self.warning = _text(reported.accept_warning(), name="warnText")
        self.body.addWidget(self.warning)
        self.body.addWidget(_text(
            f"Το μητρώο εδώ γίνεται v{reported.target_version} και η ειδοποίηση σβήνει. "
            "Αν το χαμένο μητρώο βρεθεί αργότερα, η εισαγωγή του γίνεται με συγχώνευση."))
        self.cancel, self.accept_button = self.footer("Συνέχεια με αυτό το μητρώο")
        self.accept_button.setObjectName("dangerAction")
        _restyle(self.accept_button)
        self.cancel.setDefault(True)
        self.cancel.setFocus()
        self.accept_button.clicked.connect(self.accept)


class RoleActions:
    """Ενέργειες ρόλου/εξυπηρέτησης με τους διαλόγους τους.

    ``on_change`` καλείται μετά από κάθε αλλαγή (ανανέωση μπάρας και καρτών).
    ``inform(title, text)``: μήνυμα σφάλματος (π.χ. η θύρα δεν άνοιξε).
    """

    def __init__(self, model: RoleModel, parent: QWidget | None = None, *,
                 dialog_hook: Callable[[QDialog], None] | None = None,
                 inform: Callable[[str, str], None] | None = None,
                 on_change: Callable[[], None] = lambda: None):
        self.model = model
        self.parent = parent
        self.dialog_hook = dialog_hook
        self.inform = inform or self._inform
        self.on_change = on_change
        self.last_dialog: QDialog | None = None

    def _show(self, dialog: QDialog) -> bool:
        self.last_dialog = dialog
        if self.dialog_hook is not None:
            self.dialog_hook(dialog)
            return dialog.result() == QDialog.DialogCode.Accepted
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _inform(self, title: str, text: str) -> None:
        QMessageBox.warning(self.parent, title, text)

    def _done(self) -> None:
        self.on_change()

    # --- ρόλος ---------------------------------------------------------------
    def promote(self) -> None:
        status = self.model.status()
        dialog = PromoteDialog(status, self.parent)
        if not self._show(dialog):
            return
        start = not status.serving and dialog.start.isChecked()
        try:
            self.model.promote()
        except (OSError, ValueError) as exc:
            self.inform("Αλλαγή σε Κύριο", f"Η αλλαγή απέτυχε: {exc}")
            self._done()
            return
        self._done()
        if start:
            self.start()

    def demote(self) -> None:
        if not self._show(DemoteDialog(self.parent)):
            return
        try:
            self.model.demote()
        except (OSError, ValueError) as exc:
            self.inform("Αλλαγή σε Εφεδρικό", f"Η αλλαγή απέτυχε: {exc}")
        self._done()

    def stay_backup(self) -> None:
        """«Να μείνει Εφεδρικός» στην ειδοποίηση επαναφοράς: σβήνει μόνο την ειδοποίηση."""
        try:
            self.model.acknowledge()
        except (OSError, ValueError) as exc:
            self.inform(STAY_BACKUP, str(exc))
        self._done()

    def accept_current(self) -> None:
        """«Συνέχεια με αυτό το μητρώο…» στην κόκκινη ειδοποίηση μητρώου (4.2D3)."""
        status = self.model.status()
        if not status.can_accept_current:
            self._done()
            return
        if not self._show(AcceptCurrentDialog(status, self.parent)):
            return
        try:
            self.model.accept_current()
        except (OSError, ValueError) as exc:
            self.inform("Συνέχεια με αυτό το μητρώο", f"Η αλλαγή απέτυχε: {exc}")
        self._done()

    # --- εξυπηρέτηση ---------------------------------------------------------
    def start(self) -> None:
        try:
            self.model.start()
        except ServingConflict:
            self.offer_force()
            return
        except OSError as exc:
            self.inform(START, f"Η εξυπηρέτηση δεν ξεκίνησε: {exc}")
        self._done()

    def offer_force(self) -> None:
        """Διάλογος σύγκρουσης· χωρίς άλλον Manager πια, απλή έναρξη."""
        status = self.model.status()
        if not status.others:
            if not status.serving:
                self.start()
            return
        if not self._show(StartConflictDialog(status, self.parent)):
            self._done()
            return
        try:
            self.model.start(force=True)
        except OSError as exc:
            self.inform(START, f"Η εξυπηρέτηση δεν ξεκίνησε: {exc}")
        self._done()

    def stop(self) -> None:
        if not self._show(StopDialog(self.model.status(), self.parent)):
            return
        try:
            self.model.stop()
        except OSError as exc:
            self.inform(STOP_HERE, str(exc))
        self._done()


class RoleCard(QFrame):
    """«Ρόλος Manager»: όνομα υπολογιστή επάνω δεξιά, Ρόλος / Εξυπηρέτηση / Άλλος Manager
    και κουμπιά αλλαγής ρόλου και εξυπηρέτησης τέρμα δεξιά (εγκεκριμένο mockup 23/11/2026)."""

    def __init__(self, actions: RoleActions, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.actions = actions
        self.status: RoleStatus | None = None
        self.setMaximumWidth(px(theme.IDENTITY_CARD_MAX))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(px(15), px(12), px(15), px(15))
        layout.setSpacing(px(10))
        head = QHBoxLayout()
        head.setSpacing(px(10))
        head.addWidget(_label(ROLE_TITLE, "cardTitle"))
        head.addStretch(1)
        self.host = _label("", "small")
        head.addWidget(self.host)
        layout.addLayout(head)
        self.rows = KeyValues()
        self.rows.add("role", "Ρόλος", _label("", "roleValue"))
        self.rows.add("serving", "Εξυπηρέτηση")
        other = _text("", rich=True, name="kvValue")
        self.rows.add("other", "Άλλος Manager", other)
        layout.addLayout(self.rows)
        layout.addSpacing(px(6))
        row = QHBoxLayout()
        row.setSpacing(px(8))
        row.addStretch(1)
        self.role_button = action_button(TO_BACKUP, "secondaryAction", "swap")
        self.role_button.clicked.connect(self._change_role)
        self.serve_button = stop_button()
        self.serve_button.clicked.connect(self._change_serving)
        row.addWidget(self.role_button)
        row.addWidget(self.serve_button)
        layout.addLayout(row)

    def update_status(self, status: RoleStatus) -> None:
        if status == self.status:
            return
        self.status = status
        self.host.setText(status.host)
        self.rows.values["role"].setText(status.role_text)
        self.rows.set("serving", *status.serving_text())
        text, tone = status.others_html()
        self.rows.set("other", text, tone)
        self.role_button.setText(TO_BACKUP if status.primary else TO_PRIMARY)
        size = px(theme.ICON)
        if status.serving:
            self.serve_button.setText(STOP_HERE)
            self.serve_button.setObjectName("dangerAction")
            self.serve_button.setIcon(icons.icon("stop", theme.WHITE,
                                                 disabled_color=theme.TEXT_MUTED, size=size))
        else:
            self.serve_button.setText(START)
            self.serve_button.setObjectName("primaryAction")
            self.serve_button.setIcon(icons.icon("play", theme.WHITE,
                                                 disabled_color=theme.TEXT_MUTED, size=size))
        _restyle(self.serve_button)

    def _change_role(self) -> None:
        if self.status is not None and self.status.primary:
            self.actions.demote()
        else:
            self.actions.promote()

    def _change_serving(self) -> None:
        if self.status is not None and self.status.serving:
            self.actions.stop()
        else:
            self.actions.start()
