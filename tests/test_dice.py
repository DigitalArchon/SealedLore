"""Dice mode (§4.2): the table, the bands, and the line that goes to the model."""

from __future__ import annotations

import random
from collections import Counter

import pytest

from sealedlore.engine.competence import build_inference_messages, parse_tiers
from sealedlore.engine.dice import (
    DIFFICULTY_ORDER,
    MAX_CHANCE,
    MIN_CHANCE,
    TIER_ORDER,
    band_for,
    chance_for,
    chip_text,
    explain,
    roll_for,
    tier_for,
)
from sealedlore.models.character import Character, Competence

JANE = Character(
    name="Jane Moss",
    competence=Competence(tiers={"driving": "skilled", "diplomacy": "poor"}),
)


def test_better_tiers_and_easier_tasks_never_lower_the_odds():
    for difficulty in DIFFICULTY_ORDER:
        chances = [chance_for(tier, difficulty) for tier in TIER_ORDER]
        assert chances == sorted(chances)
    for tier in TIER_ORDER:
        chances = [chance_for(tier, difficulty) for difficulty in DIFFICULTY_ORDER]
        assert chances == sorted(chances, reverse=True)


def test_nothing_is_certain_and_nothing_is_hopeless():
    assert chance_for("legendary", "trivial") == MAX_CHANCE
    assert chance_for("none", "desperate") == MIN_CHANCE


def test_competent_at_standard_splits_as_documented():
    bands = Counter(band_for(value, chance_for("competent", "standard")) for value in range(1, 101))
    assert bands == {
        "critical_success": 5,
        "success": 35,
        "success_at_a_cost": 10,
        "failure": 45,
        "critical_failure": 5,
    }


@pytest.mark.parametrize("chance", [MIN_CHANCE, 30, 50, 92, MAX_CHANCE])
def test_every_value_lands_in_exactly_one_band_and_the_edges_are_right(chance):
    assert band_for(1, chance) == "critical_success"
    assert band_for(100, chance) == "critical_failure"
    assert band_for(chance, chance) in ("success_at_a_cost", "critical_success")
    assert band_for(chance + 1, chance) in ("failure", "critical_failure")


def test_the_tier_comes_from_the_chosen_skill_or_defaults_to_competent():
    assert tier_for(JANE, "driving") == "skilled"
    assert tier_for(JANE, "swimming") == "competent"
    assert tier_for(JANE, None) == "competent"
    assert tier_for(None, "driving") == "competent"


def test_a_roll_records_what_it_was_made_against():
    roll = roll_for(JANE, "driving", "hard", random.Random(7))
    assert roll.die == 100 and 1 <= roll.value <= 100
    assert roll.tier == "skilled" and roll.domain == "driving" and roll.difficulty == "hard"
    assert roll.target == chance_for("skilled", "hard")
    assert roll.band == band_for(roll.value, roll.target)


def test_the_explanation_and_chip_say_why():
    roll = roll_for(JANE, "driving", "hard", random.Random(7))
    line = explain(roll, "Jane Moss")
    assert "Jane Moss is skilled at driving" in line
    assert f"needed {roll.target} or under" in line and f"rolled {roll.value}" in line
    assert chip_text(roll).startswith(f"🎲 {roll.value} / {roll.target} · ")


# --- competence suggestions ------------------------------------------------------


def test_tiers_are_parsed_and_unknown_tiers_dropped():
    reply = '```json\n{"Driving": "Skilled", "tracking": "competent", "cooking": "godlike"}\n```'
    assert parse_tiers(reply) == {"driving": "skilled", "tracking": "competent"}


@pytest.mark.parametrize("reply", ["no idea", '{"cooking": "godlike"}', "[1, 2]"])
def test_an_unusable_reply_is_an_error(reply):
    with pytest.raises(ValueError):
        parse_tiers(reply)


def test_the_inference_prompt_carries_the_sheet_and_a_slice_of_the_world():
    sheet = Character(name="Jane", summary="A medic.", competence=Competence(notes="Reckless."))
    messages = build_inference_messages(sheet, "The dead walk. " * 500)
    user = messages[-1].text
    assert "Name: Jane" in user and "Reckless." in user
    assert len(user) < 2500
