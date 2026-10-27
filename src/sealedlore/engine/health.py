"""A visual story's combat, health and recovery (the author, Oct 27 2026:
"a basic combat, health, injury, recovery system", forgiving, with retreat
viable unless clearly overwhelmed; serious damage a disability that becomes
permanent without first aid in a reasonable time and proper care later;
days of rest to function again and weeks to heal, so allies who tend the
injured matter; a highly proficient fighter beats several opponents with
nothing worse than cuts and bruises). Pure: no session, no Qt.

A round of a fight is one die each side (`DIE`) on top of a fighting score
(ability + weapon - penalties); the margin says what happened (`roll_fight`).
A fighter whose score beats the opponents' can never take worse than a cut
or a bruise: the serious band starts at a margin of -5, and six sides of
luck span -5 to 5, so it is reached only from a score at or below theirs.
Outnumbering costs a point per extra opponent beside the player.

An injury (models/visual.py `Injury`) is minor (bruises, a cut: healed by
a day's rest with food and drink, no penalty) or serious (a broken arm or
leg, a deep wound, a bite): first aid within FIRST_AID_HOURS (a kit, a
bandage, a splint) or it is a lasting one (a bad set, a limp); proper care
(a medic, antibiotics for an infection) and days of rest before it stops
disabling, weeks before it heals. An open wound or a bite untreated past
INFECTION_HOURS is infected and costs strength a day until antibiotics.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from sealedlore.models.visual import Injury

DIE = 6
# The margin bands of a round: the opponent down, the opponent hit, nothing,
# a minor injury, a serious one, a grave one.
DOWN_AT = 4
HIT_AT = 1
MINOR_AT = -1
SERIOUS_AT = -5
GRAVE_AT = -8
# Beside more than one opponent: a point off for each beyond the first.
OUTNUMBERED_PENALTY = 1
# Beside this many, or disabled, a retreat is overwhelmed: each gets a round
# first (the player defending at RETREAT_PENALTY).
OVERWHELMED_AT = 3
RETREAT_PENALTY = 2
RETREAT_TILES = 8
# People take this many hits before they are down (the dead take one).
PERSON_HITS = 2

HOUR = 3600
DAY = 24 * HOUR
FIRST_AID_HOURS = 6
INFECTION_HOURS = 24
# Rest a serious injury needs before it no longer disables, and before it
# is healed, by how it was looked after.
FUNCTION_DAYS = {"cared": 3, "treated": 5, "untreated": 7}
HEAL_DAYS = {"cared": 14, "treated": 21, "untreated": 28}
MINOR_HEAL_HOURS = 24
MINOR_HEAL_HOURS_UNFED = 48
# Rest counts in full when resting (sleeping, waiting), half when up and about.
ACTIVE_RATE = 0.5
# Hungry or thirsty, rest counts half.
UNFED_RATE = 0.5
# Infected, a wound a day (the hunger and thirst wounds: `AreaState.wounds`).
INFECTION_WOUND_EVERY = DAY

MINOR_KINDS = ("bruises", "cut")
SERIOUS_KINDS = ("broken arm", "broken leg", "deep wound", "bite")
LIMB_KINDS = ("broken arm", "broken leg")
OPEN_KINDS = ("deep wound", "bite")
# Weapons that take two good arms.
_TWO_HANDED = re.compile(r"\b(?:rifles?|shotguns?|carbines?|bows?|spears?|axes?)\b", re.I)
_ANTIBIOTICS = re.compile(r"\b(?:antibiotics?|penicillin|amoxicillin)\b", re.I)
_FIRST_AID = re.compile(
    r"\b(?:first.?aid|bandages?|splints?|dressings?|gauze|sutures?|medical kits?|aid kits?|"
    r"field dressings?|tourniquets?)\b",
    re.I,
)

# How the injuries of a round are told.
_MINOR_WORDS = {
    "bruises": ("bruised ribs", "a bruised shoulder", "a split lip", "a black eye"),
    "cut": ("a cut forearm", "a gashed hand", "a cut over the eye", "a torn ear"),
}
_SERIOUS_WORDS = {
    "broken arm": ("a broken arm",),
    "broken leg": ("a broken leg",),
    "deep wound": ("a deep gash in the side", "a deep wound in the thigh"),
    "bite": ("a bite on the forearm", "a bite in the shoulder"),
}


@dataclass(frozen=True)
class Outcome:
    """What a round came to: the margin, its band, and the injury the
    player took (serious ones carry a second on a grave margin)."""

    margin: int
    result: str  # down, hit, clinch, minor, serious, grave, killed
    injuries: tuple[Injury, ...]
    mine: int
    theirs: int


def roll_fight(
    rng: random.Random,
    mine: int,
    theirs: int,
    *,
    now: int,
    by_dead: bool,
    outnumbered: int = 0,
    carrying_serious: bool = False,
) -> Outcome:
    """One round: the player's score against one opponent's, a die each.
    `outnumbered`: opponents beside the player beyond this one.
    `carrying_serious`: an unhealed serious injury already, so a grave
    margin kills."""
    me = mine - OUTNUMBERED_PENALTY * max(0, outnumbered) + rng.randint(1, DIE)
    them = theirs + rng.randint(1, DIE)
    margin = me - them
    if margin >= DOWN_AT:
        return Outcome(margin, "down", (), me, them)
    if margin >= HIT_AT:
        return Outcome(margin, "hit", (), me, them)
    if margin > MINOR_AT:
        return Outcome(margin, "clinch", (), me, them)
    if margin > SERIOUS_AT:
        return Outcome(margin, "minor", (_minor(rng, now, by_dead),), me, them)
    if margin > GRAVE_AT:
        return Outcome(margin, "serious", (_serious(rng, now, by_dead),), me, them)
    if carrying_serious:
        return Outcome(margin, "killed", (_serious(rng, now, by_dead),), me, them)
    return Outcome(
        margin, "grave", (_serious(rng, now, by_dead), _minor(rng, now, by_dead)), me, them
    )


def _minor(rng: random.Random, now: int, by_dead: bool) -> Injury:
    kind = "cut" if by_dead or rng.random() < 0.5 else "bruises"
    return Injury(kind=kind, serious=False, at=now, words=rng.choice(_MINOR_WORDS[kind]))


def _serious(rng: random.Random, now: int, by_dead: bool) -> Injury:
    if by_dead:
        kind = "bite" if rng.random() < 0.7 else "deep wound"
    else:
        kind = rng.choice(SERIOUS_KINDS[:3])
    return Injury(kind=kind, serious=True, at=now, words=rng.choice(_SERIOUS_WORDS[kind]))


def took_words(outcome: Outcome, who: str, with_what: str) -> str:
    """The round's line for the player, from their side."""
    if outcome.result == "down":
        return f"You put {who} down{with_what} ({outcome.mine} against {outcome.theirs})."
    if outcome.result == "hit":
        return f"You hit {who}{with_what} ({outcome.mine} against {outcome.theirs})."
    if outcome.result == "clinch":
        return f"You and {who} grapple to nothing ({outcome.mine} against {outcome.theirs})."
    hurt = " and ".join(i.words for i in outcome.injuries)
    capital = who[0].upper() + who[1:]
    if outcome.result == "minor":
        return (
            f"{capital} gets through your guard: {hurt} ({outcome.mine} against {outcome.theirs})."
        )
    if outcome.result == "killed":
        return f"{capital} finishes you ({outcome.mine} against {outcome.theirs})."
    return f"{capital} gets the better of you: {hurt} ({outcome.mine} against {outcome.theirs})."


# --- what the injuries do --------------------------------------------------------


def open_injuries(injuries: list[Injury]) -> list[Injury]:
    return [i for i in injuries if not i.healed]


def care_level(injury: Injury) -> str:
    if injury.cared is not None:
        return "cared"
    if injury.treated is not None:
        return "treated"
    return "untreated"


def disabling(injury: Injury) -> bool:
    """A serious injury that hasn't had its days of rest yet."""
    if not injury.serious or injury.healed:
        return False
    return injury.rested < FUNCTION_DAYS[care_level(injury)] * DAY


def disabled(injuries: list[Injury]) -> bool:
    return any(disabling(i) for i in injuries)


def fight_penalty(injuries: list[Injury]) -> int:
    """What the injuries take off the fighting score: a disabling break 2,
    a recovering one 1, an open wound or bite 1 (2 infected), a lasting
    injury 1."""
    penalty = 0
    for injury in injuries:
        if injury.healed:
            penalty += 1 if injury.permanent else 0
            continue
        if not injury.serious:
            continue
        if injury.kind in LIMB_KINDS:
            penalty += 2 if disabling(injury) else 1
        else:
            penalty += 2 if injury.infected else 1
    return penalty


def weapon_bonus_allowed(injuries: list[Injury], weapon: str, bonus: int) -> int:
    """A two-handed weapon is half the use with a broken arm."""
    if bonus and weapon and _TWO_HANDED.search(weapon):
        if any(i.kind == "broken arm" and not i.healed for i in injuries):
            return bonus // 2
    return bonus


def walk_factor(injuries: list[Injury]) -> float:
    """How fast the player moves against a fit one: a broken leg 0.5 while
    it disables (0.4 untreated), 0.75 after, a lasting limp 0.85."""
    factor = 1.0
    for injury in injuries:
        if injury.kind != "broken leg":
            continue
        if injury.healed:
            factor = min(factor, 0.85 if injury.permanent else 1.0)
        elif disabling(injury):
            factor = min(factor, 0.4 if injury.treated is None else 0.5)
        else:
            factor = min(factor, 0.75)
    return factor


def condition(injuries: list[Injury]) -> str:
    """One word for the bar: fit, bruised, hurt, disabled, infected."""
    live = open_injuries(injuries)
    if not live:
        return "limping" if any(i.permanent and i.kind == "broken leg" for i in injuries) else "fit"
    if any(i.infected for i in live):
        return "infected"
    if disabled(live):
        return "disabled"
    if any(i.serious for i in live):
        return "hurt"
    return "bruised"


def injury_words(injury: Injury, now: int) -> str:
    """An injury as the bar's tooltip and the keeper read it."""
    if injury.healed:
        return f"{injury.words} (healed{', badly' if injury.permanent else ''})"
    parts = [injury.words]
    if injury.serious:
        if injury.infected:
            parts.append("infected")
        if injury.cared is not None:
            parts.append("under care")
        elif injury.treated is not None:
            parts.append("given first aid")
        else:
            hours = (now - injury.at) // HOUR
            parts.append("untreated" + (f" for {hours} h" if hours >= 1 else ""))
        if injury.permanent:
            parts.append("will never set right")
        days = injury.rested / DAY
        need = FUNCTION_DAYS[care_level(injury)]
        if disabling(injury):
            parts.append(f"{days:.1f} of {need} days' rest before it bears weight")
        else:
            parts.append("mending")
    return ", ".join(parts)


# --- time and treatment ------------------------------------------------------------


def advance(
    injuries: list[Injury], seconds: int, *, now: int, resting: bool, fed: bool
) -> tuple[list[str], int]:
    """Time passes: rest accrues (full resting, half up and about; half
    again unfed), minor injuries heal, serious ones heal in their weeks, an
    untreated open wound goes septic, a break untreated past its hours is
    a lasting one. (the lines, the wounds the infections cost)."""
    lines: list[str] = []
    wounds = 0
    rate = (1.0 if resting else ACTIVE_RATE) * (1.0 if fed else UNFED_RATE)
    for injury in injuries:
        if injury.healed:
            continue
        end = now + seconds
        if not injury.serious:
            limit = (MINOR_HEAL_HOURS if fed else MINOR_HEAL_HOURS_UNFED) * HOUR
            if end - injury.at >= limit:
                injury.healed = True
                lines.append(f"The {injury.words.removeprefix('a ')} has healed.")
            continue
        if injury.treated is None and injury.kind in LIMB_KINDS and not injury.permanent:
            if end - injury.at >= FIRST_AID_HOURS * HOUR:
                injury.permanent = True
                lines.append(f"{_cap(injury.words)}, never set: it will not mend straight.")
        festering = seconds
        if injury.kind in OPEN_KINDS and not injury.infected and injury.treated is None:
            turned = injury.at + INFECTION_HOURS * HOUR
            if end >= turned:
                injury.infected = True
                festering = end - max(turned, now)
                lines.append(f"{_cap(injury.words)} has gone bad: it is infected.")
        if injury.infected:
            injury.festered += festering
            while injury.festered >= INFECTION_WOUND_EVERY:
                injury.festered -= INFECTION_WOUND_EVERY
                wounds += 1
                lines.append("The infection is wearing you down.")
            continue  # no mending while it festers
        was_disabling = disabling(injury)
        injury.rested += int(seconds * rate)
        if was_disabling and not disabling(injury):
            lines.append(f"{_cap(injury.words)} will bear weight again, carefully.")
        if injury.rested >= HEAL_DAYS[care_level(injury)] * DAY:
            injury.healed = True
            lines.append(
                f"{_cap(injury.words)} has healed"
                + (", badly: it will always trouble you." if injury.permanent else ".")
            )
    return lines, wounds


def first_aid(injury: Injury, now: int) -> str:
    """First aid: a kit, bandage or splint on a serious injury."""
    if injury.healed or not injury.serious:
        return ""
    if injury.treated is not None:
        return f"{_cap(injury.words)} is already dressed."
    injury.treated = now
    late = now - injury.at >= FIRST_AID_HOURS * HOUR
    if injury.kind in LIMB_KINDS:
        words = f"You splint {injury.words}."
        if late:
            injury.permanent = True
            words += " Too late to set it straight: it will never be right."
        return words
    if injury.infected:
        return f"You dress {injury.words}; the infection needs more than a bandage."
    return f"You dress {injury.words}."


def care(injury: Injury, now: int, *, by: str = "", antibiotics: bool = False) -> str:
    """Proper care: a medic's attention sets a break and cleans a wound
    (and gives first aid where none was); antibiotics clear an infection."""
    if injury.healed or not injury.serious:
        return ""
    who = by or "You"
    if antibiotics:
        if not injury.infected:
            return ""
        injury.infected = False
        injury.festered = 0
        if injury.treated is None:
            injury.treated = now  # seen to: it won't go bad again untended
        verb = "treat" if who == "You" else "treats"
        return f"{who} {verb} the infection in {injury.words} with antibiotics."
    if injury.cared is not None:
        return ""
    if injury.treated is None:
        injury.treated = now
        if injury.kind in LIMB_KINDS and now - injury.at >= FIRST_AID_HOURS * HOUR:
            injury.permanent = True
    injury.cared = now
    verb = "set" if injury.kind in LIMB_KINDS else "clean and stitch"
    verb = verb if who == "You" else (verb + "s" if verb == "set" else "cleans and stitches")
    return f"{who} {verb} {injury.words}." + (
        " It was left too long to set straight."
        if injury.permanent and injury.kind in LIMB_KINDS
        else ""
    )


def is_first_aid(name: str) -> bool:
    return bool(_FIRST_AID.search(name))


def is_antibiotics(name: str) -> bool:
    return bool(_ANTIBIOTICS.search(name))


def worst_untreated(injuries: list[Injury]) -> Injury | None:
    """The serious injury most in need of first aid: untreated, the oldest."""
    open_ = [i for i in injuries if i.serious and not i.healed and i.treated is None]
    return min(open_, key=lambda i: i.at) if open_ else None


def _cap(words: str) -> str:
    return words[0].upper() + words[1:] if words else words
