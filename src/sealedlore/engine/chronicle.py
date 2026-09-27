"""The plot's state at any point in the story: clock, facts, events. Pure: no I/O.

Kept like the scene (engine/scene_state.py): a snapshot on the node it
describes, and the state anywhere is the nearest snapshot at or above it. A
story with a plot and no snapshot yet is at the plot's start.
"""

from __future__ import annotations

import random
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field

from sealedlore.models.node import Node
from sealedlore.models.plot import (
    MINUTES_PER_DAY,
    Chronicle,
    Condition,
    DirectionKind,
    EventDef,
    EventStatus,
    EventVariant,
    FactChange,
    Plot,
)


def initial_chronicle(plot: Plot) -> Chronicle:
    return Chronicle(
        minutes=plot.start_minutes,
        facts={fact.name: fact.initial for fact in plot.facts},
        events={event.id: EventStatus() for event in plot.events},
    )


def fitted(chronicle: Chronicle, plot: Plot) -> Chronicle:
    """A chronicle that matches the plot it is read against.

    A plot re-imported with a fact added or an event dropped mustn't leave
    snapshots naming things that no longer exist, or miss ones that now do.
    """
    facts = {
        fact.name: (
            chronicle.facts[fact.name]
            if fact.name in chronicle.facts
            and (not fact.values or chronicle.facts[fact.name] in fact.values)
            else fact.initial
        )
        for fact in plot.facts
    }
    events = {
        event.id: chronicle.events.get(event.id, EventStatus()).model_copy(deep=True)
        for event in plot.events
    }
    return chronicle.model_copy(update={"facts": facts, "events": events}, deep=True)


def snapshot_index(path: Sequence[Node]) -> int | None:
    for index in range(len(path) - 1, -1, -1):
        if path[index].meta.chronicle is not None:
            return index
    return None


def chronicle_at(path: Sequence[Node], plot: Plot) -> Chronicle:
    """A copy of the plot's state at the end of `path`."""
    index = snapshot_index(path)
    if index is None:
        return initial_chronicle(plot)
    source = path[index].meta.chronicle
    assert source is not None
    return fitted(source, plot)


def carried(chronicle: Chronicle) -> Chronicle:
    """The same state as a fresh snapshot: nothing yet changed by the next passage."""
    return chronicle.model_copy(
        update={"source": "author", "elapsed": 0, "elapsed_quote": "", "changes": []},
        deep=True,
    )


# --- the clock ---------------------------------------------------------------


def day_of(minutes: int) -> int:
    return minutes // MINUTES_PER_DAY + 1


def format_clock(minutes: int) -> str:
    """ "Day 35 · 22:10"."""
    within = minutes % MINUTES_PER_DAY
    return f"Day {day_of(minutes)} · {within // 60:02d}:{within % 60:02d}"


def clock_minutes(day: int, hour: int, minute: int) -> int:
    return (max(day, 1) - 1) * MINUTES_PER_DAY + hour * 60 + minute


def format_duration(minutes: int) -> str:
    """ "3 days 4h", "8h 30m", "20m"."""
    if minutes <= 0:
        return "no time"
    days, rest = divmod(minutes, MINUTES_PER_DAY)
    hours, mins = divmod(rest, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours}h")
    if mins and not days:
        parts.append(f"{mins}m")
    return " ".join(parts) or "under a minute"


def advance(minutes: int, elapsed: int, ends_at: int | None) -> int:
    """The clock after `elapsed` minutes, landing on `ends_at` (minute of day) if given.

    "She sleeps, and wakes at dawn" is best read as the next dawn, not as a
    count of hours the model had to guess: whole days pass first, then the
    clock moves on to the next time it reads that hour.
    """
    if ends_at is None:
        return minutes + max(elapsed, 0)
    base = minutes + (max(elapsed, 0) // MINUTES_PER_DAY) * MINUTES_PER_DAY
    candidate = base - base % MINUTES_PER_DAY + ends_at
    if candidate <= base:
        candidate += MINUTES_PER_DAY
    return candidate


def describe_changes(chronicle: Chronicle) -> str:
    """ "+8h 30m, now Day 36 · 06:30; hellsville: standing → fallen"."""
    parts: list[str] = []
    if chronicle.elapsed:
        parts.append(
            f"+{format_duration(chronicle.elapsed)}, now {format_clock(chronicle.minutes)}"
        )
    parts.extend(f"{change.fact}: {change.old} → {change.new}" for change in chronicle.changes)
    return "; ".join(parts)


def set_fact(chronicle: Chronicle, name: str, value: str, *, quote: str = "") -> bool:
    """Change a fact, recording the change. Returns whether it changed."""
    old = chronicle.facts.get(name)
    if old is None or old == value:
        return False
    chronicle.facts[name] = value
    chronicle.changes.append(FactChange(fact=name, old=old, new=value, quote=quote))
    return True


# --- events ------------------------------------------------------------------

# How many days ahead of its day a timed event with `lead-in: yes` may be
# foreshadowed.
LEAD_IN_DAYS = 3


def holds(conditions: Sequence[Condition], chronicle: Chronicle) -> bool:
    """Whether every condition holds. Checked in code, never by a model."""
    for condition in conditions:
        if condition.event_id is not None:
            status = chronicle.events.get(condition.event_id)
            happened = status is not None and status.state == "happened"
            if happened != condition.happened:
                return False
        elif condition.place is not None:
            place, visited = place_of(chronicle, condition.who)
            if condition.place_test == "at":
                truth = place == condition.place
            else:
                truth = condition.place in visited
            if truth != (condition.op == "="):
                return False
        else:
            is_equal = chronicle.facts.get(condition.fact or "") == condition.value
            if is_equal != (condition.op == "="):
                return False
    return True


def place_of(chronicle: Chronicle, who: str | None) -> tuple[str | None, list[str]]:
    """Where a character is and has been: the held one, or one held before."""
    if who is None or who == chronicle.held_id:
        return chronicle.place, chronicle.visited
    return chronicle.places.get(who), chronicle.visits.get(who, [])


def take_viewpoint(chronicle: Chronicle, held_id: str | None) -> bool:
    """Follow the author to the character they now hold. Returns whether it moved.

    The last character's place is kept for when the author comes back, and
    this one's is restored. Someone never held before is nowhere known yet:
    the next read says where, with no move to show (live, every read after a
    switch to the other character was refused "moved to elsewhere" and left
    him where the first one was).
    """
    if held_id is None or held_id == chronicle.held_id:
        return False
    if chronicle.held_id is None:
        # Nobody was being followed yet (the story's start, or a snapshot from
        # before places were kept per character): what is recorded is theirs.
        chronicle.held_id = held_id
        return True
    chronicle.places[chronicle.held_id] = chronicle.place
    chronicle.visits[chronicle.held_id] = list(chronicle.visited)
    chronicle.held_id = held_id
    chronicle.place_known = held_id in chronicle.places
    chronicle.place = chronicle.places.pop(held_id, None)
    chronicle.visited = chronicle.visits.pop(held_id, [])
    return True


def set_place(chronicle: Chronicle, place: str | None) -> bool:
    """Move the author's character; a place once reached counts as visited."""
    chronicle.place_known = True
    if place == chronicle.place:
        return False
    chronicle.place = place
    if place is not None and place not in chronicle.visited:
        chronicle.visited.append(place)
    return True


def introduced(event: EventDef, variant: EventVariant | None) -> list[str]:
    """Character and lore ids an event brings in, the variant's included."""
    ids = [*event.brings, *event.reveals]
    if variant is not None:
        ids += [*variant.brings, *variant.reveals]
    return list(dict.fromkeys(ids))


def variant_named(event: EventDef, title: str | None) -> EventVariant | None:
    return next((v for v in event.variants if (v.title or None) == title), None)


def schedule(plot: Plot, existing: dict[str, int], rng: random.Random) -> dict[str, int]:
    """Each timed event's day, drawn once from its window; kept ones kept.

    A kept day outside a re-imported event's window is drawn again.
    """
    days: dict[str, int] = {}
    for event in plot.events:
        if event.kind != "timed" or event.window is None:
            continue
        first, last = event.window
        kept = existing.get(event.id)
        days[event.id] = (
            kept if kept is not None and first <= kept <= last else (rng.randint(first, last))
        )
    return days


def opens_on(event: EventDef, days: dict[str, int]) -> int | None:
    """The first day the event can happen; None for any day."""
    if event.kind == "timed":
        return days.get(event.id, event.window[0] if event.window else 1)
    return event.window[0] if event.window else None


def eligible(event: EventDef, chronicle: Chronicle) -> list[EventVariant]:
    """The variants whose conditions hold now, the event's own included."""
    if not holds(event.requires, chronicle):
        return []
    return [variant for variant in event.variants if holds(variant.requires, chronicle)]


def happen(
    chronicle: Chronicle,
    event: EventDef,
    variant: EventVariant,
    *,
    node_id: str | None,
    revealed: bool,
    quote: str = "",
    at_minutes: int | None = None,
    moves: bool = True,
) -> None:
    """Record an event as happened, apply what it sets, and bring in what it brings.

    `moves` off: it happened in the past (in a time skip), so its `at:` place
    says where it was then, not where the author's character is now.
    """
    status = chronicle.events.setdefault(event.id, EventStatus())
    status.state = "happened"
    status.variant = variant.title or None
    status.node_id = node_id
    status.at_minutes = chronicle.minutes if at_minutes is None else at_minutes
    status.revealed = revealed
    status.overdue = False
    status.forced_variant = None
    status.unconfirmed_passages = 0
    for assignment in variant.sets:
        set_fact(chronicle, assignment.fact, assignment.value, quote=quote)
    # Offscreen, nobody was met: a character stays out of the story until the
    # author's character could know them.
    if revealed or not variant.offscreen:
        for item_id in introduced(event, variant):
            if item_id not in chronicle.brought_in:
                chronicle.brought_in.append(item_id)
    # Seen to happen somewhere, it puts them there: live, John was welcomed
    # at Hellsville's gate while the read, missing the arrival, never
    # recorded the visit, and the fall later went as if he had never been.
    place = variant.place or event.place
    if place is not None and moves and not variant.offscreen:
        set_place(chronicle, place)


def scheduled_minutes(event: EventDef, days: dict[str, int], now: int) -> int:
    """When an event resolved offscreen happened: its own day, not the day a
    time skip found it (a skip from day 1 to 39 dated a day-31 fall to day 39)."""
    start = opens_on(event, days)
    if start is None:
        return now
    return min(now, (start - 1) * MINUTES_PER_DAY + 22 * 60)


@dataclass
class Resolution:
    """What `resolve_due` settled, and what it needs a model to settle."""

    happened: list[EventDef]
    changed: bool
    # Guaranteed events whose window closed with no variant fitting: the
    # plot model picks one (`director.choose_variant`), then `force`.
    undecided: list[EventDef]
    # On-screen events more than `GAP_DAYS` past their window: they happened
    # in the time that passed, and are told that way (`StorySession.direct_turn`).
    gap: list[tuple[EventDef, EventVariant]] = field(default_factory=list)


# An event this many days past the end of its window when the story reaches
# it was skipped over, not missed by a day: it is told as having happened in
# the gap. Live, one skip from day 2 to 28 left eight events overdue, and a
# council "answered" on day 28 with the character it answered weeks away.
GAP_DAYS = 3


def is_late(event: EventDef, chronicle: Chronicle) -> bool:
    """Whether the story is more than `GAP_DAYS` past this event's window."""
    return event.window is not None and day_of(chronicle.minutes) > event.window[1] + GAP_DAYS


def force(
    chronicle: Chronicle,
    event: EventDef,
    variant: EventVariant,
    days: dict[str, int],
    *,
    node_id: str | None,
) -> None:
    """Make a chosen variant the outcome: offscreen now, or on-screen at the next chance."""
    if variant.offscreen:
        happen(
            chronicle,
            event,
            variant,
            node_id=node_id,
            revealed=False,
            at_minutes=scheduled_minutes(event, days, chronicle.minutes),
        )
        return
    status = chronicle.events.setdefault(event.id, EventStatus())
    status.overdue = True
    status.forced_variant = variant.title or None


def resolve_due(
    plot: Plot, chronicle: Chronicle, days: dict[str, int], *, node_id: str | None
) -> Resolution:
    """What code decides with no model: offscreen outcomes and closed windows.

    From its day, an event whose only fitting variants happen offscreen
    happens, silently, dated to its own day. Once its window has closed, an
    event still to happen is overdue if an on-screen variant fits (sent at
    the next chance, whatever the moment). If nothing fits, a guaranteed
    event (`EventDef.guaranteed`) is left for the plot model to choose an
    outcome for; any other is missed.
    """
    day = day_of(chronicle.minutes)
    result = Resolution(happened=[], changed=False, undecided=[])
    for event in plot.events:
        status = chronicle.events.setdefault(event.id, EventStatus())
        if status.state not in ("pending", "led_in"):
            continue
        if status.forced_variant:
            forced = variant_named(event, status.forced_variant)
            if forced is not None and is_late(event, chronicle) and not status.not_in_gap:
                result.gap.append((event, forced))
            continue
        start = opens_on(event, days)
        if start is None or day < start:
            continue
        closed = event.window is not None and day > event.window[1]
        # Its own conditions no longer hold: the story has moved past it. A
        # must-happen fall of a town the author's story already burned would
        # otherwise be forced, setting the town back to "under attack" (live).
        if closed and not holds(event.requires, chronicle):
            status.state = "missed"
            result.changed = True
            continue
        fitting = eligible(event, chronicle)
        onscreen = [variant for variant in fitting if not variant.offscreen]
        offscreen = [variant for variant in fitting if variant.offscreen]
        if offscreen and not onscreen:
            happen(
                chronicle,
                event,
                offscreen[0],
                node_id=node_id,
                revealed=False,
                at_minutes=scheduled_minutes(event, days, chronicle.minutes),
            )
            result.happened.append(event)
            result.changed = True
        elif (
            closed
            and onscreen
            and event.guaranteed
            and is_late(event, chronicle)
            and not status.not_in_gap
        ):
            result.gap.append((event, onscreen[0]))
        elif closed and onscreen and event.guaranteed:
            if not status.overdue:
                status.overdue = True
                result.changed = True
        elif closed and onscreen:
            # A conditional event that still fits keeps waiting for its
            # trigger ("the next time he sleeps"). Forced as overdue, the
            # day-100 death struck at dawn on day 124, snares in hand.
            continue
        elif closed and event.guaranteed:
            result.undecided.append(event)
        elif closed:
            status.state = "missed"
            result.changed = True
    return result


# An event passed on and not yet seen to happen is taken as having happened
# after this many passages: a plot must reach some outcome. It counts from the
# first time the event was passed on, not the latest: an event sent again each
# turn used to reset the count, so it never ran out (live, "A guide for the
# Reach" was sent four times running and only the read ever settled it).
# Measured over every logged playtest, 33 of 36 directed events were seen on
# the next passage; the rest were one multi-step event (find a guide, then set
# out with him) that took four and five.
UNCONFIRMED_LIMIT = 6


def begins_unasked(event: EventDef) -> bool:
    """An "any time" event with no trigger anywhere: nothing for a model to judge."""
    return (
        event.pacing == "any"
        and not event.trigger
        and all(not variant.trigger for variant in event.variants)
    )


def settle_unconfirmed(
    plot: Plot, chronicle: Chronicle, confirmed: Sequence[str], *, node_id: str | None
) -> list[EventDef]:
    """After a passage: count directed events it didn't show happening.

    One the storyteller is sent again every turn without asking — overdue
    (its window closed) or "any time" with no trigger — is taken as happened
    after `UNCONFIRMED_LIMIT` passages, so the facts carry its outcome even if
    the read never saw it, and it stops being sent. One the director chose to
    begin is left to the director, which is asked again and can say it
    happened.
    """
    day = day_of(chronicle.minutes)
    settled: list[EventDef] = []
    for event in plot.events:
        status = chronicle.events.get(event.id)
        if status is None or status.state != "directed" or event.id in confirmed:
            continue
        status.unconfirmed_passages += 1
        closed = event.window is not None and day > event.window[1]
        if (closed or begins_unasked(event)) and status.unconfirmed_passages >= UNCONFIRMED_LIMIT:
            variant = variant_named(event, status.variant) or event.variants[0]
            happen(chronicle, event, variant, node_id=node_id, revealed=True)
            settled.append(event)
    return settled


@dataclass(frozen=True)
class Armed:
    """An event the director may act on this turn, and how."""

    event: EventDef
    kinds: tuple[DirectionKind, ...]
    # The on-screen variants that fit, for "begin".
    variants: tuple[EventVariant, ...] = ()
    overdue: bool = False
    # Directed earlier and not yet seen to happen.
    unconfirmed: bool = False

    @property
    def automatic(self) -> bool:
        """Begins with no judgement to make: overdue, or "any time" with no trigger."""
        if "begin" not in self.kinds:
            return False
        if self.overdue:
            return True
        return (
            self.event.pacing == "any"
            and not self.event.trigger
            and all(not variant.trigger for variant in self.variants)
        )


# An "any time" event the director declined to begin is not asked about again
# for a turn, then two, then three at most. Live, "First bandit meeting"
# (day 2-60) and "The caves" kept the director consulted on 18 of 20 turns,
# about a second before every first token, to be told "not yet" each time. A
# no in one scene predicts a no on the next turn of it; a quiet-moment event
# is never rested, since its moment (turning in for the night) is one turn.
MAX_REST = 3
# The scene moving, or this much story time passing, ends a rest at once.
REST_ENDS_AFTER_MINUTES = 6 * 60


def can_rest(item: Armed) -> bool:
    """Declining it now says little about the next turn, so it may wait a little.

    A reveal waits for someone who could tell it. Live ("alone", GLM 5.3): the
    fall happened unseen on day 49 and the director was asked about telling it
    on 25 of 31 turns while John crossed empty country for a hundred days.
    """
    if item.overdue or item.unconfirmed:
        return False
    return item.kinds == ("reveal",) or (item.kinds == ("begin",) and item.event.pacing == "any")


def resting(item: Armed, chronicle: Chronicle, scene: str) -> bool:
    """Whether the director needn't be asked about this event this turn."""
    status = chronicle.events.get(item.event.id)
    if status is None or not can_rest(item) or status.rest <= 0:
        return False
    moved = scene != status.asked_scene
    long_since = chronicle.minutes - status.asked_minutes >= REST_ENDS_AFTER_MINUTES
    return not (moved or long_since)


def note_asked(
    items: Sequence[Armed], begun: Collection[str], chronicle: Chronicle, scene: str
) -> None:
    """After a director call: the events it declined rest a little longer each time."""
    for item in items:
        status = chronicle.events.get(item.event.id)
        if status is None or not can_rest(item) or item.event.id in begun:
            continue
        status.declined += 1
        # A reveal rests one turn at most: the moment it waits for (someone
        # who could tell it, the author heading its way) can come any time.
        status.rest = min(status.declined, 1 if item.kinds == ("reveal",) else MAX_REST)
        status.asked_minutes = chronicle.minutes
        status.asked_scene = scene


def note_skipped(items: Sequence[Armed], chronicle: Chronicle) -> None:
    """A turn on which nothing needed asking: every rest is a turn shorter."""
    for item in items:
        status = chronicle.events.get(item.event.id)
        if status is not None and status.rest > 0:
            status.rest -= 1


def armed(
    plot: Plot,
    chronicle: Chronicle,
    days: dict[str, int],
    *,
    mentioned: Collection[str] | None = None,
) -> list[Armed]:
    """Everything worth a director call. Empty means no call is made.

    `mentioned` is the places the author's turn and the last passage name.
    Given it, an optional event that happens at a place (`at:`) is armed only
    when the author's character is there or the place has come up: live,
    "Welcome to Hellsville" was armed from turn one, and the director was
    asked on 30 of 31 turns of a story that spent most of them in a farmhouse.
    The mention counts because the place read can miss an arrival. `None`
    leaves every event armed that could begin (the Plot tab's "could happen
    now").
    """
    day = day_of(chronicle.minutes)
    found: list[Armed] = []
    for event in plot.events:
        status = chronicle.events.get(event.id, EventStatus())
        if status.state == "happened":
            if not status.revealed and event.aftermath:
                found.append(Armed(event, ("reveal",)))
            continue
        if status.state == "missed":
            continue
        start = opens_on(event, days)
        is_open = start is None or day >= start
        forced = variant_named(event, status.forced_variant) if status.forced_variant else None
        onscreen = (
            (forced,)
            if forced is not None
            else tuple(v for v in eligible(event, chronicle) if not v.offscreen)
        )
        if mentioned is not None and event.kind == "optional" and status.state == "pending":
            onscreen = tuple(
                v
                for v in onscreen
                if (v.place or event.place) in (None, chronicle.place)
                or (v.place or event.place) in mentioned
            )
        if is_open and onscreen:
            # Once it can begin, it is no longer foreshadowed: offered both,
            # Haiku 4.5 led in at the very moment the event was meant to
            # start (3/3 live). The lead-in belongs to the days before.
            found.append(
                Armed(
                    event,
                    ("begin",),
                    onscreen,
                    overdue=status.overdue,
                    unconfirmed=status.state == "directed",
                )
            )
        elif (
            event.lead_in
            and status.state == "pending"
            and start is not None
            and day >= start - LEAD_IN_DAYS
        ):
            found.append(Armed(event, ("lead_in",)))
    return found
