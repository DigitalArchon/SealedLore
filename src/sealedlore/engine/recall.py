"""Recall: the detail a merge condensed away, brought back when it matters. Pure.

Merging old chapters into a part keeps the summary block inside its share of
the budget, but each merge halves what the oldest chapters said, and a long
story's beginning passes through the merger again and again. The chapters
themselves stay in summaries.json, and the archived prose stays on the nodes;
the storyteller saw neither again. The author (Oct 2026): players must not feel
they are losing information that is hard to get back.

So the chapters a part stands for (and, at the finer level, the archived
exchanges themselves) are candidates for this turn's tail, ranked by
similarity to the same query lore uses, a few at most, in story order, under a
heading that says they are the past. Never the system block: they change from
turn to turn, and the cached prefix must not.

`StorySession` owns the embedding calls and the cache; this module only
gathers candidates, chooses among scored ones and renders them.
"""

from __future__ import annotations

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


@dataclass(frozen=True)
class RecallReport:
    """What was recalled this turn, with scores, and what nearly was."""

    items: tuple[RecallItem, ...] = ()
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
                    )
                )
            position += 1
        for node_id in summary.covered_node_ids:
            chapter_of.setdefault(node_id, first)
    if mode == "exchanges":
        for index, exchange in enumerate(_exchanges(split.archived)):
            text = render_chunk(exchange, cast, texts=texts)
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
                )
            )
    return items


def select_recall(
    items: Sequence[RecallItem],
    scores: Mapping[str, float],
    *,
    k: int,
    threshold: float,
    token_cap: int,
    count_tokens: Callable[[str], int],
    near: int = 3,
) -> RecallReport:
    """The best `k` above `threshold` that fit `token_cap`, in story order.

    An exchange inside a chapter already chosen adds little, so the first of
    the two to be chosen (by score) keeps its place and the other is passed
    over. Near misses are kept for the report: without them the threshold
    can't be tuned (as for lore).
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
        score = scores[item.id]
        if score < threshold:
            if len(near_misses) < near:
                near_misses.append(item)
            continue
        if len(chosen) >= k:
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
    rendered = [f"## {item.label}\n{item.text.strip()}" for item in items]
    return texts["turn.recall"] + "\n\n" + "\n\n".join(rendered)
