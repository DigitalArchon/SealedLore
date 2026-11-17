"""The reaction round: main characters played by their own model.

The author's trial (Nov 2026): after the storyteller's passage lands, the
player and every character whose card names a model answer it at once, none
of them seeing what the others said — each character's model is given only
the story as that character experienced it. When the player sends their next
turn, the storyteller is given all of it and works out how the intentions fit
together, treating what a character said as what they intend to do, never
as fact of what they did (the author's ruling, the day the trial started).

Each character's model is told the storyteller's writing is fact, and sees
only the scenes they were present for (the scene snapshots on the nodes), so
the load-bearing presence rule holds for them as it does for everyone:
nobody answers a scene they cannot perceive. A character not in the scene
after a passage gets no call.

This is a trial, not a core feature: the author expects live A/B testing
before anything here is settled, every wording is a first draft, and a story
that names no character's own model behaves exactly as it did before. The
calls are made in the round, on the worker, one after the other: each is
blind to the others by what its prompt holds, not by the order they went.
"""

from __future__ import annotations

from collections.abc import Sequence

from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.rules import full_description_text, render_history_node
from sealedlore.engine.scene_state import scene_at
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.character import Character
from sealedlore.models.node import Node
from sealedlore.models.scene import SceneState
from sealedlore.models.story import Story


def independent_cast(cast: Sequence[Character]) -> list[Character]:
    """The main characters an own model plays (`Character.own_model`)."""
    return [character for character in cast if character.own_model]


def reacting_characters(
    cast: Sequence[Character],
    scene: SceneState,
    *,
    held_character_id: str | None = None,
) -> list[Character]:
    """Who the round calls after a passage: played by their own model, in the
    scene as the passage left it, and not the one the author is playing (the
    author's answer 4: a character played by hand is never answered by its
    model on the same turn — the author *is* them)."""
    present_ids = set(scene.present_character_ids)
    present_names = {name.strip() for name in scene.present_others}
    reacting: list[Character] = []
    for character in independent_cast(cast):
        if character.id == held_character_id:
            continue
        if character.id not in present_ids and character.name not in present_names:
            # Not in the scene: they cannot perceive the passage, and the
            # presence rule is load-bearing for them as for the storyteller.
            continue
        reacting.append(character)
    return reacting


def _present_in(node: Node, character: Character, scene: SceneState | None) -> bool:
    """Whether `node` is part of a scene `character` was present for, judged
    by the snapshot that was in force at that point on the path."""
    if scene is None:
        return False
    if node.kind == "user" and node.speaker_id == character.id:
        # A turn written as them (before they were handed to their own model,
        # or on a branch where the author still plays them).
        return True
    return character.id in scene.present_character_ids or character.name in {
        name.strip() for name in scene.present_others
    }


def character_history(
    history: Sequence[Node],
    character: Character,
    cast: Sequence[Character],
    *,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[str]:
    """The story as this character experienced it: the turns and passages of
    the scenes they were present for, nothing else (the author's answer 2 —
    each AI character builds its own history of scenes it was present for).

    `history` is the path as models may see it, so a private scene they
    weren't in never reaches them at all. The scene walks forward snapshot by
    snapshot, so a branch, a retake or a deletion is judged by exactly the
    scenes it holds. The character's own reactions are part of those scenes:
    each passage carries what they answered it with.
    """
    cast_by_id = {c.id: c for c in cast}
    rendered: list[str] = []
    scene: SceneState | None = None
    for node in history:
        if node.meta.scene is not None:
            scene = node.meta.scene
        if not _present_in(node, character, scene):
            continue
        rendered.append(render_history_node(node, cast_by_id, texts))
    return rendered


def scene_lines(
    scene: SceneState, cast: Sequence[Character], character: Character, texts: PromptTexts
) -> str:
    """Where the character is and who is with them, as the scene card has it."""
    names = [c.name for c in cast if c.id in scene.present_character_ids]
    lines = [
        texts.fill(
            "characters.own.scene",
            location=scene.location or "",
            time=scene.time_of_day or "",
            situation=scene.situation or "",
        )
    ]
    here = [
        name.strip()
        for name in [*names, *scene.present_others]
        if name.strip() and name.strip() != character.name
    ]
    if here:
        lines.append(texts.fill("characters.own.present", names=", ".join(here)))
    return "\n".join(lines)


def reaction_messages(
    *,
    story: Story,
    cast: Sequence[Character],
    character: Character,
    history: Sequence[Node],
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """The call that asks one character for their answer to the last passage.

    What the character is told: who they are (their card, whole — the trial
    keeps nothing back from them), the story as they experienced it, the
    scene they are in, and the latest passage, which they answer. What they
    are *not* told: anything any other character answered, and what the
    author is about to write — both blind to them by the round's design.
    """
    scene = scene_at(history, story.scene, cast_ids=[c.id for c in cast])
    parts = [
        texts.fill(
            "characters.own.system",
            name=character.name,
            summary=(character.summary or "").strip(),
            description=full_description_text(character) or (character.summary or ""),
        ),
    ]
    if character.voice_notes and character.voice_notes.strip():
        parts.append(texts.fill("characters.own.voice", voice=character.voice_notes.strip()))
    body: list[str] = [texts.fill("characters.own.rules", name=character.name)]
    lived = character_history(history, character, cast, texts=texts)
    if lived:
        body.append(texts.fill("characters.own.history.heading", name=character.name))
        body.append("\n\n".join(lived))
    body.append(texts["characters.own.scene.heading"])
    body.append(scene_lines(scene, cast, character, texts))
    if history and history[-1].kind == "assistant":
        body.append(texts["characters.own.passage"])
        body.append(history[-1].content.strip())
    body.append(texts.fill("characters.own.ask", name=character.name))
    return [
        PromptMessage(
            role="system",
            parts=tuple(ContentPart(text=text) for text in parts if text.strip()),
        ),
        PromptMessage(
            role="user",
            parts=tuple(ContentPart(text=text) for text in body if text.strip()),
        ),
    ]
