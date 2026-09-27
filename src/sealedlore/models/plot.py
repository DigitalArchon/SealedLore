"""Plots: facts the story keeps, and the events a director brings about.

A plot is written as Markdown (see SampleStories/PLOT_FORMAT.md) and parsed by
sealedlore.engine.plot_md. It is the *definition* only: what facts exist and
what can happen when. What has happened in a playthrough is kept per passage,
like the scene, so it follows branches.

The storyteller never sees this. The director, a small model, reads the
events that could happen now and passes one on when the moment suits it; the
storyteller learns of an event only then. Characters and places can be hidden
the same way (`Character.plot_hidden`, `LoreEntry.plot_hidden`) until an event
brings them in: a model can't reach for what it was never shown.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

EventKind = Literal["timed", "optional"]
# "quiet": wait for a lull (the author's turns shortening, the character
# settling for the night). "any": whenever the conditions hold.
Pacing = Literal["quiet", "any"]
ConditionOp = Literal["=", "!="]
# Where the author's character is (`at`), or has ever been (`visited`), among
# the plot's places. Kept by the program, not as facts.
PlaceTest = Literal["at", "visited"]

MINUTES_PER_DAY = 24 * 60


class FactDef(BaseModel):
    """A fact the story keeps: `hellsville = fallen`, `visited_hellsville = yes`."""

    name: str
    initial: str
    # What the fact means and when it changes, in words, for the read that
    # keeps it up to date.
    meaning: str = ""
    # The values it may take. Empty means any value; a yes/no fact gets both.
    values: list[str] = Field(default_factory=list)
    # Kept up to date by the read after each passage. Otherwise only an
    # event's `sets:` or the author changes it: a read asked about facts it
    # had no business judging flipped them on any stranger (live, Doomsville).
    watched: bool = False


class PlaceDef(BaseModel):
    """A place the author's character can be at, for `at` and `visited`."""

    name: str
    lore_id: str | None = None


class Condition(BaseModel):
    """`fact = value`, `fact != value`, `<event id> happened`, `at = Place` or
    `visited Place` (`op` "!=" negates the place tests)."""

    fact: str | None = None
    op: ConditionOp = "="
    value: str = ""
    event_id: str | None = None
    # For an event condition: whether it must have happened (True) or not.
    happened: bool = True
    place: str | None = None
    place_test: PlaceTest = "at"
    # A place test about a named character ("John at = The Beacon") rather
    # than whoever the author is holding: their character id.
    who: str | None = None


class Assignment(BaseModel):
    fact: str
    value: str


class EventVariant(BaseModel):
    """One way an event can go, chosen by what holds when it happens."""

    title: str = ""
    requires: list[Condition] = Field(default_factory=list)
    # In words, for the director: what has to be happening for this to fit.
    trigger: str = ""
    # What the storyteller is told should happen.
    tell: str = ""
    sets: list[Assignment] = Field(default_factory=list)
    # Happens away from the player's character: no prose, just the facts.
    offscreen: bool = False
    # Character and lore ids this way of it brings into the story.
    brings: list[str] = Field(default_factory=list)
    reveals: list[str] = Field(default_factory=list)
    # Where it happens: seen to happen, it puts the author's character there.
    place: str | None = None


class EventDef(BaseModel):
    id: str
    title: str
    kind: EventKind
    # First and last day (1-based, inclusive) for a timed event.
    window: tuple[int, int] | None = None
    pacing: Pacing = "quiet"
    # The director may pass it on ahead of time, so the storyteller can
    # foreshadow it.
    lead_in: bool = False
    requires: list[Condition] = Field(default_factory=list)
    trigger: str = ""
    # Shared by every variant, ahead of the variant's own.
    tell: str = ""
    # What is left afterwards, and what people say: what a later visitor
    # finds, passed on when it becomes relevant.
    aftermath: str = ""
    variants: list[EventVariant] = Field(default_factory=list)
    # A timed event that must resolve one way or another once its window
    # closes. None means the default: yes, unless the event has its own
    # `requires` (then it only applies if they hold, like "alone too long").
    must_happen: bool | None = None
    # Character and lore ids the event brings in, whichever way it goes.
    brings: list[str] = Field(default_factory=list)
    reveals: list[str] = Field(default_factory=list)
    place: str | None = None

    @property
    def guaranteed(self) -> bool:
        if self.kind != "timed":
            return False
        return self.must_happen if self.must_happen is not None else not self.requires


class Plot(BaseModel):
    # Story time at the first passage, in minutes from day 1, 00:00.
    start_minutes: int = 0
    facts: list[FactDef] = Field(default_factory=list)
    events: list[EventDef] = Field(default_factory=list)
    places: list[PlaceDef] = Field(default_factory=list)

    def fact(self, name: str) -> FactDef | None:
        return next((fact for fact in self.facts if fact.name == name), None)

    def event(self, event_id: str) -> EventDef | None:
        return next((event for event in self.events if event.id == event_id), None)


# --- what a playthrough has done with it -------------------------------------

# Who last set a chronicle snapshot. "story" is the read after a passage,
# which the Plot panel offers to undo; anything the author does is "author".
ChronicleSource = Literal["author", "story"]
EventState = Literal["pending", "led_in", "directed", "happened", "missed"]
# What the director asks of the storyteller for one event on one turn:
# "begin" it in this passage, "lead_in" (foreshadow it; it hasn't happened),
# or "reveal" what an event that happened elsewhere left behind.
DirectionKind = Literal["begin", "lead_in", "reveal", "gap"]


class FactChange(BaseModel):
    fact: str
    old: str
    new: str
    # The sentence that shows it, from the passage; empty when the author set it.
    quote: str = ""


class EventStatus(BaseModel):
    state: EventState = "pending"
    variant: str | None = None
    # The passage it happened at, and when on the story's clock.
    node_id: str | None = None
    at_minutes: int | None = None
    # Whether the player's character has seen it or learned of it.
    revealed: bool = False
    # How it went, for an event told as having happened in a time skip:
    # the storyteller's own account, kept in the story-time block.
    account: str = ""
    # The side call said the story shows it can't have happened in the gap:
    # it begins on-screen instead, and is never offered as a gap again.
    not_in_gap: bool = False
    # The director's back-off (`chronicle.resting`): how many times running
    # it has declined to begin this, the turns left before it is asked again,
    # and the clock and scene when it last was.
    declined: int = 0
    rest: int = 0
    asked_minutes: int = 0
    asked_scene: str = ""
    # A timed event whose last day passed: it happens at the next chance,
    # whatever the moment.
    overdue: bool = False
    # Passages written since it was passed on without being seen to happen.
    # An overdue one is taken as happened after a couple, so a plot always
    # reaches some outcome.
    unconfirmed_passages: int = 0
    # A guaranteed event whose window closed with no variant fitting: the
    # one the plot model chose, which begins whatever its conditions say.
    forced_variant: str | None = None


class Direction(BaseModel):
    """One instruction the director gave the storyteller, kept on the author's turn.

    Kept so Regenerate writes another take of the same instruction rather
    than asking the director again (as a dice roll is kept).
    """

    event_id: str
    kind: DirectionKind
    variant: str | None = None
    # The words the storyteller was given for it.
    text: str = ""
    # The director's reason, for the log and the spoiler view.
    why: str = ""


class Chronicle(BaseModel):
    """The plot's state after one passage: the clock, the facts, the events.

    A snapshot on the node it describes (`NodeMeta.chronicle`), like the
    scene: the state anywhere is the nearest snapshot at or above it, so it
    follows branches, retakes and deletions with nothing to keep in sync.
    """

    # Story time, in minutes from day 1, 00:00.
    minutes: int = 0
    facts: dict[str, str] = Field(default_factory=dict)
    events: dict[str, EventStatus] = Field(default_factory=dict)
    source: ChronicleSource = "author"
    # What this passage did, for the panel and its Undo (reads only).
    elapsed: int = 0
    elapsed_quote: str = ""
    changes: list[FactChange] = Field(default_factory=list)
    # Where the author's character is among the plot's places (None:
    # elsewhere), and every place they have been.
    place: str | None = None
    visited: list[str] = Field(default_factory=list)
    # `place` and `visited` are the character's the author holds (`held_id`).
    # Everyone held before keeps theirs here, by character id, so switching
    # back finds them where they were. `place_known` is off from taking up
    # someone never held until a read or the author says where they are.
    held_id: str | None = None
    place_known: bool = True
    places: dict[str, str | None] = Field(default_factory=dict)
    visits: dict[str, list[str]] = Field(default_factory=dict)
    # Hidden characters and places (by id) an event has brought into the
    # story on this branch.
    brought_in: list[str] = Field(default_factory=list)
