"""Chat with them… (Your characters): New simple chat with a character's own
parts added and filled in from their card (engine/card_chat.py), each yours
to change before the chat starts: who you are, the first message (their
card), which of their greetings opens it, the system prompt (theirs, else a
plain one, else one of your saved prompts) and the tail (their post-history
instructions)."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QLabel, QLineEdit

from sealedlore.engine import card_chat
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.gui.new_chat_dialog import NewChatDialog
from sealedlore.gui.sizing import GrowingText, size_to_screen
from sealedlore.gui.tavern_dialog import pictures_named
from sealedlore.models.character import Character
from sealedlore.storage.card_files import greeting_pictures

NO_GREETING = "None: they answer the first message"


@dataclass
class ChatSetup:
    title: str
    system: str
    tail: str
    first: str
    # None: the first message is sent, and the model answers it.
    greeting: str | None


def _words(text: str, count: int = 9) -> str:
    words = text.split()
    return " ".join(words[:count]) + ("…" if len(words) > count else "")


class CardChatDialog(NewChatDialog):
    def __init__(
        self,
        card: Character,
        *,
        texts: PromptTexts = DEFAULT_TEXTS,
        you: str = "",
        about: str = "",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.card = card
        self.texts = texts
        self.setWindowTitle(f"Chat with {card.name}")
        size_to_screen(self, 0.5, 0.9)
        self.title.setText(card.name)
        self.intro.setText(
            f"A simple chat with {card.name}: their card is its first message, and a greeting "
            "of theirs its first reply. Everything below is filled in from the card and yours "
            "to change before it starts."
        )
        self.prompt.setMinimumHeight(90)
        # What each prefilled box was last given: a box the author has
        # changed is left alone when the names change.
        self._given: dict[object, str] = {}

        # Who you were in the last chat with one of them (`you`, `about`):
        # typed again for every card otherwise.
        self.user = QLineEdit(you or card_chat.DEFAULT_USER)
        self.user.setPlaceholderText("Your name in the chat (the card's {{user}})")
        self.about = GrowingText(about, lines=2, most=6)
        self.about.setPlaceholderText("Optional: who you are, as a SillyTavern persona says")
        self.first = GrowingText(lines=6, most=16)
        self.greeting = QComboBox()
        assets = card.tavern.assets if card.tavern else []
        for number, text in enumerate(card_chat.greetings(card, user="")):
            words = greeting_pictures(text, assets, card.reference_images).text
            self.greeting.addItem(f"Greeting {number + 1}: {_words(words)}", number)
        self.greeting.addItem(NO_GREETING, None)
        self.greeting_text = GrowingText(lines=3, most=12)
        first_hint = QLabel(
            "Their card, as the chat's first message: kept in full however long the chat "
            "grows, as a chat's first message is."
        )
        first_hint.setObjectName("hintLabel")
        first_hint.setWordWrap(True)
        # The pictures of the card's each box names (`{{img::…}}` and the
        # like), as the chat will have them: under the message, the names
        # taken out of what the model reads (`_sync_pictures`).
        self.first_pictures, self.greeting_pictures = QLabel(), QLabel()
        for label in (self.first_pictures, self.greeting_pictures):
            label.setObjectName("hintLabel")
            label.setWordWrap(True)
            label.setTextFormat(Qt.PlainText)

        form = self.form
        row, _role = form.getWidgetPosition(self.title)
        form.insertRow(row + 1, "You are", self.user)
        form.insertRow(row + 2, "About you", self.about)
        row, _role = form.getWidgetPosition(self.keep)
        form.insertRow(row, "First message", self.first)
        form.insertRow(row + 1, "", self.first_pictures)
        form.insertRow(row + 2, "", first_hint)
        form.insertRow(row + 3, "Their greeting", self.greeting)
        form.insertRow(row + 4, "", self.greeting_text)
        form.insertRow(row + 5, "", self.greeting_pictures)

        self.user.textChanged.connect(lambda _text: self._refill())
        self.about.textChanged.connect(self._refill)
        self.greeting.currentIndexChanged.connect(lambda _index: self._choose_greeting())
        self.first.textChanged.connect(self._sync_pictures)
        self.greeting_text.textChanged.connect(self._sync_pictures)
        self._refill()

    def _put(self, box, text: str) -> None:
        """`text` in `box`, unless the author has written there themselves."""
        current = box.text() if isinstance(box, QLineEdit) else box.toPlainText()
        if box in self._given and current != self._given[box]:
            return
        self._given[box] = text
        box.blockSignals(True)
        if isinstance(box, QLineEdit):
            box.setText(text)
        else:
            box.setPlainText(text)
        box.blockSignals(False)

    def _name(self) -> str:
        return self.user.text().strip() or card_chat.DEFAULT_USER

    def _refill(self) -> None:
        user, card = self._name(), self.card
        self._put(self.prompt, card_chat.system_prompt(card, user=user, texts=self.texts))
        self._put(self.tail, card_chat.tail(card, user=user))
        self._put(
            self.first,
            card_chat.character_text(
                card, user=user, about=self.about.toPlainText(), texts=self.texts
            ),
        )
        self._refill_greeting()

    def _refill_greeting(self) -> None:
        """The greeting chosen, the names in, unless the author has written
        in it."""
        index = self.greeting.currentData()
        self.greeting_text.setVisible(index is not None)
        if index is not None:
            texts = card_chat.greetings(self.card, user=self._name())
            self._put(self.greeting_text, texts[index])
        self._sync_pictures()

    def _sync_pictures(self) -> None:
        """The pictures each box names, as the chat started from it shows
        them (gui/window_card_start.py reads both with `greeting_pictures`):
        said under the box, since the names stay in it to be edited."""
        card = self.card
        assets = card.tavern.assets if card.tavern else []
        boxes = (
            (self.first_pictures, self.first, True),
            (self.greeting_pictures, self.greeting_text, self.greeting.currentData() is not None),
        )
        for label, box, used in boxes:
            shown = greeting_pictures(box.toPlainText().strip(), assets, card.reference_images)
            named = pictures_named(shown) if used else ""
            label.setText(
                f"Shown under it, as pictures: {named}. The model doesn't read their names."
                if named
                else ""
            )
            label.setVisible(bool(named))

    def _choose_greeting(self) -> None:
        # Another greeting chosen is that greeting, whatever was written.
        self._given.pop(self.greeting_text, None)
        self._refill_greeting()

    def setup(self) -> ChatSetup:
        index = self.greeting.currentData()
        greeting = self.greeting_text.toPlainText().strip() if index is not None else None
        return ChatSetup(
            title=self.title.text().strip(),
            system=self.prompt.toPlainText().strip(),
            tail=self.tail.toPlainText().strip(),
            first=self.first.toPlainText().strip(),
            greeting=greeting or None,
        )
