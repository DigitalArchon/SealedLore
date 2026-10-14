"""The map's art (engine/world_art.py, engine/session_world_art.py,
storage/world_art.py).

The model names shapes; the program cleans and draws them, keeps each piece
once made, and never redraws a place: the story has been there, so that is
what it looks like. A changed thing is drawn as an edit of its last sprite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.world import applied, place_delta
from sealedlore.engine.world_art import (
    LAYOUT_HEIGHT,
    LAYOUT_WIDTH,
    TERRAIN_TILES,
    ArtJob,
    adopt,
    clean_svg,
    finish,
    plan_art,
    rasterise,
)
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.world import (
    PlaceLayout,
    WorldArt,
    WorldDelta,
    WorldMap,
    WorldPlace,
    WorldSprite,
    WorldThing,
)
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, read_api_log, save_story_bundle
from sealedlore.storage.world_art import load_world_art

TRUCK = (
    '```svg\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect x="8" y="30" width="30" height="20" fill="#b3384a"/>'
    '<circle cx="16" cy="48" r="7" fill="#1b1d2b"/></svg>\n```'
)

# --- SVG, cleaned ----------------------------------------------------------------


def test_a_clean_svg_keeps_only_plain_shapes():
    dirty = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" onload="alert(1)">'
        "<script>alert(1)</script>"
        '<g transform="translate(2,2)"><rect x="1" y="1" width="4" height="4" fill="#b3384a"'
        ' onclick="x()" style="fill:red"/></g>'
        '<image href="https://example.com/a.png" width="10" height="10"/>'
        '<text x="1" y="1">hi</text>'
        '<circle cx="5" cy="5" r="2" fill="url(#g)" stroke="#1b1d2b"/>'
        "<foreignObject><div>x</div></foreignObject>"
        "</svg>"
    )
    svg = clean_svg(dirty)
    assert svg is not None
    for gone in (
        "script",
        "onload",
        "onclick",
        "style",
        "image",
        "href",
        "text",
        "url(",
        "foreign",
    ):
        assert gone not in svg
    assert 'viewBox="0 0 64 64"' in svg
    assert '<rect x="1" y="1" width="4" height="4" fill="#b3384a"' in svg
    assert 'transform="translate(2,2)"' in svg
    assert '<circle cx="5" cy="5" r="2" stroke="#1b1d2b"' in svg


@pytest.mark.parametrize(
    "text",
    [
        "no svg here",
        '<svg xmlns="http://www.w3.org/2000/svg"><text>only words</text></svg>',
        '<svg><rect width="&a;" height="1"/></svg>',  # an entity: never declared, refused
        "<svg><rect width='1'",
    ],
)
def test_nothing_worth_keeping_is_none(text):
    assert clean_svg(text) is None


# --- layouts and land -------------------------------------------------------------


def test_a_building_is_drawn_with_closed_walls_and_its_door():
    rows, objects, labels = rasterise(
        {
            "ground": "snow",
            "features": [
                {
                    "kind": "building",
                    "name": "Farmhouse",
                    "x": 2,
                    "y": 2,
                    "w": 6,
                    "h": 4,
                    "doors": [{"side": "north", "at": 2}],
                },
                {"kind": "road", "points": [[4, 0], [4, 1]]},
                {"kind": "object", "name": "Old truck", "x": 10, "y": 3},
                {"kind": "area", "fill": "water", "x": 38, "y": 22, "w": 9, "h": 9},
            ],
        },
        LAYOUT_WIDTH,
        LAYOUT_HEIGHT,
    )
    assert len(rows) == LAYOUT_HEIGHT and {len(r) for r in rows} == {LAYOUT_WIDTH}
    assert rows[2][2:8] == "##+###"
    assert rows[5][2:8] == "######"
    assert rows[3][2] == "#" and rows[3][7] == "#" and rows[3][3:7] == "____"
    assert rows[0][4] == "=" and rows[1][4] == "="
    assert rows[3][10] == "1" and objects == {"1": "Old truck"}
    assert labels == {"Farmhouse": [5, 4]}
    assert rows[23][39] == "~"  # clipped at the edge, not an error


# --- what needs drawing ----------------------------------------------------------


def farm_world() -> WorldMap:
    farm = WorldPlace(id="farm", name="Farmhouse", kind="building")
    yard = WorldPlace(id="yard", name="Yard", kind="area", inside="farm")
    hills = WorldPlace(id="hills", name="The hills", kind="region")
    truck = WorldThing(id="truck", name="Pickup truck", at="farm", state="rusty")
    delta = WorldDelta(places=[farm, yard, hills], things=[truck], where={"Jane": "farm"})
    place_delta(WorldMap(), delta, "Jane")
    return applied(WorldMap(), delta)


def test_the_story_s_places_get_land_and_a_layout_and_sprites_nearest_first():
    jobs = plan_art(farm_world(), WorldArt(), here="farm", setting="a frozen winter")
    keys = [job.key for job in jobs]
    assert keys[0] == "terrain:farm"
    assert keys[1] == "layout:farm"
    assert "sprite:farm" in keys and "sprite:truck:rusty" in keys
    assert "sprite:yard" not in keys and "sprite:hills" not in keys  # drawn as land
    assert "layout:hills" not in keys  # never visited
    ask = jobs[0].messages[-1].text
    assert "Draw the land around: Farmhouse, a building" in ask and "a frozen winter" in ask
    layout = jobs[1].messages
    assert "One tile is about" in layout[0].text and "In it: Yard, a area" in layout[1].text
    assert "Pickup truck (rusty)" in layout[1].text


def test_what_is_drawn_is_never_asked_again_nor_what_keeps_failing():
    world = farm_world()
    art = WorldArt(
        layouts={"farm": PlaceLayout(place_id="farm", rows=["."])},
        sprites=[
            WorldSprite(subject="place", subject_id="farm", name="Farmhouse", svg="<svg/>"),
            WorldSprite(
                subject="thing",
                subject_id="truck",
                name="Pickup truck",
                state="rusty",
                svg="<svg/>",
            ),
        ],
        failures={"terrain:farm": 2},
    )
    assert plan_art(world, art, here="farm", setting="x") == []


def test_a_changed_thing_is_drawn_as_an_edit_of_its_last_sprite():
    world = farm_world()
    world.things["truck"].state = "burnt out"
    first = WorldSprite(
        subject="thing", subject_id="truck", name="Pickup truck", state="rusty", svg="<svg>A</svg>"
    )
    art = WorldArt(sprites=[first], layouts={"farm": PlaceLayout(place_id="farm", rows=["."])})
    (job,) = [j for j in plan_art(world, art, here="farm", setting="x") if j.subject_id == "truck"]
    assert job.key == "sprite:truck:burnt out" and job.from_sprite == first.id
    roles = [m.role for m in job.messages]
    assert roles == ["system", "user", "assistant", "user"]
    assert "<svg>A</svg>" in job.messages[2].text
    assert "Now draw the same Pickup truck, burnt out." in job.messages[3].text


def test_a_finished_drawing_is_checked_and_kept_once():
    sprite = finish(
        ArtJob("sprite:truck:rusty", "sprite", [], "truck", name="Pickup truck", subject="thing"),
        TRUCK,
        model="art/model",
        made_at="now",
    )
    assert isinstance(sprite, WorldSprite) and "<rect" in sprite.svg
    with pytest.raises(ValueError):
        finish(ArtJob("sprite:x", "sprite", [], "x"), "I can't draw that.", model="m", made_at="t")
    land = finish(
        ArtJob("terrain:farm", "terrain", [], "farm", patch=(0.0, 0.0, 400.0)),
        json.dumps(
            {
                "ground": "snow",
                "features": [
                    {"kind": "area", "fill": "trees", "x": 0, "y": 0, "w": 5, "h": 5},
                    {"kind": "building", "x": 10, "y": 10, "w": 5, "h": 5},
                ],
            }
        ),
        model="m",
        made_at="t",
    )
    assert len(land.rows) == TERRAIN_TILES and "#" not in "".join(land.rows)  # land only
    art = WorldArt()
    adopt(art, land)
    adopt(art, land.model_copy(update={"id": "other", "rows": ["~"]}))
    assert len(art.terrain) == 1 and art.terrain[0].rows == land.rows  # never redrawn


# --- in the session ---------------------------------------------------------------

PASSAGE = "Serrik shouldered the cellar door open and climbed up into the cold yard."
READ = json.dumps(
    {
        "places": [{"name": "The yard", "kind": "area", "quote": "climbed up into the cold yard"}],
        "where": [{"who": "Serrik", "place": "The yard", "quote": "climbed up into the cold yard"}],
    }
)
LAND = json.dumps(
    {
        "ground": "snow",
        "features": [{"kind": "area", "fill": "trees", "x": 0, "y": 0, "w": 9, "h": 9}],
    }
)


def make_session(tmp_path: Path, story, cast, responses, *, art: bool = True) -> StorySession:
    story.defaults.main_model = "anthropic/claude-sonnet-4.5"
    story.defaults.world_map = True
    story.held_character_id = "char-serrik"
    bundle = StoryBundle(story=story, cast=cast)
    save_story_bundle(bundle, root=tmp_path)
    config = Config(
        providers=[ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1")],
        active_provider_name="nano",
        min_cacheable_tokens=1,
        scene_model="small/scene",
        map_art_model="art/model",
        scene_reads="manual",
        map_art=art,
    )
    return StorySession(
        bundle,
        config,
        MockChatProvider(responses),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def turn() -> TurnRequest:
    return TurnRequest(
        speaker_id="char-serrik", user_text="I climb out.", controlled_character_id="char-serrik"
    )


def test_after_a_read_the_map_is_drawn_and_kept(tmp_path: Path, story, cast):
    # The passage, the read, then the land and the yard's layout (an area: no sprite).
    session = make_session(tmp_path, story, cast, [PASSAGE, READ, LAND, LAND])
    list(session.send(turn()))

    art_requests = [r for r in session.provider.requests if r.model == "art/model"]
    assert len(art_requests) == 2
    kept = load_world_art(session.story.id, tmp_path)
    assert len(kept.terrain) == 1 and len(kept.layouts) == 1
    kinds = [e["kind"] for e in read_api_log(session.story.id, root=tmp_path)]
    assert kinds.count("map_art_request") == 2 and kinds.count("map_art_response") == 2
    assert session.map_art_jobs() == []  # nothing left to draw


def test_a_failed_drawing_is_counted_and_let_be(tmp_path: Path, story, cast):
    session = make_session(tmp_path, story, cast, [PASSAGE, READ, "no", "no"])
    list(session.send(turn()))
    assert session.world_art.failures == {
        f"terrain:{next(iter(session.world().places))}": 1,
        f"layout:{next(iter(session.world().places))}": 1,
    }
    kinds = [e["kind"] for e in read_api_log(session.story.id, root=tmp_path)]
    assert kinds.count("map_art_refused") == 2


def test_with_drawing_off_nothing_is_drawn(tmp_path: Path, story, cast):
    session = make_session(tmp_path, story, cast, [PASSAGE, READ], art=False)
    list(session.send(turn()))
    assert all(r.model != "art/model" for r in session.provider.requests)
    assert session.map_art_jobs() == []


def test_nothing_is_drawn_in_a_private_scene(tmp_path: Path, story, cast):
    session = make_session(tmp_path, story, cast, [PASSAGE, READ], art=False)
    list(session.send(turn()))
    session.config.map_art = True
    session.enter_private(keep="memory", provider=MockChatProvider(["x"]), model="local/m")
    assert session.map_art_jobs() == []
    assert not session.start_map_art()


def test_a_backup_and_a_copy_carry_the_art(tmp_path: Path, story, cast):
    from sealedlore.storage.repository import copy_story

    session = make_session(tmp_path, story, cast, [PASSAGE, READ, LAND, LAND])
    list(session.send(turn()))
    copy = copy_story(session.story.id, title="Copy", root=tmp_path)
    assert len(load_world_art(copy.story.id, tmp_path).layouts) == 1
