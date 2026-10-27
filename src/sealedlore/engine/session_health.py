"""The player's fights, injuries, recovery and standing with the people
(engine/health.py; the author, Oct 27 2026). A mixin for `StorySession`
beside the visual runtime (engine/session_visual.py), which calls these
from its actions.

- A fight is rounds (`_round`): the player's score (ability, the weapon as
  far as their arms allow, less the injuries' and the needs' penalties)
  against one opponent's, outnumbering counted; what the round came to is
  applied here (the opponent hit or down, the player's injuries, death).
- Retreat (`visual_retreat`): out of reach of whoever is beside the player,
  freely unless clearly overwhelmed (three or more beside them, or
  disabled), when each gets a round first.
- Healing (`advance_healing`): time passing, resting or not, fed or not.
- Treatment: a carried kit, bandage or splint (`visual_use` sends medicine
  here), antibiotics for an infection, and a person tending the player
  (`visual_treat_by`: a medic's care, or first aid from a kit they carry),
  asked for by their own "treat" intent or given to a sleeping companion.
- Standing (`standing_of`, `set_standing`): "guest", "friend", "ally"...
  per person, or for a group of them (models/visual.py STANDINGS).
"""

from __future__ import annotations

import random

from sealedlore.engine.area import Area, bare, in_earshot, path, sees, stand_near, walk_metres
from sealedlore.engine.health import (
    OVERWHELMED_AT,
    PERSON_HITS,
    RETREAT_PENALTY,
    RETREAT_TILES,
    Outcome,
    advance,
    care,
    condition,
    disabled,
    fight_penalty,
    first_aid,
    injury_words,
    is_antibiotics,
    is_first_aid,
    roll_fight,
    took_words,
    walk_factor,
    weapon_bonus_allowed,
    worst_untreated,
)
from sealedlore.engine.visual_rules import best_weapon, kind_of
from sealedlore.models.area import Spot
from sealedlore.models.visual import SAFE_STANDINGS, STANDINGS, NpcGoal


class HealthRuntime:
    # --- scores and rounds --------------------------------------------------------

    def fight_score(self, state=None) -> tuple[int, tuple[str, int]]:
        """The player's fighting score with everything counted, and the
        weapon it counts (its bonus as their arms allow)."""
        state = state if state is not None else self.area_state()
        design = self.visual_design()
        held = self.held_name() or ""
        base = design.abilities.get(held, design.default_ability)
        name, bonus = best_weapon(r for _i, r in self.visual_inventory())
        bonus = weapon_bonus_allowed(state.injuries, name, bonus)
        score = base + bonus - fight_penalty(state.injuries) - self.needs_penalty()
        return max(1, score), (name, bonus)

    def person_score(self, state, name: str) -> int:
        """A person's fighting score: their ability and the best weapon
        they carry."""
        sheet = self.visual_sheet(name)
        carried = [r for r in state.things.values() if r.state == "taken" and r.by == name]
        return sheet.ability + best_weapon(carried)[1]

    def _beside(self, state, area: Area, me: Spot) -> list[tuple[str, bool]]:
        """Who is beside the player and after them: (name or creature id,
        one of the dead)."""
        out: list[tuple[str, bool]] = []
        for creature in state.creatures.values():
            if creature.area == area.id and creature.state != "down":
                if max(abs(creature.x - me.x), abs(creature.y - me.y)) <= 1:
                    out.append((creature.id, True))
        for name, goal in state.goals.items():
            spot = state.spots.get(name)
            if goal.intent != "attack" or spot is None or spot.area != area.id:
                continue
            if name not in state.people_down and max(abs(spot.x - me.x), abs(spot.y - me.y)) <= 1:
                out.append((name, False))
        return out

    def _round(
        self,
        state,
        area: Area,
        me: Spot,
        opponent: str,
        theirs: int,
        *,
        by_dead: bool,
        rng: random.Random,
        penalty: int = 0,
    ) -> tuple[Outcome, list[str]]:
        """One round against one opponent, applied to the player: the
        injuries taken, death on a finishing blow. The opponent's side is
        the caller's (a hit, down)."""
        mine, weapon = self.fight_score(state)
        beside = self._beside(state, area, me)
        outcome = roll_fight(
            rng,
            mine - penalty,
            theirs,
            now=state.seconds,
            by_dead=by_dead,
            outnumbered=max(0, len(beside) - 1),
            carrying_serious=any(i.serious and not i.healed for i in state.injuries),
        )
        with_what = f" with the {bare(weapon[0])}" if weapon[0] else " bare-handed"
        lines = [took_words(outcome, opponent, with_what)]
        state.injuries.extend(outcome.injuries)
        if outcome.result == "killed":
            state.dead = True
            lines.append("You are dead.")
        return outcome, lines

    def _creature_round(self, state, area: Area, me: Spot, creature, rng, *, penalty: int = 0):
        """A round against one of the dead: it goes down, takes a hit, or
        gets at the player."""
        outcome, lines = self._round(
            state, area, me, creature.name, creature.ability, by_dead=True, rng=rng, penalty=penalty
        )
        if outcome.result == "down":
            creature.hits = 0
            creature.state = "down"
        elif outcome.result == "hit":
            creature.hits -= 1
            if creature.hits <= 0:
                creature.state = "down"
            else:
                lines[-1] = f"You hit {creature.name}; it keeps coming."
        return outcome, lines

    def _person_round(self, state, area: Area, me: Spot, name: str, rng, *, penalty: int = 0):
        """A round against a person: down on a telling blow or a second
        hit (`PERSON_HITS`)."""
        outcome, lines = self._round(
            state,
            area,
            me,
            name,
            self.person_score(state, name),
            by_dead=False,
            rng=rng,
            penalty=penalty,
        )
        if outcome.result in ("down", "hit"):
            state.person_hits[name] = state.person_hits.get(name, 0) + (
                PERSON_HITS if outcome.result == "down" else 1
            )
            if state.person_hits[name] >= PERSON_HITS:
                state.people_down.append(name)
                state.goals.pop(name, None)
                if name in state.companions:
                    state.companions.remove(name)
                lines[-1] = f"You put {name} down ({outcome.mine} against {outcome.theirs})."
            else:
                lines[-1] = (
                    f"You hit {name}; they keep coming ({outcome.mine} against {outcome.theirs})."
                )
        return outcome, lines

    def visual_retreat(self) -> list[str]:
        """Break away from whoever is beside the player: out of their reach
        at a run, freely unless clearly overwhelmed (OVERWHELMED_AT beside
        them, or disabled), when each gets a round at the player's back
        first. The dead shamble after at their pace; a person after them
        follows by their own intent."""
        state = self.area_state()
        area, me = self._visual_me(state)
        if area is None or me is None:
            return []
        beside = self._beside(state, area, me)
        if not beside:
            return ["Nothing is on you."]
        lines: list[str] = []
        rng = random.Random(f"{state.seconds}:retreat")
        if len(beside) >= OVERWHELMED_AT or disabled(state.injuries):
            lines.append("You are hard pressed: they get at you as you break away.")
            for who, dead in beside:
                if state.dead:
                    break
                if dead:
                    creature = state.creatures[who]
                    _o, more = self._creature_round(
                        state, area, me, creature, rng, penalty=RETREAT_PENALTY
                    )
                else:
                    _o, more = self._person_round(
                        state, area, me, who, rng, penalty=RETREAT_PENALTY
                    )
                lines += more
            if state.dead:
                self._set_live(state)
                return lines
        threats = [
            (state.creatures[w].x, state.creatures[w].y)
            if dead
            else (state.spots[w].x, state.spots[w].y)
            for w, dead in beside
        ]
        cx = sum(t[0] for t in threats) / len(threats)
        cy = sum(t[1] for t in threats) / len(threats)
        blocked = area.blocked() | {
            (s.x, s.y)
            for n, s in state.spots.items()
            if s.area == area.id and n != self.held_name()
        }
        blocked |= {
            (c.x, c.y) for c in state.creatures.values() if c.area == area.id and c.state != "down"
        }
        best: tuple[float, tuple[int, int]] | None = None
        for y in range(max(0, me.y - RETREAT_TILES), min(area.height, me.y + RETREAT_TILES + 1)):
            for x in range(max(0, me.x - RETREAT_TILES), min(area.width, me.x + RETREAT_TILES + 1)):
                if (x, y) == (me.x, me.y) or not area.walkable(x, y, blocked):
                    continue
                away = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
                if best is None or away > best[0]:
                    best = (away, (x, y))
        if best is None:
            self._set_live(state)
            return lines + ["There is nowhere to go."]
        route = path(area, (me.x, me.y), best[1], blocked=blocked)
        if len(route) < 2:
            self._set_live(state)
            return lines + ["There is nowhere to go."]
        self._set_live(state)
        self.walked(route[: RETREAT_TILES + 1])
        lines.append("You break away.")
        return lines + self.visual_tick(0)

    # --- time, treatment ---------------------------------------------------------

    def advance_healing(self, state, seconds: int, *, resting: bool) -> list[str]:
        """Time passing on the injuries (`health.advance`), the clock
        already moved by `seconds`."""
        if seconds <= 0 or not state.injuries:
            return []
        short = set(self.visual_needs(state))
        fed = not (short & {"hungry", "starving", "thirsty", "parched"})
        lines, wounds = advance(
            state.injuries, seconds, now=max(0, state.seconds - seconds), resting=resting, fed=fed
        )
        state.wounds += wounds
        return lines

    def visual_walk_factor(self, state=None) -> float:
        state = state if state is not None else self.area_state()
        return walk_factor(state.injuries)

    def visual_condition(self, state=None) -> tuple[str, list[str]]:
        """(one word for the bar, each injury in words)."""
        state = state if state is not None else self.area_state()
        return condition(state.injuries), [
            injury_words(i, state.seconds) for i in state.injuries if not i.healed
        ]

    def visual_use_medicine(self, state, record) -> str:
        """A carried medicine on the player's own injuries: antibiotics for
        an infection, anything else as first aid on the worst untreated
        serious injury. The record is used up by one; "" leaves the use to
        the keeper."""
        name = bare(record.name)
        if is_antibiotics(record.name):
            infected = [i for i in state.injuries if i.infected and not i.healed]
            if not infected:
                return "You aren't infected."
            words = care(infected[0], state.seconds, antibiotics=True)
        else:
            injury = worst_untreated(state.injuries)
            if injury is None:
                return (
                    "Nothing needs dressing."
                    if not state.injuries
                    else "Your injuries are dressed already."
                )
            words = first_aid(injury, state.seconds)
        record.count -= 1
        if record.count <= 0:
            record.state = "gone"
        return words + (f" (the {name} is used up)" if record.state == "gone" else "")

    def visual_treat_by(self, state, name: str) -> list[str]:
        """A person tends the player: a medic's care on each serious injury
        not yet cared for (and antibiotics they carry on an infection),
        else first aid from a kit they carry on the worst untreated one.
        Lines, or none when they have nothing to treat with or nothing to do."""
        sheet = self.visual_sheet(name)
        carried = [r for r in state.things.values() if r.state == "taken" and r.by == name]
        lines: list[str] = []
        if sheet.medic:
            for injury in state.injuries:
                if injury.infected and not injury.healed:
                    if any(is_antibiotics(r.name) for r in carried):
                        words = care(injury, state.seconds, by=name, antibiotics=True)
                        if words:
                            lines.append(words)
                words = care(injury, state.seconds, by=name)
                if words:
                    lines.append(words)
            return lines
        kit = next(
            (
                r
                for r in carried
                if is_first_aid(r.name) or (r.kind or kind_of(r.name)) == "medicine"
            ),
            None,
        )
        injury = worst_untreated(state.injuries)
        if kit is None or injury is None:
            return []
        words = first_aid(injury, state.seconds)
        if words:
            lines.append(f"{name}: " + words.replace("You ", f"{name} ").replace("You", name))
        return lines

    def nothing_more_to_do(self, state, name: str) -> bool:
        """Whether this person could do nothing more for the player's
        injuries: none serious and open, or every one already seen to as
        far as they can (first aid for anyone, proper care from a medic)."""
        open_ = [i for i in state.injuries if i.serious and not i.healed]
        if not open_:
            return True
        if self.visual_sheet(name).medic:
            return all(i.cared is not None and not i.infected for i in open_)
        return all(i.treated is not None for i in open_)

    def companions_tend(self, state, area: Area) -> list[str]:
        """Whoever is with the player (a companion, a friend here) sees to
        their injuries while they rest."""
        lines: list[str] = []
        if not any(i.serious and not i.healed for i in state.injuries):
            return lines
        for name, spot in state.spots.items():
            if name == self.held_name() or spot.area != area.id or name in state.people_down:
                continue
            if name in state.companions or self.standing_of(name, state) in SAFE_STANDINGS:
                lines += self.visual_treat_by(state, name)
        return lines

    # --- standing ------------------------------------------------------------------

    def group_of(self, name: str) -> str:
        sheet = self.visual_sheet(name)
        if sheet.group:
            return sheet.group
        for group, members in self.visual_design().groups.items():
            if name in members:
                return group
        return ""

    def standing_of(self, name: str, state=None) -> str:
        """How this person stands with the player: their own standing, else
        their group's, else "" (a stranger)."""
        state = state if state is not None else self.area_state()
        own = state.standing.get(name, "")
        if own:
            return own
        group = self.group_of(name)
        return state.group_standing.get(group, "") if group else ""

    def set_standing(self, state, who: str, status: str) -> str:
        """A standing for a person or a whole group, by name; the line, or
        "" for nobody of that name or no such standing."""
        status = status.strip().lower()
        if status not in STANDINGS:
            return ""
        want = who.strip().casefold()
        for group in self.visual_design().groups:
            if group.casefold() == want:
                state.group_standing[group] = status
                return f"{group}: {status}."
        card = self.visual_card(who)
        if card is None:
            return ""
        state.standing[card.name] = status
        return f"{card.name}: {status}."

    def safe_here(self, state, area: Area) -> bool:
        """Whether someone here stands with the player well enough that
        they sleep in peace (a guest, a friend)."""
        for name, spot in state.spots.items():
            if name == self.held_name() or spot.area != area.id or name in state.people_down:
                continue
            if self.standing_of(name, state) in SAFE_STANDINGS:
                return True
        return False

    # --- a person's moves on the player's behalf ------------------------------------

    def _approach_step(self, state, area: Area, me: Spot, name: str, tiles: int) -> None:
        """A person walking up to the player (the "approach" intent): as a
        follower does, and stays once beside them."""
        spot = state.spots.get(name)
        if spot is None:
            return
        here = (spot.x, spot.y)
        if max(abs(here[0] - me.x), abs(here[1] - me.y)) <= 1:
            state.goals[name] = NpcGoal(intent="stay")
            return
        taken = {(s.x, s.y) for n, s in state.spots.items() if s.area == area.id and n != name}
        stand = stand_near(area, (me.x, me.y), taken | {(me.x, me.y)})
        route = path(area, here, stand, blocked=taken)
        if len(route) > 1:
            step = route[min(len(route) - 1, max(1, tiles))]
            state.spots[name] = Spot(area=area.id, x=step[0], y=step[1])
            if max(abs(step[0] - me.x), abs(step[1] - me.y)) <= 1:
                state.goals[name] = NpcGoal(intent="stay")

    def within_reach_of(self, state, area: Area, me: Spot, name: str) -> bool:
        spot = state.spots.get(name)
        return (
            spot is not None
            and spot.area == area.id
            and (
                max(abs(spot.x - me.x), abs(spot.y - me.y)) <= 2
                or in_earshot(area, (me.x, me.y), (spot.x, spot.y))
                and sees(area, (me.x, me.y), (spot.x, spot.y))
                and walk_metres(area, [(me.x, me.y), (spot.x, spot.y)]) < 6
            )
        )
