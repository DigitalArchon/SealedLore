"""Map-first play in the window (gui/area_view.py, gui/window_area.py,
gui/area_render.py). Offscreen."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.engine.area import build_area  # noqa: E402
from sealedlore.engine.tile_sets import TileSetInfo  # noqa: E402
from sealedlore.gui.area_render import TileSource, cut_out, render_area  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.models.world import WorldDelta  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from sealedlore.storage.world_art import save_world_art  # noqa: E402
from tests.test_area import world_and_art  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def pump(app) -> None:
    for _ in range(5):
        app.processEvents()
        app.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.fixture
def window(app, tmp_path: Path, story, cast):
    world, art = world_and_art()
    story.defaults.world_map = True
    story.world_base = WorldDelta(places=list(world.places.values()), seen=list(world.seen))
    story.world_start = "house"
    story.held_character_id = "char-serrik"
    story.scene.present_character_ids = ["char-serrik", "char-maela"]
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    save_world_art(story.id, art, tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.map_art = False
    window.resize(1600, 900)
    window.open_story(story.id)
    pump(app)
    return window


def walk_all(app, window) -> None:
    view = window.area_view
    for _ in range(500):
        if not view.walking():
            break
        view._step()
    pump(app)


def test_a_map_story_opens_on_the_area_to_walk_on(window):
    assert window.world_shown() and window.world_view.play_shown()
    view = window.area_view
    assert view.area is not None and view.area.name == "The house"
    assert [w.name for w in view.walkers if w.held] == ["Serrik Vaun"]
    assert view.title.text() == "The house" and view.clock.text().startswith("Day 1")
    # Plan is the area's plan as the map drew it; Local is back to walking.
    window.world_view.go_plan()
    assert not window.world_view.play_shown()
    window.world_view.go_local()
    assert window.world_view.play_shown()


def test_walking_into_the_door_goes_outside_and_a_menu_choice_fills_the_composer(app, window):
    session = window.session
    house = session.area("house")
    door = next(e for e in house.exits if e[1] == 23)
    window._area_walk(*door, False)
    walk_all(app, window)
    me = session.area_state().spots["Serrik Vaun"]
    assert me.area == "farm"  # through the door
    assert window.area_view.area.name == "The farm"
    well = next(p for p in session.area("farm").placements if "well" in p.name)
    window._area_action("use", well.name, well.id)
    walk_all(app, window)
    assert window.composer.text() == "I use the stone well."
    me = session.area_state().spots["Serrik Vaun"]
    assert abs(me.x - well.x) <= 1 and abs(me.y - well.y) <= 1  # walked up to it first
    window._area_action("look", "Maela Orr", "")
    assert window.composer.text().startswith("I keep out of sight and watch Maela Orr")


def test_walking_waits_while_the_story_is_being_written(app, window):
    session = window.session
    before = session.area_state().spots["Serrik Vaun"]
    window._busy = True
    window._sync_area_lock()
    window._area_walk(before.x + 1, before.y + 1, False)
    walk_all(app, window)
    assert session.area_state().spots["Serrik Vaun"] == before
    assert "being written" in window.area_view.hint.text()


def test_an_area_is_drawn_from_its_tile_set_and_a_missing_sprite_is_still_seen(app):
    world, art = world_and_art()
    farm = build_area(world, art, "farm")
    house = build_area(world, art, "house")
    plain = TileSource(TileSetInfo(name="plain"), lambda role: None)
    for area in (farm, house):
        image = render_area(area, plain)
        assert not image.isNull()
        assert image.width() == area.width * (40 if area.indoor else 16)
    # A sprite drawn on white comes out cut out of it.
    picture = QImage(64, 64, QImage.Format_RGB32)
    picture.fill(QColor("white"))
    for x in range(20, 40):
        for y in range(10, 50):
            picture.setPixelColor(x, y, QColor("#7d4a8f"))
    cut = cut_out(picture)
    assert cut is not None and (cut.width(), cut.height()) == (20, 40)
    assert (
        cut.pixelColor(0, 0).alpha() == 255
        and cut_out(QImage(8, 8, QImage.Format_RGB32)) is not None
    )
