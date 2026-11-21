"""{{user}} and {{char}}, kept as written and filled as each request goes out
(the author, Nov 21 2026: "integrate support for them").

Text from cards and lorebooks keeps its placeholders, as SillyTavern keeps
them; what a model is sent, and what keywords are matched against, has the
names in. Who they are was playtested before it was built (SealedLoreDocs
design/lorebooks.md, "What {{char}} and {{user}} should mean"; two cards in
one story, and the author switching to play a card's own character; blind
judges): every identity confusion the other readings made traced to the wrong
name in the text. So:

- {{user}} is the story's player (`StoryPlaceholders.user_id`, "You are" when a
  story is made, else whom the author first plays): fixed, whoever is played
  later. Filled with whoever was played now, a card's "{{char}} owes {{user}}"
  read "Marcus owes Marcus" once the author took Marcus up.
- {{char}} is the text's own card: a character's own fields are theirs, a
  lorebook entry that came with a card is that card's (`LoreEntry.
  of_character`), anything else is the story's (`StoryPlaceholders.char_id`,
  the card it was made from, or `char_name` for a world card). One name for
  the whole story put the other card's history on the first ("Wrong Webb").

The old <USER> and <BOT> are read as SillyTavern reads them. Pure.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from sealedlore.engine.card_condense import fingerprint
from sealedlore.models.character import Character
from sealedlore.models.lore import LoreEntry, TavernEntry, lore_fingerprint
from sealedlore.models.story import Story

# When the story names nobody for one: never sent as written.
NO_USER = "the author's character"
NO_CHAR = "the character"
_USER = re.compile(r"\{\{\s*user\s*\}\}|<USER>", re.IGNORECASE)
_CHAR = re.compile(r"\{\{\s*char\s*\}\}|<BOT>", re.IGNORECASE)
_ANY = re.compile(r"\{\{\s*(?:user|char)\s*\}\}|<USER>|<BOT>", re.IGNORECASE)
# The fields of a lorebook entry's own settings that are keys (as `_as_fired`).
_KEY_FIELDS = ("key", "keys", "keysecondary", "secondary_keys")


def has_placeholders(text: str | None) -> bool:
    return bool(text) and _ANY.search(text) is not None


def names_for(story: Story, people: Iterable[Character]) -> Names:
    """Whom {{user}} and {{char}} name in `story` now: its player (else whom
    the author plays, else the one it suggests), and its own card (else its
    name for a world card). `people` the cast and supporting cards."""
    chosen = story.setup.placeholders
    known = {character.id: character for character in people}
    user = known.get(chosen.user_id or "")
    if user is None:
        user = known.get(story.held_character_id or "") or known.get(
            story.setup.suggested_character_id or ""
        )
    char = known.get(chosen.char_id or "")
    return Names(
        user=user.name if user is not None else NO_USER,
        char=char.name if char is not None else (chosen.char_name.strip() or NO_CHAR),
        owners=tuple((character_id, c.name) for character_id, c in known.items()),
    )


@dataclass(frozen=True)
class Names:
    """Whom the placeholders name in one story, now."""

    user: str = NO_USER
    char: str = NO_CHAR
    # A character's id → their name, for an entry that came with their card.
    owners: tuple[tuple[str, str], ...] = ()

    def fill(self, text: str | None, char: str | None = None) -> str | None:
        """`text` with {{user}} and {{char}} written out; `char` the text's
        own card's name (None: the story's)."""
        if not text or not has_placeholders(text):
            return text
        own = char or self.char
        text = _CHAR.sub(lambda _m: own, text)
        return _USER.sub(lambda _m: self.user, text)

    def char_only(self, text: str | None, char: str | None = None) -> str | None:
        """`text` with {{char}} written out and {{user}} kept: for text moving
        from where {{char}} meant one person to where it would mean another
        (a lorebook entry's words becoming someone's card)."""
        if not text or _CHAR.search(text) is None:
            return text
        return _CHAR.sub(lambda _m: char or self.char, text)

    def filler(self, char: str | None = None) -> Callable[[str], str]:
        return lambda text: self.fill(text, char) or ""

    def owner_of(self, entry: LoreEntry) -> str | None:
        """The name {{char}} has in `entry`: its card's, if it came with one."""
        if entry.of_character:
            return dict(self.owners).get(entry.of_character)
        return None

    # --- copies ------------------------------------------------------------------

    def character(self, character: Character) -> Character:
        """A character with their own text's names in: `character` itself
        when there are none to fill."""
        own = character.name
        fields = {
            name: getattr(character, name)
            for name in (
                "summary",
                "full_description",
                "personality",
                "voice_notes",
                "condensed",
                "canon",
            )
        }
        if not any(has_placeholders(value) for value in fields.values()):
            return character
        update = {name: self.fill(value, own) for name, value in fields.items()}
        copy = character.model_copy(update=update)
        if character.condensed and character.condensed_from == fingerprint(character):
            # Written from the card as it stands: still current once the
            # names are in (engine/cast_fit.py checks the fingerprint).
            copy.condensed_from = fingerprint(copy)
        return copy

    def characters(self, characters: Iterable[Character]) -> list[Character]:
        return [self.character(character) for character in characters]

    def entry(self, entry: LoreEntry) -> LoreEntry:
        """An entry with its names in, keywords and SillyTavern keys too, so
        what matches it and what is sent are alike: `entry` itself when it has
        none to fill. Its condensed version, current for the entry as written,
        stays current."""
        raw_keys = []
        if entry.tavern is not None:
            for name in _KEY_FIELDS:
                value = entry.tavern.fields.get(name)
                if isinstance(value, list):
                    raw_keys += [str(item) for item in value]
                elif isinstance(value, str):
                    raw_keys.append(value)
        texts = [entry.title, entry.content, entry.condensed or "", *entry.keywords, *raw_keys]
        if not any(has_placeholders(text) for text in texts):
            return entry
        own = self.owner_of(entry)
        fill = self.filler(own)
        content = fill(entry.content)
        shorter = entry.condensed_now()
        update = {
            "title": fill(entry.title),
            "content": content,
            "keywords": [fill(keyword) for keyword in entry.keywords],
            "condensed": fill(shorter) if shorter else entry.condensed,
            "condensed_from": lore_fingerprint(content) if shorter else entry.condensed_from,
        }
        if entry.tavern is not None:
            fields = dict(entry.tavern.fields)
            for name in _KEY_FIELDS:
                value = fields.get(name)
                if isinstance(value, list):
                    fields[name] = [fill(str(item)) for item in value]
                elif isinstance(value, str):
                    fields[name] = fill(value)
            if isinstance(fields.get("content"), str):
                fields["content"] = content
            update["tavern"] = TavernEntry(
                numbered=entry.tavern.numbered,
                fields=fields,
                decorators=list(entry.tavern.decorators),
            )
        return entry.model_copy(update=update)

    def entries(self, entries: Sequence[LoreEntry]) -> list[LoreEntry]:
        return [self.entry(entry) for entry in entries]

    def story(self, story: Story) -> Story:
        """The story's own text with its names in (the world, the notes, the
        style's): `story` itself when it has none to fill."""
        style = story.style
        texts = (story.world_bible, story.storyteller_notes, style.notes, style.custom_text)
        if not any(has_placeholders(text) for text in texts):
            return story
        return story.model_copy(
            update={
                "world_bible": self.fill(story.world_bible),
                "storyteller_notes": self.fill(story.storyteller_notes),
                "style": style.model_copy(
                    update={
                        "notes": self.fill(style.notes),
                        "custom_text": self.fill(style.custom_text),
                    }
                ),
            }
        )
