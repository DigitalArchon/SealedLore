"""JSON a person may change: what a character card carries for other apps
(SillyTavern's settings on a lorebook entry, a card's `extensions`), shown
under a heading that opens it.

The card's own fields have fields of their own in Your characters; this is
for the rest, so nothing on a card is out of reach (the author, Nov 2026:
"change, add or remove anything at all"). It is applied only while it reads
as a JSON object; otherwise it says why, and the last good value stands.
An emptied box doesn't read as one either: taken as {}, a slip of the
keyboard took everything under it away (an entry's number, its extensions,
every setting SillyTavern keeps), so clearing it takes a written {}.
"""

from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QToolButton, QVBoxLayout, QWidget

from sealedlore.gui.sizing import GrowingText


def as_text(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


class JsonField(QWidget):
    # The object as it now reads, each time it reads as one.
    edited = Signal(dict)

    def __init__(self, title: str, *, tip: str = "") -> None:
        super().__init__()
        self._title = title
        self._loading = False
        self.button = QToolButton()
        self.button.setObjectName("foldButton")
        self.button.setCheckable(True)
        self.button.setToolTip(tip)
        self.button.toggled.connect(self._toggle)
        self.text = GrowingText(lines=4, most=24)
        self.text.setObjectName("jsonText")
        self.text.textChanged.connect(self._edited)
        self.problem = QLabel()
        # Said as an error is (it read as a dim hint, easy to miss).
        self.problem.setObjectName("errorLabel")
        self.problem.setWordWrap(True)
        self.problem.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.button)
        layout.addWidget(self.text)
        layout.addWidget(self.problem)
        self._toggle(False)

    def set_value(self, value: dict[str, Any]) -> None:
        self._loading = True
        self.text.setPlainText(as_text(value))
        self._loading = False
        self._fixed()

    def _toggle(self, on: bool) -> None:
        # Folded, a draft that isn't applied still says so on its heading.
        mark = "  (⚠ not applied)" if self.broken() else ""
        self.button.setText(f"{'▾' if on else '▸'}  {self._title}{mark}")
        self.text.setVisible(on)
        self.problem.setVisible(on and self.broken())

    def broken(self) -> bool:
        """The text as it stands isn't applied (it doesn't read as an object)."""
        return self.problem.property("broken") is True

    def _fixed(self) -> None:
        self.problem.setProperty("broken", False)
        self.problem.setText("")
        self._toggle(self.button.isChecked())

    def _edited(self) -> None:
        if self._loading:
            return
        source = self.text.toPlainText().strip()
        if not source:
            self._say("Empty (write {} to clear it): kept as it last was.")
            return
        try:
            value = json.loads(source)
        except json.JSONDecodeError as exc:
            self._say(
                f"Not JSON yet (line {exc.lineno}, column {exc.colno}: {exc.msg}): kept as "
                "it last was."
            )
            return
        if not isinstance(value, dict):
            self._say("This must be an object, between { and }: kept as it last was.")
            return
        self._fixed()
        self.edited.emit(value)

    def _say(self, words: str) -> None:
        self.problem.setText(words)
        self.problem.setProperty("broken", True)
        self._toggle(self.button.isChecked())
