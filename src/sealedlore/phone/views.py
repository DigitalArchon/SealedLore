"""What a phone is shown of the open story, as plain JSON-ready values.

The window builds one entry per message when nothing is running (the worker
is the story's only writer while a passage is written) and the API pages
through them. Every entry says whether it was written in private, so the
page can warn before showing it: the phone is outside everything a private
scene, a memory-only chat or a TEE chat promises.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from sealedlore.engine.branches import Branch
from sealedlore.models.private import PrivateSpan

PRIVATE_KINDS = ("scene", "memory", "tee")


def private_tag(
    span_id: str | None,
    spans: Mapping[str, PrivateSpan],
    *,
    memory_chat: bool = False,
    tee_chat: bool = False,
) -> str | None:
    """'memory' for a memory-only scene's message or any message of a
    memory-only chat, 'scene' for a scene kept on disk, 'tee' for a TEE or
    encrypted chat's, None for the rest."""
    if span_id:
        span = spans.get(span_id)
        return "memory" if span is None or span.keep == "memory" else "scene"
    if memory_chat:
        return "memory"
    if tee_chat:
        return "tee"
    return None


def page(entries: Sequence[dict], before: str | None, limit: int) -> dict:
    """The `limit` messages before `before` (the newest when None), oldest
    first, and whether there are earlier ones. An id not on the path gives
    the newest page: the story moved on under the phone."""
    end = len(entries)
    if before is not None:
        index = next((i for i, entry in enumerate(entries) if entry["id"] == before), None)
        if index is not None:
            end = index
    start = max(0, end - limit)
    return {"messages": list(entries[start:end]), "earlier": start > 0}


def branch_rows(branches: Iterable[Branch]) -> list[dict]:
    """The branches as the branch bar lists them; the first line's start is
    "" on the wire."""
    return [
        {
            "start_id": branch.start_id or "",
            "name": branch.name,
            "current": branch.is_current,
            "depth": branch.depth,
            "messages": branch.length,
        }
        for branch in branches
    ]
