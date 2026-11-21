"""Story → Suggestions from the story…: what the story suggests for its lore
and cards (engine/story_suggestions.py), each shown with what it rests on,
editable, and applied or turned down one at a time. Closing keeps the rest
for later; nothing changes until the author applies it.

The dialog changes nothing itself: the window's `apply` and `decline` do,
and it is rebuilt from the session after each."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QSize
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.story_suggestions import loses_much
from sealedlore.gui.sizing import GrowingText, size_to_screen
from sealedlore.models.story_suggestion import StorySuggestion

NONE_YET = (
    "Nothing suggested yet. As the story is archived into chapters, what it makes part "
    "of the world, or changes in it, is suggested here: new lore entries, changes to "
    "entries, and lasting changes to characters. Look over the recent story asks now."
)
HEADINGS = {
    "lore_new": "A new lore entry",
    "lore_change": "A change to the lore entry",
    "card_change": "An addition to the card of",
}
APPLY_WORDS = {
    "lore_new": "Add to the lore",
    "lore_change": "Change the entry",
    "card_change": "Add to their card",
}


class SuggestionCard(QFrame):
    """One suggestion: its fields, what it rests on, Apply and Not this."""

    def __init__(
        self,
        suggestion: StorySuggestion,
        *,
        current: str,
        apply: Callable[[StorySuggestion, dict], None],
        decline: Callable[[StorySuggestion], None],
    ) -> None:
        super().__init__()
        self.setObjectName("suggestionCard")
        self.setFrameShape(QFrame.StyledPanel)
        self.suggestion = suggestion
        column = QVBoxLayout(self)
        heading = HEADINGS[suggestion.kind]
        if suggestion.kind != "lore_new":
            heading += f" {suggestion.title}"
        # Plain text, as every label is (gui/plain_text.py): bold by its font.
        title = QLabel(heading)
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        column.addWidget(title)
        self.title_field: QLineEdit | None = None
        self.keywords_field: QLineEdit | None = None
        if suggestion.kind == "lore_new":
            self.title_field = QLineEdit(suggestion.title)
            self.title_field.setPlaceholderText("Title")
            self.keywords_field = QLineEdit(", ".join(suggestion.keywords))
            self.keywords_field.setPlaceholderText("Keywords, separated by commas")
            column.addWidget(self.title_field)
            column.addWidget(self.keywords_field)
        if suggestion.kind == "lore_change" and current:
            was = QLabel("As it stands now:")
            was.setObjectName("hintLabel")
            old = GrowingText(current, lines=2, most=8)
            old.setReadOnly(True)
            column.addWidget(was)
            column.addWidget(old)
            column.addWidget(QLabel("As it would read:"))
        if suggestion.kind == "card_change":
            note = QLabel("Added to their description as a paragraph of its own:")
            note.setObjectName("hintLabel")
            column.addWidget(note)
        self.content = GrowingText(suggestion.content, lines=2, most=10)
        column.addWidget(self.content)
        if suggestion.kind == "lore_change" and loses_much(current, suggestion.content):
            warning = QLabel(
                "⚠ This keeps less than half of the entry's words: check nothing still "
                "true is lost before you change it."
            )
            warning.setObjectName("warningLabel")
            warning.setWordWrap(True)
            column.addWidget(warning)
        if suggestion.why:
            why = QLabel(f"From the story: {suggestion.why}")
            why.setObjectName("hintLabel")
            why.setWordWrap(True)
            column.addWidget(why)
        row = QHBoxLayout()
        row.addStretch(1)
        self.decline_button = QPushButton("Not this")
        self.decline_button.setToolTip("Gone, and never suggested again.")
        self.decline_button.clicked.connect(lambda: decline(suggestion))
        self.apply_button = QPushButton(APPLY_WORDS[suggestion.kind])
        self.apply_button.clicked.connect(lambda: apply(suggestion, self.values()))
        row.addWidget(self.decline_button)
        row.addWidget(self.apply_button)
        column.addLayout(row)

    def typed(self) -> dict[str, str]:
        """What the author changed in the fields, as typed: to put back when
        the cards are made again."""
        was = {
            "content": self.suggestion.content,
            "title": self.suggestion.title,
            "keywords": ", ".join(self.suggestion.keywords),
        }
        now = {"content": self.content.toPlainText()}
        if self.title_field is not None:
            now["title"] = self.title_field.text()
        if self.keywords_field is not None:
            now["keywords"] = self.keywords_field.text()
        return {name: text for name, text in now.items() if text != was[name]}

    def put_back(self, typed: dict[str, str]) -> None:
        if "content" in typed:
            self.content.setPlainText(typed["content"])
        if "title" in typed and self.title_field is not None:
            self.title_field.setText(typed["title"])
        if "keywords" in typed and self.keywords_field is not None:
            self.keywords_field.setText(typed["keywords"])

    def values(self) -> dict:
        """The fields as the author left them."""
        values: dict = {"content": self.content.toPlainText()}
        if self.title_field is not None:
            values["title"] = self.title_field.text()
        if self.keywords_field is not None:
            values["keywords"] = [
                k.strip() for k in self.keywords_field.text().split(",") if k.strip()
            ]
        return values


class StorySuggestionsDialog(QDialog):
    """Every suggestion waiting, newest last. `apply`, `decline` and
    `look_over` are the window's; `current(suggestion)` gives a changed
    entry's text as it stands."""

    def __init__(
        self,
        suggestions: Callable[[], list[StorySuggestion]],
        *,
        current: Callable[[StorySuggestion], str],
        apply: Callable[[StorySuggestion, dict], str | None],
        decline: Callable[[StorySuggestion], None],
        look_over: Callable[[], None] | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Suggestions from the story")
        self._suggestions = suggestions
        self._current = current
        self._apply = apply
        self._decline = decline
        column = QVBoxLayout(self)
        intro = QLabel(
            "What the story has made part of its world, or changed in it, as it was "
            "archived. Change anything before you apply it; Not this turns a suggestion "
            "down for good. Nothing changes until you apply it."
        )
        intro.setWordWrap(True)
        intro.setObjectName("hintLabel")
        column.addWidget(intro)
        self.said = QLabel()
        self.said.setWordWrap(True)
        column.addWidget(self.said)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        column.addWidget(self.scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        self.look_button = QPushButton("Look over the recent story")
        self.look_button.setToolTip(
            "Read the most recent passages still in the window for suggestions now "
            "(one call to the summarisation model)."
        )
        if look_over is not None:
            self.look_button.clicked.connect(look_over)
        else:
            self.look_button.setEnabled(False)
        buttons.addButton(self.look_button, QDialogButtonBox.ActionRole)
        column.addWidget(buttons)
        size_to_screen(self, 0.5, 0.75, at_least=QSize(640, 480))
        self.refresh()

    def refresh(self) -> None:
        # What was typed in the other cards stays: every card is made again
        # after one is applied or turned down, or a reading ends, and an edit
        # waiting in another went back to the model's wording.
        typed = {card.suggestion.id: card.typed() for card in getattr(self, "cards", [])}
        holder = QWidget()
        layout = QVBoxLayout(holder)
        waiting = self._suggestions()
        self.cards: list[SuggestionCard] = []
        if not waiting:
            empty = QLabel(NONE_YET)
            empty.setWordWrap(True)
            layout.addWidget(empty)
        for suggestion in waiting:
            card = SuggestionCard(
                suggestion,
                current=self._current(suggestion),
                apply=self._on_apply,
                decline=self._on_decline,
            )
            card.put_back(typed.get(suggestion.id, {}))
            self.cards.append(card)
            layout.addWidget(card)
        layout.addStretch(1)
        self.scroll.setWidget(holder)

    def _on_apply(self, suggestion: StorySuggestion, values: dict) -> None:
        said = self._apply(suggestion, values)
        if said:
            self.said.setText(said)
        self.refresh()

    def _on_decline(self, suggestion: StorySuggestion) -> None:
        self._decline(suggestion)
        self.said.setText(f"Turned down: {suggestion.title}.")
        self.refresh()

    def set_busy(self, busy: bool) -> None:
        """While the window reads the story, nothing is applied meanwhile."""
        self.look_button.setEnabled(not busy)
        for card in self.cards:
            card.apply_button.setEnabled(not busy)
            card.decline_button.setEnabled(not busy)
