"""Asking the model for changes to a story or its plot, a request at a time.

The author (Nov 6 2026): after a story or a plot is made for them, "they
might reply, 'I like the world, but I'd prefer the characters were more
badass.' Or 'I like the characters, but the world isn't hostile enough.' and
it can update it (with the ability to undo as well)."

The dialog holds the conversation: each request, and what was changed or
couldn't be. The window runs each request on its worker
(gui/window_followup.py) and tells the dialog what came of it. A plot stays
hidden unless the author looks, so a plot's turns say only that it was
written again until then.
"""

from __future__ import annotations

import html
from collections.abc import Callable, Sequence

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.followup import PLOT, STORY, FollowUpTurn
from sealedlore.gui.plot_dialog import preview_html
from sealedlore.gui.sizing import GrowingText, may_discard, size_to_screen

STORY_EXAMPLE = (
    "e.g. I like the world, but I'd prefer the characters were more badass. Or: the "
    "world isn't hostile enough."
)
PLOT_EXAMPLE = "e.g. More danger in the first week, and no betrayal."
SEND_LABEL = "Ask for these changes"
# After a draft that wrote notes for the storyteller: that it did, and where
# they are. Never what they say.
SECRETS_WRITTEN = (
    "The draft also wrote notes for the storyteller ({words}): the truth behind what the "
    "story leaves open. They are kept from you so the story can surprise you; Story → "
    "Setup → Storyteller's notes shows or clears them."
)
UNDO_LABEL = "Undo the last change"


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hintLabel")
    label.setWordWrap(True)
    return label


class FollowUpDialog(QDialog):
    """`target`: STORY (its setup) or PLOT. `go_on`: the button that ends
    it, and what the window does next (Setup, Start, or nothing). `notes`:
    what a draft left out, shown first. `preview`: the story or the plot as
    it is now, as Markdown. `secret_words`: how long the storyteller's notes
    a draft wrote are, to say that there are some."""

    send_requested = Signal(str)
    undo_requested = Signal()

    def __init__(
        self,
        parent: QWidget | None,
        *,
        target: str,
        title: str,
        go_on: str,
        history: Sequence[FollowUpTurn] = (),
        notes: Sequence[str] = (),
        can_undo: bool = False,
        preview: Callable[[], str] | None = None,
        drafted: bool = False,
        secret_words: int = 0,
    ) -> None:
        super().__init__(parent)
        self.target = target
        self._preview = preview
        self.revealed = False
        self._entries: list[str] = []
        plot = target == PLOT
        self.setWindowTitle("Changes to the plot" if plot else "Changes to the story")
        layout = QVBoxLayout(self)
        heading = QLabel(
            f"“{title}” is drafted."
            if drafted
            else f"The plot of “{title}”"
            if plot
            else f"“{title}”"
        )
        heading.setTextFormat(Qt.PlainText)
        font = heading.font()
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)
        layout.addWidget(
            _hint(
                "Say what you'd like different in the plot. It is written again with that "
                "change, and stays hidden unless you look. Each change can be undone."
                if plot
                else "Read it here or in Setup, and say what you'd like different. The model "
                "changes just that, and whatever must change with it; the rest stays as it "
                "is. Each change can be undone."
            )
        )
        self.show_button = QPushButton(
            "Show me the plot (spoilers)" if plot else "Show the story as it is now"
        )
        self.show_button.clicked.connect(self.reveal)
        self.show_button.setVisible(preview is not None)
        layout.addWidget(self.show_button, 0, Qt.AlignLeft)
        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.hide()
        layout.addWidget(self.view, 2)

        self.log = QTextBrowser()
        self.log.setOpenLinks(False)
        self.log.setObjectName("followUpLog")
        layout.addWidget(self.log, 1)
        if secret_words:
            count = f"{secret_words:,} word{'' if secret_words == 1 else 's'}"
            self._add(f"<i>{html.escape(SECRETS_WRITTEN.format(words=count))}</i>")
        if notes:
            self._add(
                "<i>Left out of the draft:</i><br>"
                + "<br>".join(f"• {html.escape(note)}" for note in notes)
            )
        for turn in history:
            self.add_turn(turn)

        self.request = GrowingText(lines=3, most=8)
        self.request.setPlaceholderText(PLOT_EXAMPLE if plot else STORY_EXAMPLE)
        self.request.setTabChangesFocus(True)
        layout.addWidget(self.request)
        row = QHBoxLayout()
        self.send_button = QPushButton(SEND_LABEL)
        self.send_button.setObjectName("sendButton")
        self.send_button.setToolTip("Ctrl+Enter")
        self.send_button.clicked.connect(self._send)
        self.undo_button = QPushButton(UNDO_LABEL)
        self.undo_button.setToolTip("Put it back as it was before the last request")
        self.undo_button.clicked.connect(self.undo_requested)
        self.undo_button.setEnabled(can_undo)
        row.addWidget(self.send_button)
        row.addWidget(self.undo_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.status = _hint("")
        self.status.setTextFormat(Qt.PlainText)  # an endpoint's own words
        self.status.hide()
        layout.addWidget(self.status)
        buttons = QDialogButtonBox()
        self.go_on_button = buttons.addButton(go_on, QDialogButtonBox.AcceptRole)
        self.go_on_button.clicked.connect(self.accept)
        layout.addWidget(buttons)
        for keys in ("Ctrl+Return", "Ctrl+Enter"):
            QShortcut(QKeySequence(keys), self, activated=self._send)
        self.request.setFocus()
        size_to_screen(self, 0.42, 0.62, at_least=QSize(620, 520))

    # --- the conversation ------------------------------------------------------------

    def _add(self, entry: str) -> None:
        self._entries.append(entry)
        self.log.setHtml("<br><br>".join(self._entries))
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def add_turn(self, turn: FollowUpTurn) -> None:
        """A request and what came of it."""
        said = f"<b>You:</b> {html.escape(turn.request)}"
        if turn.target == PLOT and not self.revealed:
            # The plot's own words are spoilers: what happened, not what it says.
            done = "The plot is written again with that change." if turn.changed else ""
            if turn.refused:
                done += " Some of it couldn't be used: see the plot."
        else:
            lines = [html.escape(turn.summary)] if turn.summary else []
            if turn.changed:
                lines.append("<i>Changed:</i> " + html.escape("; ".join(turn.changed)))
            if turn.refused:
                lines.append(
                    "<i>Couldn't:</i><br>"
                    + "<br>".join(f"• {html.escape(line)}" for line in turn.refused)
                )
            done = "<br>".join(lines)
        if not turn.changed:
            done = done or "Nothing was changed."
        if turn.undone:
            done += "<br><i>Taken back.</i>"
        self._add(f"{said}<br>{done}")

    def show_error(self, message: str) -> None:
        self._add(f"<i>Nothing was changed:</i> {html.escape(message)}")

    def mark_undone(self) -> None:
        self._add("<i>The last change is taken back.</i>")
        self._refresh_view()

    def changed(self) -> None:
        """The window's: a request was done; the request box is cleared."""
        self.request.clear()
        self._refresh_view()

    # --- the controls ----------------------------------------------------------------

    def set_busy(self, busy: bool, text: str = "") -> None:
        self.send_button.setEnabled(not busy)
        if busy:
            # Back once the window knows what there is to undo (`set_can_undo`).
            self.undo_button.setEnabled(False)
        self.request.setReadOnly(busy)
        self.go_on_button.setEnabled(not busy)
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def set_can_undo(self, can: bool) -> None:
        self.undo_button.setEnabled(can)

    def _send(self) -> None:
        text = self.request.toPlainText().strip()
        if not text or not self.send_button.isEnabled():
            return
        self.send_requested.emit(text)

    def reveal(self) -> None:
        self.revealed = True
        self.show_button.hide()
        self._refresh_view()
        self.view.show()
        size_to_screen(self, 0.5, 0.8, at_least=QSize(680, 640))

    def _refresh_view(self) -> None:
        if self.revealed and self._preview is not None:
            self.view.setHtml(preview_html(self._preview()))

    def reject(self) -> None:
        """Esc or the window's close: the same as going on, since every
        change is made already, and can be undone from here later. A request
        typed and not sent is asked about first: Esc went on and dropped it."""
        if not self.go_on_button.isEnabled():
            return  # a request is under way: Stop is in its progress dialog
        if may_discard(self, [""], [self.request.toPlainText().strip()]):
            self.accept()


__all__ = ["PLOT", "STORY", "FollowUpDialog"]
