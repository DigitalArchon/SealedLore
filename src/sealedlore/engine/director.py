"""The director: when a plot's event should happen, and what the storyteller is told.

One call on a small model (`Config.plot_model`) before the storyteller's, made
by `StorySession.direct_turn` only when something is armed
(`engine.chronicle.armed`). Pure here: the prompt, the reply held to account,
and the two tail blocks.

The model judges only the fuzzy part: whether a `trigger:` is happening and
whether this is the moment. Every condition was already checked in code, and
the reply is checked again — an event that isn't armed, a kind it doesn't
allow, a variant that doesn't fit are all dropped. At most one event begins
per turn. An overdue event, or one that is "any time" with no trigger, begins
without asking.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from sealedlore.engine.chronicle import Armed, day_of, format_clock
from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.node import Node
from sealedlore.models.plot import Chronicle, Direction, EventDef, EventVariant, Plot

# Room for a reasoning model to think first: a cap only limits, it costs nothing
# unused. Live (GLM 5.3), 899 of 900 tokens went on reasoning and the reply was
# empty; glm-5.3-flash came back empty 5 times in 6 at 1500. Its reasoning
# can't be turned off on nano-gpt (HTTP 400).
DIRECTOR_MAX_TOKENS = 2500
# Two to four sentences for each of several events, in JSON.
GAP_MAX_TOKENS = 4000
# How many of the author's earlier turns the engagement figure is taken over.
ENGAGEMENT_TURNS = 8


@dataclass
class DirectorReply:
    directions: list[Direction] = field(default_factory=list)
    # Events the recent passages show already began.
    happened: list[str] = field(default_factory=list)
    # What the reply asked for that it couldn't have, for the log.
    refused: list[str] = field(default_factory=list)


def engagement(path: Sequence[Node], text: str) -> str:
    """The author's new turn against their recent ones, in words."""
    earlier = [
        len(node.content.split()) for node in path if node.kind == "user" and node.content.strip()
    ][-ENGAGEMENT_TURNS:]
    words = len(text.split())
    if not earlier:
        return f"The author's new turn is {words} words."
    return (
        f"The author's new turn is {words} words; their last {len(earlier)} turns were "
        f"a median of {round(statistics.median(earlier))}."
    )


_ASKED = re.compile(r"[\"“][^\"”]*\?[^\"”]*[\"”]|\?\s*$")


def awaits_answer(text: str) -> bool:
    """Whether the author's turn asks something: a question in speech, or ending on one."""
    return bool(_ASKED.search(text.strip()))


def held_for_answer(armed: Sequence[Armed], author_text: str) -> list[Armed]:
    """No quiet-moment event begins while the author waits on an answer.

    Live, Mistral Medium 3.5 started Hellsville's attack on "How many on the
    wall tonight?" at supper 3/3, even told that a question is owed its reply;
    it is plain in the text, so code decides it. Lead-ins, reveals and an
    overdue event are unaffected.
    """
    if not awaits_answer(author_text):
        return list(armed)
    kept: list[Armed] = []
    for item in armed:
        if "begin" in item.kinds and item.event.pacing == "quiet" and not item.overdue:
            kinds = tuple(kind for kind in item.kinds if kind != "begin")
            if not kinds:
                continue
            item = replace(item, kinds=kinds, variants=())
        kept.append(item)
    return kept


def _tell(event: EventDef, variant: EventVariant | None) -> str:
    parts = [event.tell]
    if variant is not None:
        parts.append(variant.tell)
    return "\n\n".join(part for part in parts if part.strip())


def render_armed(
    armed: Sequence[Armed],
    days: dict[str, int],
    chronicle: Chronicle,
    names: dict[str, str] | None = None,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> str:
    """The armed events as the director sees them. `names` maps the ids an
    event brings in to one line each (name and summary)."""
    names = names or {}
    blocks: list[str] = []
    for item in armed:
        event = item.event
        status = chronicle.events.get(event.id)
        lines = [f"## {event.id} — {event.title}"]
        lines.append(f"May: {', '.join(item.kinds)}")
        today = day_of(chronicle.minutes)
        if event.kind == "timed" and event.id in days:
            due = days[event.id]
            if due > today:
                days_off = f"{due - today} day{'s' if due - today > 1 else ''}"
                lines.append(texts.fill("plot.not_yet_due", day=str(due), wait=days_off))
            else:
                lines.append(f"Due from day {due}")
        if item.overdue:
            lines.append(texts["plot.overdue"])
        if item.unconfirmed:
            lines.append(texts["plot.unconfirmed"])
        if "begin" in item.kinds:
            lines.append(f"Pacing: {'quiet moment' if event.pacing == 'quiet' else 'any time'}")
        if "begin" in item.kinds or "lead_in" in item.kinds:
            if event.trigger:
                lines.append(f"Trigger: {event.trigger}")
            if event.tell:
                lines.append(f"What happens: {event.tell}")
            brought = [names[i] for i in (*event.brings, *event.reveals) if i in names]
            if brought:
                lines.append("Brings into the story: " + "; ".join(brought))
        for variant in item.variants:
            name = variant.title or "(the only way it goes)"
            lines.append(f"- Variant “{name}”")
            if variant.trigger:
                lines.append(f"  Trigger: {variant.trigger}")
            if variant.tell:
                lines.append(f"  What happens: {variant.tell}")
        if "reveal" in item.kinds:
            when = status.at_minutes if status is not None else None
            if when is not None:
                lines.append(f"Happened: {format_clock(when)}")
            lines.append(f"What is left, and what people say: {event.aftermath}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_director_messages(
    *,
    chronicle: Chronicle,
    armed: Sequence[Armed],
    days: dict[str, int],
    held_name: str,
    scene: str,
    recent: str,
    author_turn: str,
    engagement_line: str,
    names: dict[str, str] | None = None,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    system = texts.fill("plot.director", held=held_name)
    where = chronicle.place or "not at any of the story's named places"
    body = [
        f"THE CLOCK: {format_clock(chronicle.minutes)}",
        f"WHERE {held_name.upper()} IS: {where}",
        "THE SCENE:\n" + scene,
        "EVENTS YOU MAY ACT ON:\n\n" + render_armed(armed, days, chronicle, names, texts),
    ]
    if recent.strip():
        body.append("THE RECENT PASSAGES:\n\n" + recent.strip())
    body.append("THE AUTHOR'S NEW TURN:\n\n" + (author_turn.strip() or "(empty)"))
    body.append(engagement_line)
    return [
        PromptMessage(role="system", parts=(ContentPart(text=system),)),
        PromptMessage(role="user", parts=tuple(ContentPart(text=text) for text in body)),
    ]


def _clean(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _same_title(a: str, b: str) -> bool:
    def norm(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()

    return norm(a) == norm(b)


def direction_text(event: EventDef, kind: str, variant: EventVariant | None) -> str:
    if kind == "begin":
        return _tell(event, variant) or event.title
    if kind == "lead_in":
        return event.tell or event.title
    return event.aftermath


def _begin(item: Armed, wanted: str, why: str, refused: list[str]) -> Direction | None:
    variants = list(item.variants)
    chosen = next((v for v in variants if wanted and _same_title(v.title, wanted)), None)
    if chosen is None and len(variants) == 1:
        chosen = variants[0]
    if chosen is None:
        refused.append(f"{item.event.id}: no variant “{wanted}” fits")
        return None
    return Direction(
        event_id=item.event.id,
        kind="begin",
        variant=chosen.title or None,
        text=direction_text(item.event, "begin", chosen),
        why=why,
    )


def parse_director(text: str, armed: Sequence[Armed]) -> DirectorReply:
    """The reply, holding only what the armed events allow.

    Raises ValueError when there is no JSON to read.
    """
    data = extract_json(text, dict, "list of directions")
    by_id = {item.event.id: item for item in armed}
    reply = DirectorReply()
    items = data.get("directions")
    for entry in items if isinstance(items, list) else []:
        if not isinstance(entry, dict):
            continue
        event_id = _clean(entry.get("event"))
        kind = _clean(entry.get("kind")).lower().replace("-", "_").replace(" ", "_")
        why = _clean(entry.get("why"))
        item = by_id.get(event_id)
        if item is None:
            reply.refused.append(f"{event_id or '(no event)'} isn't one that can happen now")
            continue
        if kind == "happened":
            if item.unconfirmed:
                reply.happened.append(event_id)
            else:
                reply.refused.append(f"{event_id}: “happened” is only for one passed on")
            continue
        if kind not in item.kinds:
            reply.refused.append(f"{event_id}: can't {kind or '(no kind)'} now")
            continue
        if any(d.event_id == event_id for d in reply.directions):
            continue
        if kind == "begin":
            if any(d.kind == "begin" for d in reply.directions):
                reply.refused.append(f"{event_id}: only one event begins per passage")
                continue
            direction = _begin(item, _clean(entry.get("variant")), why, reply.refused)
            if direction is not None:
                reply.directions.append(direction)
            continue
        reply.directions.append(
            Direction(
                event_id=event_id,
                kind=kind,  # type: ignore[arg-type]
                text=direction_text(item.event, kind, None),
                why=why,
            )
        )
    return reply


def automatic_directions(armed: Sequence[Armed]) -> list[Direction]:
    """Events that begin without asking: overdue first, then "any time" ones."""
    ordered = sorted((item for item in armed if item.automatic), key=lambda i: not i.overdue)
    for item in ordered:
        direction = _begin(item, "", "overdue" if item.overdue else "any time, no trigger", [])
        if direction is not None:
            return [direction]
    return []


def merge(forced: list[Direction], chosen: list[Direction]) -> list[Direction]:
    """The forced begin wins over the director's; its lead-ins and reveals stay."""
    if not forced:
        return chosen
    others = [
        d for d in chosen if d.kind != "begin" and d.event_id not in {f.event_id for f in forced}
    ]
    return [*forced, *others]


# --- what the storyteller sees -------------------------------------------------


def render_direction_block(
    directions: Sequence[Direction],
    held_name: str | None,
    *,
    introductions: str = "",
    texts: PromptTexts = DEFAULT_TEXTS,
) -> str:
    if not directions:
        return ""
    who = held_name or "the author's character"
    lines = [texts["plot.direction"], ""]
    for direction in directions:
        text = " ".join(direction.text.split())
        kind = direction.kind if direction.kind in ("begin", "lead_in", "gap") else "reveal"
        lines.append("- " + texts.fill(f"plot.direction.{kind}", text=text, who=who))
    if introductions:
        lines.append("\n" + texts["plot.direction.introductions"] + "\n\n" + introductions)
    return "\n".join(lines)


def render_story_time_block(
    plot: Plot, chronicle: Chronicle, texts: PromptTexts = DEFAULT_TEXTS
) -> str:
    """The events the author's character knows of, and the clock only if the
    story-time text asks for it (`{now}`, not by default: `plot.story_time`).

    Nothing at all when there is nothing to say.
    """
    clock = "{now}" in texts["plot.story_time"]
    known = [
        (event, status)
        for event in plot.events
        if (status := chronicle.events.get(event.id)) is not None
        and status.state == "happened"
        and status.revealed
    ]
    if not known and not clock:
        return ""
    lines = [texts.fill("plot.story_time", now=format_clock(chronicle.minutes))]
    if known:
        if not clock:
            lines.append("")  # after the heading, as the clock line had it
        lines.append(texts["plot.story_time.known"])
        for event, status in known:
            dated = clock and status.at_minutes is not None
            when = f" ({format_clock(status.at_minutes)})" if dated else ""
            # The aftermath rides along for good: a reveal is one passage, and
            # arriving at the ruins sixteen turns later, the storyteller had
            # lost "the dead still walk its streets" and invented the rest.
            told = [_trimmed(status.account, ACCOUNT_CHARS), _trimmed(event.aftermath)]
            left = " ".join(text for text in told if text)
            lines.append(f"- {event.title}{when}" + (f": {left}" if left else ""))
    return "\n".join(lines)


AFTERMATH_CHARS = 300
# A skipped-over event's account is the only record of how it went.
ACCOUNT_CHARS = 500


def _trimmed(text: str, limit: int = AFTERMATH_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "…"


# --- choosing how an overdue event went ---------------------------------------


def build_choice_messages(
    *, event: EventDef, chronicle: Chronicle, recent: str, texts: PromptTexts = DEFAULT_TEXTS
) -> list[PromptMessage]:
    ways = "\n".join(
        f"- “{variant.title or '(the only way)'}”"
        + (" (happens away from the author's character)" if variant.offscreen else "")
        + (f": {' '.join(variant.tell.split())}" if variant.tell else "")
        for variant in event.variants
    )
    body = [
        f"THE EVENT: {event.title}. {' '.join(event.tell.split())}".strip(),
        "THE WAYS IT CAN GO:\n" + ways,
        f"THE CLOCK: {format_clock(chronicle.minutes)}; the author's character is at "
        f"{chronicle.place or 'none of the named places'}.",
    ]
    if recent.strip():
        body.append("THE RECENT PASSAGES:\n\n" + recent.strip())
    return [
        PromptMessage(role="system", parts=(ContentPart(text=texts["plot.choice"]),)),
        PromptMessage(role="user", parts=tuple(ContentPart(text=text) for text in body)),
    ]


def parse_choice(text: str, event: EventDef) -> EventVariant | None:
    """The chosen variant, or None if the reply names none of them."""
    data = extract_json(text, dict, "choice")
    wanted = _clean(data.get("variant"))
    return next(
        (v for v in event.variants if wanted and _same_title(v.title or "(the only way)", wanted)),
        None,
    )


# --- events that happened in a time skip ------------------------------------------


def build_gap_request(
    gap: Sequence[tuple[EventDef, EventVariant, int]],
    *,
    held_name: str | None,
    now: int,
    introductions: str = "",
    skipped: str = "",
    texts: PromptTexts = DEFAULT_TEXTS,
) -> str:
    """The side call's question: how each skipped-over event went.

    `gap` is each event, the variant it takes, and when it happened.
    `skipped` is the story's own telling of the time that passed, quoted so
    the accounts can't miss it: live, with it only in the history, Sonnet
    sent a character off on their journey on day 5 of a skip the story had
    spent with them in an archive.
    """
    blocks: list[str] = []
    for event, variant, at in gap:
        tell = " ".join((variant.tell or event.tell or event.title).split())
        lines = [
            f"## {event.id} — {event.title} (the plot expected it by {format_clock(at)})",
            tell,
        ]
        if event.aftermath:
            lines.append(texts["plot.gap.aftermath"] + " " + " ".join(event.aftermath.split()))
        blocks.append("\n".join(lines))
    text = texts.fill(
        "plot.gap",
        who=held_name or "The author's character",
        now=format_clock(now),
        events="\n\n".join(blocks),
        skipped=(
            f"\n{texts['plot.gap.skipped']}\n\n{skipped.strip()}\n" if skipped.strip() else ""
        ),
    )
    if introductions:
        text += "\n\n" + texts["plot.gap.introductions"] + "\n\n" + introductions
    return text


NOT_YET = ""


def parse_gap(text: str, ids: Sequence[str]) -> dict[str, str]:
    """Each event's account, by id; `NOT_YET` for one the story shows can't
    have happened yet. Events the reply leaves out are missing."""
    data = extract_json(text, dict, "accounts")
    accounts: dict[str, str] = {}
    for item in data.get("accounts") or []:
        if not isinstance(item, dict):
            continue
        event_id = _clean(item.get("event"))
        if event_id not in ids:
            continue
        if item.get("not_yet") is True:
            accounts[event_id] = NOT_YET
            continue
        account = " ".join(str(item.get("account") or "").split())
        if account:
            accounts[event_id] = account
    return accounts
