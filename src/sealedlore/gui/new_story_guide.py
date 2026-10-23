"""Write a story from scratch, a step at a time.

It used to be a bare "Title" box and then the whole of Setup, six tabs
deep, with the cast to be added afterwards on the Cast page (the author,
Oct 18 2026: the window looked confusing to a new user). The guide asks for
what a story needs to begin, in four short steps: the story and its world,
its characters, where and how it begins, and how it reads. Everything else
keeps its default, and is in Setup and on the Cast page as before; "Skip to
the full setup" (Advanced setup… since Oct 2026) opens Setup instead.

**The story is made after the first step** (the author, Oct 2026): its
characters are made on the real Cast page, lent to the guide by the window
(`lend_cast_panel`), with everything a card has, pictures drawn or chosen
included, and Fill with lore characters… for a story set in a world that
already exists. The second step used to take a name and a line each for the
author's character and two others, and a story in a canon world brought in
the first few canon characters and seldom the rest. Cancel after the first
step asks whether to keep the story made so far or move it to the trash.

The guide changes the story through its host, the window (`GuideHost`); the
window starts it, or opens Setup, once the guide is accepted.
"""

from __future__ import annotations

from typing import Protocol

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.response_style import PRESET_ORDER, PRESETS
from sealedlore.gui.sizing import GrowingText, size_to_screen
from sealedlore.models.story import DEFAULT_RESPONSE_STYLE, Story
from sealedlore.storage.repository import StoryBundle

STEPS = ("The story", "Characters", "How it begins", "How it reads")
CHARACTERS_STEP = 1
PERSONS = (
    ("Second person: “you walk in”", "second"),
    ("First person: “I walk in”", "first"),
    ("Third person: “she walks in”", "third"),
)
TENSES = (("Past: “you walked”", "past"), ("Present: “you walk”", "present"))
NOBODY = "Nobody: I'll direct the story"
CHARACTERS_ABOUT = (
    "Make as many or as few characters as you like, in as much or as little "
    "detail: a name is enough to begin. The more you put on their cards, the "
    "more say you have in who they are and how they are written; anyone you "
    "leave out, the storyteller makes up as the story needs them. Pictures can "
    "be drawn or chosen for each card here too. Set in a world that already "
    "exists? Fill with lore characters… writes cards for its people."
)


class GuideHost(Protocol):
    """What the guide needs of the window (gui/window_cast.py)."""

    session: object

    def guide_make_story(self, bundle: StoryBundle) -> bool: ...
    def guide_story_changed(self, *, reopen: bool = False) -> None: ...
    def guide_discard_story(self) -> None: ...
    def guide_busy(self) -> bool: ...
    def lend_cast_panel(self) -> QWidget: ...
    def return_cast_panel(self) -> None: ...


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hintLabel")
    label.setWordWrap(True)
    return label


def _page(*, fill: bool = False) -> tuple[QWidget, QFormLayout]:
    """A step: its form at the top. With `fill`, a field of the form takes
    the spare height (a text box that grows with the window); else it is
    left below the form."""
    page = QWidget()
    outer = QVBoxLayout(page)
    form = QFormLayout()
    outer.addLayout(form, 1 if fill else 0)
    if not fill:
        outer.addStretch(1)
    return page, form


def _text(placeholder: str, *, lines: int = 3, fill: bool = False) -> GrowingText:
    """A box that grows with what is written, and with `fill` with the window
    too (the author: a world written in a box that only grew wider felt
    cramped)."""
    edit = GrowingText(lines=lines, fill=fill)
    edit.setPlaceholderText(placeholder)
    edit.setTabChangesFocus(True)
    return edit


class NewStoryGuide(QDialog):
    """Four steps to a story ready to start (see the module). After exec:
    `made` whether the story was made, `full_setup` whether the author asked
    for the full Setup instead of starting, `discarded` whether a story made
    was moved to the trash on Cancel."""

    def __init__(self, host: GuideHost, *, model: str | None = None) -> None:
        super().__init__(host if isinstance(host, QWidget) else None)
        self.setWindowTitle("Write a story from scratch")
        self.setMinimumSize(620, 520)
        self._host = host
        self._model = model
        self.made = False
        self.full_setup = False
        self.discarded = False
        self._panel: QWidget | None = None
        self._map_when_made = False
        self._player_timer = QTimer(self)
        self._player_timer.setSingleShot(True)
        self._player_timer.timeout.connect(self._fill_players)

        self.steps_label = QLabel()
        self.steps_label.setObjectName("guideSteps")
        self.steps_label.setTextFormat(Qt.RichText)
        self.stack = QStackedWidget()
        self.stack.addWidget(self._story_page())
        self.stack.addWidget(self._characters_page())
        self.stack.addWidget(self._opening_page())
        self.stack.addWidget(self._style_page())

        # "Skip to the full setup…" read as if the full setup came later
        # (the author, Oct 2026); it is the other way to set a story up.
        self.skip_button = QPushButton("Advanced setup…")
        self.skip_button.setToolTip(
            "Make the story with what is filled in so far and open Setup, with every "
            "option, instead of this guide"
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
        size_to_screen(self, 0.42, 0.66)

    # --- the steps ----------------------------------------------------------

    def _story_page(self) -> QWidget:
        page, form = _page(fill=True)
        self.title = QLineEdit()
        self.title.setPlaceholderText("The Long Night")
        form.addRow("Title", self.title)
        self.world = _text(
            "Where and when the story is set, how its world works, what tone it has. "
            "A few sentences is enough. Set in a world that already exists? Name it, "
            "and say where this story stands in it.",
            lines=6,
            fill=True,
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

    def _characters_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_hint(CHARACTERS_ABOUT))
        row = QHBoxLayout()
        row.addWidget(QLabel("You play"))
        self.player = QComboBox()
        self.player.setMinimumContentsLength(18)
        self.player.setToolTip(
            "The character you play. The storyteller never speaks or acts for them. "
            "You can play someone else once the story has begun."
        )
        row.addWidget(self.player, 1)
        row.addStretch(1)
        layout.addLayout(row)
        # The window's Cast page stands here while this step shows.
        self._panel_host = QVBoxLayout()
        layout.addLayout(self._panel_host, 1)
        return page

    def _opening_page(self) -> QWidget:
        page, form = _page(fill=True)
        self.location = QLineEdit()
        self.location.setPlaceholderText("A farmhouse in a snowstorm, the bridge of a ship…")
        form.addRow("Where it begins", self.location)
        self.opening = _text(
            "What is happening as the story opens, and anything the first passage should show.",
            lines=6,
            fill=True,
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
            self.length.addItem(f"{PRESETS[key].label}: {PRESETS[key].summary}", key)
        self.length.setCurrentIndex(max(0, self.length.findData(DEFAULT_RESPONSE_STYLE)))
        form.addRow("Passages", self.length)
        # One's own length wording (the author, Oct 2026: the quick setup
        # offered every preset but a custom one), as the Style page offers it.
        self.custom_length = QLineEdit()
        self.custom_length.setPlaceholderText("e.g. 'two tight paragraphs, dialogue-led'")
        self.custom_length.setToolTip(
            "Your own words for how long a passage should be, when Passages is Custom"
        )
        self.custom_length.setEnabled(False)
        self.length.currentIndexChanged.connect(
            lambda _i: self.custom_length.setEnabled(self.length.currentData() == "custom")
        )
        form.addRow("Custom length", self.custom_length)
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
        if self.made:
            self._apply()
        if index == CHARACTERS_STEP and not self._enter_characters():
            return
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

    def _waiting(self) -> bool:
        """A job of the window's (lore characters being written) runs: the
        story is the worker's until it is done."""
        return self._host.guide_busy()

    def _next(self) -> None:
        if not self.title.text().strip():
            self.title.setFocus()
            return
        if self._waiting():
            return
        if self.stack.currentIndex() < self.stack.count() - 1:
            self._go(self.stack.currentIndex() + 1)
            return
        if self._finish():
            self.accept()

    def _skip(self) -> None:
        if not self.title.text().strip() or self._waiting():
            return
        if not self.made and not self._make():
            return
        self.full_setup = True
        if self._finish():
            self.accept()

    # --- the story ------------------------------------------------------------

    def _make(self) -> bool:
        """The story, made with what the first step holds, and opened in the
        window behind the guide."""
        story = Story(title=self.title.text().strip())
        if self._model:
            story.defaults.main_model = self._model
        story.world_bible = self.world.toPlainText().strip() or None
        story.defaults.world_map = self.map_box.isChecked()
        self._map_when_made = story.defaults.world_map
        self.made = self._host.guide_make_story(StoryBundle(story=story))
        return self.made

    def _story(self) -> Story | None:
        session = self._host.session
        return session.story if session is not None else None  # type: ignore[attr-defined]

    def _cast(self) -> list:
        session = self._host.session
        return session.cast if session is not None else []  # type: ignore[attr-defined]

    def _enter_characters(self) -> bool:
        if not self.made and not self._make():
            return False
        if self._panel is None:
            self._panel = self._host.lend_cast_panel()
            self._panel_host.addWidget(self._panel)
            self._panel.show()
            model = self._panel.list.model()
            for signal in (model.rowsInserted, model.rowsRemoved, model.dataChanged):
                signal.connect(self._player_timer.start)
            # The card's portrait at the left, the fields beside it: room.
            size_to_screen(self, 0.62, 0.86)
        if not self._cast():
            # A card to begin with, its name ready to type: the author's own.
            self._panel.add_character()
        self._fill_players()
        return True

    def _fill_players(self) -> None:
        story = self._story()
        current = self.player.currentData()
        if current is None and story is not None:
            current = story.setup.suggested_character_id
        self.player.blockSignals(True)
        self.player.clear()
        for character in self._cast():
            self.player.addItem(character.name or "(unnamed)", character.id)
        self.player.addItem(NOBODY, "")
        index = self.player.findData(current) if current is not None else -1
        self.player.setCurrentIndex(index if index >= 0 else 0)
        self.player.blockSignals(False)

    def _apply(self) -> None:
        """Everything the steps hold, onto the story made."""
        story = self._story()
        if story is None:
            return
        story.title = self.title.text().strip() or story.title
        story.world_bible = self.world.toPlainText().strip() or None
        story.defaults.world_map = self.map_box.isChecked()
        story.style.person = self.person.currentData()
        story.style.tense = self.tense.currentData()
        story.style.response_style = self.length.currentData()
        if story.style.response_style == "custom":
            story.style.length_target = self.custom_length.text().strip() or None
        ids = {character.id for character in self._cast()}
        player = self.player.currentData() or None
        if player not in ids:
            player = None
        setup = story.setup
        setup.suggested_character_id = player
        # Where it begins, with them in it: the opening places the rest.
        setup.starting_scene.present_character_ids = [player] if player else []
        setup.starting_scene.location = self.location.text().strip() or None
        setup.opening_text = self.opening.toPlainText().strip()
        setup.opening_mode = "generate"
        story.scene = setup.starting_scene.model_copy(deep=True)
        self._host.guide_story_changed()

    def _finish(self) -> bool:
        if not self.made and not self._make():
            return False
        self._apply()
        story = self._story()
        if story is not None and story.defaults.world_map != self._map_when_made:
            # A map turned on or off after the story was opened: open it again.
            self._host.guide_story_changed(reopen=True)
        return True

    # --- leaving ----------------------------------------------------------------

    def reject(self) -> None:
        if self.made and not self.discarded:
            if self._waiting():
                return
            answer = self._ask_leave()
            if answer == "discard":
                self._give_back()
                self._host.guide_discard_story()
                self.discarded = True
            elif answer == "keep":
                self._apply()
            else:
                return
        super().reject()

    def _ask_leave(self) -> str:
        """Cancel with a story made: "keep", "discard" or "back"."""
        box = QMessageBox(self)
        box.setWindowTitle("Leave the guide")
        box.setText("Keep the story made so far?")
        box.setInformativeText(
            "Kept, it is in your Stories list, not begun: Story → Start story… when it "
            "is ready. Discarded, it goes to the Trash with its cards and pictures."
        )
        keep = box.addButton("Keep it", QMessageBox.AcceptRole)
        discard = box.addButton("Discard it", QMessageBox.DestructiveRole)
        box.addButton("Go back", QMessageBox.RejectRole)
        box.setDefaultButton(keep)
        box.exec()
        clicked = box.clickedButton()
        return "keep" if clicked is keep else "discard" if clicked is discard else "back"

    def done(self, result: int) -> None:
        self._give_back()
        super().done(result)

    def _give_back(self) -> None:
        if self._panel is None:
            return
        model = self._panel.list.model()
        for signal in (model.rowsInserted, model.rowsRemoved, model.dataChanged):
            signal.disconnect(self._player_timer.start)
        self._player_timer.stop()
        self._panel = None
        self._host.return_cast_panel()
