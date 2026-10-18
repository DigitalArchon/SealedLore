"""Corrections on the map in the window, and panning with the middle button
(gui/area_view.py, gui/world_view.py, gui/window_corrections.py,
gui/map_pan.py). Offscreen."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QInputDialog  # noqa: E402

import tests.test_area_view as area_tests  # noqa: E402
from tests.test_area_view import _send, pump, stand  # noqa: E402

# The play view's fixtures, shared.
app = area_tests.app
window = area_tests.window


def mouse(view, kind, point: QPointF, button, buttons) -> None:
    event = QMouseEvent(
        kind, point, view.viewport().mapToGlobal(point), button, buttons, Qt.NoModifier
    )
    QApplication.sendEvent(view.viewport(), event)


def begun(app, window) -> None:
    """A turn sent, so the story has a message to keep corrections on."""
    _send(app, window, "I look around.", ["Quiet.", "{}"])


def test_the_middle_button_pans_the_play_view_and_the_region_map(app, window):
    window.show()
    view = window.area_view.view
    for _ in range(6):
        view.zoom_by(1.25)  # in close: there is room to pan
    pump(app)
    bars = (view.horizontalScrollBar(), view.verticalScrollBar())

    def panned(bar, by: int) -> int:
        return min(max(bar.value() + by, bar.minimum()), bar.maximum())

    for bar in bars:
        bar.setValue((bar.minimum() + bar.maximum()) // 2)  # room both ways
    expected = (panned(bars[0], 60), panned(bars[1], 50))
    start = QPointF(200, 200)
    mouse(view, QEvent.MouseButtonPress, start, Qt.MiddleButton, Qt.MiddleButton)
    mouse(view, QEvent.MouseMove, QPointF(140, 150), Qt.NoButton, Qt.MiddleButton)
    mouse(view, QEvent.MouseButtonRelease, QPointF(140, 150), Qt.MiddleButton, Qt.NoButton)
    assert (bars[0].value(), bars[1].value()) == expected
    assert expected != (bars[0].minimum(), bars[1].minimum())  # it moved
    assert window.area_view.walking() is False  # a pan is never a walk
    world = window.world_view
    world.go_region()
    pump(app)
    region = world.view
    region.zoom_by(8)
    pump(app)
    rbars = (region.horizontalScrollBar(), region.verticalScrollBar())
    for bar in rbars:
        bar.setValue((bar.minimum() + bar.maximum()) // 2)
    expected = (panned(rbars[0], 30), panned(rbars[1], -20))
    mouse(region, QEvent.MouseButtonPress, start, Qt.MiddleButton, Qt.MiddleButton)
    mouse(region, QEvent.MouseMove, QPointF(170, 220), Qt.NoButton, Qt.MiddleButton)
    mouse(region, QEvent.MouseButtonRelease, QPointF(170, 220), Qt.MiddleButton, Qt.NoButton)
    assert (rbars[0].value(), rbars[1].value()) == expected


def test_dragging_someone_to_another_tile_moves_them_and_undo_puts_them_back(app, window):
    window.show()
    begun(app, window)
    # In the same room, in sight (someone out of sight is in the fog: not drawn).
    stand(window, {"Serrik Vaun": ("house", 26, 5), "Maela Orr": ("house", 26, 7)})
    pump(app)
    area_view = window.area_view
    view = area_view.view
    person = area_view.people["Maela Orr"]
    assert person.isVisible()
    at = view.mapFromScene(person.scenePos())
    start = QPointF(at.x(), at.y() - 4)
    px = view.transform().m11() * 40  # INDOOR_PX on screen
    end = QPointF(start.x() + px * 4, start.y())
    mouse(view, QEvent.MouseButtonPress, start, Qt.LeftButton, Qt.LeftButton)
    mouse(view, QEvent.MouseMove, end, Qt.NoButton, Qt.LeftButton)
    mouse(view, QEvent.MouseButtonRelease, end, Qt.LeftButton, Qt.NoButton)
    pump(app)
    spot = window.session.area_state().spots["Maela Orr"]
    assert spot.area == "house" and spot.x > 26
    assert area_view.undo_words == "Maela Orr moved"
    area_view.correction_requested.emit("undo", ())
    pump(app)
    assert window.session.area_state().spots["Maela Orr"].x == 26
    assert area_view.undo_words is None


def test_the_menus_offer_corrections_and_the_window_makes_them(app, window, monkeypatch):
    begun(app, window)
    stand(window, {"Serrik Vaun": ("house", 26, 5), "Maela Orr": ("house", 26, 7)})
    pump(app)
    area_view = window.area_view
    shown: list[list[str]] = []

    def capture(menu, _at):
        shown.append([a.text() for a in menu.actions()])
        menu.deleteLater()

    monkeypatch.setattr(area_view, "_show_menu", capture)
    maela = next(w for w in area_view.walkers if w.name == "Maela Orr")
    area_view._person_menu(maela, area_view.mapToGlobal(area_view.rect().center()))
    assert {"Move…", "Is really…", "Not here"} <= set(shown[-1])
    held = area_view.held()
    area_view._person_menu(held, area_view.mapToGlobal(area_view.rect().center()))
    assert "Move…" in shown[-1] and "Not here" not in shown[-1]  # never off the map
    # Not here: off the map and off the card.
    area_view.correction_requested.emit("not_here", ("Maela Orr",))
    pump(app)
    assert "Maela Orr" not in window.session.area_state().spots
    assert "char-maela" not in window.session.story.scene.present_character_ids
    assert not any(w.name == "Maela Orr" for w in area_view.walkers)
    # Put back by hand, with the dialog's choice.
    monkeypatch.setattr(QInputDialog, "getItem", lambda *_a, **_k: ("Maela Orr", True))
    area_view.correction_requested.emit("put_person", (30, 8))
    pump(app)
    assert window.session.area_state().spots["Maela Orr"].x == 30
    # A thing put down, named in the dialog, and shown.
    monkeypatch.setattr(QInputDialog, "getText", lambda *_a, **_k: ("a coil of rope", True))
    area_view.correction_requested.emit("put_thing", (31, 10))
    pump(app)
    assert any("rope" in p.name for p in area_view.area.placements)


def test_a_place_is_renamed_and_moved_from_the_region_map(app, window, monkeypatch):
    begun(app, window)
    world = window.world_view
    monkeypatch.setattr(QInputDialog, "getText", lambda *_a, **_k: ("The old farm", True))
    world.place_correction.emit("rename", "farm", ())
    pump(app)
    assert window.session.world().places["farm"].name == "The old farm"
    assert world.undo_words == "The farm renamed The old farm"
    world._start_moving_place("farm", "The old farm")
    assert "Click where The old farm stands" in world.hint.text()
    world._clicked_map(QPointF(50.0, -40.0))
    pump(app)
    farm = window.session.world().places["farm"]
    assert (farm.x, farm.y) == (50.0, 40.0)
    # Esc cancels a Move… that hasn't been placed.
    world._start_moving_place("farm", "The old farm")
    assert world.escape() and world._moving_place is None


def test_a_thing_moved_or_put_down_moves_its_sprite_and_paints_nothing(app, window, monkeypatch):
    """The author (Oct 18 2026): things drawn on their own layer, not painted
    into the area's picture. A thing put down or moved adds or moves its
    sprite at once; only a light moved paints the ground again."""
    from sealedlore.gui.area_render import px_for, thing_light

    window.show()
    begun(app, window)
    stand(window, {"Serrik Vaun": ("house", 26, 5)})
    window._join_paint_threads(10)
    pump(app)
    area_view = window.area_view
    px = px_for(area_view.area)
    painted: list[object] = []
    real = window._paint
    monkeypatch.setattr(window, "_paint", lambda *a: painted.append(a[1]) or real(*a))
    taken = {(s.x, s.y) for s in window.session.area_state().spots.values()}

    def free(*but: tuple[int, int]) -> tuple[int, int]:
        area = area_view.area
        return next(
            (x, y)
            for y in range(1, area.height - 1)
            for x in range(1, area.width - 1)
            if area.walkable(x, y)
            and area.placement_at(x, y) is None
            and (x, y) not in taken
            and (x, y) not in but
        )

    monkeypatch.setattr(QInputDialog, "getText", lambda *_a, **_k: ("a coil of rope", True))
    first = free()
    area_view.correction_requested.emit("put_thing", first)
    pump(app)
    rope = next(p for p in area_view.area.placements if "rope" in p.name)
    sprite = area_view.things[rope.id]
    assert (sprite.pos().x(), sprite.pos().y()) == ((first[0] + 0.5) * px, (first[1] + 1) * px)
    second = free(first)
    area_view.correction_requested.emit("move_thing", (rope.id, *second))
    pump(app)
    assert area_view.things[rope.id] is sprite  # the same sprite, moved
    # Dragged, it follows the pointer before it is let go.
    from PySide6.QtCore import QPointF as Point

    third = free(first, second)
    area_view._item_dragging(
        ("thing", rope.id, "rope"), Point((third[0] + 0.5) * px, (third[1] + 0.5) * px)
    )
    assert (sprite.pos().x(), sprite.pos().y()) == ((third[0] + 0.5) * px, (third[1] + 1) * px)
    area_view.correction_requested.emit("refresh", ())
    pump(app)
    assert (sprite.pos().x(), sprite.pos().y()) == ((second[0] + 0.5) * px, (second[1] + 1) * px)
    assert painted == []  # the ground stood as it was
    stove = next(p for p in area_view.area.placements if thing_light(p, px, indoor=True))
    area_view.correction_requested.emit("move_thing", (stove.id, *first))
    pump(app)
    assert painted  # its light moved with it: the ground is painted again
