"""The reaction round: main characters played by their own model. Pure.

The author's trial (Nov 2026). After the storyteller's passage lands, the
author and every character whose card names a model (`Character.own_model`)
answer it at once, none of them seeing the others' answers. When the author
sends their turn, the storyteller gets it with every character's answer and
works out what happens when the intentions meet. A character's answer is
what they mean to do, never a fact of what they did (the author's ruling).

What a character's model is given, by the author's rulings:
- their card (who they are, their voice, what they can do) and the setting;
- the story as they saw it: the scenes they were present for, judged by the
  scene snapshots on the nodes, so nobody answers a scene they couldn't
  perceive (the presence rule holds for them as for the storyteller);
- in those scenes, the storyteller's passages (fact), the author's turns
  and Narration without their OOC notes, and their own earlier answers.
  Never a Direction (it is to the storyteller alone), never another
  character's answer (what someone means to do is their own), and never the
  storyteller's notes;
- the most recent of it that fits `HISTORY_TOKENS`: chapters cover scenes a
  character wasn't in, so they can't stand in for the older part.

A trial, not a core feature: every wording is a first draft, nothing here is
measured, and a story that names no character's own model is unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.rules import (
    full_description_text,
    render_competence_line,
    render_speaker_tag,
    summary_repeated,
)
from sealedlore.engine.scene_state import same_person, scene_at
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.character import Character
from sealedlore.models.node import DIRECTOR_SPEAKER_ID, Node
from sealedlore.models.scene import SceneState
from sealedlore.models.story import Story

# How much of the story a character's model is sent, newest first. Their
# older scenes wait for a memory of their own (the trial's next step).
HISTORY_TOKENS = 12_000


def _rough_tokens(text: str) -> int:
    return len(text) // 4 + 1


def independent_cast(cast: Sequence[Character]) -> list[Character]:
    """The main characters a model of their own plays."""
    return [character for character in cast if (character.own_model or "").strip()]


def in_scene(character: Character, scene: SceneState | None) -> bool:
    """Whether the scene card has `character` in it, by id or by name."""
    if scene is None:
        return False
    if character.id in scene.present_character_ids:
        return True
    return any(same_person(name, character) for name in scene.present_others)


def reacting_characters(
    cast: Sequence[Character], scene: SceneState, *, held_character_id: str | None = None
) -> list[Character]:
    """Who the round asks after a passage: played by a model of their own,
    in the scene the passage left, and not the character the author holds
    (the author is playing them)."""
    return [
        character
        for character in independent_cast(cast)
        if character.id != held_character_id and in_scene(character, scene)
    ]


def _entry(
    node: Node, character: Character, cast_by_id: dict[str, Character], texts: PromptTexts
) -> str:
    """One message as this character saw it, or "" for one they never see."""
    if node.kind == "assistant":
        lines = [node.content.strip()]
        own = [r for r in node.meta.reactions if r.character_id == character.id]
        lines += [texts.fill("characters.own.answered", text=r.text.strip()) for r in own]
        return "\n\n".join(line for line in lines if line)
    if node.speaker_id == DIRECTOR_SPEAKER_ID:
        return ""  # Direction is to the storyteller alone
    # The author's turn, or Narration (in-story prose, true), without its OOC
    # note: that is to the storyteller.
    speaker = cast_by_id.get(node.speaker_id)
    tag = render_speaker_tag(node.speaker_id, speaker, texts)
    return f"{tag}\n{node.content.strip()}" if node.content.strip() else ""


def character_history(
    history: Sequence[Node],
    character: Character,
    cast: Sequence[Character],
    *,
    start: SceneState,
    texts: PromptTexts = DEFAULT_TEXTS,
    count: Callable[[str], int] = _rough_tokens,
    budget: int = HISTORY_TOKENS,
) -> tuple[list[str], bool]:
    """The story as this character saw it, oldest first, and whether older
    scenes were left out to fit `budget`.

    A message counts when the character was in the scene before it or after
    it (a snapshot on a node is the scene after it), so the passage they
    arrive in, and the one they leave in, are both theirs. `history` is the
    path as models may see it: a private scene is already gone from it.
    """
    cast_by_id = {c.id: c for c in cast}
    seen: list[str] = []
    scene: SceneState | None = start
    for node in history:
        before = scene
        if node.meta.scene is not None:
            scene = node.meta.scene
        mine = node.kind == "user" and node.speaker_id == character.id
        if not (mine or in_scene(character, before) or in_scene(character, scene)):
            continue
        text = _entry(node, character, cast_by_id, texts)
        if text:
            seen.append(text)
    kept: list[str] = []
    spent = 0
    for text in reversed(seen):
        cost = count(text)
        if kept and spent + cost > budget:
            break
        kept.append(text)
        spent += cost
    kept.reverse()
    return kept, len(kept) < len(seen)


def _card(character: Character) -> str:
    summary = (character.summary or "").strip()
    description = full_description_text(character)
    parts = [] if summary_repeated(character) else [summary]
    parts.append(description)
    return "\n\n".join(part for part in parts if part)


def _scene_lines(
    scene: SceneState, cast: Sequence[Character], character: Character, texts: PromptTexts
) -> str:
    lines = [
        texts.fill(
            "characters.own.scene",
            location=scene.location or "",
            time=scene.time_of_day or "",
            situation=scene.situation or "",
        )
    ]
    here = [c.name for c in cast if c.id in scene.present_character_ids and c.id != character.id]
    here += [n.strip() for n in scene.present_others if n.strip() and not same_person(n, character)]
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
    count: Callable[[str], int] = _rough_tokens,
    budget: int = HISTORY_TOKENS,
) -> list[PromptMessage]:
    """One character's request: their card and the setting in the system
    message (the same every round, so a model that caches can), the story as
    they saw it ending on the passage they answer, then the ask.

    `cast` is the cast models may see (plot-hidden cards left out)."""
    system = [texts.fill("characters.own.system", name=character.name, card=_card(character))]
    if (character.voice_notes or "").strip():
        system.append(texts.fill("characters.own.voice", voice=character.voice_notes.strip()))
    skills = render_competence_line(character).removeprefix("Competence: ")
    if skills:
        system.append(texts.fill("characters.own.skills", skills=skills))
    if (story.world_bible or "").strip():
        system.append(texts.fill("characters.own.setting", setting=story.world_bible.strip()))
    system.append(texts.fill("characters.own.rules", name=character.name))
    system = [text for text in system if text.strip()]

    seen, trimmed = character_history(
        history,
        character,
        cast,
        start=story.setup.starting_scene,
        texts=texts,
        count=count,
        budget=budget,
    )
    body = [texts["characters.own.history.heading"]]
    if trimmed:
        body.append(texts["characters.own.trimmed"])
    body.append("\n\n".join(seen))
    scene = scene_at(history, story.scene, cast_ids=[c.id for c in cast])
    body.append(_scene_lines(scene, cast, character, texts))
    body.append(texts.fill("characters.own.ask", name=character.name))
    return [
        # Marked for the cache at its end: the system message is the same
        # every round, so a model that caches reads it back.
        PromptMessage(
            role="system",
            parts=tuple(
                ContentPart(text=text, cache_breakpoint=index == len(system) - 1)
                for index, text in enumerate(system)
            ),
        ),
        PromptMessage(role="user", parts=tuple(ContentPart(text=t) for t in body if t.strip())),
    ]
