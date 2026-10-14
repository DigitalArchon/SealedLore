"""The map in the window (gui/world_view.py, gui/window_world.py,
gui/map_art.py). Offscreen."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.map_art import pixel_art, tile_image  # noqa: E402
from sealedlore.gui.world_view import WorldView, place_card, visible_names  # noqa: E402
from sealedlore.models.node import NodeMeta  # noqa: E402
from sealedlore.models.world import (  # noqa: E402
    PlaceLayout,
    TerrainPatch,
    WorldArt,
    WorldDelta,
    WorldMap,
    WorldPlace,
    WorldSprite,
    WorldThing,
)
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from sealedlore.storage.world_art import save_world_art  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402

BARN_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect x="10" y="20" width="44" height="34" fill="#b3384a"/></svg>'
)


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def town() -> WorldMap:
    town = WorldPlace(id="town", name="Hellsville", kind="settlement", x=0.0, y=0.0, size=1500)
    diner = WorldPlace(
        id="diner", name="Diner", kind="building", inside="town", x=200.0, y=0.0, size=40
    )
    farm = WorldPlace(id="farm", name="Farmhouse", kind="building", x=0.0, y=-8000.0, size=40)
    return WorldMap(
        places={"town": town, "diner": diner, "farm": farm},
        where={"Jane": "diner", "Marcus Webb": "diner"},
        visited={"Jane": ["farm", "town", "diner"]},
        things={"map": WorldThing(id="map", name="Cave map", at="diner", state="folded")},
    )


def test_a_sprite_is_pixel_art_in_the_palette_and_tiles_are_ground():
    image = pixel_art(BARN_SVG)
    assert image is not None and image.width() == 32
    assert image.pixelColor(16, 20).name() == "#b3384a"
    assert image.pixelColor(0, 0).alpha() == 0  # the background stays clear
    ground = tile_image(("..~", "#.."), "snow")
    assert (
        ground.pixelColor(0, 0).name() == "#e9ecef" and ground.pixelColor(2, 0).name() == "#7fb3d5"
    )


def test_the_map_shows_where_the_story_has_been_and_who_is_there(app):
    view = WorldView()
    view.resize(900, 700)
    art = WorldArt(
        sprites=[WorldSprite(subject="place", subject_id="farm", name="Farmhouse", svg=BARN_SVG)]
    )
    view.set_world(town(), art, "Jane", keep_view=False)
    items = {item.place.name: item for item in view._items}
    assert set(items) == {"Hellsville", "Diner", "Farmhouse"}
    assert items["Diner"].current and items["Diner"].people == ["Jane", "Marcus Webb"]
    assert items["Farmhouse"].pixmap is not None and items["Hellsville"].pixmap is None
    card = place_card(view.world, view.world.places["diner"])
    assert "Here: Jane, Marcus Webb" in card and "Cave map (folded)" in card


def test_a_place_inside_another_shows_once_zoomed_in_far_enough(app):
    view = WorldView()
    view.resize(900, 700)
    world = town()
    world.where = {"Jane": "farm"}  # the diner isn't where the story is now
    view.set_world(world, WorldArt(), "Jane", keep_view=False)
    assert "Diner" not in visible_names(view)  # fitted to eight km: the town is a dot
    view.view.resetTransform()
    view.view.zoom_by(0.2)
    assert "Diner" in visible_names(view)


def test_a_place_opens_on_its_plan_and_goes_back_up(app):
    view = WorldView()
    view.set_world(town(), WorldArt(), "Jane", keep_view=False)
    view.open_place("diner")
    assert view.title.text() == "Diner" and "Not drawn yet" in view.hint.text()
    layout = PlaceLayout(
        place_id="diner",
        rows=["#####", "#_1_#", "#####"],
        objects={"1": "the counter"},
        labels={"Diner": [2, 1]},
        spec={"ground": "dirt"},
    )
    view.set_world(town(), WorldArt(layouts={"diner": {"": layout}}), "Jane")
    assert view.place_id == "diner"
    assert "1. the counter" in view.side.text() and "Here: Jane, Marcus Webb" in view.side.text()
    view.open_place(None)
    assert view.place_id is None and view.title.text() == "Map"


def test_a_place_changed_shows_its_changed_picture(app):
    world = town()
    world.places["farm"].state = "burnt down"
    art = WorldArt(
        sprites=[
            WorldSprite(subject="place", subject_id="farm", name="Farmhouse", svg=BARN_SVG),
            WorldSprite(
                subject="place",
                subject_id="farm",
                name="Farmhouse",
                state="burnt down",
                svg=BARN_SVG.replace("#b3384a", "#1b1d2b"),
            ),
        ]
    )
    view = WorldView()
    view.set_world(world, art, "Jane", keep_view=False)
    farm = next(item for item in view._items if item.place.id == "farm")
    assert farm.pixmap.toImage().pixelColor(20, 30).name() == "#1b1d2b"


@pytest.fixture
def window(app, tmp_path: Path, story, cast):
    nodes = make_exchange(2)
    nodes[-1].meta = NodeMeta(
        world=WorldDelta(places=list(town().places.values()), where={"Jane": "diner"})
    )
    story.defaults.world_map = True
    bundle = StoryBundle(story=story, cast=cast, nodes=nodes)
    bundle.story.active_leaf_id = nodes[-1].id
    save_story_bundle(bundle, root=tmp_path)
    patch = TerrainPatch(place_id="town", x=0, y=0, side=6000, rows=["T.." * 2] * 3, ground="snow")
    save_world_art(story.id, WorldArt(terrain=[patch]), tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.map_art = False  # nothing to draw with the mock
    window.resize(1400, 900)
    window.open_story(story.id)
    return window


def test_map_shows_for_a_story_that_keeps_one_beside_branches(window):
    assert window.branch_bar.map_button.text() == "Branches"
    assert window.world_button.text() == "Map" and not window.world_button.isHidden()
    window.world_button.click()
    assert window.world_shown() and window.composer.isHidden()
    assert {i.place.name for i in window.world_view._items} == {"Hellsville", "Diner", "Farmhouse"}
    # The branches take the same place: one at a time.
    window.show_map(True)
    assert window.map_shown() and not window.world_shown()
    assert not window.world_button.isChecked()
    window.show_map(False)
    window.show_world(True)
    # Esc goes up from a place first, then back to the story.
    window.world_view.open_place("diner")
    window._on_escape()
    assert window.world_shown() and window.world_view.place_id is None
    window._on_escape()
    assert not window.world_shown() and not window.composer.isHidden()


def test_turning_the_map_off_hides_it(window):
    window.show_world(True)
    window.session.story.defaults.world_map = False
    window._sync_world()
    assert window.world_button.isHidden() and not window.world_shown()


def test_art_coming_back_redraws_the_map(window):
    window.show_world(True)
    window._art_signal.drawn.emit()
    assert window._art_timer.isActive()
