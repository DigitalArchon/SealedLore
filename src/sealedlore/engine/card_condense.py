"""A character's card in fewer words, for a cast too large for its share.

Every cast card is sent in the cached system block on every turn, so a cast
whose cards don't fit their share of the context budget is sent with some of
them condensed (engine/cast_fit.py). The condensed card is written once by
the authoring model from the card's own words and kept on the character
(`Character.condensed`), with a fingerprint of what it was written from, so
an edit to the card shows it is out of date rather than sending an old one.

Pure: the messages, the target length, the fingerprint and the reply. The
session makes the call (`StorySession.condense_card`).
"""

from __future__ import annotations

import hashlib
import json

from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.rules import full_description_text
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.character import Character

# A condensed card's length, in words: a share of what it condenses, never
# under the least (a short card would lose its point) nor past the most.
CONDENSE_SHARE = 1 / 3
CONDENSE_LEAST_WORDS = 150
CONDENSE_MOST_WORDS = 450
# Room for a model that reasons first.
CONDENSE_MAX_TOKENS = 4_000


def card_text(character: Character) -> str:
    """What a condensed card stands for: the description (with a card's
    personality after it) and the voice notes, which for an imported card
    are its example messages. The name, aliases, canon, summary and skills
    are sent beside a condensed card as they are."""
    parts = []
    description = full_description_text(character)
    if description:
        parts.append(description)
    if character.voice_notes and character.voice_notes.strip():
        parts.append(character.voice_notes.strip())
    return "\n\n".join(parts)


def fingerprint(character: Character) -> str:
    """Of everything a condensed card is written from: a change to any of it
    makes the condensed card out of date."""
    source = {
        "name": character.name,
        "aliases": character.aliases,
        "canon": character.canon,
        "summary": character.summary,
        "description": character.full_description,
        "personality": character.personality,
        "voice": character.voice_notes,
    }
    return hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()


def target_words(character: Character) -> int:
    words = len(card_text(character).split())
    return max(CONDENSE_LEAST_WORDS, min(CONDENSE_MOST_WORDS, round(words * CONDENSE_SHARE)))


def build_condense_messages(
    character: Character,
    *,
    player: str | None = None,
    words: int | None = None,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """`player`: the author's character, when the story has one other than
    this character (the card may say how they stand with them)."""
    lines = [f"Name: {character.name}"]
    if character.aliases:
        lines.append("Also known as: " + ", ".join(character.aliases))
    if character.canon and character.canon.strip():
        lines.append(f"From: {character.canon.strip()}")
    if character.summary and character.summary.strip():
        lines.append(f"Summary: {character.summary.strip()}")
    description = full_description_text(character)
    if description:
        lines.append(f"Description:\n{description}")
    if character.voice_notes and character.voice_notes.strip():
        lines.append(f"Voice and example messages:\n{character.voice_notes.strip()}")
    system = texts.fill(
        "characters.condense",
        words=str(words or target_words(character)),
        player=player or texts["characters.condense.no_player"],
    )
    return [
        PromptMessage(role="system", parts=(ContentPart(text=system),)),
        PromptMessage(
            role="user",
            parts=(
                ContentPart(
                    text=texts.fill(
                        "characters.condense.card", name=character.name, card="\n\n".join(lines)
                    )
                ),
            ),
        ),
    ]


def parse_condensed(text: str, character: Character) -> str:
    """The condensed card as the storyteller will read it. ValueError for a
    reply with nothing in it."""
    cleaned = text.strip()
    # A model that heads its reply with the name anyway: the cast sheet
    # already names them.
    first, _, rest = cleaned.partition("\n")
    if rest and first.strip("#*: ").strip().lower() == character.name.strip().lower():
        cleaned = rest.strip()
    if not cleaned:
        raise ValueError("the model wrote nothing")
    return cleaned
