"""The people in a lorebook, found by a model and cut from it word for word.

A lorebook written for SillyTavern often carries a world's characters as
entries (a profile each, "Name: … Appearance: … Personality: …"), or several
in one (a ship's crew, a family), and its rules for the storyteller beside
its lore ("Writing Style", "Speech & tone rules"). The author (Nov 20 2026):
the people should become characters, the author choosing who is the cast and
who supporting, or kept in Your characters; the rules pointed out, with the
choice of moving them into the story's style notes.

The authoring model is shown each entry's title, keywords, opening words and
the headings inside it, in batches (`find_messages`), and says which are
about one person, several, or are instructions; an entry about several is
shown whole on its own (`split_messages`), and the model says where each
one's part begins. Their cards are the entries' own text, cut where it says
(`locate_quote`), never written anew: nothing in the lorebook is lost, and
the reply stays short whatever the size of the book.

Pure: the messages, the replies read, and the lore taken apart. The session
makes the calls (`StorySession.find_lore_people`).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from sealedlore.engine.authoring import generic_alias
from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.validators import locate_quote
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.character import Character
from sealedlore.models.lore import LoreEntry

# What the finder is shown of each entry: its opening words, and the headings
# inside it (a crew's "[WIFE 2: …]" is two thousand words in).
EXCERPT_WORDS = 70
OUTLINE_LINES = 12
OUTLINE_CHARS = 70
KEYWORDS_SHOWN = 8
# Entries per call, by the tokens they are shown in (at about four characters
# a token): a book of 320 entries is seven calls.
BATCH_TOKENS = 9_000
FIND_MAX_TOKENS = 8_000
SPLIT_MAX_TOKENS = 4_000
# An entry this long is shown whole to be split; past it, its first part.
SPLIT_MOST_WORDS = 12_000
# What is left of an entry once its people are cut out, under this many words,
# is a heading and a line, nobody's own: the entry goes with them.
REMAINDER_WORDS = 40
# A part shorter than this is a line in a list ("- Badigadi: a Demon Lord"),
# not someone's profile: left in the lore, and said.
PART_MIN_WORDS = 30

Kind = Literal["person", "people", "instructions"]
_KINDS = ("person", "people", "instructions")
# Where a person found goes in a story: a card of the cast's or a supporting
# one; left in the lore as they are; or "drop", out of the lore with no card
# made (the story already has one for them).
Placement = Literal["cast", "supporting", "lore", "drop"]


@dataclass(frozen=True)
class Shown:
    """An entry as the finder sees it; `number` is how the reply names it."""

    number: int
    entry_id: str
    title: str
    keywords: tuple[str, ...]
    always_on: bool
    content: str


@dataclass
class FoundPerson:
    name: str
    aliases: list[str]
    summary: str
    # Their part of the entry, word for word, and where it stands in it.
    text: str
    entry_id: str
    entry_title: str
    start: int
    end: int
    always_on: bool
    # Their part is the entry, all of it that matters (`REMAINDER_WORDS`).
    whole_entry: bool


@dataclass
class FoundRules:
    """An entry of instructions for the storyteller, not lore."""

    entry_id: str
    title: str
    words: int
    always_on: bool


@dataclass
class LorePeople:
    people: list[FoundPerson] = field(default_factory=list)
    instructions: list[FoundRules] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Finding:
    """One entry the finder named: what it is, and for one person, who."""

    number: int
    kind: Kind
    name: str = ""
    aliases: tuple[str, ...] = ()
    summary: str = ""


# --- what the finder is shown -----------------------------------------------------------

_HEADING = re.compile(
    r"^(\[[^\]]{2,}\]|【[^】]{2,}】|#{1,4}\s+\S.*"
    r"|[A-Z][A-Z0-9 &'’/\-—–,()]{2,}:?|<[A-Za-z_][\w.\-]*>)$"
)


def outline(content: str) -> list[str]:
    """The headings inside an entry's text: lines in brackets or tags, a
    Markdown heading, or a short line in capitals, beyond the first few
    words, which the excerpt shows."""
    found: list[str] = []
    for line in content.splitlines()[1:]:
        line = line.strip()
        if 2 < len(line) <= OUTLINE_CHARS and _HEADING.match(line) and line not in found:
            found.append(line)
        if len(found) >= OUTLINE_LINES:
            break
    return found


def shown_entries(entries: Iterable[LoreEntry]) -> list[Shown]:
    """The entries with something in them, numbered from 1 in their order."""
    shown = []
    for entry in entries:
        if entry.content.strip():
            shown.append(
                Shown(
                    number=len(shown) + 1,
                    entry_id=entry.id,
                    title=entry.title,
                    keywords=tuple(entry.keywords),
                    always_on=entry.always_on,
                    content=entry.content,
                )
            )
    return shown


def render_shown(shown: Shown) -> str:
    words = shown.content.split()
    excerpt = " ".join(words[:EXCERPT_WORDS]) + (" …" if len(words) > EXCERPT_WORDS else "")
    head = f"[{shown.number}] {shown.title}"
    if shown.always_on:
        head += " (always sent)"
    lines = [head, f"{len(words)} words"]
    if shown.keywords:
        lines.append("Keywords: " + ", ".join(shown.keywords[:KEYWORDS_SHOWN]))
    lines.append(excerpt)
    headings = outline(shown.content)
    if headings:
        lines.append("Headings inside: " + " | ".join(headings))
    return "\n".join(lines)


def _tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def batches(shown: Sequence[Shown], most: int = BATCH_TOKENS) -> list[list[Shown]]:
    """`shown` in runs of about `most` tokens each, in order."""
    result: list[list[Shown]] = []
    run: list[Shown] = []
    size = 0
    for item in shown:
        cost = _tokens(render_shown(item)) + 2
        if run and size + cost > most:
            result.append(run)
            run, size = [], 0
        run.append(item)
        size += cost
    if run:
        result.append(run)
    return result


def _messages(system: str, user: str) -> list[PromptMessage]:
    return [
        PromptMessage(role="system", parts=(ContentPart(text=system),)),
        PromptMessage(role="user", parts=(ContentPart(text=user),)),
    ]


def find_messages(
    batch: Sequence[Shown], world: str = "", texts: PromptTexts = DEFAULT_TEXTS
) -> list[PromptMessage]:
    parts = []
    if world.strip():
        parts.append(texts.fill("lore_people.world", world=world.strip()))
    parts.append(texts["lore_people.find.entries"])
    parts.append("\n\n".join(render_shown(item) for item in batch))
    return _messages(texts["lore_people.find"], "\n\n".join(parts))


def split_messages(
    shown: Shown, world: str = "", texts: PromptTexts = DEFAULT_TEXTS
) -> list[PromptMessage]:
    words = shown.content.split()
    content = shown.content
    if len(words) > SPLIT_MOST_WORDS:
        content = " ".join(words[:SPLIT_MOST_WORDS])
    parts = []
    if world.strip():
        parts.append(texts.fill("lore_people.world", world=world.strip()))
    parts.append(texts.fill("lore_people.split.entry", title=shown.title, content=content))
    return _messages(texts["lore_people.split"], "\n\n".join(parts))


# --- the replies ------------------------------------------------------------------------


def _text(value: object) -> str:
    return " ".join(str(value).split()) if isinstance(value, str | int | float) else ""


def _aliases(value: object, name: str) -> tuple[str, ...]:
    listed = value if isinstance(value, list) else []
    seen = {name.casefold()}
    kept = []
    for alias in (_text(item) for item in listed):
        if alias and alias.casefold() not in seen and not generic_alias(alias):
            seen.add(alias.casefold())
            kept.append(alias)
    return tuple(kept)


def parse_find(text: str, batch: Sequence[Shown]) -> list[Finding]:
    """The entries the reply names that are in `batch`, each once (the
    first word on it stands). Raises ValueError for a reply with no JSON."""
    data = extract_json(text, dict, "list of entries")
    listed = data.get("entries")
    numbers = {item.number for item in batch}
    found: dict[int, Finding] = {}
    for raw in listed if isinstance(listed, list) else []:
        if not isinstance(raw, dict):
            continue
        try:
            number = int(raw.get("n"))
        except (TypeError, ValueError):
            continue
        kind = _text(raw.get("kind")).lower()
        if number not in numbers or number in found or kind not in _KINDS:
            continue
        name = _text(raw.get("name"))
        if kind == "person" and not name:
            continue
        found[number] = Finding(
            number=number,
            kind=kind,  # type: ignore[arg-type]
            name=name,
            aliases=_aliases(raw.get("aliases"), name),
            summary=_text(raw.get("summary")),
        )
    return sorted(found.values(), key=lambda finding: finding.number)


def _remainder(content: str, parts: Sequence[tuple[int, int]]) -> str:
    kept, at = [], 0
    for start, end in sorted(parts):
        kept.append(content[at:start])
        at = max(at, end)
    kept.append(content[at:])
    return "".join(kept)


def _whole(content: str, parts: Sequence[tuple[int, int]]) -> bool:
    return len(_remainder(content, parts).split()) < REMAINDER_WORDS


def single(shown: Shown, finding: Finding) -> FoundPerson:
    """The person an entry is about, their card the whole entry."""
    return FoundPerson(
        name=finding.name,
        aliases=list(finding.aliases),
        summary=finding.summary,
        text=shown.content.strip(),
        entry_id=shown.entry_id,
        entry_title=shown.title,
        start=0,
        end=len(shown.content),
        always_on=shown.always_on,
        whole_entry=True,
    )


def parse_split(text: str, shown: Shown) -> tuple[list[FoundPerson], list[str]]:
    """The people an entry about several holds, each with the part of it
    from where the reply says theirs begins to where the next one's does
    (the last, to its end); and warnings for anyone whose start isn't in it.
    Raises ValueError for a reply with no JSON."""
    data = extract_json(text, dict, "list of people")
    listed = data.get("people")
    starts: list[tuple[int, str, tuple[str, ...], str]] = []
    warnings: list[str] = []
    names: set[str] = set()
    for raw in listed if isinstance(listed, list) else []:
        if not isinstance(raw, dict):
            continue
        name = _text(raw.get("name"))
        if not name or name.casefold() in names:
            continue
        where = locate_quote(shown.content, str(raw.get("starts") or ""))
        if where is None:
            warnings.append(f"where {name}'s part of “{shown.title}” begins couldn't be found")
            continue
        names.add(name.casefold())
        start = shown.content.rfind("\n", 0, where[0]) + 1
        starts.append((start, name, _aliases(raw.get("aliases"), name), _text(raw.get("summary"))))
    starts.sort()
    # Two people found to start on one line: the second has no part of their
    # own (the model named one under the other's heading).
    unique: list[tuple[int, str, tuple[str, ...], str]] = []
    for item in starts:
        if unique and unique[-1][0] == item[0]:
            warnings.append(f"{item[1]} has no part of their own in “{shown.title}”")
            continue
        unique.append(item)
    spans = [
        (start, unique[i + 1][0] if i + 1 < len(unique) else len(shown.content))
        for i, (start, *_rest) in enumerate(unique)
    ]
    short = [
        name
        for (start, name, *_rest), (_s, end) in zip(unique, spans, strict=True)
        if len(shown.content[start:end].split()) < PART_MIN_WORDS
    ]
    if short:
        listed = ", ".join(short)
        warnings.append(
            f"{listed} {'has' if len(short) == 1 else 'have'} only a line in “{shown.title}”, "
            "so they stay in the lore"
        )
        keep = [i for i, item in enumerate(unique) if item[1] not in short]
        unique, spans = [unique[i] for i in keep], [spans[i] for i in keep]
    whole = _whole(shown.content, spans)
    people = [
        FoundPerson(
            name=name,
            aliases=list(aliases),
            summary=summary,
            text=shown.content[start:end].strip(),
            entry_id=shown.entry_id,
            entry_title=shown.title,
            start=start,
            end=end,
            always_on=shown.always_on,
            whole_entry=whole and len(unique) == 1,
        )
        for (start, name, aliases, summary), (_s, end) in zip(unique, spans, strict=True)
    ]
    return people, warnings


# --- the lore taken apart ---------------------------------------------------------------


def first_sentence(text: str, limit: int = 200) -> str:
    """A summary for a person the finder gave none: the first sentence of
    their text that isn't a heading or a field's label alone."""
    for line in text.splitlines():
        line = line.strip()
        if len(line.split()) < 5 or _HEADING.match(line):
            continue
        sentence = re.split(r"(?<=[.!?])\s", line, maxsplit=1)[0]
        return sentence if len(sentence) <= limit else sentence[: limit - 1].rstrip() + "…"
    return ""


def character_of(person: FoundPerson, *, to_cast: bool) -> Character:
    """A card for someone found: their name, the other names they go by,
    their part of the lorebook whole as their description. Cast cards are
    playable, supporting ones the storyteller's, as Fill with lore
    characters makes them."""
    return Character(
        name=person.name,
        aliases=list(person.aliases),
        summary=person.summary or first_sentence(person.text) or None,
        full_description=person.text,
        is_player_available=to_cast,
    )


@dataclass
class TakenApart:
    """What taking people and rules out of a story's lore leaves: its lore
    (entries gone, or cut down to what was nobody's), the cards made, and
    the rules, for the style's notes."""

    lore: list[LoreEntry]
    cast: list[Character]
    supporting: list[Character]
    rules: str
    removed: int
    shortened: int


def take_apart(
    lore: Sequence[LoreEntry],
    people: Sequence[tuple[FoundPerson, Placement]],
    rules: Sequence[FoundRules] = (),
) -> TakenApart:
    """The people placed in the cast or supporting characters made cards,
    and their parts taken out of the entries they were in (also of those
    dropped, who have a card already): an entry left with nobody's own words
    goes. The rules' entries go too, their text
    joined under their titles for the style's notes. People left in the lore
    ("lore") change nothing. `lore` is not changed: a new list is made."""
    cast: list[Character] = []
    supporting: list[Character] = []
    cut: dict[str, list[tuple[int, int]]] = {}
    for person, placement in people:
        if placement == "lore":
            continue
        if placement != "drop":
            card = character_of(person, to_cast=placement == "cast")
            (cast if placement == "cast" else supporting).append(card)
        cut.setdefault(person.entry_id, []).append((person.start, person.end))
    rule_ids = {rule.entry_id for rule in rules}
    kept: list[LoreEntry] = []
    removed = shortened = 0
    notes: list[str] = []
    for entry in lore:
        if entry.id in rule_ids:
            text = entry.content.strip()
            notes.append(f"{entry.title}:\n{text}" if entry.title else text)
            removed += 1
            continue
        spans = cut.get(entry.id)
        if not spans:
            kept.append(entry)
            continue
        if _whole(entry.content, spans):
            removed += 1
            continue
        left = re.sub(r"\n{3,}", "\n\n", _remainder(entry.content, spans)).strip()
        kept.append(entry.model_copy(update={"content": left, "embedding_hash": None}))
        shortened += 1
    return TakenApart(
        lore=kept,
        cast=cast,
        supporting=supporting,
        rules="\n\n".join(notes),
        removed=removed,
        shortened=shortened,
    )
