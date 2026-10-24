"""Visual Stories, step 1 (the author, Oct 24 2026; the design in
SealedLoreDocs/design/visual-stories.md): a story played on its map as a
game, in its own window, with no turns and no model. Offscreen, never the
network. Marked `maps_withdrawn`: the mode works with the map withdrawn from
ordinary stories."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.engine.area import stand_near  # noqa: E402
from sealedlore.engine.session_visual import TIME_WORDS  # noqa: E402
from sealedlore.engine.world_art import is_window  # noqa: E402
from sealedlore.gui import start_page  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.visual_window import VisualWindow, player_choices  # noqa: E402
from sealedlore.models.area import Found, LiveArea, Spot, ThingRecord  # noqa: E402
from sealedlore.models.character import Character  # noqa: E402
from sealedlore.models.story import keeps_map  # noqa: E402
from sealedlore.models.visual import (  # noqa: E402
    CreatureSpawn,
    ItemSpawn,
    LockedDoor,
    VisualDesign,
)
from sealedlore.models.world import WorldDelta  # noqa: E402
from sealedlore.storage.library import story_listings, story_mode  # noqa: E402
from sealedlore.storage.paths import PLOT_FORMAT_GUIDE, visual_stories  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from sealedlore.storage.world_art import save_world_art  # noqa: E402
from tests.test_area import world_and_art  # noqa: E402
from tests.test_samples import SAMPLES  # noqa: E402

pytestmark = pytest.mark.maps_withdrawn


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def pump(app) -> None:
    for _ in range(5):
        app.processEvents()
        app.sendPostedEvents(None, QEvent.DeferredDelete)


def _visual_bundle(story, cast) -> tuple[StoryBundle, object]:
    world, art = world_and_art()
    # The town has a plan of its own here (the farm's, for a journey to
    # arrive in): a visual story never lands on open country.
    farm = art.layouts["farm"][""]
    art.layouts["town"] = {"": farm.model_copy(update={"place_id": "town"})}
    story.mode = "visual"
    story.world_base = WorldDelta(places=list(world.places.values()), seen=list(world.seen))
    story.world_start = "house"
    story.held_character_id = "char-serrik"
    story.scene.present_character_ids = ["char-serrik", "char-maela"]
    story.setup.starting_scene.time_of_day = "late afternoon, deep winter"
    return StoryBundle(story=story, cast=cast), art


@pytest.fixture
def window(app, tmp_path: Path, story, cast):
    """The main window with a visual story saved and the game's window open on it."""
    bundle, art = _visual_bundle(story, cast)
    save_story_bundle(bundle, root=tmp_path)
    save_world_art(story.id, art, tmp_path)
    main = MainWindow(root=tmp_path, use_mock=True)
    main.config.map_art = False
    main.open_story(story.id)
    pump(app)
    yield main
    if getattr(main, "visual_window", None) is not None:
        main.visual_window.close()
        pump(app)
    main.close()


def walk_all(app, view) -> None:
    for _ in range(500):
        if not view.walking():
            break
        view._step()
    pump(app)


def test_a_visual_story_is_played_on_its_map_whatever_the_flag_says(tmp_path, story, cast):
    bundle, art = _visual_bundle(story, cast)
    assert not keeps_map(bundle.story.defaults)  # ordinary stories: withdrawn
    save_story_bundle(bundle, root=tmp_path)
    save_world_art(story.id, art, tmp_path)
    assert story_mode(story.id, tmp_path) == "visual"
    listing = next(row for row in story_listings(tmp_path) if row.id == story.id)
    assert listing.visual and listing.keeps_map and not listing.chat


def test_the_start_page_offers_the_visual_stories_under_the_samples(
    app, tmp_path: Path, monkeypatch
):
    folder = tmp_path / "samples"
    folder.mkdir()
    for name in ("Doomsville.md", "Doomsville with a map.md", PLOT_FORMAT_GUIDE):
        shutil.copy(SAMPLES / name, folder)
    shutil.copytree(SAMPLES / "pictures", folder / "pictures")
    monkeypatch.setenv("SEALEDLORE_SAMPLES", str(folder))
    assert [name for name, _p in visual_stories(folder)] == ["Doomsville"]
    main = MainWindow(root=tmp_path / "data", use_mock=True)
    opened: list[Path] = []
    monkeypatch.setattr(main, "open_visual_sample", opened.append)
    main.reload_transcript()
    page = main.start_page
    card = page.cards[f"{start_page.VISUAL}:Doomsville with a map.md"]
    assert card.title.text() == "Doomsville"
    assert "Four years after the dead rose" in card.text.text()
    page._show_pictures()
    assert card.picture is not None and not card.picture.pixmap().isNull()
    # Not among the ordinary samples (the map is withdrawn there).
    assert f"{start_page.SAMPLE}:Doomsville with a map.md" not in page.cards
    card.activated.emit()
    assert opened == [folder / "Doomsville with a map.md"]
    main.close()


def test_the_game_s_window_opens_on_the_area_and_the_main_window_hides(window: MainWindow):
    game = window.visual_window
    assert isinstance(game, VisualWindow) and game.isVisible()
    assert window.isHidden()
    session = game.session
    assert session.story.visual and session.map_on
    view = game.area_view
    assert view.area is not None and view.area.id == "house"
    me = view.held()
    assert me is not None and me.name == "Serrik Vaun"
    assert game.name.text() == "Serrik Vaun"
    # The clock: the opening's late afternoon, day 1.
    assert "Day 1, 16:00" in game.where.text() and "The house" in game.where.text()
    assert view.clock.text() == "Day 1, 16:00"
    # None of the story window's tools.
    assert view.look_button.isHidden() and view.edit_button.isHidden()
    assert view.objects_button.isHidden() and view.paintings_button.isHidden()
    assert "turn" not in view.hint.text()


def test_a_click_walks_at_once_and_moves_the_clock(app, window: MainWindow):
    game = window.visual_window
    session, view = game.session, game.area_view
    me = session.area_state().spots["Serrik Vaun"]
    area = session.area(me.area)
    # Somewhere walkable a few tiles off.
    goal = next(
        (x, y)
        for y in range(area.height)
        for x in range(area.width)
        if area.walkable(x, y) and 3 <= abs(x - me.x) + abs(y - me.y) <= 6
    )
    before = session.area_state().seconds
    view.go_requested.emit(goal[0], goal[1], False, "")
    after = session.area_state().spots["Serrik Vaun"]
    assert (after.x, after.y) == goal  # moved at once, no turn
    assert session.area_state().seconds > before
    assert session.story.area_live is not None  # saved as the live state
    assert view.walking()  # the figure walks over
    walk_all(app, view)
    shown = view.held()
    assert (shown.x, shown.y) == goal
    assert session.path() == []  # still no messages: nothing was sent


def test_a_door_takes_the_player_through_with_a_fade(app, window: MainWindow):
    game = window.visual_window
    session, view = game.session, game.area_view
    house = session.area("house")
    door = house.exits[0]
    view.go_requested.emit(door[0], door[1], False, "")
    assert session.area_state().spots["Serrik Vaun"].area == "farm"
    assert "You step out into" in game.log.toPlainText()
    # The figure walks to the door in the house, then the farm shows.
    walk_all(app, view)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:  # the fade runs on real time
        if game._fading is None and view.area is not None and view.area.id == "farm":
            break
        pump(app)
        time.sleep(0.01)
    assert view.area.id == "farm"
    walk_all(app, view)
    me = session.area_state().spots["Serrik Vaun"]
    shown = view.held()
    assert (shown.x, shown.y) == (me.x, me.y)
    assert "The farm" in game.where.text()


def test_travel_sets_off_and_arrives(app, window: MainWindow):
    game = window.visual_window
    session = game.session
    # Out of the house first: journeys start outdoors.
    state = session.area_state()
    farm = session.area("farm")
    spot = next(
        (x, y) for y in range(farm.height) for x in range(farm.width) if farm.walkable(x, y)
    )
    state.spots["Serrik Vaun"] = Spot(area="farm", x=spot[0], y=spot[1])
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    game.presenter.refresh(snap=True)
    choices = session.visual_travel_choices()
    assert [c[0] for c in choices] == ["town"]
    game._travel_menu()
    before = session.area_state().seconds
    game.area_view.travel_chosen.emit("town")
    me = session.area_state().spots["Serrik Vaun"]
    assert me.area == "town" and not is_window(me.area)
    assert session.area_state().seconds - before == choices[0][2]
    assert "You set off for Hellsville" in game.log.toPlainText()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and game._fading is not None:
        pump(app)
        time.sleep(0.01)
    assert game.area_view.area is not None and game.area_view.area.id == me.area


def test_the_line_and_a_person_s_menu_say_what_is_not_in_the_game_yet(app, window: MainWindow):
    game = window.visual_window
    game.line.setText("Hello?")
    game.line.returnPressed.emit()
    words = game.log.toPlainText()
    assert "Hello?" in words and "Nobody answers yet" in words
    game.area_view.action_chosen.emit("talk", "Maela Orr", "")
    assert "Talk Maela Orr: not in the game yet" in game.log.toPlainText()


def test_closing_the_game_saves_and_brings_the_main_window_back(app, window: MainWindow):
    game = window.visual_window
    session = game.session
    story_id = session.story.id
    game.close()
    pump(app)
    assert window.visual_window is None and not window.isHidden()
    assert story_mode(story_id, window.root) == "visual"
    # Opened again from the Stories list, it is the game's window again.
    window.open_story(story_id)
    pump(app)
    assert isinstance(window.visual_window, VisualWindow)


def test_who_can_be_played_and_the_opening_s_hour():
    cast = [
        Character(id="a", name="A"),
        Character(id="b", name="B", plot_hidden=True),
        Character(id="c", name="C", is_player_available=False),
    ]
    assert [c.id for c in player_choices(cast, [])] == ["a"]
    assert [c.id for c in player_choices(cast, ["b", "a"])] == ["a", "b"]  # the cast's order
    assert dict(TIME_WORDS)["dusk"] == 18 * 60 + 15


def test_doomsville_opens_as_a_visual_story(app, tmp_path: Path, monkeypatch):
    """The shipped sample, with its map and paintings: opened from the start
    page, the character chosen, the farm shown."""
    from PySide6.QtWidgets import QInputDialog

    main = MainWindow(root=tmp_path / "data", use_mock=True)
    main.config.map_art = False
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("Jane", True))
    main.open_visual_sample(SAMPLES / "Doomsville with a map.md")
    pump(app)
    game = main.visual_window
    assert isinstance(game, VisualWindow)
    session = game.session
    assert session.story.title == "Doomsville" and session.story.visual
    assert session.story.held_character_id == next(c.id for c in session.cast if c.name == "Jane")
    assert game.name.text() == "Jane" and game.portrait.pixmap() is not None
    assert "Day 1, 15:30" in game.where.text()
    view = game.area_view
    assert view.area is not None and view.area.name in ("The farm", "The farmhouse")
    assert session.backdrop_of(view.area) is not None  # the shipped painting
    assert "Freezing wind" in game.log.toPlainText() or "farmhouse" in game.log.toPlainText()
    game.close()
    pump(app)
    main.close()


# --- the author's first play (Oct 24 2026): open country, redirecting, Look ---


def test_journeys_and_edges_never_lead_into_open_country(window: MainWindow):
    """Walking east into open country took the author to "a strange looking
    space with a single building in the middle", and entering it returned
    them to the farm: the window of open country round the farm, with the
    farm stamped on it. A visual story is always somewhere designed."""
    game = window.visual_window
    session = game.session
    state = session.area_state()
    farm = session.area("farm")
    spot = next(
        (x, y) for y in range(farm.height) for x in range(farm.width) if farm.walkable(x, y)
    )
    state.spots["Serrik Vaun"] = Spot(area="farm", x=spot[0], y=spot[1])
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    game.presenter.refresh(snap=True)
    # A place with no plan is not offered (the map story offered the open
    # country round it); the town, with one, is.
    del session.world_art.layouts["town"]
    session._art_version += 1
    assert [c[0] for c in session.travel_choices()] == ["town"]
    assert session.visual_travel_choices() == []
    assert session.visual_travel("town") is None
    for side in ("north", "east", "south", "west"):
        assert all(c[0] != "area" for c in session.visual_edge_choices(side))
        assert session.visual_cross(side) is None
    before = session.area_state().spots["Serrik Vaun"]
    game.area_view.edge_chosen.emit("east", "area")
    assert session.area_state().spots["Serrik Vaun"] == before
    assert "barred" in window.visual_window.statusBar().currentMessage()


def test_a_click_mid_walk_turns_the_figure_at_once(app, window: MainWindow):
    """The game waited for a walk to end before taking the next click (the
    author: not responsive, no changing destination part way)."""
    game = window.visual_window
    session, view = game.session, game.area_view
    me = session.area_state().spots["Serrik Vaun"]
    area = session.area(me.area)
    far = max(
        ((x, y) for y in range(area.height) for x in range(area.width) if area.walkable(x, y)),
        key=lambda t: abs(t[0] - me.x) + abs(t[1] - me.y),
    )
    view.go_requested.emit(far[0], far[1], False, "")
    assert view.walking()
    after_first = session.area_state().seconds
    for _ in range(3):
        view._step()  # three tiles along
    shown = view.held()
    stood = (shown.x, shown.y)
    assert stood != (me.x, me.y) and stood != far
    # Back the other way: the figure turns where it is, the game has them
    # there, the unwalked time is given back.
    view.go_requested.emit(me.x, me.y, False, "")
    now = session.area_state()
    assert (now.spots["Serrik Vaun"].x, now.spots["Serrik Vaun"].y) == (me.x, me.y)
    assert now.seconds < after_first
    assert view.walking()
    walk_all(app, view)
    shown = view.held()
    assert (shown.x, shown.y) == (me.x, me.y)


LOOK_REPLY = """{"see": "A heavy sack, its mouth tied with wire. It is half full.",
"found": [{"in": "%s", "name": "a tin of beans", "count": 2, "kind": "food"},
{"in": null, "name": "a rusted knife", "count": 1, "kind": "weapon", "note": "under the sack"}]}"""


def test_a_look_shows_the_part_and_makes_what_it_finds_real(app, window: MainWindow):
    """The author: a black tarp on the floor nobody could ask about. Look at
    it: the model is shown the part and told what the game knows, and what
    it finds is in the game from then on."""
    from sealedlore.engine.look import FOUND_PREFIX

    game = window.visual_window
    session, view = game.session, game.area_view
    area = view.area
    thing = area.placements[0]
    session.provider.responses = [LOOK_REPLY % thing.id]
    view.look_thing.emit(thing.id)
    assert game._look_pending is not None
    assert "You look closely" in game.log.toPlainText()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and game._look_pending is not None:
        pump(app)
        time.sleep(0.01)
    assert game._look_pending is None
    # The request: the rules, the picture of the part, what stands there by id.
    request = session.provider.requests[-1]
    assert request.messages[0].text.startswith("You are the eyes of a player")
    assert request.messages[1].parts[0].image.startswith("data:image/jpeg;base64,")
    assert f"- {thing.id}: {thing.name}" in request.messages[1].text
    assert "Serrik Vaun looks closely at it." in request.messages[1].text
    assert "image_url" in str(session.provider.payloads[-1])
    words = game.log.toPlainText()
    assert "A heavy sack, its mouth tied with wire." in words
    assert "You find a tin of beans ×2 in" in words and "You find a rusted knife here." in words
    things = session.area_state().things
    record = things[thing.id]
    assert record.state == "seen" and record.seen.startswith("A heavy sack")
    assert [(f.name, f.count, f.kind) for f in record.contents] == [("a tin of beans", 2, "food")]
    knife = next(r for pid, r in things.items() if pid.startswith(FOUND_PREFIX))
    assert knife.state == "dropped" and knife.name == "a rusted knife" and knife.spot is not None
    # The knife lies on the floor now, a thing of its own on the area.
    shown = session.area(area.id)
    assert any(p.name == "a rusted knife" for p in shown.placements)
    assert game._contents_of(thing.id) == ["a tin of beans ×2"]
    # Saved: the records are the live state's.
    assert session.story.area_live.state.things[thing.id].contents[0].name == "a tin of beans"
    # Looked at again, the model is told what was found before.
    session.provider.responses = ['{"see": "The same sack.", "found": []}']
    view.look_drawn.emit(thing.x, thing.y, thing.x + 2, thing.y + 2)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and game._look_pending is not None:
        pump(app)
        time.sleep(0.01)
    text = session.provider.requests[-1].messages[1].text
    assert "holds: a tin of beans ×2" in text and "lying here: a rusted knife" in text
    assert "You find nothing you could take." in game.log.toPlainText()


def test_the_look_s_reply_is_held_to_its_shape():
    from sealedlore.engine.area import Placement
    from sealedlore.engine.look import parse_look

    sack = Placement(id="s1", name="a sack", x=1, y=1, w=1, h=1, verbs=(), kind="object")
    reply = parse_look(
        'Sure: {"see": "  Dust.  ", "found": [{"in": "nope", "name": "a coin", "count": "999"},'
        ' {"name": ""}, {"in": "s1", "name": "rope", "count": -3}, "junk"]}',
        [sack],
    )
    assert reply.see == "Dust."
    assert [(f.name, f.count, f.inside) for f in reply.found] == [
        ("a coin", 50, ""),
        ("rope", 1, "s1"),
    ]
    with pytest.raises(ValueError):
        parse_look("nothing here", [sack])


# --- step 2: the rules (engine/visual_rules.py, models/visual.py) -------------


def _design() -> VisualDesign:
    return VisualDesign(
        abilities={"Serrik Vaun": 7},
        player_hits=2,
        locked=[LockedDoor(place="The house", key="house key")],
        creatures=[CreatureSpawn(kind="the dead", place="The farm", count=2, ability=4)],
        items=[
            ItemSpawn(name="a tin of beans", place="The house", count=2, kind="food"),
            ItemSpawn(name="a house key", place="The farm", count=1, kind="key"),
            ItemSpawn(name="a first aid kit", place="The house", count=1, kind="medicine"),
        ],
        flavour={"The house": "Dust, and a cold stove.", "The farm": "Snow to the knee."},
    )


@pytest.fixture
def game(app, tmp_path: Path, story, cast):
    """A visual story with a design: dead on the farm, a locked house, things."""
    bundle, art = _visual_bundle(story, cast)
    bundle.story.visual_design = _design()
    # Outside, on the farm, to begin with.
    farm_rows = art.layouts["farm"][""].rows
    save_story_bundle(bundle, root=tmp_path)
    save_world_art(story.id, art, tmp_path)
    main = MainWindow(root=tmp_path, use_mock=True)
    main.config.map_art = False
    main.open_story(story.id)
    pump(app)
    game = main.visual_window
    session = game.session
    farm = session.area("farm")
    state = session.area_state()
    spot = next(
        (x, y) for y in range(farm.height) for x in range(farm.width) if farm.walkable(x, y)
    )
    state.spots["Serrik Vaun"] = Spot(area="farm", x=spot[0], y=spot[1])
    state.spots.pop("Maela Orr", None)
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    game.presenter.refresh(snap=True)
    game._after_action([])
    del farm_rows
    yield game
    game.close()
    pump(app)
    main.close()


def test_the_design_is_laid_out_once_dead_things_and_locks(game: VisualWindow):
    session = game.session
    state = session.area_state()
    assert state.laid_out
    dead = [c for c in state.creatures.values() if c.kind == "the dead"]
    assert len(dead) == 2 and all(c.area == "farm" for c in dead)
    names = sorted(r.name for r in state.things.values() if r.state == "dropped")
    assert names == ["a first aid kit", "a house key", "a tin of beans"]
    beans = next(r for r in state.things.values() if r.name == "a tin of beans")
    assert beans.count == 2 and beans.kind == "food" and beans.spot.area == "house"
    farm, house = session.area("farm"), session.area("house")
    door = next(e for e in farm.entrances if e.place_id == "house")
    locked = state.doors[f"farm:{door.x},{door.y}"]
    assert locked.locked and locked.key == "house key"
    # On the area: the key lies on the farm as a thing of its own.
    assert any(p.name == "a house key" for p in farm.placements)
    assert any(p.name == "a tin of beans" for p in house.placements)
    assert game.health.text() == "♥♥" and game.carry_button.text() == "Carrying nothing"
    assert "Snow to the knee." in game.log.toPlainText()  # the flavour line, once
    # One of the dead brought into sight is reported, once.
    me = state.spots["Serrik Vaun"]
    first = next(iter(state.creatures.values()))
    first.x, first.y = stand_near(farm, (me.x, me.y), farm.blocked() | {(me.x, me.y)})
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    lines = session.visual_report()
    assert any("shambling" in line or "has seen you" in line for line in lines)
    assert session.visual_report() == []


def test_a_locked_door_stops_the_player_until_the_key_is_carried_or_it_is_forced(
    game: VisualWindow, monkeypatch
):
    from PySide6.QtWidgets import QMessageBox

    session, view = game.session, game.area_view
    farm = session.area("farm")
    door = next(e for e in farm.entrances if e.place_id == "house")
    state = session.area_state()
    state.creatures.clear()  # a quiet farm for this
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    answers = []
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: answers.append(a) or QMessageBox.No
    )
    view.go_requested.emit(door.x, door.y, False, "")
    walk_all(QApplication.instance(), view)
    assert session.area_state().spots["Serrik Vaun"].area == "farm"
    assert "Locked. It wants a house key." in game.log.toPlainText()
    assert len(answers) == 1  # offered to force it
    # With the key: in.
    key = next(p for p in farm.placements if p.name == "a house key")
    state = session.area_state()
    kx, ky = stand_near(farm, (key.x, key.y), farm.blocked())
    state.spots["Serrik Vaun"] = Spot(area="farm", x=kx, y=ky)
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    game.presenter.refresh(snap=True)
    assert session.visual_take(key.id) == "You take the house key."
    assert [r.name for _i, r in session.visual_inventory()] == ["a house key"]
    view.go_requested.emit(door.x, door.y, False, "")
    assert session.area_state().spots["Serrik Vaun"].area == "house"
    assert "You go into" in game.log.toPlainText()


def test_taking_eating_dropping_and_mending(game: VisualWindow):
    session = game.session
    house = session.area("house")
    beans = next(p for p in house.placements if p.name == "a tin of beans")
    kit = next(p for p in house.placements if p.name == "a first aid kit")
    state = session.area_state()
    state.creatures.clear()
    state.spots["Serrik Vaun"] = Spot(area="house", x=beans.x, y=beans.y)
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    assert session.visual_take(beans.id) == "You take the tin of beans."
    far = next(p for p in house.placements if max(abs(p.x - beans.x), abs(p.y - beans.y)) > 2)
    assert session.visual_take(far.id) == "You aren't close enough."
    stove = next((p for p in house.placements if "stove" in p.name.lower()), None)
    if stove is not None:
        state = session.area_state()
        state.spots["Serrik Vaun"] = Spot(area="house", x=stove.x, y=stove.y + stove.h)
        session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
        assert "too much to carry" in session.visual_take(stove.id)
    record_id, record = session.visual_inventory()[0]
    assert record.count == 2 and session.visual_item_actions(record) == ("eat", "drop")
    assert session.visual_use(record_id, "eat") == "You eat the tin of beans."
    assert (
        session.visual_use(record_id, "eat") == "You eat the tin of beans. That was the last of it."
    )
    assert session.visual_inventory() == []
    # Hurt, the kit mends one wound.
    state = session.area_state()
    state.wounds = 1
    state.spots["Serrik Vaun"] = Spot(area="house", x=kit.x, y=kit.y)
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    session.visual_take(kit.id)
    kit_id = session.visual_inventory()[0][0]
    assert session.visual_use(kit_id, "use") == "You see to your wounds with the first aid kit."
    assert session.area_state().wounds == 0 and session.visual_inventory() == []
    # Something taken out of a look's find, then put down.
    state = session.area_state()
    state.things[beans.id] = state.things[beans.id].model_copy(update={"state": "gone"})
    holder = house.placements[0]
    state.things[holder.id] = ThingRecord(
        state="seen", name=holder.name, contents=[Found(name="a candle", kind="light")]
    )
    state.spots["Serrik Vaun"] = Spot(area="house", x=holder.x, y=holder.y)
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    assert session.visual_take_content(holder.id, 0).startswith("You take a candle from the")
    candle_id, candle = session.visual_inventory()[0]
    assert candle.kind == "light"
    assert session.visual_drop(candle_id) == "You put down the candle."
    shown = session.area("house")
    assert any(p.name == "a candle" for p in shown.placements)


def test_the_dead_attack_wounds_count_and_death_offers_a_fresh_start(
    app, game: VisualWindow, monkeypatch
):
    from PySide6.QtWidgets import QMessageBox

    session, view = game.session, game.area_view
    state = session.area_state()
    me = state.spots["Serrik Vaun"]
    farm = session.area("farm")
    # One of the dead right beside, too strong to beat bare-handed.
    beside = next(
        (x, y)
        for x, y in ((me.x + 1, me.y), (me.x, me.y + 1), (me.x - 1, me.y), (me.x, me.y - 1))
        if farm.walkable(x, y)
    )
    for creature in state.creatures.values():
        creature.ability = 9
        creature.x, creature.y = beside
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    game.presenter.refresh(snap=True)
    assert len(view.creature_ids) == 2 and all(
        w.label == "" for w in view.walkers if w.name in view.creature_ids
    )
    lines = session.visual_wait(1)
    assert any("comes at you" in line for line in lines) and session.area_state().wounds >= 1
    # Attack back with a rifle: 7 + 4 beats 9.
    state = session.area_state()
    state.things["item:rifle"] = ThingRecord(
        state="taken", name="a rifle", by="Serrik Vaun", kind="weapon"
    )
    state.wounds = 0
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    target = next(iter(state.creatures))
    lines = session.visual_attack(target)
    assert (
        lines[0].startswith("You put one of the dead down with the a rifle")
        or "put one of the dead down" in lines[0]
    )
    assert session.area_state().creatures[target].state == "down"
    # Bare-handed against the other until dead: the dialog offers Start again.
    state = session.area_state()
    state.things.pop("item:rifle")
    state.wounds = 1  # one hit from death (player_hits 2)
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    chosen = {}

    def exec_(self):
        chosen["buttons"] = [b.text() for b in self.buttons()]
        self._clicked = self.buttons()[0]  # Start again
        return 0

    monkeypatch.setattr(QMessageBox, "exec", exec_)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: self._clicked)
    game._pass_time(False)
    assert chosen["buttons"] == ["Start again", "Close"]
    assert "You are dead." in game.log.toPlainText() or "You start again." in game.log.toPlainText()
    fresh = session.area_state()
    assert not fresh.dead and fresh.wounds == 0 and fresh.laid_out


def test_the_clock_passes_with_waiting_and_sleeping(game: VisualWindow):
    session = game.session
    state = session.area_state()
    state.creatures.clear()
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    before = session.map_clock()
    session.visual_wait(60)
    after = session.map_clock()
    assert after[1] - before[1] == 60
    lines = session.visual_sleep()
    assert lines[0].startswith("You sleep") and session.map_clock() == (2, 7 * 60)


def test_doomsville_s_sheet_comes_with_the_story(app, tmp_path: Path, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    main = MainWindow(root=tmp_path / "data", use_mock=True)
    main.config.map_art = False
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("John", True))
    main.open_visual_sample(SAMPLES / "Doomsville with a map.md")
    pump(app)
    game = main.visual_window
    session = game.session
    design = session.story.visual_design
    assert design is not None and design.abilities["John"] == 7
    state = session.area_state()
    assert state.laid_out and len(state.creatures) == 6
    assert any(r.name == "a hunting knife" for r in state.things.values())
    assert any(d.locked and d.key == "clinic key" for d in state.doors.values())
    assert "Snow to the knee" in game.log.toPlainText()
    # Nearest first; the barn is inside the farm, entered by its door.
    assert [c[1] for c in session.visual_travel_choices()][:3] == [
        "The creek crossing",
        "The gas station",
        "The south gate",
    ]
    game.close()
    pump(app)
    main.close()
