"""Edit Mode on the map (the author, Oct 18 2026): a map the story grows opens
in Edit Mode, where walls, doors, floors and the rest are changed by hand and
buildings added or taken off, and Edit Mode is there whenever a story keeps a
map, "so they don't always have to ask as the Director and hope it interprets
the request correctly". Mocks only."""

from __future__ import annotations

from sealedlore.engine.map_edit import (
    EDIT_TILES,
    apply_tile_edits,
    building_at,
    walled_box,
)
from sealedlore.models.world import TileEdits
from tests.test_area import make_session, turn
from tests.test_objects import _begun


def test_the_author_s_tiles_are_laid_over_the_area_and_what_they_make_is_worked_out(
    tmp_path, story, cast
):
    session = make_session(tmp_path, story, cast, ["{}"])
    _begun(session)
    house = session.area("house")
    door = next(e for e in house.exits if e[1] == 23)
    # A door painted over: no way out there any more; one painted elsewhere is.
    wall_x = next(x for x in range(1, house.width - 1) if house.rows[23][x] == "#")
    assert session.edit_tiles("house", {door: "#", (wall_x, 23): "+"})
    edited = session.area("house")
    assert door not in edited.exits and (wall_x, 23) in edited.exits
    assert edited.rows[23][wall_x] == "+" and edited.rows[door[1]][door[0]] == "#"
    assert session.edit_undo == "Tiles painted"
    # Kept with the map's art, and laid only on an area of the size they were made on.
    edits = session.area_edits("house")
    assert edits is not None and tuple(edits.size) == (house.width, house.height)
    assert apply_tile_edits(house, TileEdits(size=(1, 1), tiles=edits.tiles)) is house
    # Painted back to what the plan has: no edit kept there.
    assert session.edit_tiles("house", {door: house.at(*door)})
    assert "{},{}".format(*door) not in session.area_edits("house").tiles
    # Undo puts the last back, then the one before.
    assert session.undo_edit()
    assert session.area("house").rows[door[1]][door[0]] == "#"
    assert session.undo_edit()
    assert session.area("house").rows == house.rows
    assert session.area_edits("house") is None and not session.undo_edit()


def test_furniture_under_a_new_wall_goes_and_rooms_follow_the_walls(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast, ["{}"])
    _begun(session)
    house = session.area("house")
    rooms_before = len(house.rooms)
    thing = next(p for p in house.placements)
    session.edit_tiles("house", {(thing.x, thing.y): "#"})
    edited = session.area("house")
    assert thing.id not in {p.id for p in edited.placements}
    # A wall across the biggest room splits it in two.
    big = max(house.rooms, key=lambda r: r.cells)
    x0, y0, x1, y1 = big.box
    mid = (x0 + x1) // 2
    # Up to the plan's north edge: its way out is a gap there.
    session.edit_tiles("house", {(mid, y): "#" for y in range(0, y1 + 1)})
    split = session.area("house")
    assert all(split.rows[y][mid] == "#" for y in range(0, y1 + 1))
    row = (y0 + y1) // 2
    left, right = split.room_at(mid - 1, row), split.room_at(mid + 1, row)
    assert left is not None and right is not None and left.index != right.index
    assert house.room_at(mid - 1, row).index == house.room_at(mid + 1, row).index
    assert len(split.rooms) >= rooms_before


def test_a_room_drawn_corner_to_corner_has_a_door_onto_open_floor(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast, ["{}"])
    _begun(session)
    house = session.area("house")
    big = max(house.rooms, key=lambda r: r.cells)
    x0, y0, x1, y1 = big.box
    tiles = walled_box(house, x0 + 1, y0 + 1, x0 + 5, y0 + 4)
    assert sum(1 for ch in tiles.values() if ch == "#") == 13
    assert list(tiles.values()).count("+") == 1
    door = next(t for t, ch in tiles.items() if ch == "+")
    assert door[1] == y0 + 4, "south first, onto the open floor"
    assert walled_box(house, 3, 3, 4, 4) == {}, "too small to be a room"
    fence = walled_box(house, x0 + 1, y0 + 1, x0 + 5, y0 + 4, wall="%")
    assert "+" not in fence.values(), "a fence's way in is a gap"


def test_buildings_are_added_and_taken_off_and_undo_puts_them_back(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast, ["{}"])
    _begun(session)
    farm = session.area("farm")
    house_box = farm.buildings["house"]
    # The map's own building, taken off: off the map, its tiles open ground.
    assert session.remove_building("farm", house_box[0] + 1, house_box[1] + 1) == "The house"
    assert "house" not in session.world().places
    cleared = session.area("farm")
    assert "house" not in cleared.buildings
    assert all(
        cleared.rows[y][x] == "."
        for y in range(house_box[1], house_box[3] + 1)
        for x in range(house_box[0], house_box[2] + 1)
    )
    assert session.edit_undo == "The house taken off"
    assert session.undo_edit()
    assert "house" in session.world().places and "house" in session.area("farm").buildings
    # A new one, where the author drew it: on the map, in the farm, boxed on the plan.
    x0, y0 = 4, 4
    place_id = session.add_building("farm", x0, y0, x0 + 6, y0 + 5, "The smithy", "a forge")
    assert place_id is not None
    place = session.world().places[place_id]
    assert place.name == "The smithy" and place.kind == "building" and place.inside == "farm"
    assert place_id in session.world().seen
    with_smithy = session.area("farm")
    assert place_id in with_smithy.buildings
    box = with_smithy.buildings[place_id]
    assert abs(box[0] - x0) <= 6 and abs(box[1] - y0) <= 6
    assert session.edit_undo == "The smithy added"
    assert session.undo_edit() and place_id not in session.world().places


def test_a_building_only_the_plan_drew_is_found_by_its_walls():
    from sealedlore.engine.area import Area
    from sealedlore.engine.map_edit import cleared

    rows = [
        "..........",
        ".#####....",
        ".#   W....",
        ".#   #....",
        ".##+##....",
        "..........",
    ]
    area = Area(
        place_id="p",
        name="P",
        indoor=False,
        look="grass",
        rows=rows,
        tile_m=1.5,
        objects={},
        labels={},
    )
    place_id, tiles = building_at(area, 3, 4)
    assert place_id is None and len(tiles) == 20 and (3, 2) in tiles
    assert building_at(area, 8, 1) == (None, set()), "open ground is no building"
    assert set(cleared(area, tiles).values()) == {"."}


def test_a_place_added_on_the_region_lands_in_what_holds_it(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast, ["{}"])
    _begun(session)
    inside_farm = session.add_place("The well", "site", 30.0, -20.0, 6.0)
    assert session.world().places[inside_farm].inside == "farm"
    far = session.add_place("Ravens' Rock", "site", 30000.0, 0.0, 200.0)
    assert session.world().places[far].inside is None
    assert session.add_place("Nowhere", "castle", 0.0, 0.0, 10.0) is None, "a kind it offers"


def test_a_grown_map_opens_in_edit_mode_once_and_a_plot_file_s_does_not(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast, ["{}", "Snow.", "{}"])
    assert not session.map_setup_due(), "nothing to edit before the opening"
    _begun(session)
    # make_session's map comes from a plot file (world_base): drawn beforehand.
    assert not session.map_setup_due()
    session.story.world_base = None
    assert session.map_setup_due()
    session.map_edit_offered()
    assert not session.map_setup_due() and session.story.objects_offered
    session.story.map_edit_offered = False
    list(session.send(turn("I look out.")))
    assert not session.map_setup_due(), "not once the author has played"


def test_the_palette_paints_only_what_a_plan_can_hold():
    assert "&" not in EDIT_TILES, "stairs join levels, which painting can't make"
    assert {"#", "+", "_", "W", "."} <= set(EDIT_TILES)


def test_a_plan_drawn_again_drops_the_author_s_tiles(tmp_path, story, cast):
    from sealedlore.engine.world_art import adopt
    from sealedlore.models.world import PlaceLayout

    session = make_session(tmp_path, story, cast, ["{}"])
    _begun(session)
    session.edit_tiles("house", {(2, 2): "#"})
    art = session.world_art
    old = art.layouts["house"][""]
    adopt(
        art, PlaceLayout(place_id="house", spec=old.spec, rows=old.rows, made_at="2"), redraw=True
    )
    assert "house" not in art.tile_edits


# --- in the window -------------------------------------------------------------------

import tests.test_area_view as area_tests  # noqa: E402
from tests.test_area_view import _send, pump, stand  # noqa: E402

app = area_tests.app
window = area_tests.window


def _stroke(view, tiles):
    """A press on the first tile, a drag over the rest, a release on the last."""
    from PySide6.QtCore import QPointF

    from sealedlore.gui.area_render import px_for

    px = px_for(view.area)
    points = [QPointF((x + 0.5) * px, (y + 0.5) * px) for x, y in tiles]
    view._stroke("press", points[0])
    for point in points[1:]:
        view._stroke("move", point)
    view._stroke("release", points[-1])


def _editing(app, window, where):
    window.show()
    _send(app, window, "I look around.", ["Quiet.", "{}"])
    stand(window, {"Serrik Vaun": where})
    window.area_view.edit_button.click()
    pump(app)
    view = window.area_view
    assert window._map_editing and view.editing
    assert not view.edit_bar.isHidden() and view.travel_button.isHidden()
    return view


def test_edit_mode_paints_draws_a_room_and_undoes_in_the_window(app, window):
    view = _editing(app, window, ("house", 26, 5))
    house = view.area
    # Paint: a run of doors along the bottom wall.
    bottom = [(x, 23) for x in range(24, 28) if house.rows[23][x] == "#"]
    view.edit_bar.tile.setCurrentIndex(view.edit_bar.tile.findData("+"))
    view.edit_bar.set_tool("paint")
    _stroke(view, bottom)
    pump(app)
    painted = window.session.area("house")
    assert all(painted.rows[y][x] == "+" for x, y in bottom)
    assert view.edit_bar.undo_button.isEnabled()
    # Room: walls from corner to corner, with a door.
    view.edit_bar.set_tool("room")
    _stroke(view, [(23, 4), (27, 8)])
    pump(app)
    room = window.session.area("house")
    assert room.rows[4][23:28] == "#####" or "+" in room.rows[4][23:28]
    # Undo puts the room back, then the doors.
    view.edit_bar.undo_button.click()
    pump(app)
    assert window.session.area("house").rows == painted.rows
    # The ground clicked with Move walks nobody while the map is edited.
    view.edit_bar.set_tool("move")
    from PySide6.QtCore import QPoint, QPointF

    from sealedlore.gui.area_render import px_for

    px = px_for(view.area)
    view._clicked(QPointF(20.5 * px, 10.5 * px), QPoint())
    assert not view.walking()
    view.edit_bar.done_button.click()
    pump(app)
    assert not window._map_editing and not view.editing and not view.travel_button.isHidden()


def test_edit_mode_adds_takes_off_and_goes_inside_buildings(app, window, monkeypatch):
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtWidgets import QMessageBox

    from sealedlore.gui.area_render import px_for

    view = _editing(app, window, ("farm", 5, 18))
    farm = view.area
    assert not farm.indoor and view.edit_bar.buttons["building"].isEnabled()
    monkeypatch.setattr(window, "_ask_building", lambda: ("The smithy", "a forge"))
    view.edit_bar.set_tool("building")
    _stroke(view, [(3, 3), (9, 8)])
    pump(app)
    world = window.session.world()
    smithy = next(p for p in world.places.values() if p.name == "The smithy")
    assert smithy.inside == "farm" and smithy.id in view.area.buildings
    # Taken off again, asked first.
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: QMessageBox.Yes)
    box = view.area.buildings[smithy.id]
    px = px_for(view.area)
    view.edit_bar.set_tool("remove")
    view._clicked(QPointF((box[0] + 1.5) * px, (box[1] + 1.5) * px), QPoint())
    pump(app)
    assert smithy.id not in window.session.world().places
    # Go inside: the house's inside, edited without walking there.
    house_box = view.area.buildings["house"]
    view.edit_bar.set_tool("inside")
    view._clicked(QPointF((house_box[0] + 1.5) * px, (house_box[1] + 1.5) * px), QPoint())
    pump(app)
    assert window._edit_area_id == "house" and view.area.id == "house"
    assert view.area.indoor and not view.edit_bar.buttons["building"].isEnabled()
    assert not view.edit_bar.back_button.isHidden()
    me = window.session.area_state().spots["Serrik Vaun"]
    assert me.area == "farm", "nobody walked"
    view.edit_bar.back_button.click()
    pump(app)
    assert view.area.id == "farm" and window._edit_area_id is None


def test_go_outside_and_back_in_from_where_you_stand(app, window):
    from PySide6.QtCore import QPoint, QPointF

    from sealedlore.gui.area_render import px_for

    view = _editing(app, window, ("house", 26, 5))
    assert "The farm" in view.edit_bar.outside_button.text()
    view.edit_bar.outside_button.click()
    pump(app)
    assert view.area.id == "farm" and window._edit_area_id == "farm"
    assert view.edit_bar.back_button.text() == "Back to The house"
    assert view.edit_bar.outside_button.isHidden(), "the ground has no outside"
    # Into the house again, which is where the author's character is: home.
    box = view.area.buildings["house"]
    px = px_for(view.area)
    view.edit_bar.set_tool("inside")
    view._clicked(QPointF((box[0] + 1.5) * px, (box[1] + 1.5) * px), QPoint())
    pump(app)
    assert view.area.id == "house" and window._edit_area_id is None
    assert view.edit_bar.back_button.isHidden()


def test_a_grown_map_opens_in_edit_mode_and_a_turn_leaves_it(app, window):
    window.show()
    _send(app, window, "I look around.", ["Quiet.", "{}"])
    session = window.session
    window.refresh_area()
    pump(app)
    assert not window._map_editing, "a plot file's map, and the author has played"
    # A map the story grows, before the author's first turn (when that is,
    # `map_setup_due`, is the engine test's): it opens by itself, once.
    session.map_setup_due = lambda: not session.story.map_edit_offered
    window.refresh_area()
    pump(app)
    assert window._map_editing and session.story.map_edit_offered
    assert "Edit Mode" in window.statusBar().currentMessage()
    window.area_view.edit_bar.done_button.click()
    pump(app)
    window.refresh_area()
    pump(app)
    assert not window._map_editing, "offered once"
    window.area_view.edit_button.click()
    pump(app)
    assert window._map_editing
    _send(app, window, "I wait.", ["Still quiet.", "{}"])
    assert not window._map_editing, "a turn sent: the map is played on again"
