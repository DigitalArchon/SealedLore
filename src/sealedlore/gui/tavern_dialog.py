"""A new story from a SillyTavern character card: the few things the card
can't say (who you play, which greeting opens it, whether it is one
character or a world), asked before the story is made
(storage/tavern_cards.py `scenario_from_card`); and the link a card or a
lorebook is fetched by (providers/card_links.py)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePosixPath
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

from sealedlore.gui.cast_board import Thumbs, shown_in
from sealedlore.gui.lore_rules import LoreRulesChoice, settings_in_use
from sealedlore.gui.picture_cache import FIT
from sealedlore.gui.sizing import GrowingText, size_to_screen
from sealedlore.models.tavern import CardAsset
from sealedlore.providers.card_links import LinkError, read_link
from sealedlore.storage.card_files import GreetingPictures
from sealedlore.storage.tavern_cards import (
    TavernCard,
    first_sentence,
    greeting_shown,
    lore_from_card,
    macro_filler,
)

NO_OPENING = "None: I write the first turn"
# The Cast and Lore pages' menus, and Story → Export's (the manual names them).
TAVERN_LABEL = "SillyTavern cards"
CARD_IMPORT_LABEL = "Import a character card…"
CARD_EXPORT_LABEL = "Export this character as a card…"
EXPRESSIONS_LABEL = "Add expression pictures from a zip…"


def lore_brought(count: int, several: bool = False, dropped: int = 0) -> str:
    """Where a character's lorebook went, said after they join: ", with 6
    entries of their lorebook in the story's lore (the Lore page)", and
    `dropped`, how many the author unticked before it came."""
    if not count:
        return ""
    left = f", {dropped} left out as you chose" if dropped else ""
    return (
        f", with {count} {_entries(count)} of their "
        f"lorebook{'s' if several else ''} in the story's lore (the Lore page){left}"
    )


def _entries(count: int) -> str:
    return "entry" if count == 1 else "entries"


def _whose(names: Sequence[str]) -> str:
    """ "Elena's lorebook", "Elena's and Marta's lorebooks"."""
    owned = [f"{name}'s" for name in names]
    if len(owned) == 1:
        return f"{owned[0]} lorebook"
    return f"{', '.join(owned[:-1])} and {owned[-1]} lorebooks"


# Joining a story that exists, a character's own lorebook stays out unless
# the author brings it (the author, Nov 15 2026: their world's lore "breaks
# the game" in a story with a world of its own). Only a story made from a
# card takes it whole. Said wherever someone joins, with the reason.
BRING_LORE = "Bring their lorebook too"
BRING_LORES = "Bring their lorebooks too"


def joining_lore(names: Sequence[str], count: int, bring: bool) -> str:
    """What joining does with the lorebooks of `names` (those who have one,
    `count` entries in all), and why: shown beside the box that brings them."""
    if not names:
        return ""
    whose = _whose(names)
    several = len(names) > 1
    if bring:
        keeps = (
            "their cards keep their lorebooks as they are"
            if several
            else "their card keeps its lorebook as it is"
        )
        return (
            f"{whose} ({count} {_entries(count)}) will come into this story's lore. Next you "
            "can untick any entry that doesn't belong here and change any words, for this "
            f"story alone: {keeps}. The storyteller takes every entry that comes in as true "
            "of this story."
        )
    bring_only = (
        "Bring them only if they are about the characters alone; their cards and pictures "
        "come in either way."
        if several
        else "Bring it only if it is about the character alone; their card and pictures come "
        "in either way."
    )
    return (
        f"{whose} ({count} {_entries(count)}) {'stay' if several else 'stays'} out of this "
        "story. A character's lorebook is usually about their own world, and the storyteller "
        "takes everything in a story's lore as true of that story, so it would clash with the "
        f"world this one already has. {bring_only}"
    )


def lore_left_out(count: int, several: bool = False, chose: bool = False) -> str:
    """Said after someone joins without their lorebook: " Their lorebook (6
    entries) stayed out of the story's lore, as it is about their own
    world", or, when the author unticked every entry (`chose`), "as you
    chose"."""
    if not count:
        return ""
    whose = "Their lorebooks" if several else "Their lorebook"
    if chose:
        why = "as you chose"
    elif several:
        why = "as they are about their own worlds"
    else:
        why = "as it is about their own world"
    return f" {whose} ({count} {_entries(count)}) stayed out of the story's lore, {why}."


def lore_with_the_card(count: int, off: int) -> str:
    """A story made from a card's own scenario takes its lorebook whole,
    said in the dialog before it is made."""
    if not count:
        return ""
    stays_off = (
        f" ({off} switched off on the card {'stays' if off == 1 else 'stay'} off)" if off else ""
    )
    return (
        f"Every entry of the card's lorebook ({count}) becomes the story's lore{stays_off}: "
        "the story is set in the card's own world, so the storyteller takes them as true."
    )


LOREBOOK_LABEL = "SillyTavern lorebook"
LOREBOOK_IMPORT_LABEL = "Import a lorebook…"
LOREBOOK_EXPORT_LABEL = "Export the lore as a lorebook…"
# By link: File's (a new story from a card), the Cast and Lore pages' menus.
LINK_IMPORT_LABEL = "Import from a lin&k…"  # File's L is Lorebooks…
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


def _counted(count: int, one: str, many: str) -> str:
    """ "1 lore entry", "3 greetings"; "" for none."""
    return f"{count} {one if count == 1 else many}" if count else ""


def pictures_named(shown: GreetingPictures) -> str:
    """The pictures a greeting shows (storage/card_files.py), in words and
    in its order: "“Farmhouse” and their “happy” picture"; "" for none."""
    names = []
    for picture in shown.pictures:
        if isinstance(picture, CardAsset):
            name = picture.name or PurePosixPath(picture.path).name
            names.append(f"“{name}”" if name else "a picture of the card's")
        else:
            label = picture.expression.strip()
            names.append(f"their “{label}” picture" if label else "their picture")
    if len(names) < 2:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


class _CardPicture:
    """A card's own picture, as a store holding it alone (for `Thumbs`)."""

    FILE = "card"

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, relative: str) -> bytes | None:
        return self._data if relative == self.FILE else None


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
            # Read once the dialog is up (cast_board.Thumbs), from the card.
            own = _CardPicture(card.picture)
            self._thumbs = Thumbs(lambda: own, lambda: None, self, shape=FIT)
            self._thumbs.fill(_CardPicture.FILE, PICTURE_SIDE, PICTURE_SIDE, shown_in(picture))
        else:
            picture.hide()
        title = QLabel(card.name)
        title.setObjectName("dialogTitle")
        title.setStyleSheet("font-weight: bold;")
        # The card's own words, with the names where its macros for them
        # stand (`_sync`, as the name you play is typed).
        self.about = QLabel()
        self.about.setWordWrap(True)
        self.about.setTextFormat(Qt.PlainText)
        creator = card.text("creator").strip()
        book = len(card.book["entries"]) if card.book else 0
        facts = [
            f"by {creator}" if creator else "",
            _counted(len(card.greetings), "greeting", "greetings"),
            _counted(book, "lore entry", "lore entries"),
        ]
        details = QLabel(" · ".join(fact for fact in facts if fact))
        details.setObjectName("hintLabel")
        heading = QVBoxLayout()
        heading.addWidget(title)
        heading.addWidget(self.about)
        heading.addWidget(details)
        heading.addStretch(1)
        top = QHBoxLayout()
        top.addWidget(picture, 0, Qt.AlignTop)
        top.addLayout(heading, 1)

        self.as_character = QRadioButton(f"One character, played by the storyteller: {card.name}")
        self.as_world = QRadioButton("A world, or a narrator for several people")
        self.as_world.setToolTip(
            "The card's description becomes the story's world, with no card for "
            f"“{card.name}”; Fill with lore characters… on the cast board can write cards for "
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
        for number, _greeting in enumerate(card.greetings, start=1):
            self.opening.addItem(f"Greeting {number}", number - 1)
        self.opening.addItem(NO_OPENING, None)
        # The greeting as the story opens with it: the pictures it shows
        # taken out of its words, and named under it (`_sync`).
        self.preview = GrowingText(lines=4, most=10)
        self.preview.setReadOnly(True)
        self.preview_pictures = QLabel()
        self.preview_pictures.setObjectName("hintLabel")
        self.preview_pictures.setWordWrap(True)
        self.preview_pictures.setTextFormat(Qt.PlainText)

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
        form.addRow("", self.preview_pictures)
        form.addRow("", self.instructions)
        form.addRow("", self.expressions)
        book = lore_from_card(card, lambda text: text) if card.book else None
        made = book.entries if book is not None else []
        off = sum(not entry.enabled for entry in made)
        self.lore_rules: LoreRulesChoice | None = None
        if made:
            # Said before it is made: the card's whole lorebook comes, as a
            # story from the card is set in the card's world (one joining a
            # story that exists brings none unless asked).
            lore = QLabel(lore_with_the_card(len(made), off))
            lore.setWordWrap(True)
            lore.setTextFormat(Qt.PlainText)
            form.addRow("Lore", lore)
            self.lore_rules = LoreRulesChoice(uses=settings_in_use(book))
            form.addRow("Lore fires by", self.lore_rules)

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
        # A world shows none of the character's expression pictures, and
        # they come only when kept.
        self.as_world.toggled.connect(lambda _on: self._sync())
        self.expressions.toggled.connect(lambda _on: self._sync())
        self._sync()

    def _sync(self) -> None:
        name = self.player.text().strip()
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(bool(name))
        index = self.opening.currentData()
        fill = macro_filler(self.card.name, name or None)
        card = self.card
        self.about.setText(
            first_sentence(fill(card.text("description") or card.text("personality")), 300)
        )
        options = self.options()

        def shown(number: int) -> GreetingPictures:
            return greeting_shown(
                card,
                number,
                player=name or None,
                world=options["world"],
                expressions=options["expressions"],
            )

        for number in range(len(card.greetings)):
            self.opening.setItemText(
                number, f"Greeting {number + 1}: {_first_words(shown(number).text)}"
            )
        opened = None if index is None else shown(index)
        self.preview.setPlainText("" if opened is None else opened.text)
        self.preview.setVisible(opened is not None)
        named = pictures_named(opened) if opened is not None else ""
        said = f"Shown under the opening, as pictures: {named}." if named else ""
        self.preview_pictures.setText(said)
        self.preview_pictures.setVisible(bool(said))

    def options(self) -> dict[str, Any]:
        """`scenario_from_card`'s choices, as the dialog has them."""
        return {
            "player": self.player.text().strip(),
            "about_player": self.about_player.toPlainText(),
            "opening": self.opening.currentData(),
            "world": self.as_world.isChecked(),
            "instructions": bool(self.card.instructions) and self.instructions.isChecked(),
            "expressions": bool(self.card.expressions) and self.expressions.isChecked(),
            "lore_firing": self.lore_rules.value() if self.lore_rules else "sillytavern",
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
