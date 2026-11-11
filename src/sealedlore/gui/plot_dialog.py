"""A plot written by the model: asking for one, and the plot once it is in.

`PlotDialog` is Story → Write a plot with the model…: what the author wants
of it (gui/plot_wishes.py). The new story guide and New story from a premise
ask the same in a step of their own. The plot itself is written on the
window's worker (`StorySession.draft_plot`, gui/window_plot.py).

`PlotReadyDialog` follows it, wherever it was asked for. The author asked to
choose whether to see the plot (spoilers) or meet it in play, so it opens on
neither: the plot stays hidden until they ask to see it, and Keep it a
surprise is the default. Nothing written for the story is shown before that,
not even what was left out of it (those notes are spoilers too).
"""

from __future__ import annotations

import html

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.plot_file import render_plot_markdown
from sealedlore.gui.markdown_text import markdown_html
from sealedlore.gui.plot_wishes import ABOUT, PlotWishesForm
from sealedlore.gui.sizing import enter_only_on_buttons, may_discard, size_to_screen
from sealedlore.models.config import model_label
from sealedlore.models.plot_file import OpeningDoc, PlotDocument

WRITE_LABEL = "Write a plot with the model…"
EDIT, CONTINUE, FOLLOW_UP = "edit", "continue", "follow_up"


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hintLabel")
    label.setWordWrap(True)
    return label


def written_by(model: str) -> str:
    return (
        f"Written by {model_label(model) if model else 'the story model'} (Settings → Models → "
        "Authoring model, or this story's own in Story → Setup…), in a minute or two. It is "
        "checked as a plot file is: what can't be used is put right or left out."
    )


class PlotDialog(QDialog):
    def __init__(self, parent: QWidget | None, *, model: str, replacing: bool, begun: bool) -> None:
        super().__init__(parent)
        self.setWindowTitle("Write a plot with the model")
        layout = QVBoxLayout(self)
        layout.addWidget(_hint(ABOUT))
        if replacing:
            layout.addWidget(
                _hint(
                    "This story has a plot already: the new one takes its place. What has "
                    "happened in the story stays as it was written."
                )
            )
        if begun:
            layout.addWidget(
                _hint(
                    "The story has begun, so the plot starts from where it stands: the "
                    "model is shown the scene, the chapters so far and the latest messages."
                )
            )
        self.form = PlotWishesForm(offer_none=False)
        layout.addWidget(self.form)
        layout.addWidget(_hint(written_by(model)))
        layout.addStretch(1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.write_button = self.buttons.addButton("Write the plot", QDialogButtonBox.AcceptRole)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        size_to_screen(self, 0.4, 0.6)
        self._typed = [self.form.typed()]

    def wishes(self):
        return self.form.wishes()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Enter on a field never writes the plot (a paid call): it presses
        only a push button that has the focus (gui/sizing.py
        `enter_only_on_buttons`). Enter on a list or a box wrote it."""
        if not enter_only_on_buttons(self, event):
            super().keyPressEvent(event)

    def reject(self) -> None:
        if may_discard(self, self._typed, [self.form.typed()]):
            super().reject()


def plot_preview(document: PlotDocument) -> str:
    """The plot's own parts, as the plot file writes them: whom and what it
    keeps hidden, its places, facts and events. The story's world, opening
    and own cards are left out: they are the story's, not news."""
    shown = document.model_copy(
        update={
            "title": "",
            "description": "",
            "agency": None,
            "world_activity": None,
            "perspective": None,
            "length": None,
            "picture_style": "",
            "background": "",
            "notes": "",
            "world": "",
            "opening": OpeningDoc(),
            "characters": [c for c in document.characters if c.hidden],
            "lore": [entry for entry in document.lore if entry.hidden],
        },
        deep=True,
    )
    return render_plot_markdown(shown).text


def preview_html(text: str) -> str:
    """Model-written Markdown as the transcript shows Markdown: never as HTML
    of its own, no picture loaded, no link followed (gui/markdown_text.py)."""
    shown = markdown_html(text)
    return shown if shown is not None else f"<pre>{html.escape(text)}</pre>"


class PlotReadyDialog(QDialog):
    """The plot is written: hidden until the author asks to see it. After
    exec: `choice` (EDIT for the plot editor, FOLLOW_UP to ask for changes,
    CONTINUE), `revealed`, and
    `keep_spoilers_off` (only offered while the Plot page shows spoilers)."""

    def __init__(
        self,
        parent: QWidget | None,
        *,
        title: str,
        plot: str,
        notes: list[str],
        go_on: str,
        spoilers_on: bool,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("The plot is written")
        self.choice = CONTINUE
        self.revealed = False
        layout = QVBoxLayout(self)
        heading = QLabel(f"“{title}” has its plot.")
        heading.setTextFormat(Qt.PlainText)
        font = heading.font()
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)
        layout.addWidget(
            _hint(
                "It stays hidden unless you look: the storyteller learns of each event "
                "only as it begins, and you meet it as it happens. Keep it a surprise, or "
                "look it over first: it shows everything, the people it keeps hidden too."
            )
        )
        self.show_button = QPushButton("Show me the plot (spoilers)")
        self.show_button.clicked.connect(self.reveal)
        layout.addWidget(self.show_button, 0, Qt.AlignLeft)
        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.setHtml(preview_html(plot))
        self.view.hide()
        layout.addWidget(self.view, 1)
        self.notes = _hint(
            "Some of what the model wrote couldn't be used:\n"
            + "\n".join(f"• {note}" for note in notes)
            if notes
            else ""
        )
        self.notes.setTextFormat(Qt.PlainText)  # names and text from the model
        self.notes.hide()
        self._has_notes = bool(notes)
        layout.addWidget(self.notes)
        self.keep_off = QCheckBox("Keep it a surprise on the Plot page too (spoilers off)")
        self.keep_off.setToolTip(
            "The Plot page shows every event while Show plot (spoilers) is ticked there"
        )
        self.keep_off.setChecked(spoilers_on)
        self.keep_off.setVisible(spoilers_on)
        # Read after exec, when nothing in the closed dialog is visible.
        self._offered = spoilers_on
        layout.addWidget(self.keep_off)
        layout.addStretch(0)
        buttons = QDialogButtonBox()
        self.follow_up_button = buttons.addButton("Ask for changes…", QDialogButtonBox.ActionRole)
        self.follow_up_button.setToolTip(
            "Say what you'd like different; the plot is written again, still hidden, and each "
            "change can be undone"
        )
        self.follow_up_button.clicked.connect(self._follow_up)
        self.edit_button = buttons.addButton(
            "Edit in the plot editor…", QDialogButtonBox.ActionRole
        )
        self.edit_button.setToolTip("The whole plot, to read and change (spoilers)")
        self.edit_button.clicked.connect(self._edit)
        self.go_on_button = buttons.addButton(go_on, QDialogButtonBox.AcceptRole)
        self.go_on_button.setDefault(True)
        self.go_on_button.clicked.connect(self.accept)
        layout.addWidget(buttons)
        size_to_screen(self, 0.36, 0.3, at_least=QSize(520, 260))

    @property
    def keep_spoilers_off(self) -> bool:
        return self._offered and self.keep_off.isChecked()

    def reveal(self) -> None:
        self.revealed = True
        self.show_button.hide()
        self.view.show()
        self.notes.setVisible(self._has_notes)
        # Looked at: the Plot page may show it as well.
        self.keep_off.setChecked(False)
        size_to_screen(self, 0.5, 0.75, at_least=QSize(640, 520))

    def _edit(self) -> None:
        self.choice = EDIT
        self.accept()

    def _follow_up(self) -> None:
        self.choice = FOLLOW_UP
        self.accept()
