"""Objects and people on the map, after the author's AfterDark export (Oct 18
2026): Suzuka, named in the opening with no card, had a stranger's avatar
and no way to get a card; she was stood inside the lone farmhouse she was
knocking at, then vanished; and "my usual armchair" made no armchair, so the
author asked for objects made by saying what they are, and placed or
removed by hand, mainly to set an area up at the start. Mocks only."""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from sealedlore.engine.objects import MAX_OBJECTS, parse_objects
from sealedlore.engine.session_objects import object_role
from sealedlore.engine.world_art import fixed_in_plan, outside_part
from sealedlore.models.world import WorldDelta, WorldPlace
from tests import test_area
from tests.test_area import make_session, turn

# --- what the author typed ------------------------------------------------------


def test_objects_are_read_one_per_comma_line_or_list_and():
    assert parse_objects("an armchair, a small side table and a rug") == [
        "armchair",
        "small side table",
        "rug",
    ]
    assert parse_objects("A lantern\nthe old trunk; Lantern") == ["lantern", "old trunk"]
    # What is with a thing stays with it.
    assert parse_objects("a table with a lamp and a vase") == ["table with a lamp and a vase"]
    assert parse_objects("  ,  ") == []
    assert len(parse_objects(", ".join(f"crate {n}" for n in range(30)))) == MAX_OBJECTS
    assert object_role("an armchair") == "items/armchair"


# --- someone outside a building that stands alone ---------------------------------


def _lone_house(monkeypatch, *, porch: bool = False, outside: bool = True):
    """The house with no farm round it: open country, as AfterDark's farmhouse."""
    plain = test_area.world_and_art

    def lone(house_objects=()):
        world, art = plain(house_objects)
        world.places["house"].inside = None
        world.places.pop("farm")
        art.layouts.pop("farm")
        if porch:
            world.places["porch"] = WorldPlace(
                id="porch", name="The porch overhang", kind="area", inside="house", x=20.0, y=30.0
            )
            # A plan drawn while the read had it inside labelled it there.
            art.layouts["house"][""].labels["The porch overhang"] = [20, 12]
        return world, art

    monkeypatch.setattr(test_area, "world_and_art", lone)
    return "porch" if porch else "house", outside


def _with_ruth(session, place: str, outside: bool) -> None:
    base = session.story.world_base
    base.where = {**base.where, "Ruth": place}
    if outside:
        base.outside = {**base.outside, "Ruth": True}


def test_someone_outside_a_building_alone_on_the_land_stands_at_its_door(
    tmp_path, story, cast, monkeypatch
):
    place, outside = _lone_house(monkeypatch)
    session = make_session(tmp_path, story, cast)
    _with_ruth(session, place, outside)
    state = session.area_state()
    me, ruth = state.spots["Serrik Vaun"], state.spots["Ruth"]
    assert me.area == "house"
    assert ruth.area.startswith("window:"), "on the land round the house, not in it"
    land = session.area(ruth.area)
    door = next(e for e in land.entrances if e.place_id == "house")
    assert abs(ruth.x - door.x) + abs(ruth.y - door.y) <= 3
    # Outside by the story's word: never moved in, though the house has a plan
    # (she waits only for a plan of the ground round it, which open country
    # has none of).
    session._set_live(state)
    again = session.area_state().spots["Ruth"]
    assert again.area == ruth.area and (again.x, again.y) == (ruth.x, ruth.y)
    # From inside, shown at the door, beyond its wall.
    at = session.at_the_door()
    assert [a[0] for a in at] == ["Ruth"]
    _name, bx, by, dx, dy = at[0]
    house = session.area("house")
    assert (dx, dy) in house.exits
    assert not (0 <= bx < house.width and 0 <= by < house.height)


def test_a_porch_the_read_put_inside_a_building_is_outside_it(tmp_path, story, cast, monkeypatch):
    place, _outside = _lone_house(monkeypatch, porch=True)
    session = make_session(tmp_path, story, cast)
    _with_ruth(session, place, outside=False)
    world = session.world()
    assert outside_part(world, world.places["porch"])
    # The house's own plan has no place for it (its label stood mid-house).
    assert all(
        f["name"] != "The porch overhang" for f in fixed_in_plan(world, world.places["house"])
    )
    assert "The porch overhang" not in session.area("house").labels
    ruth = session.area_state().spots["Ruth"]
    assert ruth.area.startswith("window:")


def test_a_room_s_name_alone_doesn_t_make_it_outside(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast)
    world = session.world()
    kitchen = WorldPlace(id="k", name="The kitchen", kind="room", inside="house")
    steps = WorldPlace(id="s", name="The Steps of Ash", kind="site")
    assert not outside_part(world, kitchen)
    assert not outside_part(world, steps), "only inside a building or room"


# --- a card for someone with none ----------------------------------------------------


def test_a_card_is_written_from_the_story_for_someone_with_none(tmp_path, story, cast):
    found = json.dumps(
        {"characters": [{"name": "Ruth", "description": "A drifter, scarred, half frozen."}]}
    )
    session = make_session(tmp_path, story, cast, ["Ruth knocked at the door.", "{}", found])
    list(session.send(turn("I listen.")))
    session.story.scene.present_others.append("Ruth")
    session.bundle.supporting.dismissed.append("ruth")
    assert not session.has_card("Ruth")
    card, filled = session.card_for("Ruth", to_cast=True)
    assert filled and card.summary == "A drifter, scarred, half frozen."
    assert card in session.cast and session.has_card("Ruth")
    assert "char-serrik" in session.story.scene.present_character_ids
    assert card.id in session.story.scene.present_character_ids
    assert "Ruth" not in session.story.scene.present_others
    assert "ruth" not in session.bundle.supporting.dismissed, "asked for by name"
    # Asked again: the same card, nothing written twice.
    again, _ = session.card_for("Ruth")
    assert again is card


def test_a_card_with_the_name_alone_when_the_story_says_nothing(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast, ["Quiet.", "{}", "not json"])
    list(session.send(turn("I wait.")))
    card, filled = session.card_for("Nobody Named")
    assert not filled and card.name == "Nobody Named" and card in session.supporting


# --- objects, made and placed ----------------------------------------------------------


def test_objects_are_made_placed_and_removed_as_corrections(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast, ["{}"])
    _begun(session)
    session.config.map_art = True
    names = session.parse_objects("an armchair, a worn rug")
    offer = session.objects_offer(names)
    assert offer.to_draw == names and offer.problem is not None  # no image endpoint here
    assert session.create_objects(names) == []
    choices = session.object_choices()
    assert [c.name for c in choices[:2]] == ["worn rug", "armchair"], "newest first"
    assert all(c.mine and not c.drawn for c in choices[:2])
    assert all(not c.mine for c in choices[2:])
    me = session.area_state().spots["Serrik Vaun"]
    house = session.area("house")
    x, y = next(
        (x, y)
        for x, y in ((me.x + 1, me.y), (me.x - 1, me.y), (me.x, me.y + 1), (me.x, me.y - 1))
        if house.walkable(x, y)
    )
    thing_id = session.place_object("armchair", "house", x, y)
    assert thing_id is not None
    placed = session.area("house", things=session.area_state().things)
    chair = next(p for p in placed.placements if p.name == "armchair")
    assert (chair.x, chair.y) == (x, y)
    assert session.correction_undo == "armchair put here"
    # Anything can be taken off, the plan's own things too.
    plan_thing = next(p for p in placed.placements if p.name != "armchair")
    assert session.remove_object(plan_thing.id, "house")
    after = session.area("house", things=session.area_state().things)
    assert plan_thing.id not in {p.id for p in after.placements}
    assert session.forget_object("armchair") and "armchair" not in session.map_art_copy().objects


def test_making_objects_draws_each_in_the_set_s_style_once(tmp_path, story, cast, monkeypatch):
    session = make_session(tmp_path, story, cast)
    asked = []
    monkeypatch.setattr(
        session,
        "tile_asset_offer",
        lambda role, words: ("flux/klein", 0.003, None),
    )
    monkeypatch.setattr(session, "redraw_tile_assets", lambda wanted: asked.append(wanted) or True)
    drawing = session.create_objects(["armchair", "side table"])
    assert drawing == ["armchair", "side table"]
    assert asked == [[("items/armchair", "armchair"), ("items/side-table", "side table")]]
    # Made again: kept once.
    session.create_objects(["Armchair"])
    assert session.map_art_copy().objects == ["armchair", "side table"]


def _begun(session) -> None:
    """Begun from an opening as written (read like any passage)."""
    session.story.setup.opening_text = "It is winter."
    session.story.setup.opening_mode = "as_written"
    session.story.scene.present_character_ids = ["char-serrik"]
    list(session.begin("char-serrik"))


def test_the_set_up_line_shows_until_the_author_s_first_turn(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast, ["{}", "Snow.", "{}"])
    assert session.objects_offer_line() is None, "nothing on the map before the story begins"
    _begun(session)
    assert session.objects_offer_line() is not None
    list(session.send(turn("I look out.")))
    assert session.objects_offer_line() is None
    session.story.objects_offered = False
    session.objects_offered()
    assert session.story.objects_offered


# --- the window ---------------------------------------------------------------------


@pytest.fixture
def app():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_the_play_view_places_removes_and_offers_a_card(app):
    from PySide6.QtCore import QPoint, QPointF

    from sealedlore.engine.area import build_area
    from sealedlore.gui.area_render import px_for
    from sealedlore.gui.area_view import AreaView, Walker
    from tests.test_area import world_and_art

    world, art = world_and_art()
    area = build_area(world, art, "farm")
    view = AreaView()
    walkers = [
        Walker("Serrik Vaun", 5, 5, None, held=True),
        Walker("Ruth", 7, 5, None, carded=False),
    ]
    view.set_area(area, None, walkers, key=1)
    asked = []
    view.correction_requested.connect(lambda what, args: asked.append((what, args)))
    px = px_for(area)
    free = next(
        (x, y) for y in range(area.height) for x in range(area.width) if area.walkable(x, y)
    )
    view.set_placing("armchair")
    assert view.tool() == "place" and "armchair" in view.hint.text()
    view._clicked(QPointF(free[0] * px + 1, free[1] * px + 1), QPoint())
    assert asked[-1] == ("place_object", ("armchair", *free))
    assert view.tool() == "place", "stays chosen for the next"
    thing = area.placements[0]
    view.set_removing(True)
    assert view.tool() == "remove"
    view._clicked(QPointF(thing.x * px + 1, thing.y * px + 1), QPoint())
    assert asked[-1] == ("remove_thing", (thing.id,))
    stopped = []
    view.tool_stopped.connect(lambda: stopped.append(True))
    view._stop_tool()
    assert view.tool() is None and stopped
    # Someone with no card: the menu offers to make one.
    shown = []
    view._show_menu = lambda menu, _at: shown.append([a.text() for a in menu.actions()])
    view._person_menu(walkers[1], QPoint())
    assert any("Make a card for Ruth" in t for t in shown[-1])
    view._person_menu(Walker("Unknown 1", 8, 5, None, carded=False), QPoint())
    assert not any("Make a card" in t for t in shown[-1]), "named first, then a card"
    view.deleteLater()


def test_the_people_page_offers_a_card_for_someone_with_none(app):
    from sealedlore.gui.people_page import PeoplePage, people_view
    from sealedlore.models.character import Character
    from sealedlore.models.scene import SceneState

    page = PeoplePage(lambda: None)
    scene = SceneState(present_character_ids=["a"], present_others=["Suzuka Nakamoto"])
    page.set_view(people_view([Character(id="a", name="John")], [], scene, "a"))
    assert list(page.make_card_buttons) == ["Suzuka Nakamoto"]
    wanted = []
    page.card_requested.connect(lambda name, to_cast: wanted.append((name, to_cast)))
    actions = page.make_card_buttons["Suzuka Nakamoto"].menu().actions()
    actions[1].trigger()
    assert wanted == [("Suzuka Nakamoto", True)]
    page.deleteLater()


def test_the_people_page_lists_someone_the_map_has_not_named_yet(app):
    """On a map, the woman at the rail was on no list until she gave her
    name (Oct 18 2026): the map's scene has her, and the page says so."""
    from sealedlore.gui.people_page import PeoplePage, people_view
    from sealedlore.models.character import Character
    from sealedlore.models.scene import SceneState

    page = PeoplePage(lambda: None)
    scene = SceneState(
        present_character_ids=["a"],
        present_others=["the woman at the rail (not named yet)"],
    )
    view = people_view(
        [Character(id="a", name="John")],
        [],
        scene,
        "a",
        unnamed=[("Unknown 1", "the woman at the rail")],
    )
    assert view.also_here == [], "listed once, as unnamed, not as a name"
    page.set_view(view)
    assert list(page.name_buttons) == ["Unknown 1"] and page.make_card_buttons == {}
    asked = []
    page.name_requested.connect(asked.append)
    page.name_buttons["Unknown 1"].click()
    assert asked == ["Unknown 1"]
    page.deleteLater()


def test_the_objects_panel_makes_and_hands_out_the_choice(app):
    from sealedlore.engine.session_objects import ObjectChoice
    from sealedlore.gui.objects_panel import ObjectsPanel

    panel = ObjectsPanel()
    panel.parse = parse_objects
    panel.offer_for = lambda names: (f"Draws {len(names)}", True)
    made, placing, removing = [], [], []
    panel.create_requested.connect(made.append)
    panel.place_chosen.connect(placing.append)
    panel.remove_toggled.connect(removing.append)
    panel.what.setText("an armchair and a rug")
    assert panel.offer.text() == "Draws 2" and panel.make_button.isEnabled()
    panel._make()
    assert made == [["armchair", "rug"]] and panel.what.text() == ""
    panel.set_choices(
        [
            ObjectChoice("armchair", "armchair", True, False, True),
            ObjectChoice("a wooden chest", "a-wooden-chest", False, True, False),
        ]
    )
    assert panel.mine.count() == 1 and panel.set_list.count() == 1
    assert "being drawn" in panel.mine.item(0).text()
    panel.set_list.item(0).setSelected(True)
    assert placing[-1] == "a wooden chest"
    panel.mine.item(0).setSelected(True)
    assert placing[-1] == "armchair" and not panel.set_list.selectedItems(), "one choice"
    assert panel.forget_button.isEnabled()
    panel.remove_button.setChecked(True)
    assert removing[-1] is True and panel.chosen() is None
    panel.find.setText("chest")
    assert panel.mine.count() == 0 and panel.set_list.count() == 1
    panel.deleteLater()


def test_the_world_delta_carries_people_outside(tmp_path, story, cast, monkeypatch):
    """The fixture's way of putting someone outside works as a read's would."""
    place, outside = _lone_house(monkeypatch)
    session = make_session(tmp_path, story, cast)
    _with_ruth(session, place, outside)
    world = session.world()
    assert world.where.get("Ruth") == "house" and "Ruth" in world.outside
    assert isinstance(session.story.world_base, WorldDelta)


def _window_fixture():
    pytest.importorskip("PySide6")
    import tests.test_area_view as area_tests

    return area_tests.window


window = _window_fixture()


def test_objects_in_the_window_make_place_and_say_so(app, window):
    """Through the real window: the panel opens on the play view, makes,
    and a click puts the object down as a correction."""
    import tests.test_area_view as area_tests
    from tests.test_corrections_gui import begun

    window.show()
    begun(app, window)
    area_tests.stand(window, {"Serrik Vaun": ("house", 26, 5)})
    window.open_objects()
    area_tests.pump(app)
    panel = window._objects_panel
    assert panel is not None and panel.isVisible()
    assert window.right_tabs.currentWidget() is window.world_view
    # Filled as it opens: a hidden panel isn't, and the lists came up empty.
    choices = window.session.object_choices()
    assert choices and panel.set_list.count() + panel.mine.count() == len(choices)
    window._objects_create(["armchair"])
    area_tests.pump(app)
    assert panel.mine.count() == 1
    panel.mine.item(0).setSelected(True)
    assert window.area_view.tool() == "place"
    window.area_view.correction_requested.emit("place_object", ("armchair", 25, 5))
    area_tests.pump(app)
    placed = window.area_view.area.placements
    assert any(p.name == "armchair" and (p.x, p.y) == (25, 5) for p in placed)
    assert "Armchair placed" in window.statusBar().currentMessage()
    panel.close()
    assert window.area_view.tool() is None, "closing the panel puts the tool down"


def test_objects_and_the_look_wait_for_the_story_s_job(app, window):
    """Make, Forget and Look ▾'s set choice wrote the map's art while the
    worker owned the story; and Set up pictures… on the People page opened
    nothing, with no word why (the review, Oct 20 2026)."""
    import tests.test_area_view as area_tests
    from tests.test_corrections_gui import begun

    window.show()
    begun(app, window)
    session = window.session
    before = session.map_art_copy().tile_set
    choices = session.object_choices()
    window._busy = True
    try:
        window.open_objects()
        area_tests.pump(app)
        assert window._objects_panel is None
        assert "Objects can be made once" in window.statusBar().currentMessage()
        window._objects_create(["armchair"])
        window._objects_forget("armchair")
        assert session.object_choices() == choices
        window._area_look("village" if before != "village" else "doomsville")
        assert session.map_art_copy().tile_set == before
        assert "look can be changed once" in window.statusBar().currentMessage()
        window.open_settings("Images")
        assert "Settings can be opened once" in window.statusBar().currentMessage()
    finally:
        window._busy = False
