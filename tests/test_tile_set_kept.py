"""A story keeps the tile set it began with (the author, Oct 17 2026:
"tilesets shouldn't swap mid game. A tileset should be chosen at the start
and used in that game, and new items generated when needed if a different
type of area is encountered"): the program's choice is written to the art
once the story has begun, and ground the set has none of is drawn for the
story in its style (engine/tile_sets.py, engine/tile_draw.py `ground_jobs`,
gui/area_render.py `TileSource.ground`)."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from sealedlore.engine.area import build_area
from sealedlore.engine.tile_draw import ground_jobs
from sealedlore.engine.tile_sets import (
    TileSetInfo,
    deck_floor_wanted,
    ground_wanted,
    set_grounds,
)
from sealedlore.gui.area_render import TileSource
from sealedlore.storage.tile_sets import shipped_sets
from tests.test_area import make_session, turn, world_and_art


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


SNOW = TileSetInfo(name="farm", words={"out-ground": "deep fresh snow with soft drifts"})
STATION_WORDS = "A trading station in deep space, its docking ring busy with starships. " * 3


def test_the_set_is_chosen_once_the_story_begins_and_kept_after(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast, ["He waits.", "{}", "Still.", "{}"])
    session.pin_tile_set()
    assert session.map_art_copy().tile_set == ""  # nothing yet: the story hasn't begun
    list(session.send(turn("I look around.")))
    art = session.map_art_copy()
    chosen = art.tile_set
    assert chosen and art.tile_set_auto
    # The world's words changing later (a lighthouse's clockwork, a station
    # found) no longer change the set it is drawn in.
    session.story.world_bible = STATION_WORDS
    assert session.tile_set_name() == chosen
    list(session.send(turn("I wait.")))
    assert session.map_art_copy().tile_set == chosen
    # The program chose it, so the offer of the others still shows.
    assert session.tile_set_offer() is not None
    # The author's choice is theirs, and the offer is answered by it.
    session.set_tile_set("brasshaven")
    art = session.map_art_copy()
    assert (art.tile_set, art.tile_set_auto) == ("brasshaven", False)
    assert session.tile_set_offer() is None
    # "Chosen for the setting": chosen now, from the setting, and kept.
    session.set_tile_set("")
    art = session.map_art_copy()
    assert (art.tile_set, art.tile_set_auto) == ("station", True)


def test_a_set_s_ground_is_its_listed_looks_or_what_its_words_name():
    assert set_grounds(SNOW) == {"snow"}
    assert set_grounds(TileSetInfo(name="x", grounds=["dirt", "rock"])) == {"dirt", "rock"}
    assert set_grounds(TileSetInfo(name="x")) == set()  # says nothing: stands for every look
    assert ground_wanted(SNOW, "sand") and not ground_wanted(SNOW, "snow")
    assert not ground_wanted(SNOW, "floor")  # not a ground look
    assert not ground_wanted(TileSetInfo(name="x"), "sand")
    assert deck_floor_wanted(SNOW, "space") and not deck_floor_wanted(SNOW, "metal")
    assert not deck_floor_wanted(TileSetInfo(name="s", deck=True), "space")


def test_every_shipped_set_lists_its_grounds_and_covers_its_sample_s_plans():
    sets = {name: info for name, (info, _folder) in shipped_sets().items()}
    assert sets and all(info.grounds for info in sets.values())
    # The samples' own plans are in their set's ground: nothing new is drawn
    # for a sample until the story goes somewhere it hasn't been.
    import json
    from pathlib import Path

    samples = Path(__file__).resolve().parent.parent / "SampleStories"
    for path in samples.glob("*.world_art.json"):
        art = json.loads(path.read_text())
        info = sets[art["tile_set"]]
        for states in art["layouts"].values():
            for layout in states.values():
                look = str((layout.get("spec") or {}).get("ground") or "")
                assert not ground_wanted(info, look), (path.name, look)


def test_ground_the_set_has_none_of_is_drawn_once_in_its_style():
    world, art = world_and_art()
    farm = build_area(world, art, "farm")
    assert farm.look == "grass" and not farm.indoor
    jobs = ground_jobs([farm, farm], SNOW, lambda role: False, "a snowbound farm", style="bleak")
    assert [(j.key, j.subject, j.subject_id) for j in jobs] == [
        ("texture:out-ground-grass", "texture", "out-ground-grass")
    ]
    assert "short grass" in jobs[0].prompt and "snowbound farm" in jobs[0].prompt
    assert "bleak" in jobs[0].prompt
    assert ground_jobs([farm], SNOW, lambda role: True, "x") == []  # already drawn
    grassy = TileSetInfo(name="g", grounds=["grass"])
    assert ground_jobs([farm], grassy, lambda role: False, "x") == []  # the set's own


def _png(colour: QColor) -> bytes:
    image = QImage(8, 8, QImage.Format_RGB32)
    image.fill(colour)
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def test_until_it_is_drawn_the_set_s_ground_tinted_stands_in(app):
    snow = _png(QColor(240, 242, 248))
    sand = _png(QColor(30, 200, 30))  # a drawn ground, coloured to tell apart
    source = TileSource(SNOW, lambda role: snow if role == "out-ground" else None)
    own = source.ground("snow", 24).textureImage().pixelColor(2, 2)
    tinted = source.ground("sand", 24).textureImage().pixelColor(2, 2)
    assert own.red() > 230 and own.blue() > 230  # the set's own snow
    assert tinted.blue() < tinted.red() - 40  # snow tinted towards sand
    drawn = TileSource(SNOW, lambda role: {"out-ground": snow, "out-ground-sand": sand}.get(role))
    colour = drawn.ground("sand", 24).textureImage().pixelColor(2, 2)
    assert colour.green() > 150 and colour.red() < 80  # the ground drawn for the story
