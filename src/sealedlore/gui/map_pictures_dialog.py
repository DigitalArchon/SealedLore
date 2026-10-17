"""The map pictures that wait for the author (Settings → Images → Draw map
pictures: Ask before drawing; engine/session_map_approval.py).

Every picture the map wants is listed with what it is, the model, its size
and its listed price, and its prompt, which the author may change; a picture
drawn from a portrait shows the portrait. Draw sends the ticked ones exactly
as they stand and leaves out the rest for as long as the story is open; Not
now leaves them all out; Cancel leaves them waiting.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.session_map_approval import WaitingPicture
from sealedlore.engine.world_pictures import PictureJob
from sealedlore.gui.ref_images import thumbnail_of

THUMB_SIDE = 64
# What each kind of map picture is, as the dialog names it.
KINDS = {
    "walker": "Avatar",
    "person": "Figure",
    "item": "Thing",
    "texture": "Texture",
    "place": "Place",
    "thing": "Thing on the map",
    "postcard": "Postcard",
}


def describe(waiting: WaitingPicture) -> str:
    """The line under a picture's name: the model, the size, the price."""
    price = f"about ${waiting.price:.3f}" if waiting.price is not None else "price not listed"
    portrait = waiting.job.subject == "walker" and waiting.job.reference
    source = ", from their portrait" if portrait else ""
    return f"{waiting.model}, {waiting.size}, {price}{source}"


class _Row(QFrame):
    def __init__(self, waiting: WaitingPicture, thumbnail) -> None:
        super().__init__()
        self.setObjectName("portraitRow")
        self.waiting = waiting
        job = waiting.job
        self.tick = QCheckBox(f"{KINDS.get(job.subject, 'Picture')}: {job.name}")
        self.tick.setChecked(True)
        note = QLabel(describe(waiting))
        note.setObjectName("hintLabel")
        note.setWordWrap(True)
        self.prompt = QPlainTextEdit(job.prompt)
        self.prompt.setFixedHeight(84)
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(4)
        right.addWidget(self.tick)
        right.addWidget(note)
        right.addWidget(self.prompt)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        if thumbnail is not None and not thumbnail.isNull():
            picture = QLabel()
            picture.setFixedSize(THUMB_SIDE, THUMB_SIDE)
            picture.setAlignment(Qt.AlignCenter)
            picture.setPixmap(thumbnail)
            picture.setToolTip("The picture it is drawn from")
            layout.addWidget(picture, 0, Qt.AlignTop)
        layout.addLayout(right, 1)
        self.tick.toggled.connect(self.prompt.setEnabled)

    def wanted(self) -> bool:
        return self.tick.isChecked() and bool(self.prompt.toPlainText().strip())

    def job(self) -> PictureJob:
        """The job as the author left it: its prompt exactly as it stands."""
        return replace(self.waiting.job, prompt=self.prompt.toPlainText())


class MapPicturesDialog(QDialog):
    """Map pictures waiting: draw some or all, or none for now."""

    DECLINED = 2

    def __init__(
        self,
        waiting: Sequence[WaitingPicture],
        read: Callable[[str], bytes | None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Map pictures")
        self.setMinimumWidth(620)
        intro = QLabel(
            "The map wants these drawn as the story reaches them. Read the prompts, change "
            "any you like and untick what you'd rather not have; nothing is sent until you "
            "press Draw. Each counts against today's map pictures. To have them drawn "
            "without asking, choose Automatically under Settings → Images → Draw map "
            "pictures."
        )
        intro.setWordWrap(True)
        self.rows: list[_Row] = []
        holder = QWidget()
        rows = QVBoxLayout(holder)
        rows.setContentsMargins(0, 0, 6, 0)
        for item in waiting:
            reference = item.job.reference
            thumb = thumbnail_of(read(reference), THUMB_SIDE) if reference else None
            row = _Row(item, thumb)
            row.tick.toggled.connect(self._sync)
            row.prompt.textChanged.connect(self._sync)
            self.rows.append(row)
            rows.addWidget(row)
        rows.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(holder)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setMinimumHeight(min(560, 130 * max(1, len(self.rows))))
        self.status = QLabel()
        self.status.setObjectName("hintLabel")
        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.draw_button = self.buttons.addButton("Draw", QDialogButtonBox.AcceptRole)
        self.draw_button.setDefault(True)
        self.not_now = self.buttons.addButton("Not now", QDialogButtonBox.DestructiveRole)
        self.not_now.setToolTip("None of them: you aren't asked again while the story is open")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.not_now.clicked.connect(lambda: self.done(self.DECLINED))
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)
        self._sync()
        self.resize(720, 680)

    def _sync(self, *_args) -> None:
        wanted = [row for row in self.rows if row.wanted()]
        total = sum(row.waiting.price or 0.0 for row in wanted)
        unpriced = any(row.waiting.price is None for row in wanted)
        cost = f", about ${total:.2f}" if total else ""
        if unpriced:
            cost += " and some the listing doesn't price"
        count = len(wanted)
        self.status.setText(f"{count} picture{'' if count == 1 else 's'} to draw{cost}.")
        self.draw_button.setText(f"Draw {count}" if count else "Draw")
        self.draw_button.setEnabled(count > 0)

    def approved(self) -> list[PictureJob]:
        """The ticked pictures, each with its prompt as it stands."""
        return [row.job() for row in self.rows if row.wanted()]

    def left_out(self) -> list[str]:
        """The keys of the pictures not ticked (or with no prompt left)."""
        return [row.waiting.job.key for row in self.rows if not row.wanted()]
