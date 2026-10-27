"""Combat, injuries, recovery, standing and scouting of a visual story
(engine/health.py, engine/session_health.py, engine/session_scout.py; the
author's rulings of Oct 27 2026). The pure rules here; the session's side
in tests/test_visual.py."""

from __future__ import annotations

import random
from collections import Counter

from sealedlore.engine.health import (
    DAY,
    FIRST_AID_HOURS,
    HOUR,
    advance,
    care,
    condition,
    disabled,
    fight_penalty,
    first_aid,
    roll_fight,
    walk_factor,
    weapon_bonus_allowed,
)
from sealedlore.models.visual import Injury


def _odds(mine: int, theirs: int, outnumbered: int = 0, n: int = 4000) -> dict[str, float]:
    rng = random.Random(7)
    counts = Counter(
        roll_fight(rng, mine, theirs, now=0, by_dead=True, outnumbered=outnumbered).result
        for _ in range(n)
    )
    return {k: v / n for k, v in counts.items()}


def test_a_proficient_fighter_takes_nothing_worse_than_cuts_from_several_opponents():
    """The author: highly proficient, several opponents, nothing worse than
    cuts and bruises. John with a rifle (11) against three of the dead (4):
    outnumbered by two, never a serious injury; against three armed bandits
    (8) likewise, with cuts now and then."""
    assert set(_odds(11, 4, outnumbered=2)) <= {"down", "hit", "clinch", "minor"}
    bandits = _odds(11, 8, outnumbered=2)
    assert "serious" not in bandits and "grave" not in bandits
    assert bandits["down"] + bandits["hit"] > 0.5
    # Bare-handed and outmatched, serious hurts come, and a grave one now and then.
    weak = _odds(5, 8, outnumbered=1)
    assert weak.get("serious", 0) > 0.1 and weak.get("grave", 0) > 0


def test_a_grave_round_kills_only_someone_already_carrying_a_serious_injury():
    rng = random.Random(3)
    results = {
        roll_fight(rng, 1, 20, now=0, by_dead=False, carrying_serious=carrying).result
        for carrying in (False, True)
        for _ in range(20)
    }
    assert results == {"grave", "killed"}
    fresh = roll_fight(random.Random(1), 1, 20, now=0, by_dead=False, carrying_serious=False)
    assert fresh.result == "grave" and len(fresh.injuries) == 2
    assert fresh.injuries[0].serious and not fresh.injuries[1].serious


def test_injuries_penalise_fighting_and_walking_and_a_broken_arm_halves_a_rifle():
    leg = Injury(kind="broken leg", serious=True, at=0, words="a broken leg")
    arm = Injury(kind="broken arm", serious=True, at=0, words="a broken arm")
    cut = Injury(kind="cut", serious=False, at=0, words="a cut forearm")
    assert fight_penalty([cut]) == 0
    assert fight_penalty([leg]) == 2 and disabled([leg])
    assert walk_factor([leg]) == 0.4  # untreated and disabling
    leg.treated = 0
    assert walk_factor([leg]) == 0.5
    assert weapon_bonus_allowed([arm], "a hunting rifle", 4) == 2
    assert weapon_bonus_allowed([arm], "a knife", 1) == 1
    assert weapon_bonus_allowed([leg], "a hunting rifle", 4) == 4
    assert condition([cut]) == "bruised" and condition([leg]) == "disabled"


def test_first_aid_in_time_sets_a_break_and_late_leaves_it_lasting():
    early = Injury(kind="broken arm", serious=True, at=0, words="a broken arm")
    assert first_aid(early, 2 * HOUR) == "You splint a broken arm."
    assert early.treated == 2 * HOUR and not early.permanent
    late = Injury(kind="broken arm", serious=True, at=0, words="a broken arm")
    lines, _w = advance([late], (FIRST_AID_HOURS + 1) * HOUR, now=0, resting=False, fed=True)
    assert late.permanent and any("will not mend straight" in ln for ln in lines)
    assert "Too late" in first_aid(late, (FIRST_AID_HOURS + 1) * HOUR)


def test_rest_mends_a_treated_break_in_days_and_heals_it_in_weeks_cared_for():
    leg = Injury(kind="broken leg", serious=True, at=0, words="a broken leg")
    first_aid(leg, HOUR)
    assert care(leg, 2 * HOUR, by="Elena Ruiz") == "Elena Ruiz sets a broken leg."
    # Up and about, rest counts half: five days on the move for three days' rest.
    lines, _w = advance([leg], 3 * DAY, now=0, resting=False, fed=True)
    assert disabled([leg]) and not lines
    lines, _w = advance([leg], 3 * DAY, now=3 * DAY, resting=True, fed=True)
    assert not disabled([leg]) and any("bear weight" in ln for ln in lines)
    assert walk_factor([leg]) == 0.75
    lines, _w = advance([leg], 14 * DAY, now=6 * DAY, resting=True, fed=True)
    assert leg.healed and not leg.permanent and walk_factor([leg]) == 1.0
    assert condition([leg]) == "fit"


def test_an_untreated_bite_goes_bad_and_antibiotics_clear_it():
    bite = Injury(kind="bite", serious=True, at=0, words="a bite on the forearm")
    lines, wounds = advance([bite], 26 * HOUR, now=0, resting=True, fed=True)
    assert bite.infected and wounds == 0 and any("infected" in ln for ln in lines)
    lines, wounds = advance([bite], 2 * DAY, now=26 * HOUR, resting=True, fed=True)
    assert wounds == 2 and bite.rested == 0  # no mending while it festers
    assert condition([bite]) == "infected"
    assert "antibiotics" in care(bite, 3 * DAY, antibiotics=True)
    assert not bite.infected
    lines, wounds = advance([bite], DAY, now=3 * DAY, resting=True, fed=True)
    assert wounds == 0 and bite.rested == DAY


def test_minor_injuries_heal_in_a_day_fed_and_two_unfed():
    cut = Injury(kind="cut", serious=False, at=0, words="a cut forearm")
    advance([cut], 20 * HOUR, now=0, resting=False, fed=True)
    assert not cut.healed
    advance([cut], 5 * HOUR, now=20 * HOUR, resting=False, fed=True)
    assert cut.healed
    bruise = Injury(kind="bruises", serious=False, at=0, words="bruised ribs")
    advance([bruise], 30 * HOUR, now=0, resting=False, fed=False)
    assert not bruise.healed
    advance([bruise], 20 * HOUR, now=30 * HOUR, resting=False, fed=False)
    assert bruise.healed
