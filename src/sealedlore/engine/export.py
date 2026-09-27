"""Readable export (§9): the story as prose, for reading or sharing.

Only the prose. No OOC addenda, no Director instructions, no reasoning, no
asides. The author's in-character turns keep their speaker's name; the
model's passages and the author's narration are the story's own voice, so
they stand unlabelled.
"""

from __future__ import annotations

from collections.abc import Sequence

from sealedlore.models.character import Character
from sealedlore.models.node import DIRECTOR_SPEAKER_ID, NARRATOR_SPEAKER_ID, Node


def render_markdown(
    title: str,
    nodes: Sequence[Node],
    cast: Sequence[Character],
    *,
    subtitle: str | None = None,
) -> str:
    names = {character.id: character.name for character in cast}
    blocks = [f"# {title.strip() or 'Untitled'}"]
    if subtitle:
        blocks.append(f"*{subtitle.strip()}*")
    for node in nodes:
        text = node.content.strip()
        if not text:
            continue
        if node.kind == "assistant" or node.speaker_id == NARRATOR_SPEAKER_ID:
            blocks.append(text)
        elif node.speaker_id == DIRECTOR_SPEAKER_ID:
            continue
        else:
            name = names.get(node.speaker_id, "Unknown")
            paragraphs = text.split("\n\n")
            paragraphs[0] = f"**{name}:** {paragraphs[0]}"
            blocks.append("\n\n".join(paragraphs))
    return "\n\n".join(blocks) + "\n"
