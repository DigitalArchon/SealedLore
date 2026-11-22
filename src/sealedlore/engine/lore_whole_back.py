"""An always-on entry sent condensed, given in full with the turn that is
about it.

When always-on lore is more than its room, the least important entries stand
in the cached system block as their condensed versions
(engine/lore_condense.py), and what those leave out (a voice line, a detail
between two people) is gone from every turn. The author (Nov 23 2026): bring
the whole text back when the turn is about it, by embeddings rather than
keywords, which would bring it back nearly every turn.

So each turn the lore is ranked against the author's turn by the ranking lore
already uses (`retrieval.turn_ranking`: similarity fused with keyword scoring
over each entry's whole text, by reciprocal rank), every entry the models may
see in the pool: the entries chosen per turn, the always-on ones sent whole,
and the whole texts of those sent condensed. A condensed entry whose whole
text ranks within the first `WHOLE_BACK_TOP` goes in full with the turn, in
the tail's lore, beside its condensed version, which stays where it stands:
the system block never changes. By rank, never by a similarity threshold:
similarity here is not on any fixed scale (measured: relevant entries scored
0.48-0.60, irrelevant ones 0.31-0.56).

The always-on entries sent whole are ranked too, though nothing of theirs is
added: the condensed entry must be among the turn's best matches of all the
lore, not just the best of the condensed ones. Ranked among those alone, a
card whose lore is all always-on would bring back the first two condensed
entries every turn, whatever the turn was about.

Their room is their own, the size of the room for chosen lore
(`Config.lore_cap`): best first, each whole while it fits what is left, else
its parts the turn is about most (`retrieval.fitted_entry`). They are never
taken from the chosen lore's room, which would crowd out what the turn chose.

Pure: the session ranks (engine/session_lore_condense.py) and calls this.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from sealedlore.engine.retrieval import Injection, fit_lore, render_entry
from sealedlore.models.lore import LoreEntry

# How many of the turn's best matches an entry sent condensed must be among
# to go in full with the turn; 0 sends nothing more (the default until a live
# test says it plays better).
WHOLE_BACK_TOP = 0


def whole_back(
    condensed: Sequence[LoreEntry],
    ranked: Sequence[tuple[str, float | None]],
    *,
    top: int,
    room: int,
    query: str,
    count_tokens: Callable[[str], int],
    label: Callable[[str], str],
) -> tuple[list[Injection], list[Injection]]:
    """Of `condensed` (the whole texts of the entries sent condensed), those
    ranked within the first `top` of `ranked` (the turn's ranking of the
    whole pool, best first), fitted to `room`: (sent, left out). Each is sent
    under `label` of its title (what says it is given in full), best first."""
    if top <= 0 or room <= 0 or not condensed:
        return [], []
    by_id = {entry.id: entry for entry in condensed}
    chosen: list[Injection] = []
    for rank, (entry_id, score) in enumerate(ranked[:top], 1):
        entry = by_id.get(entry_id)
        if entry is None:
            continue
        sent = entry.model_copy(update={"title": label(entry.title)})
        chosen.append(
            Injection(
                entry=sent,
                reason="in_full",
                tokens=count_tokens(render_entry(sent)),
                score=score,
                rank=rank,
                title=entry.title,
            )
        )
    # The first takes the whole room if it needs it: one entry the turn is
    # about most, whole, over two halves.
    return fit_lore(chosen, room, query=query, count_tokens=count_tokens, most_share=1.0)
