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
- the most recent of it that fits `HISTORY_TOKENS`, and before it what they
  remember of the rest (`Recollection`, models/memory.py): their own account
  of each older stretch, written from those messages alone. The story's
  chapters can't stand in for it: they cover scenes the character wasn't in.

A trial, not a core feature: every wording is a first draft, nothing here is
measured, and a story that names no character's own model is unchanged.
"""

from __future__ import annotations

import hashlib
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
from sealedlore.models.memory import Recollection
from sealedlore.models.node import DIRECTOR_SPEAKER_ID, Node
from sealedlore.models.scene import SceneState
from sealedlore.models.story import Story

# How much of the story a character's model is sent whole, newest first; what
# they saw before it, they are sent as they remember it.
HISTORY_TOKENS = 12_000
# A recollection covers this many of the messages a character saw, written
# once they are all older than the recent window, in about this many words.
RECOLLECT_MESSAGES = 20
RECOLLECT_WORDS = 200

# One message as a character saw it: its id, and its text as they saw it.
Entry = tuple[str, str]


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
    lines = [f"{tag}\n{node.content.strip()}" if node.content.strip() else ""]
    # Their own answer to this turn, when the story has them answer after the
    # author's (`round_after`), as after a passage.
    own = [r for r in node.meta.reactions if r.character_id == character.id]
    lines += [texts.fill("characters.own.answered", text=r.text.strip()) for r in own]
    return "\n\n".join(line for line in lines if line)


def seen_entries(
    history: Sequence[Node],
    character: Character,
    cast: Sequence[Character],
    *,
    start: SceneState,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[Entry]:
    """The story as this character saw it, oldest first.

    A message counts when the character was in the scene before it or after
    it (a snapshot on a node is the scene after it), so the passage they
    arrive in, and the one they leave in, are both theirs. `history` is the
    path as models may see it: a private scene is already gone from it.
    """
    cast_by_id = {c.id: c for c in cast}
    seen: list[Entry] = []
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
            seen.append((node.id, text))
    return seen


def recent_window(
    entries: Sequence[Entry], *, count: Callable[[str], int] = _rough_tokens, budget: int
) -> tuple[list[str], bool]:
    """The newest of `entries` that fit `budget` (the newest always), and
    whether any were left out."""
    kept: list[str] = []
    spent = 0
    for _node_id, text in reversed(entries):
        cost = count(text)
        if kept and spent + cost > budget:
            break
        kept.append(text)
        spent += cost
    kept.reverse()
    return kept, len(kept) < len(entries)


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
    """The newest of what this character saw that fits `budget`, oldest first,
    and whether older messages were left out."""
    seen = seen_entries(history, character, cast, start=start, texts=texts)
    return recent_window(seen, count=count, budget=budget)


def fingerprint(entries: Sequence[Entry]) -> str:
    """What a recollection was written from: an edit to any of it changes this."""
    return hashlib.sha256("\n\n".join(text for _id, text in entries).encode()).hexdigest()


def memory_chain(
    recollections: Sequence[Recollection], character_id: str, seen: Sequence[Entry]
) -> tuple[list[Recollection], int]:
    """The character's recollections that still match what they saw, from the
    first message on, each continuing where the one before stopped, and how
    many of `seen` they cover. As the story's chapters are matched
    (`archival.split_path`): a branch or an edit stops the chain where it
    differs, and the rest is remembered again for that line. Where two start
    at one message, the longer that still matches wins."""
    ids = [node_id for node_id, _text in seen]
    by_head: dict[str, list[Recollection]] = {}
    for memory in recollections:
        if memory.character_id == character_id and memory.covered_node_ids:
            by_head.setdefault(memory.covered_node_ids[0], []).append(memory)
    for heads in by_head.values():
        heads.sort(key=lambda memory: len(memory.covered_node_ids), reverse=True)
    chain: list[Recollection] = []
    position = 0
    while position < len(ids):
        found = None
        for memory in by_head.get(ids[position], ()):
            covered = memory.covered_node_ids
            end = position + len(covered)
            if ids[position:end] == covered and memory.fingerprint == fingerprint(
                seen[position:end]
            ):
                found = memory
                break
        if found is None:
            break
        chain.append(found)
        position += len(found.covered_node_ids)
    return chain, position


def due_to_recollect(
    seen: Sequence[Entry],
    covered: int,
    *,
    count: Callable[[str], int] = _rough_tokens,
    budget: int = HISTORY_TOKENS,
    size: int = RECOLLECT_MESSAGES,
) -> list[list[Entry]]:
    """The stretches to remember now, oldest first: whole stretches of `size`
    from the first not yet remembered, until what is left fits `budget`. A
    stretch is only taken whole, so one is written once and stays."""
    rest = list(seen[covered:])
    due: list[list[Entry]] = []
    while len(rest) > size and sum(count(text) for _id, text in rest) > budget:
        due.append(rest[:size])
        rest = rest[size:]
    return due


def recollect_messages(
    character: Character,
    entries: Sequence[Entry],
    *,
    texts: PromptTexts = DEFAULT_TEXTS,
    words: int = RECOLLECT_WORDS,
) -> list[PromptMessage]:
    """The request that writes one recollection: the character's own account
    of a stretch of the story they saw, from those messages alone."""
    return [
        PromptMessage(
            role="system",
            parts=(
                ContentPart(
                    text=texts.fill(
                        "characters.own.recollect", name=character.name, words=str(words)
                    )
                ),
            ),
        ),
        PromptMessage(
            role="user",
            parts=(ContentPart(text="\n\n".join(text for _id, text in entries)),),
        ),
    ]


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
    recollections: Sequence[Recollection] = (),
    unsure: bool = True,
    after_turn_of: str | None = None,
) -> list[PromptMessage]:
    """One character's request: their card and the setting in the system
    message (the same every round, so a model that caches can), what they
    remember of older scenes (`recollections`, those still matching), the
    recent story as they saw it ending on the passage they answer, then the
    ask. `unsure`: told to say so when they don't remember something.
    `after_turn_of`: the story has them answer after the author's turn, the
    last of `history`; who took it (the author's character, or "the author"),
    for the ask that says so (`characters.own.ask.after`).

    `cast` is the cast models may see (plot-hidden cards left out)."""
    system = [texts.fill("characters.own.system", name=character.name, card=_card(character))]
    if (character.voice_notes or "").strip():
        system.append(texts.fill("characters.own.voice", voice=character.voice_notes.strip()))
    skills = render_competence_line(character).removeprefix("Competence: ")
    if skills:
        system.append(texts.fill("characters.own.skills", skills=skills))
    if (character.own_notes or "").strip():
        system.append(
            texts.fill("characters.own.notes", name=character.name, notes=character.own_notes)
        )
    if (story.world_bible or "").strip():
        system.append(texts.fill("characters.own.setting", setting=story.world_bible.strip()))
    system.append(texts.fill("characters.own.rules", name=character.name))
    if unsure:
        system.append(texts.fill("characters.own.unsure", name=character.name))
    system = [text for text in system if text.strip()]

    seen = seen_entries(history, character, cast, start=story.setup.starting_scene, texts=texts)
    chain, covered = memory_chain(recollections, character.id, seen)
    recent, trimmed = recent_window(seen[covered:], count=count, budget=budget)
    body: list[str] = []
    if chain:
        body.append(texts["characters.own.memory.heading"])
        body.append("\n\n".join(memory.content.strip() for memory in chain))
    body.append(texts["characters.own.history.heading"])
    if trimmed:
        body.append(texts["characters.own.trimmed"])
    body.append("\n\n".join(recent))
    scene = scene_at(history, story.scene, cast_ids=[c.id for c in cast])
    body.append(_scene_lines(scene, cast, character, texts))
    if after_turn_of is not None:
        body.append(
            texts.fill("characters.own.ask.after", name=character.name, player=after_turn_of)
        )
    else:
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
