"""Lore retrieval: what gets injected this turn, and why. See §7.

Pure functions over story state and pre-computed vectors — the embedding call
itself belongs to `StorySession`, so this module stays testable without a
network and the GUI can preview a selection on the main thread.

Selection follows §7: top-k semantic hits above a threshold, then every
`always_on` entry and every keyword match merged in, deduped, sorted by
priority, and fitted to the room this turn has for lore (`fit_lore`): an
entry too long for what is left goes in part, the parts the turn is about,
rather than not at all. Keyword matching is both a supplement and the whole
story when embeddings are unconfigured or failing.

Selection is only for a lorebook too big to send whole (`lore_layout`).
Measured on a long test story: at 8 entries every entry
cached cost less than retrieval and lost nothing in play; retrieval found 0.81
of what judges called relevant, the whole book all of it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Literal

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
    # Sent in part (`fit_lore`): `entry` holds the parts sent, and `whole`
    # the tokens the entry has in all.
    whole: int | None = None

    @property
    def fitted(self) -> bool:
        return self.whole is not None

    def describe(self) -> str:
        if self.reason == "semantic" and self.score is not None:
            said = f"semantic match ({self.score:.2f})"
        elif self.reason == "picked" and self.score is not None:
            said = f"picked by Jev ({self.score:.2f})"
        else:
            said = REASON_LABELS[self.reason]
        if self.whole is not None:
            said += f", in part ({self.tokens} of its {self.whole} tokens)"
        return said


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
    # Here, not at the top: numpy is ~45 ms and ~13 MB of every launch, and a
    # story whose lorebook is sent whole, or a chat, never scores a vector.
    import numpy as np

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


# A word's plural or possessive ending, which a keyword still names.
_ENDING = r"(?:['’]s|s['’]|es|s)?"


def _names(lowered: str, word: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(word)}{_ENDING}(?!\w)", lowered) is not None


def keyword_matches(text: str, entry: LoreEntry) -> bool:
    """Whole-word, case-insensitive match on any of the entry's keywords, a
    plural or possessive ending allowed ("the Adventurers' Guild" names the
    keyword "Adventurer").

    Whole-word rather than substring: "accord" should not fire on "according",
    and a lorebook full of false positives is worse than none at all. With
    no ending allowed, lorebooks written for SillyTavern (which matches a
    substring unless told otherwise) lost their entries to every plural: on
    a 320-entry lorebook, "Adventurer" never matched the turn that asked
    about the Adventurers' Guild (Nov 20 2026).
    """
    lowered = text.lower()
    for keyword in entry.keywords:
        cleaned = keyword.strip().lower()
        if cleaned and _names(lowered, cleaned):
            return True
    return False


def mentions_entry(text: str, entry: LoreEntry) -> bool:
    """Whether a text names an entry: one of its keywords or its title, whole words."""
    if keyword_matches(text, entry):
        return True
    title = entry.title.strip().lower()
    return bool(title) and _names(text.lower(), title)


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
    injected, dropped = fit_lore(
        ordered,
        token_cap,
        query=" ".join(part for part in (keyword_text, query) if part),
        count_tokens=count_tokens,
    )
    used = sum(injection.tokens for injection in injected)

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


# The least an entry is cut to: with less room left than this, it is left out
# rather than sent as a line out of context.
FIT_LEAST_TOKENS = 150
# The most of the room any one entry takes, so one long profile never shuts
# out everything after it: past it, the entry goes in part.
ENTRY_MOST_SHARE = 0.5
# Where an entry's text was cut (`fitted_entry`): a mark of the text's layout,
# not an instruction.
GAP = "[…]"


def entry_parts(content: str) -> list[str]:
    """An entry's text in its parts: paragraphs, and a heading line with what
    follows it up to the next blank line or heading."""
    parts: list[str] = []
    current: list[str] = []
    for line in content.strip().splitlines():
        stripped = line.strip()
        heading = (
            bool(stripped)
            and len(stripped) <= 80
            and (
                stripped.startswith(("#", "[", "<", "【"))
                or (stripped.endswith(":") and len(stripped.split()) <= 6)
            )
        )
        if not stripped or (heading and current):
            if current:
                parts.append("\n".join(current).strip())
            current = [] if not stripped else [line]
            continue
        current.append(line)
    if current:
        parts.append("\n".join(current).strip())
    return [part for part in parts if part]


def _cut(text: str, budget: int, count_tokens: Callable[[str], int]) -> str:
    """As much of `text` as `budget` tokens hold, ending at a sentence, else
    at a word, its own line breaks kept; "" when not even a word fits."""
    ends = [match.end() for match in re.finditer(r"[.!?](?=\s)|[.!?]$", text)]
    for stops in (ends, [match.end() for match in re.finditer(r"\S+", text)]):
        best = ""
        for stop in stops:
            piece = text[:stop].rstrip()
            if count_tokens(piece) > budget:
                break
            best = piece
        if best:
            return best
    return ""


def fitted_entry(
    entry: LoreEntry, room: int, query: str, count_tokens: Callable[[str], int]
) -> LoreEntry | None:
    """The entry cut to `room` tokens: its opening part always (what it is
    about), then the parts the query's words reach most (BM25 over the
    entry's own parts), in the entry's own order, with a gap mark where text
    was left out. The best of them that doesn't fit whole goes cut, and so
    does an opening longer than the room, at a sentence, or a word. None
    when not even the title and a word fit."""
    from sealedlore.engine.bm25 import bm25, words

    parts = entry_parts(entry.content)
    if not parts:
        return None
    head = count_tokens(f"## {entry.title}\n")
    budget = room - head - count_tokens(GAP) * 2
    sizes = [count_tokens(part) + 1 for part in parts]
    if sizes[0] > budget:
        opening = _cut(parts[0], budget, count_tokens)
        if not opening:
            return None
        return entry.model_copy(update={"content": f"{opening} {GAP}"})
    chosen: dict[int, str] = {0: parts[0]}
    used = sizes[0]
    scores = bm25(words(query), [words(part) for part in parts])
    ranked = sorted(range(1, len(parts)), key=lambda i: (-scores[i], i))
    cut_one = False
    for index in ranked:
        if used + sizes[index] <= budget:
            chosen[index] = parts[index]
            used += sizes[index]
        elif scores[index] > 0 and not cut_one:
            # The part the moment is most about, too long for what is left:
            # its start, rather than nothing of it and a part about nothing.
            cut_one = True
            kept = _cut(parts[index], budget - used - count_tokens(GAP), count_tokens)
            if kept:
                chosen[index] = f"{kept} {GAP}"
                used += count_tokens(chosen[index]) + 1
    pieces: list[str] = []
    previous = -1
    for index in sorted(chosen):
        if index != previous + 1:
            pieces.append(GAP)
        pieces.append(chosen[index])
        previous = index
    if previous != len(parts) - 1:
        pieces.append(GAP)
    return entry.model_copy(update={"content": "\n\n".join(pieces)})


def fit_lore(
    ordered: Sequence[Injection],
    room: int,
    *,
    query: str,
    count_tokens: Callable[[str], int],
) -> tuple[list[Injection], list[Injection]]:
    """`ordered` (best first) fitted to `room` tokens: (sent, left out).

    Each goes whole while it fits what is left and takes no more than
    `ENTRY_MOST_SHARE` of the room; else in part (`fitted_entry`), down to
    `FIT_LEAST_TOKENS`; only below that is it left out. A fixed 2,000 tokens,
    each entry whole or not at all, left out the entries a turn was about on
    a lorebook of long entries (a character's profile is 1,400 tokens): on a
    320-entry lorebook it dropped the Superd character when the author asked
    about the Superd, with a fifth of the budget used (Nov 20 2026)."""
    injected: list[Injection] = []
    dropped: list[Injection] = []
    used = 0
    most = max(FIT_LEAST_TOKENS, int(room * ENTRY_MOST_SHARE))
    for injection in ordered:
        space = min(room - used, most)
        if injection.tokens <= space:
            injected.append(injection)
            used += injection.tokens
            continue
        part = (
            fitted_entry(injection.entry, space, query, count_tokens)
            if (space >= FIT_LEAST_TOKENS)
            else None
        )
        if part is None:
            # Lore entries are independent: one too long for what is left
            # doesn't shut out the smaller ones behind it.
            dropped.append(injection)
            continue
        tokens = count_tokens(render_entry(part))
        injected.append(replace(injection, entry=part, tokens=tokens, whole=injection.tokens))
        used += tokens
    return injected, dropped


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
