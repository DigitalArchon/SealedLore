"""The main window's size and how its width is shared between the Stories
list, the story and the Inspector. A mixin for `MainWindow`.

The author found the Stories list too narrow to read on opening: Qt gives a
dock the width it asks for once and then hands every pixel the window gains
to the centre, so a 1280px window maximised on 1366px kept a 174px list and
cut every title. Now the docks take their share of the window's width, by
proportion, every time it changes size, until the author drags a separator;
from then on the widths are theirs, and both they and the window's size are
kept in the config (`Config.window`) for the next launch.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import QDockWidget, QStyle

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
INSPECTOR_WIDTHS = (380, 480)


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
        )
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

    def _share_docks(self) -> None:
        stories, inspector = self.stories_dock, self.inspector_dock
        placed = (
            self.dockWidgetArea(stories) == Qt.LeftDockWidgetArea
            and self.dockWidgetArea(inspector) == Qt.RightDockWidgetArea
        )
        if not placed or stories.isFloating() or inspector.isFloating():
            return  # moved by the author: theirs to size
        if not (stories.isVisible() and inspector.isVisible()):
            return
        width = self.width()
        saved = self.config.window
        if self._docks_set_by_hand and saved and not self._docks_dragged:
            want_s, want_i = saved.stories_width, saved.inspector_width
        else:
            want_s = _clamp(width * STORIES_SHARE, STORIES_WIDTHS)
            want_i = _clamp(width * INSPECTOR_SHARE, INSPECTOR_WIDTHS)
        centre = self.centralWidget()
        separator = self.style().pixelMetric(QStyle.PM_DockWidgetSeparatorExtent, None, self)
        widths = share_width(
            width,
            max(centre.minimumWidth(), centre.minimumSizeHint().width()),
            2 * separator,
            (want_s, stories.minimumSizeHint().width()),
            (want_i, inspector.minimumSizeHint().width()),
        )
        self.resizeDocks([stories, inspector], list(widths), Qt.Horizontal)
