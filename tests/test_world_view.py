"""The map in the window (gui/world_view.py, gui/window_world.py,
gui/map_art.py). Offscreen."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QGraphicsSimpleTextItem  # noqa: E402

from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.map_art import picture_sprite, pixel_art, tile_image  # noqa: E402
from sealedlore.gui.world_view import (  # noqa: E402
    MIN_PLACE_PX,
    WorldView,
    place_card,
    shown_plans,
    visible_names,
)
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


def test_the_region_shows_where_the_story_has_been_and_who_is_there(app):
    view = WorldView()
    view.resize(900, 700)
    art = WorldArt(
        sprites=[WorldSprite(subject="place", subject_id="farm", name="Farmhouse", svg=BARN_SVG)]
    )
    view.set_world(town(), art, "Jane", keep_view=False)
    view.go_region()
    assert view.level() == "region" and view.region_button.isChecked()
    items = {item.place.name: item for item in view._items}
    assert set(items) == {"Hellsville", "Diner", "Farmhouse"}
    assert items["Diner"].current and items["Diner"].people == ["Jane", "Marcus Webb"]
    assert items["Farmhouse"].pixmap is not None and items["Hellsville"].pixmap is None
    # A place's picture scales with the map, never below MIN_PLACE_PX.
    assert items["Farmhouse"].pixels == MIN_PLACE_PX  # 40 m at eight km across
    assert items["Hellsville"].pixels > MIN_PLACE_PX  # a town is bigger
    card = place_card(view.world, view.world.places["diner"])
    assert "Here: Jane, Marcus Webb" in card and "Cave map (folded)" in card


def test_a_place_inside_another_shows_once_zoomed_in_far_enough(app):
    view = WorldView()
    view.resize(900, 700)
    world = town()
    world.where = {"Jane": "farm"}  # the diner isn't where the story is now
    view.set_world(world, WorldArt(), "Jane", keep_view=False)
    view.go_region()
    assert "Diner" not in visible_names(view)  # fitted to eight km: the town is a dot
    view.view.resetTransform()
    view.view.zoom_by(0.2)
    assert "Diner" in visible_names(view)


def test_the_world_level_names_the_big_places_large(app):
    view = WorldView()
    view.resize(900, 700)
    world = town()
    world.where = {"Jane": "farm"}
    view.set_world(world, WorldArt(), "Jane", keep_view=False)
    view.go_world()
    assert view.level() == "world" and view.world_button.isChecked()
    assert visible_names(view) == ["Farmhouse", "Hellsville"]  # the farmhouse: where you are
    assert next(i for i in view._items if i.place.id == "town").big
    view.go_region()
    assert view.level() == "region" and not next(i for i in view._items if i.place.id == "town").big


def test_the_local_map_is_the_area_s_plan_with_the_building_s_inside_at_closer_zoom(app):
    view = WorldView()
    view.resize(900, 700)
    farm = WorldPlace(id="farm", name="The farm", kind="site", x=0.0, y=0.0, size=400)
    house = WorldPlace(
        id="house", name="The farmhouse", kind="building", inside="farm", x=0.0, y=50.0
    )
    world = WorldMap(
        places={"farm": farm, "house": house}, where={"Jane": "house"}, visited={"Jane": ["house"]}
    )
    farm_plan = PlaceLayout(
        place_id="farm",
        rows=["." * 40] * 6
        + ["." * 18 + "#####" + "." * 17]
        + ["." * 18 + "#___#" + "." * 17]
        + ["." * 18 + "##+##" + "." * 17]
        + ["." * 40] * 15,
        labels={"The farmhouse": [20, 7]},
        spec={"ground": "snow"},
    )
    inside = PlaceLayout(
        place_id="house",
        rows=["#" * 40] + ["#" + "_" * 38 + "#"] * 22 + ["#" * 40],
        objects={"1": "the stove"},
        spec={"ground": "floor"},
    )
    art = WorldArt(layouts={"farm": {"": farm_plan}, "house": {"": inside}})
    view.set_world(world, art, "Jane", keep_view=False)
    # Opens on the local map: the farm's plan, the farmhouse roofed, Jane at its door.
    assert view.level() == "local" and view.local_button.isChecked()
    assert shown_plans(view) == ["The farm"] and view.title.text() == "The farm"
    assert visible_names(view) == []  # the plan carries them
    jane = next(p for p in view.people if p.name == "Jane")
    assert jane.held and jane.parentItem() is view._plans["farm"]
    assert jane.pos().y() > 8 * 16  # at the front of the roofed farmhouse
    # Zoomed in on the farmhouse, its inside takes the roof's place.
    view.go_to("house")
    assert shown_plans(view) == ["The farm", "The farmhouse"]
    assert view._plans["farm"].cleared  # the roof is off where the inside shows
    assert view.title.text() == "The farmhouse" and "1. the stove" in view.side.text()
    # See inside shows every inside at any local zoom.
    view.go_local()
    assert shown_plans(view) == ["The farm"]
    view.inside_button.setChecked(True)
    assert shown_plans(view) == ["The farm", "The farmhouse"]
    # Out to the region: the plans give way to the farm's picture.
    view.go_region()
    assert shown_plans(view) == [] and "The farm" in visible_names(view)


def test_a_building_the_story_changed_shows_its_new_plan_open_on_the_local_map(app):
    """Live, a barn blown up and burning stayed an intact roof on the farm's
    plan. Its plan drawn in that state now takes the roof's place at the
    local zoom, over the box the farm's plan drew for it (5 tiles wide,
    though the program now asks for 4), and whoever is at the barn stands at
    its front, not inside the fire."""
    view = WorldView()
    view.resize(900, 700)
    farm = WorldPlace(id="farm", name="The farm", kind="site", x=0.0, y=0.0, size=400)
    barn = WorldPlace(
        id="barn",
        name="The barn",
        kind="building",
        inside="farm",
        x=0.0,
        y=50.0,
        state="blown up and burning",
    )
    world = WorldMap(
        places={"farm": farm, "barn": barn}, where={"Jane": "barn"}, visited={"Jane": ["farm"]}
    )
    farm_plan = PlaceLayout(
        place_id="farm",
        rows=["." * 40] * 6
        + ["." * 18 + "#####" + "." * 17]
        + ["." * 18 + "#___#" + "." * 17]
        + ["." * 18 + "##+##" + "." * 17]
        + ["." * 40] * 15,
        labels={"The barn": [20, 7]},
        spec={"ground": "snow"},
    )
    intact = PlaceLayout(
        place_id="barn", rows=["#" * 40] + ["#" + "_" * 38 + "#"] * 22 + ["#" * 40]
    )
    ruin = PlaceLayout(
        place_id="barn",
        state="blown up and burning",
        rows=[":" * 40] * 10 + ["::***" + ":" * 35] * 4 + [":" * 40] * 10,
        labels={"Barn": [20, 12]},
    )
    # Its plan for an earlier state only: still the roof.
    view.set_world(world, WorldArt(layouts={"farm": {"": farm_plan}, "barn": {"": intact}}), "Jane")
    assert shown_plans(view) == ["The farm"]
    # Drawn in its new state: open at the local zoom, no roof left over it.
    art = WorldArt(layouts={"farm": {"": farm_plan}, "barn": {"": intact, barn.state: ruin}})
    view.set_world(world, art, "Jane", keep_view=False)
    assert view.level() == "local"
    assert shown_plans(view) == ["The barn", "The farm"]
    farm_item, barn_item = view._plans["farm"], view._plans["barn"]
    assert farm_item.cleared == ((18, 22, 6, 8),)  # the box as drawn, all of it
    assert barn_item.rect().width() == pytest.approx(farm_item.box_rect((18, 22, 6, 8)).width())
    assert barn_item.zValue() > farm_item.zValue()
    # Jane stands at its front in the farm's plan, and nobody inside the ruin.
    jane = next(p for p in view.people if p.name == "Jane")
    assert jane.parentItem() is farm_item and jane.pos().y() > 9 * 16
    assert [p.name for p in view.people] == ["Jane"]
    # The ruin's own plan doesn't name itself again over the farm's label.
    tags = [t.text() for t in barn_item.childItems() if isinstance(t, QGraphicsSimpleTextItem)]
    assert "Barn" not in tags


def test_with_no_local_plan_the_region_shows_and_says_why(app):
    view = WorldView()
    view.resize(800, 600)
    view.set_world(town(), WorldArt(), "Jane", keep_view=False)
    assert view.level() == "region" and "isn't drawn yet" in view.hint.text()
    view.drawing = True
    view.go_local()
    assert "being drawn" in view.hint.text()
    view.drawing = False
    view.art_on = False
    view.go_local()
    assert "Draw the map is off" in view.hint.text()


def test_the_local_map_opens_on_its_plan_the_moment_it_is_drawn(app):
    """Opened before any art (the region shows), the plan arriving in the
    background opens the local map on it, unless the author moved the map."""
    view = WorldView()
    view.resize(800, 600)
    farm = WorldPlace(id="farm", name="The farm", kind="site", x=0.0, y=0.0, size=400)
    town_ = WorldPlace(id="town", name="Hellsville", kind="settlement", x=0.0, y=8000.0, size=1500)
    world = WorldMap(places={"farm": farm, "town": town_}, where={"Jane": "farm"}, seen=["town"])
    view.set_world(world, WorldArt(), "Jane", keep_view=False)
    assert view.level() == "region"
    plan = PlaceLayout(place_id="farm", rows=["." * 40] * 24, spec={"ground": "snow"})
    view.set_world(world, WorldArt(layouts={"farm": {"": plan}}), "Jane", keep_view=True)
    assert view.level() == "local" and shown_plans(view) == ["The farm"]
    view.view.zoom_by(0.5)  # the author's own zoom is kept through the next redraw
    before = view.view.transform().m11()
    view.set_world(world, WorldArt(layouts={"farm": {"": plan}}), "Jane", keep_view=True)
    assert view.view.transform().m11() == pytest.approx(before)


def test_the_levels_follow_the_zoom_in_order(app):
    """Zooming out from the local map passes region then world, each at a
    fixed zoom, and each level's button gives its level."""
    view = WorldView()
    view.resize(800, 600)
    farm = WorldPlace(id="farm", name="The farm", kind="site", x=0.0, y=0.0, size=400)
    town_ = WorldPlace(id="town", name="Hellsville", kind="settlement", x=0.0, y=8000.0, size=1500)
    world = WorldMap(places={"farm": farm, "town": town_}, where={"Jane": "farm"}, seen=["town"])
    plan = PlaceLayout(place_id="farm", rows=["." * 40] * 24, spec={"ground": "snow"})
    view.set_world(world, WorldArt(layouts={"farm": {"": plan}}), "Jane", keep_view=False)
    levels = []
    for _ in range(40):
        levels.append(view.level())
        view.view.zoom_by(0.8)
    order = [level for i, level in enumerate(levels) if i == 0 or level != levels[i - 1]]
    assert order == ["local", "region", "world"]
    for go, level in (
        (view.go_local, "local"),
        (view.go_region, "region"),
        (view.go_world, "world"),
    ):
        go()
        assert view.level() == level


def test_a_kept_view_stays_through_a_redraw(app):
    view = WorldView()
    view.resize(900, 700)
    view.set_world(town(), WorldArt(), "Jane", keep_view=False)
    view.view.zoom_by(3)
    before = view.view.transform().m11()
    view.set_world(town(), WorldArt(), "Jane", keep_view=True)
    assert view.view.transform().m11() == pytest.approx(before)


def test_a_map_picture_s_file_is_read_once_and_the_cut_out_kept(app):
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QColor, QImage

    image = QImage(64, 64, QImage.Format_ARGB32)
    image.fill(QColor("white"))
    for x in range(20, 44):
        for y in range(20, 44):
            image.setPixelColor(x, y, QColor("#b3384a"))
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    data = bytes(buffer.data())
    reads = []

    def read() -> bytes:
        reads.append(1)
        return data

    first = picture_sprite("picture-1", 26, read)
    again = picture_sprite("picture-1", 26, read)
    assert first is not None and first.height() == 26 and again is first
    assert len(reads) == 1  # the cut-out is cached, never the file
    assert first.pixelColor(0, 0).alpha() in (0, 255)  # a hard edge


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
    view.go_region()
    farm = next(item for item in view._items if item.place.id == "farm")
    farm.set_pixels(48)
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
    # The held character (Serrik) has no place yet: the map opens on the region.
    assert window.world_view.region_button.isChecked()
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
    # Esc goes out from the local map to the region first, then to the page beside the map.
    view = window.world_view
    view.go_local()
    if view.level() == "local":
        window._on_escape()
        assert window.world_shown() and view.level() != "local"
    window._on_escape()
    assert not window.world_shown()


def test_the_map_is_as_it_was_left_after_another_page(window):
    view = window.world_view
    view.view.zoom_by(3)
    before = view.view.transform().m11()
    window.right_tabs.setCurrentWidget(window.scene_panel)
    window.right_tabs.setCurrentWidget(view)
    assert view.view.transform().m11() == pytest.approx(before)


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
        "." * 40,
        "..." + "#####" + "." * 32,
        "..." + "#___#" + "." * 32,
        "..." + "#___#" + "." * 32,
        "..." + "##+##" + "." * 32,
    ] + ["." * 40] * 19
    plan = PlaceLayout(
        place_id="farm", rows=rows, labels={"barn": [5, 2]}, spec={"ground": "grass"}
    )
    land = TerrainPatch(place_id="farm", x=0, y=0, side=2000, rows=["." * 4] * 4, ground="snow")
    view = WorldView()
    view.resize(800, 600)
    view.set_world(
        world, WorldArt(layouts={"farm": {"": plan}}, terrain=[land]), "Jane", keep_view=False
    )
    assert view.level() == "local" and view.local_button.isChecked()
    assert view._ground_for(farm, "grass") == "snow"  # the plan follows the land under it
    people = {p.name: p for p in view.people}
    assert set(people) == {"Jane", "Ben"} and people["Jane"].held
    # Roofed over, the barn's people stand at its front, below its south wall.
    assert people["Jane"].pos().y() > 4 * 16
    view.inside_button.setChecked(True)
    people = {p.name: p for p in view.people}
    assert people["Jane"].pos().y() < 4 * 16  # inside, where the plan marks the barn
    view.region_button.click()
    assert view.level() == "region" and not view.inside_button.isVisible()


def test_a_place_s_postcard_shows_on_its_card(app):
    from sealedlore.models.world import MapPicture
    from tests.test_world_pictures import png

    world = town()
    plan = PlaceLayout(place_id="diner", rows=["#####", "#___#", "##+##"], spec={})
    art = WorldArt(
        layouts={"diner": {"": plan}},
        pictures=[
            MapPicture(
                subject="postcard", subject_id="diner", name="Diner", file="images/map/p.png"
            )
        ],
    )
    view = WorldView()
    view.resize(800, 600)
    view.picture_reader = lambda path: png() if path == "images/map/p.png" else None
    view.set_world(world, art, "Jane", keep_view=False)
    view.go_to("diner")
    assert view.title.text() == "Diner" and not view.postcard_button.isHidden()
    view.go_region()
    assert view.postcard_button.isHidden() and view.card.isHidden()  # no card off the local map
