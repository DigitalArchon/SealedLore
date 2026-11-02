"""A character's picture labelled for looks (engine/looks.py): what it shows,
the look that lasts (clothes, armour, injured) and the moment's expression or
pose. The labels the character's other pictures use are offered first, so a
set stays consistent ("armour", not "armor" on one and "plate" on the next)."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QSize
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.looks import COMMON_EXPRESSIONS, COMMON_LOOKS, labels_in_use
from sealedlore.gui.sizing import size_to_screen
from sealedlore.models.image import ImageRef

LABELS_LABEL = "Labels…"
LABELS_TIP = (
    "What the picture shows, and for showing them as the story has them: the look that "
    "lasts (clothes, armour) and the moment's expression or pose"
)
LOOK_HINT = "Lasts until the story changes it: normal clothes, combat armour, injured"
EXPRESSION_HINT = "This moment's: angry, happy, talking, in a fighting stance"


def _choices(in_use: Sequence[str], common: Sequence[str]) -> list[str]:
    seen: dict[str, None] = {}
    for word in (*in_use, *common):
        if word.strip() and word.strip().lower() not in {w.lower() for w in seen}:
            seen[word.strip()] = None
    return list(seen)


class PictureLabelsDialog(QDialog):
    def __init__(
        self, ref: ImageRef, others: Sequence[ImageRef], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Picture labels")
        size_to_screen(self, 0.3, 0.25, at_least=QSize(480, 270))
        looks, expressions = labels_in_use(others)
        self.caption = QLineEdit(ref.caption)
        self.caption.setPlaceholderText("front view, at the market")
        self.look = self._combo(_choices(looks, COMMON_LOOKS), ref.look)
        self.expression = self._combo(_choices(expressions, COMMON_EXPRESSIONS), ref.expression)
        form = QFormLayout()
        form.addRow("What it shows", self.caption)
        form.addRow("Look", self.look)
        look_hint = QLabel(LOOK_HINT)
        look_hint.setObjectName("hintLabel")
        look_hint.setWordWrap(True)
        form.addRow("", look_hint)
        form.addRow("Expression or pose", self.expression)
        expression_hint = QLabel(EXPRESSION_HINT)
        expression_hint.setObjectName("hintLabel")
        expression_hint.setWordWrap(True)
        form.addRow("", expression_hint)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addStretch(1)
        layout.addWidget(buttons)

    @staticmethod
    def _combo(choices: Sequence[str], value: str) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)
        combo.addItem("")
        combo.addItems(list(choices))
        combo.setCurrentText(value)
        return combo

    def labels(self) -> tuple[str, str, str]:
        """(caption, look, expression), each trimmed."""
        return (
            self.caption.text().strip(),
            " ".join(self.look.currentText().split()),
            " ".join(self.expression.currentText().split()),
        )


def edit_labels(parent: QWidget, ref: ImageRef, others: Sequence[ImageRef]) -> bool:
    """Ask for a picture's labels and set them; whether any changed."""
    dialog = PictureLabelsDialog(ref, others, parent)
    accepted = dialog.exec() == QDialog.Accepted
    caption, look, expression = dialog.labels()
    dialog.deleteLater()
    if not accepted or (caption, look, expression) == (ref.caption, ref.look, ref.expression):
        return False
    ref.caption, ref.look, ref.expression = caption, look, expression
    return True
