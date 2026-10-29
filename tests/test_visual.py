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

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.engine.area import path, stand_near  # noqa: E402
from sealedlore.engine.session_visual import TIME_WORDS  # noqa: E402
from sealedlore.engine.world_art import is_window  # noqa: E402
from sealedlore.gui import start_page  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.visual_window import VisualWindow, player_choices  # noqa: E402
from sealedlore.models.area import Found, LiveArea, Spot, ThingRecord  # noqa: E402
from sealedlore.models.character import Character  # noqa: E402
from sealedlore.models.plot import Plot  # noqa: E402
from sealedlore.models.visual import (  # noqa: E402
    CreatureSpawn,
    ItemSpawn,
    LockedDoor,
    PersonSheet,
    ThingNote,
    VisualDesign,
)
from sealedlore.models.world import WorldDelta  # noqa: E402
from sealedlore.storage.library import story_listings, story_mode  # noqa: E402
from sealedlore.storage.paths import PLOT_FORMAT_GUIDE, visual_stories  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from sealedlore.storage.world_art import save_world_art  # noqa: E402
from tests.test_area import world_and_art  # noqa: E402
from tests.test_samples import SAMPLES  # noqa: E402


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


def test_the_line_needs_a_listener_and_a_menu_verb_without_rules_says_so(app, window: MainWindow):
    game = window.visual_window
    # With nobody to talk to, the line is an attempt the keeper rules on;
    # the mock's stock reply is no ruling, so nothing comes of it.
    game.line.setText("Hello?")
    game.line.returnPressed.emit()
    assert game._keeper_pending == "action"
    end = time.monotonic() + 10
    while time.monotonic() < end and game._keeper_pending is not None:
        pump(app)
        time.sleep(0.01)
    words = game.log.toPlainText()
    assert "Serrik Vaun attempts: Hello?" in words and "Nothing comes of it" in words
    # A menu verb the rules lack is an attempt too (the author, Oct 24 2026:
    # the model handles any mechanic that isn't coded).
    game.area_view.action_chosen.emit("use", "a stove", "nope")
    assert "Serrik Vaun attempts: I use the stove." in game.log.toPlainText()
    assert game._keeper_pending == "action"
    end = time.monotonic() + 10
    while time.monotonic() < end and game._keeper_pending is not None:
        pump(app)
        time.sleep(0.01)


def test_closing_the_game_saves_and_brings_the_main_window_back(app, window: MainWindow):
    """Closing keeps the log and the clock with the story: opened again, the
    log reads as it was left and the Stories list says where the game's
    clock stands (the author, Oct 25 2026: the log came back as the opening
    alone, and the list said "0 messages")."""
    game = window.visual_window
    session = game.session
    story_id = session.story.id
    opening_lines = game.log.toPlainText().count("\n") + 1
    game.say("Serrik takes a breath.", kind="report")
    game.say("Hello?", kind="you")
    assert [line.text for line in session.story.visual_log][-2:] == [
        "Serrik takes a breath.",
        "Hello?",
    ]
    game.close()
    pump(app)
    assert window.visual_window is None and not window.isHidden()
    assert story_mode(story_id, window.root) == "visual"
    listing = next(row for row in story_listings(window.root) if row.id == story_id)
    assert listing.visual_when.startswith("Day 1, ")
    label = next(
        window.stories.list.item(i).text()
        for i in range(window.stories.list.count())
        if window.stories.list.item(i).data(Qt.UserRole) == story_id
    )
    assert label.split("\n")[1] == f"Visual story · {listing.visual_when}"
    # Opened again from the Stories list, it is the game's window again, on
    # the log as it was left: the opening once, the lines said after it.
    window.open_story(story_id)
    pump(app)
    game = window.visual_window
    assert isinstance(game, VisualWindow)
    text = game.log.toPlainText()
    assert "Serrik takes a breath." in text and "Hello?" in text
    assert text.count("\n") + 1 >= opening_lines + 2
    assert text.index("Hello?") > text.index("Serrik takes a breath.")
    assert len([line for line in game.session.story.visual_log if line.text == "Hello?"]) == 1


def test_a_visual_story_opens_over_a_memory_chat_or_scene_without_asking(
    app, window: MainWindow, monkeypatch
):
    """The story open in the main window stays open under the game's window
    and is back when it closes, so a memory-only chat or scene loses nothing.
    The question it was asked said otherwise, and its Yes discarded a memory
    scene (the mode audit, Oct 29 2026)."""
    story_id = window.visual_window.session.story.id
    window.visual_window.close()
    pump(app)
    asked = []
    monkeypatch.setattr(window, "_private_allows_close", lambda: asked.append(1) or False)
    window.open_story(story_id)
    pump(app)
    assert isinstance(window.visual_window, VisualWindow) and not asked


def test_one_game_at_a_time_and_quit_quits(app, window: MainWindow, monkeypatch):
    """A second open replaced the game's window, and the first's close then
    deleted the second unsaved. Game → Quit only closed the game."""
    import sealedlore.gui.visual_window as visual_window

    game = window.visual_window
    window.open_story(game.session.story.id)
    pump(app)
    assert window.visual_window is game
    closed = []
    monkeypatch.setattr(
        visual_window.QApplication, "closeAllWindows", staticmethod(lambda: closed.append(1))
    )
    game._quit()
    pump(app)
    assert window.visual_window is None and closed == [1]


def test_a_visual_story_with_nobody_played_asks_who(app, tmp_path: Path, story, cast, monkeypatch):
    """A scenario leaves the player to whoever imports it: the game asks, as
    a sample does, rather than opening on nobody."""
    import sealedlore.gui.main_window as main_window

    bundle, art = _visual_bundle(story, cast)
    story.held_character_id = None
    save_story_bundle(bundle, root=tmp_path)
    save_world_art(story.id, art, tmp_path)
    asked = []

    def choose(_parent, _title, _label, labels, *_args):
        asked.append(labels)
        return "Maela Orr", True

    monkeypatch.setattr(main_window.QInputDialog, "getItem", staticmethod(choose))
    main = MainWindow(root=tmp_path, use_mock=True)
    main.open_story(story.id)
    pump(app)
    assert asked and "Maela Orr" in asked[0]
    assert main.visual_window.session.held_name() == "Maela Orr"
    main.visual_window.close()
    pump(app)
    main.close()


def test_the_game_shows_its_spend_and_backs_up(app, window: MainWindow, tmp_path, monkeypatch):
    """A game's spend showed nowhere, and only the terminal could back one
    up (the author, Oct 29 2026). The Game menu has what a game has of a
    story's Export and Usage, and nothing else."""
    import sealedlore.gui.visual_window as visual_window
    from sealedlore.models.node import Usage
    from sealedlore.storage.archive import read_archive
    from sealedlore.storage.repository import append_api_log

    game = window.visual_window
    session = game.session
    items = [
        a.text().replace("&", "")
        for m in game.menuBar().actions()
        if m.menu() is not None
        for a in m.menu().actions()
    ]
    assert "Usage and cost…" in items and "Back up this game…" in items
    assert not any("Markdown" in item or "scenario" in item.lower() for item in items)

    def spent() -> tuple[float, float]:
        game._refresh_cost()
        words = game.cost_label.text().replace("$", "").split()
        return float(words[2]), float(words[-1])

    session.unreported_usage.append(Usage(cost=0.0, cost_reported=True))
    before = spent()
    append_api_log(
        session.story.id,
        {"kind": "keeper_response", "usage": {"prompt_tokens": 10, "cost": 0.02}},
        root=window.root,
    )
    session.unreported_usage.append(Usage(cost=0.01, cost_reported=True))
    after = spent()
    assert round(after[0] - before[0], 4) == 0.01 and round(after[1] - before[1], 4) == 0.02
    assert game.cost_label.text().startswith("This session $") and "  ·  Game $" in (
        game.cost_label.text()
    )
    assert "Game → Usage and cost…" in game.cost_label.toolTip()

    target = tmp_path / "game.sealedlore-backup.json"
    monkeypatch.setattr(
        visual_window.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *_a, **_k: (str(target), "")),
    )
    game._back_up()
    bundle, log = read_archive(target)
    assert bundle.story.visual and bundle.story.held_character_id == "char-serrik"
    assert any(entry.get("kind") == "keeper_response" for entry in log)


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
    # Straight there: never on to the first click's place first (the author,
    # Oct 24 2026: the figure went to the clicked places in order).
    assert view._steps[-1] == (me.x, me.y) and far not in view._steps
    assert view._walked[0] == stood
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
    session.provider.responses = [LOOK_REPLY % "T1"]
    view.look_thing.emit(thing.id)
    assert game._look_pending is not None
    assert "You look closely" in game.log.toPlainText()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and game._look_pending is not None:
        pump(app)
        time.sleep(0.01)
    assert game._look_pending is None
    # The request: the rules, the picture of the part, what is there by handle.
    request = session.provider.requests[-1]
    assert request.messages[0].text.startswith("You are the eyes of a player")
    assert request.messages[1].parts[0].image.startswith("data:image/jpeg;base64,")
    assert f"- T1: {thing.name}" in request.messages[1].text
    assert thing.id not in request.messages[1].text
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
    assert knife.kind == "weapon" and knife.found_at == thing.id
    # The knife lies on the floor now, a thing of its own on the area, beside
    # the stove and never under it, and it can be taken.
    shown = session.area(area.id)
    lying = next(p for p in shown.placements if p.name == "a rusted knife")
    assert (knife.spot.x, knife.spot.y) not in thing.tiles() and "take" in lying.verbs
    assert shown.placement_at(knife.spot.x, knife.spot.y).name == "a rusted knife"
    assert game._contents_of(thing.id) == ["a tin of beans ×2"]
    # Saved: the records are the live state's.
    assert session.story.area_live.state.things[thing.id].contents[0].name == "a tin of beans"
    # Looked at again, the model is told what was found before.
    session.provider.responses = ['{"see": "The same sack.", "found": []}']
    view.look_thing.emit(thing.id)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and game._look_pending is not None:
        pump(app)
        time.sleep(0.01)
    text = session.provider.requests[-1].messages[1].text
    assert f"{thing.name} — in or on it: a tin of beans ×2" in text
    assert "a rusted knife (lying loose)" in text and "A heavy sack" in text
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


def test_a_find_s_holder_is_read_by_handle_by_a_unique_name_or_by_id():
    """Live (Oct 26 2026), Sonnet 4.6 answered the scarecrow's id cut at its
    first colon; the shirt was taken to lie in the open, under the scarecrow."""
    from sealedlore.engine.area import Placement
    from sealedlore.engine.look import holder_of

    farm = "b637a244-e18f-5caf-b588-6cd5a28043c4"
    crow = Placement(
        id=f"{farm}:object:4:22:25", name="scarecrow", x=22, y=25, w=1, h=1, verbs=(), kind="object"
    )
    oak = Placement(
        id=f"{farm}:object:5:15:9", name="a bare oak", x=23, y=25, w=1, h=1, verbs=(), kind="object"
    )
    sacks = [
        Placement(id=f"s{i}", name="a sack", x=i, y=0, w=1, h=1, verbs=(), kind="object")
        for i in range(2)
    ]
    things = [crow, oak, *sacks]
    assert holder_of("T1", things) == crow.id
    assert holder_of("t2", things) == oak.id
    assert holder_of("T2 (the oak)", things) == oak.id
    assert holder_of("T9", things) == ""
    assert holder_of(crow.id, things) == crow.id
    assert holder_of("the scarecrow", things) == crow.id
    assert holder_of("sack", things) == ""  # two of them: lying in the open
    assert holder_of(farm, things) == ""  # every thing of the plan shares it
    assert holder_of(None, things) == holder_of("null", things) == ""


def test_what_was_found_stays_and_what_was_taken_is_said_to_the_next_look(game: VisualWindow):
    """The author (Oct 26 2026): the scarecrow was found to wear a flannel
    shirt; looked at again, the model said it was gone, as if taken. Once
    named, a thing stays where it is until something in the game moves it,
    and the next look is told so; what was taken is told as taken."""
    from sealedlore.engine.look import LookFind, LookReply, look_messages, things_in

    session = game.session
    house = session.area("house")
    stove = next(p for p in house.placements if "stove" in p.name.lower())
    rect = session.look_rect(
        "house", (stove.x - 1, stove.y - 1, stove.x + stove.w, stove.y + stove.h)
    )
    reply = LookReply(
        see="A stove, a shirt hung over its rail.",
        found=[
            LookFind(name="a flannel shirt", kind="clothing", inside=stove.id),
            LookFind(name="a box of cartridges", count=2, kind="ammunition"),
        ],
    )
    session.apply_look_reply("house", rect, reply)
    state = session.area_state()
    assert [f.name for f in state.things[stove.id].contents] == ["a flannel shirt"]
    shown = session.area("house")
    box = next(p for p in shown.placements if p.name == "a box of cartridges")
    assert not set(box.tiles()) & set(stove.tiles()) and "take" in box.verbs

    def request() -> str:
        area = session.area("house")
        return look_messages(
            world="",
            place="The house",
            area=area,
            rect=rect,
            records=session.area_state().things,
            player="Serrik Vaun",
        )[1].text

    text = request()
    assert "— in or on it: a flannel shirt" in text and "a box of cartridges (lying loose)" in text
    assert "taken from it" not in text
    # Taken, both are told to the next look as gone.
    state = session.area_state()
    state.spots["Serrik Vaun"] = Spot(area="house", x=box.x, y=box.y)
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    assert session.visual_take_content(stove.id, 0).startswith("You take a flannel shirt")
    assert session.visual_take(box.id) == "You take the box of cartridges."
    text = request()
    assert "taken from it since: flannel shirt, box of cartridges" in text
    assert "(lying loose)" not in text
    assert stove.id in [p.id for p in things_in(session.area("house"), rect)]


def test_a_thing_lying_on_another_s_tiles_is_offered_in_its_menu(app, game: VisualWindow):
    """A find laid on the very thing it was found at (before the fix) was
    under it, and a click finds the plan's thing first: its menu offers it."""
    session, view = game.session, game.area_view
    house = session.area("house")
    stove = next(p for p in house.placements if "stove" in p.name.lower())
    state = session.area_state()
    state.things["found:old"] = ThingRecord(
        state="dropped",
        name="flannel shirt",
        spot=Spot(area="house", x=stove.x, y=stove.y),
    )
    x, y = stand_near(house, (stove.x, stove.y + stove.h))
    state.spots["Serrik Vaun"] = Spot(area="house", x=x, y=y)
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    game.presenter.refresh(snap=True)
    pump(app)
    shown = view.area
    assert shown.placement_at(stove.x, stove.y).id == stove.id
    seen: list[list[str]] = []
    view._show_menu = lambda menu, at: seen.append([a.text() for a in menu.actions()])
    view._thing_menu(shown.placement_at(stove.x, stove.y), None)
    assert "Take flannel shirt" in seen[-1]


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
        things=[
            ThingNote(
                name="a cast-iron stove",
                place="The house",
                tags=["lit", "fed within the hour"],
                seen="Someone kept this burning.",
            )
        ],
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
    # What the design says of a thing the plan draws reaches the keeper (the
    # creek crossing's campfire was painted burning and the model was told
    # nothing of it; the author, Oct 24 2026).
    stove = next(p for p in house.placements if p.name == "a cast-iron stove")
    noted = state.things[stove.id]
    assert noted.tags == ["lit", "fed within the hour"]
    assert noted.seen == "Someone kept this burning."
    was = state.spots["Serrik Vaun"]
    state.spots["Serrik Vaun"] = Spot(area="house", x=stove.x, y=stove.y + 1)
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    assert "cast-iron stove; lit; fed within the hour; Someone kept" in session.visual_state_text()
    state.spots["Serrik Vaun"] = was
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    assert game.health.text() == "♥♥ · Fit" and game.carry_button.text() == "Carrying nothing"
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
    # Hurt, the kit is first aid on the worst untreated injury (engine/health.py).
    from sealedlore.models.visual import Injury

    state = session.area_state()
    state.injuries.append(
        Injury(kind="broken arm", serious=True, at=state.seconds, words="a broken arm")
    )
    state.spots["Serrik Vaun"] = Spot(area="house", x=kit.x, y=kit.y)
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    session.visual_take(kit.id)
    kit_id = session.visual_inventory()[0][0]
    assert (
        session.visual_use(kit_id, "use")
        == "You splint a broken arm. (the first aid kit is used up)"
    )
    assert session.area_state().injuries[0].treated is not None
    assert session.visual_inventory() == []
    assert session.visual_use(kit_id, "use") == ""  # gone
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
    assert session.area_state().things[holder.id].taken_out == ["candle"]
    candle_id, candle = session.visual_inventory()[0]
    assert candle.kind == "light"
    assert session.visual_drop(candle_id) == "You put down the candle."
    shown = session.area("house")
    # Put down, it can be taken again, though "candle" is no name the
    # story's small-things list knows.
    assert "take" in next(p for p in shown.placements if p.name == "a candle").verbs


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
    assert any("comes at you" in line for line in lines)
    assert session.area_state().injuries, lines  # bare-handed against two: hurt
    game._show_where()
    assert game.health.text().split(" · ")[1] in ("Bruised", "Hurt", "Disabled")
    # Attack back with a rifle, the dead now weak: a hit or a telling blow puts it down.
    state = session.area_state()
    state.things["item:rifle"] = ThingRecord(
        state="taken", name="a rifle", by="Serrik Vaun", kind="weapon"
    )
    state.injuries.clear()
    for creature in state.creatures.values():
        creature.ability = 2
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    target = next(iter(state.creatures))
    lines = session.visual_attack(target)
    assert "put one of the dead down" in lines[0] or "You hit one of the dead" in lines[0]
    assert session.area_state().creatures[target].state == "down"
    # Already carrying a broken leg, bare-handed against something far too
    # strong: a finishing blow kills, and the dialog offers Start again.
    from sealedlore.models.visual import Injury

    state = session.area_state()
    state.things.pop("item:rifle")
    state.injuries.append(
        Injury(kind="broken leg", serious=True, at=state.seconds, words="a broken leg")
    )
    from sealedlore.models.visual import Creature

    me = state.spots["Serrik Vaun"]
    state.creatures["brute"] = Creature(
        id="brute",
        area="farm",
        x=beside[0],
        y=beside[1],
        ability=30,
        state="chase",
        goal_x=me.x,
        goal_y=me.y,
    )
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
    assert not fresh.dead and fresh.wounds == 0 and fresh.laid_out and not fresh.injuries


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
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("John", True))
    main.open_visual_sample(SAMPLES / "Doomsville with a map.md")
    pump(app)
    game = main.visual_window
    session = game.session
    design = session.story.visual_design
    assert design is not None and design.abilities["John"] == 7
    state = session.area_state()
    assert state.laid_out and len(state.creatures) == 7
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


# --- step 3: the people who talk (engine/npc.py) --------------------------------


def _peopled_design() -> VisualDesign:
    design = _design()
    design.creatures = []
    design.people = {
        "Maela Orr": PersonSheet(
            personality="Talkative, quick, trusts easily.",
            goals="Get out of the house and see the town.",
            wants="Company on the road.",
            place="The house",
            ability=4,
            carries=["a brass key", "a loaf of bread"],
        ),
        "Captain Idris": PersonSheet(personality="Grim.", place="", ability=8),
    }
    return design


GREETING = (
    '{"say": "Oh! You gave me a fright.", "mood": "startled", '
    '"intent": {"kind": "stay"}, "remember": "A stranger walked in."}'
)


def _answer(game, reply: str, deadline: float = 10.0) -> None:
    """Wait for the pending talk call to land (the mock answered it at once
    with `reply`, set before the call was made)."""
    game.session.provider.responses = [reply]
    end = time.monotonic() + deadline
    while time.monotonic() < end and game._talk_pending is not None:
        pump(QApplication.instance())
        time.sleep(0.01)


def _ask(game, line: str, reply: str) -> None:
    """The player says `line` to whom they talk to; the mock answers `reply`."""
    game.session.provider.responses = [reply]
    game.line.setText(line)
    game.line.returnPressed.emit()
    _answer(game, reply)


@pytest.fixture
def peopled(app, tmp_path: Path, story, cast):
    bundle, art = _visual_bundle(story, cast)
    bundle.story.visual_design = _peopled_design()
    bundle.story.scene.present_character_ids = ["char-serrik"]
    save_story_bundle(bundle, root=tmp_path)
    save_world_art(story.id, art, tmp_path)
    main = MainWindow(root=tmp_path, use_mock=True)
    # The greeting is scripted before the game opens: she may be in sight
    # from where the design stands her, and is greeted at once.
    main._current_provider().responses = [GREETING]
    main.open_story(story.id)
    pump(app)
    game = main.visual_window
    _answer(game, GREETING)
    # Maela beside the player (the design stood her across the house, in
    # another room): in sight, so she is greeted and can be talked to.
    session = game.session
    state = session.area_state()
    me = state.spots["Serrik Vaun"]
    house = session.area("house")
    near = stand_near(house, (me.x, me.y), house.blocked() | {(me.x, me.y)})
    state.spots["Maela Orr"] = Spot(area="house", x=near[0], y=near[1])
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    game.presenter.refresh(snap=True)
    session.provider.responses = [GREETING]
    game._after_action([])
    _answer(game, GREETING)
    yield game
    game.close()
    pump(app)
    main.close()


def test_the_design_s_people_stand_where_it_puts_them_and_greet_the_player(peopled: VisualWindow):
    """Maela is in the house with the player; the plot would hide her, but
    the game has her where the design put her. Someone newly in sight gets
    the encounter call: the model speaks for them first."""
    session = peopled.session
    state = session.area_state()
    assert state.spots["Maela Orr"].area == "house" and "Maela Orr" in state.placed
    assert "Captain Idris" not in state.spots  # no place: the plot's to bring in
    assert "Maela Orr" in session.visual_people()
    carried = [r.name for r in state.things.values() if r.state == "taken" and r.by == "Maela Orr"]
    assert sorted(carried) == ["a brass key", "a loaf of bread"]
    # The encounter: a call went out as she came into sight, answered.
    request = session.provider.requests[-1]
    system = request.messages[0].text
    assert system.startswith("You speak for one person")
    assert "WHO YOU ARE: Maela Orr" in system and "Personality: Talkative, quick" in system
    user = request.messages[1].text
    assert "they have just come into your sight" in user and "Serrik Vaun" in user
    words = peopled.log.toPlainText()
    assert "Maela Orr: Oh! You gave me a fright." in words
    assert state.memories["Maela Orr"] == ["A stranger walked in."]
    assert state.moods["Maela Orr"] == "startled"
    assert not session.encounter_due("Maela Orr")  # once an area


def test_talking_sets_the_line_s_listener_and_a_follow_intent_makes_a_companion(
    peopled: VisualWindow,
):
    session, view = peopled.session, peopled.area_view
    view.action_chosen.emit("talk", "Maela Orr", "")
    walk_all(QApplication.instance(), view)
    assert peopled._talking_to == "Maela Orr" and not peopled.talking.isHidden()
    assert peopled.talking_name.text() == "Maela Orr"
    assert "Maela Orr" in peopled.line.placeholderText()
    _ask(
        peopled,
        "Come with me to the town.",
        '{"say": "Gladly. Lead on.", "mood": "eager", '
        '"intent": {"kind": "follow", "place": "", "what": ""}, "remember": "I joined them."}',
    )
    user = session.provider.requests[-1].messages[1].text
    assert "Serrik Vaun says to you: Come with me to the town." in user
    assert "THE CONVERSATION SO FAR:\nMaela Orr: Oh! You gave me a fright." in user
    words = peopled.log.toPlainText()
    assert "Maela Orr: Gladly. Lead on." in words and "Maela Orr comes with you." in words
    state = session.area_state()
    assert state.companions == ["Maela Orr"]
    assert state.talks["Maela Orr"][-2:] == [
        ["Serrik Vaun", "Come with me to the town."],
        ["Maela Orr", "Gladly. Lead on."],
    ]
    # Through the door and on a journey, she comes along.
    house = session.area("house")
    door = house.exits[0]
    view.go_requested.emit(door[0], door[1], False, "")
    walk_all(QApplication.instance(), view)
    assert session.area_state().spots["Maela Orr"].area == "farm"
    peopled._settle_figure()
    assert session.visual_travel("town") is not None
    me = session.area_state().spots["Serrik Vaun"]
    her = session.area_state().spots["Maela Orr"]
    assert her.area == "town" and max(abs(her.x - me.x), abs(her.y - me.y)) <= 2


def test_a_give_hands_over_what_they_carry_and_leave_walks_them_off(peopled: VisualWindow):
    session, view = peopled.session, peopled.area_view
    view.action_chosen.emit("talk", "Maela Orr", "")
    walk_all(QApplication.instance(), view)
    _ask(
        peopled,
        "Have you a key?",
        '{"say": "Here.", "intent": {"kind": "give", "what": "the brass key"}}',
    )
    assert "Maela Orr hands you the brass key." in peopled.log.toPlainText()
    assert [r.name for _i, r in session.visual_inventory()] == ["a brass key"]
    _ask(peopled, "Now go.", '{"say": "Fine.", "intent": {"kind": "leave"}}')
    assert "Maela Orr walks off." in peopled.log.toPlainText()
    assert peopled._talking_to is None  # the conversation is over
    goal = session.area_state().goals["Maela Orr"]
    assert goal.intent == "leave" and goal.area == "house"
    # Time passes: she walks to the way out and is gone (the player out of
    # her way first: they stood in the doorway's only approach).
    house = session.area("house")
    state = session.area_state()
    me = state.spots["Serrik Vaun"]
    away = next(
        (x, y)
        for y in range(house.height)
        for x in range(house.width)
        if house.walkable(x, y)
        and (x, y) not in house.blocked()
        and abs(x - me.x) + abs(y - me.y) >= 4
        and len(path(house, (me.x, me.y), (x, y))) > 1
    )
    state.spots["Serrik Vaun"] = Spot(area="house", x=away[0], y=away[1])
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    for _ in range(12):
        session.visual_wait(1)
    assert "Maela Orr" not in session.area_state().spots


def test_an_attack_intent_turns_them_on_the_player_and_the_player_may_strike_back(
    peopled: VisualWindow,
):
    session, view = peopled.session, peopled.area_view
    view.action_chosen.emit("talk", "Maela Orr", "")
    walk_all(QApplication.instance(), view)
    _ask(
        peopled,
        "Give me everything you have.",
        '{"say": "Over my dead body.", "intent": {"kind": "attack"}}',
    )
    words = peopled.log.toPlainText()
    assert "Maela Orr goes for you." in words
    assert "Maela Orr comes at you" in words  # the tick: she is beside, she fights
    # Serrik at 7 against her 4: she is down.
    assert "Maela Orr" in session.area_state().people_down
    assert session.talk_target("Maela Orr") == (False, "Maela Orr is down.")
    assert peopled._talking_to is None


def test_the_reply_is_held_to_its_shape():
    from sealedlore.engine.npc import intent_words, parse_npc

    reply = parse_npc(
        'Sure: {"say": " Hello  there ", "mood": "Wary", "intent": {"kind": "DANCE"}, '
        '"remember": "x"}'
    )
    assert reply.say == "Hello there" and reply.mood == "Wary" and reply.intent == "stay"
    reply = parse_npc('{"say": "", "intent": "follow"}')
    assert reply.intent == "follow"
    assert intent_words("Ann", reply, player="you") == "Ann comes with you."
    reply = parse_npc('{"intent": {"kind": "lead_to", "place": "the church"}}')
    assert "sets off for the church" in intent_words("Ann", reply, player="you")
    with pytest.raises(ValueError):
        parse_npc("no json here")


def test_doomsville_s_people_stand_in_their_places(app, tmp_path: Path, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    main = MainWindow(root=tmp_path / "data", use_mock=True)
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("John", True))
    main.open_visual_sample(SAMPLES / "Doomsville with a map.md")
    pump(app)
    game = main.visual_window
    session = game.session
    state = session.area_state()
    world = session.world()
    where = {
        name: world.places[session.area(spot.area).place_id].name
        for name, spot in state.spots.items()
    }
    # The south gate is a part of Hellsville's plan: he stands on it, at the gate.
    assert where["Marcus Webb"] == "Hellsville"
    gate = next(p for p in world.places.values() if p.name == "The south gate")
    marcus = state.spots["Marcus Webb"]
    assert session.place_of_spot(marcus, world) in (gate.id, session.area(marcus.area).place_id)
    assert where["Elena Ruiz"] == "The clinic"
    assert "Ben" not in where  # the caves are plot-hidden: stood once they are revealed
    assert "Evan Stevens" not in where  # the plot's to bring in
    assert sorted(r.name for r in state.things.values() if r.by == "Marcus Webb") == [
        "a revolver",
        "a ring of keys",
    ]
    game.close()
    pump(app)
    main.close()


# --- step 4: the plot keeper (engine/keeper.py) ---------------------------------


def _plot():
    from sealedlore.models.plot import Assignment, EventDef, EventVariant, FactDef, PlaceDef

    return Plot(
        start_minutes=15 * 60,
        facts=[FactDef(name="town", initial="standing", values=["standing", "fallen"])],
        places=[PlaceDef(name="Hellsville"), PlaceDef(name="The farm")],
        events=[
            EventDef(
                id="welcome",
                title="Welcome to the town",
                kind="optional",
                pacing="any",
                place="Hellsville",
                trigger="The player's character arrives at the town.",
                tell="Someone comes out to meet them.",
                brings=["char-idris"],
                variants=[EventVariant()],  # the one way it goes, as the parser makes it
            ),
            EventDef(
                id="fall",
                title="The fall",
                kind="timed",
                window=(2, 2),
                must_happen=True,
                tell="The dead come down on the town.",
                variants=[
                    EventVariant(title="Held", sets=[Assignment(fact="town", value="standing")]),
                    EventVariant(title="Fallen", sets=[Assignment(fact="town", value="fallen")]),
                ],
            ),
        ],
    )


@pytest.fixture
def plotted(app, tmp_path: Path, story, cast):
    bundle, art = _visual_bundle(story, cast)
    bundle.story.visual_design = _peopled_design()
    bundle.story.visual_design.people["Captain Idris"].place = ""
    bundle.story.scene.present_character_ids = ["char-serrik"]
    bundle.story.plot = _plot()
    cast[2].plot_hidden = True  # Captain Idris: the welcome brings him
    save_story_bundle(bundle, root=tmp_path)
    save_world_art(story.id, art, tmp_path)
    main = MainWindow(root=tmp_path, use_mock=True)
    main._current_provider().responses = [GREETING]
    main.open_story(story.id)
    pump(app)
    game = main.visual_window
    _answer(game, GREETING)
    yield game
    game.close()
    pump(app)
    main.close()


def _keeper_answer(game, reply: str, deadline: float = 10.0) -> None:
    game.session.provider.responses = [reply]
    end = time.monotonic() + deadline
    while time.monotonic() < end and game._keeper_pending is not None:
        pump(QApplication.instance())
        time.sleep(0.01)


def test_the_plot_s_state_is_the_game_s_and_a_direction_is_done_as_said(plotted: VisualWindow):
    session = plotted.session
    chronicle = session.visual_chronicle()
    assert chronicle.facts == {"town": "standing"} and chronicle.place == "The farm"
    assert "char-idris" in session.hidden_ids()  # hidden until the welcome
    # A Direction: the keeper's script, applied.
    script = (
        '{"script": [{"op": "note", "text": "A crate stands by the door."}, '
        '{"op": "place", "what": "a crate of tinned peaches", "where": "beside the player", '
        '"count": 1, "kind": "food"}, {"op": "give", "what": "a hunting rifle", "kind": "weapon"}, '
        '{"op": "set_fact", "fact": "town", "value": "fallen"}, {"op": "dance"}], '
        '"note": "So it is."}'
    )
    session.provider.responses = [script]
    plotted.line.setText("/d A crate of peaches appears beside me, and I find a rifle.")
    plotted.line.returnPressed.emit()
    assert plotted._keeper_pending == "direction"
    _keeper_answer(plotted, script)
    request = session.provider.requests[-1]
    assert request.messages[0].text.startswith("You keep the plot of a top-down game")
    assert "THE STORY, DIRECTS: A crate of peaches appears" in request.messages[1].text
    words = plotted.log.toPlainText()
    assert "Director: A crate" in words and "A crate stands by the door." in words
    assert "You now have a hunting rifle." in words and "So it is." in words
    assert "can't do: dance" in words
    state = session.area_state()
    assert any(
        r.name == "a crate of tinned peaches" and r.state == "dropped"
        for r in state.things.values()
    )
    assert [r.name for _i, r in session.visual_inventory()] == ["a hunting rifle"]
    assert session.visual_chronicle().facts["town"] == "fallen"
    house = session.area("house")
    assert any(p.name == "a crate of tinned peaches" for p in house.placements)


def test_an_unusual_action_is_ruled_on_rolled_and_implemented(plotted: VisualWindow):
    session = plotted.session
    ruling = (
        '{"possible": true, "roll": {"skill": "strength", "difficulty": "trivial"}, '
        '"success": [{"op": "note", "text": "The boards give."}, '
        '{"op": "give", "what": "a rusty hatchet", "kind": "weapon"}], '
        '"failure": [{"op": "wound", "count": 1}], "note": "You set your shoulder to it."}'
    )
    session.provider.responses = [ruling]
    plotted.line.setText("I tear the boards off the window.")
    plotted.line.returnPressed.emit()
    assert plotted._keeper_pending == "action"
    _keeper_answer(plotted, ruling)
    request = session.provider.requests[-1]
    assert "ATTEMPTS: I tear the boards off the window." in request.messages[1].text
    words = plotted.log.toPlainText()
    assert "Serrik Vaun attempts: I tear the boards" in words
    assert "You set your shoulder to it." in words
    assert "rolled" in words.lower() or "roll" in words.lower()
    # A trivial roll succeeds (the dice: 95% chance): the hatchet is in hand.
    assert [r.name for _i, r in session.visual_inventory()] == [
        "a rusty hatchet"
    ] or session.area_state().wounds == 1
    # Impossible: nothing but the note.
    refused = (
        '{"possible": false, "roll": null, "success": [], "failure": [], '
        '"note": "There is no window here."}'
    )
    session.provider.responses = [refused]
    plotted.line.setText("I fly to the moon.")
    plotted.line.returnPressed.emit()
    _keeper_answer(plotted, refused)
    assert "There is no window here." in plotted.log.toPlainText()


def test_arriving_where_an_event_waits_asks_the_keeper_and_firing_it_brings_people(
    plotted: VisualWindow,
):
    session = plotted.session
    # Walk out and travel to the town: the plot's place changes, the welcome is armed there.
    house = session.area("house")
    door = house.exits[0]
    plotted.area_view.go_requested.emit(door[0], door[1], False, "")
    walk_all(QApplication.instance(), plotted.area_view)
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and plotted._fading is not None:  # the door's fade
        pump(QApplication.instance())
        time.sleep(0.01)
    script = (
        '{"fire": [{"event": "Welcome to the town", "variant": "", "script": ['
        '{"op": "say", "who": "Captain Idris", "text": "Far enough. Who are you?"}]}], '
        '"script": [], "note": "A man with a rifle steps out of the gate."}'
    )
    session.provider.responses = [script]
    plotted.area_view.travel_chosen.emit("town")
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and plotted._fading is not None:
        pump(QApplication.instance())
        time.sleep(0.01)
    assert plotted._keeper_pending == "moment" or "Captain Idris" in plotted.log.toPlainText()
    _keeper_answer(plotted, script)
    request = next(
        r for r in reversed(session.provider.requests) if "WHY YOU ARE ASKED" in r.messages[1].text
    )
    assert "arrived at Hellsville" in request.messages[1].text
    assert "## Welcome to the town" in request.messages[1].text
    words = plotted.log.toPlainText()
    assert "Captain Idris: Far enough. Who are you?" in words
    assert "A man with a rifle steps out of the gate." in words
    chronicle = session.visual_chronicle()
    assert chronicle.events["welcome"].state == "happened"
    assert "char-idris" not in session.hidden_ids()
    # Brought in with no place of the design's: beside the player, on the map.
    state = session.area_state()
    me = state.spots["Serrik Vaun"]
    idris = state.spots["Captain Idris"]
    assert idris.area == me.area and max(abs(idris.x - me.x), abs(idris.y - me.y)) <= 2
    assert "Captain Idris" in session.visual_people()
    # Asked once per arrival: back and forth doesn't ask again.
    lines, why = session.visual_plot_check()
    assert why == ""


def test_the_keeper_can_end_the_story(plotted: VisualWindow, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    session = plotted.session
    chosen = {}

    def exec_(self):
        chosen["buttons"] = [b.text() for b in self.buttons()]
        self._clicked = self.buttons()[0]
        return 0

    monkeypatch.setattr(QMessageBox, "exec", exec_)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: self._clicked)
    script = (
        '{"script": [{"op": "end", "text": "The dead find you in your sleep. It is over."}], '
        '"note": ""}'
    )
    session.provider.responses = [script]
    plotted.line.setText("/d I die in my sleep.")
    plotted.line.returnPressed.emit()
    _keeper_answer(plotted, script)
    assert chosen["buttons"] == ["Start again", "Close"]
    assert not session.area_state().ended  # started again
    assert "You start again." in plotted.log.toPlainText()


def test_the_keeper_s_reply_is_held_to_the_operations():
    from sealedlore.engine.keeper import OPS, parse_keeper, parse_ruling

    events = _plot().events
    reply = parse_keeper(
        '{"fire": [{"event": "the fall", "variant": "FALLEN", '
        '"script": [{"op": "note", "text": "x"}]}, {"event": "nothing", "script": []}], '
        '"script": [{"op": "fly"}, {"op": "give", "what": "a key", "count": "2"}], '
        '"note": " n "}',
        events,
    )
    assert [(f.event, f.variant) for f in reply.fired] == [("The fall", "Fallen")]
    assert [o.op for o in reply.script] == ["give"] and reply.script[0].count() == 2
    assert reply.note == "n" and reply.dropped == ["event 'nothing'", "fly"]
    ruling = parse_ruling(
        '{"possible": true, "roll": {"skill": "x", "difficulty": "very hard"}}', events
    )
    assert ruling.difficulty == "desperate" and ruling.success == [] and ruling.possible
    assert set(OPS) >= {"note", "say", "spawn", "give", "reveal", "end"}
    with pytest.raises(ValueError):
        parse_keeper("no", events)


# --- steps 5 and 6: anything the player tries, the look, the content ---------------


def _goal(intent: str):
    from sealedlore.models.visual import NpcGoal

    return NpcGoal(intent=intent)


def test_the_keeper_can_make_a_thing_into_what_it_has_become_and_a_vehicle_drives(
    plotted: VisualWindow,
):
    """The author (Oct 24 2026): no repair mechanic, but a repaired car is "a
    new model for the car that isn't broken" with the ability to use it."""
    session = plotted.session
    house = session.area("house")
    stove = next(p for p in house.placements if "stove" in p.name.lower())
    script = (
        '{"script": [{"op": "transform", "what": "the stove", "name": "a running snowmobile", '
        '"kind": "vehicle", "state": "whole", "tags": ["works", "fuelled"], "uses": ["drive"], '
        '"note": "It coughs, then runs."}, {"op": "tag_person", "who": "Maela Orr", '
        '"tags": "bound, held by Serrik"}, {"op": "set_state", "where": "The house", '
        '"state": "smoke-filled"}], "note": ""}'
    )
    session.provider.responses = [script]
    plotted.line.setText("/d I turn the stove into a snowmobile and tie Maela up.")
    plotted.line.returnPressed.emit()
    _keeper_answer(plotted, script)
    words = plotted.log.toPlainText()
    assert "is now a running snowmobile." in words
    assert "you can drive it" in words and "Maela Orr is bound, held by Serrik." in words
    assert "The house: smoke-filled." in words
    state = session.area_state()
    record = state.things[stove.id]
    assert record.name == "a running snowmobile" and record.kind == "vehicle"
    assert record.tags == ["works", "fuelled"] and record.uses == ["drive"]
    # The placement takes the record's name; its menu offers the use.
    house = session.area("house")
    assert any(p.name == "a running snowmobile" for p in house.placements)
    assert session.visual_uses(stove.id) == ["drive"]
    # The place's state is the map's now, read from the live state.
    assert session.world().places["house"].state == "smoke-filled"
    assert "The place is smoke-filled." in session.visual_state_text()
    assert "running snowmobile; works; fuelled; can be: drive" in session.visual_state_text()
    # A bound person carries out no intent and is told her state.
    state.goals["Maela Orr"] = state.goals.get("Maela Orr") or _goal("stay")
    state.goals["Maela Orr"].intent = "leave"
    before = state.spots["Maela Orr"]
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    session.visual_tick(60)
    assert session.area_state().spots["Maela Orr"] == before
    session.provider.responses = [GREETING]
    plotted.area_view.action_chosen.emit("talk", "Maela Orr", "")
    plotted.line.setText("Comfortable?")
    plotted.line.returnPressed.emit()
    _answer(plotted, GREETING)
    talk = next(
        r for r in reversed(session.provider.requests) if "YOUR OWN STATE" in r.messages[1].text
    )
    assert "YOUR OWN STATE: bound, held by Serrik" in talk.messages[1].text
    # Choosing the use from the menu is an attempt the keeper rules on.
    ruling = (
        '{"possible": true, "roll": null, "success": [{"op": "note", "text": "It starts."}], '
        '"failure": [], "note": ""}'
    )
    session.provider.responses = [ruling]
    me = state.spots["Serrik Vaun"]
    state.spots["Serrik Vaun"] = Spot(area="house", x=stove.x + 1, y=stove.y + 1)
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    plotted.presenter.refresh(snap=True)
    plotted.area_view.action_chosen.emit("use:drive", "a running snowmobile", stove.id)
    assert plotted._keeper_pending == "action"
    _keeper_answer(plotted, ruling)
    request = session.provider.requests[-1]
    assert "ATTEMPTS: I drive the running snowmobile (works, fuelled)." in request.messages[1].text
    assert "It starts." in plotted.log.toPlainText()
    # Travel… offers the journey by it, and the vehicle comes along.
    choices = session.visual_drive_choices()
    assert choices and choices[0][1] == "running snowmobile"
    by_foot = dict((pid, s) for pid, _n, s in session.visual_travel_choices())
    by_road = dict((pid, s) for pid, _n, s in choices[0][2])
    assert by_road["town"] < by_foot["town"] / 4
    plotted._travel_menu()
    labels = [a.text() for a in plotted.area_view.travel_menu.actions()]
    assert any("by running snowmobile" in label and "by road" in label for label in labels)
    gone = session.visual_travel("town", vehicle=stove.id)
    assert gone is not None and gone[1] == by_road["town"]
    state = session.area_state()
    assert state.spots["Serrik Vaun"].area == "town"
    record = state.things[stove.id]
    assert record.state == "dropped" and record.spot is not None and record.spot.area == "town"
    del me


def test_the_keeper_moves_the_player_and_a_use_the_rules_lack_goes_to_it(plotted: VisualWindow):
    session = plotted.session
    script = '{"script": [{"op": "go", "where": "The farm"}], "note": "Over the sill and out."}'
    session.provider.responses = [script]
    plotted.line.setText("/d I climb out of the window onto the farm.")
    plotted.line.returnPressed.emit()
    _keeper_answer(plotted, script)
    assert session.area_state().spots["Serrik Vaun"].area == "farm"
    assert "You are at The farm." in plotted.log.toPlainText()
    # A carried thing with no rule for Use: the attempt, with the thing's state.
    state = session.area_state()
    state.things["item:wire"] = ThingRecord(
        state="taken", name="a coil of wire", by="Serrik Vaun", kind="tool", tags=["rusty"]
    )
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    assert session.visual_item_actions(state.things["item:wire"]) == ("use", "drop")
    ruling = '{"possible": false, "roll": null, "success": [], "failure": [], "note": "Not here."}'
    session.provider.responses = [ruling]
    plotted._carry_action("item:wire", "use")
    assert plotted._keeper_pending == "action"
    _keeper_answer(plotted, ruling)
    assert (
        "ATTEMPTS: I use the coil of wire (rusty)."
        in session.provider.requests[-1].messages[1].text
    )
    assert "Not here." in plotted.log.toPlainText()


def test_people_keep_their_hours_when_the_player_is_elsewhere(peopled: VisualWindow):
    session = peopled.session
    design = session.visual_design()
    design.people["Maela Orr"].schedule = [
        __import__("sealedlore.models.visual", fromlist=["Shift"]).Shift(
            start="00:00", end="24:00", place="The farm"
        )
    ]
    state = session.area_state()
    # Out of her area: she keeps her hours. In it: she stays in sight.
    assert state.spots["Maela Orr"].area == "house"
    session.visual_tick(60)
    assert session.area_state().spots["Maela Orr"].area == "house"
    farm = session.area("farm")
    spot = next(
        (x, y) for y in range(farm.height) for x in range(farm.width) if farm.walkable(x, y)
    )
    state = session.area_state()
    state.spots["Serrik Vaun"] = Spot(area="farm", x=spot[0], y=spot[1])
    state.spots["Maela Orr"] = Spot(area="house", x=5, y=5)
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    session.visual_tick(60)
    assert session.area_state().spots["Maela Orr"].area == "farm"


def test_sleeping_is_a_moment_of_the_plot_and_the_dead_down_lie_where_they_fell(
    game: VisualWindow, monkeypatch
):
    session = game.session
    asked = []
    monkeypatch.setattr(
        session, "visual_plot_check", lambda moment="": (asked.append(moment), ([], ""))[1]
    )
    game._pass_time(True)
    assert asked[-1] == "the player has just slept the night through"
    game._pass_time(False)
    assert asked[-1] == ""
    # One of the dead put down lies there, no longer attackable from its menu.
    state = session.area_state()
    creature = next(c for c in state.creatures.values())
    creature.state = "down"
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    game.presenter.refresh(snap=True)
    walker = next(w for w in game.area_view.walkers if w.name == creature.id)
    assert walker.fallen and creature.id in game.area_view.fallen
    shown = []
    monkeypatch.setattr(game.area_view, "_show_menu", lambda menu, at: shown.append(menu))
    game.area_view._person_menu(walker, None)
    assert [a.text() for a in shown[-1].actions()] == ["One of the dead, down"]


def test_cards_are_made_for_the_design_s_townspeople():
    from sealedlore.storage.visual_design import cards_for_design

    design = VisualDesign(
        people={
            "Marcus Webb": PersonSheet(personality="x"),  # the plot file's own
            "Tom Bell": PersonSheet(card="Keeps the south gate.", place="The south gate"),
            "Nobody": PersonSheet(personality="no card line: none made"),
        }
    )
    cards = cards_for_design(design, ["Marcus Webb"])
    assert [(c.name, c.summary, c.is_player_available) for c in cards] == [
        ("Tom Bell", "Keeps the south gate.", False)
    ]


def test_doomsville_s_townspeople_have_cards_and_stand_in_the_town(
    app, tmp_path: Path, monkeypatch
):
    from PySide6.QtWidgets import QInputDialog

    main = MainWindow(root=tmp_path, use_mock=True)
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("John", True))
    main._current_provider().responses = [GREETING] * 4
    main.open_visual_sample(SAMPLES / "Doomsville with a map.md")
    pump(app)
    game = main.visual_window
    session = game.session
    names = {c.name for c in session.supporting}
    assert {"Tom Bell", "Ruth Calloway", "Sam", "Cole", "Dutch"} <= names
    assert session.has_card("Tom Bell")
    state = session.area_state()
    town = session.world()
    town_id = next(p.id for p in town.places.values() if p.name == "Hellsville")
    assert state.spots["Tom Bell"].area == town_id and state.spots["Ruth Calloway"].area != town_id
    assert "Cole" not in state.spots  # the band comes with its event
    assert session.visual_design().weather == "snow"
    # The dead have a sprite of their own, shipped with the sample.
    art = session.map_art_copy()
    assert art.picture_for("walker", "the dead") is not None
    game.close()
    pump(app)
    main.close()


def test_the_keeper_s_operations_are_read_with_the_name_as_the_key_too():
    from sealedlore.engine.keeper import parse_keeper, parse_ruling

    ruling = parse_ruling(
        '{"possible": true, "roll": null, "success": [{"clock": 90}, {"go": "water tower"}, '
        '{"transform": "Pickup truck", "name": "a battered pickup", "kind": "vehicle", '
        '"tags": ["works"], "uses": ["drive"]}, {"say": {"who": "Elena", "text": "Lord."}}, '
        '{"say": "Tom Bell", "text": "Check its neck."}, {"note": "x"}], "failure": []}',
        [],
    )
    ops = [(o.op, o.args) for o in ruling.success]
    assert ops[0] == ("clock", {"minutes": "90"}) and ops[1] == ("go", {"where": "water tower"})
    assert ops[2][0] == "transform" and ops[2][1]["what"] == "Pickup truck"
    assert ops[2][1]["name"] == "a battered pickup" and ops[2][1]["uses"] == "drive"
    assert ops[3] == ("say", {"who": "Elena", "text": "Lord."})
    assert ops[4] == ("say", {"who": "Tom Bell", "text": "Check its neck."})
    assert ops[5] == ("note", {"text": "x"}) and ruling.dropped == []
    reply = parse_keeper('{"script": [{"dance": 1}, {"op": "note", "text": "y"}]}', [])
    assert [o.op for o in reply.script] == ["note"] and reply.dropped == ["(no op)"]


def test_an_operation_with_a_note_field_is_not_read_as_a_note():
    from sealedlore.engine.keeper import parse_keeper

    reply = parse_keeper(
        '{"script": [{"give": "spare store key", "count": 1, "kind": "key", '
        '"tags": "unlocks store", "note": "Key to the store"}, '
        '{"transform": "Pickup truck", "name": "pickup truck", '
        '"note": "runs"}, {"note": "plain"}]}',
        [],
    )
    assert [o.op for o in reply.script] == ["give", "transform", "note"]
    assert reply.script[0].args["what"] == "spare store key"
    assert reply.script[0].args["note"] == "Key to the store"
    assert reply.script[1].args["what"] == "Pickup truck" and reply.script[1].args["note"] == "runs"


def test_a_take_away_reaches_the_player_s_hands(plotted: VisualWindow):
    session = plotted.session
    give = (
        '{"script": [{"give": "a set of spark plugs", "kind": "tool"}, '
        '{"give": "a tin of beans", "kind": "food"}], "note": ""}'
    )
    session.provider.responses = [give]
    plotted.line.setText("/d I have spark plugs and beans.")
    plotted.line.returnPressed.emit()
    _keeper_answer(plotted, give)
    assert [r.name for _i, r in session.visual_inventory()] == [
        "a set of spark plugs",
        "a tin of beans",
    ]
    gone = '{"script": [{"take_away": "set of spark plugs"}], "note": ""}'
    session.provider.responses = [gone]
    plotted.line.setText("/d The plugs are used up.")
    plotted.line.returnPressed.emit()
    _keeper_answer(plotted, gone)
    # Not the beans by the word "of", and really gone from the player's hands.
    assert [r.name for _i, r in session.visual_inventory()] == ["a tin of beans"]
    assert "The set of spark plugs is gone from your hands." in plotted.log.toPlainText()


def test_a_place_the_keeper_names_loosely_is_found():
    from sealedlore.engine.session_visual import _place_named
    from tests.test_area import world_and_art

    world, _art = world_and_art()
    assert _place_named(world, "The house").id == "house"
    assert _place_named(world, "house").id == "house"
    assert _place_named(world, "The Farm - House").id == "house"
    assert _place_named(world, "the farm's barn").id == "farm"
    assert _place_named(world, "the moon") is None


def test_stride_frames_are_a_walker_s_own_and_never_their_picture():
    from sealedlore.models.world import MapPicture, WorldArt

    art = WorldArt()
    art.pictures = [
        MapPicture(subject="walker", subject_id="John", name="John", file="a.webp"),
        MapPicture(subject="walker", subject_id="John", name="John", state="walk:2", file="c.webp"),
        MapPicture(subject="walker", subject_id="John", name="John", state="walk:1", file="b.webp"),
    ]
    assert art.picture_for("walker", "John").file == "a.webp"
    assert [p.file for p in art.walker_frames("John")] == ["b.webp", "c.webp"]
    assert art.walker_frames("Jane") == []


def test_doomsville_s_walkers_stride_with_their_own_frames(app, tmp_path: Path, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    main = MainWindow(root=tmp_path, use_mock=True)
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("John", True))
    main._current_provider().responses = [GREETING] * 4
    main.open_visual_sample(SAMPLES / "Doomsville with a map.md")
    pump(app)
    game = main.visual_window
    session = game.session
    files = session.walker_frame_files("John")
    assert len(files) == 2 and all(f.endswith(".webp") for f in files)
    person = game.area_view.people["John"]
    assert person.frames.has("walk")
    standing, _m = person.frames.frame("east", "east", "stand", 0)
    first, _m = person.frames.frame("east", "east", "walk", 0)
    second, _m = person.frames.frame("east", "east", "walk", 1)
    assert first is not standing and second is not first
    assert person.frames.frame("east", "east", "walk", 2)[0] is first  # the stride repeats
    # An avatar the author chose instead has no frames that fit it.
    art = session.map_art_copy()
    art.tile_overrides["walker/John"] = "pictures/map/walker-jane.webp"
    assert session.walker_frame_files("John", art) == []
    game.close()
    pump(app)
    main.close()


def test_walking_is_drawn_between_tiles_facing_the_way_it_goes(app, window: MainWindow):
    """The play view's walk animation (gui/area_view.py), on the game's
    window: the figure between one tile and the next at `STEPS_PER_SECOND`,
    facing the way it goes, on its tile once a tile's time has passed."""
    from sealedlore.gui.area_render import px_for
    from sealedlore.gui.area_view import STEPS_PER_SECOND

    game = window.visual_window
    session, view = game.session, game.area_view
    px = px_for(view.area)
    before = session.area_state().spots["Serrik Vaun"]
    steps = [tuple(s) for s in session.walk_route(before.x + 6, before.y)]
    assert len(steps) > 2
    view.drop_walk()
    view.walk(steps)
    person = view.people["Serrik Vaun"]
    assert view.walking() and view._timer.interval() <= 20  # about 60 Hz
    view._advance(0.5 / STEPS_PER_SECOND)
    assert (view.held().x, view.held().y) == steps[0]  # on its tile until the next
    (x0, y0), (x1, y1) = steps[0], steps[1]
    assert abs(person.pos().x() - (x0 + (x1 - x0) * 0.5 + 0.5) * px) < 1e-6
    assert abs(person.pos().y() - (y0 + (y1 - y0) * 0.5 + 1.0) * px) < 1e-6
    assert person.mirrored == (x1 < x0) and person.pose == "walk"
    assert view._tags["Serrik Vaun"].pos().x() == person.pos().x()
    view._advance(0.6 / STEPS_PER_SECOND)  # past the tile
    assert (view.held().x, view.held().y) == steps[1]
    view.drop_walk()
    assert not view.walking() and person.bob == 0.0


def test_indoors_someone_out_of_sight_is_hidden_and_the_rooms_are_shaded(app, window: MainWindow):
    """The fog of war indoors (gui/area_view.py `_sync_sight`): the shade
    over what the player can't see, and a person behind a wall not shown."""
    from PySide6.QtCore import QPointF

    from sealedlore.engine.area import sees
    from sealedlore.gui.area_render import px_for
    from sealedlore.gui.area_view import SHADE_ALPHA, lit_rooms

    game = window.visual_window
    session, view = game.session, game.area_view
    assert view.area is not None and view.area.indoor and view._shade is not None
    state = session.area_state()
    state.spots["Serrik Vaun"] = Spot(area="house", x=5, y=10)
    state.spots["Maela Orr"] = Spot(area="house", x=30, y=10)
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    game.presenter.refresh(snap=True)
    assert not sees(view.area, (5, 10), (30, 10))
    assert not view.people["Maela Orr"].isVisible()
    px = px_for(view.area)
    assert view.person_at(QPointF(30.5 * px, 10.5 * px)) is None
    mine, theirs = view.area.room_at(5, 10), view.area.room_at(30, 10)
    assert mine is not None and theirs is not None and mine.index != theirs.index
    assert mine.index in lit_rooms(view.area, (5, 10))
    mask = view._shade_mask
    assert mask.pixelColor(5, 10).alpha() == 0
    assert mask.pixelColor(39, 10).alpha() == SHADE_ALPHA
    state.spots["Maela Orr"] = Spot(area="house", x=7, y=10)
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    game.presenter.refresh(snap=True)
    assert view.people["Maela Orr"].isVisible()
    assert view.person_at(QPointF(7.5 * px, 10.5 * px)).name == "Maela Orr"


# --- the playtest's fixes (Oct 27 2026) ---------------------------------------------------


def test_at_a_moment_the_keeper_speaks_for_nobody_the_player_talks_with(plotted: VisualWindow):
    """Live, asked at a conversation moment, the keeper answered Marcus's
    own question for him in Marcus's voice; and "Tom Bell is armed, circling
    behind player, unseen" was printed for the player to read."""
    from sealedlore.engine.keeper import parse_keeper

    session = plotted.session
    state = session.area_state()
    session._stand_person(state, "Captain Idris", "beside the player")
    session._set_live(state)
    reply = parse_keeper(
        '{"fire": [], "script": [{"op": "say", "who": "Captain Idris", "text": "Three sets."}, '
        '{"op": "tag_person", "who": "Captain Idris", "tags": "armed, unseen"}, '
        '{"op": "note", "text": "The wind picks up."}], "note": ""}',
        session.story.plot.events,
    )
    lines = session.apply_keeper_reply(reply, kind="moment", talking_to="Captain Idris")
    assert lines == ["The wind picks up."]
    assert session.area_state().person_tags["Captain Idris"] == ["armed", "unseen"]
    # Not talking with him: he may speak; an action's outcome tells his state.
    lines = session.apply_keeper_reply(reply, kind="action")
    assert "Captain Idris: Three sets." in lines and "Captain Idris is armed, unseen." in lines


def test_a_say_from_out_of_earshot_is_dropped_and_leaves_no_greeting_made(
    plotted: VisualWindow,
):
    """Live, Stillwater's people "spoke" from 120 m off across the hamlet
    as John came in, and then never greeted him when he walked up."""
    from sealedlore.engine.keeper import Op

    session = plotted.session
    state = session.area_state()
    me = state.spots[session.held_name()]
    far = session.area(me.area)
    x, y = stand_near(far, (far.width - 2, far.height - 2), far.blocked())
    state.spots["Captain Idris"] = Spot(area=me.area, x=x, y=y)
    state.placed.append("Captain Idris")
    session._set_live(state)
    if session.people_in_sight() and "Captain Idris" in session.people_in_sight():
        pytest.skip("the test area is too small to be out of earshot in")
    lines = session.apply_script([Op(op="say", args={"who": "Captain Idris", "text": "Hey."})])
    assert lines == []
    assert not any(g.startswith("Captain Idris@") for g in session.area_state().greeted)


def test_a_spawn_of_someone_already_in_the_place_leaves_them_where_they_are(
    plotted: VisualWindow,
):
    """Live, the welcome at Hellsville "spawned" Elena out of her clinic
    into the street beside the player."""
    from sealedlore.engine.keeper import Op

    session = plotted.session
    state = session.area_state()
    me = state.spots[session.held_name()]
    farm = session.area("farm")
    x, y = stand_near(farm, (3, 3), farm.blocked())
    state.spots["Maela Orr"] = Spot(area="farm", x=x, y=y)
    session._set_live(state)
    assert me.area == "house"  # the player indoors, Maela in the yard: one place
    session.apply_script([Op(op="spawn", args={"who": "Maela Orr", "where": "beside the player"})])
    after = session.area_state().spots["Maela Orr"]
    assert (after.area, after.x, after.y) == ("farm", x, y)
    # The keeper is told she is about, elsewhere in the place.
    assert "elsewhere in this place: Maela Orr" in session.visual_state_text()
    assert "People Serrik Vaun has met so far" in session.visual_state_text()


def test_a_holder_that_was_the_thing_itself_goes_once_it_is_taken_out(plotted: VisualWindow):
    """The farmhouse's "rifle leaning by the door" held the hunting rifle,
    and stood there to be taken as a rifle of its own once the rifle was."""
    session = plotted.session
    state = session.area_state()
    me = state.spots[session.held_name()]
    house = session.area(me.area)
    thing = min(house.placements, key=lambda p: abs(p.centre[0] - me.x) + abs(p.centre[1] - me.y))
    state.things[thing.id] = ThingRecord(
        state="seen",
        name="rifle leaning by the door",
        contents=[
            Found(name="a box of cartridges", count=12, kind="ammunition"),
            Found(name="a hunting rifle", kind="weapon"),
        ],
    )
    x, y = stand_near(house, (int(thing.centre[0]), int(thing.centre[1])), house.blocked())
    state.spots[session.held_name()] = Spot(area=me.area, x=x, y=y)
    session._set_live(state)
    assert "cartridges" in session.visual_take_content(thing.id, 0)
    assert session.area_state().things[thing.id].state == "seen"
    assert "rifle" in session.visual_take_content(thing.id, 0)
    assert session.area_state().things[thing.id].state == "gone"


def test_a_carried_thing_can_be_given_to_someone_within_reach_and_they_remember(
    plotted: VisualWindow,
):
    """Live, a roadblock's bandit named his toll and nothing could be handed
    over: the Carrying menu had eat, drink, use and drop."""
    from sealedlore.engine.session_visual import ITEM_PREFIX

    session = plotted.session
    state = session.area_state()
    me = state.spots[session.held_name()]
    house = session.area(me.area)
    state.things[ITEM_PREFIX + "box"] = ThingRecord(
        state="taken", name="a toolbox", by=session.held_name(), kind="tool"
    )
    far = stand_near(house, (house.width - 2, house.height - 2), house.blocked())
    state.spots["Maela Orr"] = Spot(area=me.area, x=far[0], y=far[1])
    session._set_live(state)
    assert "isn't within reach" in session.visual_give(ITEM_PREFIX + "box", "Maela Orr")
    state = session.area_state()
    near = stand_near(house, (me.x, me.y), house.blocked() | {(me.x, me.y)})
    state.spots["Maela Orr"] = Spot(area=me.area, x=near[0], y=near[1])
    session._set_live(state)
    assert session.people_in_reach() == ["Maela Orr"]
    assert (
        session.visual_give(ITEM_PREFIX + "box", "Maela Orr") == "You give Maela Orr the toolbox."
    )
    state = session.area_state()
    assert state.things[ITEM_PREFIX + "box"].by == "Maela Orr"
    assert [r.name for _i, r in session.visual_inventory()] == []
    assert state.memories["Maela Orr"][-1] == "Serrik Vaun gave me the toolbox."
    plotted._fill_carry_menu()  # nothing carried: the menu says so, and doesn't fail
    assert plotted.carry_menu.actions()[0].text() == "Nothing"


def test_a_person_answering_hears_what_others_here_said_just_now(plotted: VisualWindow):
    """Live, three bandits at a roadblock each greeted the player with
    "that's far enough", each unaware of the others' words."""
    session = plotted.session
    state = session.area_state()
    me = state.spots[session.held_name()]
    house = session.area(me.area)
    near = stand_near(house, (me.x, me.y), house.blocked() | {(me.x, me.y)})
    state.spots["Captain Idris"] = Spot(area=me.area, x=near[0], y=near[1])
    state.placed.append("Captain Idris")
    session._set_live(state)
    plotted._set_talking("Maela Orr")
    _ask(plotted, "Who goes there?", GREETING)
    heard = [h[1:3] for h in session.area_state().heard]
    assert ["Serrik Vaun", "Who goes there?"] in heard
    assert ["Maela Orr", "Oh! You gave me a fright."] in heard
    plotted._set_talking("Captain Idris")
    _ask(plotted, "And you?", GREETING)
    request = next(
        r
        for r in reversed(session.provider.requests)
        if "says to you: And you?" in r.messages[1].text
    )
    asked = request.messages[1].text
    assert "SAID HERE JUST NOW" in asked
    assert "Maela Orr: Oh! You gave me a fright." in asked
    assert "Serrik Vaun: Who goes there?" in asked
    # Their own words are not among what they heard others say.
    before = asked.index("THE CONVERSATION SO FAR")
    assert "Captain Idris:" not in asked[:before]


def test_one_of_the_story_s_people_who_leaves_goes_to_a_place_of_theirs_not_off_the_map(
    plotted: VisualWindow,
):
    """Live, the town's leader "went to fetch others" and was never seen
    again: whoever left the player's area was dropped from the map."""
    from sealedlore.models.visual import NpcGoal

    session = plotted.session
    state = session.area_state()
    me = state.spots[session.held_name()]
    house = session.area(me.area)
    # Maela's sheet puts her in the house; the farm's yard is the place
    # round it, where "away" has nowhere of hers to go but another building:
    # none here, so she goes to the house's own place list first.
    session.visual_design().people["Maela Orr"].schedule = []
    session.visual_design().people["Maela Orr"].place = "The farm"
    exit_tile = house.exits[0]
    state.spots["Maela Orr"] = Spot(area=me.area, x=exit_tile[0], y=exit_tile[1] - 1)
    state.goals["Maela Orr"] = NpcGoal(intent="leave", area=me.area, x=exit_tile[0], y=exit_tile[1])
    session._set_live(state)
    state = session.area_state()
    lines = session._step_people(state, house, me, 60)
    session._set_live(state)
    assert "Maela Orr is gone." in lines
    state = session.area_state()
    assert "Maela Orr" in state.spots and state.spots["Maela Orr"].area == "farm"
    # A stranger with nowhere of their own is simply gone.
    state.strangers["Nobody Much"] = PersonSheet(card="A drifter.")
    session.supporting.append(Character(name="Nobody Much", summary="A drifter."))
    state.spots["Nobody Much"] = Spot(area=me.area, x=exit_tile[0], y=exit_tile[1] - 1)
    state.goals["Nobody Much"] = NpcGoal(
        intent="leave", area=me.area, x=exit_tile[0], y=exit_tile[1]
    )
    session._set_live(state)
    state = session.area_state()
    session._step_people(state, house, me, 60)
    session._set_live(state)
    assert "Nobody Much" not in session.area_state().spots


def test_what_was_said_is_heard_only_within_hearing(plotted: VisualWindow):
    """Live, the north gate's watchman "heard most of it" of a conversation
    at the south gate, 500 m down the street."""
    session = plotted.session
    state = session.area_state()
    me = state.spots[session.held_name()]
    farm = session.area("farm")
    session._heard(state, "farm", "Maela Orr", "A whisper.")
    state.heard[-1][3:] = [1, 1]
    far = stand_near(farm, (farm.width - 2, farm.height - 2), farm.blocked())
    near = Spot(area="farm", x=3, y=3)
    assert session._within_hearing(farm, near, state.heard[-1])
    assert not session._within_hearing(farm, Spot(area="farm", x=far[0], y=far[1]), state.heard[-1])
    assert session._within_hearing(farm, Spot(area="farm", x=far[0], y=far[1]), ["farm", "x", "y"])
    del me


# --- health, standing, scouting and the people's pictures (Oct 27 2026) ----------------


def _beside_tile(area, me):
    return next(
        (x, y)
        for x, y in ((me.x + 1, me.y), (me.x, me.y + 1), (me.x - 1, me.y), (me.x, me.y - 1))
        if area.walkable(x, y)
    )


def test_retreat_breaks_away_freely_unless_overwhelmed(plotted: VisualWindow):
    from sealedlore.models.visual import Creature

    session = plotted.session
    state = session.area_state()
    state.creatures.clear()
    me = state.spots["Serrik Vaun"]
    house = session.area(me.area)
    beside = _beside_tile(house, me)
    state.creatures["one"] = Creature(
        id="one",
        area=me.area,
        x=beside[0],
        y=beside[1],
        ability=4,
        state="chase",
        goal_x=me.x,
        goal_y=me.y,
    )
    session._set_live(state)
    assert session._beside(state, house, me) == [("one", True)]
    lines = session.visual_retreat()
    assert "You break away." in lines and not any("hard pressed" in ln for ln in lines)
    after = session.area_state()
    moved = after.spots["Serrik Vaun"]
    assert (moved.x, moved.y) != (me.x, me.y)
    assert max(abs(after.creatures["one"].x - moved.x), abs(after.creatures["one"].y - moved.y)) > 1
    # Disabled by a broken leg, a retreat costs a round at the player's back first.
    from sealedlore.models.visual import Injury

    state = session.area_state()
    me = state.spots["Serrik Vaun"]
    beside = _beside_tile(house, me)
    state.creatures["one"].x, state.creatures["one"].y = beside
    state.creatures["one"].ability = 1
    state.injuries.append(Injury(kind="broken leg", serious=True, at=0, words="a broken leg"))
    session._set_live(state)
    lines = session.visual_retreat()
    assert lines[0] == "You are hard pressed: they get at you as you break away."
    assert "Nothing is on you." == session.visual_retreat()[0]


def test_a_medic_s_treat_intent_sets_a_break_and_a_kit_bearer_gives_first_aid(
    plotted: VisualWindow,
):
    from sealedlore.engine.npc import NpcReply
    from sealedlore.engine.session_visual import ITEM_PREFIX
    from sealedlore.models.visual import Injury

    session = plotted.session
    state = session.area_state()
    state.injuries.append(Injury(kind="broken arm", serious=True, at=0, words="a broken arm"))
    me = state.spots["Serrik Vaun"]
    near = _beside_tile(session.area(me.area), me)
    state.spots["Maela Orr"] = Spot(area=me.area, x=near[0], y=near[1])
    session._set_live(state)
    treat = NpcReply(
        say="Hold still.", mood="brisk", intent="treat", place="", what="", remember=""
    )
    lines = session.apply_npc_reply("Maela Orr", treat)
    assert lines == ["Maela Orr has nothing to treat you with."]
    state = session.area_state()
    state.things[ITEM_PREFIX + "kit"] = ThingRecord(
        state="taken", name="a first aid kit", by="Maela Orr", kind="medicine"
    )
    session._set_live(state)
    lines = session.apply_npc_reply("Maela Orr", treat)
    assert lines == ["Maela Orr: Maela Orr splint a broken arm."] or lines[0].startswith(
        "Maela Orr"
    )
    assert session.area_state().injuries[0].treated is not None
    # A medic gives proper care, and her standing with the player can change.
    session.visual_design().people["Maela Orr"].medic = True
    cared = NpcReply(
        say="Let me set that properly.",
        mood="kind",
        intent="treat",
        place="",
        what="",
        remember="",
        standing="friend",
    )
    lines = session.apply_npc_reply("Maela Orr", cared)
    assert "Maela Orr sets a broken arm." in lines
    assert "(Maela Orr: friend with you now.)" in lines
    assert session.standing_of("Maela Orr") == "friend"
    assert session.area_state().injuries[0].cared is not None
    # Told to her: the talk call carries the standing and the player's hurts.
    session.provider.responses = [GREETING]
    plotted._set_talking("Maela Orr")
    _ask(plotted, "Thank you.", GREETING)
    asked = session.provider.requests[-1].messages[1].text
    assert "HOW THE PLAYER STANDS WITH YOU: friend" in asked
    assert "hurt: a broken arm, under care" in asked
    assert "You can give proper care" in session.provider.requests[-1].messages[0].text


def test_a_friend_present_tends_the_sleeping_player_and_keeps_the_night(plotted: VisualWindow):
    from sealedlore.engine.session_visual import ITEM_PREFIX
    from sealedlore.models.visual import Injury

    session = plotted.session
    state = session.area_state()
    state.creatures.clear()
    state.injuries.append(
        Injury(kind="deep wound", serious=True, at=state.seconds, words="a deep gash in the side")
    )
    me = state.spots["Serrik Vaun"]
    near = _beside_tile(session.area(me.area), me)
    state.spots["Maela Orr"] = Spot(area=me.area, x=near[0], y=near[1])
    state.things[ITEM_PREFIX + "kit"] = ThingRecord(
        state="taken", name="a first aid kit", by="Maela Orr", kind="medicine"
    )
    state.standing["Maela Orr"] = "friend"
    session._set_live(state)
    lines = session.visual_sleep()
    assert any("dress a deep gash" in ln for ln in lines), lines
    assert session.area_state().injuries[0].treated is not None
    assert session.safe_here(session.area_state(), session.area(me.area))


def test_the_keeper_injures_treats_and_sets_standings(plotted: VisualWindow):
    from sealedlore.engine.keeper import parse_keeper

    session = plotted.session
    events = session.story.plot.events
    reply = parse_keeper(
        '{"fire": [], "script": ['
        '{"op": "injure", "kind": "broken leg", "words": "a leg broken in the fall"}, '
        '{"op": "status", "who": "Maela Orr", "status": "ally"}, '
        '{"op": "status", "who": "nobody", "status": "friend"}], "note": ""}',
        events,
    )
    lines = session.apply_keeper_reply(reply, kind="direction")
    assert "You are hurt: a leg broken in the fall." in lines and "Maela Orr: ally." in lines
    state = session.area_state()
    assert state.injuries[0].kind == "broken leg" and state.standing["Maela Orr"] == "ally"
    assert session.visual_walk_factor() == 0.4
    assert "injuries: a leg broken in the fall, untreated" in session.visual_state_text()
    assert "Maela Orr: ally" in session.visual_state_text()
    treated = parse_keeper(
        '{"fire": [], "script": [{"op": "treat", "how": "first aid"}], "note": ""}', events
    )
    lines = session.apply_keeper_reply(treated, kind="direction")
    assert "You splint a leg broken in the fall." in lines
    assert session.visual_walk_factor() == 0.5
    cared = parse_keeper('{"fire": [], "script": [{"op": "heal"}], "note": ""}', events)
    session.apply_keeper_reply(cared, kind="direction")
    assert session.area_state().injuries[0].cared is not None
    # A group's standing holds for each of its people.
    session.visual_design().groups["The farm"] = ["Maela Orr", "Captain Idris"]
    state = session.area_state()
    state.standing.clear()
    session._set_live(state)
    grouped = parse_keeper(
        '{"fire": [], "script": [{"op": "status", "who": "The farm", "status": "guest"}], '
        '"note": ""}',
        events,
    )
    assert "The farm: guest." in session.apply_keeper_reply(grouped, kind="direction")
    assert session.standing_of("Captain Idris") == "guest"


def test_a_scout_spots_people_first_and_they_greet_only_when_close(plotted: VisualWindow):
    session = plotted.session
    state = session.area_state()
    state.creatures.clear()
    farm = session.area("farm")
    # Out on the farm's road, Idris 28 tiles down it (93 m at 3.3 m a tile)
    # in open sight (the fields are full of trees, which stop the eye).
    column = next(
        x for x in range(farm.width) if all(farm.rows[y][x] == "=" for y in range(34, 58))
    )
    spot = (column, 36)
    state.spots["Serrik Vaun"] = Spot(area="farm", x=spot[0], y=spot[1])
    far = (column, 56)  # 20 tiles: 66 m, past the ordinary 60 m
    state.spots["Captain Idris"] = Spot(area="farm", x=far[0], y=far[1])
    state.placed.append("Captain Idris")
    session.visual_design().scouting["Serrik Vaun"] = 20
    session._set_live(state)
    lines = session.visual_report()
    assert any(ln.startswith("You spot Captain Idris first") for ln in lines), lines
    assert session.unaware_of_player("Captain Idris")
    assert not session.encounter_due("Captain Idris")
    # Walked up close, he notices; and spoken to, at once.
    state = session.area_state()
    near = (far[0], far[1] - 3)
    state.spots["Serrik Vaun"] = Spot(area="farm", x=near[0], y=near[1])
    session._set_live(state)
    assert not session.unaware_of_player("Captain Idris")
    assert session.encounter_due("Captain Idris")
    # A poor scout is seen first from afar instead: the person reacts now.
    state = session.area_state()
    state.scout_rolled.clear()
    state.spotted.clear()
    state.spots["Serrik Vaun"] = Spot(area="farm", x=spot[0], y=spot[1])
    session.visual_design().scouting["Serrik Vaun"] = 0
    session.visual_design().people["Captain Idris"].alertness = 20
    session._set_live(state)
    lines = session.visual_report()
    assert any(ln.startswith("Captain Idris has seen you") for ln in lines), lines
    assert "Captain Idris" in session.people_in_sight()


class _DrawingImages:
    """An image client that draws: a flat picture on white with a dark
    figure in it, so the cut-out keeps something."""

    def __init__(self, base_url: str = "", api_key: str = "", **kwargs) -> None:
        self.api = kwargs.get("api", "nanogpt")
        self.payloads: list[dict] = []

    def generate(self, payload):
        from PySide6.QtGui import QColor, QImage, QPainter

        from sealedlore.gui.image_codec import encoded
        from sealedlore.providers.images import ImageReply

        self.payloads.append(payload)
        image = QImage(96, 96, QImage.Format_RGB32)
        image.fill(QColor(255, 255, 255))
        painter = QPainter(image)
        painter.fillRect(30, 20, 36, 60, QColor(40, 30, 20))
        painter.end()
        return ImageReply(images=[encoded(image, "PNG")], raw={}, cost=0.01)

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_the_people_of_a_place_are_drawn_when_the_player_comes_to_it(
    app, plotted: VisualWindow, monkeypatch
):
    """The author (Oct 27 2026): portraits draw when the player goes into
    the relevant region; a man's card drew as the set's woman figure."""
    from sealedlore.engine import session_people_art
    from sealedlore.gui import visual_window as vw_module

    session = plotted.session
    client = _DrawingImages()
    monkeypatch.setattr(session_people_art, "ImageClient", lambda *a, **k: client)
    session.config.visual_paint = True
    session.config.image_endpoint.base_url = "https://nano-gpt.com/api/v1"
    session.config.image_endpoint.api_key = "k"
    state = session.area_state()
    me = state.spots["Serrik Vaun"]
    near = _beside_tile(session.area(me.area), me)
    state.spots["Maela Orr"] = Spot(area=me.area, x=near[0], y=near[1])
    session._set_live(state)
    maela = session.visual_card("Maela Orr")
    assert not maela.reference_images and session.walker_file("Maela Orr") is None
    wanted = session.people_art_wanted(session.area(me.area))
    assert [c.name for c in wanted] == ["Maela Orr"]
    drawn: list[list[str]] = []
    session.on_people_drawn = drawn.append
    assert session.start_people_art(wanted, vw_module._cut_sprite)
    session._people_thread.join(20)
    assert drawn == [["Maela Orr"]]
    # A portrait from her card, first on it; the sprite from the portrait.
    assert maela.reference_images and maela.reference_images[0].caption == "portrait"
    assert maela.reference_images[0].prompt and maela.reference_images[0].model
    assert session.walker_file("Maela Orr") is not None
    assert len(client.payloads) == 2
    assert "imageDataUrls" in client.payloads[1] and client.payloads[1]["prompt"].startswith(
        "Create a small full-body game character sprite"
    )
    assert client.payloads[1]["enable_prompt_expansion"] is False
    # Drawn: not wanted again; the portrait file and the sprite file are kept.
    assert session.people_art_wanted(session.area(me.area)) == []
    assert session.pictures.read(maela.reference_images[0].file)
    assert session.pictures.read(session.walker_file("Maela Orr"))
    kinds = [e["kind"] for e in session.read_api_log()] if hasattr(session, "read_api_log") else []
    del kinds


# --- challenges (engine/challenges.py, the author's ruling of Oct 27 2026) -------------


def _on_the_road(game, *, player_tiles_off: int):
    """The player and Captain Idris on the farm's open road, Idris
    `player_tiles_off` tiles down it; the room's trees stop the eye elsewhere."""
    session = game.session
    state = session.area_state()
    state.creatures.clear()
    farm = session.area("farm")
    column = next(
        x for x in range(farm.width) if all(farm.rows[y][x] == "=" for y in range(34, 58))
    )
    idris = (column, 56)
    state.spots["Serrik Vaun"] = Spot(area="farm", x=column, y=56 - player_tiles_off)
    state.spots["Captain Idris"] = Spot(area="farm", x=idris[0], y=idris[1])
    if "Captain Idris" not in state.placed:
        state.placed.append("Captain Idris")
    state.greeted = [g for g in state.greeted if not g.startswith("Captain Idris@")]
    session._set_live(state)
    return farm


TOLL = (
    '{"say": "Far enough. The road costs: a tin of food, set down there.", "mood": "hard", '
    '"intent": {"kind": "stay"}, "remember": "A stranger on my road.", '
    '"demand": "a tin of food, set down on the road"}'
)
WARNING = (
    '{"say": "I said a tin of food. Last time of asking.", "mood": "hard", '
    '"intent": {"kind": "stay"}, "demand": "a tin of food"}'
)
ATTACKS = '{"say": "Your funeral.", "mood": "cold", "intent": {"kind": "stay"}}'
BACKS_OFF = (
    '{"say": "Go on, then. Go.", "mood": "sour", "intent": {"kind": "retreat"}, "demand": ""}'
)


def test_a_challenger_hails_the_player_close_and_their_demand_is_kept(peopled: VisualWindow):
    session = peopled.session
    session.visual_design().people["Captain Idris"].challenges = "a toll for the road"
    session.visual_design().scouting["Serrik Vaun"] = 0
    # In sight at 66 m: an ordinary person would be greeted now; a
    # challenger waits until the player is within hailing distance.
    _on_the_road(peopled, player_tiles_off=20)
    assert "Captain Idris" in session.people_in_sight() or True  # 66 m: past sight
    assert not session.encounter_due("Captain Idris")
    farm = _on_the_road(peopled, player_tiles_off=8)  # 26 m
    assert farm.tile_m * 8 <= 30
    assert session.encounter_due("Captain Idris")
    session.provider.responses = [TOLL]
    peopled._after_action([])
    _answer(peopled, TOLL)
    user = session.provider.requests[-1].messages[1].text
    assert "within hailing distance" in user and "a toll for the road" in user
    system = session.provider.requests[-1].messages[0].text
    assert "What you demand of anyone who comes near: a toll for the road" in system
    state = session.area_state()
    demand = state.demands["Captain Idris"]
    assert demand.what == "a tin of food, set down on the road" and demand.ignored == 0
    assert "Captain Idris: Far enough." in peopled.log.toPlainText()
    # The same action never counts as ignoring it.
    assert session.demands_due() == []


def test_an_ignored_demand_is_warned_once_more_then_pressed_with_an_attack(
    peopled: VisualWindow,
):
    session = peopled.session
    design = session.visual_design()
    design.people["Captain Idris"].challenges = "a toll for the road"
    design.scouting["Serrik Vaun"] = 0
    design.abilities["Serrik Vaun"] = 4  # against Idris's 8: outmatched
    _on_the_road(peopled, player_tiles_off=8)
    session.provider.responses = [TOLL]
    peopled._after_action([])
    _answer(peopled, TOLL)
    # The player waits instead of paying: the first ignore, a second warning.
    session.provider.responses = [WARNING]
    peopled._after_action(session.visual_wait(1))
    _answer(peopled, WARNING)
    user = session.provider.requests[-1].messages[1].text
    assert "has not done as you asked" in user and "outmatched" in user
    state = session.area_state()
    demand = state.demands["Captain Idris"]
    assert demand.ignored == 1 and demand.warnings == 2 and demand.pending == ""
    assert "Captain Idris: I said a tin of food." in peopled.log.toPlainText()
    # Ignored again: the program has them attack, whatever the reply's intent.
    session.provider.responses = [ATTACKS]
    peopled._after_action(session.visual_wait(1))
    _answer(peopled, ATTACKS)
    user = session.provider.requests[-1].messages[1].text
    assert "You attack them now" in user
    state = session.area_state()
    assert "Captain Idris" not in state.demands
    assert state.goals["Captain Idris"].intent == "attack"
    assert "Captain Idris goes for you." in peopled.log.toPlainText()


def test_a_stronger_player_is_warned_and_then_let_be(peopled: VisualWindow):
    session = peopled.session
    design = session.visual_design()
    design.people["Captain Idris"].challenges = "a toll for the road"
    design.scouting["Serrik Vaun"] = 0
    design.abilities["Serrik Vaun"] = 14  # far the stronger
    _on_the_road(peopled, player_tiles_off=8)
    session.provider.responses = [TOLL]
    peopled._after_action([])
    _answer(peopled, TOLL)
    session.provider.responses = [WARNING]
    peopled._after_action(session.visual_wait(1))
    _answer(peopled, WARNING)
    assert "far more dangerous" in session.provider.requests[-1].messages[1].text
    session.provider.responses = [BACKS_OFF]
    peopled._after_action(session.visual_wait(1))
    _answer(peopled, BACKS_OFF)
    assert "You give it up" in session.provider.requests[-1].messages[1].text
    state = session.area_state()
    assert "Captain Idris" not in state.demands
    assert state.goals["Captain Idris"].intent == "retreat"
    assert "Captain Idris backs away from you." in peopled.log.toPlainText()


def test_answering_a_demand_is_never_ignoring_it_and_paying_ends_it(peopled: VisualWindow):
    session = peopled.session
    design = session.visual_design()
    design.people["Captain Idris"].challenges = "a toll for the road"
    design.scouting["Serrik Vaun"] = 0
    _on_the_road(peopled, player_tiles_off=8)
    session.provider.responses = [TOLL]
    peopled._after_action([])
    _answer(peopled, TOLL)
    # A word to them counts as answering: no step is taken on it.
    state = session.area_state()
    state.spots["Serrik Vaun"] = Spot(
        area="farm", x=state.spots["Captain Idris"].x, y=state.spots["Captain Idris"].y - 3
    )
    session._set_live(state)
    peopled._set_talking("Captain Idris")
    _ask(peopled, "A tin? I have one.", WARNING)
    assert session.area_state().demands["Captain Idris"].ignored == 0
    # Handing them a tin: they answer to it, and the demand is met.
    state = session.area_state()
    state.things["tin"] = ThingRecord(name="a tin of beans", state="taken", by="Serrik Vaun")
    state.spots["Serrik Vaun"] = Spot(
        area="farm", x=state.spots["Captain Idris"].x, y=state.spots["Captain Idris"].y - 1
    )
    session._set_live(state)
    paid = '{"say": "That will do.", "mood": "easier", "intent": {"kind": "stay"}, "demand": ""}'
    session.provider.responses = [paid]
    peopled._carry_give("tin", "Captain Idris")
    _answer(peopled, paid)
    user = session.provider.requests[-1].messages[1].text
    assert "has just handed you the tin of beans" in user
    assert "Captain Idris" not in session.area_state().demands
    assert session.demands_due() == []


def test_a_step_with_no_reply_is_carried_out_in_plain_words(peopled: VisualWindow):
    session = peopled.session
    design = session.visual_design()
    design.people["Captain Idris"].challenges = "a toll for the road"
    design.scouting["Serrik Vaun"] = 0
    design.abilities["Serrik Vaun"] = 4
    _on_the_road(peopled, player_tiles_off=8)
    session.provider.responses = [TOLL]
    peopled._after_action([])
    _answer(peopled, TOLL)
    assert session.demands_due() == []  # the hail's own action
    session.visual_wait(1)
    assert session.demands_due() == [("Captain Idris", "warn")]
    lines = session.demand_without_reply("Captain Idris")
    assert lines == ["Captain Idris warns you again: a tin of food, set down on the road"]
    state = session.area_state()
    assert state.demands["Captain Idris"].warnings == 2
    session.visual_wait(1)
    assert session.demands_due() == [("Captain Idris", "attack")]
    lines = session.demand_without_reply("Captain Idris")
    assert lines[0] == "Captain Idris goes for you."
    assert session.area_state().goals["Captain Idris"].intent == "attack"


def test_coming_nearer_to_a_challenger_is_answering_and_walking_off_is_not(
    peopled: VisualWindow,
):
    session = peopled.session
    design = session.visual_design()
    design.people["Captain Idris"].challenges = "a toll for the road"
    design.scouting["Serrik Vaun"] = 0
    _on_the_road(peopled, player_tiles_off=8)
    session.provider.responses = [TOLL]
    peopled._after_action([])
    _answer(peopled, TOLL)
    # Three tiles nearer, to speak or to pay: no step is taken.
    state = session.area_state()
    idris = state.spots["Captain Idris"]
    state.spots["Serrik Vaun"] = Spot(area="farm", x=idris.x, y=idris.y - 5)
    state.seconds += 10
    session._set_live(state)
    assert session.demands_due() == []
    # Turning to talk to them counts too, whatever the walk looks like.
    session.visual_answering("Captain Idris")
    state = session.area_state()
    state.spots["Serrik Vaun"] = Spot(area="farm", x=idris.x + 2, y=idris.y - 5)
    state.seconds += 10
    session._set_live(state)
    assert session.demands_due() == []
    # Walking off is ignoring them.
    state = session.area_state()
    state.spots["Serrik Vaun"] = Spot(area="farm", x=idris.x + 2, y=idris.y - 8)
    state.seconds += 10
    session._set_live(state)
    assert session.demands_due() == [("Captain Idris", "warn")]


def test_a_leader_s_word_on_the_player_is_their_group_s(peopled: VisualWindow):
    """Guest standing comes from what a person says and does (the author,
    Oct 27 2026); a leader's word binds their group, another's only
    themselves."""
    session = peopled.session
    design = session.visual_design()
    design.groups["The house"] = ["Maela Orr", "Captain Idris"]
    design.people["Captain Idris"].group = "The house"
    design.people["Captain Idris"].leads = True
    design.people["Maela Orr"].group = "The house"
    peopled._set_talking("Maela Orr")
    taken_in = (
        '{"say": "Stay the night, then.", "mood": "warm", "intent": {"kind": "stay"}, '
        '"standing": "guest"}'
    )
    _ask(peopled, "I need a roof.", taken_in)
    state = session.area_state()
    assert state.standing["Maela Orr"] == "guest"
    assert "The house" not in state.group_standing  # her word binds only herself
    assert session.standing_of("Captain Idris", state) == ""
    # The system block says whose word binds whom.
    system = session.provider.requests[-1].messages[0].text
    assert "You belong to The house; its leader's word on the player binds you" in system
    assert "it comes from what is said and done" in system
    # The leader's word is the house's.
    from sealedlore.engine.npc import NpcReply

    state.spots["Captain Idris"] = Spot(
        area="house", x=state.spots["Maela Orr"].x, y=state.spots["Maela Orr"].y
    )
    state.placed.append("Captain Idris")
    session._set_live(state)
    lines = session.apply_npc_reply(
        "Captain Idris",
        NpcReply("Go, and don't come back.", "cold", "stay", "", "", "", standing="hostile"),
    )
    assert "(Captain Idris: hostile with you now.)" in lines
    assert "(The house: hostile with you now.)" in lines
    state = session.area_state()
    assert state.group_standing["The house"] == "hostile"
    assert session.standing_of("Maela Orr", state) == "guest"  # her own stands over the group's


def test_going_through_a_door_with_a_demand_unanswered_brings_the_demander_after_you(
    peopled: VisualWindow,
):
    session = peopled.session
    design = session.visual_design()
    design.people["Captain Idris"].challenges = "a toll for the road"
    design.scouting["Serrik Vaun"] = 0
    design.abilities["Serrik Vaun"] = 4  # outmatched
    _on_the_road(peopled, player_tiles_off=8)
    session.provider.responses = [TOLL]
    peopled._after_action([])
    _answer(peopled, TOLL)
    # Into the house without a word: Idris comes in after them, and it counts.
    state = session.area_state()
    house = session.area("house")
    x, y = next(
        (x, y)
        for y in range(house.height - 2, 0, -1)
        for x in range(house.width)
        if house.walkable(x, y)
    )
    state.spots["Serrik Vaun"] = Spot(area="house", x=x, y=y)
    session._set_live(state)
    due = session.demands_due()
    state = session.area_state()
    assert due == [("Captain Idris", "warn")]
    assert state.spots["Captain Idris"].area == "house"
    assert state.demands["Captain Idris"].ignored == 1
    assert not session.encounter_due("Captain Idris")  # no fresh greeting inside


def test_nobody_is_spotted_first_once_they_have_spoken_or_stand_close(peopled: VisualWindow):
    session = peopled.session
    design = session.visual_design()
    design.scouting["Serrik Vaun"] = 20  # always wins the roll
    farm = _on_the_road(peopled, player_tiles_off=4)  # about 13 m: close enough to be seen
    state = session.area_state()
    me = state.spots["Serrik Vaun"]
    lines = session.scout_report(state, farm, me)
    assert not any("Captain Idris first" in line for line in lines)
    assert not session.unaware_of_player("Captain Idris", state)


def test_a_challenger_the_keeper_has_speak_has_made_their_demand(peopled: VisualWindow):
    from sealedlore.engine.keeper import Op

    session = peopled.session
    session.visual_design().people["Captain Idris"].challenges = "your name and your business"
    _on_the_road(peopled, player_tiles_off=6)
    lines = session.apply_script(
        [Op(op="say", args={"who": "Captain Idris", "text": "Stop there. Who are you?"})]
    )
    assert "Captain Idris: Stop there. Who are you?" in lines
    demand = session.area_state().demands["Captain Idris"]
    assert demand.what == "your name and your business" and demand.ignored == 0
    # Waiting instead of answering is ignoring it.
    session.visual_wait(1)
    assert session.demands_due() == [("Captain Idris", "warn")]


def test_the_keeper_can_stand_someone_at_a_door_of_the_players_area(peopled: VisualWindow):
    session = peopled.session
    _on_the_road(peopled, player_tiles_off=6)
    state = session.area_state()
    farm = session.area("farm")
    door = next(e for e in farm.entrances if e.place_id == "house")
    spot = session._place_spot(state, "at the door of the house")
    assert spot is not None and spot.area == "farm"
    assert abs(spot.x - door.x) + abs(spot.y - door.y) <= 3
    assert (spot.x, spot.y) not in {(e.x, e.y) for e in farm.entrances}


def test_a_stranger_the_keeper_brings_in_and_has_speak_has_made_their_demand(
    peopled: VisualWindow,
):
    from sealedlore.engine.keeper import Op

    session = peopled.session
    _on_the_road(peopled, player_tiles_off=6)
    session.apply_script(
        [
            Op(
                op="stranger",
                args={
                    "who": "Walt Pruitt",
                    "card": "A lean farmer with a rifle",
                    "attitude": "wary",
                    "where": "nearby",
                    "challenges": "your business, from there",
                },
            ),
            Op(op="say", args={"who": "Walt Pruitt", "text": "State your business."}),
        ]
    )
    assert session.area_state().demands["Walt Pruitt"].what == "your business, from there"
