"""Lore retrieval: what gets injected this turn, and why. See §7.

Pure functions over story state and pre-computed vectors — the embedding call
itself belongs to `StorySession`, so this module stays testable without a
network and the GUI can preview a selection on the main thread.

Selection follows §7 exactly: top-k semantic hits above a threshold, then
every `always_on` entry and every keyword match merged in, deduped, sorted by
priority, and truncated to a token cap. Keyword matching is both a supplement
and the whole story when embeddings are unconfigured or failing.

Selection is only for a lorebook too big to send whole (`lore_layout`).
Measured on a long test story: at 8 entries every entry
cached cost less than retrieval and lost nothing in play; retrieval found 0.81
of what judges called relevant, the whole book all of it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import Node

Reason = Literal["always_on", "picked", "semantic", "keyword"]
LoreMode = Literal["whole", "select"]

REASON_ORDER: dict[Reason, int] = {"always_on": 0, "picked": 1, "semantic": 2, "keyword": 3}

# How many just-missed entries to keep for the panel. Enough to see where the
# threshold is falling, not so many that the list becomes the whole lorebook.
NEAR_MISS_LIMIT = 5

# How many exchanges (the author's turn and the passage answering it) the
# keyword match reaches back when the author's own turn names no entry.
KEYWORD_LOOKBACK = 3

REASON_LABELS: dict[Reason, str] = {
    "always_on": "always on",
    "picked": "picked by the lore model",
    "semantic": "semantic match",
    "keyword": "keyword match",
}


@dataclass(frozen=True)
class Injection:
    entry: LoreEntry
    reason: Reason
    tokens: int
    score: float | None = None

    def describe(self) -> str:
        if self.reason == "semantic" and self.score is not None:
            return f"semantic match ({self.score:.2f})"
        if self.reason == "picked" and self.score is not None:
            return f"picked by Jev ({self.score:.2f})"
        return REASON_LABELS[self.reason]


@dataclass(frozen=True)
class RetrievalReport:
    """What went in, what didn't, and whether embeddings were involved."""

    injected: tuple[Injection, ...] = ()
    dropped: tuple[Injection, ...] = ()
    # Scored but not injected, best first. Live measurement showed bge-m3's
    # similarities sit in a narrow band, so the threshold has to be tuned per
    # lorebook — and that is impossible without seeing the near misses.
    near_misses: tuple[tuple[LoreEntry, float], ...] = ()
    used_embeddings: bool = False
    fallback_reason: str | None = None
    query_text: str = ""
    tokens: int = 0
    # How far back the keywords were matched: 0 is the author's turn alone,
    # N the turn and the last N exchanges (`keyword_window`).
    keyword_reach: int = 0
    # How the lorebook reached the prompt (`lore_layout`): "whole" means every
    # entry is standing in the system block and nothing was selected.
    mode: LoreMode = "select"
    standing: tuple[LoreEntry, ...] = ()
    # Who chose the entries ("picker", "jev" or "similarity"), and why the
    # author's chosen method didn't this turn when it fell back.
    selector: str = "similarity"
    selector_note: str | None = None

    @property
    def entries(self) -> tuple[LoreEntry, ...]:
        return tuple(injection.entry for injection in self.injected)

    @property
    def semantic_count(self) -> int:
        return sum(1 for injection in self.injected if injection.reason == "semantic")


def content_hash(text: str) -> str:
    """The cache key for an entry's text (§2.4, §7)."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def embedding_text(entry: LoreEntry) -> str:
    """What actually gets embedded: the title carries meaning the body assumes."""
    title = entry.title.strip()
    content = entry.content.strip()
    return f"{title}\n{content}" if title else content


def query_text(user_text: str, history: Sequence[Node], *, turns: int) -> str:
    """Current message plus the last K turns of the path (§7).

    Recent prose is what makes the query specific to the scene; the author's
    own line alone is usually too short to retrieve on.
    """
    parts = [node.content.strip() for node in history[-max(turns, 0) :] if node.content.strip()]
    if user_text.strip():
        parts.append(user_text.strip())
    return "\n\n".join(parts)


def cosine_scores(query: Sequence[float], vectors: Sequence[Sequence[float]]) -> list[float]:
    """Cosine similarity of one query against many vectors."""
    if not vectors:
        return []
    matrix = np.asarray(vectors, dtype=np.float32)
    target = np.asarray(query, dtype=np.float32)
    if matrix.ndim != 2 or target.shape[0] != matrix.shape[1]:
        raise ValueError("query and entry vectors have different dimensions")

    norms = np.linalg.norm(matrix, axis=1)
    target_norm = float(np.linalg.norm(target))
    if target_norm == 0:
        return [0.0] * len(vectors)
    # A zero-length row would divide by zero; score it 0 rather than nan.
    safe = np.where(norms == 0, 1.0, norms)
    scores = (matrix @ target) / (safe * target_norm)
    return [0.0 if norm == 0 else float(score) for score, norm in zip(scores, norms, strict=True)]


def keyword_matches(text: str, entry: LoreEntry) -> bool:
    """Whole-word, case-insensitive match on any of the entry's keywords.

    Whole-word rather than substring: "accord" should not fire on "according",
    and a lorebook full of false positives is worse than none at all.
    """
    lowered = text.lower()
    for keyword in entry.keywords:
        cleaned = keyword.strip().lower()
        if not cleaned:
            continue
        if re.search(rf"(?<!\w){re.escape(cleaned)}(?!\w)", lowered):
            return True
    return False


def mentions_entry(text: str, entry: LoreEntry) -> bool:
    """Whether a text names an entry: one of its keywords or its title, whole words."""
    if keyword_matches(text, entry):
        return True
    title = entry.title.strip().lower()
    return bool(title) and re.search(rf"(?<!\w){re.escape(title)}(?!\w)", text.lower()) is not None


def held_back(entries: Iterable[LoreEntry], nodes: Sequence[Node]) -> set[str]:
    """Entries kept back until the author names them, not yet named on this path.

    Only the author's own messages count (a turn, a Direction or Narration):
    the storyteller naming something isn't the author deciding it has come.
    """
    waiting = [entry for entry in entries if entry.until_mentioned]
    if not waiting:
        return set()
    said = [node.content for node in nodes if node.kind == "user" and node.content.strip()]
    return {entry.id for entry in waiting if not any(mentions_entry(t, entry) for t in said)}


def keyword_window(
    entries: Sequence[LoreEntry],
    turn_text: str,
    history: Sequence[Node],
    *,
    exchanges: int = KEYWORD_LOOKBACK,
) -> tuple[str, int]:
    """The text keywords are matched over, and how many exchanges back it reaches.

    The author's turn, while it names an entry. When it names none, the last
    exchange joins it, then two, then three: a turn that just carries the
    scene on still gets what the scene is about, without the recent prose
    naming half the lorebook on every turn.
    """
    live = [entry for entry in entries if entry.enabled]
    if not live or any(keyword_matches(turn_text, entry) for entry in live):
        return turn_text, 0
    prose = [node.content for node in history if node.content.strip()]
    for reach in range(1, exchanges + 1):
        text = "\n\n".join([*prose[-2 * reach :], turn_text])
        if any(keyword_matches(text, entry) for entry in live):
            return text, reach
        if 2 * reach >= len(prose):
            break
    return turn_text, 0


@dataclass(frozen=True)
class LoreLayout:
    """Where each entry goes: standing in the cached system block, or chosen per turn."""

    mode: LoreMode
    standing: tuple[LoreEntry, ...]
    selectable: tuple[LoreEntry, ...]
    # The whole lorebook's size, and the most the system block may give it.
    tokens: int
    limit: int


def lore_layout(
    entries: Iterable[LoreEntry],
    *,
    budget: int,
    share: float,
    count_tokens: Callable[[str], int],
) -> LoreLayout:
    """Send the whole lorebook while it is small; select from it once it isn't.

    Whole, it sits in the system block, cached, so a turn pays a tenth of the
    price for it and every entry is there. Past `share` of the budget it would
    crowd out the story's own prose, so entries are chosen per turn instead,
    except `always_on` ones: they are sent every turn anyway, so they stand.

    A pure function of the lorebook and the budget: the layout changes only
    when an entry is edited, revealed or the budget moves, each of which
    rewrites the system block already. Entries keep the lorebook's order,
    which is stable, so the cached prefix is too.
    """
    live = [entry for entry in entries if entry.enabled]
    total = sum(count_tokens(render_entry(entry)) for entry in live)
    limit = max(0, int(budget * share))
    if live and total <= limit:
        return LoreLayout("whole", tuple(live), (), total, limit)
    return LoreLayout(
        "select",
        tuple(entry for entry in live if entry.always_on),
        tuple(entry for entry in live if not entry.always_on),
        total,
        limit,
    )


def select_lore(
    entries: Iterable[LoreEntry],
    *,
    query: str = "",
    keyword_text: str | None = None,
    scores: dict[str, float] | None = None,
    k: int = 3,
    threshold: float = 0.35,
    token_cap: int = 2_000,
    count_tokens: Callable[[str], int],
    used_embeddings: bool = False,
    fallback_reason: str | None = None,
) -> RetrievalReport:
    """Choose the lore for one turn.

    `scores` maps entry id to cosine similarity; leave it out for the keyword
    path. Disabled entries never appear by any route.

    Keywords are matched over `keyword_text` (the session passes the author's
    turn), or over `query` when it is None. Matched over three messages of
    the story's own prose, they fired on ~39 of 100 entries a turn, because
    the names they key on are the story's everyday words, and the cap then
    kept them by title. Live on Sonnet 4.6 (16 turns, 3 takes, 100 entries),
    the author's turn alone with hits ranked by similarity was preferred by
    all three judges (30-17, 28-20, 23-22) with fewer contradictions on each.
    """
    live = [entry for entry in entries if entry.enabled]
    scores = scores or {}

    semantic: dict[str, float] = {}
    if scores:
        ranked = sorted(
            ((entry, scores[entry.id]) for entry in live if entry.id in scores),
            key=lambda pair: pair[1],
            reverse=True,
        )
        semantic = {entry.id: score for entry, score in ranked[: max(k, 0)] if score >= threshold}

    matched_over = query if keyword_text is None else keyword_text
    chosen: dict[str, Injection] = {}
    for entry in live:
        reason: Reason | None = None
        score: float | None = None
        # always_on wins the label when an entry qualifies twice: it would have
        # been injected regardless of what the query happened to look like.
        if entry.always_on:
            reason = "always_on"
        elif entry.id in semantic:
            reason, score = "semantic", semantic[entry.id]
        elif matched_over and keyword_matches(matched_over, entry):
            # Its similarity, when there is one, orders it under the cap:
            # by title, the cap kept whichever entries sorted first.
            reason, score = "keyword", scores.get(entry.id)
        if reason is None:
            continue
        chosen[entry.id] = Injection(
            entry=entry,
            reason=reason,
            tokens=count_tokens(render_entry(entry)),
            score=score,
        )

    ordered = sorted(chosen.values(), key=_sort_key)

    injected: list[Injection] = []
    dropped: list[Injection] = []
    used = 0
    for injection in ordered:
        # Skip past an entry that doesn't fit rather than stopping: lore
        # entries are independent, so one fat low-priority entry shouldn't
        # shut out the smaller ones behind it. (History works the other way —
        # a gap mid-scene reads worse than a shorter window.)
        if used + injection.tokens > token_cap:
            dropped.append(injection)
            continue
        injected.append(injection)
        used += injection.tokens

    missed = sorted(
        (
            (entry, scores[entry.id])
            for entry in live
            if entry.id in scores and entry.id not in chosen
        ),
        key=lambda pair: pair[1],
        reverse=True,
    )

    return RetrievalReport(
        injected=tuple(injected),
        dropped=tuple(dropped),
        near_misses=tuple(missed[:NEAR_MISS_LIMIT]),
        used_embeddings=used_embeddings,
        fallback_reason=fallback_reason,
        query_text=query,
        tokens=used,
    )


def render_entry(entry: LoreEntry) -> str:
    """Mirrors `render_lore_block`, so the token count matches what is sent."""
    return f"## {entry.title}\n{entry.content.strip()}"


def _sort_key(injection: Injection) -> tuple[int, int, float, str]:
    return (
        -injection.entry.priority,
        REASON_ORDER[injection.reason],
        -(injection.score or 0.0),
        injection.entry.title.lower(),
    )
