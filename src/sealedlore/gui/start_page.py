"""The start page, and File → New…: every way to begin, each said in a
sentence.

The author (Oct 18 2026): the File menu had five ways to begin (New story,
New story from a premise, New simple chat, New story from a sample, Import)
and the start page five bare buttons, "with no real indicator to the user
what is what". `StartChooser` is both: the stories played last, to pick up
where they left off; the ways to start one's own (drafted from an idea,
from scratch, a simple chat, a file); and the sample stories, each with its
picture and its line, first for someone with no stories yet. File → New…
shows it in `NewDialog`, without the stories played last.

A choice is `chosen(kind, value)`; the window does the rest, with the same
methods the menus called before.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent, QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.plot_md import front_matter_of
from sealedlore.storage.paths import sample_stories

# What a choice is, as `chosen` carries it: the value is the story's id for
# "open", the plot file for "sample", else None.
OPEN, PREMISE, BLANK, CHAT, IMPORT, SAMPLE = "open", "premise", "blank", "chat", "import", "sample"
# The ways to start one's own, in the order shown, with what each says.
OWN: tuple[tuple[str, str, str], ...] = (
    (
        PREMISE,
        "Draft a story from an idea",
        "Describe the story you want in a few sentences. The model drafts its world, its "
        "characters and its lore, and you look them over before you begin.",
    ),
    (
        BLANK,
        "Write a story from scratch",
        "Start with a blank story and set up its world, its characters and its opening yourself.",
    ),
    (
        CHAT,
        "Have a simple chat",
        "A plain conversation with the model: your own instructions and your messages, "
        "with none of the story features.",
    ),
    (
        IMPORT,
        "Import a file",
        "A story backup, a scenario someone shared with you, or a plot file (.md).",
    ),
)
SAMPLES_HEADING = "Play a sample story"
SAMPLES_TEXT = (
    "Ready-made stories with their own world, characters, plot and pictures: the "
    "quickest way to see what SealedLore does. Setup opens first, so you can look "
    "everything over and choose who you play."
)
RECENT_HEADING = "Pick up where you left off"
OWN_HEADING = "Start your own"
# How many stories played last are offered; the Stories list has them all.
RECENT_COUNT = 3
# A sample's picture on its card: the samples' backgrounds are wide.
PICTURE = QSize(320, 150)
# Sample cards a row, by the width there is.
WIDE_FROM = 900


@dataclass(frozen=True)
class Recent:
    """A story played lately: its id, title and a line under it."""

    story_id: str
    title: str
    detail: str


@dataclass(frozen=True)
class Sample:
    """A sample story as its card shows it."""

    path: Path
    title: str
    description: str
    picture: Path | None


def samples(folder: Path | None = None) -> list[Sample]:
    """The sample stories with their front matter's title, description and
    background picture (a path inside the samples' folder, or None)."""
    found = []
    for name, path in sample_stories(folder):
        try:
            front = front_matter_of(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            front = {}
        picture = None
        if front.get("background"):
            from sealedlore.gui.ref_images import inside

            target = inside(path.parent, front["background"])
            picture = target if target is not None and target.is_file() else None
        found.append(
            Sample(
                path=path,
                title=front.get("title") or name,
                description=front.get("description", ""),
                picture=picture,
            )
        )
    return found


def when_played(stamp: str) -> str:
    """'17 Oct 2026' from a story's ISO `updated_at`, or ''."""
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return ""
    return f"{moment.day} {moment:%b %Y}"


# Read once a process: the start page and File → New… both show them.
_pictures: dict[tuple[str, float], QPixmap] = {}


def _picture(path: Path) -> QPixmap:
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        return QPixmap()
    if key not in _pictures:
        from sealedlore.gui.ref_images import pixmap_within

        try:
            data = path.read_bytes()
        except OSError:
            return QPixmap()
        whole = pixmap_within(data, QSize(PICTURE.width() * 2, PICTURE.height() * 2))
        _pictures[key] = whole
    return _pictures[key]


class Card(QFrame):
    """One choice: a title and a sentence (and a sample's picture), the whole
    card clickable, and Enter or Space with the focus on it."""

    activated = Signal()

    def __init__(self, title: str, text: str, *, picture: bool = False) -> None:
        super().__init__()
        self.setObjectName("startCard")
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName(title)
        self.setAccessibleDescription(text)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.picture: QLabel | None = None
        if picture:
            self.picture = QLabel()
            self.picture.setObjectName("startPicture")
            self.picture.setFixedHeight(PICTURE.height())
            self.picture.setAlignment(Qt.AlignCenter)
            # Cut to the card, never setting its width: the pixmap's width
            # held the cards wider than the page once its scrollbar came.
            self.picture.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            self.picture.setMinimumWidth(1)
            layout.addWidget(self.picture)
        words = QVBoxLayout()
        words.setContentsMargins(16, 12, 16, 14)
        words.setSpacing(4)
        self.title = QLabel(title)
        self.title.setObjectName("startCardTitle")
        self.title.setWordWrap(True)
        words.addWidget(self.title)
        self.text = QLabel(text)
        self.text.setObjectName("startCardText")
        self.text.setWordWrap(True)
        self.text.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        words.addWidget(self.text, 1)
        layout.addLayout(words, 1)

    def set_picture(self, pixmap: QPixmap) -> None:
        """The picture cut to the card's width and the picture's height,
        from its middle."""
        if self.picture is None or pixmap.isNull():
            return
        width = max(1, self.picture.width())
        height = PICTURE.height()
        scaled = pixmap.scaled(
            QSize(width, height), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
        )
        x = max(0, (scaled.width() - width) // 2)
        y = max(0, (scaled.height() - height) // 2)
        self.picture.setPixmap(scaled.copy(x, y, width, height))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.activated.emit()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.activated.emit()
            return
        super().keyPressEvent(event)


class StartChooser(QScrollArea):
    """Every way to begin, as cards in sections (see the module)."""

    chosen = Signal(str, object)

    def __init__(
        self,
        *,
        heading: str = "",
        recent: Sequence[Recent] = (),
        samples_folder: Path | None = None,
        own_first: bool = False,
    ) -> None:
        super().__init__()
        self.setObjectName("startPage")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._samples_folder = samples_folder
        self._sample_cards: list[tuple[Card, Sample]] = []
        self._columns = 0
        self._pictures_shown = False
        self.cards: dict[str, Card] = {}
        self._heading = heading
        self._own_first = own_first
        self.rebuild(recent)

    # --- building ---------------------------------------------------------

    def rebuild(self, recent: Sequence[Recent] = ()) -> None:
        """Every section afresh (the stories played last change; a sample
        dropped into the folder appears). A new page: the scroll area
        deletes the old one, cards in their grids and all."""
        page = QWidget()
        page.setObjectName("startColumn")
        self._layout = QVBoxLayout(page)
        self._layout.setContentsMargins(32, 28, 32, 32)
        self._layout.setSpacing(10)
        self.cards = {}
        self._sample_cards = []
        self._columns = 0
        self._pictures_shown = False
        if self._heading:
            heading = QLabel(self._heading)
            heading.setObjectName("startHeading")
            self._layout.addWidget(heading)
        recent = list(recent)[:RECENT_COUNT]
        if recent:
            self._section(RECENT_HEADING, "")
            row = QGridLayout()
            row.setSpacing(12)
            for column, story in enumerate(recent):
                card = Card(story.title, story.detail)
                card.activated.connect(lambda s=story.story_id: self.chosen.emit(OPEN, s))
                self.cards[f"{OPEN}:{story.story_id}"] = card
                row.addWidget(card, 0, column)
            for column in range(RECENT_COUNT):
                row.setColumnStretch(column, 1)
            self._layout.addLayout(row)
        # Someone with no stories yet is best shown the samples first; File →
        # New… is someone who has been here before.
        if recent or self._own_first:
            self._own()
            self._samples()
        else:
            self._samples()
            self._own()
        self._layout.addStretch(1)
        self.setWidget(page)
        if self.isVisible():
            self._lay_samples()
            QTimer.singleShot(0, self._show_pictures)

    def _section(self, title: str, text: str) -> None:
        label = QLabel(title)
        label.setObjectName("startSection")
        self._layout.addSpacing(8)
        self._layout.addWidget(label)
        if text:
            words = QLabel(text)
            words.setObjectName("startSectionText")
            words.setWordWrap(True)
            self._layout.addWidget(words)

    def _own(self) -> None:
        self._section(OWN_HEADING, "")
        grid = QGridLayout()
        grid.setSpacing(12)
        for index, (kind, title, text) in enumerate(OWN):
            card = Card(title, text)
            card.activated.connect(lambda k=kind: self.chosen.emit(k, None))
            self.cards[kind] = card
            grid.addWidget(card, index // 2, index % 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self._layout.addLayout(grid)

    def _samples(self) -> None:
        found = samples(self._samples_folder)
        if not found:
            return
        self._section(SAMPLES_HEADING, SAMPLES_TEXT)
        self._sample_grid = QGridLayout()
        self._sample_grid.setSpacing(12)
        for sample in found:
            card = Card(sample.title, sample.description, picture=sample.picture is not None)
            card.activated.connect(lambda p=sample.path: self.chosen.emit(SAMPLE, p))
            self.cards[f"{SAMPLE}:{sample.path.name}"] = card
            self._sample_cards.append((card, sample))
        self._layout.addLayout(self._sample_grid)
        self._lay_samples()

    def _lay_samples(self) -> None:
        """Three sample cards a row where there is room, else two."""
        if not self._sample_cards:
            return
        columns = 3 if self.viewport().width() >= WIDE_FROM else 2
        if columns == self._columns:
            return
        self._columns = columns
        for card, _sample in self._sample_cards:
            self._sample_grid.removeWidget(card)
        for index, (card, _sample) in enumerate(self._sample_cards):
            self._sample_grid.addWidget(card, index // columns, index % columns)
        for column in range(3):
            self._sample_grid.setColumnStretch(column, 1 if column < columns else 0)

    def _show_pictures(self) -> None:
        """The samples' pictures, read after the page is first drawn: six
        JPEGs decoded before it would hold up the first paint at launch."""
        self._pictures_shown = True
        for card, sample in self._sample_cards:
            if sample.picture is not None:
                card.set_picture(_picture(sample.picture))

    # --- Qt ---------------------------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self._lay_samples()
        if not self._pictures_shown:
            QTimer.singleShot(0, self._show_pictures)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._lay_samples()
        if self._pictures_shown:
            # Cut again to the cards' new width.
            QTimer.singleShot(0, self._show_pictures)


class NewDialog(QDialog):
    """File → New…: the start page's choices but the stories played last.
    `choice` is (kind, value) once one is made."""

    def __init__(self, parent: QWidget | None = None, *, samples_folder: Path | None = None):
        super().__init__(parent)
        self.setWindowTitle("New")
        self.resize(1040, 760)
        self.choice: tuple[str, object] | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.chooser = StartChooser(
            heading="Start something new", samples_folder=samples_folder, own_first=True
        )
        self.chooser.chosen.connect(self._chosen)
        layout.addWidget(self.chooser)

    def _chosen(self, kind: str, value: object) -> None:
        self.choice = (kind, value)
        self.accept()
