"""The main window's size and how its width is shared between the Stories
list, the story and the Inspector. A mixin for `MainWindow`.

The author found the Stories list too narrow to read on opening: Qt gives a
dock the width it asks for once and then hands every pixel the window gains
to the centre, so a 1280px window maximised on 1366px kept a 174px list and
cut every title. Now the docks take their share of the window's width, by
proportion, every time it changes size, until the author drags a separator;
from then on the widths are theirs, and both they and the window's size are
kept in the config (`Config.window`) for the next launch.

The Stories list is put away while a story is open (the author, Oct 15
2026: more room for the story, as a story with a map first did), unless the
author brings it back (Stories above the transcript, or View → Stories
list): that choice is kept for every story (`Config.stories_list`). With no
story open it is always shown, since it is how a story is opened.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import QDockWidget, QStyle, QToolButton

from sealedlore.models.config import WindowLayout

# A screen narrower than this opens the window maximised; a wider one opens
# it at a share of the screen, up to the largest default size.
ROOMY_SCREEN = 1600
SCREEN_SHARE = 0.85
LARGEST_DEFAULT = (1680, 1050)
SMALLEST_DEFAULT = (1280, 860)

# Each dock's share of the window's width, and the bounds of that share.
STORIES_SHARE = 0.18
STORIES_WIDTHS = (220, 320)
INSPECTOR_SHARE = 0.27
# Above the minimum of any of its pages: squeezed to the empty page's
# minimum, the dock jumped wider when a story opened and filled the forms.
# 450 since the Cast card sets the portrait beside its fields (Oct 15
# 2026; the author: assume 1080p, not a 1366px laptop).
INSPECTOR_WIDTHS = (450, 520)
# A story that keeps a map gives the Inspector, with the map on it, half the
# window (the author's call).
MAP_SHARE = 0.5
# With the Stories list put away (as it is while a story is open, unless the
# author brings it back) the Inspector takes the room it left: the author
# wanted the pages beside the story less cramped and more graphical.
INSPECTOR_ALONE_SHARE = 0.38
INSPECTOR_ALONE_WIDTHS = (450, 720)


def _clamp(value: float, bounds: tuple[int, int]) -> int:
    return int(min(max(value, bounds[0]), bounds[1]))


def _cut(width: int, floor: int, over: int) -> tuple[int, int]:
    """Take up to `over` from `width`, not below `floor`; what is still over."""
    taken = max(0, min(over, width - floor))
    return width - taken, over - taken


def share_width(
    window: int,
    centre_min: int,
    separators: int,
    stories: tuple[int, int],
    inspector: tuple[int, int],
) -> tuple[int, int]:
    """The two docks' widths in a window this wide. `stories` and
    `inspector` are each (wanted, minimum). What the centre can't spare comes
    out of the Inspector down to INSPECTOR_WIDTHS' floor, then the Stories
    list down to its minimum, then the Inspector down to its own."""
    room = window - centre_min - separators
    want_s, want_i = max(stories), max(inspector)
    over = want_s + want_i - room
    want_i, over = _cut(want_i, max(inspector[1], INSPECTOR_WIDTHS[0]), over)
    want_s, over = _cut(want_s, stories[1], over)
    want_i, over = _cut(want_i, inspector[1], over)
    return want_s, want_i


class StoriesDock(QDockWidget):
    """The Stories list's dock: its close button is the author putting the
    list away (`closed`), as View → Stories list is. Never with no story
    open (`may_close`): the list is how one is opened, and closed then it
    was gone until View → Stories list (the review, Oct 15 2026)."""

    closed = Signal()
    may_close: Callable[[], bool] | None = None

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self.may_close is not None and not self.may_close():
            event.ignore()
            return
        super().closeEvent(event)
        self.closed.emit()


class LayoutWindow:
    def _init_layout(self, stories: QDockWidget, inspector: QDockWidget) -> None:
        """Called from `_build_ui` once both docks are in place."""
        self.stories_dock = stories
        self.inspector_dock = inspector
        saved = self.config.window
        # The widths are shared out until the author drags a separator, or
        # were dragged before (kept in the config).
        self._docks_set_by_hand = bool(saved and saved.stories_width and saved.inspector_width)
        self._docks_dragged = False
        self._share_timer = QTimer(self)
        self._share_timer.setSingleShot(True)
        self._share_timer.setInterval(0)
        self._share_timer.timeout.connect(self._share_docks)

    # --- the Stories list ---------------------------------------------------

    def _build_stories_button(self) -> None:
        """Stories, above the transcript: the list back, or away again."""
        self.stories_button = QToolButton()
        self.stories_button.setObjectName("viewToggleButton")
        self.stories_button.setText("Stories")
        self.stories_button.setCheckable(True)
        self.stories_button.setToolTip(
            "Show or hide the list of stories (View → Stories list). It is put away while "
            "a story is open, to give the story the room, unless you bring it back."
        )
        self.stories_button.toggled.connect(self._choose_stories_list)

    def _init_stories_list(self) -> None:
        """Called once the dock and View's action exist."""
        self.stories_list_action.triggered.connect(self._choose_stories_list)
        self.stories_dock.may_close = lambda: getattr(self, "session", None) is not None
        self.stories_dock.closed.connect(lambda: self._choose_stories_list(False))
        self.stories_dock.visibilityChanged.connect(lambda _shown: self._sync_stories_button())
        # The first story made, or the last deleted, with none open.
        self.stories.refreshed.connect(
            lambda: self._sync_stories_list() if getattr(self, "session", None) is None else None
        )
        self._sync_stories_list()

    def _choose_stories_list(self, shown: bool) -> None:
        """The author shows or hides the list. With a story open that is
        their choice for every story from now on (`Config.stories_list`);
        with none open the list is how stories are opened, so it stays."""
        if getattr(self, "session", None) is not None:
            self.config.stories_list = bool(shown)
            self.stories_dock.setVisible(bool(shown))
        else:
            self.stories_dock.setVisible(True)
        self._sync_stories_button()
        self._reshare_docks()

    def _sync_stories_list(self) -> None:
        """The list as it should be now: shown with no story open, else as
        the author last chose (put away unless they brought it back). With no
        stories at all it is empty, and the start page is the way in: away
        (Oct 18 2026: a new player's first screen sat between an empty list
        and an empty side panel)."""
        if getattr(self, "session", None) is None:
            shown = self.stories.has_stories()
        else:
            shown = self.config.stories_list
        self.stories_dock.setVisible(shown)
        self._sync_stories_button()
        self._reshare_docks()

    def _sync_inspector_dock(self, has_story: bool) -> None:
        """The panel beside the story is the open story's: away with none
        open (its empty page only said to open one), back when one opens.
        Changed only as a story opens or closes, so the author's own closing
        of it stands."""
        if getattr(self, "_inspector_for", None) == has_story:
            return
        self._inspector_for = has_story
        self.inspector_dock.setVisible(has_story)
        self._reshare_docks()

    def _sync_stories_button(self) -> None:
        shown = not self.stories_dock.isHidden()
        if self.stories_button.isChecked() != shown:
            self.stories_button.blockSignals(True)
            self.stories_button.setChecked(shown)
            self.stories_button.blockSignals(False)
        self.stories_button.setEnabled(getattr(self, "session", None) is not None)

    def place_on_screen(self) -> None:
        """Size the window for its screen before it is first shown (the
        launch only: tests size their own windows). The size it was left at,
        or a maximised window on a laptop, or most of a larger screen."""
        screen = self.screen()
        if screen is None:
            return
        room = screen.availableGeometry()
        saved = self.config.window
        if saved and saved.width and saved.height:
            width, height = saved.width, saved.height
            maximise = saved.maximized
        else:
            width = max(SMALLEST_DEFAULT[0], round(room.width() * SCREEN_SHARE))
            height = max(SMALLEST_DEFAULT[1], round(room.height() * SCREEN_SHARE))
            width, height = min(width, LARGEST_DEFAULT[0]), min(height, LARGEST_DEFAULT[1])
            maximise = room.width() < ROOMY_SCREEN
        self.resize(min(width, room.width()), min(height, room.height()))
        frame = self.frameGeometry()
        frame.moveCenter(room.center())
        self.move(frame.topLeft())
        if maximise:
            self.setWindowState(self.windowState() | Qt.WindowMaximized)

    def _remember_layout(self) -> None:
        """Called from `closeEvent`, before the config is saved."""
        normal = self.normalGeometry() if self.isMaximized() else self.geometry()
        before = self.config.window or WindowLayout()
        layout = WindowLayout(
            width=normal.width(),
            height=normal.height(),
            maximized=self.isMaximized(),
            stories_width=before.stories_width,
            inspector_width=before.inspector_width,
            composer_height=before.composer_height,
        )
        if self._composer_dragged and self.composer.isVisible():
            layout.composer_height = self.composer.height()
        if self._docks_dragged:
            layout.stories_width = self.stories_dock.width()
            layout.inspector_width = self.inspector_dock.width()
        self.config.window = layout

    # --- sharing the width ----------------------------------------------------

    def event(self, event) -> bool:
        # A press where no widget is, is on a separator between a dock and the
        # centre: the widths are the author's now. (A press a widget didn't
        # take comes up here too, but over that widget.)
        if (
            event.type() == QEvent.MouseButtonPress
            and self.childAt(event.position().toPoint()) is None
        ):
            self._docks_dragged = True
            self._docks_set_by_hand = True
        return super().event(event)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        if not self._docks_dragged:
            # After the layout has taken the new size.
            self._share_timer.start()

    def _reshare_docks(self) -> None:
        """Share the width out again (the window laid out for a map or not)."""
        self._share_timer.start()

    def _share_docks(self) -> None:
        stories, inspector = self.stories_dock, self.inspector_dock
        placed = (
            self.dockWidgetArea(stories) == Qt.LeftDockWidgetArea
            and self.dockWidgetArea(inspector) == Qt.RightDockWidgetArea
        )
        if not placed or stories.isFloating() or inspector.isFloating():
            return  # moved by the author: theirs to size
        if self._docks_dragged:
            # The author's widths, from the drag on: opening or closing a
            # story reshared them away (a drag to 300/560 came back 288/462).
            return
        width = self.width()
        centre = self.centralWidget()
        separator = self.style().pixelMetric(QStyle.PM_DockWidgetSeparatorExtent, None, self)
        centre_min = max(centre.minimumWidth(), centre.minimumSizeHint().width())
        on_map = getattr(self, "map_layout", False)
        saved = self.config.window
        by_hand = self._docks_set_by_hand and saved is not None and saved.inspector_width
        if not inspector.isVisible():
            # No story open (the panel is the open story's): the list alone,
            # at the width the author left it or its share.
            if stories.isVisible():
                want_s = (
                    saved.stories_width
                    if self._docks_set_by_hand and saved is not None and saved.stories_width
                    else _clamp(width * STORIES_SHARE, STORIES_WIDTHS)
                )
                self.resizeDocks([stories], [want_s], Qt.Horizontal)
            return
        if not stories.isVisible():
            if on_map:
                want_i = max(round(width * MAP_SHARE), INSPECTOR_WIDTHS[0])
            elif by_hand:
                # A width the author dragged before, kept from the last time:
                # applied with no story open only, it was lost to the share
                # whenever one was.
                want_i = saved.inspector_width
            else:
                want_i = _clamp(width * INSPECTOR_ALONE_SHARE, INSPECTOR_ALONE_WIDTHS)
            want_i = min(want_i, max(INSPECTOR_WIDTHS[0], width - centre_min - separator))
            self.resizeDocks([inspector], [want_i], Qt.Horizontal)
            return
        if on_map:
            # Brought back by hand: the list takes its share from the story.
            want_i = max(round(width * MAP_SHARE), INSPECTOR_WIDTHS[0])
            want_s = _clamp(width * STORIES_SHARE, STORIES_WIDTHS)
            widths = share_width(
                width,
                centre_min,
                2 * separator,
                (want_s, stories.minimumSizeHint().width()),
                (want_i, inspector.minimumSizeHint().width()),
            )
            self.resizeDocks([stories, inspector], list(widths), Qt.Horizontal)
            return
        if by_hand:
            want_s, want_i = saved.stories_width, saved.inspector_width
        else:
            want_s = _clamp(width * STORIES_SHARE, STORIES_WIDTHS)
            want_i = _clamp(width * INSPECTOR_SHARE, INSPECTOR_WIDTHS)
        widths = share_width(
            width,
            centre_min,
            2 * separator,
            (want_s, stories.minimumSizeHint().width()),
            (want_i, inspector.minimumSizeHint().width()),
        )
        self.resizeDocks([stories, inspector], list(widths), Qt.Horizontal)
