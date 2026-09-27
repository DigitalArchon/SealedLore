"""Dice mode (§4.2): the app rolls, the model narrates what it is given.

One table, one place to tune. A roll is d100, low is good, against a target
chance that comes from two things only:

- the acting character's tier in the skill the author picked for this roll
  (or "competent" when there is no tier for it — the spec's default), and
- the per-message difficulty hint.

There is no character-sheet maths beyond that. Tiers are the author's ruling
on a character's ceiling (§4.1); this just turns the ruling into odds.

The five outcomes sit inside the target like this (competent, standard: 50):

    1-5 critical success | 6-40 success | 41-50 success at a cost |
    51-95 failure | 96-100 critical failure

so a roll that only just made it costs something, and the critical ranges
scale with how likely success was — a legendary duellist fumbles on a 100
only, and a hopeless one rarely lands a critical.
"""

from __future__ import annotations

import random

from sealedlore.models.character import Character, CompetenceTier
from sealedlore.models.node import Difficulty, Roll, RollBand

DIE = 100

TIER_ORDER: tuple[CompetenceTier, ...] = (
    "none",
    "poor",
    "competent",
    "skilled",
    "expert",
    "legendary",
)
DIFFICULTY_ORDER: tuple[Difficulty, ...] = ("trivial", "easy", "standard", "hard", "desperate")

# Chance of success at standard difficulty, in percent.
BASE_CHANCE: dict[CompetenceTier, int] = {
    "none": 10,
    "poor": 30,
    "competent": 50,
    "skilled": 65,
    "expert": 80,
    "legendary": 92,
}

# Added to the base chance. Asymmetric on purpose: "hard" should hurt a
# competent character more than "easy" helps them.
DIFFICULTY_SHIFT: dict[Difficulty, int] = {
    "trivial": 35,
    "easy": 15,
    "standard": 0,
    "hard": -20,
    "desperate": -35,
}

# Nothing is certain and nothing is hopeless.
MIN_CHANCE = 5
MAX_CHANCE = 95

# The top of the success range that still succeeds, but at a cost.
COST_MARGIN = 10

# With no tier for the skill, the spec says assume competent.
DEFAULT_TIER: CompetenceTier = "competent"


def chance_for(tier: CompetenceTier, difficulty: Difficulty) -> int:
    chance = BASE_CHANCE[tier] + DIFFICULTY_SHIFT[difficulty]
    return max(MIN_CHANCE, min(MAX_CHANCE, chance))


def band_for(value: int, chance: int) -> RollBand:
    critical_success = max(1, chance // 10)
    critical_failure = DIE - max(1, (DIE - chance) // 10)
    if value <= critical_success:
        return "critical_success"
    if value > critical_failure:
        return "critical_failure"
    if value <= chance - COST_MARGIN:
        return "success"
    if value <= chance:
        return "success_at_a_cost"
    return "failure"


def tier_for(character: Character | None, domain: str | None) -> CompetenceTier:
    if character is None or not domain:
        return DEFAULT_TIER
    return character.competence.tiers.get(domain, DEFAULT_TIER)


def roll_for(
    character: Character | None,
    domain: str | None,
    difficulty: Difficulty,
    rng: random.Random,
) -> Roll:
    tier = tier_for(character, domain)
    target = chance_for(tier, difficulty)
    value = rng.randint(1, DIE)
    return Roll(
        die=DIE,
        value=value,
        band=band_for(value, target),
        target=target,
        tier=tier,
        domain=domain or None,
        difficulty=difficulty,
    )


def explain(roll: Roll, character_name: str) -> str:
    """The one-line reason that travels with the outcome in the tail (§4.2)."""
    if roll.domain:
        ability = f"{character_name} is {roll.tier} at {roll.domain}"
    else:
        ability = f"{character_name} is {roll.tier or DEFAULT_TIER} in general"
    difficulty = roll.difficulty or "standard"
    target = f"; needed {roll.target} or under" if roll.target is not None else ""
    return f"{ability}; the attempt was {difficulty}{target}, and rolled {roll.value}."


BAND_SHORT: dict[RollBand, str] = {
    "critical_success": "critical success",
    "success": "success",
    "success_at_a_cost": "success at a cost",
    "failure": "failure",
    "critical_failure": "critical failure",
}


def chip_text(roll: Roll) -> str:
    """Short enough for a transcript header: '🎲 38 / 45 · success'."""
    target = f" / {roll.target}" if roll.target is not None else ""
    return f"🎲 {roll.value}{target} · {BAND_SHORT[roll.band]}"
