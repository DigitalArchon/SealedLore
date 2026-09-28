"""Plot files as documents: written out, read back, and checked.

`render_plot_markdown` writes a `PlotDocument` (models/plot_file.py) as the
Markdown that `parse_plot_markdown` reads, and remembers which item each line
came from. `read_plot_document` loads a file into a document, keeping what
the author wrote (a condition it can't read stays as written) and saying what
it had to leave out. `check_plot_document` is the editor's only judge: it
renders, runs the importer, and hands each problem back against the item it
belongs to. Nothing here decides what is valid; the importer does.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from sealedlore.engine.plot_md import (
    _CHARACTER_FIELDS,
    _COMMENT,
    _EVENT_FIELDS,
    _FACT_NAME,
    _FIELD,
    _OPENING_FIELDS,
    _PLACE_FIELDS,
    _ROLES,
    _SECTIONS,
    _VARIANT_FIELDS,
    PlotProblem,
    _Block,
    _Fields,
    _key,
    _norm,
    _Parser,
    _prose,
    _slug,
    fact_parts,
    parse_plot_markdown,
    picture_parts,
    read_clause,
    split_clauses,
)
from sealedlore.models.plot_file import (
    AssignmentDoc,
    CharacterDoc,
    ConditionDoc,
    EntryDoc,
    EventDoc,
    FactDoc,
    PictureDoc,
    PlotDocument,
    VariantDoc,
)

# Where a line of the rendered file came from: a section, the item's index
# in it, and for an event the variant's index (-1 for the event itself).
Section = str
ItemRef = tuple[Section, int, int]

FRONT = "story"
WORLD = "world"
OPENING = "opening"
CHARACTER = "character"
PLACE = "place"
LORE = "lore"
FACT = "fact"
EVENT = "event"
NOTES = "notes"

UNNAMED = "Unnamed"
_FRONT_KEYS = ("title", "description", "start", "agency", "world_activity", "perspective", "length")

# What can't be in a name that a condition or a list will name: the
# separators those are split on.
_LIST_BREAKERS = re.compile(r"[,;]|\band\b", re.IGNORECASE)
_FACT_BREAKERS = re.compile(r"[(),|\[\]]|\s+(?:—|–|--|-)\s+|—|–")


@dataclass(frozen=True)
class DocProblem:
    """One thing the importer (or the document itself) has to say.

    `where` is None for the file as a whole.
    """

    where: ItemRef | None
    message: str
    error: bool = False


@dataclass
class RenderedPlot:
    text: str
    # One entry per line, 0-based.
    owners: list[ItemRef | None] = field(default_factory=list)

    def owner(self, line: int) -> ItemRef | None:
        """The item that wrote 1-based line `line`."""
        if 1 <= line <= len(self.owners):
            return self.owners[line - 1]
        return None


# --- writing ------------------------------------------------------------------


class _Writer:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.owners: list[ItemRef | None] = []
        self.current: ItemRef | None = None

    def line(self, text: str = "") -> None:
        self.lines.append(text.rstrip())
        self.owners.append(self.current)

    def blank(self) -> None:
        if self.lines and self.lines[-1] != "":
            self.line()

    def heading(self, level: int, title: str) -> None:
        self.blank()
        self.line(f"{'#' * level} {title.strip() or UNNAMED}")

    def field(self, key: str, value: str | None) -> None:
        """`- key: value`; further lines of the value are indented to continue it.

        Blank lines inside a value would end the run of settings, so they go.
        """
        if not value:
            return
        parts = [part.strip() for part in value.split("\n") if part.strip()]
        if not parts:
            return
        self.line(f"- {key}: {parts[0]}")
        for part in parts[1:]:
            self.line(f"  {part}")

    def flag(self, key: str, value: bool | None, *, only_when: bool | None = True) -> None:
        if value is None or value is not only_when:
            return
        self.field(key, "yes" if value else "no")

    def pictures(self, pictures: Sequence[PictureDoc]) -> None:
        """A line for each picture, in order: `- picture: file | caption`."""
        for picture in pictures:
            file = picture.file.strip()
            if not file:
                continue
            # One line: a caption's own line break would start a new setting.
            caption = " ".join(picture.caption.replace("|", "/").split())
            self.field("picture", f"{file} | {caption}" if caption else file)

    def prose(self, text: str) -> None:
        if not text.strip():
            return
        self.blank()
        for part in text.strip().split("\n"):
            self.line(part)

    def text(self) -> str:
        return "\n".join(self.lines).rstrip("\n") + "\n"


def render_condition(condition: ConditionDoc) -> str:
    who = f"{condition.who.strip()} " if condition.who.strip() else ""
    if condition.kind == "at":
        return f"{who}at {'!=' if condition.negated else '='} {condition.place.strip()}"
    if condition.kind == "visited":
        return f"{who}{'not ' if condition.negated else ''}visited {condition.place.strip()}"
    if condition.kind == "happened":
        return f"{condition.event.strip()} {'not happened' if condition.negated else 'happened'}"
    if condition.kind == "text":
        return condition.text.strip()
    return (
        f"{condition.fact.strip()} {'!=' if condition.negated else '='} {condition.value.strip()}"
    )


def render_fact(fact: FactDoc) -> str:
    head = fact.initial.strip()
    if fact.values:
        head += f" ({', '.join(value.strip() for value in fact.values)})"
    if fact.watched:
        head += " [watch]"
    if fact.meaning.strip():
        head += f" — {fact.meaning.strip()}"
    return f"- {fact.name.strip()}: {head}"


def _render_way(writer: _Writer, way: VariantDoc, *, event_offscreen: bool | None) -> None:
    """The lines an event and a variant share."""
    writer.field("at", way.at)
    writer.field("requires", ", ".join(render_condition(c) for c in way.requires))
    writer.field("trigger", way.trigger)
    writer.field("brings", ", ".join(name.strip() for name in way.brings if name.strip()))
    writer.field("reveals", ", ".join(name.strip() for name in way.reveals if name.strip()))
    writer.field(
        "sets",
        ", ".join(f"{a.fact.strip()} = {a.value.strip()}" for a in way.sets if a.fact.strip()),
    )
    # A variant says "no" only to undo the event's "yes".
    if way.offscreen or (way.offscreen is False and event_offscreen):
        writer.flag("offscreen", way.offscreen, only_when=way.offscreen)


def render_plot_markdown(document: PlotDocument) -> RenderedPlot:
    writer = _Writer()
    writer.current = (FRONT, 0, -1)
    front: list[tuple[str, str]] = [
        ("title", document.title.strip()),
        ("description", document.description.strip()),
    ]
    if document.facts or document.events:
        front.append(("start", f"day {document.start_day}, {document.start_time.strip()}"))
    front.extend(
        (key, value.replace("_", " "))
        for key, value in (
            ("agency", document.agency),
            ("world activity", document.world_activity),
            ("perspective", document.perspective),
            ("length", document.length),
        )
        if value
    )
    if any(value for _, value in front):
        writer.line("---")
        for key, value in front:
            if value:
                writer.line(f"{key}: {value}")
        writer.line("---")

    if document.notes.strip():
        writer.current = (NOTES, 0, -1)
        writer.blank()
        writer.line("<!--")
        for part in document.notes.strip().split("\n"):
            writer.line(part.replace("-->", "- ->"))
        writer.line("-->")

    if document.world.strip():
        writer.current = (WORLD, 0, -1)
        writer.heading(1, "World")
        writer.prose(document.world)

    opening = document.opening
    if any((opening.location, opening.time, opening.situation, opening.notes)) or (
        opening.mode == "as_written"
    ):
        writer.current = (OPENING, 0, -1)
        writer.heading(1, "Opening")
        if opening.mode == "as_written":
            writer.field("mode", "as written")
        writer.field("location", opening.location)
        writer.field("time", opening.time)
        writer.field("situation", opening.situation)
        writer.prose(opening.notes)

    if document.characters:
        writer.current = (CHARACTER, 0, -1)
        writer.heading(1, "Characters")
        for index, character in enumerate(document.characters):
            writer.current = (CHARACTER, index, -1)
            writer.heading(2, character.name)
            writer.field("role", character.role)
            if character.choose:
                writer.field("choose", "start")
            writer.flag("present", character.present)
            writer.field("aliases", ", ".join(a.strip() for a in character.aliases if a.strip()))
            writer.field("voice", character.voice)
            writer.field("canon", character.canon)
            writer.field("summary", character.summary)
            writer.flag("hidden", character.hidden)
            writer.pictures(character.pictures)
            writer.prose(character.description)

    for section, title, entries in (
        (PLACE, "Places", document.places),
        (LORE, "Lore", document.lore),
    ):
        if not entries:
            continue
        writer.current = (section, 0, -1)
        writer.heading(1, title)
        for index, entry in enumerate(entries):
            writer.current = (section, index, -1)
            writer.heading(2, entry.name)
            writer.field("keywords", ", ".join(k.strip() for k in entry.keywords if k.strip()))
            writer.flag("always", entry.always)
            writer.flag("hidden", entry.hidden)
            writer.pictures(entry.pictures)
            writer.prose(entry.description)

    if document.facts:
        writer.current = (FACT, 0, -1)
        writer.heading(1, "Facts")
        writer.blank()
        for index, fact in enumerate(document.facts):
            writer.current = (FACT, index, -1)
            writer.line(render_fact(fact))

    for timeline, title in ((True, "Timeline"), (False, "Events")):
        events = [(i, e) for i, e in enumerate(document.events) if e.timeline == timeline]
        if not events:
            continue
        writer.current = (EVENT, events[0][0], -1)
        writer.heading(1, title)
        for index, event in events:
            writer.current = (EVENT, index, -1)
            writer.heading(2, event.title)
            # An optional event's days count too (`chronicle.opens_on`, and
            # a closed window skips it when its conditions no longer hold).
            if event.when is not None:
                first, last = event.when
                writer.field("when", f"day {first}" if first == last else f"day {first}–{last}")
            if event.pacing == "any":
                writer.field("pacing", "any time")
            writer.flag("lead-in", event.lead_in)
            if event.timeline:
                writer.flag("must happen", event.must_happen, only_when=event.must_happen)
            _render_way(writer, event, event_offscreen=None)
            writer.field("aftermath", event.aftermath)
            writer.prose(event.tell)
            for variant_index, variant in enumerate(event.variants):
                writer.current = (EVENT, index, variant_index)
                writer.heading(3, variant.title)
                _render_way(writer, variant, event_offscreen=event.offscreen)
                writer.prose(variant.tell)
    return RenderedPlot(writer.text(), writer.owners)


# --- reading ------------------------------------------------------------------


@dataclass
class ReadPlotDocument:
    document: PlotDocument
    # What the reader could not keep, or had to change to read: the author
    # should see these before the file is written back without them.
    problems: list[PlotProblem] = field(default_factory=list)


def _names(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


class _Reader:
    def __init__(self, text: str) -> None:
        self.parser = _Parser(text, fallback_title="")
        self.notes = "\n\n".join(
            match.group(0)[4:-3].strip() for match in _COMMENT.finditer(text.replace("\r\n", "\n"))
        )
        self.document = PlotDocument(notes=self.notes)
        self.lines = self.parser.text.split("\n")

    def problem(self, line: int, message: str) -> None:
        self.parser.note(line, message)

    def _flag(self, values: dict[str, tuple[int, str]], key: str, what: str) -> bool | None:
        if key not in values:
            return None
        return self.parser.flag(*values[key], what)

    def _pictures(self, found: _Fields, owner: str) -> list[PictureDoc]:
        """Kept as written, a bad path too: the importer's check says what is
        wrong with it, against the item it belongs to. A line naming no file
        has nothing to keep."""
        pictures = []
        for line, value in found.pictures:
            file, caption = picture_parts(value)
            if file:
                pictures.append(PictureDoc(file=file, caption=caption))
            else:
                self.problem(line, f"“{owner}” has a picture line with no file; left out.")
        return pictures

    def _character(self, block: _Block) -> CharacterDoc:
        found = self.parser.body(block, _CHARACTER_FIELDS)
        values = found.values
        role = _norm(values.get("role", (0, "storyteller"))[1]) or "storyteller"
        if role not in _ROLES:
            self.problem(
                values["role"][0], f"“{block.title}” has role “{role}”; set to storyteller."
            )
            role = "storyteller"
        choose = False
        if "choose" in values:
            line, value = values["choose"]
            choose = _norm(value) in ("start", "at start", "one", "yes")
            if not choose:
                self.problem(line, f"`choose: {value}` isn't a choice the format has; left out.")
        return CharacterDoc(
            name=block.title,
            role=role,  # type: ignore[arg-type]
            choose=choose,
            present=bool(self._flag(values, "present", "present")),
            aliases=_names(values.get("aliases", (0, ""))[1]),
            voice=values.get("voice", (0, ""))[1],
            summary=values.get("summary", (0, ""))[1],
            canon=values.get("canon", (0, ""))[1],
            hidden=bool(self._flag(values, "hidden", "hidden")),
            pictures=self._pictures(found, block.title),
            description=found.prose,
        )

    def _entry(self, block: _Block) -> EntryDoc:
        found = self.parser.body(block, _PLACE_FIELDS)
        values = found.values
        return EntryDoc(
            name=block.title,
            keywords=_names(values.get("keywords", (0, ""))[1]),
            always=bool(self._flag(values, "always", "always")),
            hidden=bool(self._flag(values, "hidden", "hidden")),
            pictures=self._pictures(found, block.title),
            description=found.prose,
        )

    def _facts(self, block: _Block) -> list[FactDoc]:
        facts: list[FactDoc] = []
        for number, line in block.lines:
            match = _FIELD.match(line)
            if not match:
                if line.strip():
                    self.problem(number, f"Not a fact line, so left out: {line.strip()}")
                continue
            initial, values, watched, meaning = fact_parts(match.group(2))
            facts.append(
                FactDoc(
                    name=_key(match.group(1)),
                    initial=initial,
                    values=values,
                    watched=watched,
                    meaning=meaning,
                )
            )
        return facts

    def _condition(self, clause: str) -> ConditionDoc:
        document = self.document
        names = [c.name for c in document.characters if c.name.strip()]
        names += [alias for c in document.characters for alias in c.aliases]
        found = read_clause(clause, names)
        if found is None:
            return ConditionDoc(kind="text", text=clause)
        if found.kind in ("at", "visited"):
            place = next(
                (p.name for p in document.places if _norm(p.name) == _norm(found.reference)),
                found.reference,
            )
            who = next(
                (
                    c.name
                    for c in document.characters
                    if found.who and _norm(found.who) in (_norm(c.name), *map(_norm, c.aliases))
                ),
                found.who,
            )
            return ConditionDoc(kind=found.kind, negated=found.negated, place=place, who=who)
        if found.kind == "happened":
            title = next(
                (
                    e.title
                    for e in document.events
                    if _norm(e.title) == _norm(found.reference)
                    or _slug(e.title) == _slug(found.reference)
                ),
                found.reference,
            )
            return ConditionDoc(kind="happened", negated=found.negated, event=title)
        return ConditionDoc(
            kind="fact", negated=found.negated, fact=found.reference, value=found.value
        )

    def _assignments(self, line: int, text: str) -> list[AssignmentDoc]:
        assignments: list[AssignmentDoc] = []
        for clause in re.split(r",|;", text):
            clause = clause.strip()
            if not clause:
                continue
            name, equals, value = clause.partition("=")
            if not equals:
                self.problem(line, f"Can't read “{clause}” as `fact = value`; left out.")
                continue
            assignments.append(AssignmentDoc(fact=_key(name), value=_norm(value)))
        return assignments

    def _way(self, way: VariantDoc, block: _Block, known: set[str]) -> None:
        found = self.parser.fields(block, known)
        values = found.values
        if "requires" in values:
            way.requires = [self._condition(c) for c in split_clauses(values["requires"][1])]
        way.trigger = values.get("trigger", (0, ""))[1]
        way.tell = "\n\n".join(
            part for part in (values.get("tell", (0, ""))[1], found.prose) if part
        )
        if "sets" in values:
            way.sets = self._assignments(*values["sets"])
        way.offscreen = self._flag(values, "offscreen", "offscreen")
        way.brings = _names(values.get("brings", (0, ""))[1])
        way.reveals = _names(values.get("reveals", (0, ""))[1])
        way.at = values.get("at", (0, ""))[1]
        for child in block.children:
            if child.level > 3 or known is _VARIANT_FIELDS:
                self.problem(
                    child.line,
                    f"“{child.title}” is a heading under “{block.title}”; the format has no "
                    "place for it, so it was left out.",
                )

    def _event(self, block: _Block, *, timeline: bool) -> EventDoc:
        event = EventDoc(title=block.title, timeline=timeline)
        values = self.parser.fields(block, _EVENT_FIELDS).values
        if "when" in values:
            event.when = self.parser.window(*values["when"])
        if "pacing" in values:
            event.pacing = self.parser.pacing(*values["pacing"])
        event.lead_in = bool(self._flag(values, "lead_in", "lead-in"))
        event.must_happen = self._flag(values, "must_happen", "must happen")
        event.aftermath = values.get("aftermath", (0, ""))[1]
        self._way(event, block, _EVENT_FIELDS)
        for child in block.children:
            if child.level == 3:
                variant = VariantDoc(title=child.title)
                self._way(variant, child, _VARIANT_FIELDS)
                event.variants.append(variant)
        return event

    def read(self) -> ReadPlotDocument:
        parser, document = self.parser, self.document
        front, body_start = parser.front_matter(self.lines)
        document.title = front.get("title", (0, ""))[1]
        document.description = front.get("description", (0, ""))[1]
        if "start" in front:
            minutes = parser.clock(*front["start"])
            if minutes is not None:
                document.start_day = minutes // (24 * 60) + 1
                document.start_time = f"{minutes % (24 * 60) // 60:02d}:{minutes % 60:02d}"
        for key, choices in (
            ("agency", ("fiat", "plausible", "contested", "dice")),
            ("world_activity", ("quiet", "normal", "eventful")),
            ("perspective", ("whole_story", "with_character")),
            ("length", ("adaptive", "brief", "standard", "descriptive", "literary")),
        ):
            if key in front:
                line, value = front[key]
                if _key(value) in choices:
                    setattr(document, key, _key(value))
                else:
                    self.problem(line, f"`{key}: {value}` isn't a choice the format has; left out.")
        for key, (line, value) in front.items():
            if key not in _FRONT_KEYS:
                self.problem(line, f"`{key}: {value}` isn't a setting the format has; left out.")

        sections: dict[str, _Block] = {}
        for block in parser.blocks(self.lines, body_start):
            name = _SECTIONS.get(_norm(block.title))
            if name is None or name in sections:
                self.problem(
                    block.line,
                    f"“# {block.title}” "
                    + ("is a second such section" if name else "isn't a section the format has")
                    + "; it was left out, with everything under it.",
                )
                continue
            sections[name] = block

        if "world" in sections:
            document.world = sections["world"].text()
        if "opening" in sections:
            found = parser.body(sections["opening"], _OPENING_FIELDS)
            opening = document.opening
            mode = _norm(found.values.get("mode", (0, "generate"))[1])
            if mode in ("as written", "as_written", "written"):
                opening.mode = "as_written"
            elif mode not in ("generate", "generated"):
                self.problem(found.values["mode"][0], f"`mode: {mode}` was read as generate.")
            opening.location = found.values.get("location", (0, ""))[1]
            opening.time = found.values.get("time", (0, ""))[1]
            opening.situation = found.values.get("situation", (0, ""))[1]
            opening.notes = found.prose
        for name in ("characters", "places", "lore", "facts", "timeline", "events"):
            block = sections.get(name)
            if block is None:
                continue
            if name != "facts" and block.lines and _prose(block.lines):
                self.problem(
                    block.line, f"Text directly under “# {block.title}” has no place; left out."
                )
            if name == "characters":
                document.characters = [self._character(child) for child in block.children]
            elif name == "places":
                document.places = [self._entry(child) for child in block.children]
            elif name == "lore":
                document.lore = [self._entry(child) for child in block.children]
            elif name == "facts":
                document.facts = self._facts(block)
                for child in block.children:
                    self.problem(child.line, f"“{child.title}” under Facts was left out.")
        # Events last: their conditions name everything else.
        for name, timeline in (("timeline", True), ("events", False)):
            block = sections.get(name)
            if block is not None:
                # Titles first, so a condition can name a later event.
                document.events.extend(
                    EventDoc(title=c.title, timeline=timeline) for c in block.children
                )
        index = 0
        for name, timeline in (("timeline", True), ("events", False)):
            block = sections.get(name)
            if block is None:
                continue
            for child in block.children:
                document.events[index] = self._event(child, timeline=timeline)
                index += 1
        problems = sorted(parser.problems, key=lambda p: p.line)
        return ReadPlotDocument(document, problems)


def read_plot_document(text: str) -> ReadPlotDocument:
    """Load a plot file for editing. Every problem is something the document
    doesn't carry, so the author should know before it is written back."""
    return _Reader(text).read()


# --- checking -----------------------------------------------------------------


def _breakers(text: str, pattern: re.Pattern[str]) -> bool:
    return bool(pattern.search(text))


def _cut_names(way: VariantDoc) -> list[str]:
    """Names this event uses that its own line would cut in two.

    Lists are split on commas, conditions on commas and “and”, so a name
    with either in it can't be named there whatever the importer says.
    """
    used = [*way.brings, *way.reveals, way.at]
    for condition in way.requires:
        if condition.kind in ("at", "visited"):
            used += [condition.place, condition.who]
        elif condition.kind == "happened":
            used.append(condition.event)
        elif condition.kind == "fact":
            used.append(condition.value)
    used += [assignment.value for assignment in way.sets]
    return [name for name in used if name.strip() and _breakers(name, _LIST_BREAKERS)]


def _document_problems(document: PlotDocument) -> list[DocProblem]:
    """What the importer can't see because the renderer papers over it: blank
    names, and names that the format's own separators would cut in two."""
    problems: list[DocProblem] = []

    def add(where: ItemRef | None, message: str, *, error: bool = True) -> None:
        problems.append(DocProblem(where, message, error))

    if not document.title.strip():
        add(None, "The story has no title; the file's name will be used.", error=False)
    if not re.fullmatch(r"\d{1,2}:\d{2}", document.start_time.strip()):
        add((FRONT, 0, -1), f"The start time “{document.start_time}” isn't HH:MM.")
    for index, character in enumerate(document.characters):
        if not character.name.strip():
            add((CHARACTER, index, -1), "This character needs a name.")
    for section, entries in ((PLACE, document.places), (LORE, document.lore)):
        for index, entry in enumerate(entries):
            if not entry.name.strip():
                add((section, index, -1), "This entry needs a name.")
    for index, fact in enumerate(document.facts):
        where = (FACT, index, -1)
        if not fact.name.strip():
            add(where, "This fact needs a name.")
        elif not _FACT_NAME.match(fact.name):
            add(where, "A fact's name is lower-case letters, digits and _, starting with a letter.")
        for value in (fact.initial, *fact.values):
            if _breakers(value, _FACT_BREAKERS) or _breakers(value, _LIST_BREAKERS):
                add(where, f"“{value}” can't be a value: no brackets, dashes, commas or “and”.")
                break
    for index, event in enumerate(document.events):
        where = (EVENT, index, -1)
        if not event.title.strip():
            add(where, "This event needs a title.")
        if event.timeline and event.when is None:
            add(where, "A timeline event needs its days.")
        for variant_index, variant in enumerate((event, *event.variants)):
            where = (EVENT, index, variant_index - 1)
            if variant_index and not variant.title.strip():
                add(where, "This variant needs a title.")
            for name in _cut_names(variant):
                add(
                    where,
                    f"“{name}” can't be named here: the comma, semicolon or “and” in it "
                    "cuts the name in two. Rename it, or use an alias.",
                )
    return problems


def check_plot_document(document: PlotDocument) -> tuple[RenderedPlot, list[DocProblem]]:
    """Render the document and run the importer over it.

    The problems are the importer's, each against the item whose lines it
    points at, after the few the document has to raise itself.
    """
    rendered = render_plot_markdown(document)
    problems = _document_problems(document)
    parsed = parse_plot_markdown(rendered.text, fallback_title=document.title or "Untitled")
    for problem in parsed.problems:
        problems.append(DocProblem(rendered.owner(problem.line), problem.message, problem.error))
    errors = [p for p in problems if p.error]
    notes = [p for p in problems if not p.error]
    return rendered, errors + notes


def describe_where(document: PlotDocument, where: ItemRef | None) -> str:
    """“Event “The fall”, variant “Away””, for a problems list."""
    if where is None:
        return "File"
    section, index, variant = where
    if section == FRONT:
        return "Story"
    if section == NOTES:
        return "Notes"
    if section == WORLD:
        return "World"
    if section == OPENING:
        return "Opening"
    items: list[str]
    if section == CHARACTER:
        items = [c.name for c in document.characters]
        label = "Character"
    elif section == PLACE:
        items = [p.name for p in document.places]
        label = "Place"
    elif section == LORE:
        items = [entry.name for entry in document.lore]
        label = "Lore"
    elif section == FACT:
        items = [f.name for f in document.facts]
        label = "Fact"
    else:
        items = [e.title for e in document.events]
        label = "Event"
    name = items[index] if 0 <= index < len(items) else ""
    text = f"{label} “{name or UNNAMED}”"
    if section == EVENT and variant >= 0:
        variants = document.events[index].variants if 0 <= index < len(document.events) else []
        variant_name = variants[variant].title if 0 <= variant < len(variants) else ""
        text += f", variant “{variant_name or UNNAMED}”"
    return text


__all__ = [
    "CHARACTER",
    "EVENT",
    "FACT",
    "FRONT",
    "LORE",
    "NOTES",
    "OPENING",
    "PLACE",
    "WORLD",
    "DocProblem",
    "ItemRef",
    "ReadPlotDocument",
    "RenderedPlot",
    "check_plot_document",
    "describe_where",
    "read_plot_document",
    "render_condition",
    "render_fact",
    "render_plot_markdown",
]
