"""The program's isometric render of a plan (gui/map_iso.py): what the
image model is given to repaint as the isometric view."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.map_iso import FIRE, iso_image, iso_png, render_rows  # noqa: E402
from sealedlore.gui.map_tiles import LOOKS  # noqa: E402
from sealedlore.models.world import PlaceLayout, WorldArt, WorldMap, WorldPlace  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


ROWS = [
    "........................................",
    "...~~~..............#W+W#...............",
    "...~~~..............W___W...............",
    "...%%%..............#####...............",
    "......................=.................",
    "......................=.....1...........",
    "......................=.........3.......",
    "......................==================",
    "........................................",
    "....T.........#####.....................",
    "..............#___#.....................",
    "..............##+##.....................",
]


def _colours(image: QImage) -> set[tuple[int, int, int]]:
    seen = set()
    for y in range(0, image.height(), 2):
        for x in range(0, image.width(), 2):
            c = QColor(image.pixel(x, y))
            seen.add((c.red(), c.green(), c.blue()))
    return seen


def test_the_render_is_the_size_asked_and_draws_what_the_plan_has(app):
    look = LOOKS["snow"]
    image = render_rows(ROWS, look, {"1": "water tower", "3": "gas lamp"}, (640, 360))
    assert (image.width(), image.height()) == (640, 360)
    colours = _colours(image)
    roof = QColor(look.roof[1])
    assert (roof.red(), roof.green(), roof.blue()) in colours  # a roof on each building
    water = QColor(look.water[0])
    assert (water.red(), water.green(), water.blue()) in colours
    road = QColor(look.road[0])
    assert (road.red(), road.green(), road.blue()) in colours
    assert (0xFF, 0xD8, 0x6A) in colours  # the lamp's glow
    # Nothing of the fire palette on an unchanged plan.
    fire = {tuple(QColor(c).getRgb()[:3]) for c in FIRE}
    assert not fire & colours


def test_a_building_the_story_changed_is_shown_burning(app):
    farm = WorldPlace(id="farm", name="The farm", kind="site", x=0.0, y=0.0, size=300)
    barn = WorldPlace(
        id="barn", name="The barn", kind="building", inside="farm", x=0.0, y=50.0, state="burning"
    )
    world = WorldMap(places={"farm": farm, "barn": barn})
    plan = PlaceLayout(
        place_id="farm", rows=ROWS, spec={"ground": "snow"}, labels={"The barn": [21, 2]}
    )
    burning = PlaceLayout(place_id="barn", state="burning", rows=["#*#", "*_*", "#+#"])
    art = WorldArt(layouts={"farm": {"": plan}, "barn": {"burning": burning}})
    image = iso_image(world, art, "farm", (640, 360))
    assert image is not None
    fire = {tuple(QColor(c).getRgb()[:3]) for c in FIRE}
    assert fire & _colours(image)
    data = iso_png(world, art, "farm", (320, 180))
    assert data is not None and data[:8] == b"\x89PNG\r\n\x1a\n"
    assert iso_png(world, art, "nowhere", (320, 180)) is None
