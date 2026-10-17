"""The paintings on the map (Paintings on the map): each local area's own
painting, laid over the area it shows on the 2D map (gui/world_view.py),
and what can be done for the area in view.

The author (Oct 15 2026): the 2D map is the source of truth; an isometric
view matches it closely, not exactly, and says so. Each area has its own
view, the map marks the areas that have one before Paintings is pressed,
and a view lies over the map where its area is, so it is plain what it
shows and where that is. A view is never painted on its own: the author
presses Paint it (with its price), and again once the map there has
changed (a view painted under another state says it is out of date), or
brings in a picture of their own.

This module holds the bar over the map while the views are shown, and the
view seen whole (`IsometricDialog`); the map draws the views themselves.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from sealedlore.gui.wrap_row import WrapRow

ZOOM_STEP = 1.25
TRUTH_NOTE = (
    "Painted from the map by {model}. The 2D map is the story's truth: this painting matches it "
    "closely, not exactly."
)
OWN_NOTE = (
    "A picture of your own. The 2D map is the story's truth: the storyteller never sees "
    "this painting."
)
NO_VIEW_NOTE = (
    "A painting is made from this area's map by an image model when you press Paint it, or is "
    "a picture of your own; it lies over the map where the area is."
)
# How much of the map shows through the views, at most: some of the view
# always shows, or the slider would hide what it is for.
MIN_OPACITY = 20
# A view's thumbnail in the chooser: the plan's 5:3, big enough to tell
# half a dozen takes apart.
CHOICE_THUMB = (240, 144)


class _PictureView(QGraphicsView):
    """The painting, fitted to the view; the wheel zooms, a drag moves it."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("isometricView")
        self.setScene(QGraphicsScene(self))
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self._item: QGraphicsPixmapItem | None = None
        self._fitted = True

    def show_image(self, image: QImage | None) -> None:
        self.scene().clear()
        self._item = None
        if image is None or image.isNull():
            return
        self._item = self.scene().addPixmap(QPixmap.fromImage(image))
        self.scene().setSceneRect(self._item.boundingRect())
        self._fitted = True
        self._fit()

    def _fit(self) -> None:
        if self._item is not None:
            self.fitInView(self._item, Qt.KeepAspectRatio)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt's name
        step = ZOOM_STEP if event.angleDelta().y() > 0 else 1 / ZOOM_STEP
        self._fitted = False
        self.scale(step, step)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt's name
        super().resizeEvent(event)
        if self._fitted:
            self._fit()


def view_note(painted_by: str) -> str:
    """What a view is: painted from the map by a model, or the author's own."""
    return TRUTH_NOTE.format(model=painted_by) if painted_by else OWN_NOTE


class IsometricDialog(QDialog):
    """A view seen whole: the wheel zooms, a drag moves it."""

    def __init__(self, place: str, image: QImage, painted_by: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{place}: a painting")
        self.picture = _PictureView()
        note = QLabel(view_note(painted_by))
        note.setObjectName("hintLabel")
        note.setWordWrap(True)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addWidget(note, 1)
        row.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(self.picture, 1)
        layout.addLayout(row)
        self.resize(1100, 720)
        self.picture.show_image(image)


def made_when(made_at: str) -> str:
    """When a picture was made, in local time ("15 Oct 2026, 04:12"), or
    the record's own words when they aren't a time."""
    try:
        moment = datetime.fromisoformat(made_at.replace("Z", "+00:00"))
    except ValueError:
        return made_at
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    return f"{moment.day} {moment.strftime('%b %Y, %H:%M')}"


class ViewChoice:
    """One of an area's views as the chooser lists it."""

    def __init__(
        self,
        picture_id: str,
        thumbnail: QPixmap | None,
        *,
        painted_by: str,
        made_at: str,
        current: bool,
        shown: bool,
    ) -> None:
        self.picture_id = picture_id
        self.thumbnail = thumbnail
        self.painted_by = painted_by
        self.made_at = made_at
        self.current = current
        self.shown = shown

    def label(self) -> str:
        by = f"Painted by {self.painted_by}" if self.painted_by else "A picture of your own"
        when = made_when(self.made_at) if self.made_at else ""
        state = "of the map as it is now" if self.current else "out of date"
        line = ", ".join(part for part in (when, state) if part)
        return by + (f"\n{line}" if line else "") + ("\n✓ On the map now" if self.shown else "")


class ChooseViewDialog(QDialog):
    """An area's views, newest first, to choose the one laid over the map
    (the author, Oct 15 2026: with half a dozen painted, swap between them).
    `chosen` is the picture chosen, or None."""

    def __init__(self, place: str, choices: list[ViewChoice], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{place}: choose the painting to show")
        self.chosen: str | None = None
        # A list, the thumbnail beside its words: laid out as icons, the
        # third line of a label was cut to "…".
        self.list = QListWidget()
        self.list.setIconSize(QSize(*CHOICE_THUMB))
        self.list.setSpacing(4)
        for choice in choices:
            item = QListWidgetItem(choice.label())
            item.setData(Qt.UserRole, choice.picture_id)
            if choice.thumbnail is not None:
                item.setIcon(QIcon(choice.thumbnail))
            item.setToolTip(choice.label().replace("\n", " · "))
            self.list.addItem(item)
            if choice.shown:
                self.list.setCurrentItem(item)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        self.list.itemActivated.connect(lambda _item: self.accept())
        note = QLabel(
            "Every painting made or brought in is kept; the one chosen lies over the map "
            "for the map as it is there now, and Media lists them all."
        )
        note.setObjectName("hintLabel")
        note.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Show this painting")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.list, 1)
        layout.addWidget(note)
        layout.addWidget(buttons)
        self.resize(820, 560)

    def accept(self) -> None:
        item = self.list.currentItem()
        self.chosen = item.data(Qt.UserRole) if item is not None else None
        super().accept()


class IsometricBar(QWidget):
    """Over the map while the paintings are shown, one row where it
    fits: what the area in view has (its view, an out-of-date one, or none),
    the offer to paint it or bring a picture in, a choice among its views
    when it has several, its view whole, and how much of the map shows
    through the views. What the view is (`note`) goes in the map's hint
    line."""

    paint_requested = Signal()
    # The author's own picture as the view (a file chosen and cropped).
    own_requested = Signal()
    # Choose among the area's views.
    choose_requested = Signal()
    whole_requested = Signal()
    # 0-1: how opaque the views are drawn over the map.
    opacity_changed = Signal(float)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("isometricBar")
        self.offer = QLabel()
        # What the view is, for the map's hint line ("" with no view).
        self.note = ""
        self.paint_button = QPushButton("Paint it")
        self.paint_button.clicked.connect(self.paint_requested.emit)
        self.own_button = QPushButton("Use a picture of your own…")
        self.own_button.setToolTip(
            "Bring in a picture from disk as this area's painting, cropped to its shape"
        )
        self.own_button.clicked.connect(self.own_requested.emit)
        self.choose_button = QPushButton("Choose a painting…")
        self.choose_button.setToolTip(
            "This area has several paintings: choose the one to lay over the map (Media lists "
            "them too, and can show one from there)"
        )
        self.choose_button.clicked.connect(self.choose_requested.emit)
        self.whole_button = QPushButton("See it whole")
        self.whole_button.setToolTip("This area's painting on its own, to zoom into")
        self.whole_button.clicked.connect(self.whole_requested.emit)
        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(MIN_OPACITY, 100)
        self.opacity.setValue(100)
        self.opacity.setFixedWidth(110)  # a group of its own width: the row wraps, not squeezes
        self.opacity.setToolTip("Fade the views to see the 2D map under them")
        self.opacity.valueChanged.connect(lambda value: self.opacity_changed.emit(value / 100))
        fade = QLabel("Fade")
        fade.setObjectName("hintLabel")
        fade.setToolTip(self.opacity.toolTip())
        row = WrapRow(spacing=6, group_spacing=10)
        row.add_group([self.offer])
        row.add_group([self.paint_button])
        row.add_group([self.own_button])
        row.add_group([self.choose_button])
        row.add_group([self.whole_button])
        row.add_group([fade, self.opacity])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(row)

    def show_state(
        self,
        *,
        place: str,
        has_view: bool,
        current: bool,
        painting: bool,
        model: str,
        price: float | None,
        problem: str | None,
        painted_by: str = "",
        views: int = 0,
    ) -> None:
        """The area in view (`place`): whether it `has_view`, `current` when
        that was painted for the map as it is now, the offer to paint it
        (`model`, `price`, or the `problem` that stops it), and how many
        `views` it has to choose among."""
        cost = f", ${price:.2f}" if price else ""
        several = f" (one of {views})" if views > 1 else ""
        if painting:
            said = f"Painting the view of {place}…"
        elif not has_view:
            said = f"{place} has no painting yet."
        elif not current:
            said = f"{place}: the map has changed since this view was painted{several}."
        else:
            said = f"{place}: its painting{several}."
        self.offer.setText(said)
        self.choose_button.setVisible(views > 1)
        self.choose_button.setEnabled(not painting)
        again = "Paint it again" if has_view else "Paint it"
        self.paint_button.setText(f"{again} ({model}{cost})" if model else again)
        self.paint_button.setVisible(not painting)
        self.paint_button.setEnabled(problem is None)
        self.paint_button.setToolTip(
            problem
            or "An image model paints this area from its map, now; the price is listed by the "
            "endpoint"
        )
        self.own_button.setEnabled(not painting)
        self.whole_button.setVisible(has_view)
        self.note = view_note(painted_by) if has_view else NO_VIEW_NOTE
        if problem and not painting:
            self.note = f"{problem} {self.note}"
