"""A world kit (the author, Oct 26 2026: the DDA method for the insides of
places, item tables and small stamped pieces): models/kit.py,
engine/kits.py, storage/kits.py, and what a visual story does with one.
Never the network: an invented kit, and the shipped one checked."""

from __future__ import annotations

import os
import random
from pathlib import Path

import pytest

from sealedlore.engine.area import Placement, build_area, find_rooms, name_rooms, openings
from sealedlore.engine.kits import (
    container_rule,
    has_words,
    kind_for,
    kit_layout,
    kit_problems,
    merged,
    orientation,
    rng_for,
    roll,
    stock,
    turned_point,
    turned_side,
    yard_pieces,
)
from sealedlore.engine.tile_sets import item_slugs
from sealedlore.models.area import Found
from sealedlore.models.kit import Kit
from sealedlore.storage.kits import load_kit

KIT = Kit.model_validate(
    {
        "name": "test",
        "tables": {
            "food": {
                "type": "distribution",
                "entries": [
                    {"item": "a tin of beans", "chance": 1, "kind": "food"},
                    {"item": "a tin of soup", "chance": 1, "kind": "food"},
                ],
            },
            "cupboard": {
                "type": "collection",
                "entries": [
                    {"table": "food", "chance": 100, "count": [2, 2]},
                    {"item": "a box of matches", "chance": 0, "kind": "light"},
                    {"item": "a candle", "chance": 100, "count": [3, 3], "kind": "light"},
                ],
            },
            "nothing_much": {"type": "distribution", "entries": [{"chance": 1}]},
            "floor": {"type": "collection", "entries": [{"item": "a crumpled newspaper"}]},
        },
        "containers": [
            {"match": ["medicine cabinet"], "table": "food", "chance": 100},
            {"match": ["cabinet"], "table": "cupboard", "chance": 100},
            {"match": ["dresser"], "table": "cupboard", "chance": 100},
        ],
        "palettes": {
            "house": {
                "k": {"thing": "a kitchen counter", "stock": "cupboard"},
                "b": {"thing": "a bed"},
                "!": {"loot": "floor"},
            }
        },
        "chunks": {
            "nook": {"rows": ["  ", " b"], "labels": {"nook": [1, 1]}},
            "bare": {"rows": ["  ", "  "]},
        },
        "buildings": {
            "house": {
                "match": ["house", "cottage"],
                "variants": [
                    {
                        "id": "house_a",
                        "doors": ["south"],
                        "palettes": ["house"],
                        "rows": [
                            "########",
                            "#kkk___#",
                            "#______#",
                            "#__!___#",
                            "#______#",
                            "###+####",
                        ],
                        "labels": {"kitchen": [2, 2]},
                        "nested": [{"x": 5, "y": 2, "chunks": [["nook", 1], ["bare", 1]]}],
                        "levels": [
                            {
                                "index": 1,
                                "name": "Loft",
                                "rows": [
                                    "########",
                                    "#bb____#",
                                    "#bb____#",
                                    "#______#",
                                    "#____&_#",
                                    "########",
                                ],
                            }
                        ],
                    }
                ],
            }
        },
        "yard": [{"thing": "a woodpile", "w": 2.0, "h": 1.0, "near": ["house"]}],
        "yard_per_building": 2,
    }
)


# --- item tables ----------------------------------------------------------------------


def test_a_collection_rolls_each_entry_and_a_distribution_exactly_one():
    found = merged(roll(KIT, "cupboard", random.Random(1)))
    names = {f.name: f.count for f in found}
    # The food table rolled twice (one of two each time), no matches (0 in
    # a hundred), three candles.
    assert sum(n for name, n in names.items() if name.startswith("a tin of")) == 2
    assert "a box of matches" not in names and names["a candle"] == 3
    assert roll(KIT, "nothing_much", random.Random(1)) == []
    assert roll(KIT, "no such table", random.Random(1)) == []


def test_stocking_is_fixed_by_its_seed_and_its_chance():
    one = stock(KIT, "cupboard", 100, rng_for("story", "kitchen"))
    again = stock(KIT, "cupboard", 100, rng_for("story", "kitchen"))
    assert one == again and one
    assert stock(KIT, "cupboard", 0, rng_for("story", "kitchen")) == []
    assert merged([Found(name="a candle"), Found(name="A candle", count=2)])[0].count == 3


def test_a_container_is_known_by_the_words_of_its_name_the_first_rule_first():
    assert container_rule(KIT, "a locked medicine cabinet").table == "food"
    assert container_rule(KIT, "a filing cabinet").table == "cupboard"
    assert container_rule(KIT, "a battered wooden dresser").table == "cupboard"
    assert container_rule(KIT, "a dressing gown") is None
    assert has_words("a row of metal folding cots", "cot") and not has_words("a cottage", "cot")


# --- templates ------------------------------------------------------------------------


def test_turning_moves_sides_and_tiles_together():
    assert turned_side("south", 1, False) == "west"
    assert turned_side("east", 0, True) == "west"
    assert turned_point(0, 0, 8, 6, 1, False) == (5, 0)
    assert turned_point(7, 5, 8, 6, 2, False) == (0, 0)
    template = KIT.buildings["house"].variants[0]
    for want in ("north", "east", "south", "west"):
        rot, mirror = orientation(template, want, random.Random(0))
        assert turned_side("south", rot, mirror) == want


def test_a_kind_of_building_is_read_from_the_name_then_the_words():
    assert kind_for(KIT, "The cottage") == "house"
    assert kind_for(KIT, "Old Mill", "a house by the river") == "house"
    assert kind_for(KIT, "The mill") is None


@pytest.mark.parametrize("door", ["south", "east", "north", "west"])
def test_a_template_becomes_a_plan_with_its_door_where_the_map_has_it(door):
    layout = kit_layout(KIT, "p", "The cottage", size_m=8, door=door, seed="s")
    rows = layout.rows
    h, w = len(rows), len(rows[0])
    edge = {
        "north": rows[0],
        "south": rows[-1],
        "west": "".join(r[0] for r in rows),
        "east": "".join(r[-1] for r in rows),
    }[door]
    assert "+" in edge
    assert layout.spec["kit"] == "test" and layout.spec["tile_m"] == 1.0
    # The counter is one piece three long, on floor; the loot is a spot.
    counter = next(p for p in layout.spec["pieces"] if p["name"] == "a kitchen counter")
    assert counter["w"] * counter["h"] == 3 and counter["stock"] == "cupboard"
    for p in layout.spec["pieces"]:
        for dy in range(p["h"]):
            for dx in range(p["w"]):
                assert rows[p["y"] + dy][p["x"] + dx] == "_"
    assert len(layout.spec["loot"]) == 1 and layout.spec["loot"][0]["table"] == "floor"
    # The loft turned the same way, its stairs over the plan's own.
    loft = layout.levels[0]
    assert loft.name == "Loft" and len(loft.rows) == h and len(loft.rows[0]) == w
    assert any(p["name"] == "a bed" and p["w"] * p["h"] == 4 for p in loft.spec["pieces"])
    # The same seed, the same inside.
    assert kit_layout(KIT, "p", "The cottage", size_m=8, door=door, seed="s") == layout


def test_a_chunk_is_stamped_by_weight_and_keeps_what_is_under_its_blanks():
    seen = set()
    for n in range(20):
        layout = kit_layout(KIT, "p", "The cottage", size_m=8, door="south", seed=f"s{n}")
        beds = [p for p in layout.spec["pieces"] if p["name"] == "a bed"]
        seen.add(len(beds))
        if beds:
            assert "nook" in layout.labels
    assert seen == {0, 1}


def test_a_kit_made_plan_is_built_as_any_plan_its_furniture_stocked_by_table():
    from tests.test_area import world_and_art

    world, art = world_and_art()
    # A seed that stamped no nook: one label in the room names it.
    layout = next(
        lay
        for n in range(40)
        if "nook"
        not in (
            lay := kit_layout(KIT, "house", "The house", size_m=40, door="south", seed=f"s{n}")
        ).labels
    )
    art.layouts["house"] = {"": layout}
    house = build_area(world, art, "house")
    counter = next(p for p in house.placements if p.name == "a kitchen counter")
    assert (
        counter.kind == "furniture" and counter.stock == "cupboard" and counter.w * counter.h == 3
    )
    assert house.loot and house.loot[0][2] == "floor"
    assert any(r.name == "kitchen" for r in house.rooms)
    assert house.tile_m == 1.0


def test_name_rooms_takes_a_kit_label_as_the_name_only_when_told():
    rows = ["#####", "#___#", "#___#", "#####"]
    room_rows = find_rooms(rows, {}, openings(rows))[0]
    name_rooms(room_rows, {"pantry": (2, 2)})
    assert room_rows[0].name != "pantry"
    name_rooms(room_rows, {"pantry": (2, 2)}, named=True)
    assert room_rows[0].name == "pantry"


# --- yards ----------------------------------------------------------------------------


def test_yard_pieces_stand_on_open_ground_near_their_building_and_cut_nothing_off():
    rows = [
        "..........",
        "..........",
        "..####....",
        "..####....",
        "..##+#....",
        "..........",
        "..........",
    ]
    keep = {(4, 5), (3, 5), (5, 5)}
    pieces = yard_pieces(
        KIT,
        rows,
        {"house": (2, 2, 5, 4)},
        {"house": "The house"},
        1.0,
        "seed",
        keep=keep,
        reach_from=[(4, 5)],
    )
    assert 1 <= len(pieces) <= 2
    for p in pieces:
        assert p["name"] == "a woodpile" and (p["w"], p["h"]) == (2, 1)
        tiles = {(p["x"] + dx, p["y"]) for dx in range(p["w"])}
        assert not tiles & keep and all(rows[y][x] == "." for x, y in tiles)
    assert yard_pieces(KIT, rows, {"barn": (2, 2, 5, 4)}, {"barn": "The barn"}, 1.0, "s") == []


# --- checks ---------------------------------------------------------------------------


def test_a_kit_s_faults_are_named():
    bad = KIT.model_copy(deep=True)
    variant = bad.buildings["house"].variants[0]
    variant.rows = [*variant.rows[:-1], "########"]  # no door left on the south wall
    variant.rows[1] = "#kkkQ__#"
    variant.levels[0].rows = variant.levels[0].rows[:3]
    variant.nested[0].chunks.append(("missing", 1))
    bad.tables["cupboard"].entries[0].table = "nope"
    problems = kit_problems(bad, has_picture=lambda name: name != "a bed")
    text = "\n".join(problems)
    assert "no door on its south wall" in text
    assert "'Q' is no tile and not in the legend" in text
    assert "not on the template's grid" in text
    assert "no chunk 'missing'" in text
    assert "no table 'nope'" in text
    assert "no picture of 'a bed'" in text
    assert kit_problems(KIT) == []


SAMPLES = Path(__file__).resolve().parent.parent / "SampleStories"


def test_the_shipped_kit_is_whole_and_every_piece_has_its_picture():
    kit = load_kit("abandoned", SAMPLES)
    assert kit is not None and kit.buildings and kit.tables
    folders = [
        SAMPLES / "tilesets" / "abandoned" / "items",
        SAMPLES / "tilesets" / "doomsville" / "items",
    ]

    def has_picture(name: str) -> bool:
        return any(
            (d / f"{s}{ext}").exists()
            for d in folders
            for s in item_slugs(name)
            for ext in (".webp", ".png")
        )

    assert kit_problems(kit, has_picture) == []
    # Every item a table can give is drawn too: it may lie on a floor.
    items = {e.item for t in kit.tables.values() for e in t.entries if e.item}
    assert sorted(n for n in items if not has_picture(n)) == []
    assert sorted(p.thing for p in kit.yard if not has_picture(p.thing)) == []


# --- a visual story with a kit ----------------------------------------------------------

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def session(tmp_path, story, cast, monkeypatch):
    """A visual story over the test world, its house's plan taken away so
    the kit makes one, and the kit given to it."""
    pytest.importorskip("PySide6")
    from sealedlore.engine.session import StorySession
    from sealedlore.providers.mock import MockChatProvider
    from sealedlore.storage.repository import save_story_bundle
    from sealedlore.storage.world_art import save_world_art
    from tests.test_visual import _visual_bundle

    bundle, art = _visual_bundle(story, cast)
    del art.layouts["house"]
    save_story_bundle(bundle, root=tmp_path)
    save_world_art(story.id, art, tmp_path)
    from sealedlore.models.config import Config

    s = StorySession.load(story.id, Config(), MockChatProvider(), root=tmp_path)
    monkeypatch.setattr(type(s), "visual_kit", lambda self: KIT)
    return s


def test_a_building_with_no_plan_gets_one_from_the_kit_once(session):
    assert session.visual_kit_plans() == ["The house"]
    layout = session.world_art.layouts["house"][""]
    assert layout.spec["template"] == "house_a" and layout.model == "kit:test"
    assert session.visual_kit_plans() == []  # kept: made once


def test_an_area_is_stocked_the_first_time_it_is_entered_and_searched_to_be_seen(session):
    from sealedlore.models.area import LiveArea, Spot

    session.visual_kit_plans()
    house = session.area("house")
    counter = next(p for p in house.placements if p.name == "a kitchen counter")
    state = session.area_state()
    state.spots["Serrik Vaun"] = Spot(area="house", x=counter.x, y=counter.y + 1)
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    session.visual_report()
    state = session.area_state()
    assert "house" in state.stocked
    record = state.things[counter.id]
    assert record.stocked and not record.searched
    assert {f.name for f in record.contents} >= {"a candle"}
    newspaper = [r for r in state.things.values() if r.name == "a crumpled newspaper"]
    assert len(newspaper) == 1 and newspaper[0].state == "dropped"
    # Entered again, nothing is rolled again.
    session.visual_report()
    assert session.area_state().things[counter.id].contents == record.contents
    assert (
        len([r for r in session.area_state().things.values() if r.name == "a crumpled newspaper"])
        == 1
    )
    # Searched: seen, half a minute gone.
    seconds = session.area_state().seconds
    line = session.visual_search(counter.id)
    assert line.startswith("You search the kitchen counter and find") and "candle" in line
    state = session.area_state()
    assert state.things[counter.id].searched and state.seconds == seconds + 30


def test_a_look_is_told_what_the_tables_decided_and_cannot_add_to_it(session):
    from sealedlore.engine.look import LookFind, LookReply, look_messages
    from sealedlore.models.area import LiveArea, Spot

    session.visual_kit_plans()
    house = session.area("house")
    counter = next(p for p in house.placements if p.name == "a kitchen counter")
    state = session.area_state()
    state.spots["Serrik Vaun"] = Spot(area="house", x=counter.x, y=counter.y + 1)
    session.story.area_live = LiveArea(leaf_id=None, held="Serrik Vaun", state=state)
    session.visual_report()
    rect = (counter.x, counter.y, counter.x + counter.w - 1, counter.y)
    text = look_messages(
        world="",
        place="The house",
        area=session.area("house"),
        rect=rect,
        records=session.area_state().things,
        player="Serrik Vaun",
    )[1].text
    assert "a kitchen counter — in or on it:" in text and ", and nothing else" in text
    before = list(session.area_state().things[counter.id].contents)
    session.apply_look_reply(
        "house",
        rect,
        LookReply(
            see="A counter.", found=[LookFind(name="a revolver", kind="weapon", inside=counter.id)]
        ),
    )
    record = session.area_state().things[counter.id]
    assert record.contents == before and record.searched


def test_a_design_s_container_is_found_by_its_phrase_in_a_kit_made_inside():
    from sealedlore.engine.kits import has_words as found

    assert found("a locked medicine cabinet", "medicine cabinet")
    assert not found("a medicine chest", "medicine cabinet")


# --- drawing ---------------------------------------------------------------------------


def test_a_long_run_of_furniture_repeats_its_picture_along_it():
    pytest.importorskip("PySide6")
    from sealedlore.gui.area_render import _run_middles

    shelf = Placement(id="s", name="a shelf", x=2, y=0, w=6, h=1, verbs=(), kind="furniture")
    assert len(_run_middles(shelf, 40, 80.0)) == 3
    one = Placement(id="t", name="a table", x=2, y=0, w=2, h=1, verbs=(), kind="furniture")
    assert _run_middles(one, 40, 80.0) == [120.0]
    thing = Placement(id="u", name="a crate", x=2, y=0, w=6, h=1, verbs=(), kind="object")
    assert len(_run_middles(thing, 40, 80.0)) == 1


def test_a_kit_room_s_name_is_no_place_mark_on_the_play_view(session):
    """Every room of the kit's farmhouse showed its name as a gold diamond,
    the play view's mark for a place in the area with nothing drawn."""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from sealedlore.gui.area_view import AreaView
    from sealedlore.gui.visual_presenter import VisualPresenter

    app = QApplication.instance() or QApplication([])
    del app
    session.visual_kit_plans()
    house = session.area("house")
    assert any(r.name == "kitchen" for r in house.rooms) or "kitchen" in house.labels
    presenter = VisualPresenter(AreaView(), lambda: session, None)
    marks = presenter._place_marks(house, session, session._art_read())
    assert not {m.name for m in marks} & {r.name for r in house.rooms if r.name}


def test_a_burnt_out_building_gets_a_gutted_inside():
    """Live (Oct 26 2026), a farmhouse "burnt to its frame" had the kit's
    whole inside, a rifle by the door and a stocked pantry."""
    kit = load_kit("abandoned")
    whole = kit_layout(kit, "b1", "farmhouse", size_m=24, door="south", seed="s", state="")
    burnt = kit_layout(kit, "b1", "farmhouse", size_m=24, door="south", seed="s", state="burnt out")
    assert whole is not None and burnt is not None
    assert burnt.levels == [] and whole.levels  # the upper floor went with the fire
    assert whole.spec["pieces"] and burnt.spec["pieces"]
    assert {p["name"] for p in burnt.spec["pieces"]} == {"scrap-pile"}
    assert all(lt["table"] == "ruins" for lt in burnt.spec["loot"]) and burnt.spec["loot"]
    rows = burnt.rows
    assert any(":" in row for row in rows) and not any("&" in row for row in rows)
    # The walls and the door as they were.
    assert "+" in "".join(rows) and "#" in "".join(rows)


def test_a_thing_gives_light_by_a_whole_word_and_not_when_it_is_out():
    from sealedlore.engine.tile_sets import gives_light

    assert gives_light("a brass lantern") and gives_light("campfire")
    assert gives_light("a burning oil drum fire") and gives_light("a fireplace")
    assert not gives_light("a stack of firewood") and not gives_light("stacked-firewood")
    assert not gives_light("dead-campfire") and not gives_light("a street lamp, dark")
    assert not gives_light("a rusted stove") and not gives_light("a flashlight")


def test_an_abandoned_place_keeps_little_of_worth_and_half_the_rest():
    from sealedlore.engine.kits import picked_over

    rolled = [Found(name="a tin of beans", count=4, kind="food")] * 50 + [
        Found(name="a bent spoon", count=4, kind="thing")
    ] * 50
    left = picked_over(rolled, rng_for("story", "cupboard"), lambda f: f.kind)
    worth = [f for f in left if f.kind == "food"]
    plain = [f for f in left if f.kind == "thing"]
    assert 2 <= len(worth) <= 15 and 18 <= len(plain) <= 38
    assert all(f.count == 2 for f in left)
    assert left == picked_over(rolled, rng_for("story", "cupboard"), lambda f: f.kind)
