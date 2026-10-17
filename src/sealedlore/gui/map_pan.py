"""Panning a map with the middle mouse button held (the author, Oct 17 2026:
"middle click and move the mouse on the map to scroll around on it so they
don't need to use the scroll bars"). One helper for the play view and the
region map: Qt's own hand drag follows the left button only, and the play
view's middle press, which set it, panned nothing."""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QGraphicsView


class MiddlePan:
    """Give it a view's middle-button presses, moves and releases; each
    returns whether it took the event."""

    def __init__(self, view: QGraphicsView) -> None:
        self.view = view
        self.at: QPoint | None = None

    @property
    def panning(self) -> bool:
        return self.at is not None

    def press(self, event) -> bool:
        if event.button() != Qt.MiddleButton:
            return False
        self.at = event.position().toPoint()
        self.view.viewport().setCursor(Qt.ClosedHandCursor)
        event.accept()
        return True

    def move(self, event) -> bool:
        if self.at is None or not event.buttons() & Qt.MiddleButton:
            return False
        here = event.position().toPoint()
        delta = here - self.at
        self.at = here
        bars = (self.view.horizontalScrollBar(), self.view.verticalScrollBar())
        bars[0].setValue(bars[0].value() - delta.x())
        bars[1].setValue(bars[1].value() - delta.y())
        event.accept()
        return True

    def release(self, event) -> bool:
        if event.button() != Qt.MiddleButton or self.at is None:
            return False
        self.at = None
        self.view.viewport().unsetCursor()
        event.accept()
        return True
