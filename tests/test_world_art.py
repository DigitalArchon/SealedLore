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
    clean_svg,
    local_area,
    plan_rect,
    rasterise,
)
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.world import (
    PlaceLayout,
    WorldArt,
    WorldDelta,
    WorldLink,
    WorldMap,
    WorldPlace,
    WorldThing,
)
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, save_story_bundle
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


# --- plans on the map: the local area, and what the map fixes in a plan ------------


def doomsville() -> WorldMap:
    farm = WorldPlace(id="farm", name="The farm", kind="site", x=0.0, y=0.0, size=400.0)
    house = WorldPlace(
        id="house", name="The farmhouse", kind="building", inside="farm", x=0.0, y=50.0
    )
    barn = WorldPlace(id="barn", name="The barn", kind="building", inside="farm", x=120.0, y=-40.0)
    pond = WorldPlace(
        id="pond", name="The frozen pond", kind="area", inside="farm", x=-120.0, y=-90.0, size=60.0
    )
    town = WorldPlace(id="town", name="Hellsville", kind="settlement", x=0.0, y=8000.0, size=1500.0)
    hills = WorldPlace(id="hills", name="The hills", kind="area", x=4800.0, y=8600.0, size=3000.0)
    caves = WorldPlace(
        id="caves", name="The caves", kind="site", inside="hills", x=5200.0, y=9100.0, size=200.0
    )
    return WorldMap(
        places={p.id: p for p in (farm, house, barn, pond, town, hills, caves)},
        links=[WorldLink(a="farm", b="town", direction="north", distance="five miles")],
        seen=["farm", "town"],
        start="house",
    )


def test_the_local_map_is_the_outermost_place_up_to_a_few_km_wide():
    world = doomsville()
    assert local_area(world, "house").id == "farm"  # the farm, not its farmhouse
    assert local_area(world, "caves").id == "caves"  # the hills are country, not a place
    assert local_area(world, "town").id == "town"
    assert local_area(world, "nowhere") is None


def test_a_plan_lies_on_the_map_where_its_place_is():
    world = doomsville()
    assert plan_rect(world, world.places["farm"]) == (-200.0, 120.0, 400.0, 240.0)
    west, north, width, height = plan_rect(world, world.places["house"])
    assert (width, height) == (40.0, 24.0) and (west, north) == (-20.0, 62.0)


def test_a_drawing_reply_s_numbers_can_t_hang_or_crash_the_parser():
    """ "w": 1e9 ran the building loop for the rest of the session and 1e999
    overflowed round (the review, Oct 15 2026): held to the grid."""
    import time

    from sealedlore.engine.world_art import COORD_LIMIT, _int, rasterise

    assert _int("1e999") == 0 and _int(float("inf"), 7) == 7 and _int(-1e12) == -COORD_LIMIT
    spec = {
        "ground": "grass",
        "features": [
            {"kind": "building", "x": 2, "y": 2, "w": 1e9, "h": 1e9, "name": "Vast"},
            {"kind": "area", "x": -1e9, "y": 0, "w": 1e9, "h": 3, "fill": "water"},
            {"kind": "road", "points": [[0, 0], [1e9, 1e9]], "width": 1e9},
        ],
    }
    started = time.monotonic()
    rows, _objects, labels = rasterise(spec, 20, 12)
    assert time.monotonic() - started < 5
    assert len(rows) == 12 and all(len(r) == 20 for r in rows)
    assert "Vast" in labels


# --- what needs drawing ----------------------------------------------------------


def farm_world() -> WorldMap:
    farm = WorldPlace(id="farm", name="Farmhouse", kind="building")
    yard = WorldPlace(id="yard", name="Back room", kind="area", inside="farm")
    # A read's porch or yard put inside the building is outside it: never in
    # its plan (AfterDark, Oct 18 2026).
    porch = WorldPlace(id="porch", name="The porch", kind="area", inside="farm")
    hills = WorldPlace(id="hills", name="The hills", kind="region")
    truck = WorldThing(id="truck", name="Pickup truck", at="farm", state="rusty")
    delta = WorldDelta(places=[farm, yard, porch, hills], things=[truck], where={"Jane": "farm"})
    place_delta(WorldMap(), delta, "Jane")
    return applied(WorldMap(), delta)


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
        scene_reads="manual",
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


def test_art_a_bundle_carries_is_written_with_the_story_and_a_restart_keeps_it(
    tmp_path: Path, story, cast
):
    from sealedlore.models.world import TerrainPatch
    from sealedlore.storage.scenario import (
        bundle_from_scenario,
        fresh_playthrough,
        scenario_from_bundle,
    )
    from sealedlore.storage.world_art import load_sidecar_art, save_world_art, sidecar_art_path

    story.defaults.world_map = True
    art = WorldArt(terrain=[TerrainPatch(place_id="farm", x=0, y=0, side=1600, rows=["."])])
    bundle = StoryBundle(story=story, cast=cast, world_art=art)
    save_story_bundle(bundle, root=tmp_path)
    assert bundle.world_art is None  # written, not kept on the bundle
    assert load_world_art(story.id, tmp_path).terrain[0].place_id == "farm"
    # A restart carries the art it had; a scenario file keeps it too.
    again = fresh_playthrough(bundle, "again", None, load_world_art(story.id, tmp_path))
    assert again.world_art is not None and again.world_art.terrain[0].side == 1600
    scenario = scenario_from_bundle(bundle, None, art)
    assert bundle_from_scenario(scenario).world_art is not None
    # A plot file's art beside it is read by name; a missing or broken one is none.
    plot = tmp_path / "Farm.md"
    plot.write_text("# World\n", encoding="utf-8")
    assert load_sidecar_art(plot) is None
    save_world_art("x", art, tmp_path)
    sidecar_art_path(plot).write_bytes((tmp_path / "stories" / "x" / "world_art.json").read_bytes())
    assert load_sidecar_art(plot).terrain[0].place_id == "farm"
    sidecar_art_path(plot).write_text("{not json", encoding="utf-8")
    assert load_sidecar_art(plot) is None


def test_art_read_from_a_file_is_cleaned_as_drawn_art_is(tmp_path: Path):
    """A scenario's or a sample's art may be anyone's: its SVG is rebuilt
    from plain shapes when the story loads it, as drawn art always is. A
    sample's art beside its plot file brings no counts, nor a picture
    without its file."""
    from sealedlore.engine.world_art import cleaned_art
    from sealedlore.models.world import MapPicture, WorldSprite
    from sealedlore.storage.world_art import load_sidecar_art, sidecar_art_path

    rect = '<rect x="1" y="1" width="5" height="5" />'
    bad = f'<svg>{rect}<image href="file:///etc/passwd"/><script>x</script></svg>'
    art = cleaned_art(
        WorldArt(
            sprites=[
                WorldSprite(subject="place", subject_id="barn", name="Barn", svg=bad),
                WorldSprite(
                    subject="place",
                    subject_id="nothing",
                    name="N",
                    svg="<svg><script>x</script></svg>",
                ),
            ]
        )
    )
    assert [s.subject_id for s in art.sprites] == ["barn"]
    assert "image" not in art.sprites[0].svg and "script" not in art.sprites[0].svg
    assert cleaned_art(art.model_copy(deep=True)) == art  # clean art comes back the same
    plot = tmp_path / "Farm.md"
    sidecar_art_path(plot).write_text(
        WorldArt(
            pictures=[
                MapPicture(subject="person", subject_id="J", name="J", file="images/map/j.png")
            ],
            pictures_by_day={"2026-10-14": 3},
            failures={"x": 1},
        ).model_dump_json(),
        encoding="utf-8",
    )
    shipped = load_sidecar_art(plot)
    assert shipped is not None
    assert (shipped.pictures, shipped.pictures_by_day, shipped.failures) == ([], {}, {})


def test_a_place_as_it_was_never_borrows_a_changed_state_s_drawing():
    """A barn blown up on one branch was drawn burning on the branch where it
    stands (the review, Oct 15 2026)."""
    from sealedlore.models.world import MapPicture, WorldSprite

    burning = PlaceLayout(place_id="barn", state="blown up", rows=["*"], spec={})
    art = WorldArt(
        layouts={"barn": {"blown up": burning}},
        sprites=[
            WorldSprite(
                subject="place", subject_id="barn", name="The barn", state="blown up", svg="<svg/>"
            )
        ],
        pictures=[
            MapPicture(subject="place", subject_id="barn", name="b", state="blown up", file="f")
        ],
    )
    assert art.layout_shown("barn", "") is None
    assert art.layout_shown("barn", "blown up") is burning
    assert art.layout_shown("barn", "burning") is burning  # a changed place: the latest
    assert art.sprite_shown("barn", "") is None and art.sprite_shown("barn", "ruined") is not None
    assert art.picture_shown("place", "barn", "") is None
    assert art.picture_shown("place", "barn", None) is not None
    intact = PlaceLayout(place_id="barn", state="", rows=["#"], spec={})
    art.layouts["barn"][""] = intact
    assert art.layout_shown("barn", "") is intact and art.layout_shown("barn", "ruined") is intact


def test_a_reply_that_drew_shapes_with_no_svg_root_is_wrapped_and_kept():
    """GLM 5.3 Flash answered a rift with a "<g>…</g>" in a ```svg fence,
    twice (live, Oct 17 2026), and the plain shapes were thrown away as "no
    usable SVG". A fragment of shapes is the document the prompt asked for."""
    fenced = (
        '```svg\n<g> <!-- snow -->\n <circle cx="14" cy="18" r="1.5" fill="#f2f2ee"/>\n</g>\n```'
    )
    svg = clean_svg(fenced)
    assert svg is not None and "<circle" in svg and 'viewBox="0 0 64 64"' in svg
    bare = '<polygon points="30,10 26,18 28,26" fill="#f2f2ee"/>'
    assert clean_svg(bare) is not None
    assert clean_svg("```svg\nI cannot draw that.\n```") is None  # no shapes: nothing
    assert clean_svg("") is None
