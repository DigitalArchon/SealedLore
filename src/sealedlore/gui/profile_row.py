"""The row of model profiles over Settings → Models' fields
(engine/model_profiles.py): choose one and its models fill the fields; Save
as… keeps the fields as they stand under a name; Delete takes one of the
author's away. Nothing is kept until Settings is saved, as everything else
on the page."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.model_profiles import (
    BUILT_IN,
    Profile,
    matching,
    name_problem,
    to_save,
)
from sealedlore.gui.plain_text import plain_tip
from sealedlore.gui.sizing import enter_only_on_buttons
from sealedlore.models.config import ModelProfile
from sealedlore.models.route import ModelRoute

# What the combo shows while the fields hold no profile.
OWN_MIX = "Your own mix"
OWN_MIX_ABOUT = (
    "The models below are your own mix. Save as… keeps them as a profile to put back later, "
    "here or in a story's Setup → Models."
)

Fields = Callable[[], tuple[dict[str, str], dict[str, ModelRoute | None]]]


class ProfileRow(QWidget):
    """`fields()`: each job's model and route as the page holds them.
    `chosen` carries a profile to put into the fields."""

    chosen = Signal(object)

    def __init__(
        self,
        built_in: Callable[[], list[Profile]],
        saved: list[ModelProfile],
        fields: Fields,
        default: Callable[[], str | None],
        from_saved: Callable[[ModelProfile], Profile],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._built_in = built_in
        self.saved = [p.model_copy(deep=True) for p in saved]
        self._fields = fields
        self._default = default
        self._from_saved = from_saved
        self.combo = QComboBox()
        self.combo.setToolTip("Choose one, and its models fill the fields below")
        self.combo.activated.connect(self._activated)
        self.save_as = QPushButton("Save as…")
        self.save_as.setToolTip("Keep the models below, and their routes, under a name")
        self.save_as.clicked.connect(self.ask_name)
        self.delete = QPushButton("Delete")
        self.delete.setToolTip("Take away the profile chosen (one of your own)")
        self.delete.clicked.connect(self._delete)
        self.about = QLabel()
        self.about.setObjectName("hintLabel")
        self.about.setWordWrap(True)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.combo, 1)
        row.addWidget(self.save_as)
        row.addWidget(self.delete)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(row)
        layout.addWidget(self.about)
        self.refresh()

    # --- what is offered ---------------------------------------------------

    def profiles(self) -> list[Profile]:
        return self._built_in() + [self._from_saved(p) for p in self.saved]

    def refresh(self) -> None:
        """The list again, showing the profile the fields hold, if any."""
        profiles = self.profiles()
        models, routes = self._fields()
        now = matching(profiles, models, routes)
        self.combo.blockSignals(True)
        self.combo.clear()
        if now is None:
            self.combo.addItem(OWN_MIX, None)
        for profile in profiles:
            label = profile.name
            if profile.problem:
                label += " (not on these endpoints)"
            self.combo.addItem(label, profile.name)
            index = self.combo.count() - 1
            self.combo.setItemData(
                index, plain_tip(profile.problem or profile.about), Qt.ToolTipRole
            )
        self.combo.setCurrentIndex(0 if now is None else max(self.combo.findData(now.name), 0))
        self.combo.blockSignals(False)
        self.delete.setEnabled(now is not None and not now.built_in)
        self.about.setText(now.about if now is not None and now.about else OWN_MIX_ABOUT)
        if now is not None and not now.built_in:
            self.about.setText(f"The models below are your profile {now.name}.")

    def _by_name(self, name: str) -> Profile | None:
        return next((p for p in self.profiles() if p.name == name), None)

    def _activated(self, index: int) -> None:
        name = self.combo.itemData(index)
        profile = self._by_name(name) if name else None
        if profile is None:
            return
        if profile.problem:
            self.about.setText(profile.problem)
            return
        self.chosen.emit(profile)
        self.refresh()

    # --- keeping one ---------------------------------------------------------

    def ask_name(self) -> None:
        name = ask_profile_name(self)
        if name is not None:
            self.keep(name)

    def keep(self, name: str) -> bool:
        """The fields as they stand, under `name` (asking before replacing one
        of the author's own of that name)."""
        problem = name_problem(name, self.saved)
        if problem is not None:
            QMessageBox.warning(self, "Save the models as a profile", problem)
            return False
        name = name.strip()
        index = next(
            (i for i, p in enumerate(self.saved) if p.name.casefold() == name.casefold()), None
        )
        if index is not None:
            answer = QMessageBox.question(
                self,
                "Replace the profile?",
                f"You have a profile called {self.saved[index].name}. Replace it with the "
                "models below?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return False
        models, routes = self._fields()
        kept = to_save(name, models, routes, self._default())
        if index is None:
            self.saved.append(kept)
        else:
            self.saved[index] = kept
        self.refresh()
        return True

    def _delete(self) -> None:
        name = self.combo.currentData()
        index = next((i for i, p in enumerate(self.saved) if p.name == name), None)
        if index is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete the profile?",
            f"Delete your profile {name}? The models below stay as they are.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            del self.saved[index]
            self.refresh()

    def renamed(self, renaming: Callable[[list[ModelProfile]], list[ModelProfile]]) -> None:
        """An endpoint renamed: the author's profiles follow it."""
        self.saved = renaming(self.saved)
        self.refresh()


class _NameDialog(QDialog):
    def __init__(self, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Save the models as a profile")
        self.field = QLineEdit()
        self.field.setPlaceholderText("e.g. Cheap and quick, or Horror on Opus")
        hint = QLabel(
            f"A name of your own: {', '.join(BUILT_IN)} come with SealedLore. The same name as "
            "one of yours replaces it."
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Name"))
        layout.addWidget(self.field)
        layout.addWidget(hint)
        layout.addWidget(buttons)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Enter in the name saves: the one field and a button to press.
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and self.field.hasFocus():
            self.accept()
            return
        if not enter_only_on_buttons(self, event):
            super().keyPressEvent(event)


def ask_profile_name(parent: QWidget | None) -> str | None:
    """The name typed, or None for Cancel."""
    dialog = _NameDialog(parent)
    try:
        if dialog.exec() != QDialog.Accepted:
            return None
        return dialog.field.text()
    finally:
        dialog.deleteLater()
