"""Write a story from scratch, a step at a time.

It used to be a bare "Title" box and then the whole of Setup, six tabs
deep, with the cast to be added afterwards on the Cast page (the author,
Oct 18 2026: the window looked confusing to a new user). The guide asks for
what a story needs to begin, in four short steps: the story and its world,
who you play (and anyone else to start with), where and how it begins, and
how it reads. Everything else keeps its default, and is in Setup and on the
Cast page as before; "Skip to the full setup" makes the story with what is
filled in and opens Setup instead.

The guide only builds the bundle (`bundle()`); the window saves, opens and
starts it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.response_style import PRESET_ORDER, PRESETS
from sealedlore.models.character import Character
from sealedlore.models.story import DEFAULT_RESPONSE_STYLE, Story
from sealedlore.storage.repository import StoryBundle

STEPS = ("The story", "Who you play", "How it begins", "How it reads")
# Others to start with, beside the author's own; the Cast page takes more.
OTHERS = 2
PERSONS = (
    ("Second person: “you walk in”", "second"),
    ("First person: “I walk in”", "first"),
    ("Third person: “she walks in”", "third"),
)
TENSES = (("Past: “you walked”", "past"), ("Present: “you walk”", "present"))


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hintLabel")
    label.setWordWrap(True)
    return label


def _page() -> tuple[QWidget, QFormLayout]:
    """A step: its form at the top, the spare height below it."""
    page = QWidget()
    outer = QVBoxLayout(page)
    form = QFormLayout()
    outer.addLayout(form)
    outer.addStretch(1)
    return page, form


def _text(placeholder: str, height: int = 90) -> QPlainTextEdit:
    edit = QPlainTextEdit()
    edit.setPlaceholderText(placeholder)
    edit.setTabChangesFocus(True)
    edit.setFixedHeight(height)
    return edit


class NewStoryGuide(QDialog):
    """Four steps to a story ready to start (see the module). After exec:
    `bundle()` is the story made, `full_setup` whether the author asked for
    the full Setup instead of starting."""

    def __init__(self, parent: QWidget | None = None, *, model: str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Write a story from scratch")
        self.setMinimumSize(620, 520)
        self._model = model
        self.full_setup = False
        self._bundle: StoryBundle | None = None

        self.steps_label = QLabel()
        self.steps_label.setObjectName("guideSteps")
        self.steps_label.setTextFormat(Qt.RichText)
        self.stack = QStackedWidget()
        self.stack.addWidget(self._story_page())
        self.stack.addWidget(self._player_page())
        self.stack.addWidget(self._opening_page())
        self.stack.addWidget(self._style_page())

        self.skip_button = QPushButton("Skip to the full setup…")
        self.skip_button.setToolTip(
            "Make the story with what is filled in so far and open Setup, with every "
            "option, instead"
        )
        self.skip_button.clicked.connect(self._skip)
        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(lambda: self._go(self.stack.currentIndex() - 1))
        self.next_button = QPushButton("Next")
        self.next_button.setDefault(True)
        self.next_button.clicked.connect(self._next)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(self.skip_button)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(self.back_button)
        buttons.addWidget(self.next_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.steps_label)
        layout.addWidget(self.stack, 1)
        layout.addLayout(buttons)
        self.title.textChanged.connect(self._sync)
        self._go(0)

    # --- the steps ----------------------------------------------------------

    def _story_page(self) -> QWidget:
        page, form = _page()
        self.title = QLineEdit()
        self.title.setPlaceholderText("The Long Night")
        form.addRow("Title", self.title)
        self.world = _text(
            "Where and when the story is set, how its world works, what tone it has. "
            "A few sentences is enough.",
            140,
        )
        form.addRow("The world", self.world)
        form.addRow(
            "",
            _hint(
                "Standing facts the storyteller always has. Detail that matters only "
                "sometimes can go in Lore later."
            ),
        )
        self.map_box = QCheckBox("Keep a map of the places the story goes")
        self.map_box.setToolTip(
            "The story is played on a map that grows as it goes, drawn as you play. "
            "It can be turned on or off later in Setup."
        )
        form.addRow("", self.map_box)
        return page

    def _player_page(self) -> QWidget:
        page, form = _page()
        form.addRow(
            _hint(
                "The character you play. The storyteller never speaks or acts for them. "
                "Leave the name blank to direct the story without a character of your own."
            )
        )
        self.player_name = QLineEdit()
        self.player_name.setPlaceholderText("Name")
        form.addRow("You play", self.player_name)
        self.player_about = _text("Who they are: age, looks, what they can do, what they want.")
        form.addRow("Who they are", self.player_about)
        form.addRow(_hint("Anyone else to start with (optional; the Cast page adds more):"))
        self.others: list[tuple[QLineEdit, QLineEdit]] = []
        for _ in range(OTHERS):
            name = QLineEdit()
            name.setPlaceholderText("Name")
            about = QLineEdit()
            about.setPlaceholderText("Who they are, in a line")
            row = QHBoxLayout()
            row.addWidget(name, 1)
            row.addWidget(about, 2)
            form.addRow("Someone else", row)
            self.others.append((name, about))
        return page

    def _opening_page(self) -> QWidget:
        page, form = _page()
        self.location = QLineEdit()
        self.location.setPlaceholderText("A farmhouse in a snowstorm, the bridge of a ship…")
        form.addRow("Where it begins", self.location)
        self.opening = _text(
            "What is happening as the story opens, and anything the first passage should show.",
            140,
        )
        form.addRow("What happens first", self.opening)
        form.addRow(
            "",
            _hint(
                "The storyteller writes the opening passage from this when you start. "
                "Leave it blank to write the first turn yourself."
            ),
        )
        return page

    def _style_page(self) -> QWidget:
        page, form = _page()
        self.person = QComboBox()
        for label, value in PERSONS:
            self.person.addItem(label, value)
        form.addRow("Told in", self.person)
        self.tense = QComboBox()
        for label, value in TENSES:
            self.tense.addItem(label, value)
        form.addRow("Tense", self.tense)
        self.length = QComboBox()
        for key in PRESET_ORDER:
            if key == "custom":
                continue
            self.length.addItem(f"{PRESETS[key].label}: {PRESETS[key].summary}", key)
        self.length.setCurrentIndex(max(0, self.length.findData(DEFAULT_RESPONSE_STYLE)))
        form.addRow("Passages", self.length)
        form.addRow(
            "",
            _hint(
                "Person and tense stay as they are once the story has begun; the length "
                "can change any time, for one turn or for good. Everything else is in "
                "Story → Setup… and on the Style page."
            ),
        )
        return page

    # --- moving between them --------------------------------------------------

    def _go(self, index: int) -> None:
        index = max(0, min(index, self.stack.count() - 1))
        self.stack.setCurrentIndex(index)
        marks = []
        for number, name in enumerate(STEPS):
            text = f"{number + 1}. {name}"
            marks.append(f"<b>{text}</b>" if number == index else text)
        self.steps_label.setText("&nbsp;&nbsp;·&nbsp;&nbsp;".join(marks))
        self.back_button.setEnabled(index > 0)
        last = index == self.stack.count() - 1
        self.next_button.setText("Create the story" if last else "Next")
        self._sync()

    def _sync(self) -> None:
        ready = bool(self.title.text().strip())
        self.next_button.setEnabled(ready or self.stack.currentIndex() == 0)
        self.skip_button.setEnabled(ready)
        self.next_button.setToolTip("" if ready else "The story needs a title")

    def _next(self) -> None:
        if not self.title.text().strip():
            self.title.setFocus()
            return
        if self.stack.currentIndex() < self.stack.count() - 1:
            self._go(self.stack.currentIndex() + 1)
            return
        self._bundle = self._build()
        self.accept()

    def _skip(self) -> None:
        if not self.title.text().strip():
            return
        self._bundle = self._build()
        self.full_setup = True
        self.accept()

    # --- the story ------------------------------------------------------------

    def bundle(self) -> StoryBundle | None:
        return self._bundle

    def _build(self) -> StoryBundle:
        story = Story(title=self.title.text().strip())
        if self._model:
            story.defaults.main_model = self._model
        story.world_bible = self.world.toPlainText().strip() or None
        story.defaults.world_map = self.map_box.isChecked()
        story.style.person = self.person.currentData()
        story.style.tense = self.tense.currentData()
        story.style.response_style = self.length.currentData()
        cast: list[Character] = []
        name = self.player_name.text().strip()
        if name:
            player = Character(name=name, summary=self.player_about.toPlainText().strip() or None)
            cast.append(player)
            story.setup.suggested_character_id = player.id
            # Where it begins, with them in it: the opening places the rest.
            story.setup.starting_scene.present_character_ids = [player.id]
        for other_name, other_about in self.others:
            if other_name.text().strip():
                cast.append(
                    Character(
                        name=other_name.text().strip(),
                        summary=other_about.text().strip() or None,
                    )
                )
        story.setup.starting_scene.location = self.location.text().strip() or None
        story.setup.opening_text = self.opening.toPlainText().strip()
        story.setup.opening_mode = "generate"
        story.scene = story.setup.starting_scene.model_copy(deep=True)
        return StoryBundle(story=story, cast=cast)
