"""The plot keeper of a visual story (the author, Oct 24 2026: "the AI
handles plot points and can modify the world as needed to suit the plot,
including Director type instructions from the player"; the unusual action
"goes through the keeper with a roll", and "the Director always has full
command"). The director's successor for this mode: the plot file's facts,
timeline and events are the program's (engine/chronicle.py decides what is
due and armed); the keeper writes a **script** of operations from a fixed
set, which the program applies to the board, so the board stays the truth.

Pure: the operations and their shapes, the three requests (a moment of the
plot, a Director line, an unusual action), and the replies held to the set.
The session applies the scripts (engine/session_visual.py).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from sealedlore.engine.chronicle import Armed, day_of, format_clock
from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.plot import Chronicle, EventDef, EventVariant, Plot

# The operations a script may hold, each with the fields it takes. The
# program applies them (`VisualRuntime.apply_script`); anything else in a
# reply is dropped and said.
OPS: dict[str, tuple[str, ...]] = {
    "note": ("text",),  # a line the player reads
    "say": ("who", "text"),  # a person on the map speaks
    "spawn": ("who", "where"),  # a character of the story stands at a place
    "spawn_dead": ("where", "count"),  # the dead appear at a place
    "move": ("who", "where"),  # a person goes to a place ("beside the player", "away")
    "remove": ("who",),  # a person is gone from the map
    "tag_person": ("who", "tags"),  # a person's state, in words ("held by John", "bound")
    "go": ("where",),  # the player is now at a place (over the wall, inside, driven there)
    "place": ("what", "where", "count", "kind", "tags", "uses", "note"),  # a thing lies at a place
    "give": ("what", "count", "kind", "tags", "uses", "note"),  # a thing into the player's hands
    "take_away": ("what",),  # a thing the player carries is gone
    "break": ("what",),  # a thing here is broken
    # A thing here or carried becomes what it now is: a new name, kind and
    # state ("whole", "broken", "gone"), words for its state (tags) and the
    # verbs it now offers (uses), which the player picks from its menu and
    # which come back to the keeper as attempts. The author (Oct 24 2026): a
    # repaired car is "a new model for the car that isn't broken", with the
    # ability to use it that it lacked broken.
    "transform": ("what", "name", "kind", "state", "tags", "uses", "note"),
    "set_state": ("where", "state"),  # a place's state ("burnt out", "flooded")
    "unlock": ("where",),  # a building's door opens
    "lock": ("where", "key"),  # a building's door is locked
    "seal": ("where",),  # a building's door won't open until unsealed
    "unseal": ("where",),
    "reveal": ("what",),  # a hidden place or person of the plot comes into the story
    "set_fact": ("fact", "value"),
    "wound": ("count",),
    "heal": ("count",),
    "clock": ("minutes",),  # time passes
    "end": ("text",),  # the story ends
}
MAX_OPS = 24
KEEPER_MAX_TOKENS = 1800
WORLD_CHARS = 1500
# A ruling's difficulty words, as the dice take them (models/node.Difficulty).
DIFFICULTIES = ("trivial", "easy", "standard", "hard", "desperate")


@dataclass(frozen=True)
class Op:
    op: str
    args: dict[str, str]

    def get(self, key: str, default: str = "") -> str:
        return self.args.get(key, default)

    def count(self, default: int = 1) -> int:
        try:
            return max(0, min(200, int(float(self.args.get("count", default) or default))))
        except (TypeError, ValueError):
            return default

    def words(self, key: str) -> list[str]:
        """A comma- or semicolon-separated field as a list of words."""
        return [w.strip() for w in re.split(r"[,;]", self.args.get(key, "")) if w.strip()]


@dataclass(frozen=True)
class Firing:
    """An event the keeper fires: its title (matched to the plot), the
    variant's title ("" for the only way), and the script that tells it."""

    event: str
    variant: str
    script: list[Op]


@dataclass(frozen=True)
class KeeperReply:
    """A moment's or a Direction's reply."""

    fired: list[Firing] = field(default_factory=list)
    script: list[Op] = field(default_factory=list)
    note: str = ""
    dropped: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Ruling:
    """An unusual action's reply: possible or not, a roll (or none) and
    what happens either way."""

    possible: bool
    skill: str
    difficulty: str
    success: list[Op]
    failure: list[Op]
    note: str
    fired: list[Firing]
    dropped: list[str]


# --- the requests ----------------------------------------------------------------


def ops_catalogue() -> str:
    return "\n".join(f"- {op}: {', '.join(fields)}" for op, fields in OPS.items())


def armed_text(
    armed: Sequence[Armed],
    days: Mapping[str, int],
    chronicle: Chronicle,
    names: Mapping[str, str],
) -> str:
    """The events that could happen now, as the keeper reads them."""
    blocks = []
    today = day_of(chronicle.minutes)
    for item in armed:
        if "begin" not in item.kinds:
            continue
        event = item.event
        lines = [f"## {event.title}"]
        if event.kind == "timed" and event.id in days:
            lines.append(f"Due from day {days[event.id]} (today is day {today})")
        if item.overdue:
            lines.append("Overdue: it must happen now.")
        if event.trigger:
            lines.append(f"When: {event.trigger}")
        if event.tell:
            lines.append(f"What happens: {event.tell}")
        brought = [names[i] for i in (*event.brings, *event.reveals) if i in names]
        if brought:
            lines.append("Brings in: " + "; ".join(brought))
        for variant in item.variants:
            lines.append(f"- Variant “{variant.title or 'the only way'}”")
            if variant.trigger:
                lines.append(f"  When: {variant.trigger}")
            if variant.tell:
                lines.append(f"  What happens: {variant.tell}")
            brought = [names[i] for i in (*variant.brings, *variant.reveals) if i in names]
            if brought:
                lines.append("  Brings in: " + "; ".join(brought))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or "(nothing)"


def aftermaths(plot: Plot, chronicle: Chronicle) -> str:
    """What has happened already, in the plot's own aftermath words."""
    lines = []
    for event in plot.events:
        status = chronicle.events.get(event.id)
        if status is not None and status.state == "happened":
            when = format_clock(status.at_minutes) if status.at_minutes is not None else ""
            lines.append(
                f"- {event.title}"
                + (f" ({when})" if when else "")
                + (f": {event.aftermath}" if event.aftermath else "")
            )
    return "\n".join(lines) or "(nothing yet)"


def facts_text(chronicle: Chronicle) -> str:
    return ", ".join(f"{k} = {v}" for k, v in chronicle.facts.items()) or "(none)"


def _system(texts: PromptTexts, world: str) -> str:
    return "\n\n".join(
        [
            texts.fill("keeper.system", ops=ops_catalogue()),
            "THE WORLD:\n" + (" ".join(world.split())[:WORLD_CHARS] or "(nothing said of it)"),
        ]
    )


def moment_messages(
    *,
    world: str,
    state: str,
    armed: str,
    happened: str,
    facts: str,
    reason: str,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """A moment of the plot: what is armed, what has happened, where
    things stand, and why the keeper is asked now."""
    user = texts.fill(
        "keeper.moment", state=state, armed=armed, happened=happened, facts=facts, reason=reason
    )
    return [
        PromptMessage(
            role="system", parts=(ContentPart(text=_system(texts, world), cache_breakpoint=True),)
        ),
        PromptMessage(role="user", parts=(ContentPart(text=user),)),
    ]


def direction_messages(
    *,
    world: str,
    state: str,
    armed: str,
    happened: str,
    facts: str,
    line: str,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """A Director line: done as said, whatever it takes."""
    user = texts.fill(
        "keeper.direction", state=state, armed=armed, happened=happened, facts=facts, line=line
    )
    return [
        PromptMessage(
            role="system", parts=(ContentPart(text=_system(texts, world), cache_breakpoint=True),)
        ),
        PromptMessage(role="user", parts=(ContentPart(text=user),)),
    ]


def action_messages(
    *,
    world: str,
    state: str,
    armed: str,
    happened: str,
    facts: str,
    line: str,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """An action the rules have no verb for: a ruling, with a roll."""
    user = texts.fill(
        "keeper.action", state=state, armed=armed, happened=happened, facts=facts, line=line
    )
    return [
        PromptMessage(
            role="system", parts=(ContentPart(text=_system(texts, world), cache_breakpoint=True),)
        ),
        PromptMessage(role="user", parts=(ContentPart(text=user),)),
    ]


# --- the replies ------------------------------------------------------------------


def _ops(raw: object, dropped: list[str]) -> list[Op]:
    out: list[Op] = []
    if not isinstance(raw, list):
        return out
    for item in raw[:MAX_OPS]:
        if not isinstance(item, dict):
            dropped.append(str(item)[:60])
            continue
        item = _keyed(item)
        name = str(item.get("op") or "").strip().lower()
        if name not in OPS:
            dropped.append(name or "(no op)")
            continue
        args = {
            key: " ".join(
                (
                    ", ".join(str(v) for v in item.get(key))
                    if isinstance(item.get(key), list)
                    else str(item.get(key))
                ).split()
            )
            for key in OPS[name]
            if item.get(key) is not None
        }
        out.append(Op(op=name, args=args))
    return out


def _keyed(item: dict) -> dict:
    """An operation written with its name as the key, as Sonnet did live
    (`{"clock": 90}`, `{"go": "water tower"}`, `{"say": "Elena", "text":
    "…"}`, `{"say": {"who": …, "text": …}}`), as the `op` shape."""
    if item.get("op") is not None:
        return item
    candidates = [name for name in OPS if name in item]
    if not candidates:
        return item
    # `{"give": "a key", "note": "…"}`: "note" is an operation's name and a
    # field of give; the operation is the key that is no other candidate's
    # field (live, a transform with a note was read as the note alone).
    fields_of_others = {
        field for other in candidates for field in OPS[other] if other != candidates[0]
    } | {field for other in candidates for field in OPS[other]}
    chosen = next((c for c in candidates if c not in fields_of_others), candidates[0])
    value = item[chosen]
    out = {k: v for k, v in item.items() if k != chosen}
    if isinstance(value, dict):
        out.update(value)
    elif value is not None and not isinstance(value, bool):
        out.setdefault(OPS[chosen][0], value)
    out["op"] = chosen
    return out


def _firings(raw: object, events: Sequence[EventDef], dropped: list[str]) -> list[Firing]:
    out: list[Firing] = []
    if not isinstance(raw, list):
        return out
    titles = {e.title.strip().casefold(): e for e in events}
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("event") or "").split())
        event = titles.get(title.casefold())
        if event is None:
            dropped.append(f"event {title!r}")
            continue
        variant = " ".join(str(item.get("variant") or "").split())
        names = {v.title.casefold(): v.title for v in event.variants}
        variant = names.get(variant.casefold(), "" if not event.variants else variant)
        if event.variants and variant not in names.values():
            # A variant it made up: the first that fits is the program's choice.
            variant = ""
        out.append(
            Firing(event=event.title, variant=variant, script=_ops(item.get("script"), dropped))
        )
    return out


def parse_keeper(text: str, events: Sequence[EventDef]) -> KeeperReply:
    """A moment's or a Direction's reply, held to the operations and the
    plot's events. Raises ValueError on a reply with no JSON object."""
    reply = extract_json(text, dict, "the keeper's script")
    dropped: list[str] = []
    return KeeperReply(
        fired=_firings(reply.get("fire"), events, dropped),
        script=_ops(reply.get("script"), dropped),
        note=" ".join(str(reply.get("note") or "").split()),
        dropped=dropped,
    )


def parse_ruling(text: str, events: Sequence[EventDef]) -> Ruling:
    """An unusual action's reply."""
    reply = extract_json(text, dict, "the keeper's ruling")
    dropped: list[str] = []
    roll = reply.get("roll")
    skill, difficulty = "", ""
    if isinstance(roll, dict):
        skill = " ".join(str(roll.get("skill") or "").split())[:40]
        difficulty = str(roll.get("difficulty") or "").strip().lower()
        if difficulty not in DIFFICULTIES:
            difficulty = {"normal": "standard", "medium": "standard", "very hard": "desperate"}.get(
                difficulty, "standard"
            )
    return Ruling(
        possible=bool(reply.get("possible", True)),
        skill=skill,
        difficulty=difficulty,
        success=_ops(reply.get("success"), dropped),
        failure=_ops(reply.get("failure"), dropped),
        note=" ".join(str(reply.get("note") or "").split()),
        fired=_firings(reply.get("fire"), events, dropped),
        dropped=dropped,
    )


def variant_for(event: EventDef, title: str, fitting: Sequence[EventVariant]) -> EventVariant:
    """The variant a firing names, else the first fitting, else a bare one."""
    for variant in event.variants:
        if variant.title == title and title:
            return variant
    if fitting:
        return fitting[0]
    return event.variants[0] if event.variants else EventVariant()
