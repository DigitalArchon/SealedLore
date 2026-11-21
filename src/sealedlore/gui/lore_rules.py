"""Which rules a story's lore fires by, chosen where a story is made from a
lorebook or a card with one (gui/lorebook_story.py, gui/tavern_dialog.py),
and said on the Lore page under its choice (gui/lore_panel.py): SealedLore's
own, or exactly SillyTavern's with each entry's own settings
(engine/tavern_firing.py). The author, Nov 21 2026: "make it easy for the
user to choose which ruleset to use"."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QLabel, QRadioButton, QVBoxLayout, QWidget

from sealedlore.storage.tavern_lore import KEPT_FOR_FIRING, LoreImport

RULES = ("sillytavern", "sealedlore")
NAMES = {"sillytavern": "SillyTavern's rules", "sealedlore": "SealedLore's rules"}
ABOUT = {
    "sillytavern": (
        "Each entry fires as it did in SillyTavern: by its own keywords and second "
        "keywords, its chance, sticky, cooldown and delay, inclusion groups and order, "
        "within SillyTavern's budget. Best for a lorebook written for SillyTavern."
    ),
    "sealedlore": (
        "A small lorebook is sent whole. From a large one, the entries the turn needs are "
        "chosen each turn by its words and their meaning, as Settings → Lore says, with "
        "room for long entries. Entries' SillyTavern settings are kept but not used."
    ),
}
# The SillyTavern settings an entry may carry, for saying what a book uses.
_USES = {
    "secondary": "second keywords",
    "probability": "a chance below 100%",
    "timing": "sticky, cooldown or delay",
    "group": "inclusion groups",
    "regex": "keywords written as patterns",
    "recursion": "firing only from other entries",
    "filter": "character filters",
}


def settings_in_use(imported: LoreImport) -> str:
    """The SillyTavern settings a lorebook's entries use that only
    SillyTavern's rules read, in words; "" for none."""
    used = [
        f"{words} ({imported.left_out[kind]})"
        for kind, words in _USES.items()
        if kind in KEPT_FOR_FIRING and imported.left_out[kind]
    ]
    return ", ".join(used)


class LoreRulesChoice(QWidget):
    """Two choices, each with what it does; `uses`, what of the book only
    SillyTavern's rules read, said under them."""

    changed = Signal(str)

    def __init__(
        self, value: str = "sillytavern", *, uses: str = "", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._group = QButtonGroup(self)
        self._buttons: dict[str, QRadioButton] = {}
        for rule in RULES:
            button = QRadioButton(NAMES[rule])
            about = QLabel(ABOUT[rule])
            about.setObjectName("hintLabel")
            about.setWordWrap(True)
            about.setTextFormat(Qt.PlainText)
            about.setContentsMargins(22, 0, 0, 4)
            self._group.addButton(button)
            self._buttons[rule] = button
            layout.addWidget(button)
            layout.addWidget(about)
            button.toggled.connect(lambda on, rule=rule: on and self.changed.emit(rule))
        if uses:
            said = QLabel(f"Its entries use {uses}: only SillyTavern's rules read them.")
            said.setObjectName("hintLabel")
            said.setWordWrap(True)
            said.setTextFormat(Qt.PlainText)
            layout.addWidget(said)
        self.set_value(value)

    def value(self) -> str:
        return next(
            (rule for rule, button in self._buttons.items() if button.isChecked()), RULES[0]
        )

    def set_value(self, value: str) -> None:
        self._buttons.get(value, self._buttons[RULES[0]]).setChecked(True)
