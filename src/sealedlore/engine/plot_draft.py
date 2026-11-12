"""A plot written by the model, for a story with none, or in place of its own.

The author asked (Nov 2026): a story written from scratch or drafted from a
premise had no plot, since plots were written by hand. They wanted the model
to write one, from the story alone or from what they describe (how many
events, how often, how big, how disruptive, of what kinds), and to choose
whether to read it or meet it in play. Since Nov 6 2026 also how many people,
places and lore entries it brings in, and how fully each is written.

The model is shown the story as a plot file holds it (engine/story_plot.py)
and replies in that format: the plot sections alone. The reply is read by the
plot editor's reader and joined to the story's own parts (`merge_reply`: the
story's cards are never the reply's to change), then judged by the importer
as a file would be. What the importer refuses, a newcomer whose name another
already answers to, and a secret named before its time (engine/plot_lint.py)
are sent back once to be put right (`plot_problems`); whatever the importer
still refuses after that is left out, and said (`drop_broken`).

Pure: the session makes the calls (engine/session_plot_draft.py).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from sealedlore.engine.plot_file import (
    CHARACTER,
    EVENT,
    FACT,
    LORE,
    PLACE,
    ReadPlotDocument,
    check_plot_document,
    describe_where,
    read_plot_document,
    render_plot_markdown,
)
from sealedlore.engine.plot_lint import named_too_early
from sealedlore.engine.plot_md import _norm, parse_plot_markdown
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.story_plot import PlotDocumentError, made_of
from sealedlore.engine.validators import name_forms
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.plot import MINUTES_PER_DAY, Plot
from sealedlore.models.plot_file import OpeningDoc, PlotDocument

# A plot of a dozen events with their variants passed 8k tokens in drafting
# terms (Doomsville's is ~3k words); room for that, and a model that reasons
# first. Only what is used is billed.
PLOT_MAX_TOKENS = 32_000
# The author's choices (gui/plot_wishes.py), each with its words for the model
# in prompt_texts (`authoring.plot.<choice>.<value>`).
EVENT_COUNTS = ("few", "several", "many")
SPACINGS = ("close", "steady", "spread")
SCALES = ("personal", "mixed", "world")
DISRUPTIONS = ("gentle", "steady", "upending")
# Whether a plot may bring in people, places and lore the story doesn't have
# ("none": only the story's own), and how many; and how fully each is written
# (the author, Nov 6 2026, for plots as for premise drafts).
NEWCOMERS = ("none", "few", "several", "many")
NO_NEWCOMERS = "none"
DEPTHS = ("sketch", "deep")
KINDS = (
    "danger",
    "mystery",
    "arrivals",
    "relationships",
    "intrigue",
    "discovery",
    "disaster",
    "choices",
)
# Rounds of leaving out what the importer refuses: each round can only take
# away, and a dropped event can take the ones that required it with it.
DROP_ROUNDS = 12


@dataclass(frozen=True)
class PlotWishes:
    """What the author wants of a plot: nothing at all leaves it to the model."""

    premise: str = ""
    events: str = ""
    spacing: str = ""
    scale: str = ""
    disruption: str = ""
    kinds: tuple[str, ...] = ()
    # How many people, places and lore entries it may bring in that the story
    # doesn't have: "none", or how many. It was a yes or no until Nov 2026.
    newcomers: str = ""
    # How fully each of them is written.
    depth: str = ""

    def __post_init__(self) -> None:
        for value, allowed in (
            (self.events, EVENT_COUNTS),
            (self.spacing, SPACINGS),
            (self.scale, SCALES),
            (self.disruption, DISRUPTIONS),
            (self.newcomers, NEWCOMERS),
            (self.depth, DEPTHS),
        ):
            # Strictly: `newcomers=False`, the yes or no it once was, is
            # refused rather than read as "the model decides".
            if value != "" and value not in allowed:
                raise ValueError(f"“{value}” isn't one of {', '.join(allowed)}")
        unknown = [kind for kind in self.kinds if kind not in KINDS]
        if unknown:
            raise ValueError(f"no kind of event called {', '.join(unknown)}")


@dataclass
class PlotDraft:
    """A plot the model wrote, as the story has it now."""

    document: PlotDocument
    # What was left out or couldn't be done, for the author who reads it.
    notes: list[str] = field(default_factory=list)
    # Whether the first reply had to be put right.
    repaired: bool = False

    @property
    def events(self) -> int:
        return len(self.document.events)


# --- the request ------------------------------------------------------------------


def story_text(document: PlotDocument) -> str:
    """The story as the model is shown it: its own parts, as a plot file,
    with none of the settings a plot needn't know."""
    shown = document.model_copy(
        update={
            "facts": [],
            "events": [],
            "agency": None,
            "world_activity": None,
            "perspective": None,
            "length": None,
            "picture_style": "",
            "background": "",
            "notes": "",
        },
        deep=True,
    )
    return render_plot_markdown(shown).text.strip()


def told_from_start(document: PlotDocument) -> str:
    """What the story says before any event: a name in it is no secret."""
    opening = document.opening
    return "\n".join(
        [document.world, opening.notes, opening.location, opening.time, opening.situation]
    )


def render_wishes(wishes: PlotWishes, texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """The author's wishes, a line each, in the words of `authoring.plot.*`."""
    lines: list[str] = []
    premise = wishes.premise.strip()
    if premise:
        lines.append(texts.fill("authoring.plot.premise", premise=" ".join(premise.split())))
    else:
        lines.append(texts["authoring.plot.auto"])
    for choice, value in (
        ("events", wishes.events),
        ("spacing", wishes.spacing),
        ("scale", wishes.scale),
        ("disruption", wishes.disruption),
    ):
        if value:
            lines.append(texts[f"authoring.plot.{choice}.{value}"])
    if wishes.kinds:
        kinds = ", ".join(texts[f"authoring.plot.kind.{kind}"] for kind in wishes.kinds)
        lines.append(texts.fill("authoring.plot.kinds", kinds=kinds))
    if wishes.newcomers == NO_NEWCOMERS:
        lines.append(texts["authoring.plot.no_newcomers"])
    else:
        if wishes.newcomers:
            lines.append(texts[f"authoring.plot.newcomers.{wishes.newcomers}"])
        # How fully newcomers are written means nothing with none wanted.
        if wishes.depth:
            lines.append(texts[f"authoring.plot.depth.{wishes.depth}"])
    return "\n".join(f"- {line}" for line in lines)


def build_plot_messages(
    story: str,
    wishes: PlotWishes,
    *,
    plays: str = "",
    so_far: str = "",
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """The plot call. `story`: `story_text`; `plays`: who the author plays,
    in words; `so_far`: where a story under way stands."""
    parts = [texts.fill("authoring.plot.story", story=story)]
    if plays:
        parts.append(plays)
    if so_far:
        parts.append(texts.fill("authoring.plot.so_far", so_far=so_far))
    parts.append(texts.fill("authoring.plot.wishes", wishes=render_wishes(wishes, texts)))
    return [
        PromptMessage(role="system", parts=(ContentPart(text=texts["authoring.plot"]),)),
        PromptMessage(role="user", parts=(ContentPart(text="\n\n".join(parts)),)),
    ]


def plot_reply_text(document: PlotDocument) -> str:
    """A story's plot as a reply of the plot writer's holds it: the people,
    places and lore it keeps hidden, its places (one of the story's own by
    its title alone), its facts and its events. What a follow-up hands the
    model as its own last reply."""
    shown = document.model_copy(
        update={
            "title": "",
            "description": "",
            "world": "",
            "notes": "",
            "background": "",
            "picture_style": "",
            "agency": None,
            "world_activity": None,
            "perspective": None,
            "length": None,
            "start_day": 1,
            "start_time": "00:00",
            "opening": OpeningDoc(),
            "characters": [c for c in document.characters if c.hidden],
            "places": [
                place
                if place.hidden
                else place.model_copy(
                    update={"keywords": [], "description": "", "always": False, "pictures": []}
                )
                for place in document.places
            ],
            "lore": [entry for entry in document.lore if entry.hidden],
        },
        deep=True,
    )
    text = render_plot_markdown(shown).text
    # A reply begins at its first section: the clock's front matter is the
    # story's, as the plot writer's own replies have none.
    return text[text.find("# ") :].strip() if "# " in text else text.strip()


def build_plot_revision_messages(
    story: str,
    previous: str,
    request: str,
    *,
    plays: str = "",
    so_far: str = "",
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """A plot's follow-up: the story, the plot as it is now as the model's
    own last reply (`plot_reply_text`), and the author's request after it
    (`followup.plot_request_text`)."""
    parts = [texts.fill("authoring.plot.story", story=story)]
    if plays:
        parts.append(plays)
    if so_far:
        parts.append(texts.fill("authoring.plot.so_far", so_far=so_far))
    parts.append(texts["authoring.plot.revise"])
    return [
        PromptMessage(role="system", parts=(ContentPart(text=texts["authoring.plot"]),)),
        PromptMessage(role="user", parts=(ContentPart(text="\n\n".join(parts)),)),
        PromptMessage(role="assistant", parts=(ContentPart(text=previous),)),
        PromptMessage(role="user", parts=(ContentPart(text=request),)),
    ]


def build_repair_messages(
    messages: Sequence[PromptMessage],
    reply: str,
    problems: Sequence[str],
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """The same call, with its reply and what is wrong with it, once."""
    listed = "\n".join(f"- {problem}" for problem in problems)
    return [
        *messages,
        PromptMessage(role="assistant", parts=(ContentPart(text=reply),)),
        PromptMessage(
            role="user",
            parts=(ContentPart(text=texts.fill("authoring.plot.repair", problems=listed)),),
        ),
    ]


# --- the reply --------------------------------------------------------------------

_FENCE = re.compile(r"^\s*(?:```|~~~)[\w-]*\s*$", re.MULTILINE)
# "- **when**: day 3" and "- **when:** day 3": a setting made bold.
_BOLD_KEY = re.compile(r"^(\s*[-*]\s+)\*\*([A-Za-z][A-Za-z _-]*?)(:?)\*\*(:?)", re.MULTILINE)
# "## **The raid**": a heading made bold.
_BOLD_HEADING = re.compile(r"^(#{1,3})\s+\*\*(.+?)\*\*\s*$", re.MULTILINE)


def _plain_key(match: re.Match[str]) -> str:
    if not (match.group(3) or match.group(4)):
        return match.group(0)  # bold words in a list, not a setting
    return f"{match.group(1)}{match.group(2)}:"


def tidy_reply(text: str) -> str:
    """The reply as a plot file: the fence a model wraps it in, and the bold
    it gives its settings and headings, taken off. Nothing else is changed."""
    text = _FENCE.sub("", text.replace("\r\n", "\n"))
    text = _BOLD_KEY.sub(_plain_key, text)
    return _BOLD_HEADING.sub(r"\1 \2", text)


def read_reply(text: str) -> ReadPlotDocument:
    return read_plot_document(tidy_reply(text))


def merge_reply(
    story: PlotDocument, reply: PlotDocument, *, newcomers: str = "", revise: bool = False
) -> tuple[PlotDocument, list[str]]:
    """The story's own parts with the reply's plot.

    Only the reply's facts and events are taken whole. Of its characters,
    places and lore, those the story has (by name, or a character's alias)
    stay the story's: a place it names is one of the story's entries made a
    place. The rest are newcomers, unless none were wanted (`newcomers`, as
    `PlotWishes` has it: "none"), and never in the opening scene: that is the
    story's. How many were asked for is the model's to keep to: a newcomer
    left out would take the events that bring them with it. Returns the
    document and what was left out.

    `revise`: the reply is a plot written again at the author's request. The
    people, places and lore the old plot kept hidden are the plot's, so the
    reply's words for them stand (on the same cards), and those it no longer
    has go with the old plot.
    """
    if newcomers not in ("", *NEWCOMERS):
        # Loud for the yes or no it once was, too.
        raise ValueError(f"“{newcomers}” isn't one of {', '.join(NEWCOMERS)}")
    nobody_new = newcomers == NO_NEWCOMERS
    merged = story.model_copy(deep=True)
    merged.facts = [fact.model_copy(deep=True) for fact in reply.facts]
    merged.events = [event.model_copy(deep=True) for event in reply.events]
    notes: list[str] = []
    if revise:
        _take_revised(merged, reply)
    names = {
        _norm(form)
        for character in merged.characters
        for form in (character.name, *character.aliases)
        if form.strip()
    }
    for character in reply.characters:
        if _norm(character.name) in names or not character.name.strip():
            continue
        if nobody_new:
            notes.append(f"Left out {character.name}: nobody new was wanted.")
            continue
        names.add(_norm(character.name))
        merged.characters.append(
            character.model_copy(
                update={"present": False, "choose": False, "story_id": "", "pictures": []},
                deep=True,
            )
        )
    for entries, target in ((reply.places, merged.places), (reply.lore, merged.lore)):
        for entry in entries:
            title = _norm(entry.name)
            if not title:
                continue
            lore_at = next((i for i, e in enumerate(merged.lore) if _norm(e.name) == title), None)
            if any(_norm(place.name) == title for place in merged.places):
                continue
            if lore_at is not None:
                if target is merged.places:
                    # One of the story's own entries, made one of the plot's places.
                    merged.places.append(merged.lore.pop(lore_at))
                continue
            if nobody_new:
                notes.append(f"Left out {entry.name}: nowhere new was wanted.")
                continue
            target.append(entry.model_copy(update={"story_id": "", "pictures": []}, deep=True))
    return merged, notes


def _take_revised(merged: PlotDocument, reply: PlotDocument) -> None:
    """The old plot's hidden people, places and lore as the revised plot has
    them: the reply's words on the story's own cards, and out of the
    document if the reply no longer has them."""
    people = {_norm(c.name): c for c in reply.characters if c.name.strip()}
    kept = []
    for character in merged.characters:
        if not character.hidden or not character.story_id:
            kept.append(character)
            continue
        written = people.get(_norm(character.name))
        if written is None:
            continue  # gone with the old plot
        kept.append(
            written.model_copy(
                update={
                    "story_id": character.story_id,
                    "hidden": True,
                    "present": False,
                    "choose": False,
                    "pictures": [],
                },
                deep=True,
            )
        )
    merged.characters = kept
    entries = {_norm(e.name): e for e in (*reply.places, *reply.lore) if e.name.strip()}
    for target in (merged.places, merged.lore):
        kept_entries = []
        for entry in target:
            if not entry.hidden or not entry.story_id:
                kept_entries.append(entry)
                continue
            written = entries.get(_norm(entry.name))
            if written is None:
                continue
            kept_entries.append(
                written.model_copy(
                    update={"story_id": entry.story_id, "hidden": True, "pictures": []},
                    deep=True,
                )
            )
        target[:] = kept_entries


def _newcomer_clashes(document: PlotDocument, people) -> list[str]:
    """A newcomer whose name a part of another's answers to: the story finds
    people by the parts of their names, so one is taken for the other."""
    forms = {made.id: {form.casefold() for form in name_forms(made)} for _, made in people}
    found: list[str] = []
    seen: set[frozenset[str]] = set()
    for doc, made in people:
        if doc.story_id:
            continue
        for _, other in people:
            pair = frozenset((made.id, other.id))
            shared = forms[made.id] & forms[other.id]
            if other.id == made.id or not shared or pair in seen:
                continue
            seen.add(pair)
            part = sorted(shared, key=len)[0].title()
            found.append(
                f"Character “{made.name}”: shares “{part}” with {other.name}, and the story "
                "finds people by the parts of their names. Give the newcomer a name of their "
                "own, and change it everywhere the plot uses it."
            )
    return found


NOTHING_AT_FIRST = (
    "Nothing can happen in the story's first hours: every event waits for a later day or for "
    "another event. Under `# Events`, give one event no `when:` (or one that opens on day {day}) "
    "and a trigger the opening scene itself meets: the place the story opens in, with the people "
    "it opens with."
)


def nothing_at_first(plot: Plot) -> str:
    """The problem to put to the model when no event can come on the story's
    first day, or "". A player may play a whole evening before the clock
    passes its first day, and until then such a plot is silent: live, plots
    written with every other choice left to the model gave a talkative player
    nothing in two sessions of fourteen turns, three times running."""
    first = plot.start_minutes // MINUTES_PER_DAY + 1
    for event in plot.events:
        opens = event.window is None or event.window[0] <= first
        waits = any(c.event_id is not None and c.happened for c in event.requires)
        if opens and not waits:
            return ""
    return NOTHING_AT_FIRST.format(day=first)


def plot_problems(document: PlotDocument, *, opening: bool = False) -> list[str]:
    """What stops the plot working as written, in words for the model: the
    importer's errors, a newcomer's name another answers to, and a secret
    named before its time. All at once: live (GLM 5.3), a reply sent back
    with its one error alone came back with that put right and seven secrets
    still named early, none of which it had been told of. The importer reads
    past what it refuses, so what it made of the rest is judged too.
    `opening`: the story hasn't begun, so a plot with nothing for its first
    hours is said too (`nothing_at_first`)."""
    rendered, problems = check_plot_document(document)
    found = [
        f"{describe_where(document, problem.where)}: {problem.message}"
        for problem in problems
        if problem.error
    ]
    scenario = parse_plot_markdown(rendered.text).scenario
    if scenario.plot is None:
        return [*found, "There is no plot: write its facts and events."]
    people, _entries = made_of(document, scenario)
    found += _newcomer_clashes(document, people)
    found += [
        early.describe()
        for early in named_too_early(
            scenario.plot,
            [made for _, made in people],
            scenario.lore,
            told_from_start(document),
        )
    ]
    if opening and (gap := nothing_at_first(scenario.plot)):
        found.append(gap)
    return found


def _items(document: PlotDocument, section: str) -> list:
    return {
        CHARACTER: document.characters,
        PLACE: document.places,
        LORE: document.lore,
        FACT: document.facts,
        EVENT: document.events,
    }[section]


def drop_broken(document: PlotDocument) -> tuple[PlotDocument, list[str]]:
    """The document less whatever the importer still refuses: a whole event
    for an error in any way it can go, a fact, a newcomer. Leaving one out
    can leave out what needed it, so it goes round until nothing is refused.
    An error in the story's own parts can't be left out: PlotDocumentError.
    Returns the document and a line for each item left out."""
    document = document.model_copy(deep=True)
    notes: list[str] = []
    for _ in range(DROP_ROUNDS):
        _, problems = check_plot_document(document)
        errors = [problem for problem in problems if problem.error]
        if not errors:
            return document, notes
        dropping: dict[str, dict[int, str]] = {}
        for problem in errors:
            where = problem.where
            section = where[0] if where is not None else None
            if section not in (FACT, EVENT, CHARACTER, PLACE, LORE):
                raise PlotDocumentError([f"{describe_where(document, where)}: {problem.message}"])
            item = _items(document, section)[where[1]]
            if section in (CHARACTER, PLACE, LORE) and item.story_id:
                raise PlotDocumentError([f"{describe_where(document, where)}: {problem.message}"])
            dropping.setdefault(section, {}).setdefault(
                where[1], f"{describe_where(document, (section, where[1], -1))}: {problem.message}"
            )
        for section, found in dropping.items():
            items = _items(document, section)
            for index in sorted(found, reverse=True):
                notes.append(f"Left out {found[index]}")
                del items[index]
    _, problems = check_plot_document(document)
    raise PlotDocumentError([problem.message for problem in problems if problem.error])
