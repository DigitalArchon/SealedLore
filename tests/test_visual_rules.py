"""A visual story's rules, pure (engine/visual_rules.py): kinds of thing,
the simple combat, the dead's moves, doors and locks, what is reported seen."""

from __future__ import annotations

import random

import pytest

from sealedlore.engine.area import Area
from sealedlore.engine.visual_rules import (
    DEAD_M_PER_S,
    best_weapon,
    can_open,
    door_id,
    fight,
    is_loud,
    item_actions,
    kind_of,
    opens_with,
    sight_report,
    spawn_creatures,
    step_creatures,
    weapon_bonus,
)
from sealedlore.models.area import ThingRecord
from sealedlore.models.visual import Creature, DoorState


def _yard(rows: list[str], tile_m: float = 2.0) -> Area:
    return Area(
        place_id="yard",
        name="The yard",
        indoor=False,
        look="grass",
        rows=rows,
        tile_m=tile_m,
        objects={},
        labels={},
    )


def test_a_thing_s_kind_is_read_from_its_name_or_the_hint():
    assert kind_of("a tin of beans") == "food"
    assert kind_of("a bottle of water") == "drink"
    assert kind_of("a hunting rifle") == "weapon"
    assert kind_of("the barn key") == "key"
    assert kind_of("a box of matches") == "light"
    assert kind_of("a first aid kit") == "medicine"
    assert kind_of("a folded note") == "paper"
    assert kind_of("a heavy sack") == "container"
    assert kind_of("a strange device") == "thing"
    assert kind_of("a strange device", "tool") == "tool"
    assert kind_of("a tin of beans", "not a kind") == "food"
    assert item_actions("food") == ("eat", "drop")
    assert item_actions("drink") == ("drink", "drop")
    assert item_actions("medicine") == ("use", "drop")
    assert item_actions("key") == ("drop",)


def test_weapons_add_to_the_fight_and_guns_are_loud():
    assert weapon_bonus("a hunting rifle") == 4
    assert weapon_bonus("a kitchen knife") == 1
    assert weapon_bonus("a tin of beans") == 0
    assert is_loud("a shotgun") and not is_loud("an axe")
    carried = [
        ThingRecord(state="taken", name="a knife", by="me"),
        ThingRecord(state="taken", name="a rifle", by="me"),
    ]
    assert best_weapon(carried) == ("a rifle", 4)
    assert best_weapon([]) == ("", 0)


def test_the_simple_combat_the_higher_number_wins_and_a_tie_goes_to_the_defender():
    creature = Creature(area="yard", x=1, y=1, ability=6)
    assert fight(7, ("", 0), creature, attacking=True).won
    assert not fight(6, ("", 0), creature, attacking=True).won  # a tie: the attacked wins
    assert fight(6, ("", 0), creature, attacking=False).won  # attacked at 6 against 6
    assert fight(2, ("a rifle", 4), creature, attacking=False).won
    result = fight(3, ("", 0), creature, attacking=True)
    assert not result.won and "gets the better of you" in result.words
    assert "with the rifle" in fight(2, ("a rifle", 4), creature, attacking=True).words


def test_the_dead_wander_chase_on_sight_and_stop_beside_the_player():
    rows = ["." * 20 for _ in range(6)]
    area = _yard(rows, tile_m=2.0)
    creature = Creature(id="z", area="yard", x=2, y=2, ability=4)
    creatures = {"z": creature}
    # Seen from 10 tiles (20 m): a chase, two tiles a step of ~7 s.
    beside = step_creatures(area, creatures, (12, 2), 7, rng=random.Random(1))
    assert creature.state == "chase" and creature.x > 2 and not beside
    for _ in range(10):
        beside = step_creatures(
            area, creatures, (12, 2), int(4 / DEAD_M_PER_S), rng=random.Random(1)
        )
    assert beside == ["z"] and max(abs(creature.x - 12), abs(creature.y - 2)) == 1
    # Down, it moves no more.
    creature.state = "down"
    assert step_creatures(area, creatures, (0, 0), 60) == []
    # Out of sight (a wall between), a noise draws it toward the noise.
    rows = ["." * 20, "." * 9 + "#" + "." * 10, "." * 9 + "#" + "." * 10, "." * 20]
    walled = _yard(rows, tile_m=2.0)
    deaf = Creature(id="d", area="yard", x=2, y=1, ability=4)
    step_creatures(walled, {"d": deaf}, (12, 1), 1, noise=(12, 1), rng=random.Random(2))
    assert deaf.state in ("drawn", "chase") and (deaf.goal_x, deaf.goal_y) == (12, 1)


def test_the_dead_are_spawned_apart_on_walkable_tiles():
    area = _yard(["." * 20 for _ in range(12)])
    taken = {(5, 5)}
    out = spawn_creatures(area, "the dead", 3, 4, taken, "seed")
    assert len(out) == 3 and all(area.walkable(c.x, c.y) for c in out)
    spots = {(c.x, c.y) for c in out}
    assert len(spots) == 3 and (5, 5) not in spots
    assert out[0].name == "one of the dead"


def test_a_locked_door_opens_with_its_key_or_not_at_all():
    carried = [ThingRecord(state="taken", name="the barn key", by="me")]
    assert can_open(None, []) == (True, "")
    assert can_open(DoorState(), []) == (True, "")
    locked = DoorState(locked=True, key="barn key")
    assert can_open(locked, []) == (False, "Locked. It wants a barn key.")
    assert can_open(locked, carried) == (True, "")
    assert opens_with("store key", carried) is None
    assert opens_with("key", carried) is not None
    assert can_open(DoorState(locked=True, sealed=True), carried)[0] is False
    assert can_open(DoorState(locked=True, forced=True), [])[0] is True
    assert door_id("farm", 3, 4) == "farm:3,4"


def test_what_is_newly_seen_or_heard_is_reported_once():
    rows = ["." * 20, "." * 9 + "#" + "." * 10, "." * 20]
    area = _yard(rows, tile_m=2.0)
    people = {"Maela Orr": (6, 0), "Ben": (14, 1)}  # Ben behind the wall's gap? in sight
    creature = Creature(id="z", area="yard", x=3, y=2, ability=4)
    lines, now = sight_report(area, (1, 0), people, [creature], [])
    assert "Maela Orr is here, about 10 m east." in lines
    assert any(line.startswith("One of the dead, ") and "shambling" in line for line in lines)
    assert set(now) >= {"Maela Orr", "z"}
    again, _now = sight_report(area, (1, 0), people, [creature], now)
    assert again == []  # nothing new


@pytest.mark.parametrize("name", ["a riflex", "beansx", "a keyboard"])
def test_kinds_are_whole_words(name):
    assert kind_of(name) == "thing"
