"""Help → Models SealedLore adjusts for…: every model SealedLore treats
differently, what it does and why (engine/model_tuning.py), read-only.

The author (Nov 18 2026): the app should show which models it is tuned for
and how. Each model field says so beside it ("· tuned"); this is the whole
list in one place.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.model_tuning import described
from sealedlore.gui.sizing import size_to_screen

TITLE = "Models SealedLore adjusts for"
INTRO = (
    "SealedLore sends every model the same story, but for some models it leaves a text "
    "out of the prompt, or shapes the request the way the model's own service requires. "
    "These are all of them. A model field shows “· tuned” beside a model they apply to."
)
KINDS = {"prompt": "Prompt", "request": "Request"}


class ModelTuningDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(TITLE)
        layout = QVBoxLayout(self)
        intro = QLabel(INTRO)
        intro.setWordWrap(True)
        layout.addWidget(intro)
        body = QWidget()
        rows = QVBoxLayout(body)
        rows.setSpacing(14)
        self.entries: list[QLabel] = []
        for models, kind, what, why in described():
            head = QLabel(f"{models}  ·  {KINDS[kind]}")
            head.setObjectName("peopleHeading")
            rows.addWidget(head)
            text = QLabel(f"{what}\n\nWhy: {why}")
            text.setTextFormat(Qt.PlainText)
            text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextSelectableByMouse)
            rows.addWidget(text)
            self.entries.append(text)
        rows.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        size_to_screen(self, 0.4, 0.55, at_least=QSize(560, 420))


def show_model_tuning(parent: QWidget) -> None:
    dialog = ModelTuningDialog(parent)
    try:
        dialog.exec()
    finally:
        dialog.deleteLater()  # read, then freed
