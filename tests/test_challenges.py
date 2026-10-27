"""Challenges (engine/challenges.py): who challenges, and what they do when
the player ignores them, by standing and the odds."""

from sealedlore.engine.challenges import (
    STEP_ATTACK,
    STEP_STAND_DOWN,
    STEP_WARN,
    danger_words,
    escalate,
    is_challenger,
)
from sealedlore.engine.npc import parse_npc
from sealedlore.models.visual import PersonSheet


def test_a_sheet_s_demand_or_a_hostile_standing_makes_a_challenger():
    assert is_challenger(PersonSheet(challenges="a toll for the road"), "stranger")
    assert is_challenger(PersonSheet(), "hostile")
    assert not is_challenger(PersonSheet(), "stranger")
    assert not is_challenger(PersonSheet(challenges="  "), "friend")


def test_a_neutral_warns_twice_then_attacks_or_backs_off_by_the_odds():
    # Evenly matched: a second warning, then an attack.
    assert escalate(1, "stranger", 0) == STEP_WARN
    assert escalate(2, "stranger", 0) == STEP_ATTACK
    # The player looks the stronger: a second warning, then they let it go.
    assert escalate(1, "wary", 2) == STEP_WARN
    assert escalate(2, "wary", 2) == STEP_STAND_DOWN


def test_a_hostile_attacks_on_the_first_ignore_unless_outmatched():
    assert escalate(1, "hostile", 0) == STEP_ATTACK
    assert escalate(1, "hostile", -3) == STEP_ATTACK
    # Facing the stronger they warn first, and attack unless far outmatched.
    assert escalate(1, "hostile", 2) == STEP_WARN
    assert escalate(2, "hostile", 2) == STEP_ATTACK
    assert escalate(2, "hostile", 4) == STEP_STAND_DOWN


def test_a_friend_never_attacks_over_a_demand():
    assert escalate(1, "guest", -5) == STEP_WARN
    assert escalate(2, "friend", -5) == STEP_STAND_DOWN


def test_the_odds_in_words():
    assert "far more dangerous" in danger_words(4)
    assert danger_words(2) == "the player looks the more dangerous"
    assert "outmatched" in danger_words(-2)
    assert "evenly" in danger_words(1)


def test_a_reply_s_demand_is_read_as_given_or_left_unsaid():
    assert parse_npc('{"say": "Stop.", "intent": "stay"}').demand is None
    assert parse_npc('{"say": "Toll.", "intent": "stay", "demand": " a tin of food "}').demand == (
        "a tin of food"
    )
    assert parse_npc('{"say": "Go on.", "intent": "stay", "demand": ""}').demand == ""
