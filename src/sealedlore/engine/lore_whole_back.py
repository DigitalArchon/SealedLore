"""The parts of an always-on entry sent condensed that the turn is about,
given with the turn from the entry's whole text.

When always-on lore is more than its room, the least important entries stand
in the cached system block as their condensed versions
(engine/lore_condense.py), and what those leave out (a voice line, a detail
between two people) is gone from every turn. The author (Nov 23 2026): bring
the whole text back when the turn is about it, by embeddings rather than
keywords, which would bring it back nearly every turn.

Whole entries were too broad, live (Nov 23 2026): ranked against the turn,
the longest, broadest entries of a card came back on nearly every turn
whatever it was about. So the whole texts are ranked by their parts: each
part of each entry sent condensed (`pieces`: `retrieval.entry_parts`, a part
of fewer than `PART_LEAST_WORDS` with the part after it, one over
`PART_MOST_WORDS` cut at its line and sentence ends into pieces of about the
same size) is an item of its own in the turn's ranking (`retrieval.
turn_ranking`, similarity fused with keyword scoring, by rank, never a
threshold on the score), beside the entries chosen per turn and the always-on
entries sent whole, which compete for a place though nothing of theirs is
added: a part must be among the turn's best matches of all the lore, not
just the best of the parts.

The parts ranked within the first `WHOLE_BACK_TOP` go with the turn, in the
tail's lore, under their entry's title (prompt text `turn.lore.from_full`),
in the entry's own order, a gap mark between parts that aren't next to each
other, as `retrieval.fitted_entry` marks them; a part the condensed version
holds word for word is not sent again. The condensed version stays where it
stands: the system block never changes. Their room is their own, the room for
chosen lore (`Config.lore_cap`) up to `ROOM_MOST`, never taken from the
chosen lore's.

Sent to every storyteller but those engine/model_tuning.py leaves it out
for (Claude); the parts' vectors are made ahead of the turns
(engine/session_lore_vectors.py), and a turn before they are in ranks by
words alone.

Pure: the session ranks (engine/session_lore_condense.py) and calls this.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise

from sealedlore.engine.retrieval import (
    FIT_LEAST_TOKENS,
    GAP,
    Injection,
    _cut,
    entry_parts,
    render_entry,
)
from sealedlore.models.lore import LoreEntry

# How many of the turn's best matches a part of an entry sent condensed must
# be among to go with the turn; 0 sends nothing more. On for every storyteller
# but Claude (the author, Nov 23 2026; engine/model_tuning.py): judged blind,
# GLM 5.3 wrote clearly better with three, Claude Opus 5.5 and Sonnet 4.6 no
# better, and Sonnet's turns cost about a third more.
WHOLE_BACK_TOP = 3
# The longest a part is ranked and sent as one: a character's profile written
# as one block of attribute lines runs to many hundreds of words, about
# everything she is.
PART_MOST_WORDS = 250
# The shortest that stands alone: a part of fewer words (a heading on a line
# of its own, "CIVILIAN:") goes with the part after it, which it heads.
PART_LEAST_WORDS = 20
# The most room the parts take, whatever the room for chosen lore is: three
# parts of the longest are ~1,000 tokens.
ROOM_MOST = 1500

# Where a part may be cut: after a line, or after a sentence's end (its
# closing quotation marks and brackets with it) and the space after it.
_CUT = re.compile(r"\n|(?<=[.!?…])[\"'”’)\]*]*[ \t]+")


@dataclass(frozen=True)
class Piece:
    """One part of an entry's whole text (`pieces`): `entry` is the entry,
    names filled, `index` its place among the entry's parts."""

    entry: LoreEntry
    index: int
    text: str

    @property
    def id(self) -> str:
        return piece_id(self.entry.id, self.index)


def piece_id(entry_id: str, index: int) -> str:
    return f"{entry_id}#{index}"


def _words(text: str) -> int:
    return len(text.split())


def _split(part: str) -> list[str]:
    """`part`, longer than `PART_MOST_WORDS`, in pieces of about the same
    size cut at its lines and sentences, none past that unless one sentence
    is; a last piece shorter than `PART_LEAST_WORDS` goes with the one before
    it, though that takes it a sentence past."""
    starts = [0, *(match.end() for match in _CUT.finditer(part))]
    units = [
        (start, end) for start, end in pairwise([*starts, len(part)]) if part[start:end].strip()
    ]
    total = sum(_words(part[start:end]) for start, end in units)
    target = total / math.ceil(total / PART_MOST_WORDS)
    # Where each piece begins.
    begins = [units[0][0]]
    size = 0
    for start, end in units:
        count = _words(part[start:end])
        if size and (size + count > PART_MOST_WORDS or size >= target):
            begins.append(start)
            size = 0
        size += count
    if len(begins) > 1 and _words(part[begins[-1] :]) < PART_LEAST_WORDS:
        begins.pop()
    return [part[a:b].strip() for a, b in pairwise([*begins, len(part)])]


def pieces(entry: LoreEntry) -> list[Piece]:
    """`entry`'s whole text in the parts it is ranked and sent by: its
    paragraphs and headed blocks (`entry_parts`), one of fewer than
    `PART_LEAST_WORDS` with the part after it, each over `PART_MOST_WORDS`
    cut further."""
    joined_up: list[str] = []
    carried = ""
    for part in entry_parts(entry.content):
        part = f"{carried}\n\n{part}" if carried else part
        carried = part if _words(part) < PART_LEAST_WORDS else ""
        if not carried:
            joined_up.append(part)
    if carried:
        # The last part, short: with the one before it, if there is one.
        if joined_up:
            joined_up[-1] = f"{joined_up[-1]}\n\n{carried}"
        else:
            joined_up.append(carried)
    found: list[str] = []
    for part in joined_up:
        found.extend(_split(part) if _words(part) > PART_MOST_WORDS else [part])
    return [Piece(entry, index, text) for index, text in enumerate(found)]


def ranked_item(piece: Piece) -> LoreEntry:
    """`piece` as an item of the turn's ranking: under its entry's title,
    which carries what the part assumes, without its keywords, which every
    part of the entry would share."""
    return piece.entry.model_copy(
        update={"id": piece.id, "content": piece.text, "keywords": [], "enabled": True}
    )


def _flat(text: str) -> str:
    return " ".join(text.split())


def in_condensed(piece: Piece) -> bool:
    """Whether the condensed version holds the part word for word."""
    condensed = piece.entry.condensed_now()
    return bool(condensed) and _flat(piece.text) in _flat(condensed or "")


def joined(chosen: dict[int, str], count: int) -> str:
    """The parts chosen (index → text) in the entry's own order, a gap mark
    wherever text of the entry's `count` parts was left out between, before
    or after them."""
    out: list[str] = []
    previous = -1
    for index in sorted(chosen):
        if index != previous + 1:
            out.append(GAP)
        out.append(chosen[index])
        previous = index
    if previous != count - 1:
        out.append(GAP)
    return "\n\n".join(out)


# The parts taken so far: entry id → part index → (text sent, rank,
# similarity).
_Taken = dict[str, dict[int, tuple[str, int, float | None]]]


def _with(taken: _Taken, piece: Piece, text: str, rank: int, score: float | None) -> _Taken:
    entry_id = piece.entry.id
    return {**taken, entry_id: {**taken.get(entry_id, {}), piece.index: (text, rank, score)}}


def parts_back(
    condensed: Sequence[LoreEntry],
    ranked: Sequence[tuple[str, float | None]],
    *,
    top: int,
    room: int,
    count_tokens: Callable[[str], int],
    label: Callable[[str], str],
) -> list[Injection]:
    """Of the parts of `condensed` (the whole texts of the entries sent
    condensed, names filled), those ranked within the first `top` of
    `ranked` (the turn's ranking of the whole pool, best first, parts by
    `piece_id`), fitted to `room`: one injection an entry, under `label` of
    its title, best ranked first. Best first, each whole while it fits what
    is left, else cut at a sentence when `FIT_LEAST_TOKENS` are left for it,
    else left out."""
    if top <= 0 or room <= 0 or not condensed:
        return []
    by_entry = {entry.id: (entry, pieces(entry)) for entry in condensed}
    found = {piece.id: piece for _, made in by_entry.values() for piece in made}

    def rendered(entry_id: str, parts: dict[int, tuple[str, int, float | None]]) -> LoreEntry:
        entry, made = by_entry[entry_id]
        text = joined({index: sent for index, (sent, _, _) in parts.items()}, len(made))
        return entry.model_copy(update={"title": label(entry.title), "content": text})

    def size(taken: _Taken) -> int:
        return sum(count_tokens(render_entry(rendered(i, parts))) for i, parts in taken.items())

    taken: _Taken = {}
    for rank, (item_id, score) in enumerate(ranked[:top], 1):
        piece = found.get(item_id)
        if piece is None or in_condensed(piece):
            continue
        whole = _with(taken, piece, piece.text, rank, score)
        if size(whole) <= room:
            taken = whole
            continue
        # What adding it costs beyond its own words (its entry's title, the
        # gap marks around it), and what is left for those.
        used = size(taken)
        around = size(whole) - used - count_tokens(piece.text)
        space = room - used - around - count_tokens(GAP) - 1
        kept = _cut(piece.text, space, count_tokens) if space >= FIT_LEAST_TOKENS else ""
        cut = _with(taken, piece, f"{kept} {GAP}", rank, score) if kept else None
        if cut is not None and size(cut) <= room:
            taken = cut

    out: list[Injection] = []
    for entry_id, parts in taken.items():
        entry, _made = by_entry[entry_id]
        sent = rendered(entry_id, parts)
        matches = tuple(sorted((rank, score) for _, rank, score in parts.values()))
        out.append(
            Injection(
                entry=sent,
                reason="in_full",
                tokens=count_tokens(render_entry(sent)),
                score=matches[0][1],
                rank=matches[0][0],
                whole=count_tokens(render_entry(entry)),
                title=entry.title,
                matches=matches,
            )
        )
    return sorted(out, key=lambda injection: injection.rank or 0)
