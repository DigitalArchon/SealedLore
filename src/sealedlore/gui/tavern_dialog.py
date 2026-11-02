"""A new story from a SillyTavern character card: the few things the card
can't say (who you play, which greeting opens it, whether it is one
character or a world), asked before the story is made
(storage/tavern_cards.py `scenario_from_card`); and the link a card or a
lorebook is fetched by (providers/card_links.py)."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.ref_images import thumbnail_of
from sealedlore.gui.sizing import GrowingText, size_to_screen
from sealedlore.providers.card_links import LinkError, read_link
from sealedlore.storage.tavern_cards import TavernCard, first_sentence, macro_filler

NO_OPENING = "None: I write the first turn"
# The Cast and Lore pages' menus, and Story → Export's (the manual names them).
TAVERN_LABEL = "SillyTavern cards"
CARD_IMPORT_LABEL = "Import a character card…"
CARD_EXPORT_LABEL = "Export this character as a card…"
EXPRESSIONS_LABEL = "Add expression pictures from a zip…"
LOREBOOK_LABEL = "SillyTavern lorebook"
LOREBOOK_IMPORT_LABEL = "Import a lorebook…"
LOREBOOK_EXPORT_LABEL = "Export the lore as a lorebook…"
# By link: File's (a new story from a card), the Cast and Lore pages' menus.
LINK_IMPORT_LABEL = "Import from a link…"
CARD_LINK_LABEL = "Import a card from a link…"
LOREBOOK_LINK_LABEL = "Import a lorebook from a link…"
LINK_SITES = (
    "A character or a lorebook on Chub, a character on RisuRealm, or a link straight "
    "to a card's file (PNG, JSON or CHARX) or a lorebook's (JSON). SealedLore "
    "downloads it from that site, which sees your address as a browser would; "
    "nothing else of yours is sent."
)
PICTURE_SIDE = 160


def _first_words(text: str, count: int = 9) -> str:
    words = text.split()
    return " ".join(words[:count]) + ("…" if len(words) > count else "")


class CardImportDialog(QDialog):
    def __init__(self, card: TavernCard, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.card = card
        self.setWindowTitle(f"A new story from {card.name}")
        size_to_screen(self, 0.4, 0.7, at_least=QSize(620, 600))

        picture = QLabel()
        picture.setFixedSize(PICTURE_SIDE, PICTURE_SIDE)
        picture.setAlignment(Qt.AlignCenter)
        if card.picture:
            picture.setPixmap(thumbnail_of(card.picture, PICTURE_SIDE))
        else:
            picture.hide()
        title = QLabel(card.name)
        title.setObjectName("dialogTitle")
        title.setStyleSheet("font-weight: bold;")
        about = QLabel(first_sentence(card.text("description") or card.text("personality"), 300))
        about.setWordWrap(True)
        creator = card.text("creator").strip()
        book = len(card.book["entries"]) if card.book else 0
        facts = [f"by {creator}" if creator else "", f"{len(card.greetings)} greetings"]
        if book:
            facts.append(f"{book} lore entries")
        details = QLabel(" · ".join(fact for fact in facts if fact))
        details.setObjectName("hintLabel")
        heading = QVBoxLayout()
        heading.addWidget(title)
        heading.addWidget(about)
        heading.addWidget(details)
        heading.addStretch(1)
        top = QHBoxLayout()
        top.addWidget(picture, 0, Qt.AlignTop)
        top.addLayout(heading, 1)

        self.as_character = QRadioButton(f"One character, played by the storyteller: {card.name}")
        self.as_world = QRadioButton("A world, or a narrator for several people")
        self.as_world.setToolTip(
            "The card's description becomes the story's world, with no card for "
            f"“{card.name}”; Fill with lore characters… on the Cast page can write cards for "
            "the people in it"
        )
        self.as_character.setChecked(True)
        kinds = QButtonGroup(self)
        kinds.addButton(self.as_character)
        kinds.addButton(self.as_world)
        kind_box = QVBoxLayout()
        kind_box.setContentsMargins(0, 0, 0, 0)
        kind_box.addWidget(self.as_character)
        kind_box.addWidget(self.as_world)
        kind_widget = QWidget()
        kind_widget.setLayout(kind_box)

        self.player = QLineEdit()
        self.player.setPlaceholderText("The name of the character you play")
        self.about_player = GrowingText(lines=2, most=6)
        self.about_player.setPlaceholderText(
            "Optional: who they are, as a SillyTavern persona would say"
        )

        self.opening = QComboBox()
        for number, greeting in enumerate(card.greetings, start=1):
            self.opening.addItem(f"Greeting {number}: {_first_words(greeting)}", number - 1)
        self.opening.addItem(NO_OPENING, None)
        self.preview = GrowingText(lines=4, most=10)
        self.preview.setReadOnly(True)

        self.instructions = QCheckBox("Add the card's own instructions to Style → Other notes")
        self.instructions.setToolTip(
            "The card's system prompt. SealedLore's own storyteller rules stand either way; "
            "most cards' instructions repeat them, or ask for things they already forbid"
        )
        self.instructions.setVisible(bool(card.instructions))
        count = len(card.expressions)
        size = sum(len(data) for _name, data in card.expressions) / (1024 * 1024)
        self.expressions = QCheckBox(
            f"Keep its {count} expression picture{'s' if count != 1 else ''} ({size:.0f} MB)"
        )
        self.expressions.setToolTip(
            "Pictures of the character with other expressions, labelled with them, kept "
            "with the story: the People page can show the character with whichever fits"
        )
        self.expressions.setChecked(True)
        self.expressions.setVisible(bool(card.expressions))

        form = QFormLayout()
        form.addRow("The card is", kind_widget)
        form.addRow("You play", self.player)
        form.addRow("About you", self.about_player)
        form.addRow("Opening", self.opening)
        form.addRow("", self.preview)
        form.addRow("", self.instructions)
        form.addRow("", self.expressions)

        note = QLabel(
            "Setup opens next, to set the style (person, tense, length) before the story "
            "begins. The card's picture becomes the portrait (a world's, the background)."
        )
        note.setObjectName("hintLabel")
        note.setWordWrap(True)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Make the story")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addStretch(1)
        layout.addWidget(self.buttons)

        self.player.textChanged.connect(lambda _text: self._sync())
        self.opening.currentIndexChanged.connect(lambda _index: self._sync())
        self._sync()

    def _sync(self) -> None:
        name = self.player.text().strip()
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(bool(name))
        index = self.opening.currentData()
        fill = macro_filler(self.card.name, name or None)
        self.preview.setPlainText("" if index is None else fill(self.card.greetings[index]))
        self.preview.setVisible(index is not None)

    def options(self) -> dict[str, Any]:
        """`scenario_from_card`'s choices, as the dialog has them."""
        return {
            "player": self.player.text().strip(),
            "about_player": self.about_player.toPlainText(),
            "opening": self.opening.currentData(),
            "world": self.as_world.isChecked(),
            "instructions": bool(self.card.instructions) and self.instructions.isChecked(),
            "expressions": bool(self.card.expressions) and self.expressions.isChecked(),
        }


class LinkDialog(QDialog):
    """The link a card or a lorebook is fetched by, read as it is typed
    (`read_link`): one that can't be fetched says why before anything is
    sent."""

    def __init__(self, title: str, lead: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        size_to_screen(self, 0.35, 0.3, at_least=QSize(620, 280))
        heading = QLabel(lead)
        heading.setWordWrap(True)
        self.field = QLineEdit()
        self.field.setPlaceholderText("https://chub.ai/characters/…")
        self.field.setClearButtonEnabled(True)
        self.problem = QLabel()
        self.problem.setObjectName("warningLabel")
        self.problem.setWordWrap(True)
        self.problem.setTextFormat(Qt.PlainText)
        self.problem.hide()
        sites = QLabel(LINK_SITES)
        sites.setObjectName("hintLabel")
        sites.setWordWrap(True)
        buttons = QDialogButtonBox()
        self.import_button = QPushButton("Import")
        self.import_button.setDefault(True)
        buttons.addButton(self.import_button, QDialogButtonBox.AcceptRole)
        buttons.addButton(QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(self.field)
        layout.addWidget(self.problem)
        layout.addWidget(sites)
        layout.addStretch(1)
        layout.addWidget(buttons)
        # Taller when a problem shows, rather than its words over the note's.
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        self.field.textChanged.connect(self._check)
        self._check()

    def _check(self) -> None:
        text = self.link()
        problem = ""
        if text:
            try:
                read_link(text)
            except LinkError as exc:
                problem = str(exc)
        self.problem.setText(problem)
        self.problem.setVisible(bool(problem))
        self.import_button.setEnabled(bool(text) and not problem)

    def link(self) -> str:
        return self.field.text().strip()
