"""The window a visual story is played in (the author, Oct 24 2026: a
separate game mode, the visuals centre stage, chat part of the game). The
play view (gui/area_view.py) fills most of it; a column at the right holds
the log (what happens, what people say) and the line you type; the player's
portrait, the place and the clock sit under the view.

Step 1 of the design (SealedLoreDocs/design/visual-stories.md): walking,
doors (with a fade through), journeys, the clock, saving as you go. No
model is called; the line says so. The window owns its session, and the
main window (hidden meanwhile) comes back when it closes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QSize, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QAction, QColor, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.area_view import AreaView
from sealedlore.gui.ref_images import pixmap_within
from sealedlore.gui.visual_presenter import VisualPresenter
from sealedlore.models.character import Character
from sealedlore.models.config import Config

# The side column's share of the window, and its narrowest.
SIDE_SHARE = 0.26
SIDE_MIN = 320
# The fade through a door: to black, and back.
FADE_MS = 220
# The portrait under the view.
PORTRAIT = QSize(72, 96)
# Verbs done beside whoever or whatever they are for.
WALK_UP = ("talk", "attack", "use", "take", "eat", "destroy")
# What the line does for now (step 1: nothing is asked of a model).
LINE_HINT = "Say or do something (talking to people and acting come in the next steps)"


def _duration(seconds: int) -> str:
    minutes = seconds / 60
    if minutes < 1:
        return "under a minute on foot"
    if minutes < 90:
        return f"{round(minutes)} min on foot"
    hours = minutes / 60
    return f"{hours:.1f} h on foot" if hours < 10 else f"{hours:.0f} h on foot"


class VisualAreaView(AreaView):
    """The play view with the game's own words, and none of the story
    window's tools (tile sets, paintings, objects, Edit Mode)."""

    def __init__(self) -> None:
        super().__init__()
        for button in (
            self.look_button,
            self.paintings_button,
            self.objects_button,
            self.edit_button,
            self.carry_button,
        ):
            button.hide()

    def _hint(self) -> str:
        if self.locked:
            return "One moment."
        return (
            "Click where to go, a building to go inside, or a door to go through it. "
            "Travel… sets off for another place; Leave walks on past an edge. Drag the "
            "ground to look around, wheel to zoom, ◎ Me (Home) finds you."
        )


class _Fade(QWidget):
    """Black over the view, at an opacity the animation sets."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.opacity = 0.0
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.hide()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt's name
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, int(255 * self.opacity)))


class VisualWindow(QMainWindow):
    # The window closed: the main window shows again.
    closed = Signal()

    def __init__(
        self,
        session,
        config: Config,
        root: Path | None,
        *,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.session = session
        self.config = config
        self.root = root
        self._on_close = on_close
        self.setWindowTitle(f"SealedLore — {session.story.title}")
        self.setObjectName("visualWindow")

        self.area_view = VisualAreaView()
        self.presenter = VisualPresenter(self.area_view, lambda: self.session, root)
        self.presenter.through_door.connect(self._through_door)
        self.area_view.go_requested.connect(self._go)
        self.area_view.action_chosen.connect(self._action)
        self.area_view.travel_wanted.connect(self._travel_menu)
        self.area_view.travel_chosen.connect(self._travel)
        self.area_view.edge_wanted.connect(self._edge_menu)
        self.area_view.edge_chosen.connect(self._edge_go)
        self._fade = _Fade(self.area_view.view.viewport())
        self._fading: QVariantAnimation | None = None

        # The column: who is talking (later), the log, the line.
        self.log = QTextEdit()
        self.log.setObjectName("visualLog")
        self.log.setReadOnly(True)
        self.line = QLineEdit()
        self.line.setPlaceholderText(LINE_HINT)
        self.line.returnPressed.connect(self._line_sent)
        side = QWidget()
        side.setObjectName("visualSide")
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(8, 8, 8, 8)
        side_layout.setSpacing(6)
        side_layout.addWidget(self.log, 1)
        side_layout.addWidget(self.line)
        side.setMinimumWidth(SIDE_MIN)

        # Under the view: the player, where they are, the clock.
        self.portrait = QLabel()
        self.portrait.setFixedSize(PORTRAIT)
        self.portrait.setAlignment(Qt.AlignCenter)
        self.name = QLabel()
        self.name.setObjectName("emptyTitle")
        self.where = QLabel()
        self.where.setObjectName("hintLabel")
        bar = QWidget()
        bar.setObjectName("visualBar")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(8, 4, 8, 4)
        words = QVBoxLayout()
        words.addWidget(self.name)
        words.addWidget(self.where)
        bar_layout.addWidget(self.portrait)
        bar_layout.addLayout(words, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        left_layout.addWidget(self.area_view, 1)
        left_layout.addWidget(bar)
        self.split = QSplitter(Qt.Horizontal)
        self.split.addWidget(left)
        self.split.addWidget(side)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 1)
        self.setCentralWidget(self.split)
        self._build_menus()
        self._show_player()
        self.presenter.refresh(snap=True)
        self.area_view.centre_on_me()
        self.statusBar().showMessage(
            "Saved as you go. Close the window to return to your stories.", 8000
        )
        opening = (session.story.setup.opening_text or "").strip()
        if opening:
            self.say(opening.split("\n\n")[0], kind="note")

    # --- the frame -------------------------------------------------------------------

    def _build_menus(self) -> None:
        game = self.menuBar().addMenu("&Game")
        close = QAction("&Close", self)
        close.setShortcut(QKeySequence("Ctrl+W"))
        close.triggered.connect(self.close)
        game.addAction(close)
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        quit_action.triggered.connect(self.close)
        game.addAction(quit_action)
        view = self.menuBar().addMenu("&View")
        me = QAction("Centre on &me", self)
        me.setShortcut(QKeySequence("Home"))
        me.triggered.connect(self.area_view.centre_on_me)
        view.addAction(me)

    def _show_player(self) -> None:
        session = self.session
        held = session.held_character_id if hasattr(session, "held_character_id") else None
        character = next((c for c in session.cast if c.id == session.story.held_character_id), None)
        self.name.setText(character.name if character is not None else "")
        pixmap = QPixmap()
        if character is not None and character.reference_images:
            data = session.pictures.read(character.reference_images[0].file)
            pixmap = pixmap_within(data, PORTRAIT * 2)
        if not pixmap.isNull():
            self.portrait.setPixmap(
                pixmap.scaled(PORTRAIT, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        self._show_where()
        del held

    def _show_where(self) -> None:
        session = self.session
        state = session.area_state()
        me = state.spots.get(session.held_name() or "")
        area = session.area(me.area) if me is not None else None
        clock = session.map_clock()
        when = f"Day {clock[0]}, {clock[1] // 60:02d}:{clock[1] % 60:02d}" if clock else ""
        self.where.setText(" · ".join(p for p in (area.title if area else "", when) if p))

    def say(self, text: str, *, kind: str = "report") -> None:
        """A line in the log: a report of what happens, a note (the flavour
        of a place or an event), or the player's own words."""
        colour = {"report": "#b8bcc8", "note": "#d8c9a0", "you": "#e6e9f0"}.get(kind, "#b8bcc8")
        self.log.append(f'<p style="color:{colour}">{text}</p>')

    # --- moving ----------------------------------------------------------------------

    def _go(self, x: int, y: int, beside: bool, _name: str = "") -> None:
        if self._fading is not None:
            return
        move = self.session.visual_go(x, y, beside=beside)
        if move is None:
            self.statusBar().showMessage("There's no way there from here.", 4000)
            return
        if move.words:
            self.say(move.words)
        self.presenter.refresh()
        self._show_where()

    def _action(self, verb: str, name: str, target_id: str) -> None:
        """A menu choice on a person or a thing: walked up to for now; what
        it does comes with the rules (step 2) and the people (step 3)."""
        area = self.area_view.area
        if area is None:
            return
        at = None
        if target_id:
            thing = next((p for p in area.placements if p.id == target_id), None)
            if thing is not None:
                at = (int(thing.centre[0]), int(thing.centre[1]))
        if at is None:
            person = next((w for w in self.area_view.walkers if w.name == name), None)
            at = (person.x, person.y) if person is not None else None
        if verb in WALK_UP and at is not None:
            self._go(at[0], at[1], True, name)
        self.say(f"{verb.capitalize()} {name}: not in the game yet.")

    def _travel_menu(self) -> None:
        self.area_view.set_travel(
            [
                (place_id, name, _duration(seconds))
                for place_id, name, seconds in self.session.visual_travel_choices()[:16]
            ]
        )

    def _travel(self, place_id: str) -> None:
        if self._fading is not None:
            return
        gone = self.session.visual_travel(place_id)
        if gone is None:
            self.statusBar().showMessage("There's no way there from here.", 4000)
            return
        name, seconds = gone
        self.say(f"You set off for {name}: {_duration(seconds)}.")
        self._fade_through(lambda: self.presenter.refresh(snap=True))

    def _edge_menu(self, side: str) -> None:
        choices = []
        for choice_id, name, seconds in self.session.edge_choices(side):
            if choice_id == "area":
                choices.append(("area", f"Walk on into {name}"))
            else:
                choices.append((choice_id, f"{name} — {_duration(seconds)}"))
        self.area_view.set_edge_choices(side, choices)

    def _edge_go(self, side: str, choice_id: str) -> None:
        if choice_id != "area":
            self._travel(choice_id)
            return
        name = self.session.visual_cross(side)
        if name is None:
            self.statusBar().showMessage(f"The way {side} is barred here.", 4000)
            return
        self.say(f"You walk on {side}, into {name}.")
        self._fade_through(lambda: self.presenter.refresh(snap=True))

    def _through_door(self, _left: str, _arrived: str) -> None:
        """The walk to the door is over: through it with a fade, the walk on
        from the far door once the new area shows."""
        self._fade_through(self.presenter.arrive)

    def _fade_through(self, between: Callable[[], None]) -> None:
        """To black, `between` (the view changed), and back."""
        if self._fading is not None:
            between()
            return
        viewport = self.area_view.view.viewport()
        self._fade.resize(viewport.size())
        self._fade.raise_()
        self._fade.show()
        animation = QVariantAnimation(self)
        animation.setDuration(FADE_MS * 2)
        animation.setStartValue(0.0)
        animation.setEndValue(2.0)
        animation.setEasingCurve(QEasingCurve.InOutSine)
        done = {"between": False}

        def at(value) -> None:
            if value >= 1.0 and not done["between"]:
                done["between"] = True
                between()
                self._show_where()
            self._fade.opacity = value if value <= 1.0 else 2.0 - value
            self._fade.update()

        def finished() -> None:
            if not done["between"]:
                between()
                self._show_where()
            self._fade.hide()
            self._fading = None
            self._show_where()

        animation.valueChanged.connect(at)
        animation.finished.connect(finished)
        self._fading = animation
        animation.start()

    # --- the line --------------------------------------------------------------------

    def _line_sent(self) -> None:
        text = self.line.text().strip()
        if not text:
            return
        self.line.clear()
        self.say(text, kind="you")
        self.say("Nobody answers yet: talking and acting come in the next steps.")

    # --- Qt --------------------------------------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt's name
        super().resizeEvent(event)
        if self._fade.isVisible():
            self._fade.resize(self.area_view.view.viewport().size())

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt's name
        self.area_view.finish_walk()
        self.presenter.close()
        session, self.session = self.session, None
        if session is not None:
            session.close()
            session.save()
        super().closeEvent(event)
        if self._on_close is not None:
            self._on_close()
        self.closed.emit()


def player_choices(cast: list[Character], choose: list[str]) -> list[Character]:
    """Whom a visual story can be played as: the plot file's "choose one of",
    else every playable character."""
    chosen = [c for c in cast if c.id in choose] if choose else []
    return chosen or [c for c in cast if c.is_player_available and not c.plot_hidden]
