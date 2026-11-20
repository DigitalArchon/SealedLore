"""Lore fired exactly as SillyTavern 1.19.1 fires it (engine/tavern_firing.py):
each rule of its checkWorldInfo, on small made-up lorebooks. Pure: no
session, no network; a seeded random source for probability and groups."""

from __future__ import annotations

import random

import pytest

from sealedlore.engine.tavern_firing import (
    AND_ALL,
    AND_ANY,
    NOT_ALL,
    NOT_ANY,
    FireEntry,
    TavernSettings,
    Timed,
    fire,
    fire_entry,
    replay,
)

WORDS = lambda text: len(text.split())  # noqa: E731 - a token a word


def entry(id: str, keys=(), **fields) -> FireEntry:  # noqa: A002
    return FireEntry(
        id=id, title=id, content=fields.pop("content", f"About {id}."), keys=tuple(keys), **fields
    )


def run(entries, messages, *, settings=None, length=None, timed=None, context=1000, **kw):
    return fire(
        entries,
        messages,
        settings=settings or TavernSettings(),
        context=context,
        count_tokens=WORDS,
        timed=timed or Timed(),
        length=len(messages) if length is None else length,
        rng=kw.pop("rng", random.Random(1)),
        **kw,
    )


# --- the scan -----------------------------------------------------------------


def test_a_key_fires_within_the_scan_depth_and_not_beyond():
    wolf = entry("wolf", ["wolf"])
    messages = ["John: I listen.", "Narrator: The night is quiet.", "John: A wolf howls."]
    assert run([wolf], messages).ids == []  # two messages back by default
    assert run([wolf], messages, settings=TavernSettings(scan_depth=3)).ids == ["wolf"]
    assert run([entry("wolf", ["wolf"], scan_depth=3)], messages).ids == ["wolf"]


def test_whole_words_case_and_patterns():
    messages = ["John: The Wolves are near.", "Narrator: ..."]
    assert run([entry("w", ["wolf"])], messages).ids == []  # whole words by default
    assert run([entry("w", ["wolve"])], messages).ids == []
    assert run([entry("w", ["wolve"], match_whole_words=False)], messages).ids == ["w"]
    assert run([entry("w", ["Wolves"], case_sensitive=True)], messages).ids == ["w"]
    assert run([entry("w", ["wolves"], case_sensitive=True)], messages).ids == []
    assert run([entry("w", ["/wol(f|ves)/i"])], messages).ids == ["w"]
    # A key of several words is matched as it stands, inside other words too.
    assert run([entry("w", ["ves are"])], messages).ids == ["w"]


def test_names_are_scanned_with_the_messages():
    """SillyTavern scans "name: text", so a key that is a name fires on every
    message that person sends (include_names, on by default)."""
    messages = ["Serrik: I wait.", "Narrator: Rain."]
    assert run([entry("s", ["Serrik"])], messages).ids == ["s"]


@pytest.mark.parametrize(
    ("logic", "secondary", "fires"),
    [
        (AND_ANY, ["moon", "nothing"], True),
        (AND_ANY, ["nothing"], False),
        (NOT_ALL, ["moon", "nothing"], True),
        (NOT_ALL, ["moon"], False),
        (NOT_ANY, ["nothing"], True),
        (NOT_ANY, ["moon"], False),
        (AND_ALL, ["moon", "night"], True),
        (AND_ALL, ["moon", "nothing"], False),
    ],
)
def test_secondary_keys_by_their_logic(logic, secondary, fires):
    messages = ["John: A wolf under the moon at night.", "Narrator: ..."]
    wolf = entry("w", ["wolf"], secondary=tuple(secondary), selective=True, logic=logic)
    assert (run([wolf], messages).ids == ["w"]) is fires


# --- what fires, in what order ----------------------------------------------------


def test_constant_sticky_and_decorators_fire_without_a_key():
    always = entry("a", constant=True)
    loud = entry("l", decorators=("@@activate",))
    quiet = entry("q", ["wolf"], constant=True, decorators=("@@dont_activate",))
    result = run([always, loud, quiet], ["John: A wolf."])
    assert sorted(result.ids) == ["a", "l"]
    assert result.why["a"] == "constant" and result.not_fired["q"] == "@@dont_activate"


def test_order_lowest_first_in_the_prompt():
    entries = [
        entry("a", ["wolf"], order=10),
        entry("b", ["wolf"], order=300),
        entry("c", ["wolf"], order=100),
    ]
    assert run(entries, ["John: A wolf."]).ids == ["a", "c", "b"]


def test_disabled_triggers_and_character_filter():
    messages = ["John: A wolf."]
    assert run([entry("w", ["wolf"], disabled=True)], messages).ids == []
    on_continue = entry("w", ["wolf"], triggers=("continue",))
    assert run([on_continue], messages).ids == []
    assert run([on_continue], messages, trigger="continue").ids == ["w"]
    only_elena = entry("w", ["wolf"], filter_names=("Elena Ruiz",))
    assert run([only_elena], messages).ids == []
    assert run([only_elena], messages, present=["Elena Ruiz"]).ids == ["w"]
    not_elena = entry("w", ["wolf"], filter_names=("Elena Ruiz",), filter_exclude=True)
    assert run([not_elena], messages, present=["Elena Ruiz"]).ids == []


# --- recursion ---------------------------------------------------------------------


def test_what_fired_is_read_again():
    wolf = entry("wolf", ["wolf"], content="Wolves fear the Hollow King.")
    king = entry("king", ["Hollow King"])
    result = run([wolf, king], ["John: A wolf."])
    assert sorted(result.ids) == ["king", "wolf"]
    assert result.why["king"] == "key “Hollow King”, in another entry's text"
    assert run([wolf, king], ["John: A wolf."], settings=TavernSettings(recursive=False)).ids == [
        "wolf"
    ]


def test_recursion_flags():
    messages = ["John: A wolf."]
    stops = entry("wolf", ["wolf"], content="The Hollow King.", prevent_recursion=True)
    assert run([stops, entry("king", ["Hollow King"])], messages).ids == ["wolf"]
    wolf = entry("wolf", ["wolf"], content="The Hollow King.")
    shy = entry("king", ["Hollow King"], exclude_recursion=True)
    assert run([wolf, shy], messages).ids == ["wolf"]
    waits = entry("cave", ["wolf"], delay_until_recursion=1)
    assert run([waits], messages).ids == []  # nothing else fired: no recursion pass
    assert sorted(run([wolf, waits], messages).ids) == ["cave", "wolf"]


def test_max_recursion_steps():
    chain = [
        entry("a", ["wolf"], content="bear"),
        entry("b", ["bear"], content="crow"),
        entry("c", ["crow"]),
    ]
    assert len(run(chain, ["John: A wolf."]).ids) == 3
    assert (
        len(run(chain, ["John: A wolf."], settings=TavernSettings(max_recursion_steps=2)).ids) == 2
    )


def test_min_activations_reach_further_back():
    old = entry("old", ["lantern"])
    messages = ["John: I wait.", "Narrator: Rain.", "John: I light the lantern."]
    assert run([old], messages).ids == []
    settings = TavernSettings(min_activations=1)
    assert run([old], messages, settings=settings).ids == ["old"]
    # SillyTavern checks the limit before reaching one further: a limit of 2
    # still reaches the third message, a limit of 1 doesn't.
    assert run(
        [old], messages, settings=TavernSettings(min_activations=1, min_activations_depth_max=2)
    ).ids == ["old"]
    assert (
        run(
            [old], messages, settings=TavernSettings(min_activations=1, min_activations_depth_max=1)
        ).ids
        == []
    )


# --- probability, groups, budget -------------------------------------------------------


def test_probability_is_rolled_and_said():
    rare = entry("rare", ["wolf"], probability=30)
    rolls = [run([rare], ["John: A wolf."], rng=random.Random(seed)).ids for seed in range(200)]
    share = sum(1 for ids in rolls if ids) / len(rolls)
    assert 0.2 < share < 0.4
    missed = next(
        run([rare], ["John: A wolf."], rng=random.Random(s))
        for s in range(200)
        if not run([rare], ["John: A wolf."], rng=random.Random(s)).ids
    )
    assert missed.not_fired["rare"] == "its 30% chance didn't come up"
    assert run(
        [entry("r", ["wolf"], probability=30, use_probability=False)], ["John: A wolf."]
    ).ids == ["r"]


def test_an_inclusion_group_has_one_winner():
    a = entry("a", ["wolf"], groups=("beast",), group_text="beast")
    b = entry("b", ["wolf"], groups=("beast",), group_text="beast")
    winners = {tuple(run([a, b], ["John: A wolf."], rng=random.Random(s)).ids) for s in range(50)}
    assert winners == {("a",), ("b",)}
    chosen = entry("c", ["wolf"], groups=("beast",), group_text="beast", group_override=True)
    assert run([a, b, chosen], ["John: A wolf."]).ids == ["c"]
    loser = run([a, b, chosen], ["John: A wolf."]).not_fired
    assert loser["a"] == "its group “beast”: c won"


def test_group_scoring_keeps_the_best_match():
    one = entry("one", ["wolf"], groups=("beast",), group_text="beast")
    two = entry("two", ["wolf", "moon"], groups=("beast",), group_text="beast")
    settings = TavernSettings(use_group_scoring=True)
    assert run([one, two], ["John: A wolf and the moon."], settings=settings).ids == ["two"]


def test_the_budget_leaves_out_what_overflows_but_not_what_ignores_it():
    big = entry("big", ["wolf"], content="word " * 30, order=300)
    small = entry("small", ["wolf"], content="tiny", order=200)
    free = entry("free", ["wolf"], content="word " * 30, order=100, ignore_budget=True)
    # 25% of a 100-token context is 25: the big one alone is over it.
    result = run([big, small, free], ["John: A wolf."], context=100)
    assert result.budget == 25
    assert "big" not in result.ids and "free" in result.ids
    assert result.not_fired["big"] == "over the lore budget"
    capped = run([big], ["John: A wolf."], settings=TavernSettings(budget_cap=10), context=10_000)
    assert capped.budget == 10 and capped.ids == []


# --- timed effects ---------------------------------------------------------------------


def test_sticky_keeps_firing_then_cooldown_follows():
    wolf = entry("wolf", ["wolf"], sticky=2, cooldown=2)
    entries = {"wolf": wolf}
    # Fired at a chat of 4 messages.
    turns = [(4, ["wolf"])]
    quiet = ["John: Nothing here."]
    assert (
        run([wolf], quiet, timed=replay(turns, entries), length=5).why["wolf"].startswith("sticky")
    )
    # At 6 the sticky has ended and the cooldown it starts holds it back.
    turns_6 = [(4, ["wolf"]), (5, ["wolf"])]
    result = run([wolf], ["John: A wolf."], timed=replay(turns_6, entries), length=6)
    assert result.ids == [] and result.not_fired["wolf"] == "on cooldown"
    # A sticky's cooldown starts when a scan next sees the sticky ended, as
    # SillyTavern's does: here at 9, so it still holds there and at 10...
    later = replay(turns_6, entries)
    assert run([wolf], ["John: A wolf."], timed=later, length=9).not_fired["wolf"] == "on cooldown"
    # ...and the entry fires by its key again at 11 (each turn is recorded,
    # fired or not, as the scans that saw it).
    turns_11 = [*turns_6, (9, []), (10, [])]
    assert run([wolf], ["John: A wolf."], timed=replay(turns_11, entries), length=11).ids == [
        "wolf"
    ]


def test_cooldown_alone_and_delay():
    wolf = entry("wolf", ["wolf"], cooldown=3)
    timed = replay([(2, ["wolf"])], {"wolf": wolf})
    assert run([wolf], ["John: A wolf."], timed=timed, length=4).not_fired["wolf"] == "on cooldown"
    late = entry("late", ["wolf"], delay=5)
    assert run([late], ["John: A wolf."], length=4).not_fired["late"].startswith("delayed until")
    assert run([late], ["John: A wolf."], length=5).ids == ["late"]


def test_a_sticky_member_wins_its_group():
    a = entry("a", ["wolf"], groups=("g",), group_text="g", sticky=3)
    b = entry("b", ["wolf"], groups=("g",), group_text="g", group_override=True)
    timed = replay([(1, ["a"])], {"a": a, "b": b})
    assert run([a, b], ["John: A wolf."], timed=timed, length=2).ids == ["a"]


def test_a_branch_has_its_own_state():
    """Replayed from the path: a turn the branch doesn't have isn't counted."""
    wolf = entry("wolf", ["wolf"], cooldown=5)
    assert run([wolf], ["John: A wolf."], timed=replay([], {"wolf": wolf}), length=3).ids == [
        "wolf"
    ]


# --- reading an entry ---------------------------------------------------------------------


def test_a_world_file_entry_and_a_cards_entry_read_alike():
    world = {
        "key": ["wolf"],
        "keysecondary": ["moon"],
        "selective": True,
        "selectiveLogic": AND_ALL,
        "order": 250,
        "probability": 40,
        "useProbability": True,
        "group": "beast, night",
        "groupOverride": True,
        "groupWeight": 30,
        "sticky": 2,
        "cooldown": 3,
        "delay": 4,
        "scanDepth": 5,
        "caseSensitive": True,
        "matchWholeWords": False,
        "excludeRecursion": True,
        "preventRecursion": True,
        "delayUntilRecursion": 2,
        "triggers": ["continue"],
        "ignoreBudget": True,
        "characterFilter": {"names": ["Elena"], "isExclude": True},
    }
    card = {
        "keys": ["wolf"],
        "secondary_keys": ["moon"],
        "selective": True,
        "insertion_order": 250,
        "extensions": {
            "selectiveLogic": AND_ALL,
            "probability": 40,
            "useProbability": True,
            "group": "beast, night",
            "group_override": True,
            "group_weight": 30,
            "sticky": 2,
            "cooldown": 3,
            "delay": 4,
            "scan_depth": 5,
            "case_sensitive": True,
            "match_whole_words": False,
            "exclude_recursion": True,
            "prevent_recursion": True,
            "delay_until_recursion": 2,
            "triggers": ["continue"],
            "ignore_budget": True,
        },
    }
    common = dict(id="w", title="Wolf", content="x", keywords=[], always_on=False, enabled=True)
    a = fire_entry(raw=world, numbered=True, **common)
    b = fire_entry(raw=card, numbered=False, **common)
    assert a.groups == b.groups == ("beast", "night")
    for name in (
        "keys",
        "secondary",
        "logic",
        "order",
        "probability",
        "group_override",
        "group_weight",
        "sticky",
        "cooldown",
        "delay",
        "scan_depth",
        "case_sensitive",
        "match_whole_words",
        "exclude_recursion",
        "prevent_recursion",
        "delay_until_recursion",
        "triggers",
        "ignore_budget",
    ):
        assert getattr(a, name) == getattr(b, name), name
    assert a.filter_names == ("Elena",) and a.filter_exclude


def test_an_entry_written_here_fires_by_its_keywords_at_the_defaults():
    plain = fire_entry(
        id="k", title="Keep", content="x", keywords=["keep"], always_on=True, enabled=True, raw=None
    )
    assert plain.keys == ("keep",) and plain.constant and plain.order == 100
    assert plain.standing  # always on and nothing can stop it: the cached system block
    assert not entry("s", ["x"], constant=True, sticky=2).standing
    assert not entry("p", ["x"], constant=True, probability=50).standing
