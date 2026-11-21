"""Shorter versions of always-on lore, and which of them a story sends.

Always-on entries go into the cached system block whole, every turn: a card
of long constants (19,000 tokens in one the author imported, Nov 2026) takes
a third of a 60k prompt before the story says a word. The author's way out
(Nov 21 2026): a condensed version written beside an entry, on the author's
word, never in its place; and when the always-on entries are more than the
story's lore budget holds, the condensed versions of as few as are needed
are sent, least important first, so the most important stay whole.

Importance is the lorebook's own: under SillyTavern's rules an entry's order
(the lowest is read last, and goes first); under SealedLore's its priority;
and among equals, the lower in the list the less important.

Which entries go condensed depends on the lore and the budget alone, never
on the turn: the system block keeps its shape, and its cache, from turn to
turn (an edit or a change of budget is a new block anyway).

Pure: the messages, the reply read, the fit. The session makes the calls
(engine/session_lore_condense.py).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.lore import LoreEntry

# An entry shorter than this saves too little to be worth a call.
CONDENSE_FROM_WORDS = 150
# A condensed version's length: half the entry, never under the least (a
# short one loses its point). Measured on the author's card (13 always-on
# entries, 12,970 words; GLM 5.3 writing, an Opus judge against the
# originals, Nov 21 2026): a third capped at 800 words kept about 75% of
# what matters, 7 entries safe to send, 2 not (its two longest, cut to under
# a third, lost a character sheet's voices and a scenario's detail, and a
# rules entry was reworded into other rules); half with no cap kept about
# 82%, 10 safe and none unsafe, every instruction kept.
CONDENSE_SHARE = 1 / 2
CONDENSE_LEAST_WORDS = 60
# Room for a model that reasons first, and for what a long entry's half takes
# (about 1.4 tokens a word): a reply cut short is never kept.
CONDENSE_MAX_TOKENS = 4_000


def worth_condensing(content: str) -> bool:
    return len(content.split()) >= CONDENSE_FROM_WORDS


def target_words(content: str) -> int:
    return max(CONDENSE_LEAST_WORDS, round(len(content.split()) * CONDENSE_SHARE))


def reply_tokens(content: str) -> int:
    """The most a condensed version's reply may take: room to reason, and
    the words asked for with some over (GLM 5.3 wrote up to twice the words
    asked for at a third, Nov 21 2026)."""
    return CONDENSE_MAX_TOKENS + round(target_words(content) * 2.8)


def build_lore_condense_messages(
    title: str, content: str, *, words: int | None = None, texts: PromptTexts = DEFAULT_TEXTS
) -> list[PromptMessage]:
    system = texts.fill("lore.condense", words=str(words or target_words(content)))
    if _PLACEHOLDER.search(content):
        system += "\n\n" + texts["lore.condense.placeholders"]
    entry = texts.fill("lore.condense.entry", title=title or "(untitled)", content=content.strip())
    return [
        PromptMessage(role="system", parts=(ContentPart(text=system),)),
        PromptMessage(role="user", parts=(ContentPart(text=entry),)),
    ]


_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def unasked_placeholders(text: str, source: str, user: str) -> str:
    """`text` with the {{user}} and {{char}} its `source` hasn't written out:
    a condensed version is sent as it is, and a placeholder the condenser
    made up would reach the storyteller as written. {{user}} becomes `user`
    (the author's character, else "the user"), {{char}} "the character"."""
    had = {name.lower() for name in _PLACEHOLDER.findall(source)}

    def written(match: re.Match[str]) -> str:
        name = match.group(1).lower()
        if name in had or name not in ("user", "char"):
            return match.group(0)
        return user if name == "user" else "the character"

    return _PLACEHOLDER.sub(written, text)


def parse_condensed_lore(text: str, title: str) -> str:
    """The condensed version as it will be sent. ValueError for a reply with
    nothing in it, or none shorter than what it condenses is checked by the
    caller."""
    cleaned = text.strip()
    # A model that heads its reply with the title anyway: the entry has it.
    first, _, rest = cleaned.partition("\n")
    if rest and first.strip("#*: ").strip().casefold() == title.strip().casefold():
        cleaned = rest.strip()
    if not cleaned:
        raise ValueError("the model wrote nothing")
    return cleaned


@dataclass(frozen=True)
class StandingFit:
    """The always-on entries as sent: `entries` in their own order, some as
    their condensed versions (`condensed`, by id); `whole` the tokens they
    take whole, `sent` as sent, `room` the lore budget they are held to."""

    entries: tuple[LoreEntry, ...]
    condensed: tuple[str, ...]
    whole: int
    sent: int
    room: int

    @property
    def over(self) -> bool:
        return self.sent > self.room


def fit_standing(
    standing: Sequence[LoreEntry],
    *,
    room: int,
    count_tokens: Callable[[str], int],
    least_first: Sequence[str],
    use_condensed: bool = True,
) -> StandingFit:
    """`standing` held to `room` tokens by condensed versions: none while it
    fits; past it, the least important entry with a condensed version
    (`least_first`, entry ids) goes condensed, then the next, until it fits
    or there are none left. An entry whose condensed version saves nothing,
    or is out of date, stays whole."""
    whole = {entry.id: count_tokens(_rendered(entry.title, entry.content)) for entry in standing}
    total = sum(whole.values())
    sent = total
    chosen: dict[str, str] = {}
    if use_condensed and total > room:
        by_id = {entry.id: entry for entry in standing}
        for entry_id in least_first:
            if sent <= room:
                break
            entry = by_id.get(entry_id)
            shorter = entry.condensed_now() if entry is not None else None
            if not shorter:
                continue
            saving = whole[entry_id] - count_tokens(_rendered(entry.title, shorter))
            if saving <= 0:
                continue
            chosen[entry_id] = shorter
            sent -= saving
    entries = tuple(
        entry.model_copy(update={"content": chosen[entry.id]}) if entry.id in chosen else entry
        for entry in standing
    )
    return StandingFit(
        entries=entries,
        condensed=tuple(entry.id for entry in standing if entry.id in chosen),
        whole=total,
        sent=sent,
        room=room,
    )


def _rendered(title: str, content: str) -> str:
    # As retrieval.render_entry renders one, so the count is what is sent.
    return f"## {title}\n{content.strip()}"


def least_important_first(
    entries: Sequence[LoreEntry], importance: Callable[[LoreEntry], float]
) -> list[str]:
    """Entry ids, least important first: by `importance` (higher matters
    more), and among equals the lower in the list first."""
    place = {entry.id: at for at, entry in enumerate(entries)}
    ordered = sorted(entries, key=lambda e: (importance(e), -place[e.id]))
    return [entry.id for entry in ordered]
