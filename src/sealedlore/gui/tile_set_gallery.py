"""Look ▾ → Tile sets… (map-first play; engine/session_area.py): every tile
set the map can be drawn in, each as a card with its pictures, and drawing a
new one. The author's ruling (Oct 16 2026): show the sets there are, ask
whether they want to generate new ones, have them describe what they want,
and use Opus 5.5 to manage the calls required to create them.

`TileSetGallery`: the shipped sets and this story's own, the one in use
marked; Use this set, Remove (an own set, its pictures deleted from the
story), Draw a new set… (the designer) and Edit the tile set… (the editor,
gui/tile_set_editor.py).

`TileSetDesigner`: the author names the set and describes the look they want
(prefilled with the world and the style now in use); the designer model
(`Config.tileset_model`, Opus 5.5 by default) writes the set's words and a
prompt tailored to every picture; the author sees and may change each, with
the pictures' price at the tile model's listed prices, and only then is
anything drawn, each picture exactly as approved.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.session_area import ASSET_LABELS, CARD_ROLES, TileSetCard
from sealedlore.engine.tile_draw import DESIGNER_MODELS, PROMPT_KEY, tile_jobs, world_words
from sealedlore.engine.tile_sets import SPRITES, TEXTURES
from sealedlore.gui.image_codec import image_from_data
from sealedlore.gui.model_picker import ModelField
from sealedlore.gui.sizing import GrowingText, size_to_screen

THUMB = 56
# Thumbnails read a few at a time, so the gallery opens at once.
THUMBS_A_TICK = 8
# How often the designer looks whether the design has come in.
POLL_MS = 500


class TileSetGallery(QDialog):
    """Every set as a card; `exec()` returns DESIGN or EDIT when the author
    went on to the designer or the editor, else Accepted/Rejected."""

    DESIGN = 2
    EDIT = 3

    def __init__(self, session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.setWindowTitle("Tile sets")
        size_to_screen(self, 0.5, 0.8, at_least=QSize(760, 640))
        self.heading = QLabel()
        self.heading.setWordWrap(True)
        self.column = QVBoxLayout()
        self.column.setSpacing(10)
        host = QWidget()
        host.setLayout(self.column)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(host)
        self.cards: list[_Card] = []
        self.design_button = QPushButton("Draw a new set…")
        self.design_button.setToolTip(
            "Describe the look you want; a designer model writes the set and you see every "
            "prompt and the price before anything is drawn"
        )
        self.design_button.clicked.connect(lambda: self.done(self.DESIGN))
        self.edit_button = QPushButton("Edit the tile set…")
        self.edit_button.setToolTip("Every picture of the set in use, one at a time")
        self.edit_button.clicked.connect(lambda: self.done(self.EDIT))
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.addButton(self.design_button, QDialogButtonBox.ActionRole)
        buttons.addButton(self.edit_button, QDialogButtonBox.ActionRole)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.heading)
        layout.addWidget(scroll, 1)
        layout.addWidget(buttons)
        self._pending: list[tuple[QLabel, Callable[[], bytes | None]]] = []
        self._thumbs = QTimer(self)
        self._thumbs.setInterval(0)
        self._thumbs.timeout.connect(self._read_thumbs)
        self._fill()

    def _fill(self) -> None:
        while self.column.count():
            item = self.column.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.cards = []
        self._pending = []
        cards = self.session.tile_set_cards()
        in_use = next((c.title for c in cards if c.in_use), "no set")
        self.heading.setText(
            f"The map is drawn in <b>{in_use}</b>. Each set is pixel art in a style of its own; "
            "one drawn for this story is kept with it, and a change under Edit the tile set… is "
            "for this story only."
        )
        for card in cards:
            widget = _Card(card, self._use, self._remove)
            self.cards.append(widget)
            self.column.addWidget(widget)
            self._pending += [
                (label, lambda role=role, read=card.read: read(role))
                for role, label in widget.thumbs
            ]
        self.column.addStretch(1)
        self._thumbs.start()

    def _read_thumbs(self) -> None:
        for _ in range(THUMBS_A_TICK):
            if not self._pending:
                self._thumbs.stop()
                return
            label, read = self._pending.pop(0)
            data = read()
            image = image_from_data(data)
            if image.isNull():
                label.setText("·")
                continue
            label.setPixmap(
                QPixmap.fromImage(
                    image.scaled(THUMB, THUMB, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
            )

    def _use(self, name: str) -> None:
        self.session.set_tile_set(name)
        self._fill()

    def _remove(self, name: str, title: str) -> None:
        answer = QMessageBox.question(
            self,
            "Remove this set?",
            f"{title} is removed from this story and its pictures deleted. The map is drawn "
            "in the set chosen for the setting if it was drawn in this one.",
        )
        if answer != QMessageBox.Yes:
            return
        self.session.remove_own_tile_set(name)
        self._fill()


class _Card(QFrame):
    def __init__(
        self,
        card: TileSetCard,
        use: Callable[[str], None],
        remove: Callable[[str, str], None],
    ) -> None:
        super().__init__()
        self.setObjectName("tileSetCard")
        self.setFrameShape(QFrame.StyledPanel)
        self.card = card
        title = QLabel(f"<b>{card.title}</b>" + ("  · in use" if card.in_use else ""))
        title.setObjectName("emptyTitle")
        strip = QHBoxLayout()
        strip.setSpacing(4)
        self.thumbs: list[tuple[str, QLabel]] = []
        for role in CARD_ROLES:
            label = QLabel()
            label.setFixedSize(QSize(THUMB, THUMB))
            label.setAlignment(Qt.AlignCenter)
            label.setObjectName("tilePreview")
            label.setToolTip(ASSET_LABELS.get(role, role))
            strip.addWidget(label)
            self.thumbs.append((role, label))
        strip.addStretch(1)
        self.style = QLabel(card.style)
        self.style.setWordWrap(True)
        self.keywords = QLabel(
            ", ".join(card.keywords) if card.keywords else "Drawn for this story"
        )
        self.keywords.setObjectName("hintLabel")
        self.keywords.setWordWrap(True)
        self.use_button = QPushButton("Use this set")
        self.use_button.setEnabled(not card.in_use)
        self.use_button.clicked.connect(lambda: use(card.name))
        buttons = QHBoxLayout()
        buttons.addWidget(self.use_button)
        self.remove_button: QPushButton | None = None
        if card.own:
            self.remove_button = QPushButton("Remove…")
            self.remove_button.setToolTip("Take this set out of the story and delete its pictures")
            self.remove_button.clicked.connect(lambda: remove(card.name, card.title))
            buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addLayout(strip)
        layout.addWidget(self.style)
        layout.addWidget(self.keywords)
        layout.addLayout(buttons)


class TileSetDesigner(QDialog):
    """`exec()` is Accepted once the set is being drawn."""

    def __init__(
        self,
        session,
        parent: QWidget | None = None,
        *,
        browse: Callable[[str], str | None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.setWindowTitle("Draw a new tile set")
        size_to_screen(self, 0.55, 0.88, at_least=QSize(900, 760))
        intro = QLabel(
            "Describe the look you want for the map. The designer writes the set's words and "
            "a prompt for every picture; you see and may change each, with the price, before "
            "anything is drawn. What you approve is what is sent."
        )
        intro.setWordWrap(True)
        self.name = QLineEdit()
        self.name.setPlaceholderText("A name for the set (Neon alleys)")
        self.description = GrowingText(lines=3, most=10)
        self.description.setPlaceholderText(
            "gritty rain-slick cyberpunk alleys, neon and wet concrete; bright and cheerful "
            "storybook meadow"
        )
        # The story's own world in a sentence, and the style the map is drawn
        # in now: a start the author rewrites to the look they want.
        world = world_words({}, session.map_setting())
        self.description.setPlainText(f"{world}. {session.tile_style()}")
        # Who designs: blank is Opus 5.5 on NanoGPT, else the map art model
        # (`tileset_designer`); a choice here is kept as that same setting.
        self.designer = ModelField(
            session.config.tileset_model or "", browse, recommended=DESIGNER_MODELS
        )
        self.designer.setPlaceholderText(session.tileset_designer)
        self.drawn_by = QLabel(f"Drawn by {session.config.map_tile_model}, the map tile model")
        self.drawn_by.setObjectName("hintLabel")
        self.write_button = QPushButton("Write the design")
        self.write_button.clicked.connect(self._write)
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setMaximumHeight(6)
        self.busy.setTextVisible(False)
        self.busy.hide()
        self.status = QLabel("")
        self.status.setObjectName("hintLabel")
        self.status.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Name", self.name)
        form.addRow("The look", self.description)
        form.addRow("Designed by", self.designer)
        form.addRow("", self.drawn_by)
        form.addRow("", self.write_button)
        form.addRow("", self.busy)
        form.addRow("", self.status)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(8)
        host = QWidget()
        host.setLayout(self.rows)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(host)
        self.scroll.hide()
        self.price = QLabel("")
        self.price.setObjectName("hintLabel")
        self.price.setWordWrap(True)
        self.draw_button = QPushButton("Draw the set…")
        self.draw_button.setObjectName("sendButton")
        self.draw_button.setEnabled(False)
        self.draw_button.setDefault(False)
        self.draw_button.setAutoDefault(False)
        self.draw_button.clicked.connect(self._draw)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        buttons.addButton(self.draw_button, QDialogButtonBox.ActionRole)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(self.scroll, 1)
        layout.addWidget(self.price)
        layout.addWidget(buttons)
        self.world_edit = QLineEdit()
        self.style_edit = QLineEdit()
        self._rows: dict[str, _Row] = {}
        self._fixed: dict[str, str] = {}
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._check)

    def designer_model(self) -> str:
        return self.designer.text().strip()

    def reject(self) -> None:
        """Cancel while the designer writes stops its call."""
        if self._poll.isActive():
            self._poll.stop()
            self.session.cancel_tile_set_design()
        super().reject()

    # --- the design --------------------------------------------------------------

    def _write(self) -> None:
        problem = self.session.own_tile_set_offer()[2]
        if problem is not None:
            self.status.setText(problem)
            return
        if not self.session.start_tile_set_design(
            self.description.toPlainText(), self.designer_model()
        ):
            self.status.setText("The design couldn't be started.")
            return
        self.write_button.setEnabled(False)
        self.busy.show()
        self.status.setText(
            f"{self.designer_model() or self.session.tileset_designer} is designing the set…"
        )
        self._poll.start()

    def _check(self) -> None:
        state, words, error = self.session.tile_set_design
        if state == "writing":
            return
        self._poll.stop()
        self.busy.hide()
        self.write_button.setEnabled(True)
        if state == "done" and words is not None:
            self.status.setText("")
            self.show_design(words)
        else:
            self.status.setText(f"The design failed: {error or 'no answer'}")

    def show_design(self, words: dict[str, str]) -> None:
        """The design as the author will approve it: the world and style,
        and for every picture its words and the prompt that will be sent
        (the designer's, or the template filled in), each editable."""
        while self.rows.count():
            item = self.rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._rows = {}
        self._fixed = {k: v for k, v in words.items() if k in ("light", "lava")}
        head = QFormLayout()
        self.world_edit = QLineEdit(words.get("world", ""))
        self.style_edit = QLineEdit(words.get("style", ""))
        self.style_edit.setToolTip("The pixel art's style, named in every prompt")
        head.addRow("World", self.world_edit)
        head.addRow("Style", self.style_edit)
        head_host = QWidget()
        head_host.setLayout(head)
        self.rows.addWidget(head_host)
        sent = {role: prompt for role, prompt, _size in tile_jobs(words, self.session.texts)}
        left_out = []
        for role in (*TEXTURES, *SPRITES):
            if not words.get(role):
                left_out.append(ASSET_LABELS.get(role, role))
                continue
            designed = words.get(f"{PROMPT_KEY}{role}")
            row = _Row(ASSET_LABELS.get(role, role), words[role], sent.get(role, ""), designed)
            row.words.textChanged.connect(lambda _t, r=role: self._words_changed(r))
            row.prompt.textChanged.connect(self._sync_price)
            self._rows[role] = row
            self.rows.addWidget(row)
        if left_out:
            note = QLabel(
                "Not in this set (the map's plain stand-ins draw them): " + ", ".join(left_out)
            )
            note.setObjectName("hintLabel")
            note.setWordWrap(True)
            self.rows.addWidget(note)
        self.rows.addStretch(1)
        self.scroll.show()
        self.draw_button.setEnabled(True)
        self._sync_price()

    def _words_changed(self, role: str) -> None:
        """A row's words changed: a template prompt follows them (a prompt
        the designer wrote, or the author changed, is left as it is)."""
        row = self._rows[role]
        if row.auto is None or row.prompt.toPlainText() != row.auto:
            self._sync_price()
            return
        design = self.design()
        design.pop(f"{PROMPT_KEY}{role}", None)
        prompt = next((p for r, p, _s in tile_jobs(design, self.session.texts) if r == role), "")
        row.auto = prompt
        row.prompt.setPlainText(prompt)
        self._sync_price()

    def design(self) -> dict[str, str]:
        """The set as it stands in the dialog: a role with no words is not
        drawn; every drawn picture's prompt is its box, word for word."""
        out = dict(self._fixed)
        out["world"] = " ".join(self.world_edit.text().split())
        out["style"] = " ".join(self.style_edit.text().split())
        for role, row in self._rows.items():
            words = " ".join(row.words.text().split())
            if not words:
                continue
            out[role] = words
            prompt = row.prompt.toPlainText().strip()
            if prompt:
                out[f"{PROMPT_KEY}{role}"] = prompt
        return out

    def _sync_price(self) -> None:
        count, price = self.session.tile_set_price(self.design())
        model = self.session.config.map_tile_model
        if count == 0:
            self.price.setText("Nothing to draw: give at least the floor, walls and ground words.")
            self.draw_button.setEnabled(False)
            return
        cost = f"about ${price:.2f}" if price is not None else "at a price the listing doesn't give"
        self.price.setText(f"{count} pictures by {model}, {cost}.")
        self.draw_button.setEnabled(True)

    def _draw(self) -> None:
        design = self.design()
        count, price = self.session.tile_set_price(design)
        if count == 0:
            return
        title = " ".join(self.name.text().split()) or design.get("world", "")
        cost = f"about ${price:.2f}" if price is not None else "at the model's listed price"
        answer = QMessageBox.question(
            self,
            "Draw the set?",
            f"{count} pictures are drawn by {self.session.config.map_tile_model}, {cost}, each "
            f"from its prompt exactly as shown. The map is drawn in {title or 'the new set'} "
            "once all of it is in; the set it has now stays until then.",
        )
        if answer != QMessageBox.Yes:
            return
        if self.session.start_own_tile_set(design, title):
            self.accept()
        else:
            self.status.setText(
                self.session.own_tile_set_offer()[2] or "The set couldn't be started."
            )


class _Row(QWidget):
    """One picture of the design: its words and the prompt to be sent."""

    def __init__(self, label: str, words: str, prompt: str, designed: str | None) -> None:
        super().__init__()
        self.title = QLabel(f"<b>{label}</b>")
        self.words = QLineEdit(words)
        self.words.setToolTip("What it shows, in a few words")
        self.prompt = GrowingText(prompt, lines=3, most=10)
        self.prompt.setToolTip("Sent to the image model exactly as it is here")
        # The template's prompt follows the words until the author changes
        # it; the designer's own never does.
        self.auto: str | None = None if designed else prompt
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.title)
        layout.addWidget(self.words)
        layout.addWidget(self.prompt)
