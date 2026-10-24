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

import base64
import html
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QObject,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QAction, QColor, QImage, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSplitter,
    QTextEdit,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.area import Placement
from sealedlore.engine.keeper import Ruling
from sealedlore.engine.look import LookReply, Rect
from sealedlore.engine.npc import NpcReply
from sealedlore.gui.area_render import px_for
from sealedlore.gui.area_view import AreaView, Walker, _bare_name
from sealedlore.gui.image_codec import encoded
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
# The line: an action with nobody to talk to, words to someone, a Direction.
LINE_HINT = "Say what you attempt (the game rules on it), or Talk to someone first"
TALK_HINT = "Say something to {name} (Enter); start with /d to direct the story instead"
DIRECT_HINT = "Direct the story: what you say is so (Enter)"
DIRECTOR_PREFIX = "/d"
# The picture of a part sent to the Look's model: no wider than this.
LOOK_PICTURE_SIDE = 768
# A thing looked at: its footprint and a tile round it.
LOOK_MARGIN = 1


# A journey by vehicle in Travel…'s entries: "<DRIVE><record id>|<place id>".
DRIVE = "drive:"


def _driven(seconds: int) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes} min by road" if minutes < 90 else f"{minutes / 60:.1f} h by road"


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
    window's tools (tile sets, paintings, objects, Edit Mode); and Look: a
    frame dragged over a part of the area (`look_drawn`), or a thing's menu
    (`look_thing`)."""

    look_drawn = Signal(int, int, int, int)
    look_thing = Signal(str)

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
        self.looking = False
        # The window supplies what a thing is known to hold (its record's
        # contents) and the uses the keeper granted it (its menu's verbs).
        self.contents_of: Callable[[str], list[str]] = lambda _id: []
        self.uses_of: Callable[[str], list[str]] = lambda _id: []
        # The dead in the area shown (their walkers' names are their ids),
        # and whoever lies fallen.
        self.creature_ids: set[str] = set()
        self.fallen: set[str] = set()

    def set_looking(self, on: bool) -> None:
        """Look at…: the next drag over the area frames what to look at."""
        self.looking = on
        self._clear_stroke()
        self.hint.setText(self._hint())
        self.view.viewport().setCursor(Qt.CrossCursor if on else Qt.ArrowCursor)

    def remaining_steps(self) -> list[tuple[int, int]]:
        """The tiles of the walk being shown still to walk."""
        return list(self._steps)

    def _hint(self) -> str:
        if self.locked:
            return "One moment."
        if self.looking:
            return "Drag a frame round what you want to look at closely. Esc to stop."
        return (
            "Click where to go, a building to go inside, or a door to go through it. "
            "Right-click a thing to look at it closely, or Look at… to frame any part. "
            "Travel… sets off for another place; Leave walks on past an edge."
        )

    def _stroke(self, phase: str, point: QPointF) -> bool:
        if not self.looking or self.area is None or self.locked:
            return super()._stroke(phase, point)
        tile = self.tile_at(point)
        if phase == "press":
            if tile is None:
                return False
            self._clear_stroke()
            self._stroke_from = tile
        if self._stroke_from is None:
            return False
        corner = tile or self._stroke_from
        self._show_band(self._stroke_from, corner)
        if phase == "release":
            (x0, y0), (x1, y1) = self._stroke_from, corner
            self._clear_stroke()
            self.look_drawn.emit(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        return True

    def cancel_pending(self) -> bool:
        if self.looking:
            self.set_looking(False)
            return True
        return super().cancel_pending()

    def _person_menu(self, person: Walker, at, mark=None) -> None:
        menu = QMenu(self)
        if person.name in self.creature_ids:
            down = person.name in self.fallen
            title = menu.addAction("One of the dead, down" if down else "One of the dead")
            title.setEnabled(False)
            if not down:
                attack = menu.addAction("Attack it")
                attack.setEnabled(not self.locked)
                attack.triggered.connect(lambda: self.action_chosen.emit("attack", person.name, ""))
            self._show_menu(menu, at)
            return
        if person.held:
            return
        if person.name in self.fallen:
            title = menu.addAction(f"{person.name}, down")
            title.setEnabled(False)
            self._show_menu(menu, at)
            return
        title = menu.addAction(person.name)
        title.setEnabled(False)
        talk = menu.addAction("Talk to them")
        talk.setEnabled(not self.locked)
        talk.triggered.connect(lambda: self.action_chosen.emit("talk", person.name, ""))
        attack = menu.addAction("Attack them")
        attack.setEnabled(not self.locked)
        attack.triggered.connect(lambda: self.action_chosen.emit("attack", person.name, ""))
        menu.addSeparator()
        walk = menu.addAction("Go over to them")
        walk.setEnabled(not self.locked)
        walk.triggered.connect(
            lambda: self.go_requested.emit(person.x, person.y, True, person.name)
        )
        self._show_menu(menu, at)

    def _thing_menu(self, thing: Placement, at, mark=None) -> None:
        menu = QMenu(self)
        title = menu.addAction(thing.name[0].upper() + thing.name[1:])
        title.setEnabled(False)
        for index, item in enumerate(self.contents_of(thing.id)):
            take = menu.addAction(f"Take {item}")
            take.setEnabled(not self.locked)
            take.triggered.connect(
                lambda _c=False, i=index: self.action_chosen.emit("take_content", thing.id, str(i))
            )
        look = menu.addAction("Look at it closely")
        look.setEnabled(not self.locked)
        look.triggered.connect(lambda: self.look_thing.emit(thing.id))
        for verb in thing.verbs:
            if verb not in ("take", "destroy"):
                continue  # eating and using are the inventory's
            action = menu.addAction("Take it" if verb == "take" else "Break it")
            action.setEnabled(not self.locked)
            action.triggered.connect(
                lambda _c=False, v=verb: self.action_chosen.emit(v, thing.name, thing.id)
            )
        for use in self.uses_of(thing.id):
            # A use the keeper granted (a repaired truck's "drive"): the
            # attempt goes back to the keeper with the thing's state.
            action = menu.addAction(f"{use[0].upper()}{use[1:]} it")
            action.setEnabled(not self.locked)
            action.triggered.connect(
                lambda _c=False, v=use: self.action_chosen.emit(f"use:{v}", thing.name, thing.id)
            )
        menu.addSeparator()
        walk = menu.addAction("Go over to it")
        walk.setEnabled(not self.locked)
        cx, cy = thing.centre
        walk.triggered.connect(
            lambda: self.go_requested.emit(int(cx), int(cy), True, _bare_name(thing.name))
        )
        self._show_menu(menu, at)

    def picture_of(self, rect: Rect) -> QImage:
        """The part as the view shows it (the painting, the things and the
        people, not the frame), for the Look's model."""
        if self.area is None:
            return QImage()
        px = px_for(self.area)
        x0, y0, x1, y1 = rect
        source = QRectF(x0 * px, y0 * px, (x1 - x0 + 1) * px, (y1 - y0 + 1) * px)
        scale = min(1.0, LOOK_PICTURE_SIDE / max(source.width(), source.height(), 1.0))
        image = QImage(
            max(1, int(source.width() * scale)),
            max(1, int(source.height() * scale)),
            QImage.Format_RGB32,
        )
        image.fill(QColor(0, 0, 0))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        self.scene.render(painter, QRectF(0, 0, image.width(), image.height()), source)
        painter.end()
        return image


class _LookDone(QObject):
    """The Look's reply, emitted on its thread, delivered on the GUI thread."""

    done = Signal(object, str)


class _TalkDone(QObject):
    """A person's reply, emitted on its thread, delivered on the GUI thread:
    (the person's name, the reply or None, the problem)."""

    done = Signal(str, object, str)


class _KeeperDone(QObject):
    """The keeper's reply: (the kind, the line, the reply or None, the problem)."""

    done = Signal(str, str, object, str)


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
        self.area_view.look_drawn.connect(self._look_part)
        self.area_view.look_thing.connect(self._look_thing)
        self.area_view.contents_of = self._contents_of
        self.area_view.uses_of = lambda pid: self.session.visual_uses(pid) if self.session else []
        # The wounds shown last, so a new one flashes the figure.
        self._wounds_shown = session.area_state().wounds
        self._fade = _Fade(self.area_view.view.viewport())
        self._fading: QVariantAnimation | None = None
        self._look_signal = _LookDone()
        self._look_signal.done.connect(self._looked)
        # The part being looked at, while the model answers.
        self._look_pending: tuple[str, Rect] | None = None

        # The column: who is talking, the log, the line.
        self.talking_portrait = QLabel()
        self.talking_portrait.setFixedSize(QSize(48, 64))
        self.talking_portrait.setAlignment(Qt.AlignCenter)
        self.talking_name = QLabel()
        self.talking_name.setObjectName("emptyTitle")
        self.talking_mood = QLabel()
        self.talking_mood.setObjectName("hintLabel")
        self.talking_stop = QToolButton()
        self.talking_stop.setText("Done talking")
        self.talking_stop.clicked.connect(lambda: self._set_talking(None))
        talking = QWidget()
        talking.setObjectName("visualTalking")
        talking_layout = QHBoxLayout(talking)
        talking_layout.setContentsMargins(0, 0, 0, 0)
        talking_words = QVBoxLayout()
        talking_words.addWidget(self.talking_name)
        talking_words.addWidget(self.talking_mood)
        talking_layout.addWidget(self.talking_portrait)
        talking_layout.addLayout(talking_words, 1)
        talking_layout.addWidget(self.talking_stop, 0, Qt.AlignTop)
        self.talking = talking
        self.talking.hide()
        self._talking_to: str | None = None
        self._talk_pending: str | None = None
        self._talk_signal = _TalkDone()
        self._talk_signal.done.connect(self._talked)
        self.log = QTextEdit()
        self.log.setObjectName("visualLog")
        self.log.setReadOnly(True)
        self.line = QLineEdit()
        self.line.setPlaceholderText(LINE_HINT)
        self.line.returnPressed.connect(self._line_sent)
        self.director_tool = QToolButton()
        self.director_tool.setText("Director")
        self.director_tool.setCheckable(True)
        self.director_tool.setToolTip(
            "What you type is so: the story's keeper makes it happen on the board, whatever "
            "it takes (or start a line with /d)"
        )
        self.director_tool.toggled.connect(self._director_toggled)
        line_row = QHBoxLayout()
        line_row.setContentsMargins(0, 0, 0, 0)
        line_row.addWidget(self.line, 1)
        line_row.addWidget(self.director_tool)
        self._keeper_signal = _KeeperDone()
        self._keeper_signal.done.connect(self._kept)
        self._keeper_pending: str | None = None
        side = QWidget()
        side.setObjectName("visualSide")
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(8, 8, 8, 8)
        side_layout.setSpacing(6)
        side_layout.addWidget(self.talking)
        side_layout.addWidget(self.log, 1)
        side_layout.addLayout(line_row)
        side.setMinimumWidth(SIDE_MIN)

        # Under the view: the player, where they are, the clock.
        self.portrait = QLabel()
        self.portrait.setFixedSize(PORTRAIT)
        self.portrait.setAlignment(Qt.AlignCenter)
        self.name = QLabel()
        self.name.setObjectName("emptyTitle")
        self.where = QLabel()
        self.where.setObjectName("hintLabel")
        self.health = QLabel()
        self.health.setToolTip("Hits you can still take")
        self.carry_button = QToolButton()
        self.carry_button.setText("Carrying")
        self.carry_button.setToolTip("What you carry: eat, drink, use or put down any of it")
        self.carry_button.setPopupMode(QToolButton.InstantPopup)
        self.carry_menu = QMenu(self)
        self.carry_menu.aboutToShow.connect(self._fill_carry_menu)
        self.carry_button.setMenu(self.carry_menu)
        bar = QWidget()
        bar.setObjectName("visualBar")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(8, 4, 8, 4)
        words = QVBoxLayout()
        words.addWidget(self.name)
        words.addWidget(self.where)
        bar_layout.addWidget(self.portrait)
        bar_layout.addLayout(words, 1)
        bar_layout.addWidget(self.health)
        bar_layout.addWidget(self.carry_button)

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
        self._build_toolbar()
        session.visual_lay_out()
        self._show_player()
        self.presenter.refresh(snap=True)
        self.area_view.centre_on_me()
        self.statusBar().showMessage(
            "Saved as you go. Close the window to return to your stories.", 8000
        )
        opening = (session.story.setup.opening_text or "").strip()
        if opening:
            self.say(opening.split("\n\n")[0], kind="note")
        self._after_action([])

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

    def _build_toolbar(self) -> None:
        bar = QToolBar("Play")
        bar.setMovable(False)
        self.addToolBar(bar)
        self.look_tool = QToolButton()
        self.look_tool.setText("Look at…")
        self.look_tool.setCheckable(True)
        self.look_tool.setToolTip(
            "Look closely at a part of the place: drag a frame round it, and the model is "
            "shown it and says what you see and find there (a right-click on a thing looks "
            "at that thing)"
        )
        self.look_tool.toggled.connect(self.area_view.set_looking)
        bar.addWidget(self.look_tool)
        wait = QAction("Wait an hour", self)
        wait.setToolTip("An hour passes where you stand")
        wait.triggered.connect(lambda: self._pass_time(False))
        bar.addAction(wait)
        sleep = QAction("Sleep till morning", self)
        sleep.setToolTip("Sleep until seven in the morning, here")
        sleep.triggered.connect(lambda: self._pass_time(True))
        bar.addAction(sleep)

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
        if session is None:
            return
        state = session.area_state()
        me = state.spots.get(session.held_name() or "")
        area = session.area(me.area) if me is not None else None
        clock = session.map_clock()
        when = f"Day {clock[0]}, {clock[1] // 60:02d}:{clock[1] % 60:02d}" if clock else ""
        self.where.setText(" · ".join(p for p in (area.title if area else "", when) if p))
        hits = session.visual_hits_left()
        self.health.setText("♥" * hits + "♡" * (session.visual_design().player_hits - hits))
        carrying = session.visual_inventory()
        self.carry_button.setText(f"Carrying ({len(carrying)})" if carrying else "Carrying nothing")

    def _after_action(self, lines: list[str], *, moment: str = "") -> None:
        """After anything that moved the game: the rules' lines, what is
        newly seen or heard (a sound cue in its own colour), the bar, a hit's
        flash, and death when it comes. `moment`: a moment of the plot the
        window knows of (the player has slept), for the keeper."""
        session = self.session
        if session is None:
            return
        for line in lines:
            if line:
                self.say(line, kind="report")
        for line in session.visual_report():
            self.say(line, kind="sound" if line.startswith("You hear") else "note")
        self._show_where()
        wounds = session.area_state().wounds
        if wounds > self._wounds_shown:
            held = self.area_view.held()
            if held is not None:
                self.area_view.flash_person(held.name)
        self._wounds_shown = wounds
        if session.area_state().dead:
            self._died()
            return
        if self._talking_to is not None and not session.talk_target(self._talking_to)[0]:
            self._set_talking(None)
        self._greet_newcomers()
        plot_lines, why = session.visual_plot_check(moment)
        for line in plot_lines:
            self.say(line, kind="note")
        if why:
            self._ask_keeper("moment", why)

    def _died(self) -> None:
        self.area_view.set_locked(True)
        box = QMessageBox(self)
        box.setWindowTitle("You are dead")
        box.setText("The story ends here. Start again from the opening, or close the game.")
        again = box.addButton("Start again", QMessageBox.AcceptRole)
        box.addButton("Close", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is again and self.session is not None:
            self.session.visual_reset()
            self.session.visual_lay_out()
            self.log.clear()
            self.area_view.set_locked(False)
            self.presenter.refresh(snap=True)
            self._after_action(["You start again."])
        else:
            self.close()

    def say(self, text: str, *, kind: str = "report") -> None:
        """A line in the log: a report of what happens, a note (the flavour
        of a place or an event), or the player's own words."""
        colour = {
            "report": "#b8bcc8",
            "note": "#d8c9a0",
            "you": "#e6e9f0",
            "speech": "#f4e9c8",
            "sound": "#9fb8c8",
        }.get(kind, "#b8bcc8")
        words = html.escape(text)
        if kind == "sound":
            words = f"<i>{words}</i>"
        self.log.append(f'<p style="color:{colour}">{words}</p>')

    # --- moving ----------------------------------------------------------------------

    def _settle_figure(self) -> None:
        """A walk still being shown ends where the figure is, and the game
        has the player there (the author, Oct 24 2026: no changing
        destination part way; the game waited for the walk to end). The
        unwalked tiles' time is given back."""
        view = self.area_view
        if not view.walking() or view.area is None:
            return
        # Cut, not finished: a finish tells the presenter, which walks the
        # figure on to the old destination before the new click is seen.
        remaining = view.cut_walk()
        held = view.held()
        if held is None:
            return
        refund = self.session.unwalked_seconds(view.area, [(held.x, held.y), *remaining])
        self.presenter.cancel_walk()
        self.session.visual_stand(view.area.id, held.x, held.y, refund)

    def _go(self, x: int, y: int, beside: bool, _name: str = "") -> None:
        if self._fading is not None:
            return
        self._settle_figure()
        move = self.session.visual_go(x, y, beside=beside)
        if move is None:
            self.statusBar().showMessage("There's no way there from here.", 4000)
            return
        if move.words:
            self.say(move.words)
        self.presenter.refresh()
        self._after_action(move.lines)
        if move.locked is not None:
            self._locked_door(*move.locked)

    def _action(self, verb: str, name: str, target_id: str) -> None:
        """A menu choice on a person, one of the dead or a thing: the rules
        for things and the dead; people wait for step 3."""
        session = self.session
        area = self.area_view.area
        if session is None or area is None or self._fading is not None:
            return
        if verb == "take_content":
            holder = next((p for p in area.placements if p.id == name), None)
            if holder is not None:
                self._go(int(holder.centre[0]), int(holder.centre[1]), True, holder.name)
            self.say(session.visual_take_content(name, int(target_id)))
            self.presenter.refresh(snap=True)
            self._after_action([])
            return
        at = None
        if target_id:
            thing = next((p for p in area.placements if p.id == target_id), None)
            if thing is not None:
                at = (int(thing.centre[0]), int(thing.centre[1]))
        if at is None:
            person = next((w for w in self.area_view.walkers if w.name == name), None)
            at = (person.x, person.y) if person is not None else None
        if verb == "talk" and session.has_card(name):
            if at is not None:
                self._go(at[0], at[1], True, name)
            can, why = session.talk_target(name)
            if not can:
                self.say(why)
                return
            self._set_talking(name)
            return
        if verb == "attack" and session.has_card(name) and name not in self.area_view.creature_ids:
            if at is not None:
                self._go(at[0], at[1], True, name)
            lines = session.visual_attack_person(name)
            self.presenter.refresh(snap=True)
            self._after_action(lines)
            if self._talking_to == name:
                self._set_talking(None)
            return
        if verb == "attack" and name in self.area_view.creature_ids:
            lines: list[str] = []
            for _try in range(3):
                # Walk up and strike; one that shambled off meanwhile is
                # followed up to twice more.
                creature = session.area_state().creatures.get(name)
                if creature is None or creature.state == "down":
                    break
                self._go(creature.x, creature.y, True, name)
                lines = session.visual_attack(name)
                if lines != ["It isn't within reach."]:
                    break
            self.presenter.refresh(snap=True)
            self._after_action(lines)
            return
        if verb in ("take", "destroy") and target_id:
            if at is not None:
                self._go(at[0], at[1], True, name)
            line = (
                session.visual_take(target_id)
                if verb == "take"
                else session.visual_break(target_id)
            )
            self.presenter.refresh(snap=True)
            self._after_action(line.split("\n"))
            return
        if verb.startswith("use:"):
            # A use the keeper granted this thing: walk up, and the attempt
            # goes to the keeper with the thing's state.
            if at is not None:
                self._go(at[0], at[1], True, name)
            words = session.visual_attempt_words(verb[4:], name, target_id)
            self.say(f"{session.held_name()} attempts: {words}", kind="you")
            self._ask_keeper("action", words)
            return
        if verb in WALK_UP and at is not None:
            self._go(at[0], at[1], True, name)
        # No rule of the game's own: the keeper rules on it as an attempt.
        words = session.visual_attempt_words(verb, name, target_id)
        self.say(f"{session.held_name()} attempts: {words}", kind="you")
        self._ask_keeper("action", words)

    def _locked_door(self, x: int, y: int) -> None:
        """A locked door stopped the move: force it, or not."""
        session = self.session
        if session is None:
            return
        door = session.visual_door(x, y)
        if door is None or door.sealed:
            return
        answer = QMessageBox.question(
            self,
            "Locked",
            "The door is locked" + (f"; it wants a {door.key}" if door.key else "") + ". Force it? "
            "That takes strength, and makes a noise.",
        )
        if answer == QMessageBox.Yes:
            lines = session.visual_force(x, y).split("\n")
            self.presenter.refresh(snap=True)
            self._after_action(lines)

    def _pass_time(self, sleep: bool) -> None:
        session = self.session
        if session is None or self._fading is not None:
            return
        lines = session.visual_sleep() if sleep else session.visual_wait(60)
        self.presenter.refresh(snap=True)
        # Sleeping is a moment of the plot (Doomsville's "Alone too long"):
        # the keeper is asked when anything is armed.
        self._after_action(
            lines or ["Time passes."],
            moment="the player has just slept the night through" if sleep else "",
        )

    def _fill_carry_menu(self) -> None:
        session = self.session
        self.carry_menu.clear()
        if session is None:
            return
        carrying = session.visual_inventory()
        if not carrying:
            self.carry_menu.addAction("Nothing").setEnabled(False)
            return
        for record_id, record in carrying:
            words = f"{_bare_name(record.name)}" + (f" ×{record.count}" if record.count > 1 else "")
            sub = self.carry_menu.addMenu(words)
            for verb in session.visual_item_actions(record):
                action = sub.addAction(verb.capitalize())
                action.triggered.connect(
                    lambda _c=False, r=record_id, v=verb: self._carry_action(r, v)
                )

    def _carry_action(self, record_id: str, verb: str) -> None:
        session = self.session
        if session is None:
            return
        line = (
            session.visual_drop(record_id)
            if verb == "drop"
            else session.visual_use(record_id, verb)
            if verb in ("eat", "drink", "use")
            else ""
        )
        if not line:
            # No rule for it (a tool used, a use the keeper granted): the
            # keeper rules on the attempt, with the thing's state.
            record = session.area_state().things.get(record_id)
            words = session.visual_attempt_words(verb, record.name if record else "it", record_id)
            self.say(f"{session.held_name()} attempts: {words}", kind="you")
            self._ask_keeper("action", words)
            return
        self.presenter.refresh(snap=True)
        self._after_action([line])

    def _travel_menu(self) -> None:
        """Travel…: every designed place on foot, and by any vehicle within
        reach that the keeper has made to work (`visual_drive_choices`)."""
        choices = [
            (place_id, name, _duration(seconds))
            for place_id, name, seconds in self.session.visual_travel_choices()[:16]
        ]
        for vehicle_id, vehicle, journeys in self.session.visual_drive_choices():
            choices += [
                (f"{DRIVE}{vehicle_id}|{place_id}", f"{name} by {vehicle}", _driven(seconds))
                for place_id, name, seconds in journeys[:16]
            ]
        self.area_view.set_travel(choices)

    def _travel(self, place_id: str) -> None:
        if self._fading is not None:
            return
        self._settle_figure()
        vehicle = None
        if place_id.startswith(DRIVE):
            vehicle, place_id = place_id[len(DRIVE) :].split("|", 1)
        gone = self.session.visual_travel(place_id, vehicle=vehicle)
        if gone is None:
            self.statusBar().showMessage("There's no way there from here.", 4000)
            return
        name, seconds = gone
        how = _driven(seconds) if vehicle else _duration(seconds)
        self.say(f"You {'drive' if vehicle else 'set off'} for {name}: {how}.")
        self._fade_through(lambda: self._arrived(seconds))

    def _edge_menu(self, side: str) -> None:
        choices = []
        for choice_id, name, seconds in self.session.visual_edge_choices(side):
            if choice_id == "area":
                choices.append(("area", f"Walk on into {name}"))
            else:
                choices.append((choice_id, f"{name} — {_duration(seconds)}"))
        self.area_view.set_edge_choices(side, choices)

    def _edge_go(self, side: str, choice_id: str) -> None:
        if choice_id != "area":
            self._travel(choice_id)
            return
        self._settle_figure()
        name = self.session.visual_cross(side)
        if name is None:
            self.statusBar().showMessage(f"The way {side} is barred here.", 4000)
            return
        self.say(f"You walk on {side}, into {name}.")
        self._fade_through(lambda: self._arrived(0))

    def _arrived(self, seconds: int) -> None:
        """A journey's end: the dead there have had the journey's time; and
        an arrival is a moment of the plot wherever it is (an event with no
        place of its own, the bandits on the road, has nowhere else to come)."""
        session = self.session
        if session is None:
            return
        lines = session.visual_tick(min(seconds, 120))
        self.presenter.refresh(snap=True)
        state = session.area_state()
        me = state.spots.get(session.held_name() or "")
        area = session.area(me.area) if me is not None else None
        where = area.title if area is not None else "a new place"
        self._after_action(lines, moment=f"the player has just arrived at {where} after a journey")

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

    # --- Look (engine/look.py) -------------------------------------------------------

    def _contents_of(self, placement_id: str) -> list[str]:
        if self.session is None:
            return []
        record = self.session.area_state().things.get(placement_id)
        if record is None:
            return []
        return [f"{f.name} ×{f.count}" if f.count > 1 else f.name for f in record.contents]

    def _look_thing(self, placement_id: str) -> None:
        area = self.area_view.area
        thing = next((p for p in area.placements if p.id == placement_id), None) if area else None
        if thing is None:
            return
        self._look_part(
            thing.x - LOOK_MARGIN,
            thing.y - LOOK_MARGIN,
            thing.x + thing.w - 1 + LOOK_MARGIN,
            thing.y + thing.h - 1 + LOOK_MARGIN,
        )

    def _look_part(self, x0: int, y0: int, x1: int, y1: int) -> None:
        """Look closely at this part of the area: its picture and what the
        game knows of it go to the model; what it finds becomes real."""
        session = self.session
        area = self.area_view.area
        if session is None or area is None:
            return
        self.look_tool.setChecked(False)
        if self._look_pending is not None:
            self.statusBar().showMessage("Still looking: one look at a time.", 4000)
            return
        rect = session.look_rect(area.id, (x0, y0, x1, y1))
        picture = ""
        if session.look_model_reads_pictures():
            image = self.area_view.picture_of(rect)
            if not image.isNull():
                data = encoded(image, "JPEG", 85)
                picture = "data:image/jpeg;base64," + base64.b64encode(data).decode()
        started = session.start_look(area.id, rect, picture, self._look_signal.done.emit)
        if not started:
            self.statusBar().showMessage("Nothing to look at here.", 4000)
            return
        self._look_pending = (area.id, rect)
        self.say("You look closely…")
        self.statusBar().showMessage(f"Looking, with {session.role_model('look')}…")

    def _looked(self, reply: LookReply | None, problem: str) -> None:
        pending, self._look_pending = self._look_pending, None
        session = self.session
        if session is None or pending is None:
            return
        self.statusBar().clearMessage()
        if reply is None:
            self.say(f"You can't make it out ({problem or 'no answer'}).")
            return
        area_id, rect = pending
        if reply.see:
            self.say(reply.see, kind="note")
        for line in session.apply_look_reply(area_id, rect, reply):
            self.say(line)
        if not reply.found:
            self.say("You find nothing you could take.")
        self.presenter.refresh(snap=True)
        self._show_where()

    # --- the line --------------------------------------------------------------------

    def _line_sent(self) -> None:
        text = self.line.text().strip()
        if not text:
            return
        session = self.session
        if session is None:
            return
        directing = self.director_tool.isChecked() or text.lower().startswith(DIRECTOR_PREFIX)
        if directing:
            words = (
                text[len(DIRECTOR_PREFIX) :].strip()
                if text.lower().startswith(DIRECTOR_PREFIX)
                else text
            )
            if words:
                self.line.clear()
                self.say(f"Director: {words}", kind="you")
                self._ask_keeper("direction", words)
            return
        if self._talking_to is None:
            self.line.clear()
            self.say(f"{session.held_name()} attempts: {text}", kind="you")
            self._ask_keeper("action", text)
            return
        if self._talk_pending is not None:
            self.statusBar().showMessage(f"{self._talking_to} is still answering.", 4000)
            return
        can, why = session.talk_target(self._talking_to)
        if not can:
            self.say(why)
            self.line.clear()
            return
        self.line.clear()
        self.say(f"{session.held_name()}: {text}", kind="you")
        self._ask_person(self._talking_to, text)

    # --- the plot keeper (engine/keeper.py) ------------------------------------------

    def _director_toggled(self, on: bool) -> None:
        if on:
            self.line.setPlaceholderText(DIRECT_HINT)
        elif self._talking_to is not None:
            self.line.setPlaceholderText(TALK_HINT.format(name=self._talking_to))
        else:
            self.line.setPlaceholderText(LINE_HINT)
        self.line.setFocus()

    def _ask_keeper(self, kind: str, line: str) -> None:
        """A moment of the plot, a Direction or an unusual action goes to
        the keeper on a thread; one at a time."""
        session = self.session
        if session is None:
            return
        if self._keeper_pending is not None:
            self.statusBar().showMessage("The story's keeper is still at work: one moment.", 4000)
            return
        started = session.start_keeper(
            kind,
            line,
            lambda reply, problem, k=kind, w=line: self._keeper_signal.done.emit(
                k, w, reply, problem
            ),
        )
        if not started:
            if kind != "moment":
                self.say("The game has no rule for that, and nobody to ask.")
            return
        self._keeper_pending = kind
        self.statusBar().showMessage(
            f"The story's keeper is at work, with {session.role_model('keeper')}…"
        )

    def _kept(self, kind: str, line: str, reply, problem: str) -> None:
        session = self.session
        self._keeper_pending = None
        if session is None:
            return
        self.statusBar().clearMessage()
        if reply is None:
            if kind != "moment":
                self.say(f"Nothing comes of it ({problem or 'no answer'}).")
            return
        if isinstance(reply, Ruling):
            lines = session.apply_ruling(reply, line)
        else:
            lines = session.apply_keeper_reply(reply)
        self.presenter.refresh(snap=True)
        self._after_action(lines)
        ended = session.area_state().ended
        if ended and not session.area_state().dead:
            self._ended(ended)

    def _ended(self, words: str) -> None:
        self.area_view.set_locked(True)
        box = QMessageBox(self)
        box.setWindowTitle("The end")
        box.setText(words + "\n\nStart again from the opening, or close the game.")
        again = box.addButton("Start again", QMessageBox.AcceptRole)
        box.addButton("Close", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is again and self.session is not None:
            self.session.visual_reset()
            self.session.visual_lay_out()
            self.log.clear()
            self.area_view.set_locked(False)
            self.presenter.refresh(snap=True)
            self._after_action(["You start again."])
        else:
            self.close()

    # --- the people (engine/npc.py) --------------------------------------------------

    def _set_talking(self, name: str | None) -> None:
        """Whom the line speaks to: their portrait and mood in the column."""
        session = self.session
        self._talking_to = name
        if name is None or session is None:
            self.talking.hide()
            self.line.setPlaceholderText(LINE_HINT)
            return
        card = session.visual_card(name)
        pixmap = QPixmap()
        if card is not None and card.reference_images:
            pixmap = pixmap_within(
                session.pictures.read(card.reference_images[0].file), QSize(96, 128)
            )
        self.talking_portrait.setPixmap(
            pixmap.scaled(QSize(48, 64), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            if not pixmap.isNull()
            else QPixmap()
        )
        self.talking_name.setText(name)
        mood = session.area_state().moods.get(name, "")
        self.talking_mood.setText(mood)
        self.talking.show()
        self.line.setPlaceholderText(TALK_HINT.format(name=name))
        self.line.setFocus()

    def _ask_person(self, name: str, line: str) -> None:
        """What the player says (or "" when they have just appeared) goes to
        the person's model on a thread."""
        session = self.session
        if session is None or self._talk_pending is not None:
            return
        started = session.start_talk(
            name,
            line,
            lambda reply, problem, n=name: self._talk_signal.done.emit(n, reply, problem),
        )
        if not started:
            if line:
                can, why = session.talk_target(name)
                if why:
                    self.say(why)
            return
        self._talk_pending = name
        self.statusBar().showMessage(f"{name} is answering, with {session.role_model('npc')}…")

    def _talked(self, name: str, reply: NpcReply | None, problem: str) -> None:
        session = self.session
        self._talk_pending = None
        if session is None:
            return
        self.statusBar().clearMessage()
        if reply is None:
            self.say(f"{name} doesn't answer ({problem or 'no reply'}).")
            return
        if reply.say:
            self.say(f"{name}: {reply.say}", kind="speech")
        lines = session.apply_npc_reply(name, reply)
        self.presenter.refresh(snap=True)
        self._after_action(lines, moment=session.talk_moment(name))
        if self._talking_to == name:
            self.talking_mood.setText(session.area_state().moods.get(name, ""))
        if reply.intent in ("leave", "fetch_help", "attack", "hide") and self._talking_to == name:
            self._set_talking(None)

    def _greet_newcomers(self) -> None:
        """Someone newly in sight here speaks first, or not: one encounter
        call a person an area (the author's point 2: the model decides how
        the person responds)."""
        session = self.session
        if session is None or self._talk_pending is not None:
            return
        for name in session.people_in_sight():
            if session.encounter_due(name):
                session.mark_encountered(name)
                self._ask_person(name, "")
                return

    # --- Qt --------------------------------------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt's name
        super().resizeEvent(event)
        if self._fade.isVisible():
            self._fade.resize(self.area_view.view.viewport().size())

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt's name
        self._settle_figure()
        self._look_pending = None
        self._talk_pending = None
        self._keeper_pending = None
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
