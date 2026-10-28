"""Challenges and demands in a visual story (engine/challenges.py for the
rules; the author's ruling, Oct 27 2026). A mixin for `StorySession`
beside the health runtime.

A challenger (someone whose sheet says what they demand of anyone who
comes near, or who is hostile to the player) gets their one encounter
call at CHALLENGE_M with a challenge cue, and what their reply demands is
kept as a `Demand`. Each of the player's actions that doesn't answer them
(a word, a thing handed over) counts as ignoring it; the program decides
the step (a second warning, an attack, standing down) by the player's
standing and the odds, and the person voices it through a talk call with
the step's cue; a reply that fails is carried out in plain words.
"""

from __future__ import annotations

import math

from sealedlore.engine.area import Area, in_earshot, sees, stand_near
from sealedlore.engine.challenges import (
    ALLY_M,
    CHALLENGE_M,
    ENFORCE_M,
    STEP_ATTACK,
    STEP_STAND_DOWN,
    STEP_WARN,
    danger_words,
    escalate,
    is_challenger,
)
from sealedlore.engine.npc import NpcReply
from sealedlore.models.area import Spot
from sealedlore.models.visual import Demand, NpcGoal

STEP_CUES = {
    STEP_WARN: "npc.ignored",
    STEP_ATTACK: "npc.attacks",
    STEP_STAND_DOWN: "npc.stands_down",
}


class ChallengeRuntime:
    def is_challenger(self, name: str, state=None) -> bool:
        state = state if state is not None else self.area_state()
        return is_challenger(self.visual_sheet(name), self.standing_of(name, state))

    def challenge_due(self, name: str, state, area: Area, me: Spot) -> bool:
        """Whether a challenger is close enough to hail the player: within
        CHALLENGE_M and in sight."""
        spot = state.spots.get(name)
        if spot is None or spot.area != area.id:
            return False
        metres = math.hypot(spot.x - me.x, spot.y - me.y) * area.tile_m
        return metres <= CHALLENGE_M and sees(area, (me.x, me.y), (spot.x, spot.y))

    def challenge_cue(self, name: str, state) -> str:
        """The encounter line for a challenger: their demand of anyone who
        comes near, or, hostile with none, a hostile's."""
        sheet = self.visual_sheet(name)
        if sheet.challenges.strip():
            return self.texts.fill("npc.challenge", challenges=sheet.challenges.strip())
        return self.texts["npc.hostile"]

    def demand_cue(self, name: str, state) -> str:
        """The cue for the step pending on this person's demand."""
        demand = state.demands.get(name)
        if demand is None or not demand.pending:
            return ""
        area, me = self._visual_me(state)
        odds = danger_words(self.danger(state, area, name)) if area is not None else ""
        return self.texts.fill(
            STEP_CUES[demand.pending],
            demand=demand.what,
            odds=odds,
            player=self.held_name() or "the player",
        )

    def address_demand(self, state, name: str) -> None:
        """The player answers a challenger (a word, a thing handed over)."""
        demand = state.demands.get(name)
        if demand is not None:
            demand.addressed += 1

    def visual_answering(self, name: str) -> None:
        """The player turns to a challenger (Talk to them, walking up to
        speak): answering, whatever the walk there looks like."""
        state = self.area_state()
        if name in state.demands:
            self.address_demand(state, name)
            self._set_live(state)

    def _metres_between(self, area: Area, me: Spot, spot: Spot) -> float:
        return math.hypot(spot.x - me.x, spot.y - me.y) * area.tile_m

    def note_demand(self, state, name: str, reply: NpcReply) -> list[str]:
        """What a reply does to the person's demand: one made or restated
        stands (`demand` in the reply); "" is satisfied or given up; an
        attack ends it. Lines for the log."""
        demand = state.demands.get(name)
        if reply.intent == "attack":
            state.demands.pop(name, None)
            return []
        if reply.demand is None:
            return []
        what = reply.demand.strip()
        if what:
            if demand is None:
                area, me = self._visual_me(state)
                spot = state.spots.get(name)
                metres = (
                    self._metres_between(area, me, spot)
                    if area is not None and me is not None and spot is not None
                    else -1.0
                )
                state.demands[name] = Demand(
                    what=what,
                    at=state.seconds,
                    last_key=self._action_key(state, me),
                    last_metres=metres,
                )
            else:
                demand.what = what
            return []
        if demand is not None:
            state.demands.pop(name, None)
            return [f"({name} wants nothing more of you.)"]
        return []

    def _action_key(self, state, me: Spot | None) -> list[int]:
        return [state.seconds, me.x if me else -1, me.y if me else -1]

    def danger(self, state, area: Area, name: str) -> int:
        """The player's side less the challenger's: fighting scores, the
        player's companions here, the challenger's group within ALLY_M."""
        held = self.held_name() or ""
        mine, _weapon = self.fight_score(state)
        mine += sum(
            1
            for n in state.companions
            if (sp := state.spots.get(n)) is not None
            and sp.area == area.id
            and n not in state.people_down
        )
        theirs = self.person_score(state, name)
        spot = state.spots.get(name)
        group = self.group_of(name)
        if spot is not None and group:
            for other, sp in state.spots.items():
                if other in (name, held) or sp.area != area.id or other in state.people_down:
                    continue
                if self.group_of(other) != group:
                    continue
                if math.hypot(sp.x - spot.x, sp.y - spot.y) * area.tile_m <= ALLY_M:
                    theirs += 1
        return mine - theirs

    def _follows_in(self, state, area: Area, me: Spot, name: str, spot, demand) -> bool:
        """Whether a demander left behind on the other side of a door comes
        after the player: their demand unanswered, the player within their
        reach when last seen, the two areas parts of one place (a farm and
        its farmhouse). Stands them beside the player when so."""
        if spot is None or spot.area == area.id or demand.pending:
            return False
        if name in state.people_down or name == self.held_name():
            return False
        if state.goals.get(name, NpcGoal()).intent == "attack":
            return False
        if not 0 <= demand.last_metres <= ENFORCE_M or not self._same_root(spot.area, area.id):
            return False
        taken = area.blocked() | {(s.x, s.y) for s in state.spots.values() if s.area == area.id}
        x, y = stand_near(area, (me.x, me.y), taken | {(me.x, me.y)})
        state.spots[name] = Spot(area=area.id, x=x, y=y)
        return True

    def demands_due(self) -> list[tuple[str, str]]:
        """After one of the player's actions: each standing demand the
        action ignored, with the step the program decides (saved on the
        demand as pending, to be voiced). A demand lapses once the player
        is out of reach of it."""
        state = self.area_state()
        area, me = self._visual_me(state)
        if area is None or me is None or state.dead:
            return []
        held = self.held_name() or ""
        out: list[tuple[str, str]] = []
        changed = False
        key = self._action_key(state, me)
        for name, demand in list(state.demands.items()):
            spot = state.spots.get(name)
            if self._follows_in(state, area, me, name, spot, demand):
                # The player went through a door with the demand unanswered,
                # within the demander's reach: they come in after them, and
                # the going counts as ignoring them (live, Oct 28 2026: John
                # walked past a patrol's rifle into a farmhouse and the demand
                # lapsed without a word).
                spot = state.spots[name]
                demand.ignored += 1
                demand.last_key = key
                demand.last_metres = math.hypot(spot.x - me.x, spot.y - me.y) * area.tile_m
                demand.pending = escalate(
                    demand.ignored, self.standing_of(name, state), self.danger(state, area, name)
                )
                changed = True
                out.append((name, demand.pending))
                continue
            if (
                spot is None
                or spot.area != area.id
                or name in state.people_down
                or name == held
                or state.goals.get(name, NpcGoal()).intent == "attack"
            ):
                state.demands.pop(name, None)
                changed = True
                continue
            here = (spot.x, spot.y)
            metres = math.hypot(spot.x - me.x, spot.y - me.y) * area.tile_m
            if metres > ENFORCE_M or not (
                sees(area, (me.x, me.y), here) or in_earshot(area, (me.x, me.y), here)
            ):
                state.demands.pop(name, None)  # out of their reach: it lapses
                changed = True
                continue
            if demand.pending:
                continue
            if demand.addressed > demand.seen:
                demand.seen = demand.addressed
                demand.last_key = key
                demand.last_metres = metres
                changed = True
                continue
            if demand.last_key == key:
                continue
            if metres < demand.last_metres - 1.0:
                # Coming nearer (to speak, to hand something over) is
                # answering: live, the walk up to talk counted as a second
                # ignore and the gate watch opened fire.
                demand.last_key = key
                demand.last_metres = metres
                changed = True
                continue
            demand.ignored += 1
            demand.last_key = key
            demand.last_metres = metres
            demand.pending = escalate(
                demand.ignored, self.standing_of(name, state), self.danger(state, area, name)
            )
            changed = True
            out.append((name, demand.pending))
        if changed:
            self._set_live(state)
        return out

    def apply_demand_step(self, state, name: str, reply: NpcReply) -> str | None:
        """The step pending on this person's demand, carried out as their
        reply comes in: the intent the program holds them to (None leaves
        the reply's own)."""
        demand = state.demands.get(name)
        if demand is None or not demand.pending:
            return None
        step, demand.pending = demand.pending, ""
        if step == STEP_WARN:
            demand.warnings += 1
            area, me = self._visual_me(state)
            demand.last_key = self._action_key(state, me)
            spot = state.spots.get(name)
            if area is not None and me is not None and spot is not None:
                demand.last_metres = self._metres_between(area, me, spot)
            return None
        state.demands.pop(name, None)
        if step == STEP_ATTACK:
            return "attack"
        return "stay" if reply.intent == "attack" else None

    def demand_without_reply(self, name: str) -> list[str]:
        """The pending step carried out in the program's own words when the
        person's model didn't answer."""
        state = self.area_state()
        demand = state.demands.get(name)
        if demand is None or not demand.pending:
            return []
        step = demand.pending
        forced = self.apply_demand_step(state, name, NpcReply("", "", "stay", "", "", ""))
        lines: list[str] = []
        if step == STEP_WARN:
            lines.append(f"{name} warns you again: {demand.what}")
        elif step == STEP_STAND_DOWN:
            lines.append(f"{name} lets you be.")
        if forced == "attack":
            if name in state.companions:
                state.companions.remove(name)
            state.goals[name] = NpcGoal(intent="attack", toward=self.held_name() or "")
            lines.append(f"{name} goes for you.")
            self._set_live(state)
            lines += self.visual_tick(0)
            return lines
        self._set_live(state)
        return lines
