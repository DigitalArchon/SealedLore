"""In a story played on the map, the card the storyteller is sent follows the
map (engine/map_presence.py; SealedLoreDocs design/map-prompt.md): the map
is current, the roster joins it, and someone the plot brought in unnamed is
that character on the map."""

from __future__ import annotations

from pathlib import Path

from sealedlore.engine.map_presence import reconcile_scene
from sealedlore.engine.plot_md import parse_plot_markdown
from sealedlore.models.area import LiveArea, Spot
from sealedlore.models.character import Character
from sealedlore.models.node import Node, NodeMeta
from sealedlore.models.plot import Direction
from sealedlore.models.scene import OffstageCharacter, SceneState
from sealedlore.models.world import WorldDelta, WorldMap
from tests.test_area import make_session, turn

JOHN = Character(id="john", name="John")
RUTH = Character(id="ruth", name="Ruth")
EVAN = Character(id="evan", name="Evan Stevens")


def test_the_card_follows_the_map_where_the_map_knows():
    card = SceneState(
        location="A seemingly abandoned farmhouse",
        situation="Ruth watches from the kitchen door.",
        present_character_ids=["john", "ruth"],
        present_others=["Marcus Webb"],
        offstage_but_nearby=[OffstageCharacter(character_id="evan", note="on the road")],
    )
    out = reconcile_scene(
        card,
        here=["Evan Stevens", "Unknown 1"],
        away={"Ruth": "The farmhouse", "Marcus Webb": "Hellsville"},
        cast=[JOHN, RUTH, EVAN],
        unknowns={"Unknown 1": "the woman with the lantern"},
        held_id="john",
    )
    assert out.location is None and out.situation == card.situation
    assert out.present_character_ids == ["john", "evan"]
    assert out.present_others == ["the woman with the lantern (not named yet)"]
    notes = {e.character_id: e.note for e in out.offstage_but_nearby}
    assert notes == {"ruth": "the map has them at The farmhouse"}
    # The story's own card is never touched.
    assert card.present_character_ids == ["john", "ruth"] and card.location
    # The author's character is never sent away.
    held = reconcile_scene(
        card, here=[], away={"John": "The barn"}, cast=[JOHN], unknowns={}, held_id="john"
    )
    assert "john" in held.present_character_ids


def _tail(session) -> str:
    told = [r for r in session.provider.requests if "REMEMBER:" in r.messages[-1].text]
    return told[-1].messages[-1].text


def test_the_storyteller_is_told_who_the_map_has_here_and_that_the_map_wins(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast, ["Quiet.", "{}", "Still.", "{}"])
    session.config.scene_reads = "manual"
    list(session.send(turn("I look around.")))
    # The card says only Serrik; the map stands Captain Idris beside him and
    # Maela out in the farm.
    session.story.scene.present_character_ids = ["char-serrik", "char-maela"]
    state = session.area_state()
    me = state.spots["Serrik Vaun"]
    state.spots["Captain Idris"] = Spot(area=me.area, x=me.x + 1, y=me.y)
    state.spots["Maela Orr"] = Spot(area="farm", x=5, y=5)
    session.story.area_live = LiveArea(leaf_id=session.story.active_leaf_id, state=state)
    list(session.send(turn("I wait.")))
    tail = _tail(session)
    scene = tail[tail.index("# THIS SCENE") : tail.index("# THE MAP HERE")]
    here = scene[scene.index("IN THE SCENE") : scene.index("NOT IN THE SCENE")]
    assert "Captain Idris" in here and "Maela Orr" not in here
    assert "Maela Orr — the map has them at" in scene
    assert "Location:" not in scene  # the map's chain is where the scene is
    assert session.texts["map.current"] in tail
    # The story's card is as it was.
    assert "char-maela" in session.story.scene.present_character_ids


def test_a_story_not_on_a_map_is_sent_its_card_as_it_is(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast, ["Quiet.", "{}"])
    session.story.defaults.world_map = False
    list(session.send(turn("I look around.")))
    tail = _tail(session)
    assert "Location: The undercroft beneath Calder Keep" in tail
    assert session.texts["map.current"] not in tail


def test_someone_the_plot_brought_in_unnamed_is_that_character_on_the_map(tmp_path):
    from sealedlore.engine.session import StorySession
    from sealedlore.models.config import Config
    from sealedlore.providers.mock import MockChatProvider
    from sealedlore.storage.repository import save_story_bundle
    from sealedlore.storage.scenario import bundle_from_scenario

    path = Path(__file__).resolve().parent.parent / "SampleStories" / "Doomsville with a map.md"
    scenario = parse_plot_markdown(path.read_text()).scenario
    bundle = bundle_from_scenario(scenario, main_model="x")
    save_story_bundle(bundle, root=tmp_path)
    session = StorySession(
        bundle,
        Config(map_art=False),
        MockChatProvider(["x"]),
        root=tmp_path,
        learn_corrections=False,
    )
    event = next(e for e in session.story.plot.events if e.title == "First bandit meeting")
    author = Node(
        id="t",
        kind="user",
        speaker_id="x",
        content="I walk north.",
        meta=NodeMeta(direction=[Direction(event_id=event.id, kind="begin")]),
    )
    before = WorldMap()
    delta = WorldDelta(
        unknowns={"Unknown 1": "the young man with the scoped rifle"},
        where={"Unknown 1": "road"},
    )
    passage = "A young man with a scoped rifle steps out from behind the truck."
    session._name_brought(delta, author, before, passage)
    assert delta.unknowns == {} and delta.where == {"Evan Stevens": "road"}
    # Two strangers, or the event's man named already: left as they are.
    two = WorldDelta(unknowns={"Unknown 1": "a man", "Unknown 2": "a woman"})
    session._name_brought(two, author, before, passage)
    assert len(two.unknowns) == 2
    named = WorldDelta(unknowns={"Unknown 1": "the young man"})
    session._name_brought(named, author, before, "Evan Stevens grins.")
    assert named.unknowns == {"Unknown 1": "the young man"}
    # Not on a turn that began nothing.
    quiet = Node(id="q", kind="user", speaker_id="x", content="I wait.")
    alone = WorldDelta(unknowns={"Unknown 1": "the young man"})
    session._name_brought(alone, quiet, before, passage)
    assert alone.unknowns == {"Unknown 1": "the young man"}


def _doomsville_with_a_map(tmp_path):
    from sealedlore.engine.session import StorySession
    from sealedlore.models.config import Config
    from sealedlore.providers.mock import MockChatProvider
    from sealedlore.storage.repository import save_story_bundle
    from sealedlore.storage.scenario import bundle_from_scenario

    path = Path(__file__).resolve().parent.parent / "SampleStories" / "Doomsville with a map.md"
    scenario = parse_plot_markdown(path.read_text()).scenario
    bundle = bundle_from_scenario(scenario, main_model="x")
    save_story_bundle(bundle, root=tmp_path)
    return StorySession(
        bundle, Config(map_art=False), MockChatProvider(["x"]), root=tmp_path,
        learn_corrections=False,
    )  # fmt: skip


def test_a_group_or_someone_of_the_other_sex_is_never_the_character_an_event_brought(tmp_path):
    """Live (Oct 20 2026): the welcome to Hellsville brought Marcus Webb and
    Elena Ruiz; the passage named Marcus, and "the watchmen" the map read put
    at the gate were taken for Elena, who stood there with John though the
    passage never had her."""
    session = _doomsville_with_a_map(tmp_path)
    event = next(e for e in session.story.plot.events if e.title == "Welcome to Hellsville")
    author = Node(
        id="t", kind="user", speaker_id="x", content="I set off for Hellsville.",
        meta=NodeMeta(direction=[Direction(event_id=event.id, kind="begin")]),
    )  # fmt: skip
    passage = '"Marcus Webb," he said, stopping a few paces short.'
    watch = WorldDelta(
        unknowns={"Unknown 1": "the watchmen"},
        where={"Unknown 1": "gate", "Marcus Webb": "street"},
    )
    session._name_brought(watch, author, WorldMap(), passage)
    assert watch.unknowns == {"Unknown 1": "the watchmen"} and "Elena Ruiz" not in watch.where
    man = WorldDelta(
        unknowns={"Unknown 1": "a bearded man on the barricade"},
        where={"Unknown 1": "gate", "Marcus Webb": "street"},
    )
    session._name_brought(man, author, WorldMap(), passage)
    assert "Elena Ruiz" not in man.where
    # One woman, whom the passage doesn't name: Elena, as before.
    woman = WorldDelta(
        unknowns={"Unknown 1": "a woman with a canvas bag"},
        where={"Unknown 1": "gate", "Marcus Webb": "street"},
    )
    session._name_brought(woman, author, WorldMap(), passage)
    assert woman.where.get("Elena Ruiz") == "gate"
