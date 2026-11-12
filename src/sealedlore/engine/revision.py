"""Change this passage…: the passage written again with a change the author
asks for, as a new take beside it.

Pure: which passage can be changed and the request that asks for it.
`StorySession.revise_passage` makes the call.

The request is built as Continue's is (engine/continuation.py): the passage's
own turn as Regenerate sends it, so it rides the cached prefix the take was
written on, then the passage as the storyteller's own message and the request
to write it again with the change, with the turn's REMEMBER line after it.
Nothing is prefilled. The reply is a whole passage: it joins the story as
another take of the turn, where Regenerate's would, and the passage it was
made from stays beside it (‹ ›).
"""

from __future__ import annotations

from collections.abc import Sequence

from sealedlore.engine.continuation import continuable
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.models.node import Node

SECTION_REVISE_FROM = "revise.passage"
SECTION_REVISE = "revise.request"

NOT_REVISABLE = (
    "Only a passage written in answer to a turn can be changed this way: this one "
    "answers none. Edit corrects any message by hand."
)
NOTHING_ASKED = "Say what should change in the passage."
# The reply came back whole and held nothing: said, and nothing is attached.
NOTHING_CAME = "The storyteller sent back nothing: the passage is as it was."
NOTHING_AT_THE_LIMIT = (
    "The storyteller reached the length limit before it wrote any of the passage: it is "
    "as it was. Raising Max tokens under Settings → Generation may be what it takes."
)
NOTHING_DECLINED = "The model declined to write the change: the passage is as it was."


def nothing_came(finish_reason: str | None, refused: bool) -> str:
    """What to tell the author when the reply held no passage."""
    if refused:
        return NOTHING_DECLINED
    return NOTHING_AT_THE_LIMIT if finish_reason == "length" else NOTHING_CAME


def revisable(path: Sequence[Node]) -> bool:
    """Whether the end of `path` is a passage that can be written again with
    a change: the storyteller's, answering an author turn (as Continue's
    `continuable`, but for any passage, not the latest alone)."""
    return continuable(path)


def revise_request(
    texts: PromptTexts = DEFAULT_TEXTS, *, change: str, reminder: str = "", chat: bool = False
) -> str:
    """The message after the passage. A story's carries the turn's REMEMBER
    line again; a chat has none."""
    if chat:
        return texts.fill("chat.revise", change=change)
    parts = ["# CHANGE THE PASSAGE", texts.fill("turn.revise", change=change)]
    if reminder:
        parts.append(reminder)
    return "\n\n".join(parts)
