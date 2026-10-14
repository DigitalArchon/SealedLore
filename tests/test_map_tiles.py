"""The map's ground, drawn by the program (gui/map_tiles.py). Offscreen."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.map_tiles import (  # noqa: E402
    LOOKS,
    TILE,
    building_boxes,
    person_sprite,
    render_tiles,
)

FARM = [
    "..........=.........",
    "....###+###=........",
    "....#_____#.=.......",
    "....W__1__#..=......",
    "....##=####...,,....",
    "...........TT..~~~..",
    "..%%%%.....TT..~~~..",
]


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_the_same_rows_draw_the_same_picture(app):
    first = render_tiles(FARM, "snow", roofs=True)
    again = render_tiles(FARM, "snow", roofs=True)
    assert first == again
    assert (first.width(), first.height()) == (20 * TILE, 7 * TILE)


def test_a_building_is_found_even_with_a_path_through_its_wall(app):
    assert building_boxes(FARM) == [(4, 10, 1, 4)]  # the path in at the bottom


def test_a_building_from_outside_is_roofed_and_from_inside_shows_its_floor(app):
    outside = render_tiles(FARM, "snow", roofs=True)
    inside = render_tiles(FARM, "snow", roofs=False)
    middle = (7 * TILE + TILE // 2, 2 * TILE + TILE // 2)
    assert outside.pixelColor(*middle) != inside.pixelColor(*middle)


def test_every_look_draws(app):
    for look in LOOKS:
        assert not render_tiles(FARM, look, roofs=True).isNull()


def test_a_person_keeps_their_look_and_others_differ(app):
    assert person_sprite("Jane") == person_sprite("Jane")
    assert person_sprite("Jane") != person_sprite("Marcus Webb")
