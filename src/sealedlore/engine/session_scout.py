"""Scouting (the author, Oct 27 2026: "a scouting stat and ability which
lets them attempt to extend their line of sight to see other characters
first, so they can decide to approach or not"). A mixin for `StorySession`
beside the visual runtime.

When someone (a person, one of the dead) first comes within SCOUT_M of the
player with a line of sight, further than the ordinary sight range, a roll
is made once: the player's scouting against the other's alertness, a die
each. Won, the player has spotted them first: they stand unaware (the
encounter, their reaction on seeing the player, waits until the player is
within NOTICE_M of them, or acts on them), and the player can choose to go
round. Lost, the other has seen the player first, from afar: they react
now, as they would (a greeting across the yard, a shot, a retreat).
"""

from __future__ import annotations

import math
import random

from sealedlore.engine.area import Area, sees
from sealedlore.engine.visual_rules import PERSON_SIGHT_M
from sealedlore.models.area import Spot

# How far a scout may see someone first.
SCOUT_M = 100.0
# This close, anyone notices the player whatever the scouting.
NOTICE_M = 25.0
DIE = 6
# A card's tier at a scouting skill, as a scouting score.
TIER_SCOUT = {"none": 2, "poor": 3, "competent": 4, "skilled": 5, "expert": 6, "legendary": 7}
SCOUT_SKILLS = ("scouting", "scout", "tracking", "stealth", "awareness", "reconnaissance")


class ScoutRuntime:
    def scout_score(self) -> int:
        """The player's scouting: the design's for them, raised to a card
        tier's score at a scouting skill when the card has one."""
        design = self.visual_design()
        held = self.held_name() or ""
        score = design.scouting.get(held, design.default_scouting)
        card = next((c for c in self.cast if c.id == self.story.held_character_id), None)
        if card is not None:
            for skill, tier in card.competence.tiers.items():
                if any(word in skill.lower() for word in SCOUT_SKILLS):
                    score = max(score, TIER_SCOUT.get(tier, score))
        return score

    def alertness_of(self, name: str) -> int:
        sheet = self.visual_sheet(name)
        return sheet.alertness if sheet.alertness is not None else sheet.ability

    def scout_report(self, state, area: Area, me: Spot) -> list[str]:
        """The scouting rolls due where the player stands: whoever has come
        within SCOUT_M with a line of sight and not been rolled for in this
        area, and the lines for the log."""
        lines: list[str] = []
        held = self.held_name()
        rng = random.Random(f"{self.visual_seed()}:{state.seconds}:scout")
        scout = self.scout_score()
        candidates: list[tuple[str, str, int, int, int]] = []
        for name, spot in state.spots.items():
            if name == held or spot.area != area.id or name in state.people_down:
                continue
            candidates.append((name, name, spot.x, spot.y, self.alertness_of(name)))
        for creature in state.creatures.values():
            if creature.area == area.id and creature.state != "down":
                candidates.append((creature.id, creature.name, creature.x, creature.y, 3))
        for key, label, x, y, alert in candidates:
            tag = f"{key}@{area.id}"
            if tag in state.scout_rolled:
                continue
            metres = math.hypot(x - me.x, y - me.y) * area.tile_m
            if metres > SCOUT_M or not sees(area, (me.x, me.y), (x, y)):
                continue
            state.scout_rolled.append(tag)
            where = self._whereabouts(area, me, (x, y))
            if scout + rng.randint(1, DIE) > alert + rng.randint(1, DIE):
                state.spotted.append(tag)
                if label.startswith("one of"):
                    lines.append(f"You spot {label} first, {where}: it hasn't seen you.")
                else:
                    lines.append(f"You spot {label} first, {where}: they haven't seen you.")
            elif metres > PERSON_SIGHT_M and not label.startswith("one of"):
                state.noticed.append(tag)
                lines.append(f"{label} has seen you, {where}.")
        return lines

    @staticmethod
    def _whereabouts(area: Area, me: Spot, spot: tuple[int, int]) -> str:
        metres = math.hypot(spot[0] - me.x, spot[1] - me.y) * area.tile_m
        dx, dy = spot[0] - me.x, spot[1] - me.y
        way = (
            ("east" if dx > 0 else "west")
            if abs(dx) >= abs(dy)
            else ("south" if dy > 0 else "north")
        )
        return f"about {round(metres / 5) * 5} m {way}"

    def unaware_of_player(self, name: str, state=None) -> bool:
        """Whether this person was spotted first and hasn't noticed the
        player yet (further than NOTICE_M)."""
        state = state if state is not None else self.area_state()
        me = state.spots.get(self.held_name() or "")
        spot = state.spots.get(name)
        if me is None or spot is None or spot.area != me.area:
            return False
        if f"{name}@{me.area}" not in state.spotted:
            return False
        area = self.area(me.area)
        if area is None:
            return False
        return math.hypot(spot.x - me.x, spot.y - me.y) * area.tile_m > NOTICE_M

    def noticed_from_afar(self, name: str, state=None) -> bool:
        """Whether this person saw the player first, beyond the ordinary sight range."""
        state = state if state is not None else self.area_state()
        me = state.spots.get(self.held_name() or "")
        return me is not None and f"{name}@{me.area}" in state.noticed

    def player_noticed_by(self, state, name: str) -> None:
        """The player acts on someone they had spotted first: they are
        noticed now."""
        me = state.spots.get(self.held_name() or "")
        if me is None:
            return
        tag = f"{name}@{me.area}"
        if tag in state.spotted:
            state.spotted.remove(tag)
