"""A character's profile written by a model, in Your characters (the author,
Nov 19 2026): **Expand** fills in what the card is missing from what it
says; **Generate** writes the whole profile. Both take the author's wishes
("make them tougher", "a criminal version of themselves") and may go by a
world the character comes from (Hamlet, made by hand with a name, filled
in from what the play establishes), as Fill with lore characters… does for a
story's cast, for one character.

Pure: the messages, reading the reply, and what it would change, field by
field, for the author to tick (gui/card_fill.py). The session makes the call
(`StorySession.fill_card`). Not measured.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.character import Character
from sealedlore.models.tavern import TavernFields

FillMode = Literal["expand", "generate"]
EXPAND: FillMode = "expand"
GENERATE: FillMode = "generate"
# Room for a whole profile with its greeting and example messages, and a
# model that reasons first.
CARD_FILL_MAX_TOKENS = 8_000

# The fields a fill writes, in the card's order, with how the form names them.
FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "Name"),
    ("aliases", "Aliases"),
    ("canon", "From (a published work)"),
    ("summary", "Summary"),
    ("description", "Description"),
    ("personality", "Personality"),
    ("voice", "Voice"),
    ("competence", "What they can do"),
    ("scenario", "Scenario (for chats)"),
    ("greeting", "Greeting (for chats)"),
    ("examples", "Example messages (for chats)"),
    ("tags", "Tags"),
)
LABELS = dict(FIELDS)
# Fields holding a list, as text one per comma.
LISTS = ("aliases", "tags")


@dataclass(frozen=True)
class FieldChange:
    key: str
    label: str
    old: str
    new: str


@dataclass
class CardFill:
    fields: dict[str, str] = field(default_factory=dict)
    # What the model said the author should know.
    notes: str = ""


def card_view(character: Character) -> dict[str, str]:
    """Every field a fill writes, as the card has it now ("" for none)."""
    tavern = character.tavern or TavernFields()
    return {
        "name": character.name or "",
        "aliases": ", ".join(character.aliases),
        "canon": character.canon or "",
        "summary": character.summary or "",
        "description": character.full_description or "",
        "personality": character.personality or "",
        "voice": character.voice_notes or "",
        "competence": character.competence.notes or "",
        "scenario": tavern.scenario,
        "greeting": tavern.first_mes,
        "examples": tavern.mes_example,
        "tags": ", ".join(tavern.tags),
    }


def build_card_fill_messages(
    character: Character,
    *,
    mode: FillMode,
    wishes: str = "",
    world: str = "",
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """The request: the card as it stands, what to do with it, the author's
    wishes and the world they come from, each only when given."""
    view = card_view(character)
    if mode == GENERATE and not view["name"].strip() and not wishes.strip() and not world.strip():
        raise ValueError("give them a name, or say what character you want")
    shown = {key: value for key, value in view.items() if value.strip()}
    parts = [
        texts.fill(
            "characters.fill.card",
            card=json.dumps(shown, ensure_ascii=False, indent=1) if shown else "{}",
        ),
        texts[f"characters.fill.{mode}"],
    ]
    if world.strip():
        parts.append(texts.fill("characters.fill.world", world=world.strip()))
    if wishes.strip():
        parts.append(texts.fill("characters.fill.wishes", wishes=wishes.strip()))
    return [
        PromptMessage(role="system", parts=(ContentPart(text=texts["characters.fill"]),)),
        PromptMessage(role="user", parts=(ContentPart(text="\n\n".join(parts)),)),
    ]


def _text(value: object, joiner: str = "\n") -> str | None:
    """A field's text: a list (some models give the example messages as one)
    joined, by commas for the fields that are lists on the card."""
    if value is None:
        return None
    if isinstance(value, list):
        return joiner.join(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return None


def parse_card_fill(reply: str) -> CardFill:
    """The model's fields, by the card's names; ValueError for a reply that
    holds no profile."""
    data = extract_json(reply, dict, "profile")
    if not isinstance(data, dict):
        raise ValueError("the reply held no profile")
    raw = data.get("character") if isinstance(data.get("character"), dict) else data
    fields = {}
    for key, _label in FIELDS:
        text = _text(raw.get(key), ", " if key in LISTS else "\n")
        if text is not None:
            fields[key] = text
    if not fields:
        raise ValueError("the reply held no profile")
    notes = data.get("notes")
    return CardFill(fields=fields, notes=notes.strip() if isinstance(notes, str) else "")


def proposed_changes(character: Character, fill: CardFill, mode: FillMode) -> list[FieldChange]:
    """What the fill would change, field by field. Expand never empties a
    field, and Generate never names someone nameless: a blank answer for a
    field leaves it as it is."""
    view = card_view(character)
    changes = []
    for key, label in FIELDS:
        new = fill.fields.get(key)
        if new is None or not new.strip():
            continue
        old = view[key]
        if _same(key, old, new):
            continue
        changes.append(FieldChange(key, label, old, new.strip()))
    return changes


def _same(key: str, old: str, new: str) -> bool:
    if key in LISTS:
        return _items(old) == _items(new)
    return old.strip() == new.strip()


def _items(text: str) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


def apply_changes(character: Character, changes: Sequence[FieldChange]) -> None:
    """The changes ticked, onto the card."""
    for change in changes:
        new = change.new
        if change.key == "name":
            character.name = new
        elif change.key == "aliases":
            character.aliases = [a for a in _items(new) if a.lower() != character.name.lower()]
        elif change.key == "canon":
            character.canon = new or None
        elif change.key == "summary":
            character.summary = new or None
        elif change.key == "description":
            character.full_description = new or None
        elif change.key == "personality":
            character.personality = new or None
        elif change.key == "voice":
            character.voice_notes = new or None
        elif change.key == "competence":
            character.competence.notes = new
        else:
            tavern = character.tavern or TavernFields()
            character.tavern = tavern
            if change.key == "scenario":
                tavern.scenario = new
            elif change.key == "greeting":
                tavern.first_mes = new
            elif change.key == "examples":
                tavern.mes_example = new
            elif change.key == "tags":
                tavern.tags = _items(new)
