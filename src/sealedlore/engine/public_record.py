"""The public record: what everyone where the story is set knows by now, for
characters played by their own model (engine/reactions.py). Pure.

The author (Nov 18 2026): a character who comes back after days away should
know what the whole station heard meanwhile, as a briefed officer would,
without anything said in private reaching them. Their model sees only the
scenes they were in, so a cheap model keeps a record after every passage:
how things stand now, at most `MAX_FACTS` sentences, and its own private notes
of what was said in confidence, so a secret that surfaces later among a few
people stays out. The notes reach no model but the keeper. Each character's
request carries the record before the story as they saw it.

Every passage is read, private-marked scenes too: a scene's mark says who was
in the room, not whether what happened stayed in it (a city-wide Engine
failure landed in a passage whose scene was marked private, live). The keeper
checks the old facts before adding any, and gives the whole record back with
any new notes: asked for edits only, keepers added and seldom revised.

Measured (SealedLoreDocs design/own-models.md, "A public record"): five
keepers on two scripted stories, every record graded blind; GLM 5.3 kept the
most complete record at a fraction of a cent a passage (free on a NanoGPT
subscription), so the record model falls back to the scene model.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.models.node import Node

MAX_FACTS = 25
# Past MAX_FACTS the keeper is asked to drop what matters least; past this, the
# oldest go. Cutting the newest instead lost every later event once a keeper
# that never dropped had filled the record (DeepSeek V4.1 Flash, Brasshaven).
HARD_FACTS = 35
MAX_NOTES = 60
# A record of at least this many facts may not lose more than half at once
# (the ledger's guard: a cut-off reply once turned 38 lines into 3).
SMALL_RECORD = 6


@dataclass(frozen=True)
class RecordState:
    facts: tuple[str, ...] = ()
    # The keeper's own notes of what was said in confidence: never shown to
    # any model but the keeper.
    notes: tuple[str, ...] = ()


def record_at(path: Sequence[Node]) -> RecordState:
    """The record after the last node of `path` that holds one (a snapshot
    on the node, as the scene is), or an empty one."""
    for node in reversed(path):
        if node.meta.public_record is not None:
            return RecordState(tuple(node.meta.public_record), tuple(node.meta.public_notes or ()))
    return RecordState()


def numbered(items: Sequence[str], texts: PromptTexts = DEFAULT_TEXTS) -> str:
    return (
        "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1))
        or texts["characters.own.record.empty"]
    )


def passage_text(node: Node) -> str:
    """One passage for the keeper, with where its scene was and its mark: a
    hint, never a gate."""
    scene = node.meta.scene
    mark = (
        f"{scene.location}; scene marked {scene.privacy or 'unmarked'}"
        if scene is not None
        else "the scene unknown"
    )
    return f"({mark})\n{node.content.strip()}"


def keeper_prompt(
    state: RecordState, passages: Sequence[Node], texts: PromptTexts = DEFAULT_TEXTS
) -> str:
    """The keeper's request: the record, its notes, the passages to read."""
    what = (
        "latest passage" if len(passages) == 1 else f"latest {len(passages)} passages, oldest first"
    )
    body = "\n\n".join(
        f"PASSAGE{'' if len(passages) == 1 else f' {i}'} {passage_text(node)}"
        for i, node in enumerate(passages, 1)
    )
    return texts.fill(
        "characters.own.record.keep",
        what=what,
        limit=str(MAX_FACTS),
        record=numbered(state.facts, texts),
        notes=numbered(state.notes, texts),
        passages=body,
    )


def apply_reply(reply: str, state: RecordState) -> RecordState | None:
    """The record the reply gives, with any new private notes added to the
    keeper's, or None when the reply won't do: no JSON object, the wrong
    shapes, or most of a record of some size gone at once."""
    try:
        data = extract_json(reply, dict, "public record")
    except ValueError:
        return None
    facts, private = data.get("facts"), data.get("private")
    if not isinstance(facts, list) or not all(isinstance(f, str) for f in facts):
        return None
    if private is not None and (
        not isinstance(private, list) or not all(isinstance(p, str) for p in private)
    ):
        return None
    facts = [f.strip() for f in facts if f.strip()]
    if len(state.facts) >= SMALL_RECORD and len(facts) < len(state.facts) / 2:
        return None
    notes = list(state.notes) + [p.strip() for p in private or [] if p.strip()]
    return RecordState(tuple(facts[-HARD_FACTS:]), tuple(notes[-MAX_NOTES:]))


def render_record(facts: Sequence[str], texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """The record in a character's request (before the story as they saw
    it), or "" when it holds nothing."""
    if not facts:
        return ""
    return texts["characters.own.record.heading"] + "\n" + "\n".join(f"- {f}" for f in facts)
