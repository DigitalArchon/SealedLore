"""A simple chat: the author's own system prompt and a plain conversation. Pure.

The author asked (Sept 2026) for "a normal simple chat with the story model
with almost none of our features enabled, but using our interface". What a
chat keeps from the app is the window, branches and takes, pictures, the cost,
and archival: once the conversation passes the context budget its oldest part
is summarised, as a story's is. What it drops is everything that makes a
story a story: the rules, cast, lore, scene, plot, the per-turn directives and
every side model.

A message can be kept in full (`NodeMeta.keep_full`; the first one is by
default). Once its part is summarised it is carried word for word after that
part's summary, in order, so the summaries block reads as the conversation
went; the summariser is told so and doesn't repeat it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.node import Node
from sealedlore.models.summary import Summary

SECTION_CHAT_PROMPT = "system.chat_prompt"
SECTION_CHAT_TAIL = "tail.chat_tail"

KEPT_NOTE = "[kept in full, carried separately]"


def speaker(node: Node) -> str:
    return "Assistant" if node.kind == "assistant" else "User"


def render_chat_node(node: Node) -> str:
    """A message in a chat's history: exactly as written, nothing around it."""
    return node.content.strip()


def render_chat_summaries(
    summaries: Sequence[Summary],
    nodes_by_id: Mapping[str, Node],
    texts: PromptTexts = DEFAULT_TEXTS,
) -> str:
    """The summaries block of a chat: each part's summary, then the messages
    it covers that are kept in full, in order."""
    if not summaries:
        return ""
    entries = []
    first = 1
    for summary in summaries:
        last = first + summary.chapters - 1
        heading = f"Part {first}" if last == first else f"Parts {first}–{last}"
        lines = [f"## {heading}", "", summary.content.strip()]
        for node_id in summary.covered_node_ids:
            node = nodes_by_id.get(node_id)
            if node is not None and node.meta.keep_full and node.content.strip():
                lines += ["", f"Kept in full — {speaker(node)}:", node.content.strip()]
        entries.append("\n".join(lines))
        first = last + 1
    return texts["chat.summaries"] + "\n\n" + "\n\n".join(entries)


def render_chat_chunk(nodes: Sequence[Node]) -> str:
    """Messages for the summariser: who said each, and which are kept in full."""
    parts = []
    for node in nodes:
        text = node.content.strip()
        if not text:
            continue
        label = f"{speaker(node)} {KEPT_NOTE}" if node.meta.keep_full else speaker(node)
        parts.append(f"{label}: {text}")
    return "\n\n".join(parts)


def build_chat_summary_messages(
    nodes: Sequence[Node],
    previous: Summary | None,
    *,
    target_words: int,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    body = []
    if previous is not None and previous.content.strip():
        body.append(texts["chat.previous"] + "\n\n" + previous.content.strip())
    body.append(
        texts.fill("chat.length", words=str(target_words)) + f"\n\n{render_chat_chunk(nodes)}"
    )
    system = texts.fill("chat.summariser", kept=KEPT_NOTE)
    return [
        PromptMessage(role="system", parts=(ContentPart(text=system),)),
        PromptMessage(role="user", parts=tuple(ContentPart(text=text) for text in body)),
    ]


def build_chat_merge_messages(
    run: Sequence[Summary], *, target_words: int, texts: PromptTexts = DEFAULT_TEXTS
) -> list[PromptMessage]:
    records = "\n\n".join(summary.content.strip() for summary in run)
    ask = texts.fill("chat.merge_length", words=str(target_words))
    return [
        PromptMessage(role="system", parts=(ContentPart(text=texts["chat.merger"]),)),
        PromptMessage(role="user", parts=(ContentPart(text=f"{ask}\n\n{records}"),)),
    ]
