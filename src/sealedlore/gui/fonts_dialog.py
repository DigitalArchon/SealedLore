"""View → Fonts…: the window's font, the story's, and the monospace one.

Each is the app's own until the author picks a family; the size is Text
size's, so only the family is chosen here.
"""

from __future__ import annotations

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.theme import Fonts

OWN = "The app's own"
SAMPLE = "The saloon doors swing, and the silence does the talking. 0123456789"


def _families(*, fixed_pitch: bool) -> list[str]:
    families = QFontDatabase.families()
    if fixed_pitch:
        families = [family for family in families if QFontDatabase.isFixedPitch(family)]
    return sorted(dict.fromkeys(families), key=str.casefold)


class FontsDialog(QDialog):
    def __init__(self, chosen: Fonts, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Fonts")
        self.setMinimumWidth(520)
        form = QFormLayout()
        self.ui = self._row(form, "Window", chosen.ui, fixed_pitch=False)
        self.story = self._row(form, "Story", chosen.story, fixed_pitch=False)
        self.mono = self._row(form, "Monospace", chosen.mono, fixed_pitch=True)

        hint = QLabel(
            "Window is every label, button and field. Story is the passages and turns, and "
            "the boxes you write them in. Monospace is the Prompt tab and the plot file "
            "preview. The size is View → Text size."
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.RestoreDefaults
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.RestoreDefaults).clicked.connect(self._restore)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(buttons)

    def _row(self, form: QFormLayout, label: str, chosen: str, *, fixed_pitch: bool) -> QComboBox:
        combo = QComboBox()
        combo.addItem(OWN, "")
        for family in _families(fixed_pitch=fixed_pitch):
            combo.addItem(family, family)
        index = combo.findData(chosen) if chosen else 0
        # A family that is no longer installed is kept, and says so.
        if index < 0:
            combo.addItem(f"{chosen} (not installed)", chosen)
            index = combo.count() - 1
        combo.setCurrentIndex(index)
        combo.setMaxVisibleItems(20)
        sample = QLabel(SAMPLE)
        sample.setWordWrap(True)

        def show(_index: int = 0) -> None:
            family = combo.currentData() or ""
            font = QFont(sample.font())
            if family:
                font.setFamily(family)
            elif fixed_pitch:
                font = QFont("monospace")
                font.setStyleHint(QFont.Monospace)
            else:
                font.setFamily(self.font().family())
            sample.setFont(font)

        combo.currentIndexChanged.connect(show)
        show()
        form.addRow(label, combo)
        form.addRow("", sample)
        return combo

    def _restore(self) -> None:
        for combo in (self.ui, self.story, self.mono):
            combo.setCurrentIndex(0)

    def chosen(self) -> Fonts:
        return Fonts(
            ui=self.ui.currentData() or "",
            story=self.story.currentData() or "",
            mono=self.mono.currentData() or "",
        )
