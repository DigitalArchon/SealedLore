"""Plot files: a story, its facts and its events, written as Markdown.

The format is in SampleStories/PLOT_FORMAT.md. It is meant to stay readable
as a document: a few fixed headings and `- key: value` lines carry the
structure, and everything else is prose. Pure: text in, a `Scenario` and a
list of problems out. Nothing is guessed — anything that can't be read is a
problem with its line number, and an error stops the import.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, get_args

from sealedlore.engine.validators import name_forms
from sealedlore.ids import stable_id
from sealedlore.models.character import Character
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import AgencyMode, ResponseStyle
from sealedlore.models.plot import (
    MINUTES_PER_DAY,
    Assignment,
    Condition,
    ConditionOp,
    EventDef,
    EventKind,
    EventVariant,
    FactDef,
    Pacing,
    PlaceDef,
    Plot,
)
from sealedlore.models.scenario import Scenario
from sealedlore.models.scene import SceneState
from sealedlore.models.story import Perspective, StyleDirectives, WorldActivity
from sealedlore.models.world import PlaceKind, WorldDelta, WorldLink, WorldPlace, WorldThing

PLOT_SUFFIX = ".md"

YES = "yes"
NO = "no"

# Top-level sections, by every name an author is likely to give them.
_SECTIONS = {
    "world": "world",
    "world info": "world",
    "setting": "world",
    "opening": "opening",
    "start": "opening",
    "characters": "characters",
    "cast": "characters",
    "places": "places",
    "lore": "lore",
    "facts": "facts",
    "timeline": "timeline",
    "events": "events",
    "optional events": "events",
    "map": "map",
}

_OPENING_FIELDS = {"mode", "location", "time", "situation"}
_CHARACTER_FIELDS = {
    "role",
    "choose",
    "present",
    "aliases",
    "voice",
    "canon",
    "summary",
    "hidden",
    "picture",
}
_PLACE_FIELDS = {"keywords", "always", "hidden", "picture"}
# `# Map`: its own settings, and each place's.
_MAP_FIELDS = {"unit", "start"}
_MAP_PLACE_FIELDS = {"kind", "at", "size", "in", "known", "way", "thing"}
# The settings that may be given more than once: a line per picture, and on
# a map's place a line per way out and per thing there.
PICTURE = "picture"
WAY = "way"
THING = "thing"
REPEATED = frozenset({PICTURE, WAY, THING})
# What a picture line may name. The app reads these and no others.
PICTURE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
_EVENT_FIELDS = {
    "when",
    "pacing",
    "lead_in",
    "aftermath",
    "requires",
    "trigger",
    "tell",
    "sets",
    "offscreen",
    "brings",
    "reveals",
    "must_happen",
    "at",
}
_VARIANT_FIELDS = {
    "requires",
    "trigger",
    "tell",
    "sets",
    "offscreen",
    "brings",
    "reveals",
    "at",
}
_ALL_FIELDS = (
    _OPENING_FIELDS | _CHARACTER_FIELDS | _PLACE_FIELDS | _EVENT_FIELDS | _MAP_PLACE_FIELDS
)

_ROLES = {"player", "storyteller", "supporting", "author"}

_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
_FIELD = re.compile(r"^\s*[-*]\s+([A-Za-z][A-Za-z _-]*?)\s*:\s*(.*)$")
_FACT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_MEANING_SEPARATOR = re.compile(r"\s+(?:—|–|--|-)\s+|—|–")
_WATCH = re.compile(r"\s*\[watch(?:ed)?\]\s*", re.IGNORECASE)


@dataclass(frozen=True)
class PlotProblem:
    line: int
    message: str
    error: bool = False

    def describe(self) -> str:
        kind = "Error" if self.error else "Note"
        where = f"line {self.line}" if self.line else "file"
        return f"{kind}, {where}: {self.message}"


@dataclass(frozen=True)
class PlotPicture:
    """A `- picture:` line: a file beside the plot file, for a character's
    card or a place's or lore entry's. The parser reads no files; whoever
    imports the plot does (gui/ref_images.load_plot_pictures)."""

    owner_kind: str  # "character", "lore", or "background" (the front matter's)
    owner_id: str
    owner_name: str
    # As written, relative to the plot file's folder, with forward slashes.
    source: str
    caption: str
    line: int


def picture_parts(value: str) -> tuple[str, str]:
    """`pictures/john.png | front view` as (path, caption). Shared by the
    importer and the editor's reader, so the two read a line the same way."""
    source, _, caption = value.partition("|")
    return source.strip().replace("\\", "/"), caption.strip()


def picture_problem(source: str) -> str | None:
    """Why a picture's path can't be used, or None. A plot file may come from
    someone else: a path must stay inside the folder the file is in."""
    parts = source.split("/")
    if not source:
        return "`picture` needs a file: `- picture: pictures/name.png | what it shows`."
    if source.startswith(("/", "~")) or ":" in source or ".." in parts or "" in parts:
        return (
            f"The picture “{source}” must be a file in the plot file's own folder or one "
            "inside it, written from there (pictures/name.png)."
        )
    if not source.lower().endswith(PICTURE_SUFFIXES):
        return f"“{source}” isn't a picture the app reads ({', '.join(PICTURE_SUFFIXES)})."
    return None


@dataclass
class ParsedPlotFile:
    scenario: Scenario
    problems: list[PlotProblem] = field(default_factory=list)
    pictures: list[PlotPicture] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(problem.error for problem in self.problems)


@dataclass
class _Block:
    level: int
    title: str
    line: int
    # (line number, text) for the lines directly under the heading, before
    # any sub-heading.
    lines: list[tuple[int, str]] = field(default_factory=list)
    children: list[_Block] = field(default_factory=list)

    def text(self) -> str:
        """Everything under the heading, sub-headings included, as written."""
        parts = [text for _, text in self.lines]
        for child in self.children:
            parts.append(f"{'#' * child.level} {child.title}")
            parts.append(child.text())
        return _prose([(0, part) for part in parts])


@dataclass
class _Fields:
    values: dict[str, tuple[int, str]]
    prose: str
    # Every `- picture:` line, in order: (line number, value).
    pictures: list[tuple[int, str]] = field(default_factory=list)
    # Every line of each setting that may be given more than once (`REPEATED`).
    repeated: dict[str, list[tuple[int, str]]] = field(default_factory=dict)


def _norm(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _key(raw: str) -> str:
    return re.sub(r"[\s-]+", "_", raw.strip().lower())


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_") or "event"


# A line that starts its own line in Markdown: a list item, a heading, a
# quote, a table row or a fence.
_OWN_LINE = re.compile(r"\s*(?:[-*+]\s|\d+[.)]\s|#|>|\||```|~~~)")


def _prose(lines: list[tuple[int, str]]) -> str:
    """The prose as Markdown reads it: a line wrapped in the file joins the
    line before it, and a blank line starts a paragraph. Kept as wrapped,
    the samples' cards showed their file's line breaks in the Cast page's
    fields (Oct 18 2026). A list item, a heading or a quote keeps its line,
    and so does a line after one ending in two spaces (a hard break)."""
    out: list[str] = []
    broken = False  # the line before ended in a hard break
    for _, raw in lines:
        line = raw.rstrip()
        if (
            line
            and out
            and out[-1]
            and not broken
            and not _OWN_LINE.match(line)
            and not out[-1].lstrip().startswith("#")
        ):
            out[-1] = f"{out[-1]} {line.strip()}"
        else:
            out.append(line)
        broken = raw.endswith("  ")
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fact_parts(rest: str) -> tuple[str, list[str], bool, str]:
    """The pieces of a fact line after its name: `initial (values) [watch] — meaning`."""
    watched = bool(_WATCH.search(rest))
    rest = _WATCH.sub(" ", rest).strip()
    parts = _MEANING_SEPARATOR.split(rest, maxsplit=1)
    head, meaning = parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")
    values: list[str] = []
    listed = re.search(r"\(([^)]*)\)\s*$", head)
    if listed:
        values = [_norm(value) for value in re.split(r"[|,]", listed.group(1))]
        values = [value for value in values if value]
        head = head[: listed.start()].strip()
    return _norm(head), values, watched, meaning


@dataclass(frozen=True)
class Clause:
    """One condition as written, before anything is looked up.

    `reference` is the place, the event or the fact it names; `who` the
    character whose place is meant, when one is named.
    """

    kind: Literal["fact", "at", "visited", "happened"]
    negated: bool = False
    reference: str = ""
    value: str = ""
    who: str = ""


_WHO_SUFFIX = re.compile(
    r"\s+(?:(?:is\s+)?(?:not\s+|never\s+)?(?:at|has\s+visited|visited))\b", re.IGNORECASE
)
_AT = re.compile(r"at\s*(!=|=|\bis not\b|\bisn't\b|\bis\b)\s*(.+)", re.IGNORECASE)
# A fact that happens to be called `visited` ("visited = yes") is a fact.
_BARE_AT = re.compile(r"(not\s+)?at\s+(?![=!]|is\b)(.+)", re.IGNORECASE)
_VISITED = re.compile(r"(not\s+|never\s+)?(?:has\s+)?visited\s+(?![=!]|is\b)(.+)", re.IGNORECASE)
_HAPPENED = re.compile(
    r"(.+?)\s+(has not happened|hasn['’]t happened|not happened|has happened|happened)",
    re.IGNORECASE,
)
_COMPARISON = re.compile(r"([A-Za-z][\w ]*?)\s*(!=|=|\bis not\b|\bisn't\b|\bis\b)\s*(.+)")
_NEGATED_FLAG = re.compile(r"not\s+([A-Za-z][\w]*)", re.IGNORECASE)
_FLAG = re.compile(r"[A-Za-z][\w]*")


def split_clauses(text: str) -> list[str]:
    """The conditions in a `requires:` line, as written."""
    return [
        clause.strip()
        for clause in re.split(r",|\band\b|;", text, flags=re.IGNORECASE)
        if clause.strip()
    ]


def read_clause(clause: str, names: list[str]) -> Clause | None:
    """Read one condition. `names` are the characters a place test can be about,
    as they may be written (a name or an alias): `John at = The Beacon`."""
    who = ""
    for name in sorted(names, key=len, reverse=True):
        rest = clause[len(name) :]
        if _norm(clause[: len(name)]) == _norm(name) and _WHO_SUFFIX.match(rest):
            who, clause = name, re.sub(r"^\s+(?:is\s+)?", "", rest)
            break
    if at := _AT.fullmatch(clause):
        negated = at.group(1).strip().lower() not in ("=", "is")
        return Clause("at", negated, at.group(2).strip(), who=who)
    if bare_at := _BARE_AT.fullmatch(clause):
        return Clause("at", bool(bare_at.group(1)), bare_at.group(2).strip(), who=who)
    if visited := _VISITED.fullmatch(clause):
        return Clause("visited", bool(visited.group(1)), visited.group(2).strip(), who=who)
    if who:
        return None
    if happened := _HAPPENED.fullmatch(clause):
        # "hasn't happened" has no "not" in it.
        negated = bool(re.search(r"not|n['’]t", happened.group(2), re.IGNORECASE))
        return Clause("happened", negated, happened.group(1).strip())
    if match := _COMPARISON.fullmatch(clause):
        negated = match.group(2).strip() not in ("=", "is")
        return Clause("fact", negated, _key(match.group(1)), _norm(match.group(3)))
    if negated_flag := _NEGATED_FLAG.fullmatch(clause):
        return Clause("fact", False, _key(negated_flag.group(1)), NO)
    if _FLAG.fullmatch(clause):
        return Clause("fact", False, _key(clause), YES)
    return None


def _strip_comments(text: str) -> str:
    # Comments are the author's notes to themselves. Kept line for line so
    # problems still point at the right place.
    return _COMMENT.sub(lambda match: "\n" * match.group(0).count("\n"), text)


class _Parser:
    def __init__(self, text: str, fallback_title: str) -> None:
        self.text = _strip_comments(text.replace("\r\n", "\n"))
        self.fallback_title = fallback_title
        self.problems: list[PlotProblem] = []
        # Filled as the sections are read, for conditions and `brings`.
        self.people: dict[str, str] = {}
        self.place_entries: dict[str, LoreEntry] = {}
        # Places and lore both: anything `reveals:` can name.
        self.lore_entries: dict[str, LoreEntry] = {}
        self.pictures: list[PlotPicture] = []
        # Each event's heading line, for problems found across events.
        self.event_lines: dict[str, int] = {}

    def note(self, line: int, message: str) -> None:
        self.problems.append(PlotProblem(line, message))

    def error(self, line: int, message: str) -> None:
        self.problems.append(PlotProblem(line, message, error=True))

    # --- structure -------------------------------------------------------------

    def front_matter(self, lines: list[str]) -> tuple[dict[str, tuple[int, str]], int]:
        """`---` … `---` at the top: `key: value` lines. Returns where the body starts."""
        index = 0
        while index < len(lines) and not lines[index].strip():
            index += 1
        if index >= len(lines) or lines[index].strip() != "---":
            return {}, 0
        values: dict[str, tuple[int, str]] = {}
        for end in range(index + 1, len(lines)):
            line = lines[end]
            if line.strip() == "---":
                return values, end + 1
            if not line.strip():
                continue
            key, colon, value = line.partition(":")
            if not colon:
                self.note(end + 1, f"Not a `key: value` line, so it was ignored: {line.strip()}")
                continue
            values[_key(key)] = (end + 1, value.strip())
        self.error(index + 1, "The `---` block at the top is never closed.")
        return values, len(lines)

    def blocks(self, lines: list[str], start: int) -> list[_Block]:
        top: list[_Block] = []
        stack: list[_Block] = []
        loose: list[tuple[int, str]] = []
        in_fence = False
        for index in range(start, len(lines)):
            number, line = index + 1, lines[index]
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
            match = None if in_fence else _HEADING.match(line)
            if match:
                block = _Block(len(match.group(1)), match.group(2).strip(), number)
                while stack and stack[-1].level >= block.level:
                    stack.pop()
                if stack:
                    stack[-1].children.append(block)
                elif block.level == 1:
                    top.append(block)
                else:
                    self.note(number, f"“{block.title}” comes before any `#` section; ignored.")
                    continue
                stack.append(block)
            elif stack:
                stack[-1].lines.append((number, line))
            elif line.strip():
                loose.append((number, line))
        if loose:
            self.note(loose[0][0], "Text before the first `#` section is ignored.")
        return top

    def fields(self, block: _Block, known: set[str]) -> _Fields:
        """The `- key: value` lines straight after a heading, and the prose after them.

        Only a run of known keys counts, so a bullet list in a description
        ("- Strength: legendary") stays prose. A key that belongs somewhere
        else in the format is pointed out, since it is almost certainly a
        field in the wrong place.
        """
        values: dict[str, tuple[int, str]] = {}
        pictures: list[tuple[int, str]] = []
        repeated: dict[str, list[tuple[int, str]]] = {PICTURE: pictures}
        lines = block.lines
        index = 0
        current: str | None = None
        while index < len(lines):
            number, line = lines[index]
            match = _FIELD.match(line)
            if match and _key(match.group(1)) in known:
                current = _key(match.group(1))
                if current in REPEATED:
                    repeated.setdefault(current, []).append((number, match.group(2).strip()))
                    index += 1
                    continue
                if current in values:
                    self.note(number, f"`{current}` is given twice; the later one is used.")
                values[current] = (number, match.group(2).strip())
            elif match and _key(match.group(1)) in _ALL_FIELDS:
                self.note(
                    number,
                    f"`{_key(match.group(1))}` isn't used under “{block.title}”, "
                    "so it was kept as prose.",
                )
                break
            elif current is not None and line.startswith(("  ", "\t")) and line.strip():
                # An indented line continues the field above it.
                held = repeated.get(current) if current in REPEATED else None
                line_number, value = held[-1] if held is not None else values[current]
                joined = (line_number, f"{value} {line.strip()}".strip())
                if held is not None:
                    held[-1] = joined
                else:
                    values[current] = joined
            elif not line.strip():
                # Blank lines inside the run are fine; the run ends at prose.
                following = next((text for _, text in lines[index + 1 :] if text.strip()), "")
                next_match = _FIELD.match(following)
                if not (next_match and _key(next_match.group(1)) in known):
                    break
            else:
                break
            index += 1
        return _Fields(values, _prose(lines[index:]), pictures, repeated)

    def body(self, block: _Block, known: set[str]) -> _Fields:
        """`fields`, with any sub-headings under the item kept in its prose.

        A `### Skills` list inside a character's description is part of the
        description; only events have sub-headings of their own.
        """
        found = self.fields(block, known)
        parts = [found.prose] if found.prose else []
        for child in block.children:
            parts.append(f"{'#' * child.level} {child.title}")
            if text := child.text():
                parts.append(text)
        return _Fields(found.values, "\n\n".join(parts), found.pictures, found.repeated)

    def keep_pictures(self, found: _Fields, kind: str, owner_id: str, name: str) -> None:
        """Note an item's picture lines for whoever imports the file."""
        for line, value in found.pictures:
            source, caption = picture_parts(value)
            problem = picture_problem(source)
            if problem:
                self.error(line, problem)
                continue
            self.pictures.append(PlotPicture(kind, owner_id, name, source, caption, line))

    # --- values -----------------------------------------------------------------

    def flag(self, line: int, value: str, what: str) -> bool:
        lowered = _norm(value)
        if lowered in ("yes", "true", "y", "on"):
            return True
        if lowered in ("no", "false", "n", "off", ""):
            return False
        self.error(line, f"`{what}` should be yes or no, not “{value}”.")
        return False

    def clock(self, line: int, value: str) -> int | None:
        """ "day 3, 16:00", "day 3", or "16:00" (on day 1), as minutes from day 1, 00:00."""
        text = _norm(value)
        day_match = re.search(r"day\s*(\d+)", text)
        time_match = re.search(r"(\d{1,2})[:.](\d{2})", text)
        if not day_match and not time_match:
            self.error(line, f"Can't read the time “{value}”; write e.g. “day 1, 16:00”.")
            return None
        day = int(day_match.group(1)) if day_match else 1
        hours, minutes = (
            (int(time_match.group(1)), int(time_match.group(2))) if time_match else (0, 0)
        )
        if day < 1 or hours > 23 or minutes > 59:
            self.error(line, f"“{value}” isn't a time on the story's clock.")
            return None
        return (day - 1) * MINUTES_PER_DAY + hours * 60 + minutes

    def window(self, line: int, value: str) -> tuple[int, int] | None:
        text = _norm(value)
        match = re.fullmatch(r"days?\s*(\d+)\s*(?:-|–|—|to)\s*(?:day\s*)?(\d+)", text)
        if match:
            first, last = int(match.group(1)), int(match.group(2))
        elif single := re.fullmatch(r"days?\s*(\d+)", text):
            first = last = int(single.group(1))
        else:
            self.error(line, f"Can't read the days “{value}”; write e.g. “day 30–50”.")
            return None
        if first < 1 or last < first:
            self.error(line, f"“{value}” isn't a range of days.")
            return None
        return first, last

    def pacing(self, line: int, value: str) -> Pacing:
        text = _norm(value)
        if text in ("quiet", "quiet moment", "lull", "at a lull", "a quiet moment"):
            return "quiet"
        if text in ("any", "any time", "anytime", "immediately", "at once", "whenever"):
            return "any"
        self.error(line, f"`pacing` is “quiet moment” or “any time”, not “{value}”.")
        return "quiet"

    # --- sections ----------------------------------------------------------------

    def facts(self, block: _Block | None) -> list[FactDef]:
        if block is None:
            return []
        facts: list[FactDef] = []
        for number, line in block.lines:
            match = _FIELD.match(line)
            if not match:
                continue
            name = _key(match.group(1))
            if not _FACT_NAME.match(name):
                self.error(number, f"“{match.group(1)}” can't be a fact name: letters, digits, _.")
                continue
            if any(fact.name == name for fact in facts):
                self.error(number, f"The fact `{name}` is defined twice.")
                continue
            initial, values, watched, meaning = fact_parts(match.group(2))
            if not initial:
                self.error(number, f"`{name}` needs a starting value: `- {name}: no — …`.")
                continue
            if not values and initial in (YES, NO):
                values = [YES, NO]
            if values and initial not in values:
                self.error(
                    number, f"`{name}` starts as “{initial}”, which isn't one of its values."
                )
                continue
            if not meaning and watched:
                self.note(
                    number, f"`{name}` has no meaning line; the read that keeps it needs one."
                )
            facts.append(
                FactDef(name=name, initial=initial, meaning=meaning, values=values, watched=watched)
            )
        return facts

    def conditions(
        self, line: int, text: str, facts: list[FactDef], events: dict[str, str]
    ) -> list[Condition]:
        conditions: list[Condition] = []
        for clause in split_clauses(text):
            condition = self.condition(line, clause, facts, events)
            if condition is not None:
                conditions.append(condition)
        return conditions

    def place_name(self, line: int, reference: str) -> str | None:
        entry = self.place_entries.get(_norm(reference))
        if entry is None:
            self.error(line, f"No place called “{reference.strip()}” (the Places section).")
            return None
        return entry.title

    def condition(
        self, line: int, clause: str, facts: list[FactDef], events: dict[str, str]
    ) -> Condition | None:
        found = read_clause(clause, list(self.people))
        if found is None:
            self.error(line, f"Can't read the condition “{clause}”; write `fact = value`.")
            return None
        op: ConditionOp = "!=" if found.negated else "="
        if found.kind in ("at", "visited"):
            name = self.place_name(line, found.reference)
            if name is None:
                return None
            who = self.people[_norm(found.who)] if found.who else None
            return Condition(place=name, place_test=found.kind, op=op, who=who)
        if found.kind == "happened":
            reference = found.reference
            event_id = events.get(_norm(reference)) or events.get(_slug(reference))
            if event_id is None:
                self.error(line, f"No event called “{reference}”.")
                return None
            return Condition(event_id=event_id, happened=not found.negated)
        name, value = found.reference, found.value
        fact = next((fact for fact in facts if fact.name == name), None)
        if fact is None:
            self.error(line, f"No fact called `{name}` (the Facts section defines them).")
            return None
        if fact.values and value not in fact.values:
            self.error(
                line, f"`{name}` can't be “{value}”; it is one of: {', '.join(fact.values)}."
            )
            return None
        return Condition(fact=name, op=op, value=value)

    def assignments(self, line: int, text: str, facts: list[FactDef]) -> list[Assignment]:
        assignments: list[Assignment] = []
        for clause in re.split(r",|;", text):
            clause = clause.strip()
            if not clause:
                continue
            name, equals, value = clause.partition("=")
            name, value = _key(name), _norm(value)
            if not equals or not value:
                self.error(line, f"Can't read “{clause}”; `sets` takes `fact = value`.")
                continue
            fact = next((fact for fact in facts if fact.name == name), None)
            if fact is None:
                self.error(line, f"No fact called `{name}` (the Facts section defines them).")
                continue
            if fact.values and value not in fact.values:
                self.error(
                    line, f"`{name}` can't be “{value}”; it is one of: {', '.join(fact.values)}."
                )
                continue
            assignments.append(Assignment(fact=name, value=value))
        return assignments

    def brought(self, values: dict[str, tuple[int, str]]) -> tuple[list[str], list[str]]:
        """`brings:` characters and `reveals:` places, as ids."""
        brings: list[str] = []
        reveals: list[str] = []
        if "brings" in values:
            line, text = values["brings"]
            for name in (part.strip() for part in text.split(",")):
                if not name:
                    continue
                character_id = self.people.get(_norm(name))
                if character_id is None:
                    self.error(line, f"`brings` names “{name}”, who isn't in the Characters.")
                else:
                    brings.append(character_id)
        if "reveals" in values:
            line, text = values["reveals"]
            for name in (part.strip() for part in text.split(",")):
                if not name:
                    continue
                entry = self.lore_entries.get(_norm(name))
                if entry is None:
                    self.error(
                        line, f"`reveals` names “{name}”, which isn't in the Places or Lore."
                    )
                else:
                    reveals.append(entry.id)
        return brings, reveals

    def events(
        self, block: _Block | None, kind: EventKind, facts: list[FactDef], ids: dict[str, str]
    ) -> list[EventDef]:
        if block is None:
            return []
        events: list[EventDef] = []
        for child in block.children:
            parsed = self.event(child, kind, facts, ids)
            if parsed is not None:
                events.append(parsed)
                self.event_lines[parsed.id] = child.line
        if block.lines and _prose(block.lines):
            self.note(block.line, f"Text directly under “# {block.title}” is ignored.")
        return events

    def event(
        self, block: _Block, kind: EventKind, facts: list[FactDef], ids: dict[str, str]
    ) -> EventDef | None:
        found = self.fields(block, _EVENT_FIELDS)
        values = found.values
        event_id = ids[_norm(block.title)]

        def get(key: str) -> tuple[int, str]:
            return values.get(key, (block.line, ""))

        window = self.window(*get("when")) if "when" in values else None
        if kind == "timed" and window is None and "when" not in values:
            self.error(block.line, f"“{block.title}” is on the timeline but has no `when`.")
        shared_sets = self.assignments(*get("sets"), facts) if "sets" in values else []
        shared_offscreen = (
            self.flag(*get("offscreen"), "offscreen") if "offscreen" in values else None
        )
        tell = "\n\n".join(part for part in (get("tell")[1], found.prose) if part)

        variants: list[EventVariant] = []
        for child in block.children:
            if child.level != 3:
                continue
            variant_fields = self.fields(child, _VARIANT_FIELDS)
            variant = self.variant(child.title, variant_fields, child.line, facts, ids)
            # What the event sets, every way it goes; a variant's own value
            # for the same fact wins.
            own = {assignment.fact for assignment in variant.sets}
            variant.sets = [a for a in shared_sets if a.fact not in own] + variant.sets
            if "offscreen" not in variant_fields.values and shared_offscreen is not None:
                variant.offscreen = shared_offscreen
            variants.append(variant)
        if not variants:
            variants = [
                EventVariant(sets=shared_sets, offscreen=bool(shared_offscreen)),
            ]

        if not tell and not any(variant.tell for variant in variants):
            if not all(variant.offscreen for variant in variants):
                self.note(block.line, f"“{block.title}” has nothing to tell the storyteller.")
        brings, reveals = self.brought(values)
        must_happen = None
        if "must_happen" in values:
            must_happen = self.flag(*get("must_happen"), "must happen")
            if kind != "timed":
                self.note(get("must_happen")[0], "`must happen` is for timeline events only.")

        return EventDef(
            id=event_id,
            title=block.title,
            kind=kind,
            window=window,
            pacing=self.pacing(*get("pacing")) if "pacing" in values else "quiet",
            lead_in=self.flag(*get("lead_in"), "lead-in") if "lead_in" in values else False,
            requires=(
                self.conditions(*get("requires"), facts, ids) if "requires" in values else []
            ),
            trigger=get("trigger")[1],
            tell=tell,
            aftermath=get("aftermath")[1],
            variants=variants,
            must_happen=must_happen,
            brings=brings,
            reveals=reveals,
            place=self.place_name(*get("at")) if "at" in values else None,
        )

    def variant(
        self, title: str, found: _Fields, line: int, facts: list[FactDef], ids: dict[str, str]
    ) -> EventVariant:
        values = found.values

        def get(key: str) -> tuple[int, str]:
            return values.get(key, (line, ""))

        brings, reveals = self.brought(values)
        return EventVariant(
            title=title,
            brings=brings,
            reveals=reveals,
            place=self.place_name(*get("at")) if "at" in values else None,
            requires=self.conditions(*get("requires"), facts, ids) if "requires" in values else [],
            trigger=get("trigger")[1],
            tell="\n\n".join(part for part in (get("tell")[1], found.prose) if part),
            sets=self.assignments(*get("sets"), facts) if "sets" in values else [],
            offscreen=self.flag(*get("offscreen"), "offscreen") if "offscreen" in values else False,
        )

    def event_ids(self, blocks: list[_Block | None]) -> dict[str, str]:
        """Every event's id, by its title and by its id, before any is parsed.

        Conditions can name an event defined further down.
        """
        ids: dict[str, str] = {}
        taken: set[str] = set()
        for block in blocks:
            if block is None:
                continue
            for child in block.children:
                slug = base = _slug(child.title)
                suffix = 2
                while slug in taken:
                    slug = f"{base}_{suffix}"
                    suffix += 1
                if slug != base:
                    self.note(child.line, f"Two events are called “{child.title}”.")
                taken.add(slug)
                ids.setdefault(_norm(child.title), slug)
                ids.setdefault(slug, slug)
        return ids

    def characters(
        self, block: _Block | None, scene: SceneState
    ) -> tuple[list[Character], list[Character], list[str]]:
        cast: list[Character] = []
        supporting: list[Character] = []
        choose: list[str] = []
        if block is None:
            return cast, supporting, choose
        for child in block.children:
            found = self.body(child, _CHARACTER_FIELDS)
            values = found.values
            role_line, role = values.get("role", (child.line, "storyteller"))
            role = _norm(role)
            if role not in _ROLES:
                self.error(
                    role_line, f"`role` is one of {', '.join(sorted(_ROLES))}, not “{role}”."
                )
                role = "storyteller"
            prose = found.prose
            summary = values.get("summary", (0, ""))[1] or _first_sentence(prose)
            character = Character(
                name=child.title,
                aliases=[
                    alias.strip()
                    for alias in values.get("aliases", (0, ""))[1].split(",")
                    if alias.strip()
                ],
                summary=summary or None,
                full_description=prose or None,
                voice_notes=values.get("voice", (0, ""))[1] or None,
                canon=values.get("canon", (0, ""))[1] or None,
                is_player_available=role in ("player", "author"),
                author_only=role == "author",
            )
            chooses = False
            if "choose" in values:
                line, value = values["choose"]
                if _norm(value) not in ("start", "at start", "one", "yes"):
                    self.error(line, f"`choose` takes “start”, not “{value}”.")
                elif role not in ("player", "author"):
                    self.error(line, f"{child.title} can't be chosen at the start: not a player.")
                else:
                    chooses = True
            present = self.flag(*values["present"], "present") if "present" in values else False
            if "hidden" in values:
                character.plot_hidden = self.flag(*values["hidden"], "hidden")
                if character.plot_hidden and (present or chooses):
                    self.error(
                        values["hidden"][0],
                        f"{child.title} is hidden until an event brings them in, so can't be "
                        "in the opening scene or chosen at the start.",
                    )
            self.keep_pictures(found, "character", character.id, character.name)
            self.people[_norm(character.name)] = character.id
            for alias in character.aliases:
                self.people.setdefault(_norm(alias), character.id)
            if role == "supporting":
                if chooses:
                    self.error(child.line, f"{child.title} is supporting, so can't be played.")
                supporting.append(character)
                if present:
                    scene.present_others.append(character.name)
                continue
            cast.append(character)
            if chooses:
                choose.append(character.id)
            if present and not chooses:
                scene.present_character_ids.append(character.id)
            elif present:
                self.note(
                    values["present"][0],
                    f"{child.title} is chosen at the start, so is in the scene if picked.",
                )
        if block.children and not any(c.is_player_available for c in cast):
            self.note(block.line, "Nobody has `role: player`, so the author can only direct.")
        return cast, supporting, choose

    def name_clashes(self, characters: list[Character]) -> None:
        """Warn when two people answer to the same name part.

        The validators match parts of names ("Serrik" for "Serrik Vaun"), so a
        player's John beside an NPC called John Smith makes John Smith's lines
        look like the storyteller writing the author's character.
        """
        forms = {
            character.name: {form.lower() for form in name_forms(character)}
            for character in characters
        }
        names = list(forms)
        for index, first in enumerate(names):
            for second in names[index + 1 :]:
                shared = forms[first] & forms[second]
                if shared:
                    self.note(
                        0,
                        f"{first} and {second} both answer to "
                        f"“{sorted(shared, key=len)[0].title()}”. The checks match parts of "
                        "names, so one can be mistaken for the other; a different name, or an "
                        "alias the story uses instead, avoids it.",
                    )

    def names_like_places(self, characters: list[Character], titles: list[str]) -> None:
        """Warn when a part of someone's name is a place's or lore entry's
        whole name ("Ysolde of Sylvaranth", "Mags of the Undercroft"): the
        checks match parts of names, so every passage naming the place names
        them too, and a hidden one is brought in by it."""
        names = {}
        for title in titles:
            words = [
                w for w in re.findall(r"[\w’']+", title) if w.lower() not in ("the", "a", "an")
            ]
            if len(words) == 1:
                names[words[0].lower()] = title
        for character in characters:
            for form in name_forms(character):
                place = names.get(form.lower())
                if place is not None and " " not in form:
                    self.note(
                        0,
                        f"{character.name} answers to “{form}”, which is also {place}'s name. "
                        "The checks match parts of names, so a passage naming the place "
                        "names them too; an alias without it avoids that.",
                    )
                    break

    def places(self, block: _Block | None, *, are_places: bool = True) -> list[LoreEntry]:
        """`# Places` (where the story can be) or, with `are_places` off, `# Lore`:
        the same entries, but never offered to the chronicle as somewhere to be."""
        if block is None:
            return []
        entries: list[LoreEntry] = []
        for child in block.children:
            found = self.body(child, _PLACE_FIELDS)
            if not found.prose:
                self.note(child.line, f"“{child.title}” has no description.")
            keywords = [child.title] + [
                keyword.strip()
                for keyword in found.values.get("keywords", (0, ""))[1].split(",")
                if keyword.strip()
            ]
            always = (
                self.flag(*found.values["always"], "always") if "always" in found.values else False
            )
            entry = LoreEntry(
                title=child.title,
                content=found.prose or child.title,
                keywords=keywords,
                always_on=always,
            )
            if "hidden" in found.values:
                entry.plot_hidden = self.flag(*found.values["hidden"], "hidden")
            self.keep_pictures(found, "lore", entry.id, entry.title)
            if _norm(child.title) in self.lore_entries:
                self.error(child.line, f"Two places or lore entries are called “{child.title}”.")
            self.lore_entries[_norm(child.title)] = entry
            if are_places:
                self.place_entries[_norm(child.title)] = entry
            entries.append(entry)
        return entries

    def world_map(self, block: _Block | None) -> tuple[WorldDelta | None, str | None]:
        """`# Map`: a map drawn up beforehand (models/world.py). Its places
        stand where the file puts them, fixed from the start; `at:` is east,
        north in the map's `unit` (km unless it says), from the place it is
        `in:` or from the map's origin. Returns the map and its start."""
        if block is None:
            return None, None
        head = self.fields(block, _MAP_FIELDS)
        unit_line, unit_name = head.values.get("unit", (block.line, "km"))
        unit = _MAP_UNITS.get(_norm(unit_name))
        if unit is None:
            self.error(unit_line, f"`unit` is m, km or miles, not “{unit_name}”.")
            unit = 1000.0
        delta = WorldDelta()
        by_name: dict[str, WorldPlace] = {}
        read: list[tuple[_Block, _Fields, WorldPlace, tuple[float, float] | None, str]] = []
        for child in block.children:
            found = self.body(child, _MAP_PLACE_FIELDS)
            values = found.values
            if _norm(child.title) in by_name:
                self.error(child.line, f"Two places on the map are called “{child.title}”.")
                continue
            kind = None
            if "kind" in values:
                line, text = values["kind"]
                kind = _norm(text)
                if kind not in _MAP_KINDS:
                    self.error(
                        line, f"`kind` is one of {', '.join(sorted(_MAP_KINDS))}, not “{text}”."
                    )
                    kind = None
            at = None
            if "at" in values:
                line, text = values["at"]
                parts = [p.strip() for p in text.replace(";", ",").split(",")]
                try:
                    if len(parts) != 2:
                        raise ValueError
                    at = (float(parts[0]), float(parts[1]))
                    if not all(math.isfinite(v) for v in at):
                        raise ValueError
                except ValueError:
                    self.error(
                        line, f"`at` is two numbers, east and north (“1.5, -2”), not “{text}”."
                    )
            size = None
            if "size" in values:
                line, text = values["size"]
                try:
                    size = float(text) * unit
                    if size <= 0 or not math.isfinite(size):
                        raise ValueError
                except ValueError:
                    self.error(
                        line, f"`size` is how wide it is, a number of {unit_name}, not “{text}”."
                    )
                    size = None
            entry = self.lore_entries.get(_norm(child.title))
            # The same id at every import of the file (ids.stable_id), so a
            # map's art made once (a sample's, shipped beside it) fits.
            place = WorldPlace(
                id=stable_id("map place", _norm(child.title)),
                name=child.title,
                kind=kind,  # type: ignore[arg-type]
                what=found.prose,
                lore_id=entry.id if entry is not None else None,
                size=size,
            )
            if "known" in values and self.flag(*values["known"], "known"):
                if entry is not None and entry.plot_hidden:
                    self.note(
                        values["known"][0],
                        f"“{child.title}” is hidden in the plot, so it isn't known "
                        "until it is revealed.",
                    )
                else:
                    delta.seen.append(place.id)
            by_name[_norm(child.title)] = place
            inside = values.get("in", (0, ""))[1]
            read.append((child, found, place, at, inside))
        for _child, found, place, _at, inside in read:
            if not inside:
                continue
            parent = by_name.get(_norm(inside))
            if parent is None or parent is place:
                self.error(found.values["in"][0], f"“{inside}” isn't a place on the map.")
                continue
            place.inside = parent.id
        # Positions, parents first: a place `in:` another is placed from it.
        at_of = {place.id: at for _c, _f, place, at, _i in read}
        places = {place.id: place for _c, _f, place, _a, _i in read}

        def position(place: WorldPlace, depth: int = 0) -> None:
            if place.x is not None or depth > len(places):
                return
            at = at_of.get(place.id)
            parent = places.get(place.inside or "")
            if parent is not None:
                position(parent, depth + 1)
            if at is None:
                return  # placed by the program from its ways and its parent
            x, y = at[0] * unit, at[1] * unit
            if parent is not None and parent.x is not None:
                x, y = parent.x + x, parent.y + y  # type: ignore[operator]
            place.x, place.y = x, y

        for place in places.values():
            position(place)
        for _child, found, place, _at, _inside in read:
            for line, text in found.repeated.get(WAY, []):
                target, _, words = text.partition("|")
                other = by_name.get(_norm(target))
                if other is None or other is place:
                    self.error(line, f"“{target.strip()}” isn't another place on the map.")
                    continue
                direction, distance = _way_words(words)
                delta.links = [k for k in delta.links if {k.a, k.b} != {place.id, other.id}]
                delta.links.append(
                    WorldLink(a=place.id, b=other.id, direction=direction, distance=distance)
                )
            for line, text in found.repeated.get(THING, []):
                name, _, state = text.partition("|")
                if not name.strip():
                    self.error(line, "A `thing` needs a name.")
                    continue
                delta.things.append(
                    WorldThing(
                        id=stable_id("map thing", _norm(child.title), _norm(name)),
                        name=name.strip(),
                        at=place.id,
                        state=state.strip(),
                    )
                )
            if found.pictures:
                self.note(
                    found.pictures[0][0],
                    "Map places take no pictures; put them on the place under # Places.",
                )
        delta.places = list(places.values())
        start = None
        if "start" in head.values:
            line, name = head.values["start"]
            found_start = by_name.get(_norm(name))
            if found_start is None:
                self.error(line, f"`start` names “{name}”, which isn't a place on the map.")
            else:
                start = found_start.id
        if not delta.places:
            self.note(block.line, "The # Map section has no places.")
            return None, None
        return delta, start

    def opening(self, block: _Block | None, scene: SceneState) -> tuple[str, str]:
        if block is None:
            return "", "generate"
        found = self.body(block, _OPENING_FIELDS)
        values = found.values
        mode = "generate"
        if "mode" in values:
            line, value = values["mode"]
            text = _norm(value)
            if text in ("as written", "as_written", "written"):
                mode = "as_written"
            elif text not in ("generate", "generated"):
                self.error(line, f"`mode` is “generate” or “as written”, not “{value}”.")
        scene.location = values.get("location", (0, ""))[1] or None
        scene.time_of_day = values.get("time", (0, ""))[1] or None
        scene.situation = values.get("situation", (0, ""))[1] or None
        return found.prose, mode

    def literal(
        self, front: dict[str, tuple[int, str]], key: str, choices: tuple[str, ...]
    ) -> str | None:
        if key not in front:
            return None
        line, value = front[key]
        text = _key(value)
        if text not in choices:
            self.error(line, f"`{key}` is one of {', '.join(choices)}, not “{value}”.")
            return None
        return text

    # --- the whole file ----------------------------------------------------------

    def requirement_loops(self, events: Sequence[EventDef]) -> None:
        """Events that each need another of them to have happened first can
        never happen: an error, on the first of them in the file. Only the
        event's own `requires:` count (a variant's choose between versions of
        an event that happens anyway)."""
        needs = {
            event.id: {
                condition.event_id
                for condition in event.requires
                if condition.event_id is not None and condition.happened
            }
            for event in events
        }

        def reaches(start: str) -> set[str]:
            seen: set[str] = set()
            todo = list(needs.get(start, ()))
            while todo:
                found = todo.pop()
                if found not in seen:
                    seen.add(found)
                    todo.extend(needs.get(found, ()))
            return seen

        reach = {event.id: reaches(event.id) for event in events}
        titles = {event.id: event.title for event in events}
        done: set[str] = set()
        for event in events:
            if event.id in done or event.id not in reach[event.id]:
                continue
            loop = [
                other.id
                for other in events
                if other.id in reach[event.id] and event.id in reach[other.id]
            ]
            done.update(loop)
            names = ", ".join(f"“{titles[event_id]}”" for event_id in loop)
            if len(loop) == 1:
                message = f"{names} requires itself to have happened first: it can never happen."
            else:
                message = (
                    f"{names} each require another of them first: none of them can ever happen."
                )
            self.error(self.event_lines.get(event.id, 0), message)

    def parse(self) -> ParsedPlotFile:
        lines = self.text.split("\n")
        front, body_start = self.front_matter(lines)
        sections: dict[str, _Block] = {}
        for block in self.blocks(lines, body_start):
            name = _SECTIONS.get(_norm(block.title))
            if name is None:
                self.note(block.line, f"“# {block.title}” isn't a section this format reads.")
                continue
            if name in sections:
                self.error(block.line, f"There are two “{block.title}” sections.")
                continue
            sections[name] = block

        title = front.get("title", (0, ""))[1] or self.fallback_title
        scene = SceneState()
        world = sections["world"].text() if "world" in sections else ""
        opening, mode = self.opening(sections.get("opening"), scene)
        cast, supporting, choose = self.characters(sections.get("characters"), scene)
        self.name_clashes(cast + supporting)
        places = self.places(sections.get("places"))
        lore = [*places, *self.places(sections.get("lore"), are_places=False)]
        world_base, world_start = self.world_map(sections.get("map"))
        titles = [entry.title for entry in lore]
        if world_base is not None:
            titles += [place.name for place in world_base.places]
        self.names_like_places(cast + supporting, titles)

        facts = self.facts(sections.get("facts"))
        ids = self.event_ids([sections.get("timeline"), sections.get("events")])
        events = [
            *self.events(sections.get("timeline"), "timed", facts, ids),
            *self.events(sections.get("events"), "optional", facts, ids),
        ]
        self.requirement_loops(events)
        start = self.clock(*front["start"]) if "start" in front else None
        plot = (
            Plot(
                start_minutes=start or 0,
                facts=facts,
                events=events,
                places=[PlaceDef(name=entry.title, lore_id=entry.id) for entry in places],
            )
            if facts or events
            else None
        )
        brought = {
            item_id
            for event in events
            for holder in (event, *event.variants)
            for item_id in (*holder.brings, *holder.reveals)
        }
        for item in (*cast, *supporting, *lore):
            if item.plot_hidden and item.id not in brought:
                name = getattr(item, "name", None) or getattr(item, "title", "")
                self.note(0, f"{name} is hidden, but no event brings them in.")
        if plot is None and "start" in front:
            self.note(front["start"][0], "`start` sets the clock, but there are no events.")

        style = StyleDirectives()
        perspective = self.literal(front, "perspective", get_args(Perspective))
        if perspective:
            style.perspective = perspective  # type: ignore[assignment]
        length = self.literal(front, "length", get_args(ResponseStyle))
        if length == "custom":
            self.error(front["length"][0], "A custom length is set in the app, not in the file.")
        elif length:
            style.response_style = length  # type: ignore[assignment]

        scenario = Scenario(
            title=title,
            description=front.get("description", (0, ""))[1] or None,
            world_bible=world or None,
            opening_text=opening,
            opening_mode=mode,  # type: ignore[arg-type]
            starting_scene=scene,
            suggested_character_id=(
                choose[0] if choose else next((c.id for c in cast if c.is_player_available), None)
            ),
            choose_one_of=choose if len(choose) > 1 else [],
            style=style,
            cast=cast,
            lore=lore,
            supporting=supporting,
            plot=plot,
            world_map=world_base is not None,
            world_base=world_base,
            world_start=world_start,
        )
        agency = self.literal(front, "agency", get_args(AgencyMode))
        if agency:
            scenario.agency_mode = agency  # type: ignore[assignment]
        activity = self.literal(front, "world_activity", get_args(WorldActivity))
        if activity:
            scenario.world_activity = activity  # type: ignore[assignment]
        if "background" in front:
            line, value = front["background"]
            source, _caption = picture_parts(value)
            problem = picture_problem(source)
            if problem:
                self.error(line, problem.replace("`picture`", "`background`"))
            else:
                self.pictures.append(
                    PlotPicture("background", "", "The background", source, "", line)
                )
        for key, (line, _) in front.items():
            if key not in (
                "title",
                "description",
                "start",
                "agency",
                "world_activity",
                "perspective",
                "length",
                "background",
            ):
                self.note(line, f"`{key}` isn't a setting this format reads.")
        if not world:
            self.note(0, "There is no # World section.")
        return ParsedPlotFile(
            scenario, sorted(self.problems, key=lambda p: p.line), list(self.pictures)
        )


_MAP_UNITS = {
    "m": 1.0,
    "metre": 1.0,
    "metres": 1.0,
    "meter": 1.0,
    "meters": 1.0,
    "km": 1000.0,
    "kilometre": 1000.0,
    "kilometres": 1000.0,
    "kilometer": 1000.0,
    "kilometers": 1000.0,
    "mile": 1609.344,
    "miles": 1609.344,
}
_MAP_KINDS = frozenset(PlaceKind.__args__)  # type: ignore[attr-defined]


def _way_words(words: str) -> tuple[str | None, str | None]:
    """A way's words as (direction, distance): "north, five miles" is both;
    one part is a distance if it measures, else a direction."""
    from sealedlore.engine.world_layout import metres

    parts = [p.strip() for p in words.split(",") if p.strip()]
    if not parts:
        return None, None
    if len(parts) == 1:
        return (None, parts[0]) if metres(parts[0]) is not None else (parts[0], None)
    return parts[0], ", ".join(parts[1:])


def _first_sentence(text: str) -> str:
    paragraph = text.strip().split("\n\n", 1)[0].replace("\n", " ")
    match = re.match(r"(.+?[.!?])(\s|$)", paragraph)
    sentence = match.group(1) if match else paragraph
    return sentence if len(sentence) <= 240 else sentence[:237].rstrip() + "…"


def front_matter_of(text: str) -> dict[str, str]:
    """A plot file's front matter (`title`, `description`, `background`…) as
    plain values, read as the importer reads it, for a list of plot files that
    needs nothing else of them (the start page's sample stories)."""
    parser = _Parser(text, "")
    values, _body = parser.front_matter(parser.text.split("\n"))
    return {key: value for key, (_line, value) in values.items()}


def parse_plot_markdown(text: str, *, fallback_title: str = "Untitled") -> ParsedPlotFile:
    """Read a plot file. `fallback_title` (usually the file's name) is used when
    the file sets none."""
    return _Parser(text, fallback_title).parse()


def rekey_plot(
    plot: Plot,
    *,
    file_people: Sequence[Character],
    file_lore: Sequence[LoreEntry],
    story_people: Sequence[Character],
    story_lore: Sequence[LoreEntry],
) -> tuple[Plot, list[str]]:
    """A parsed plot's links re-keyed to a story's own cards, by name.

    Every parse of a file gives its characters and lore entries fresh ids, and
    the plot's `brings:`, `reveals:`, `Name at =` conditions and places carry
    them. Attached to a story as parsed (Story → Add a plot from a file…), no
    event could bring anyone in and every named-character condition tested
    nobody (the review of Oct 20 2026). Each id among the file's own cards
    (`file_people`, `file_lore`) becomes the story card's of the same name
    (or alias, or title); an id the file doesn't know is left as it is (a
    plot built against the story's own cards). Returns the plot and what it
    couldn't match: a `brings`/`reveals` of nobody is dropped, a condition
    about nobody stays and tests nobody, a place keeps its name alone.
    """
    story_by_name: dict[str, str] = {}
    for character in story_people:
        for alias in character.aliases:
            story_by_name.setdefault(_norm(alias), character.id)
    for character in story_people:
        story_by_name[_norm(character.name)] = character.id
    lore_by_title = {_norm(entry.title): entry.id for entry in story_lore}
    file_names = {character.id: character.name for character in file_people}
    file_titles = {entry.id: entry.title for entry in file_lore}
    problems: list[str] = []

    def found(item_id: str) -> str | None:
        """The story's id for a file card's; None for none; the id itself
        when the file doesn't know it."""
        if item_id in file_names:
            return story_by_name.get(_norm(file_names[item_id]))
        if item_id in file_titles:
            return lore_by_title.get(_norm(file_titles[item_id]))
        return item_id

    def brought(ids: list[str], field_name: str, where: str) -> list[str]:
        kept: list[str] = []
        for item_id in ids:
            story_id = found(item_id)
            if story_id is None:
                name = file_names.get(item_id) or file_titles.get(item_id, item_id)
                problems.append(
                    f"{where}: `{field_name}` names {name}, not in this story's cast or lore, "
                    "so it brings them in no longer."
                )
            else:
                kept.append(story_id)
        return list(dict.fromkeys(kept))

    rekeyed = plot.model_copy(deep=True)
    for event in rekeyed.events:
        holders = [(event, event.title)]
        holders += [(v, f"{event.title} › {v.title or 'its variant'}") for v in event.variants]
        for holder, where in holders:
            holder.brings = brought(holder.brings, "brings", where)
            holder.reveals = brought(holder.reveals, "reveals", where)
            for condition in holder.requires:
                if condition.who is None or condition.who not in file_names:
                    continue
                story_id = found(condition.who)
                if story_id is None:
                    problems.append(
                        f"{where}: a condition is about {file_names[condition.who]}, not in "
                        "this story's cast, so it tests nobody."
                    )
                else:
                    condition.who = story_id
    for place in rekeyed.places:
        if place.lore_id is not None and place.lore_id in file_titles:
            place.lore_id = found(place.lore_id)
    return rekeyed, problems
