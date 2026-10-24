"""The people of a visual story (the author, Oct 24 2026): each has a sheet
(models/visual.py `PersonSheet`, over their card), the model speaks for them
one call a line, and chooses what they do from a fixed set of intents the
program carries out on the map (engine/session_visual.py: follow, lead_to,
attack, retreat, hide, fetch_help, leave, give, stay).

Pure: the sheet and the situation as the call is given them, the reply held
to its shape, and the words the intents are reported in.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.character import Character
from sealedlore.models.visual import INTENTS, PersonSheet

WORLD_CHARS = 1500
CARD_CHARS = 900
# The conversation the call is given: the latest lines.
TALK_LINES = 24
MEMORY_LINES = 12
WITNESS_LINES = 10
NPC_MAX_TOKENS = 900


@dataclass(frozen=True)
class NpcReply:
    say: str
    mood: str
    intent: str
    place: str
    what: str
    remember: str


def sheet_of(name: str, design_people: Mapping[str, PersonSheet]) -> PersonSheet:
    """The design's sheet for a name, or an empty one."""
    for key, sheet in design_people.items():
        if key.strip().casefold() == name.strip().casefold():
            return sheet
    return PersonSheet()


def card_words(card: Character | None) -> str:
    if card is None:
        return "(no card)"
    parts = [card.summary or "", card.full_description or ""]
    if card.canon:
        parts.insert(0, f"From {card.canon}.")
    return " ".join(" ".join(parts).split())[:CARD_CHARS] or "(no card)"


def _or(text: str, fallback: str = "nothing in particular") -> str:
    return " ".join(text.split()) or fallback


def npc_messages(
    *,
    name: str,
    sheet: PersonSheet,
    card: Character | None,
    world: str,
    where: str,
    when: str,
    about: Sequence[str],
    player: str,
    player_words: str,
    witnessed: Sequence[str],
    memories: Sequence[str],
    talk: Sequence[Sequence[str]],
    line: str,
    state: Sequence[str] = (),
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """The talk call: the rules and the sheet as the system (cached per
    person), the situation and the player's words as the user message.
    `state`: the person's own state in the keeper's words (held, bound,
    hurt), as the program has it."""
    system = "\n\n".join(
        [
            texts["npc.system"],
            texts.fill(
                "npc.sheet",
                world=" ".join(world.split())[:WORLD_CHARS] or "(nothing said of it)",
                name=name,
                card=card_words(card),
                personality=_or(sheet.personality, "as the card says"),
                goals=_or(sheet.goals),
                wants=_or(sheet.wants, "to be left alone, unless they prove useful"),
                knows=_or(sheet.knows, "what anyone here would know"),
                never=_or(sheet.never, "nothing in particular"),
                speech=_or(sheet.speech, "plainly"),
            ),
        ]
    )
    lines = [f"{who}: {words}" for who, words in list(talk)[-TALK_LINES:]]
    user = texts.fill(
        "npc.situation",
        where=where,
        when=when,
        about=", ".join(about) or "nobody else",
        player=player,
        player_words=player_words or "nothing to remark on",
        state=", ".join(state) or "as usual",
        witnessed="; ".join(list(witnessed)[-WITNESS_LINES:]) or "nothing",
        memories="; ".join(list(memories)[-MEMORY_LINES:]) or "nothing yet",
        talk="\n".join(lines) or "(nothing yet)",
        line=line,
    )
    return [
        PromptMessage(role="system", parts=(ContentPart(text=system, cache_breakpoint=True),)),
        PromptMessage(role="user", parts=(ContentPart(text=user),)),
    ]


def parse_npc(text: str) -> NpcReply:
    """The reply held to its shape: words, a mood, an intent from the set
    (else "stay"), the place or thing it names, a memory line. Raises
    ValueError on a reply with no JSON object."""
    reply = extract_json(text, dict, "what the person says")
    intent = reply.get("intent")
    kind, place, what = "stay", "", ""
    if isinstance(intent, dict):
        kind = str(intent.get("kind") or "stay").strip().lower()
        place = " ".join(str(intent.get("place") or "").split())
        what = " ".join(str(intent.get("what") or "").split())
    elif isinstance(intent, str):
        kind = intent.strip().lower()
    if kind not in INTENTS:
        kind = "stay"
    return NpcReply(
        say=" ".join(str(reply.get("say") or "").split()),
        mood=" ".join(str(reply.get("mood") or "").split())[:24],
        intent=kind,
        place=place[:80],
        what=what[:80],
        remember=" ".join(str(reply.get("remember") or "").split())[:200],
    )


def intent_words(name: str, reply: NpcReply, *, player: str) -> str:
    """How the program reports what the person does ("" for staying)."""
    kind = reply.intent
    if kind == "follow":
        return f"{name} comes with you."
    if kind == "lead_to":
        return f"{name} sets off for {reply.place or 'somewhere'}, and means you to follow."
    if kind == "attack":
        return f"{name} goes for you."
    if kind == "retreat":
        return f"{name} backs away from you."
    if kind == "hide":
        return f"{name} slips out of sight."
    if kind == "fetch_help":
        return f"{name} goes to fetch others."
    if kind == "leave":
        return f"{name} walks off."
    if kind == "give":
        return f"{name} hands you {reply.what or 'something'}."
    return ""


def mood_words(mood: str) -> str:
    return mood.strip().lower()


def player_description(weapon: str, wounds: int, carrying: int) -> str:
    """What a person can see of the player."""
    parts = []
    if weapon:
        parts.append(f"carrying a {weapon} in plain sight")
    if wounds:
        parts.append("hurt" if wounds == 1 else "badly hurt")
    if carrying:
        parts.append(f"carrying {carrying} thing{'s' if carrying != 1 else ''}")
    return ", ".join(parts)
