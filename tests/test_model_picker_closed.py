"""A model picker closed by a double-click: nothing of it is left taking the mouse."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow
from shiboken6 import isValid

from sealedlore.engine.catalog import ModelInfo
from sealedlore.gui.model_picker import ModelCatalog, ModelPickerDialog, pick_model
from tests.test_ui_tidy import settle


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _Moves(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.moves = 0

    def eventFilter(self, _obj, event) -> bool:  # noqa: N802 - Qt naming
        if event.type() == QEvent.MouseMove:
            self.moves += 1
        return False


def _send(window, kind, pos, button=Qt.NoButton, buttons=Qt.NoButton) -> None:
    point = QPointF(pos)
    QApplication.sendEvent(
        window,
        QMouseEvent(
            kind,
            point,
            point,
            QPointF(window.mapToGlobal(pos)),
            button,
            buttons,
            Qt.NoModifier,
        ),
    )


def test_a_model_chosen_by_double_click_leaves_nothing_taking_the_mouse(app):
    """The press closes the picker, and where the release never comes (the
    window has gone: Wayland) Qt keeps sending the mouse to the widget pressed.
    Moving over the story brought up the closed list's tooltips, and the story
    got no mouse at all, until the next click."""
    catalog = ModelCatalog()
    catalog.models = {
        f"m/{n}": ModelInfo(id=f"m/{n}", name=f"{n}", description=f"Tip {n}") for n in range(30)
    }
    catalog._endpoint = "e"
    catalog._fetched_at = datetime.now(UTC)
    main = QMainWindow()
    chat = QLabel("the story")
    chat.setMouseTracking(True)
    main.setCentralWidget(chat)
    main.resize(900, 700)
    main.show()
    settle(app)
    held: list[ModelPickerDialog] = []

    def double_click() -> None:
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, ModelPickerDialog)
        held.append(dialog)
        table = dialog.table
        at = table.viewport().mapTo(dialog, table.visualItemRect(table.topLevelItem(5)).center())
        handle = dialog.windowHandle()
        _send(handle, QEvent.MouseButtonPress, at, Qt.LeftButton, Qt.LeftButton)
        _send(handle, QEvent.MouseButtonRelease, at, Qt.LeftButton)
        _send(handle, QEvent.MouseButtonPress, at, Qt.LeftButton, Qt.LeftButton)
        _send(handle, QEvent.MouseButtonDblClick, at, Qt.LeftButton, Qt.LeftButton)

    QTimer.singleShot(50, double_click)
    chosen = pick_model(catalog, lambda: (None, "e", False), "", main)
    assert chosen is not None
    # The window's event loop deletes it as soon as this call returns to it.
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert not isValid(held[0]), "a picker closed is deleted, not left hidden"
    spy = _Moves()
    chat.installEventFilter(spy)
    for n in range(5):
        _send(main.windowHandle(), QEvent.MouseMove, QPoint(300 + n * 10, 200 + n * 10))
    assert spy.moves == 5, "the story has the mouse"
    main.close()
