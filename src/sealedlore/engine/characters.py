"""Finding the characters the model introduced, and showing their cards when relevant.

Pure: prompt building, parsing and selection. `StorySession` makes the call.

Why suggestions and not automatic cards: every cast card sits in the cached
system block and under the presence rules, and the model names every ensign.
Canonical characters need no introduction to a model that knows the canon;
what a card is for is the invented ones, whose details would otherwise vanish
once the prose that established them is archived. So the model proposes, the
author decides, and supporting cards ride in the tail only when mentioned.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.validators import mentions, name_forms
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.character import Character
from sealedlore.models.supporting import CharacterSuggestion


def known_names(characters: Iterable[Character], extra: Iterable[str] = ()) -> set[str]:
    """Every lowercased form by which an existing card, or a refused name, is known."""
    names: set[str] = set()
    for character in characters:
        names.update(form.lower() for form in name_forms(character))
    names.update(name.strip().lower() for name in extra if name.strip())
    return names


def build_extraction_messages(
    prose: str,
    *,
    on_file: Sequence[str],
    declined: Sequence[str] = (),
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    body = []
    if on_file:
        body.append(texts.fill("characters.on_file", names=", ".join(on_file)))
    if declined:
        body.append(texts.fill("characters.declined", names=", ".join(declined)))
    body.append("The passage:\n\n" + prose)
    return [
        PromptMessage(role="system", parts=(ContentPart(text=texts["characters.scan"]),)),
        PromptMessage(role="user", parts=tuple(ContentPart(text=text) for text in body)),
    ]


def _clean_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_suggestions(text: str, *, known: set[str]) -> list[CharacterSuggestion]:
    """Suggestions from the model's reply, minus anyone already known.

    Forgiving about the wrapping (code fences, a sentence before the array)
    and strict about the content: an entry with no name is dropped, and so is
    anyone whose name or alias matches a card or a refusal already on file.
    Raises ValueError when there is no JSON array at all.
    """
    items = extract_json(text, list, "list of characters")

    found: list[CharacterSuggestion] = []
    seen = set(known)
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _clean_text(item.get("name"))
        if not name:
            continue
        raw_aliases = item.get("aliases")
        aliases = [
            alias
            for alias in (
                _clean_text(a) for a in (raw_aliases if isinstance(raw_aliases, list) else [])
            )
            if alias and alias.lower() != name.lower()
        ]
        candidate = Character(name=name, aliases=aliases)
        if seen.intersection(form.lower() for form in name_forms(candidate)):
            continue
        canon = _clean_text(item.get("canon"))
        found.append(
            CharacterSuggestion(
                name=name,
                aliases=aliases,
                canon=None if canon.lower() in ("", "null", "none") else canon,
                description=_clean_text(item.get("description")),
            )
        )
        seen.update(form.lower() for form in name_forms(candidate))
    return found


def card_from_suggestion(suggestion: CharacterSuggestion) -> Character:
    return Character(
        name=suggestion.name,
        aliases=list(suggestion.aliases),
        summary=suggestion.description or None,
        canon=suggestion.canon,
        origin="model",
    )


def mentioned(characters: Sequence[Character], text: str, *, limit: int) -> list[Character]:
    """The supporting characters this text names, in card order, at most `limit`."""
    if not text.strip():
        return []
    return [character for character in characters if mentions(text, character)][:limit]


def render_supporting_block(
    characters: Sequence[Character], texts: PromptTexts = DEFAULT_TEXTS
) -> str:
    if not characters:
        return ""
    sheets = []
    for character in characters:
        heading = character.name
        if character.aliases:
            heading += " — also known as " + ", ".join(character.aliases)
        if character.canon:
            heading += f" ({character.canon})"
        lines = [heading]
        for text in (
            character.summary,
            character.voice_notes and f"Voice: {character.voice_notes}",
        ):
            if text and text.strip():
                lines.append(f"  {text.strip()}")
        sheets.append("\n".join(lines))
    return texts["turn.supporting"] + "\n\n" + "\n\n".join(sheets)
