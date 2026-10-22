"""Cards for the people of a published world, written by a model.

The author names a world (a book, a series, a myth, a period of history) and,
if they like, the characters they want for sure; the authoring model writes a
card for each and for the world's other main and recurring people, sorted
into the cast and supporting cards. A story set in a canon world otherwise
brought in the first few canon characters and seldom the rest, unless the
author had carded them by hand (the author, Oct 2026).

Pure: the messages, and turning the reply into cards. The session makes the
call (`StorySession.fill_lore_cast`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sealedlore.engine.authoring import TIERS, character_from
from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.character import Character

# How many cards one call asks for, and the most a reply may add: past it, the
# rest are dropped and the author can ask again.
MOST_PER_CALL = 16
MOST_KEPT = 24
# An alias longer than this is a note, not a name.
ALIAS_MOST_WORDS = 5
# Room for that many cards, and a model that reasons first.
LORE_CAST_MAX_TOKENS = 16_000


@dataclass
class LoreCast:
    cast: list[Character] = field(default_factory=list)
    supporting: list[Character] = field(default_factory=list)
    # Names already on file, not added again.
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # What the model said the author should know.
    notes: str = ""

    @property
    def added(self) -> int:
        return len(self.cast) + len(self.supporting)


def lore_cast_system(texts: PromptTexts = DEFAULT_TEXTS) -> str:
    return texts.fill(
        "authoring.lore_cast",
        most=str(MOST_PER_CALL),
        tiers=", ".join(f'"{tier}"' for tier in TIERS),
        character_shape=texts["authoring.character_shape"],
    )


def build_lore_cast_messages(
    lore: str,
    *,
    wanted: str = "",
    setting: str | None = None,
    known: Sequence[str] = (),
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """`known`: the names of the cards the model may be told of (never a
    plot-hidden one's)."""
    if not lore.strip():
        raise ValueError("name the world first")
    parts = [texts.fill("authoring.lore_cast.world", lore=lore.strip())]
    if wanted.strip():
        parts.append(texts.fill("authoring.lore_cast.wanted", wanted=wanted.strip()))
    if setting and setting.strip():
        parts.append(texts.fill("authoring.lore_cast.setting", setting=setting.strip()))
    if known:
        parts.append(texts.fill("authoring.lore_cast.known", names=", ".join(known)))
    return [
        PromptMessage(role="system", parts=(ContentPart(text=lore_cast_system(texts)),)),
        PromptMessage(role="user", parts=(ContentPart(text="\n\n".join(parts)),)),
    ]


def generic_alias(alias: str) -> bool:
    """An alias that would fit others too: "the king", "the spirit", "my
    lord". Live, GLM 5.3 gave Claudius "the king" and the ghost "the spirit"
    against the prompt's word; "the Grey Wolf" names one person, and stays."""
    words = alias.split()
    if len(words) > ALIAS_MOST_WORDS:
        # "the Lady of the Lake in some traditions" (Sonnet 4.6, for Morgan
        # le Fay): a note, not a name.
        return True
    if words and words[0].lower() in ("the", "a", "an", "my", "our"):
        words = words[1:]
    return len(words) <= 1 and (not words or words[0].islower()) or alias.islower()


def _names(character: Character) -> set[str]:
    return {name.casefold() for name in (character.name, *character.aliases) if name.strip()}


def parse_lore_cast(text: str, existing: Sequence[Character] = ()) -> LoreCast:
    """The cards in the reply, minus anyone already on file. A card on file
    is the same person only by a whole name or alias: a shared surname
    ("Mr. Bennet", "Mrs. Bennet") is two people. Raises ValueError when the
    reply holds no cards at all."""
    data = extract_json(text, dict, "set of cards")
    result = LoreCast()
    notes = data.get("notes")
    result.notes = notes.strip() if isinstance(notes, str) else ""
    # On file: by any of its names. Among the reply's own cards, by name
    # only: one card's stray alias must not cost another card its place.
    taken: set[str] = set()
    for character in existing:
        taken |= _names(character)
    new_names: set[str] = set()
    found = 0
    for key, playable, into in (
        ("cast", True, result.cast),
        ("supporting", False, result.supporting),
    ):
        items = data.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            found += 1
            try:
                card = character_from(item, result.warnings, playable=playable)
            except ValueError as exc:
                result.warnings.append(str(exc))
                continue
            card.aliases = [alias for alias in card.aliases if not generic_alias(alias)]
            if _names(card) & taken or card.name.casefold() in new_names:
                result.skipped.append(card.name)
                continue
            if result.added >= MOST_KEPT:
                result.warnings.append(
                    f"left out {card.name}: one request adds {MOST_KEPT} at most"
                )
                continue
            if not playable:
                # A supporting card is the storyteller's, and has no skills.
                card.is_player_available = False
                card.author_only = False
                card.competence.tiers = {}
            card.origin = "model"
            new_names.add(card.name.casefold())
            into.append(card)
    if not found:
        raise ValueError("the reply held no cards")
    # An alias that is another card's name would make the two one person.
    for card in (*result.cast, *result.supporting):
        others = {
            other.name.casefold()
            for other in (*existing, *result.cast, *result.supporting)
            if other is not card
        }
        card.aliases = [alias for alias in card.aliases if alias.casefold() not in others]
    return result
