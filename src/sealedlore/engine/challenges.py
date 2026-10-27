"""Challenges (the author, Oct 27 2026: "a general trigger for approaching
anyone considered hostile in any way, which would include a neutral party
that demands a toll … if the person for example doesn't pay or doesn't
respond to the communication, they either get a second warning, or
attacked, depending on their standing and how dangerous they appear
compared to the enemy").

Pure: who counts as a challenger, how far off they challenge, and what
they do when ignored. The session's side is engine/session_challenge.py.
"""

from __future__ import annotations

from sealedlore.models.visual import SAFE_STANDINGS, PersonSheet

# A challenger hails the player this close (in sight), not on first sight
# at 60 m: the encounter is the challenge.
CHALLENGE_M = 30.0
# A demand is pressed while the player stays this close in sight or
# earshot; further off it lapses.
ENFORCE_M = 45.0
# Someone of the challenger's group this close stands with them.
ALLY_M = 15.0
# The player looks the stronger by this much: a neutral warns twice, then
# lets them be; by ROUTED_BY even a hostile backs off.
STRONGER_BY = 2
ROUTED_BY = 4

STEP_WARN = "warn"
STEP_ATTACK = "attack"
STEP_STAND_DOWN = "stand_down"


def is_challenger(sheet: PersonSheet, standing: str) -> bool:
    """Whether this person challenges anyone who comes near: their sheet
    says what they demand, or they are hostile to the player."""
    return bool(sheet.challenges.strip()) or standing == "hostile"


def escalate(ignored: int, standing: str, danger: int) -> str:
    """What a challenger does the `ignored`th time the player fails to
    answer their demand (1: the first time), by how the player stands with
    them and `danger`, the player's side less theirs (positive: the player
    looks the more dangerous)."""
    if standing in SAFE_STANDINGS:
        return STEP_WARN if ignored <= 1 else STEP_STAND_DOWN
    hostile = standing == "hostile"
    if ignored <= 1:
        return STEP_ATTACK if hostile and danger < STRONGER_BY else STEP_WARN
    if danger >= ROUTED_BY or (danger >= STRONGER_BY and not hostile):
        return STEP_STAND_DOWN
    return STEP_ATTACK


def danger_words(danger: int) -> str:
    """How the odds look to the challenger, for their call."""
    if danger >= ROUTED_BY:
        return "the player looks far more dangerous than you and yours"
    if danger >= STRONGER_BY:
        return "the player looks the more dangerous"
    if danger <= -STRONGER_BY:
        return "you and yours have the player outmatched"
    return "the two sides look evenly matched"
