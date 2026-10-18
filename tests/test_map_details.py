"""The map's drawing details (Oct 17 2026 playtest): things the set has no
picture of borrow its nearest one, roofs follow what a building is made of
(drawn on the fly when map pictures are on), snow is sized by the tile."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from sealedlore.engine.area import build_area
from sealedlore.engine.tile_draw import roof_jobs
from sealedlore.engine.tile_sets import TileSetInfo, closest_item, head_noun, roof_material
from sealedlore.gui.area_render import TileSource
from tests.test_area import world_and_art


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


STATION = [
    "a-bank-of-control-consoles",
    "a-metal-locker",
    "a-small-round-table",
    "a-storage-footlocker",
    "bunks",
    "cargo-crates",
    "reactor-conduit",
]


def test_a_thing_with_no_picture_borrows_the_set_s_nearest_of_its_kind():
    # Names a furnisher gave live, against the station set's things.
    assert closest_item("console bank", STATION) == "a-bank-of-control-consoles"
    assert closest_item("command console (dark)", STATION) == "a-bank-of-control-consoles"
    assert closest_item("reactor (cold)", STATION) == "reactor-conduit"
    assert closest_item("emergency gear locker", STATION) == "a-metal-locker"
    assert closest_item("a wall bunk bed", STATION) == "bunks"  # a word shared
    assert closest_item("galley table", STATION) == "a-small-round-table"
    assert closest_item("docked ship hull plate", STATION) is None  # nothing like it
    assert head_noun("a kitchen table with oil lamp") == "table"
    assert head_noun("medical bay - bed") == "bed"
    assert head_noun("Old Two-Storey Farmhouse") == head_noun("The farmhouse")


def test_the_renderer_draws_the_stand_in_and_keeps_a_box_for_what_has_none(app):
    sprite = QImage(16, 16, QImage.Format_ARGB32)
    sprite.fill(QColor("#7d4a8f"))
    from PySide6.QtCore import QBuffer, QIODevice

    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    sprite.save(buffer, "PNG")
    png = bytes(buffer.data())

    def read(role: str) -> bytes | None:
        return png if role == "items/a-metal-locker" else None

    read.items = lambda: list(STATION)  # type: ignore[attr-defined]
    source = TileSource(TileSetInfo(name="station"), read)
    assert source.item("emergency gear locker") is not None
    assert source.item("docked ship hull plate") is None


def test_a_building_s_roof_follows_its_words_and_is_drawn_on_the_fly_when_missing(app):
    world, art = world_and_art()
    world.places["house"] = world.places["house"].model_copy(
        update={"what": "an old cottage of grey fieldstone"}
    )
    farm = build_area(world, art, "farm")
    assert farm.roofs == {"house": "stone"}
    assert roof_material("a barn") == "" and roof_material("tin-roofed shack") == "metal"
    jobs = roof_jobs([farm, farm], lambda role: False, "a snowbound farm")
    assert [(j.key, j.subject, j.subject_id) for j in jobs] == [
        ("texture:out-roof-stone", "texture", "out-roof-stone")
    ]
    assert "stone roofing" in jobs[0].prompt and "snowbound farm" not in jobs[0].prompt
    assert roof_jobs([farm], lambda role: True, "x") == []  # the set or the story has one
    # Until it is drawn the set's roof stands in, tinted towards stone.
    roof = QImage(8, 8, QImage.Format_RGB32)
    roof.fill(QColor(150, 60, 40))  # a red shingle
    from PySide6.QtCore import QBuffer, QIODevice

    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    roof.save(buffer, "PNG")
    data = bytes(buffer.data())
    source = TileSource(TileSetInfo(name="farm"), lambda r: data if r == "out-roof" else None)
    plain = source.roof("", 24).textureImage().pixelColor(2, 2)
    stone = source.roof("stone", 24).textureImage().pixelColor(2, 2)
    assert plain.red() > plain.blue() + 60  # red shingle as drawn
    assert abs(stone.red() - stone.blue()) < 30  # greyed to stone


def test_snow_is_sized_by_the_tile_and_as_dense_on_a_large_area(app):
    from sealedlore.gui.area_view import WEATHER_PARTICLES, WEATHER_PARTICLES_MAX, _Weather

    farm = _Weather(QRectF(0, 0, 120 * 24, 72 * 24), 24)
    room = _Weather(QRectF(0, 0, 20 * 24, 12 * 24), 24)
    assert len(farm.seeds) == 120 * 72 // 6 <= WEATHER_PARTICLES_MAX
    assert len(room.seeds) == WEATHER_PARTICLES
    assert farm.tile == 24
