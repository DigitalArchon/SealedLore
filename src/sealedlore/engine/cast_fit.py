"""Which cast cards are sent whole, condensed or short.

Every main cast card is sent in the cached system block on every turn. While
they fit their room (`Config.cast_cap`: a share of the context budget, or a
fixed number of tokens) they all go whole. Past it, cards are condensed one
at a time, the least needed first, until the cast fits:

- a card the author keeps whole (`Character.keep_whole`) never;
- otherwise, by who the storyteller writes: one it never writes
  (`author_only`) first, then one the author may play, and one the
  storyteller voices outright (not playable) last;
- among those, the largest card first, so a long imported card gives way
  before a hand-written one of a few lines; a card whose condensed text
  would save nothing is passed over.

A card is sent condensed only with a condensed text written from the card
as it is now (`card_condense.fingerprint`). Until one is written the card is
listed as waiting and sent as `waiting_form` says: whole in a story's turns
(a condensed text is on its way, and the whole card is the better one
meanwhile), short in a private scene (nothing is written while one is open,
and its budget is small). Which cards are to be condensed is decided with an
estimate of each missing condensed text's size, so they are all asked for
at once rather than one round at a time.

If the cast is still over its room with every card it can condense
condensed, cards are sent short (name, summary, voice notes and skills, what
every card was sent as past the limit until Nov 2026) in the same order,
where that is smaller, and the fit says so.

Pure, and a function of the cast and the room alone, never of the turn, so
the system block keeps its shape from one turn to the next (the cache).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from sealedlore.engine.card_condense import fingerprint, target_words
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.rules import CardForm, render_cast_block, render_cast_sheet
from sealedlore.models.character import Character

# Tokens a condensed card's word takes, for the estimate (English prose
# runs at about this).
TOKENS_PER_WORD = 1.4


@dataclass(frozen=True)
class CastFit:
    cap: int
    # The cast block with every card whole, and as it is sent.
    whole_tokens: int
    sent_tokens: int
    forms: Mapping[str, CardForm] = field(default_factory=dict)
    # Cards to be sent condensed that have no condensed text up to date
    # yet: sent as the fit's waiting form until one is written.
    waiting: tuple[str, ...] = ()

    @property
    def over(self) -> bool:
        """The cards whole don't fit their room."""
        return self.whole_tokens > self.cap

    @property
    def still_over(self) -> bool:
        """Even as sent, the cast is over its room."""
        return self.sent_tokens > self.cap

    def ids(self, form: CardForm) -> list[str]:
        return [cid for cid, sent in self.forms.items() if sent == form]


def has_condensed(character: Character) -> bool:
    """A condensed text written from the card as it is now."""
    return bool(
        character.condensed
        and character.condensed.strip()
        and character.condensed_from == fingerprint(character)
    )


def _tier(character: Character) -> int:
    """The order cards give way in: lower first."""
    if character.author_only:
        return 0
    if character.is_player_available:
        return 1
    return 2


def condense_order(cast: Sequence[Character], count: Callable[[str], int]) -> list[Character]:
    """The cards that may be condensed, in the order they give way: the
    least needed first, the largest first among equals, then the cast's own
    order. A card kept whole is never among them."""
    sizes = {c.id: count(render_cast_sheet(c)) for c in cast}
    position = {c.id: index for index, c in enumerate(cast)}
    return sorted(
        (c for c in cast if not c.keep_whole),
        key=lambda c: (_tier(c), -sizes[c.id], position[c.id]),
    )


def _condensed_size(character: Character, count: Callable[[str], int]) -> int:
    if has_condensed(character):
        return count(render_cast_sheet(character, "condensed"))
    # The sheet without its description or voice, and the text to come.
    bare = character.model_copy(update={"condensed": None})
    return count(render_cast_sheet(bare, "condensed")) + round(
        target_words(character) * TOKENS_PER_WORD
    )


def fit_cast(
    cast: Sequence[Character],
    cap: int,
    count: Callable[[str], int],
    *,
    waiting_form: CardForm = "whole",
    texts: PromptTexts = DEFAULT_TEXTS,
) -> CastFit:
    forms: dict[str, CardForm] = {c.id: "whole" for c in cast}
    whole = count(render_cast_block(cast, forms, texts=texts))
    if whole <= cap:
        return CastFit(cap, whole, whole, forms)

    # Planned with each sheet's own size (a block's tokens are their sum,
    # near enough), the missing condensed texts estimated.
    size: dict[str, dict[CardForm, int]] = {
        c.id: {
            "whole": count(render_cast_sheet(c, "whole")),
            "short": count(render_cast_sheet(c, "short")),
            "condensed": _condensed_size(c, count),
        }
        for c in cast
    }
    order = condense_order(cast, count)
    total = whole
    for character in order:
        if total <= cap:
            break
        saving = size[character.id]["whole"] - size[character.id]["condensed"]
        if saving > 0:
            forms[character.id] = "condensed"
            total -= saving
    for character in order:
        if total <= cap:
            break
        now = size[character.id][forms[character.id]]
        if size[character.id]["short"] < now:
            forms[character.id] = "short"
            total -= now - size[character.id]["short"]

    by_id = {c.id: c for c in cast}
    waiting = tuple(
        cid for cid, form in forms.items() if form == "condensed" and not has_condensed(by_id[cid])
    )
    for cid in waiting:
        forms[cid] = waiting_form
    sent = count(render_cast_block(cast, forms, texts=texts))
    return CastFit(cap, whole, sent, forms, waiting)
