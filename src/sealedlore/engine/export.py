"""Readable export (§9): the story as prose, for reading or sharing.

Only the prose. No OOC addenda, no Director instructions, no reasoning, no
asides. The author's in-character turns keep their speaker's name; the
model's passages and the author's narration are the story's own voice, so
they stand unlabelled. A simple chat's messages are the user's, labelled with
the chat's name for them ("You" when it has none, as the transcript says).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sealedlore.models.character import Character
from sealedlore.models.node import CHAT_USER_ID, DIRECTOR_SPEAKER_ID, NARRATOR_SPEAKER_ID, Node
from sealedlore.models.story import Story

# What a chat calls its user when it has no name for them (the transcript's
# `speaker_label_for`, and the map's `CHAT_MAP_NAME`, say the same).
CHAT_USER_NAME = "You"


def markdown_user_name(story: Story, cast: Sequence[Character]) -> str | None:
    """The name the export gives the author's messages when the cast can't: a
    chat's name for its user, or the character a story's author is playing.
    None when a story holds nobody."""
    if story.chat:
        return CHAT_USER_NAME
    held = next((c for c in cast if c.id == story.held_character_id), None)
    return held.name if held is not None else None


def render_markdown(
    title: str,
    nodes: Sequence[Node],
    cast: Sequence[Character],
    *,
    subtitle: str | None = None,
    user_name: str | None = None,
) -> str:
    """`user_name` (`markdown_user_name`): the label for a chat's user, and
    for an author turn whose speaker no card names any more."""
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
            name = _speaker_name(node, names, user_name)
            paragraphs = text.split("\n\n")
            paragraphs[0] = f"**{name}:** {paragraphs[0]}"
            blocks.append("\n\n".join(paragraphs))
    return "\n\n".join(blocks) + "\n"


def _speaker_name(node: Node, names: Mapping[str, str], user_name: str | None) -> str:
    """Who wrote an author turn: a chat's user by the chat's name; a story's
    by the card the turn names, else the card the author held for it, else
    the name given for the author, before ever "Unknown"."""
    if node.speaker_id == CHAT_USER_ID:
        return user_name or CHAT_USER_NAME
    name = names.get(node.speaker_id)
    if name is None and node.meta.controlled_character_id:
        name = names.get(node.meta.controlled_character_id)
    return name or user_name or "Unknown"
