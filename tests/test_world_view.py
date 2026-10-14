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


def test_a_story_that_keeps_a_map_opens_on_it_with_the_room_it_needs(window):
    assert window.world_shown() and window.world_button.isChecked()
    assert window.stories_dock.isHidden() and not window.composer.isHidden()
    assert {i.place.name for i in window.world_view._items} == {"Hellsville", "Diner", "Farmhouse"}
    assert not window.stories_list_action.isChecked()  # View → Stories list brings it back
    window.show()
    window.resize(1366, 760)
    QApplication.processEvents()
    window._share_docks()
    QApplication.processEvents()
    assert abs(window.inspector_dock.width() - 683) <= 40  # half the window


def test_map_and_the_inspector_s_other_pages_and_branches(window):
    window.world_button.click()  # off: the page beside it
    assert not window.world_shown() and window.right_tabs.currentWidget() is window.scene_panel
    window.world_button.click()
    assert window.world_shown()
    # Branches take the story's place; the map stays beside it.
    window.show_map(True)
    assert window.map_shown() and window.world_shown()
    window.show_map(False)
    # Esc goes up from a place first, then to the page beside the map.
    window.world_view.open_place("diner")
    window._on_escape()
    assert window.world_shown() and window.world_view.place_id is None
    window._on_escape()
    assert not window.world_shown()


def test_turning_the_map_off_gives_the_room_back(window):
    window.session.story.defaults.world_map = False
    window._sync_world()
    assert window.world_button.isHidden() and not window.world_shown()
    assert not window.stories_dock.isHidden()
    assert not window.right_tabs.isTabVisible(window.right_tabs.indexOf(window.world_view))


def test_closing_the_story_brings_the_stories_back(window):
    window._close_story()
    assert not window.stories_dock.isHidden() and not window.map_layout


def test_art_coming_back_redraws_the_map(window):
    window.show_world(True)
    window._art_signal.drawn.emit()
    assert window._art_timer.isActive()


def test_under_fog_only_what_the_story_found_and_what_is_around_it_shows(app):
    farm = WorldPlace(id="farm", name="The farm", kind="site", x=0.0, y=0.0, size=400)
    house = WorldPlace(id="house", name="Farmhouse", kind="building", inside="farm", x=0.0, y=50.0)
    town = WorldPlace(id="town", name="Hellsville", kind="settlement", x=0.0, y=8000.0)
    diner = WorldPlace(id="diner", name="Diner", kind="building", inside="town", x=0.0, y=8000.0)
    hills = WorldPlace(id="hills", name="The hills", kind="area", x=4000.0, y=8000.0)
    world = WorldMap(
        places={p.id: p for p in (farm, house, town, diner, hills)},
        seen=["town"],
        visited={"Jane": ["farm"]},
        where={"Jane": "farm"},
    )
    view = WorldView()
    view.set_world(world, WorldArt(), "Jane", keep_view=False)
    shown = {item.place.name for item in view._items}
    assert shown == {"The farm", "Farmhouse", "Hellsville"}  # not the diner, not the hills


def test_the_map_opens_where_the_story_is_with_everyone_there_standing_in_it(app):
    farm = WorldPlace(id="farm", name="The farm", kind="site", x=0.0, y=0.0, size=400)
    barn = WorldPlace(id="barn", name="The barn", kind="building", inside="farm", x=50.0, y=0.0)
    world = WorldMap(
        places={"farm": farm, "barn": barn},
        where={"Jane": "barn", "Ben": "farm"},
        visited={"Jane": ["farm", "barn"]},
    )
    rows = [
        "............",
        "...#####....",
        "...#___#....",
        "...#___#....",
        "...##+##....",
        "............",
        "............",
    ]
    plan = PlaceLayout(
        place_id="farm", rows=rows, labels={"barn": [5, 2]}, spec={"ground": "grass"}
    )
    land = TerrainPatch(place_id="farm", x=0, y=0, side=2000, rows=["." * 4] * 4, ground="snow")
    view = WorldView()
    view.resize(800, 600)
    view.set_world(
        world, WorldArt(layouts={"farm": {"": plan}}, terrain=[land]), "Jane", keep_view=False
    )
    assert view.place_id == "farm" and view.here_button.isChecked()
    assert view._ground_for(farm, "grass") == "snow"  # the plan follows the land under it
    people = {p.name: p for p in view.people}
    assert set(people) == {"Jane", "Ben"} and people["Jane"].held
    # Roofed over, the barn's people stand at its front, below its south wall.
    assert people["Jane"].pos().y() > 4 * 16
    view.inside_button.setChecked(True)
    people = {p.name: p for p in view.people}
    assert people["Jane"].pos().y() < 4 * 16  # inside, where the plan marks the barn
    view.region_button.click()
    assert view.place_id is None and view.region_button.isChecked()


def test_a_place_s_postcard_shows_on_its_card(app):
    from sealedlore.models.world import MapPicture
    from tests.test_world_pictures import png

    world = town()
    art = WorldArt(
        pictures=[
            MapPicture(
                subject="postcard", subject_id="diner", name="Diner", file="images/map/p.png"
            )
        ]
    )
    view = WorldView()
    view.picture_reader = lambda path: png() if path == "images/map/p.png" else None
    view.set_world(world, art, "Jane", keep_view=False)
    view.open_place("diner")
    assert not view.postcard_button.isHidden()
    view.open_place("farm")
    assert view.postcard_button.isHidden()  # none drawn for the farmhouse
