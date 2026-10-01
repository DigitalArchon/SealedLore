"""Chunked archival and summarisation.

Pure functions over story state: which summaries apply to a path, which nodes
they replace, which run of nodes to archive next, and the prompt that turns
that run into a chapter summary. Nothing here touches the network or the disk —
`StorySession` makes the call and stores the result.

Two decisions shape this module:

**Chunks, not single turns.** A summary is written once for ten exchanges and
then never changes, which is what keeps the summary block byte-stable while the
story grows. Archiving one turn at a time would rewrite that block every turn
and throw the cache prefix away with it (§5.2).

**Summaries are keyed by the exact run of nodes they replace.** A summary
applies to a path only if its `covered_node_ids` appear in that path, in order,
starting where the previous summary left off. A branch that forks below the
summarised line therefore simply fails to match and falls back to verbatim
prose, which is the lazy recomputation §5.2 asks for — no bookkeeping on fork,
no summary silently describing events that never happened on this branch.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.rules import render_history_node
from sealedlore.engine.validators import mentions
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.character import Character
from sealedlore.models.node import Node
from sealedlore.models.summary import Summary

# §5.2: the oldest ten *exchanges* — an author turn and the passage answering
# it — go into one chapter summary.
ARCHIVE_CHUNK_TURNS = 10

# Never archive the story out from under the author's feet: the most recent
# exchanges stay verbatim however tight the budget is.
MIN_VERBATIM_TURNS = 2

# The smallest chunk the author may archive by hand. Automatic archival takes
# whole chunks only (see plan_chunk).
MIN_MANUAL_TURNS = 2

SUMMARY_TARGET_WORDS = 250


@dataclass(frozen=True)
class PathSplit:
    """How a path divides into summarised history and verbatim prose."""

    summaries: tuple[Summary, ...]
    archived: tuple[Node, ...]
    verbatim: tuple[Node, ...]

    @property
    def archived_ids(self) -> frozenset[str]:
        return frozenset(node.id for node in self.archived)


def split_path(summaries: Sequence[Summary], path: Sequence[Node]) -> PathSplit:
    """Split `path` into the summaries covering its head and the prose after them.

    Matching is anchored: the chain starts at the first node of the path and
    each summary must cover the exact run of nodes that follows the previous
    one. The longest match wins where several summaries start at the same node,
    so a rebuilt summary of a longer run takes precedence over a shorter one.
    """
    ids = [node.id for node in path]
    by_head: dict[str, list[Summary]] = {}
    for summary in summaries:
        if summary.covered_node_ids:
            by_head.setdefault(summary.covered_node_ids[0], []).append(summary)
    for candidates in by_head.values():
        candidates.sort(key=lambda summary: len(summary.covered_node_ids), reverse=True)

    chain: list[Summary] = []
    position = 0
    while position < len(ids):
        matched: Summary | None = None
        for candidate in by_head.get(ids[position], ()):
            covered = candidate.covered_node_ids
            if ids[position : position + len(covered)] == covered:
                matched = candidate
                break
        if matched is None:
            break
        chain.append(matched)
        position += len(matched.covered_node_ids)

    return PathSplit(
        summaries=tuple(chain),
        archived=tuple(path[:position]),
        verbatim=tuple(path[position:]),
    )


def plan_chunk(
    verbatim: Sequence[Node],
    *,
    turns: int = ARCHIVE_CHUNK_TURNS,
    keep_turns: int = MIN_VERBATIM_TURNS,
    allow_partial: bool = False,
) -> list[Node]:
    """The oldest run of nodes to archive next, or empty if archival can't help.

    The run always ends on a model passage, so a chunk never splits an exchange
    and the verbatim window always opens on an author turn. Consecutive author
    turns — a speaker switch, a director aside — travel with the passage that
    answers them rather than counting as exchanges of their own.

    **Whole chunks only**, unless `allow_partial`. Live testing settled this:
    archiving four exchanges because that was all the budget left, and then one
    more the next turn, rewrites the summary block on consecutive turns and
    takes a full cache miss each time — which is exactly what chunking exists
    to prevent. When a full chunk isn't available the honest answer is to leave
    the history alone and say the budget can't hold a chapter yet. The author
    can still archive a short chunk deliberately.
    """
    ends = [index for index, node in enumerate(verbatim) if node.kind == "assistant"]
    available = len(ends) - max(keep_turns, 0)
    wanted = max(turns, 0)
    if available < wanted:
        if not allow_partial or available < MIN_MANUAL_TURNS:
            return []
        wanted = available
    if wanted <= 0:
        return []
    return list(verbatim[: ends[wanted - 1] + 1])


def turns_in(nodes: Sequence[Node]) -> int:
    """Exchanges in a run of nodes, counted by the model's passages."""
    return sum(1 for node in nodes if node.kind == "assistant")


def summaries_covering(summaries: Sequence[Summary], node_id: str) -> list[Summary]:
    return [summary for summary in summaries if node_id in summary.covered_node_ids]


def mark_stale(summaries: Sequence[Summary], node_id: str) -> list[Summary]:
    """Flag every summary covering `node_id`; return the ones that changed (§5.3).

    Mutates the summaries in place — the caller persists them and shows the
    banner naming what went stale. An already-stale summary is not reported
    again, so a second edit in the same region doesn't re-raise the banner.
    """
    changed: list[Summary] = []
    for summary in summaries_covering(summaries, node_id):
        if not summary.stale:
            summary.stale = True
            changed.append(summary)
    return changed


def swap_covered(summaries: Sequence[Summary], old_id: str, new_id: str) -> list[Summary]:
    """Another take stands where `old_id` stood: every summary covering it
    covers the new one instead (so it still matches the path) and is stale,
    since it summarised the old text. Returns the ones newly stale."""
    changed = mark_stale(summaries, old_id)
    for summary in summaries:
        if old_id in summary.covered_node_ids:
            summary.covered_node_ids = [
                new_id if node_id == old_id else node_id for node_id in summary.covered_node_ids
            ]
    return changed


def stale_summaries(summaries: Sequence[Summary]) -> list[Summary]:
    return [summary for summary in summaries if summary.stale]


def chapter_number(summaries: Sequence[Summary], summary: Summary) -> int:
    """1-based chapter number for display. Parts don't count: a part is
    numbered by its first chapter, which stays in the list."""
    target = summary.merged_from[0] if summary.merged_from else summary.id
    chapters = [candidate for candidate in summaries if not candidate.merged_from]
    for position, candidate in enumerate(chapters, start=1):
        if candidate.id == target:
            return position
    return len(chapters) + 1


def render_chunk(
    nodes: Sequence[Node],
    cast: Sequence[Character],
    *,
    include_private: bool = False,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> str:
    """Nodes as named prose for a side call. A private scene's messages are
    left out unless asked for (only its own summary call does): every read,
    scan and summary goes through here."""
    cast_by_id = {character.id: character for character in cast}
    rendered = [
        render_history_node(node, cast_by_id, texts)
        for node in nodes
        if include_private or node.meta.private_span is None
    ]
    return "\n\n".join(text for text in rendered if text)


def characters_in(text: str, cast: Sequence[Character]) -> list[Character]:
    """The cast members this prose actually names, by name, alias or a
    distinctive part of one (`validators.mentions`).

    Only these are given to the summariser. Handing it the whole cast invites
    it to account for the ones who weren't there — the first live run produced
    "Nils Carrow did not appear in this section", which is a statement about
    presence that the record has no business making (§3.4). A plain substring
    test found "Ann" in "announcer" and missed "Elena" for Elena Ruiz.
    """
    return [character for character in cast if mentions(text, character)]


def build_summary_messages(
    nodes: Sequence[Node],
    cast: Sequence[Character],
    *,
    previous: Summary | None = None,
    target_words: int = SUMMARY_TARGET_WORDS,
    chat: bool = False,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """The summariser call for one chunk.

    The immediately preceding summary travels with it so the summariser keeps
    the same names and doesn't re-establish facts the record already holds.
    A simple chat's is plain (engine/chat.py): no story, no characters.
    """
    if chat:
        from sealedlore.engine.chat import build_chat_summary_messages

        return build_chat_summary_messages(nodes, previous, target_words=target_words, texts=texts)
    prose = render_chunk(nodes, cast, texts=texts)
    body: list[str] = []
    if previous is not None and previous.content.strip():
        body.append(texts["chapters.previous"] + "\n\n" + previous.content.strip())

    names = ", ".join(character.name for character in characters_in(prose, cast))
    if names:
        body.append(texts.fill("chapters.names", names=names))

    body.append("The prose to compress:\n\n" + prose)
    body.append(texts.fill("chapters.length", words=str(target_words)))

    return [
        PromptMessage(role="system", parts=(ContentPart(text=texts["chapters.summariser"]),)),
        PromptMessage(role="user", parts=tuple(ContentPart(text=text) for text in body)),
    ]


# --- merging old chapters into parts -------------------------------------------


def summaries_tokens(
    summaries: Sequence[Summary],
    count_tokens,  # noqa: ANN001
    texts: PromptTexts = DEFAULT_TEXTS,
) -> int:
    from sealedlore.engine.rules import render_summaries_block

    text = render_summaries_block(summaries, texts)
    return count_tokens(text) if text else 0


def plan_merge(
    path_summaries: Sequence[Summary],
    *,
    over_threshold: bool,
    keep: int,
) -> list[Summary]:
    """The oldest run of the path's summaries to merge into one part, or empty.

    Only once the summary block is over its share of the budget, and never the
    newest `keep`: the recent past is where the story needs its detail. An
    existing part leads the run and is merged again with what follows it, so a
    long story keeps one part, not a growing stack of them.

    The merge goes around a chapter the author keeps as written, and around a
    stale one (merging it would bury the edit in a part with the old text),
    taking the oldest run between them that has something to merge. Before,
    such a chapter ended the run, and one near the start stopped merging for
    good. A hand-edited chapter is merged like any other (the author's call).
    """
    if not over_threshold:
        return []
    candidates = list(path_summaries[: max(len(path_summaries) - max(keep, 1), 0)])
    run: list[Summary] = []
    for summary in candidates:
        if summary.stale or summary.keep_as_written:
            if len(run) >= 2:
                return run
            run = []
            continue
        run.append(summary)
    # Merging one chapter into itself, or a part with nothing new, saves nothing.
    return run if len(run) >= 2 else []


def source_chapters(run: Sequence[Summary]) -> list[str]:
    """The chapter ids a merge of `run` stands for, parts flattened."""
    ids: list[str] = []
    for summary in run:
        ids.extend(summary.merged_from or [summary.id])
    return ids


def build_merge_messages(
    run: Sequence[Summary],
    cast: Sequence[Character],
    *,
    target_words: int,
    chat: bool = False,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    if chat:
        from sealedlore.engine.chat import build_chat_merge_messages

        return build_chat_merge_messages(run, target_words=target_words, texts=texts)
    records = "\n\n".join(summary.content.strip() for summary in run)
    body: list[str] = []
    names = ", ".join(character.name for character in characters_in(records, cast))
    if names:
        body.append(texts.fill("chapters.names", names=names))
    body.append("The record to condense, oldest first:\n\n" + records)
    body.append(texts.fill("chapters.merge_length", words=str(target_words)))
    return [
        PromptMessage(role="system", parts=(ContentPart(text=texts["chapters.merger"]),)),
        PromptMessage(role="user", parts=tuple(ContentPart(text=text) for text in body)),
    ]


# A summariser reply that ran into its token limit is half a chapter: the
# prose it would have covered stays verbatim instead (the ledger already
# refuses a cut-off update the same way).
SUMMARY_CUT_OFF = (
    "the summariser's reply was cut off by its token limit, so the chapter was not kept; "
    "the prose stays as written"
)


def chapter_numbers(path_summaries: Sequence[Summary]) -> dict[str, int]:
    """Chapter numbers as the reader counts them along one path, by summary id.

    A part is numbered by its first chapter and counts for all of them, so
    the chapter after "Part (chapters 1–6)" is 7 everywhere: the navigator,
    the transcript's chapter cards, the Story so far panel and the notices
    used to number these differently.
    """
    numbers: dict[str, int] = {}
    count = 0
    for summary in path_summaries:
        numbers[summary.id] = count + 1
        count += len(summary.merged_from) or 1
    return numbers
