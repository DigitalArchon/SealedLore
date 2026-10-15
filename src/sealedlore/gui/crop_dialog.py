"""Cropping a picture of one's own as it comes in (the author, Oct 2026): any
part of it, or the exact shape the app uses for a portrait, a lore picture,
the background or an isometric view (engine/picture_shapes.py).

The frame is kept in the picture's own pixels. The geometry is plain
functions on (x, y, width, height) tuples, so it is tested without a mouse;
the view only maps the screen to the picture and back. What comes out goes
through `picture_bytes_of`, so a crop is stripped and scaled like any
picture added from disk.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable, Sequence
from pathlib import Path

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPaintEvent, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sealedlore.engine.picture_shapes import FREE, ORIGINAL, PictureShape, picture_shapes
from sealedlore.gui.ref_images import _encoded, load_image, picture_bytes, picture_bytes_of
from sealedlore.models.config import Config

Rect = tuple[float, float, float, float]

# The smallest crop, in the picture's pixels: smaller is a mistake, not a picture.
MIN_SIDE = 16
# How near a corner or edge the pointer must be to take it, in screen pixels.
GRAB = 10
# Below this on its longer side a crop is said to be small: image models
# draw at 1024 and more, and a small reference is soft beside them.
SMALL_SIDE = 512

# The window's config, for the shapes its models draw (`use_config`). Held
# weakly: a dialog must never keep a closed window alive.
_config_source: Callable[[], Config | None] | None = None


def use_config(owner: object, read: Callable[[object], Config | None]) -> None:
    """Let crops anywhere in the app use `owner`'s config (the main window's)."""
    global _config_source
    ref = weakref.ref(owner)

    def source() -> Config | None:
        alive = ref()
        return read(alive) if alive is not None else None

    _config_source = source


def shapes(background: tuple[int, int] | None = None) -> list[PictureShape]:
    config = _config_source() if _config_source is not None else None
    return picture_shapes(config, background=background)


# --- geometry, in the picture's pixels --------------------------------------------


def fit_rect(width: float, height: float, ratio: float | None) -> Rect:
    """The largest frame of `ratio` centred in the picture; the whole picture
    when free."""
    if not ratio:
        return (0.0, 0.0, float(width), float(height))
    w = min(float(width), height * ratio)
    h = w / ratio
    return ((width - w) / 2, (height - h) / 2, w, h)


def moved(rect: Rect, dx: float, dy: float, width: float, height: float) -> Rect:
    x, y, w, h = rect
    return (min(max(x + dx, 0.0), width - w), min(max(y + dy, 0.0), height - h), w, h)


def from_corner(
    anchor: tuple[float, float],
    point: tuple[float, float],
    ratio: float | None,
    width: float,
    height: float,
) -> Rect:
    """The frame from a fixed corner to the pointer, kept in the picture and
    to `ratio` when there is one. Used to drag a corner and to draw anew."""
    ax, ay = anchor
    dx, dy = point[0] - ax, point[1] - ay
    room_w = (width - ax) if dx >= 0 else ax
    room_h = (height - ay) if dy >= 0 else ay
    if ratio:
        # The shape follows whichever way the pointer has gone further.
        w = min(max(abs(dx), abs(dy) * ratio), room_w, room_h * ratio)
        w = max(w, min(MIN_SIDE * max(ratio, 1.0), room_w, room_h * ratio))
        h = w / ratio
    else:
        w = max(min(abs(dx), room_w), min(MIN_SIDE, room_w))
        h = max(min(abs(dy), room_h), min(MIN_SIDE, room_h))
    x = ax if dx >= 0 else ax - w
    y = ay if dy >= 0 else ay - h
    return (x, y, w, h)


def from_edge(
    rect: Rect, edge: str, point: tuple[float, float], width: float, height: float
) -> Rect:
    """One side moved to the pointer (a free crop only), the frame kept at
    least MIN_SIDE and inside the picture."""
    x, y, w, h = rect
    px = min(max(point[0], 0.0), width)
    py = min(max(point[1], 0.0), height)
    if edge == "left":
        right = x + w
        x = min(px, right - MIN_SIDE)
        w = right - x
    elif edge == "right":
        w = max(px - x, MIN_SIDE)
    elif edge == "top":
        bottom = y + h
        y = min(py, bottom - MIN_SIDE)
        h = bottom - y
    elif edge == "bottom":
        h = max(py - y, MIN_SIDE)
    return (x, y, w, h)


def whole_pixels(rect: Rect, width: int, height: int) -> QRect:
    """The frame in whole pixels, inside the picture."""
    x, y, w, h = rect
    left, top = max(0, round(x)), max(0, round(y))
    right, bottom = min(width, round(x + w)), min(height, round(y + h))
    return QRect(left, top, max(1, right - left), max(1, bottom - top))


# --- the view ---------------------------------------------------------------------


class CropView(QWidget):
    """The picture, fitted, with the frame over it: drag inside to move it,
    a corner (or, free, a side) to resize it, outside it to draw a new one."""

    changed = Signal()

    def __init__(self, image: QImage, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image = image
        self._pixmap = QPixmap.fromImage(image)
        self._scaled: QPixmap | None = None
        self._ratio: float | None = None
        self._rect: Rect = fit_rect(image.width(), image.height(), None)
        self._drag: tuple[str, object] | None = None
        self.setMouseTracking(True)
        self.setMinimumSize(480, 320)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # The frame ----------------------------------------------------------------

    def set_ratio(self, ratio: float | None) -> None:
        self._ratio = ratio
        self._rect = fit_rect(self._image.width(), self._image.height(), ratio)
        self.update()
        self.changed.emit()

    def set_rect(self, rect: Rect) -> None:
        self._rect = rect
        self.update()
        self.changed.emit()

    def rect_in_picture(self) -> Rect:
        return self._rect

    def crop(self) -> QRect:
        return whole_pixels(self._rect, self._image.width(), self._image.height())

    # Screen and picture --------------------------------------------------------

    def _placement(self) -> tuple[float, float, float]:
        """(scale, left, top) of the fitted picture in the widget."""
        iw, ih = max(self._image.width(), 1), max(self._image.height(), 1)
        scale = min(self.width() / iw, self.height() / ih)
        return scale, (self.width() - iw * scale) / 2, (self.height() - ih * scale) / 2

    def _to_picture(self, point: QPointF) -> tuple[float, float]:
        scale, left, top = self._placement()
        return ((point.x() - left) / scale, (point.y() - top) / scale)

    def _on_screen(self, rect: Rect) -> QRectF:
        scale, left, top = self._placement()
        x, y, w, h = rect
        return QRectF(left + x * scale, top + y * scale, w * scale, h * scale)

    def _hit(self, point: QPointF) -> tuple[str, object] | None:
        frame = self._on_screen(self._rect)
        corners = {
            "top-left": (frame.topLeft(), frame.bottomRight()),
            "top-right": (frame.topRight(), frame.bottomLeft()),
            "bottom-left": (frame.bottomLeft(), frame.topRight()),
            "bottom-right": (frame.bottomRight(), frame.topLeft()),
        }
        for _name, (corner, opposite) in corners.items():
            if abs(point.x() - corner.x()) <= GRAB and abs(point.y() - corner.y()) <= GRAB:
                return ("corner", self._to_picture(opposite))
        if self._ratio is None:
            near_x = frame.left() - GRAB <= point.x() <= frame.right() + GRAB
            near_y = frame.top() - GRAB <= point.y() <= frame.bottom() + GRAB
            for edge, distance, near in (
                ("left", abs(point.x() - frame.left()), near_y),
                ("right", abs(point.x() - frame.right()), near_y),
                ("top", abs(point.y() - frame.top()), near_x),
                ("bottom", abs(point.y() - frame.bottom()), near_x),
            ):
                if near and distance <= GRAB:
                    return ("edge", edge)
        if frame.contains(point):
            return ("move", self._to_picture(point))
        return None

    # Mouse ----------------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt's name
        if event.button() != Qt.LeftButton:
            return
        point = event.position()
        hit = self._hit(point)
        if hit is None:
            # Outside the frame: a new one, drawn from here.
            iw, ih = self._image.width(), self._image.height()
            px, py = self._to_picture(point)
            hit = ("corner", (min(max(px, 0.0), iw), min(max(py, 0.0), ih)))
        self._drag = hit

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt's name
        point = event.position()
        if self._drag is None:
            hit = self._hit(point)
            kind = hit[0] if hit else None
            cursor = {
                "corner": Qt.SizeAllCursor,
                "edge": Qt.SizeHorCursor
                if hit and hit[1] in ("left", "right")
                else Qt.SizeVerCursor,
                "move": Qt.OpenHandCursor,
            }.get(kind, Qt.CrossCursor)
            self.setCursor(cursor)
            return
        iw, ih = float(self._image.width()), float(self._image.height())
        here = self._to_picture(point)
        kind, data = self._drag
        if kind == "move":
            last = data
            self._rect = moved(self._rect, here[0] - last[0], here[1] - last[1], iw, ih)
            self._drag = ("move", here)
        elif kind == "corner":
            self._rect = from_corner(data, here, self._ratio, iw, ih)
        elif kind == "edge":
            self._rect = from_edge(self._rect, str(data), here, iw, ih)
        self.update()
        self.changed.emit()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt's name
        self._drag = None

    # Painting --------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt's name
        painter = QPainter(self)
        scale, left, top = self._placement()
        size = QSize(round(self._image.width() * scale), round(self._image.height() * scale))
        if self._scaled is None or self._scaled.size() != size:
            self._scaled = self._pixmap.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        painter.drawPixmap(round(left), round(top), self._scaled)
        frame = self._on_screen(self._rect)
        shown = QRectF(left, top, size.width(), size.height())
        shade = QColor(0, 0, 0, 150)
        # Everything left out, dimmed.
        painter.fillRect(
            QRectF(shown.left(), shown.top(), shown.width(), frame.top() - shown.top()), shade
        )
        painter.fillRect(
            QRectF(shown.left(), frame.bottom(), shown.width(), shown.bottom() - frame.bottom()),
            shade,
        )
        painter.fillRect(
            QRectF(shown.left(), frame.top(), frame.left() - shown.left(), frame.height()), shade
        )
        painter.fillRect(
            QRectF(frame.right(), frame.top(), shown.right() - frame.right(), frame.height()), shade
        )
        # Thirds, faint, and the frame.
        painter.setPen(QPen(QColor(255, 255, 255, 70), 1))
        for i in (1, 2):
            x = frame.left() + frame.width() * i / 3
            y = frame.top() + frame.height() * i / 3
            painter.drawLine(QPointF(x, frame.top()), QPointF(x, frame.bottom()))
            painter.drawLine(QPointF(frame.left(), y), QPointF(frame.right(), y))
        painter.setPen(QPen(QColor(255, 255, 255), 2))
        painter.drawRect(frame)
        painter.setBrush(QColor(255, 255, 255))
        for corner in (frame.topLeft(), frame.topRight(), frame.bottomLeft(), frame.bottomRight()):
            painter.drawRect(QRectF(corner.x() - 5, corner.y() - 5, 10, 10))
        painter.end()


# --- the dialog -------------------------------------------------------------------


class CropDialog(QDialog):
    """Crop a picture as it comes in, or take it whole."""

    def __init__(
        self,
        image: QImage,
        shapes: Sequence[PictureShape],
        initial: str = FREE,
        parent: QWidget | None = None,
        *,
        counter: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Crop the picture" + (f" ({counter})" if counter else ""))
        self.whole = False
        self._image = image
        self._shapes = list(shapes)
        hint = QLabel(
            "Drag the frame to move it and its corners to resize it, or drag across the "
            "picture to draw a new one. Only what is inside the frame is kept."
        )
        hint.setWordWrap(True)
        self.shape = QComboBox()
        for shape in self._shapes:
            self.shape.addItem(shape.label, shape.key)
        self.view = CropView(image)
        self.size_label = QLabel()
        self.size_label.setObjectName("hintLabel")
        row = QHBoxLayout()
        row.addWidget(QLabel("Shape"))
        row.addWidget(self.shape, 1)
        buttons = QDialogButtonBox()
        self.crop_button = buttons.addButton("Crop", QDialogButtonBox.AcceptRole)
        self.whole_button: QPushButton = buttons.addButton(
            "Use the whole picture", QDialogButtonBox.ActionRole
        )
        buttons.addButton(QDialogButtonBox.Cancel)
        self.crop_button.setDefault(True)
        self.crop_button.clicked.connect(self.accept)
        self.whole_button.clicked.connect(self._take_whole)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addLayout(row)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.size_label)
        layout.addWidget(buttons)
        self.shape.currentIndexChanged.connect(self._on_shape)
        self.view.changed.connect(self._show_size)
        index = self.shape.findData(initial)
        self.shape.setCurrentIndex(max(index, 0))
        self._on_shape()
        self.resize(960, 760)

    def shape_key(self) -> str:
        return str(self.shape.currentData())

    def _ratio(self, key: str) -> float | None:
        if key == ORIGINAL:
            return self._image.width() / max(self._image.height(), 1)
        shape = next((s for s in self._shapes if s.key == key), None)
        return shape.ratio if shape is not None else None

    def _on_shape(self) -> None:
        self.view.set_ratio(self._ratio(self.shape_key()))

    def _show_size(self) -> None:
        crop = self.view.crop()
        text = f"Keeps {crop.width()} × {crop.height()} pixels of {self._image.width()} × "
        text += f"{self._image.height()}."
        if max(crop.width(), crop.height()) < SMALL_SIDE:
            text += " That is small: it may look soft beside pictures the app draws."
        self.size_label.setText(text)

    def _take_whole(self) -> None:
        self.whole = True
        self.accept()

    def cropped(self) -> QImage:
        return self._image.copy(self.view.crop())


def cropped_picture(
    parent: QWidget | None,
    source: Path,
    initial: str = FREE,
    *,
    background: tuple[int, int] | None = None,
    counter: str = "",
) -> tuple[bytes, str] | None:
    """A picture file, cropped as the author chooses, ready to keep (hidden
    data removed, scaled if large), with its extension; None when they
    cancel. Raises ValueError when the file isn't a picture the app reads."""
    image = load_image(source)
    if image.isNull():
        raise ValueError(f"{source.name} isn't a picture this app can read")
    dialog = CropDialog(image, shapes(background), initial, parent, counter=counter)
    if dialog.exec() != QDialog.Accepted:
        return None
    if dialog.whole or dialog.view.crop() == QRect(0, 0, image.width(), image.height()):
        return picture_bytes(source)  # as it was, nothing re-encoded
    part = dialog.cropped()
    if part.hasAlphaChannel():
        return picture_bytes_of(_encoded(part, "PNG"), ".png")
    return picture_bytes_of(_encoded(part, "JPEG", 92), ".jpg")
