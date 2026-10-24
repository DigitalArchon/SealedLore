"""The People page: who is in the story, by their faces.

The author (Oct 18 2026): the start page's graphical look should carry into
play. A story with a map has the map beside it; one without should have
"characters and their profiles to keep things visual, along with easy ways
to get people who play without a map started with portraits and stuff to
suit their story". So a story without a map opens on this page: who is here
now, large, the author's own character first; who is elsewhere and the
supporting characters smaller; each card a portrait, a name and the first
line of their summary. A click opens the profile (the Cast page); a card with
no portrait offers to draw one or choose a picture, and the panel at the top
offers portraits for everyone who has none and a background behind the
story.

Who is here is the scene card's, as the Scene page shows it (playtesting:
the scene read is wrong too often to go unwatched; here it is watched by
face), and "Correct the scene…" goes there. The page reads; the window acts.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QFont, QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.scene_state import same_person
from sealedlore.gui import theme
from sealedlore.models.character import Character
from sealedlore.models.scene import SceneState

# A portrait's shape on its card: the portraits are drawn tall (3:4 or 2:3).
ASPECT = 4 / 3
# The tallest a portrait is drawn: those here, and everyone else.
HERE_MAX_HEIGHT = 300
OTHERS_MAX_HEIGHT = 200
# The width a portrait is read at, whatever the card: enough for the widest.
READ_AT = QSize(480, 640)

HERE_HEADING = "Here now"
AWAY_HEADING = "Not here"
OTHERS_HEADING = "Also in the story"
CAST_HEADING = "The cast"
SUPPORTING_HEADING = "Supporting characters"
NOT_BEGUN = (
    "The story hasn't begun. Story → Start story… chooses who you play, and the "
    "opening places everyone."
)
CARD_TIP = "Click to open {name}'s profile. Right-click for more."
ALSO_HERE_HEADING = "Also here, with no card yet"
NAME_LABEL = "Name…"
# How the map's scene lists someone it has no name for (engine/map_presence.py).
NOT_NAMED_SUFFIX = " (not named yet)"
MAKE_CARD_LABEL = "Make a card"


def card_choices(name: str) -> list[tuple[str, bool]]:
    """Make a card's choices for someone with none: (words, into the cast)."""
    return [
        (f"Make a card for {name} (a supporting character)", False),
        (f"Make a card for {name} in the cast (you can play them)", True),
    ]


@dataclass(frozen=True)
class Person:
    """One card, as the page shows it."""

    card_id: str
    name: str
    # The first sentence of their summary.
    line: str
    # Their first picture's file in the story's store (the portrait), or "".
    picture: str
    you: bool = False
    playable: bool = False
    # Where they are, when it is known and not here.
    note: str = ""


@dataclass
class PeopleView:
    """Everything the page shows, worked out from the story by `people_view`."""

    story_id: str = ""
    location: str = ""
    situation: str = ""
    begun: bool = True
    here: list[Person] = field(default_factory=list)
    away: list[Person] = field(default_factory=list)
    others: list[Person] = field(default_factory=list)
    # People named in the scene with no card ("a guard, Gus").
    also_here: list[str] = field(default_factory=list)
    # In a story played on the map, people it has here with no name yet:
    # (the map's label, "Unknown 1", and how the story described them).
    unnamed: list[tuple[str, str]] = field(default_factory=list)
    # Names of the visible characters with no picture that one can be drawn of.
    missing: list[str] = field(default_factory=list)
    # Whether an image endpoint is set up, and whether the story has a background.
    pictures_ready: bool = False
    background: bool = False


def first_sentence(text: str | None, limit: int = 140) -> str:
    """A summary's first sentence, or its first `limit` characters."""
    text = " ".join((text or "").split())
    for end in (". ", "! ", "? "):
        at = text.find(end)
        if 0 < at < limit:
            return text[: at + 1]
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _person(character: Character, held_id: str | None, note: str = "") -> Person:
    first = character.reference_images[0].file if character.reference_images else ""
    return Person(
        card_id=character.id,
        name=character.name,
        line=first_sentence(character.summary or character.full_description),
        picture=first,
        you=character.id == held_id,
        playable=character.is_player_available,
        note=note,
    )


def people_view(
    cast: Sequence[Character],
    supporting: Sequence[Character],
    scene: SceneState,
    held_id: str | None,
    *,
    story_id: str = "",
    begun: bool = True,
    missing: Sequence[str] = (),
    pictures_ready: bool = False,
    background: bool = False,
    unnamed: Sequence[tuple[str, str]] = (),
) -> PeopleView:
    """Who is where, from the scene card. `cast` and `supporting` are the
    cards the author may see (the plot's hidden ones already left out).
    `unnamed`: on a map, the people it has here not named yet (label,
    description); the map's scene lists them as "… (not named yet)"."""
    present = set(scene.present_character_ids)
    notes = {entry.character_id: entry.note for entry in scene.offstage_but_nearby}
    view = PeopleView(
        story_id=story_id,
        location=(scene.location or "").strip(),
        situation=(scene.situation or "").strip(),
        begun=begun,
        missing=list(missing),
        pictures_ready=pictures_ready,
        background=background,
        unnamed=list(unnamed),
    )
    for character in cast:
        if character.id in present:
            view.here.append(_person(character, held_id))
        else:
            view.away.append(_person(character, held_id, notes.get(character.id, "")))
    for character in supporting:
        if any(same_person(name, character) for name in scene.present_others):
            view.here.append(_person(character, held_id))
        else:
            view.others.append(_person(character, held_id))
    view.also_here = [
        name
        for name in scene.present_others
        if not any(same_person(name, character) for character in supporting)
        and not any(same_person(name, character) for character in cast)
        and not name.endswith(NOT_NAMED_SUFFIX)
    ]
    # The author's own character first.
    view.here.sort(key=lambda person: not person.you)
    return view


def _initials(name: str) -> str:
    words = [word for word in name.split() if word[:1].isalpha()]
    return "".join(word[0].upper() for word in words[:2]) or "?"


def _placeholder(name: str, size: QSize) -> QPixmap:
    """A card with no portrait: their initials on the panel's ground."""
    pixmap = QPixmap(size)
    pixmap.fill(theme.colour("button"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    font = QFont()
    font.setPixelSize(max(18, size.height() // 4))
    font.setWeight(QFont.DemiBold)
    painter.setFont(font)
    painter.setPen(theme.colour("text_faint"))
    painter.drawText(pixmap.rect(), int(Qt.AlignCenter), _initials(name))
    painter.end()
    return pixmap


class PersonCard(QFrame):
    """A portrait, a name and a line; the whole card opens the profile."""

    profile_requested = Signal(str)
    draw_requested = Signal(str)
    file_requested = Signal(str)
    play_requested = Signal(str)

    def __init__(self, person: Person, *, max_height: int, can_draw: bool, can_play: bool) -> None:
        super().__init__()
        self.person = person
        self._max_height = max_height
        self._can_draw = can_draw
        self._can_play = can_play
        self._pixmap = QPixmap()
        self.setObjectName("personCard")
        self.setProperty("you", "true" if person.you else "false")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setToolTip(CARD_TIP.format(name=person.name))
        self.setAccessibleName(person.name)
        self.setAccessibleDescription(person.line)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.picture = QLabel()
        self.picture.setObjectName("personPicture")
        self.picture.setAlignment(Qt.AlignCenter)
        # Cut to the card, never setting its width (the start page's lesson:
        # a pixmap's width held the cards wider than the page).
        self.picture.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.picture.setMinimumWidth(1)
        layout.addWidget(self.picture)
        words = QVBoxLayout()
        words.setContentsMargins(10, 8, 10, 10)
        words.setSpacing(2)
        if person.you:
            badge = QLabel("You")
            badge.setObjectName("personYou")
            words.addWidget(badge)
        self.name = QLabel(person.name)
        self.name.setObjectName("personName")
        self.name.setWordWrap(True)
        words.addWidget(self.name)
        if person.note:
            note = QLabel(person.note)
            note.setObjectName("personNote")
            note.setWordWrap(True)
            words.addWidget(note)
        if person.line:
            line = QLabel(person.line)
            line.setObjectName("personLine")
            line.setWordWrap(True)
            line.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            words.addWidget(line)
        if not person.picture:
            # The way to a face, on the card itself.
            row = QHBoxLayout()
            row.setSpacing(6)
            if can_draw:
                draw = QToolButton()
                draw.setObjectName("personAction")
                draw.setText("Draw a portrait…")
                draw.clicked.connect(lambda: self.draw_requested.emit(person.card_id))
                row.addWidget(draw)
            choose = QToolButton()
            choose.setObjectName("personAction")
            choose.setText("Choose a picture…")
            choose.clicked.connect(lambda: self.file_requested.emit(person.card_id))
            row.addWidget(choose)
            row.addStretch(1)
            words.addSpacing(4)
            words.addLayout(row)
        words.addStretch(1)
        layout.addLayout(words, 1)
        if not person.picture:
            self.set_pixmap(QPixmap())

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        self._fit()

    def _fit(self) -> None:
        width = max(1, self.width())
        height = min(self._max_height, int(width * ASPECT))
        self.picture.setFixedHeight(height)
        box = QSize(width, height)
        if self._pixmap.isNull():
            self.picture.setPixmap(_placeholder(self.person.name, box))
            return
        scaled = self._pixmap.scaled(box, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        # A face is near the top of a portrait: cut from the top, centred.
        x = max(0, (scaled.width() - width) // 2)
        y = max(0, min((scaled.height() - height) // 6, scaled.height() - height))
        self.picture.setPixmap(scaled.copy(x, y, width, height))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._fit()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.profile_requested.emit(self.person.card_id)
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.profile_requested.emit(self.person.card_id)
            return
        super().keyPressEvent(event)

    def menu_actions(self) -> list[tuple[str, Callable[[], None]]]:
        """What the right-click menu offers, in order."""
        person = self.person
        actions: list[tuple[str, Callable[[], None]]] = [
            ("Open the profile", lambda: self.profile_requested.emit(person.card_id)),
        ]
        if self._can_draw:
            label = "Draw a new portrait…" if person.picture else "Draw a portrait…"
            actions.append((label, lambda: self.draw_requested.emit(person.card_id)))
        actions.append(("Choose a picture…", lambda: self.file_requested.emit(person.card_id)))
        if self._can_play and person.playable and not person.you:
            actions.append(
                (f"Play as {person.name}", lambda: self.play_requested.emit(person.card_id))
            )
        return actions

    def _menu(self, at: QPoint) -> None:
        menu = QMenu(self)
        for label, act in self.menu_actions():
            menu.addAction(label).triggered.connect(act)
        menu.exec(self.mapToGlobal(at))


class PeoplePage(QScrollArea):
    """The page (see the module). `pictures` returns the open story's picture
    store, as the Cast page's does."""

    profile_requested = Signal(str)
    draw_requested = Signal(str)
    file_requested = Signal(str)
    play_requested = Signal(str)
    draw_all_requested = Signal()
    background_requested = Signal()
    background_file_requested = Signal()
    settings_requested = Signal()
    scene_requested = Signal()
    add_requested = Signal()
    # Make a card for someone here with none: their name, into the cast.
    card_requested = Signal(str, bool)
    # Name someone the map has here with no name yet: their label.
    name_requested = Signal(str)

    def __init__(self, pictures: Callable[[], object]) -> None:
        super().__init__()
        self.setObjectName("peoplePage")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._pictures = pictures
        self._view = PeopleView()
        self._columns: tuple[int, int] = (0, 0)
        # Portraits read once each, for the story they are from.
        self._read: dict[str, QPixmap] = {}
        self._read_for: object = None
        # "Not now" on the panel at the top, by story, for this session.
        self._tips_put_away: set[str] = set()
        self.cards: dict[str, PersonCard] = {}
        self.tip: QFrame | None = None
        self.tip_buttons: dict[str, QPushButton] = {}
        self.set_view(PeopleView())

    # --- what it shows ------------------------------------------------------

    def set_view(self, view: PeopleView) -> None:
        self._view = view
        self._build()

    def view(self) -> PeopleView:
        return self._view

    def _columns_for(self, width: int) -> tuple[int, int]:
        """Cards a row: those here, and everyone else."""
        here = 3 if width >= 620 else 2
        others = 4 if width >= 600 else 3 if width >= 420 else 2
        return here, others

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        if self._columns_for(self.viewport().width()) != self._columns:
            self._build()

    def _build(self) -> None:
        view = self._view
        self._columns = self._columns_for(self.viewport().width())
        page = QWidget()
        page.setObjectName("peopleColumn")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 6, 4, 12)
        layout.setSpacing(8)
        self.cards = {}
        self.tip = None
        self.tip_buttons = {}
        self.make_card_buttons: dict[str, QToolButton] = {}

        if view.location or view.situation:
            heading = QLabel(view.location or "Where the story is")
            heading.setObjectName("peopleHeading")
            heading.setWordWrap(True)
            layout.addWidget(heading)
            if view.situation:
                situation = QLabel(view.situation)
                situation.setObjectName("peopleSituation")
                situation.setWordWrap(True)
                layout.addWidget(situation)
        correct = QToolButton()
        correct.setObjectName("linkButton")
        correct.setText("Correct the scene…")
        correct.setToolTip(
            "Someone here who isn't, or missing who is? The Scene page: who is here, "
            "where and when, as the story has it"
        )
        correct.clicked.connect(self.scene_requested)
        if view.here or view.away or view.others or view.also_here or view.unnamed:
            layout.addWidget(correct, 0, Qt.AlignLeft)

        self._add_tip(layout)

        here_columns, other_columns = self._columns
        if not (view.here or view.away or view.others):
            # A story from scratch before its cast is made.
            nobody = QLabel(
                "Nobody in the story yet. Add the character you play and the people "
                "around them; each gets a card with a portrait."
            )
            nobody.setObjectName("hintLabel")
            nobody.setWordWrap(True)
            layout.addWidget(nobody)
            add = QPushButton("Add a character…")
            add.clicked.connect(self.add_requested)
            self.tip_buttons["add"] = add
            layout.addWidget(add, 0, Qt.AlignLeft)
        elif not view.begun:
            hint = QLabel(NOT_BEGUN)
            hint.setObjectName("hintLabel")
            hint.setWordWrap(True)
            layout.addWidget(hint)
            cast = view.here + view.away
            self._grid(layout, CAST_HEADING, cast, other_columns, OTHERS_MAX_HEIGHT)
            self._grid(layout, SUPPORTING_HEADING, view.others, other_columns, OTHERS_MAX_HEIGHT)
        else:
            self._grid(layout, HERE_HEADING, view.here, here_columns, HERE_MAX_HEIGHT)
            if view.also_here or view.unnamed:
                self._also_here(layout, view.also_here, view.unnamed)
            if not view.here and not view.also_here and not view.unnamed:
                nobody = QLabel("Nobody the story knows is here.")
                nobody.setObjectName("hintLabel")
                layout.addWidget(nobody)
            self._grid(layout, AWAY_HEADING, view.away, other_columns, OTHERS_MAX_HEIGHT)
            self._grid(layout, OTHERS_HEADING, view.others, other_columns, OTHERS_MAX_HEIGHT)
        layout.addStretch(1)
        self.setWidget(page)
        self._show_pictures()

    def _also_here(
        self,
        layout: QVBoxLayout,
        names: Sequence[str],
        unnamed: Sequence[tuple[str, str]] = (),
    ) -> None:
        """Those here with no card, each with Make a card (AfterDark, Oct 18
        2026: Suzuka, named in the opening, was only a name here, with no
        way to give her a card, a portrait or an avatar of her own); on a
        map, those it has here with no name yet, each with Name… (the woman
        at the rail in Doomsville was on no list until she said her name)."""
        heading = QLabel(ALSO_HERE_HEADING)
        heading.setObjectName("peopleSituation")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        self.make_card_buttons = {}
        self.name_buttons = {}
        for label, described in unnamed:
            row = QHBoxLayout()
            row.setSpacing(10)
            face = QLabel()
            face.setPixmap(_placeholder("?", QSize(48, 64)))
            words = QLabel(f"{described[:1].upper()}{described[1:]} · {label}")
            words.setWordWrap(True)
            button = QPushButton(NAME_LABEL)
            button.setToolTip(
                "Not named by the story yet. Say who they are: one of the cards, or a "
                "name (they then get a card of their own with Make a card)"
            )
            button.clicked.connect(lambda _c=False, n=label: self.name_requested.emit(n))
            self.name_buttons[label] = button
            row.addWidget(face, 0)
            row.addWidget(words, 1)
            row.addWidget(button, 0)
            layout.addLayout(row)
        for name in names:
            row = QHBoxLayout()
            row.setSpacing(10)
            face = QLabel()
            face.setPixmap(_placeholder(name, QSize(48, 64)))
            label = QLabel(name)
            label.setWordWrap(True)
            button = QToolButton()
            button.setText(MAKE_CARD_LABEL)
            button.setToolTip(
                f"Give {name} a card of their own, written from what the story says of them: "
                "a profile and a portrait"
            )
            button.setPopupMode(QToolButton.InstantPopup)
            menu = QMenu(button)
            for words, to_cast in card_choices(name):
                menu.addAction(words).triggered.connect(
                    lambda _c=False, n=name, c=to_cast: self.card_requested.emit(n, c)
                )
            button.setMenu(menu)
            self.make_card_buttons[name] = button
            row.addWidget(face, 0)
            row.addWidget(label, 0)
            row.addWidget(button, 0)
            row.addStretch(1)
            layout.addLayout(row)

    def _grid(
        self,
        layout: QVBoxLayout,
        heading: str,
        people: Sequence[Person],
        columns: int,
        max_height: int,
    ) -> None:
        if not people:
            return
        label = QLabel(heading)
        label.setObjectName("peopleSection")
        layout.addWidget(label)
        grid = QGridLayout()
        grid.setSpacing(10)
        for index, person in enumerate(people):
            card = PersonCard(
                person,
                max_height=max_height,
                can_draw=self._view.pictures_ready,
                can_play=self._view.begun,
            )
            card.profile_requested.connect(self.profile_requested)
            card.draw_requested.connect(self.draw_requested)
            card.file_requested.connect(self.file_requested)
            card.play_requested.connect(self.play_requested)
            self.cards[person.card_id] = card
            grid.addWidget(card, index // columns, index % columns, Qt.AlignTop)
        for column in range(columns):
            grid.setColumnStretch(column, 1)
        layout.addLayout(grid)

    def _add_tip(self, layout: QVBoxLayout) -> None:
        """Portraits for everyone with none, and a background: offered until
        the story has them, or until Not now."""
        view = self._view
        if view.story_id in self._tips_put_away:
            return
        wants_faces = bool(view.missing)
        wants_background = not view.background
        if not wants_faces and not wants_background:
            return
        tip = QFrame()
        tip.setObjectName("peopleTip")
        box = QVBoxLayout(tip)
        box.setContentsMargins(12, 10, 12, 10)
        box.setSpacing(6)
        title = QLabel("Make the story yours")
        title.setObjectName("peopleTipTitle")
        box.addWidget(title)
        lines = []
        if wants_faces:
            names = view.missing
            shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
            lines.append(
                f"{shown} {'has' if len(names) == 1 else 'have'} no portrait yet."
                if view.pictures_ready
                else f"{shown} {'has' if len(names) == 1 else 'have'} no portrait yet. "
                "Portraits are drawn by an image model: set one up in Settings, or "
                "choose a picture of your own on any card."
            )
        if wants_background:
            lines.append("A picture behind the story sets the scene.")
        text = QLabel(" ".join(lines))
        text.setObjectName("peopleTipText")
        text.setWordWrap(True)
        box.addWidget(text)
        row = QHBoxLayout()
        row.setSpacing(6)

        def button(key: str, label: str, signal) -> None:
            widget = QPushButton(label)
            widget.clicked.connect(signal)
            self.tip_buttons[key] = widget
            row.addWidget(widget)

        if wants_faces and view.pictures_ready:
            button("portraits", "Draw portraits…", self.draw_all_requested)
        if not view.pictures_ready:
            button("settings", "Set up pictures…", self.settings_requested)
        if wants_background:
            if view.pictures_ready:
                button("background", "Draw a background…", self.background_requested)
            button("background_file", "Choose a background…", self.background_file_requested)
        row.addStretch(1)
        later = QToolButton()
        later.setObjectName("linkButton")
        later.setText("Not now")
        later.clicked.connect(lambda: self._put_tip_away(view.story_id))
        row.addWidget(later)
        box.addLayout(row)
        layout.addWidget(tip)
        self.tip = tip

    def _put_tip_away(self, story_id: str) -> None:
        self._tips_put_away.add(story_id)
        self._build()

    # --- pictures -----------------------------------------------------------

    def _show_pictures(self) -> None:
        store = self._pictures()
        key = (
            (getattr(store, "story_id", None), getattr(store, "root", None))
            if store is not None
            else None
        )
        if key != self._read_for:
            self._read.clear()
            self._read_for = key
        for card in self.cards.values():
            file = card.person.picture
            if not file or store is None:
                continue
            pixmap = self._read.get(file)
            if pixmap is None:
                from sealedlore.gui.ref_images import pixmap_within

                try:
                    data = store.read(file)
                except (OSError, ValueError):
                    data = None
                pixmap = pixmap_within(data, READ_AT) if data else QPixmap()
                self._read[file] = pixmap
            card.set_pixmap(pixmap)

    def forget_pictures(self) -> None:
        """A card's picture changed: read them afresh."""
        self._read.clear()
        self._show_pictures()
