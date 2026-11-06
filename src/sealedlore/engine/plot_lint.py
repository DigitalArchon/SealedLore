"""What a plot names before its time: a check the importer can't make.

An event's own text is shown to the storyteller days ahead as a lead-in (with
the names of whoever it brings taken out), and it opens the passage the event
begins in, a variant's tell after it. A hidden character or lore entry named
there, which nothing that must have happened by then has brought in, reaches
the storyteller early and with no card: Brasshaven's night of the automatons
named the hidden calculating engine, and the Silverwood's Seal breaking named
the hidden Seal of Thorns. A name the world or the opening already uses is no
secret.

The samples are held to it (tests/test_samples.py), and a plot the model
writes is sent back to be put right when it fails it (engine/plot_draft.py).
Pure.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from sealedlore.engine.chronicle import introduced
from sealedlore.engine.director import direction_text
from sealedlore.engine.validators import TITLES, mentions
from sealedlore.models.character import Character
from sealedlore.models.lore import LoreEntry
from sealedlore.models.plot import Condition, EventDef, Plot


def without_names(text: str, people: Sequence[Character]) -> str:
    """`text` with each person's name and aliases, and a rank before them,
    as "someone": what a lead-in says of whoever its event will bring in.
    Given a name, the storyteller wrote the arrival (live, GLM 5.3)."""
    forms = sorted(
        {form for person in people for form in (person.name, *person.aliases) if form.strip()},
        key=len,
        reverse=True,
    )
    rank = r"(?:(?:" + "|".join(sorted(TITLES)) + r")\.?\s+)?"
    for form in forms:
        text = re.sub(rf"\b{rank}{re.escape(form)}\b", "someone", text, flags=re.IGNORECASE)
    return text


def names_entry(text: str, entry: LoreEntry) -> bool:
    """Whether a text names a lore entry: its title, or a keyword of more than
    one word, articles aside. A one-word keyword ("the singing", "the gate")
    is the story's everyday word, not the secret."""
    for form in (entry.title, *entry.keywords):
        words = re.sub(r"^(?:the|a|an)\s+", "", form.strip(), flags=re.IGNORECASE)
        if form is not entry.title and len(words.split()) < 2:
            continue
        if words and re.search(rf"(?<!\w){re.escape(words)}(?!\w)", text, re.IGNORECASE):
            return True
    return False


def brought_by(event: EventDef) -> set[str]:
    """What an event has certainly brought in once it has happened. Whoever
    an offscreen way brings joins only when the character learns of it."""
    if any(variant.offscreen for variant in event.variants):
        return set()
    ways = [set(variant.brings) | set(variant.reveals) for variant in event.variants]
    return set(event.brings) | set(event.reveals) | (set.intersection(*ways) if ways else set())


def known_once(
    plot: Plot, conditions: Sequence[Condition], seen: frozenset[str] = frozenset()
) -> set[str]:
    """Hidden ids certainly brought in by the time the conditions hold: what
    an event they need has brought, a hidden place they stand the character
    in (and whatever revealed it), and what the events that alone set a fact
    to the value they ask have brought."""

    def after(event: EventDef) -> set[str]:
        return brought_by(event) | known_once(plot, event.requires, seen | {event.id})

    def all_of(events) -> set[str]:
        events = [event for event in events if event.id not in seen]
        return set.intersection(*(after(event) for event in events)) if events else set()

    known: set[str] = set()
    for condition in conditions:
        if condition.event_id and condition.happened and condition.event_id not in seen:
            event = plot.event(condition.event_id)
            if event is not None:
                known |= after(event)
        elif condition.place and condition.op == "=":
            place = next((p for p in plot.places if p.name == condition.place), None)
            if place is not None and place.lore_id:
                known.add(place.lore_id)
                known |= all_of(
                    event
                    for event in plot.events
                    if place.lore_id in event.reveals
                    or any(place.lore_id in variant.reveals for variant in event.variants)
                )
        elif condition.fact and condition.op == "=":
            fact = plot.fact(condition.fact)
            if fact is None or fact.watched or condition.value == fact.initial:
                continue
            known |= all_of(
                event
                for event in plot.events
                if any(
                    (a.fact, a.value) == (condition.fact, condition.value)
                    for a in (a for variant in event.variants for a in variant.sets)
                )
            )
    return known


@dataclass(frozen=True)
class TooEarly:
    """A secret an event's text names before its time."""

    event: str
    # "lead-in", a variant's title, or "begin" for an event of one way.
    way: str
    name: str

    def describe(self) -> str:
        where = "its lead-in" if self.way == "lead-in" else f"“{self.way}”"
        if self.way == "begin":
            where = "its text"
        return (
            f"Event “{self.event}”: {where} names {self.name}, who is hidden and not "
            "certainly brought in by then."
        )


def named_too_early(
    plot: Plot,
    people: Sequence[Character],
    lore: Sequence[LoreEntry],
    told: str,
    *,
    only: Collection[str] | None = None,
) -> list[TooEarly]:
    """Every hidden character or entry an event names before what must have
    happened by then brings it in. `told`: what the story says from the start
    (its world and opening), where a name is no secret. `only`: the ids to
    look for (all hidden ones without)."""
    hidden_people = [c for c in people if c.plot_hidden and (only is None or c.id in only)]
    hidden_lore = [e for e in lore if e.plot_hidden and (only is None or e.id in only)]
    secrets: list[Character | LoreEntry] = [c for c in hidden_people if not mentions(told, c)]
    secrets += [entry for entry in hidden_lore if not names_entry(told, entry)]
    if not secrets:
        return []
    by_id = {c.id: c for c in people}

    def named(text: str, allowed: set[str]) -> list[str]:
        return [
            item.name if isinstance(item, Character) else item.title
            for item in secrets
            if item.id not in allowed
            and (mentions(text, item) if isinstance(item, Character) else names_entry(text, item))
        ]

    found: list[TooEarly] = []
    for event in plot.events:
        before = known_once(plot, event.requires)
        if event.lead_in:
            arriving = [by_id[i] for i in introduced(event, None) if i in by_id]
            lead = without_names(direction_text(event, "lead_in", None), arriving)
            found += [TooEarly(event.title, "lead-in", name) for name in named(lead, before)]
        for variant in event.variants:
            if variant.offscreen:
                continue
            allowed = before | known_once(plot, variant.requires) | set(introduced(event, variant))
            text = direction_text(event, "begin", variant)
            way = variant.title or "begin"
            found += [TooEarly(event.title, way, name) for name in named(text, allowed)]
    return found
