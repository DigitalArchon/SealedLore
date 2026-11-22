"""What the story suggests for its lore and cards (the author, Nov 20 2026,
after seeing other roleplay apps keep their lorebooks and cards up to date
from the story): read from prose as it is archived, or on demand, and kept
as suggestions the author applies, changes or turns down.

Three kinds (`models/story_suggestion.py`):

- a new lore entry, for what the story has made part of its world;
- an entry's new text, where the story has made it untrue;
- words to add to a character's card, for a lasting change.

The reader is shown only what the prose touches: every visible entry's title,
the text of those the prose names, the cards of the characters it names, and
what was already suggested or turned down. Never a plot-hidden card or entry,
nor one held back until mentioned (the caller passes only what models may
see). The reply is held to that: a change to an entry or card not shown, a
new entry under a title that exists or was turned down, an empty or unchanged
text, are dropped.

What is shown has a size (`shown_lore`, `shown_cards`): the entries and cards
the prose names, whole, while they fit; one that doesn't fit isn't shown, and
so can't be changed (shown in part, its new text would lose the rest). On a
320-entry lorebook of long entries the request was many times the prose, on
every archive run.

Pure: the messages and the reply read. The session makes the call
(engine/session_story_suggestions.py).
"""

from __future__ import annotations

from collections.abc import Sequence

from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.retrieval import mentions_entry
from sealedlore.engine.validators import fold_apostrophes, mentions, name_forms
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.character import Character
from sealedlore.models.lore import LoreEntry
from sealedlore.models.story_suggestion import StorySuggestion

# At most this many of each kind from one reading.
MOST_OF_EACH = 5
# The most of the entries' text and of the cards' a reading is shown, in
# characters (about 10,000 and 5,000 tokens): far more than a stretch of
# story names in a story with a lorebook of ordinary size, where nothing is
# left out.
LORE_CHARS = 40_000
CARDS_CHARS = 20_000
# What a reply may hold: past these it isn't a title, a reason or an entry.
TITLE_CHARS = 200
WHY_CHARS = 600
CONTENT_CHARS = 20_000
KEYWORD_CHARS = 80
# A card's addition past this is not a sentence or two.
CARD_WORDS_MAX = 120
KEYWORDS_MAX = 8


def decline_key(suggestion: StorySuggestion) -> str:
    """What a suggestion turned down is remembered by, so a later reading
    doesn't offer it again: a new entry by its title, a change by what it
    changes and its text."""
    if suggestion.kind == "lore_new":
        return f"lore:{suggestion.title.strip().casefold()}"
    return f"{suggestion.kind}:{suggestion.target_id}:{_squeeze(suggestion.content)[:200]}"


def _squeeze(text: str) -> str:
    return " ".join(text.split()).casefold()


# The setting shown, at most: enough for any setting the author wrote.
SETTING_CHARS = 6000


def _fitting(items: Sequence, text_of, most: int) -> list:  # noqa: ANN001
    """`items` in their order, each whole while they fit `most` characters."""
    kept, used = [], 0
    for item in items:
        size = len(text_of(item))
        if used + size <= most:
            kept.append(item)
            used += size
    return kept


def shown_lore(prose: str, lore: Sequence[LoreEntry]) -> list[LoreEntry]:
    """The entries whose text a reading of `prose` is shown: those it names,
    whole, in the lorebook's order, while they fit."""
    named = [e for e in lore if mentions_entry(prose, e)]
    return _fitting(named, lambda e: e.content, LORE_CHARS)


def shown_cards(prose: str, cards: Sequence[Character]) -> list[Character]:
    """The cards a reading of `prose` is shown: of the characters it names."""
    return _fitting([c for c in cards if mentions(prose, c)], _card_text, CARDS_CHARS)


def build_suggestion_messages(
    prose: str,
    *,
    lore: Sequence[LoreEntry],
    cards: Sequence[Character],
    setting: str = "",
    pending: Sequence[StorySuggestion] = (),
    declined_titles: Sequence[str] = (),
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """The request: the rules, then the setting and what the prose touches,
    then the prose. `lore` and `cards` are what models may see (visible,
    enabled). The setting, so what the world already says isn't suggested
    as lore (live, GLM 5.3 offered the town the whole story is set in)."""
    body: list[str] = []
    if setting.strip():
        shown = setting.strip()
        if len(shown) > SETTING_CHARS:
            shown = shown[:SETTING_CHARS].rsplit(" ", 1)[0] + " …"
        body.append("THE STORY'S SETTING:\n" + shown)
    if lore:
        body.append("THE LOREBOOK'S ENTRIES (titles):\n" + "\n".join(f"- {e.title}" for e in lore))
        named = shown_lore(prose, lore)
        if named:
            body.append(
                "ENTRIES THE PROSE NAMES, AS THEY STAND:\n\n"
                + "\n\n".join(f"### {e.title}\n{e.content.strip()}" for e in named)
            )
    else:
        body.append("THE LOREBOOK'S ENTRIES: none yet.")
    named_cards = shown_cards(prose, cards)
    if named_cards:
        body.append(
            "THE CARDS OF THE CHARACTERS THE PROSE NAMES:\n\n"
            + "\n\n".join(f"### {c.name}\n{_card_text(c)}" for c in named_cards)
        )
    already = [f"- {s.title}" for s in pending] + [f"- {t}" for t in declined_titles]
    if already:
        body.append(texts["suggest_changes.already"] + "\n" + "\n".join(already))
    body.append("THE PROSE:\n\n" + prose.strip())
    return [
        PromptMessage(role="system", parts=(ContentPart(text=texts["suggest_changes.rules"]),)),
        PromptMessage(role="user", parts=tuple(ContentPart(text=part) for part in body)),
    ]


def _card_text(card: Character) -> str:
    parts = [card.summary or "", card.full_description or ""]
    return "\n".join(part.strip() for part in parts if part and part.strip()) or "(no description)"


def parse_story_suggestions(
    text: str,
    *,
    lore: Sequence[LoreEntry],
    cards: Sequence[Character],
    pending: Sequence[StorySuggestion] = (),
    declined: Sequence[str] = (),
    reserved_names: Sequence[str] = (),
    node_ids: Sequence[str] = (),
    prose: str | None = None,
) -> list[StorySuggestion]:
    """The reply's suggestions that hold up. Raises ValueError when the
    reply holds no JSON object. `reserved_names`: titles and names a new
    entry may not take (a hidden card's or entry's: the reply never saw
    them, but a new entry under one would stand beside it). `prose`: what
    was read, so a change is kept only to an entry or a card the reading was
    shown (an entry's new text written without its old one would lose all
    that was still true); None holds a change to any of `lore` and `cards`."""
    data = extract_json(text, dict, "object of suggestions")
    titles = [e.title.strip().casefold() for e in lore]
    changeable = lore if prose is None else shown_lore(prose, lore)
    # By its title, where one entry alone has it: never a guess between two.
    by_title = {
        e.title.strip().casefold(): e
        for e in changeable
        if titles.count(e.title.strip().casefold()) == 1
    }
    cards_shown = cards if prose is None else shown_cards(prose, cards)
    taken = set(titles) | {name.strip().casefold() for name in reserved_names}
    # People are never lore entries: a new entry under a card's name is the
    # card's business (live, GLM 5.3 wrote one for a warlord).
    taken |= {form.casefold() for card in cards for form in name_forms(card)}
    taken |= {s.title.strip().casefold() for s in pending if s.kind == "lore_new"}
    refused = set(declined) | {decline_key(s) for s in pending}
    found: list[StorySuggestion] = []
    # One change to an entry or a card a reading (the judges, Nov 20 2026:
    # the same card suggested twice in a stretch, each a little different).
    changed = {s.target_id for s in pending if s.target_id}

    def keep(suggestion: StorySuggestion) -> None:
        key = decline_key(suggestion)
        if key in refused or (suggestion.target_id and suggestion.target_id in changed):
            return
        refused.add(key)
        if suggestion.target_id:
            changed.add(suggestion.target_id)
        found.append(suggestion)

    for item in _items(data, "lore_new"):
        title, content = _text(item.get("title")), _text(item.get("content"))
        if not title or not content or title.casefold() in taken:
            continue
        if len(title) > TITLE_CHARS or len(content) > CONTENT_CHARS:
            continue
        taken.add(title.casefold())
        keywords = item.get("keywords")
        keep(
            StorySuggestion(
                kind="lore_new",
                title=title,
                keywords=[
                    k.strip()
                    for k in (keywords if isinstance(keywords, list) else [])
                    if isinstance(k, str) and k.strip() and len(k.strip()) <= KEYWORD_CHARS
                ][:KEYWORDS_MAX],
                content=content,
                why=_why(item),
                node_ids=list(node_ids),
            )
        )
    for item in _items(data, "lore_change"):
        entry = by_title.get(_text(item.get("title")).casefold())
        content = _text(item.get("content"))
        if entry is None or not content or _squeeze(content) == _squeeze(entry.content):
            continue
        if len(content) > CONTENT_CHARS:
            continue
        keep(
            StorySuggestion(
                kind="lore_change",
                target_id=entry.id,
                title=entry.title,
                content=content,
                why=_why(item),
                node_ids=list(node_ids),
            )
        )
    for item in _items(data, "card_change"):
        card = _card_named(_text(item.get("name")), cards)
        if card is not None and not any(card is shown for shown in cards_shown):
            continue
        addition = _text(item.get("add"))
        if card is None or not addition or len(addition.split()) > CARD_WORDS_MAX:
            continue
        if _squeeze(addition) in _squeeze(card.full_description or ""):
            continue
        keep(
            StorySuggestion(
                kind="card_change",
                target_id=card.id,
                title=card.name,
                content=addition,
                why=_why(item),
                node_ids=list(node_ids),
            )
        )
    return found


def _items(data: dict, key: str) -> list[dict]:
    items = data.get(key)
    return (
        [i for i in items if isinstance(i, dict)][:MOST_OF_EACH] if isinstance(items, list) else []
    )


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _why(item: dict) -> str:
    said = _text(item.get("why"))
    return said if len(said) <= WHY_CHARS else said[: WHY_CHARS - 1].rstrip() + "…"


def _as_named(name: str) -> str:
    """A name for comparing: any case, whichever apostrophe it was written with."""
    return fold_apostrophes(name).casefold()


def _card_named(name: str, cards: Sequence[Character]) -> Character | None:
    """The card a name is: its whole name first, else the one card a name
    form answers to (never a guess between two)."""
    if not name:
        return None
    wanted = _as_named(name)
    for card in cards:
        if _as_named(card.name) == wanted:
            return card
    found = [c for c in cards if wanted in {_as_named(form) for form in name_forms(c)}]
    return found[0] if len(found) == 1 else None


def loses_much(old: str, new: str) -> bool:
    """A changed entry that keeps less than half its words: the dialog says
    so, since a careless rewrite would lose what was still true."""
    old_words = set(_squeeze(old).split())
    if len(old_words) < 12:
        return False
    kept = old_words & set(_squeeze(new).split())
    return len(kept) < len(old_words) / 2


def with_addition(description: str, addition: str) -> str:
    """A card's description with words added as their own paragraph."""
    description = (description or "").rstrip()
    return f"{description}\n\n{addition.strip()}" if description else addition.strip()
