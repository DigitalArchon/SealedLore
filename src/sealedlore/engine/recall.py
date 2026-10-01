"""Recall: the detail a merge condensed away, brought back when it matters. Pure.

Merging old chapters into a part keeps the summary block inside its share of
the budget, but each merge halves what the oldest chapters said, and a long
story's beginning passes through the merger again and again. The chapters
themselves stay in summaries.json, and the archived prose stays on the nodes;
the storyteller saw neither again. The author (Oct 2026): players must not feel
they are losing information that is hard to get back.

So the chapters a part stands for (and, at the finer level, the archived
exchanges themselves) are candidates for the tail, a few at most, in story
order, under a heading that says they are the past. Never the system block:
they change from turn to turn, and the cached prefix must not.

They are ranked by three rankings fused (`fuse`): the embeddings model's
similarity to the query, and words weighted by how rare they are among the
candidates (`keyword_scores`), over each candidate's own text and over the
archived exchanges a chapter stands for. Measured on the long test run (Oct
2026, 79 Questions about merged chapters): similarity alone put the right
chapter first 20 times and in the top two 34; fused, 33 and 43. The model
hardly mattered (five tried, all within a few). Keywords need no endpoint, so
without one recall still runs on them alone.

`StorySession` owns the embedding calls and the cache; this module only
gathers candidates, ranks scored ones, chooses and renders them.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from sealedlore.engine.archival import PathSplit, chapter_numbers, render_chunk
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.models.character import Character
from sealedlore.models.node import Node
from sealedlore.models.summary import Summary

RecallKind = Literal["chapter", "exchange"]
# "chapters": the chapters a part stands for; "exchanges": those and the
# archived prose itself, an exchange at a time.
RecallMode = Literal["off", "chapters", "exchanges"]


@dataclass(frozen=True)
class RecallItem:
    """One thing that can be recalled: a merged-away chapter, or an archived exchange."""

    id: str
    kind: RecallKind
    label: str
    text: str
    # Its place in the story, so what is recalled reads in order.
    position: int
    # What else it can be found by: a chapter's archived exchanges, as
    # written (for keywords only: never sent, never embedded).
    detail: tuple[str, ...] = ()
    # Brought back because a recent Question recalled it (`carried`).
    carried: bool = False


@dataclass(frozen=True)
class RecallReport:
    """What was recalled this turn, in what order it ranked, and what nearly was."""

    items: tuple[RecallItem, ...] = ()
    # The fused score, by item id: an order, not a measure of relevance.
    scores: Mapping[str, float] = field(default_factory=dict)
    near_misses: tuple[RecallItem, ...] = ()
    reason: str | None = None


def _exchanges(nodes: Sequence[Node]) -> list[list[Node]]:
    """Author turns with the passage that answers them, as `plan_chunk` counts."""
    out: list[list[Node]] = []
    current: list[Node] = []
    for node in nodes:
        current.append(node)
        if node.kind == "assistant":
            out.append(current)
            current = []
    if current:
        out.append(current)
    return out


def recall_items(
    split: PathSplit,
    summaries: Sequence[Summary],
    cast: Sequence[Character],
    *,
    mode: RecallMode,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[RecallItem]:
    """Everything that could be recalled on this path.

    Chapters: those a part on the path stands for (a chapter that is itself
    on the path is already in the prompt). Exchanges: every archived exchange,
    labelled with the chapter it belongs to. A private scene's messages are
    never among them: `split` comes from `path()`, and `render_chunk` leaves
    them out as well.
    """
    if mode == "off":
        return []
    by_id = {summary.id: summary for summary in summaries}
    numbers = chapter_numbers(split.summaries)
    exchanges = [
        (exchange, render_chunk(exchange, cast, texts=texts))
        for exchange in _exchanges(split.archived)
    ]
    exchanges_of: dict[str, list[str]] = {}
    for exchange, text in exchanges:
        if text.strip():
            exchanges_of.setdefault(exchange[0].id, []).append(text)
    items: list[RecallItem] = []
    chapter_of: dict[str, int] = {}
    position = 0
    for summary in split.summaries:
        first = numbers[summary.id]
        parts = summary.merged_from or [summary.id]
        for offset, chapter_id in enumerate(parts):
            chapter = by_id.get(chapter_id)
            if chapter is None:
                continue
            for node_id in chapter.covered_node_ids:
                chapter_of.setdefault(node_id, first + offset)
            if summary.merged_from and chapter.content.strip():
                items.append(
                    RecallItem(
                        id=chapter.id,
                        kind="chapter",
                        label=f"Chapter {first + offset}, as first written",
                        text=chapter.content.strip(),
                        position=position,
                        detail=tuple(
                            text
                            for node_id in chapter.covered_node_ids
                            for text in exchanges_of.get(node_id, ())
                        ),
                    )
                )
            position += 1
        for node_id in summary.covered_node_ids:
            chapter_of.setdefault(node_id, first)
    if mode == "exchanges":
        for index, (exchange, text) in enumerate(exchanges):
            if not text.strip():
                continue
            number = chapter_of.get(exchange[0].id)
            items.append(
                RecallItem(
                    id=f"x:{exchange[0].id}",
                    kind="exchange",
                    label=f"From chapter {number}, as written" if number else "As written",
                    text=text,
                    position=100_000 + index,
                    detail=(text,),
                )
            )
    return items


# Words too common to tell one chapter from another.
_STOPWORDS = frozenset(
    """a an the and or but of to in on at by for with from as is was were be been
    are it its this that these those he she they them his her their him i you we us
    our your my me what who whom which when where why how did do does done had has
    have not no so if then than there here about into out up down over after before
    while during again once all any some such only own same too very can will just
    would could should said says say""".split()
)


def _words(text: str) -> list[str]:
    text = text.lower().replace("\u2019", "'")
    return [w for w in re.findall(r"[a-z0-9']+", text) if len(w) > 1 and w not in _STOPWORDS]


def _bm25(query: Sequence[str], units: Sequence[Sequence[str]]) -> list[float]:
    """Okapi BM25 of each unit against the query's words (k1 1.2, b 0.75):
    a word counts for more the rarer it is among the units."""
    if not units:
        return []
    average = sum(len(unit) for unit in units) / len(units) or 1.0
    frequency = Counter(word for unit in units for word in set(unit))
    total = len(units)
    out = []
    for unit in units:
        counts = Counter(unit)
        score = 0.0
        for word in query:
            seen = counts.get(word)
            if not seen:
                continue
            idf = math.log(1 + (total - frequency[word] + 0.5) / (frequency[word] + 0.5))
            score += idf * seen * 2.2 / (seen + 1.2 * (0.25 + 0.75 * len(unit) / average))
        out.append(score)
    return out


def keyword_scores(
    query: str, items: Sequence[RecallItem]
) -> tuple[dict[str, float], dict[str, float]]:
    """Keyword scores by item id: over each item's own text, and over its
    `detail` (a chapter scores its best exchange). Items no word of the query
    reaches are left out."""
    words = _words(query)
    own = _bm25(words, [_words(item.text) for item in items])
    units = [(item.id, _words(text)) for item in items for text in item.detail]
    detail: dict[str, float] = {}
    for (item_id, _), score in zip(units, _bm25(words, [u for _, u in units]), strict=True):
        if score > detail.get(item_id, 0.0):
            detail[item_id] = score
    by_own = {item.id: score for item, score in zip(items, own, strict=True) if score > 0}
    return by_own, detail


def fuse(rankings: Sequence[Mapping[str, float]], k: int = 60) -> dict[str, float]:
    """Reciprocal-rank fusion: each ranking adds 1/(k + rank) for the items it holds."""
    fused: dict[str, float] = {}
    for scores in rankings:
        ordered = sorted(scores, key=lambda item_id: scores[item_id], reverse=True)
        for rank, item_id in enumerate(ordered, 1):
            fused[item_id] = fused.get(item_id, 0.0) + 1.0 / (k + rank)
    return fused


def rank_recall(
    query: str, items: Sequence[RecallItem], similarity: Mapping[str, float]
) -> dict[str, float]:
    """The fused score of every item any ranking reaches. `similarity` may be
    empty (no embeddings endpoint): keywords then rank alone."""
    own, detail = keyword_scores(query, items)
    return fuse([similarity, own, detail])


def select_recall(
    items: Sequence[RecallItem],
    scores: Mapping[str, float],
    *,
    k: int,
    token_cap: int,
    count_tokens: Callable[[str], int],
    near: int = 3,
) -> RecallReport:
    """The best `k` by `scores` that fit `token_cap`, in story order.

    No threshold: similarity scores don't separate the relevant from the rest
    (right chapters a median 0.56, the best wrong one 0.59), and none tried
    could tell a turn that needs recall from one that doesn't. An exchange
    inside a chapter already chosen adds little, so the first of the two to be
    chosen keeps its place and the other is passed over. The next few are kept
    for the report.
    """
    ranked = sorted(
        (item for item in items if item.id in scores),
        key=lambda item: scores[item.id],
        reverse=True,
    )
    chosen: list[RecallItem] = []
    used = 0
    near_misses: list[RecallItem] = []
    labels: set[str] = set()
    for item in ranked:
        if len(chosen) >= k:
            if len(near_misses) < near:
                near_misses.append(item)
            continue
        number = item.label.split(",")[0].replace("From chapter", "Chapter")
        if number in labels:
            continue
        cost = count_tokens(item.text)
        if used + cost > token_cap:
            continue
        chosen.append(item)
        labels.add(number)
        used += cost
    chosen.sort(key=lambda item: item.position)
    return RecallReport(
        items=tuple(chosen),
        scores={item.id: scores[item.id] for item in ranked},
        near_misses=tuple(near_misses),
    )


def recall_embedding_text(item: RecallItem) -> str:
    return item.text


def render_recall_block(items: Sequence[RecallItem], texts: PromptTexts = DEFAULT_TEXTS) -> str:
    if not items:
        return ""
    heading = texts["turn.recall"]
    if any(item.carried for item in items):
        heading += "\n\n" + texts["turn.recall.carried"]
    rendered = [f"## {item.label}\n{item.text.strip()}" for item in items]
    return heading + "\n\n" + "\n\n".join(rendered)
