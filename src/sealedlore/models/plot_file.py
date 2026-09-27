"""A plot file as a document: what the editor edits and the renderer writes.

`sealedlore.engine.plot_md` turns a plot file into a `Scenario` for play, and
throws away how it was written to get there: a character's `role:` becomes
two flags, an event's shared `sets:` is folded into each variant, a condition
becomes ids. An author editing the file needs the file's own shape back, so
this is that shape, one model per thing the format has a heading or a line
for. Every value is as the author would write it: names, not ids, and a
condition that couldn't be read is kept as written rather than dropped.

The editor never checks this itself. It renders the document to Markdown and
runs the real importer over it, so what the editor passes is what imports.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from sealedlore.models.node import AgencyMode
from sealedlore.models.story import OpeningMode, Perspective, WorldActivity

CharacterRole = Literal["player", "storyteller", "supporting", "author"]
PacingChoice = Literal["quiet", "any"]
# How a condition is written. "text" is one the reader couldn't make sense
# of, kept as the author wrote it so the importer can say what is wrong.
ConditionKind = Literal["fact", "at", "visited", "happened", "text"]
# The length choices the file accepts: `custom` is set in the app.
FileLength = Literal["adaptive", "brief", "standard", "descriptive", "literary"]


class OpeningDoc(BaseModel):
    mode: OpeningMode = "generate"
    location: str = ""
    time: str = ""
    situation: str = ""
    # The notes the opening is written from, or the opening itself.
    notes: str = ""


class CharacterDoc(BaseModel):
    name: str = ""
    role: CharacterRole = "storyteller"
    # One of the characters the author picks from at the start.
    choose: bool = False
    present: bool = False
    aliases: list[str] = Field(default_factory=list)
    voice: str = ""
    summary: str = ""
    canon: str = ""
    hidden: bool = False
    description: str = ""


class EntryDoc(BaseModel):
    """A place or a lore entry: the same lines, under different headings."""

    name: str = ""
    keywords: list[str] = Field(default_factory=list)
    always: bool = False
    hidden: bool = False
    description: str = ""


class FactDoc(BaseModel):
    name: str = ""
    initial: str = ""
    # Empty means any value; the importer gives a yes/no fact both.
    values: list[str] = Field(default_factory=list)
    watched: bool = False
    meaning: str = ""


class ConditionDoc(BaseModel):
    kind: ConditionKind = "fact"
    negated: bool = False
    # kind "fact": the fact and the value it is (or isn't).
    fact: str = ""
    value: str = ""
    # kinds "at" and "visited": the place, and whose place it is when not
    # the held character's (a character's name).
    place: str = ""
    who: str = ""
    # kind "happened": the event's title.
    event: str = ""
    # kind "text": the clause as written.
    text: str = ""


class AssignmentDoc(BaseModel):
    fact: str = ""
    value: str = ""


class VariantDoc(BaseModel):
    """One way an event goes; an event's own fields are the same, plus timing."""

    title: str = ""
    requires: list[ConditionDoc] = Field(default_factory=list)
    trigger: str = ""
    tell: str = ""
    sets: list[AssignmentDoc] = Field(default_factory=list)
    # None: not said, so a variant takes the event's value.
    offscreen: bool | None = None
    # Character names, and place or lore names.
    brings: list[str] = Field(default_factory=list)
    reveals: list[str] = Field(default_factory=list)
    at: str = ""


class EventDoc(VariantDoc):
    timeline: bool = False
    # First and last day, timeline events only.
    when: tuple[int, int] | None = None
    pacing: PacingChoice = "quiet"
    lead_in: bool = False
    aftermath: str = ""
    # None: the format's default (yes, unless the event has its own requires).
    must_happen: bool | None = None
    variants: list[VariantDoc] = Field(default_factory=list)


class PlotDocument(BaseModel):
    title: str = ""
    description: str = ""
    start_day: int = 1
    start_time: str = "00:00"
    agency: AgencyMode | None = None
    world_activity: WorldActivity | None = None
    perspective: Perspective | None = None
    length: FileLength | None = None
    # The author's own notes, written as a comment the app never reads.
    notes: str = ""
    world: str = ""
    opening: OpeningDoc = Field(default_factory=OpeningDoc)
    characters: list[CharacterDoc] = Field(default_factory=list)
    places: list[EntryDoc] = Field(default_factory=list)
    lore: list[EntryDoc] = Field(default_factory=list)
    facts: list[FactDoc] = Field(default_factory=list)
    # Timeline and optional events together, each saying which it is, so the
    # author can move one between the two without it changing lists.
    events: list[EventDoc] = Field(default_factory=list)

    # --- renaming: every reference follows -------------------------------------

    def _holders(self) -> list[VariantDoc]:
        holders: list[VariantDoc] = []
        for event in self.events:
            holders.append(event)
            holders.extend(event.variants)
        return holders

    def rename_fact(self, old: str, new: str) -> None:
        for fact in self.facts:
            if fact.name == old:
                fact.name = new
        for holder in self._holders():
            for condition in holder.requires:
                if condition.kind == "fact" and condition.fact == old:
                    condition.fact = new
            for assignment in holder.sets:
                if assignment.fact == old:
                    assignment.fact = new

    def rename_place(self, old: str, new: str) -> None:
        for entry in (*self.places, *self.lore):
            if entry.name == old:
                entry.name = new
        for holder in self._holders():
            if holder.at == old:
                holder.at = new
            holder.reveals = [new if name == old else name for name in holder.reveals]
            for condition in holder.requires:
                if condition.kind in ("at", "visited") and condition.place == old:
                    condition.place = new

    def rename_event(self, old: str, new: str) -> None:
        for event in self.events:
            if event.title == old:
                event.title = new
        for holder in self._holders():
            for condition in holder.requires:
                if condition.kind == "happened" and condition.event == old:
                    condition.event = new

    def rename_character(self, old: str, new: str) -> None:
        for character in self.characters:
            if character.name == old:
                character.name = new
        for holder in self._holders():
            holder.brings = [new if name == old else name for name in holder.brings]
            for condition in holder.requires:
                if condition.kind in ("at", "visited") and condition.who == old:
                    condition.who = new
