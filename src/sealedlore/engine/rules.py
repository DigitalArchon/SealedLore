"""Renderers for each section of the storyteller's prompt.

The texts themselves are in `engine/prompt_texts.py`, where the author can see
and change them; every renderer takes the texts in force (`texts`).

Everything here is a pure function of story state. The split matters for
caching (§6.1): the rules and the `render_*` helpers used by the system
block must never depend on turn-specific state, or the cache prefix dies every
turn. The volatile-tail renderers are where per-turn instructions belong.

The two rules this app exists to enforce — never write the user's character,
and never move someone into a scene by shortcut — are stated here in general
terms and restated per turn with names by the tail renderers. The second is an
arrival rule, not a ban: someone not in the scene may come into it, shown
arriving, but nobody is simply there and nobody perceives a scene from outside
it.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Literal

from sealedlore.engine.ledger import render_ledger_block
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.response_style import LengthInstruction, story_default
from sealedlore.engine.scene_state import same_person
from sealedlore.models.character import Character
from sealedlore.models.lore import LoreEntry
from sealedlore.models.node import (
    DIRECTOR_SPEAKER_ID,
    NARRATOR_SPEAKER_ID,
    AgencyMode,
    Node,
    NpcScope,
    Roll,
)
from sealedlore.models.scene import SceneLogEntry, SceneState
from sealedlore.models.story import (
    NarrativePerson,
    NarrativeTense,
    Perspective,
    StyleDirectives,
    WorldActivity,
)
from sealedlore.models.summary import Summary

_ROLL_BAND_LABELS = {
    "critical_success": "CRITICAL SUCCESS",
    "success": "SUCCESS",
    "success_at_a_cost": "SUCCESS AT A COST",
    "failure": "FAILURE",
    "critical_failure": "CRITICAL FAILURE",
}


def _joined(blocks: Sequence[str]) -> str:
    return "\n".join(block for block in blocks if block)


def character_display_name(character: Character | None, speaker_id: str) -> str:
    if character is not None:
        return character.name
    if speaker_id == NARRATOR_SPEAKER_ID:
        return "Narrator"
    if speaker_id == DIRECTOR_SPEAKER_ID:
        return "Director"
    return "Unknown"


LENGTH_LINE_PREFIX = "Length of your passages:"


def length_line(style: StyleDirectives, texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """The standing length, first in the style block, hand-edited or not."""
    return f"{LENGTH_LINE_PREFIX} {story_default(style, texts).directive}"


def without_length_line(text: str) -> str:
    """A hand-edited block's own text: the length line is never part of it.

    It used to be copied in when the block was detached, and then stayed as it
    was: change Length afterwards and the cached block said "150 to 300 words"
    while the turn's directives said "one to three sentences".
    """
    return "\n".join(
        line for line in text.strip().splitlines() if not line.startswith(LENGTH_LINE_PREFIX)
    ).strip()


def _sentence(label: str, value: str) -> str:
    """ "Label: value." without doubling a full stop the value already has
    ("Content limits: No graphic gore.." was in a real prompt)."""
    value = value.strip()
    return f"{label}: {value}" + ("" if value.endswith((".", "!", "?", "…")) else ".")


def render_style_block(style: StyleDirectives, texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """The style directive block for the system section (§8.2).

    A hand-edited block is used verbatim: editing detaches the text from the
    form until the user resets it. The length line is the form's either way.
    """
    if style.detached and style.custom_text and without_length_line(style.custom_text):
        return f"# STYLE\n\n{length_line(style, texts)}\n{without_length_line(style.custom_text)}"

    # Always present: a story with no length instruction is how a one-line
    # action came back as 363 words. The standing default lives here; a
    # per-turn override belongs in the tail, never in this block.
    lines: list[str] = [length_line(style, texts)]
    if style.person:
        lines.append(f"Narrative person: {style.person}.")
    if style.tense:
        lines.append(f"Narrative tense: {style.tense}.")
    fields = (
        ("Prose density", style.prose_density),
        ("Dialogue to narration balance", style.dialogue_narration_balance),
        ("Pacing", style.pacing),
        ("Content rating", style.content_rating),
        ("Content limits", style.content_limits),
    )
    lines += [_sentence(label, value) for label, value in fields if value and value.strip()]
    if style.forbidden_phrases:
        forbidden = "; ".join(style.forbidden_phrases)
        lines.append(f"Never use these words or phrases: {forbidden}.")
    if style.notes and style.notes.strip():
        lines.append(style.notes.strip())

    return "# STYLE\n\n" + "\n".join(lines)


# Its own system section rather than a line in the style block: a hand-edited
# (detached) style block replaces that block wholesale, and perspective carries
# the presence rules, which must survive a hand edit.


def render_perspective_block(perspective: Perspective, texts: PromptTexts = DEFAULT_TEXTS) -> str:
    return texts[f"storyteller.perspective.{perspective}"]


def render_world_block(activity: WorldActivity, texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """The world's own momentum, per story (stable, so it stays cacheable)."""
    return texts[f"storyteller.world.{activity}"]


def render_world_bible(world_bible: str | None) -> str:
    if not world_bible or not world_bible.strip():
        return ""
    # "SETTING", not "WORLD": beside the world-activity rules ("THE WORLD
    # ACTS ON ITS OWN") two headings a word apart read as one section.
    return f"# SETTING\n\n{world_bible.strip()}"


def render_competence_line(character: Character) -> str:
    parts: list[str] = []
    if character.competence.tiers:
        tiers = "; ".join(f"{domain} {tier}" for domain, tier in character.competence.tiers.items())
        parts.append(tiers)
    if character.competence.notes:
        parts.append(character.competence.notes.strip())
    if not parts:
        return ""
    return "Competence: " + " — ".join(parts)


def render_cast_sheet(character: Character, *, include_full_description: bool) -> str:
    heading = character.name
    if character.aliases:
        heading += " — also known as " + ", ".join(character.aliases)

    lines = [heading]
    if character.canon:
        lines.append(f"  From: {character.canon.strip()}")
    if character.summary:
        lines.append(f"  {character.summary.strip()}")
    if character.voice_notes:
        lines.append(f"  Voice: {character.voice_notes.strip()}")
    competence = render_competence_line(character)
    if competence:
        lines.append(f"  {competence}")
    if include_full_description and character.full_description:
        lines.append(f"  {character.full_description.strip()}")
    return "\n".join(lines)


def render_cast_block(
    characters: Sequence[Character],
    *,
    include_full_descriptions: bool,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> str:
    if not characters:
        return ""
    sheets = [
        render_cast_sheet(character, include_full_description=include_full_descriptions)
        for character in characters
    ]
    return texts["storyteller.cast_header"] + "\n\n" + "\n\n".join(sheets)


def render_summaries_block(summaries: Sequence[Summary], texts: PromptTexts = DEFAULT_TEXTS) -> str:
    if not summaries:
        return ""
    entries = []
    first = 1
    for summary in summaries:
        marker = " (revised by the author)" if summary.hand_edited else ""
        last = first + summary.chapters - 1
        heading = f"Chapter {first}" if last == first else f"Chapters {first}–{last}"
        entries.append(f"## {heading}{marker}\n\n{summary.content.strip()}")
        first = last + 1
    ledger = render_ledger_block(summaries, texts)
    text = texts["storyteller.story_so_far"] + "\n\n" + "\n\n".join(entries)
    # In the same section: it changes only when the chapters do.
    return f"{text}\n\n{ledger}" if ledger else text


def render_lore_block(entries: Sequence[LoreEntry], texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """Retrieved lore, framed as reference rather than instruction (§7)."""
    if not entries:
        return ""
    rendered = [f"## {entry.title}\n{entry.content.strip()}" for entry in entries]
    return texts["turn.lore"] + "\n\n" + "\n\n".join(rendered)


def render_standing_lore(entries: Sequence[LoreEntry], texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """Lore sent every turn, in the cached system block (`retrieval.lore_layout`).

    The whole lorebook while it is small, or only its `always_on` entries once
    the rest is selected per turn. Not chosen for this scene, so it says so,
    and keeps `render_lore_block`'s line about the roster.
    """
    if not entries:
        return ""
    rendered = [f"## {entry.title}\n{entry.content.strip()}" for entry in entries]
    return texts["storyteller.standing_lore"] + "\n\n" + "\n\n".join(rendered)


Voicing = Literal["held", "author", "storyteller", "either"]


def voicing(
    character: Character | None,
    character_id: str,
    controlled_character_id: str | None,
) -> Voicing:
    """Who writes this character this turn.

    - "held": the author is holding them right now (load-bearing rule 1).
    - "author": theirs on every turn — `author_only`, for a character the
      author switches between rather than hands over.
    - "storyteller": the author never plays them (`is_player_available` off),
      so the model voices them outright.
    - "either": the default — the model voices them on turns the author is not
      holding them.
    """
    if character_id == controlled_character_id:
        return "held"
    if character is None:
        return "either"
    if character.author_only:
        return "author"
    if not character.is_player_available:
        return "storyteller"
    return "either"


def _join_names(names: Sequence[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def author_held_names(
    cast_by_id: dict[str, Character], controlled_character_id: str | None
) -> list[str]:
    """Present-or-not, the characters the model must never voice this turn."""
    return [
        character.name
        for character_id, character in cast_by_id.items()
        if voicing(character, character_id, controlled_character_id) in ("held", "author")
    ]


def render_scene_block(
    scene: SceneState,
    cast_by_id: dict[str, Character],
    controlled_character_id: str | None,
    perspective: Perspective = "with_character",
    supporting: Sequence[Character] = (),
    texts: PromptTexts = DEFAULT_TEXTS,
    not_placed: Collection[str] = (),
    show_time: bool = True,
) -> str:
    """Scene framing plus the presence roster (§3.4).

    `not_placed` is cast the opening places (`prompt.not_placed_at_start`):
    listed apart, never as elsewhere. `show_time` is
    `prompt.scene_time_shown`: the card's Time line only where it helps.

    Location, time and situation come from the scene read after each passage
    (`source == "story"`) or from the author's hand, and are labelled by
    which: an author's setting can go stale, so it is labelled as the
    author's last setting, and the prose since wins.
    """
    lines = ["# THIS SCENE"]
    setting = [
        (label, value)
        for label, value in (
            ("Location", scene.location),
            ("Time", scene.time_of_day if show_time else None),
            ("Situation", scene.situation),
        )
        if value
    ]
    if setting:
        source = "story" if scene.source == "story" else "author"
        lines.append(texts[f"turn.scene.set_by_{source}"])
        lines.extend(f"{label}: {value}" for label, value in setting)
    if scene.privacy in ("private", "public"):
        lines.append(texts[f"turn.scene.{scene.privacy}"])

    present_ids = list(scene.present_character_ids)
    lines.append("")
    lines.append(texts["turn.roster.present"])
    handed_over = False
    roles = {
        "held": "turn.roster.held",
        "author": "turn.roster.author_only",
        "storyteller": "turn.roster.storytellers",
    }
    if present_ids:
        for character_id in present_ids:
            character = cast_by_id.get(character_id)
            name = character.name if character else character_id
            role = voicing(character, character_id, controlled_character_id)
            handed_over = handed_over or role == "storyteller"
            if role in roles:
                lines.append(f"  - {name} {texts[roles[role]]}")
            else:
                lines.append(f"  - {name}")
    elif not scene.present_others:
        lines.append(f"  - {texts['turn.roster.nobody']}")

    others = [
        name
        for name in scene.present_others
        if not any(same_person(name, character) for character in cast_by_id.values())
    ]
    if others:
        lines.append(f"  - {texts.fill('turn.roster.others', names=', '.join(others))}")

    if handed_over:
        lines.append("")
        lines.append(texts["turn.roster.handed_over"])

    # A note about where someone is only applies while they are away. Live, a
    # story had a character listed present *and* carrying "still on the far
    # side — not reachable", which is a contradiction the model has to resolve
    # for itself, and it resolved it by leaving them out.
    notes = {
        entry.character_id: entry.note
        for entry in scene.offstage_but_nearby
        if entry.character_id not in present_ids
    }
    unplaced = [
        character
        for character_id, character in cast_by_id.items()
        if character_id in not_placed and character_id not in present_ids
    ]
    if unplaced:
        lines.append("")
        lines.append(texts["turn.roster.not_placed"])
        lines.extend(f"  - {character.name}" for character in unplaced)
    elsewhere = [
        character
        for character_id, character in cast_by_id.items()
        if character_id not in present_ids and character not in unplaced
    ]
    if elsewhere:
        lines.append("")
        lines.append(texts[f"turn.roster.cast_elsewhere.{perspective}"])
        for character in elsewhere:
            note = notes.get(character.id)
            lines.append(f"  - {character.name}" + (f" — {note}" if note else ""))

    # Only once the scene has kept track of everyone: an untracked card says
    # nothing about the story's own people, and listing them here as absent
    # would write Hollis out of a room he is standing in.
    away = [
        character
        for character in supporting
        if scene.tracked and not any(same_person(name, character) for name in scene.present_others)
    ]
    if away:
        lines.append("")
        lines.append(texts["turn.roster.on_file_elsewhere"])
        lines.append("  " + ", ".join(character.name for character in away))

    return "\n".join(lines)


def render_scene_log_block(
    entries: Sequence[SceneLogEntry], texts: PromptTexts = DEFAULT_TEXTS
) -> str:
    """Who was where for the scenes just past: the record of who can know what."""
    if not entries:
        return ""
    lines = [texts["turn.scene_log"]]
    for entry in entries:
        where = entry.location or "Somewhere"
        if entry.privacy:
            where += f" ({entry.privacy})"
        who = ", ".join(entry.present_names) or "nobody recorded"
        gist = f" — {entry.gist}" if entry.gist else ""
        lines.append(f"- {where}: {who}{gist}")
    return "\n".join(lines)


def render_npc_scope_directive(
    scope: NpcScope,
    scope_ids: Sequence[str],
    cast_by_id: dict[str, Character],
    controlled_character_id: str | None = None,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> str:
    if scope == "all":
        held = author_held_names(cast_by_id, controlled_character_id)
        except_whom = f" except {_join_names(held)}" if held else ""
        return texts.fill("turn.voice.all", **{"except": except_whom})
    if scope == "model_choice":
        return texts["turn.voice.model_choice"]
    names = [
        cast_by_id[character_id].name for character_id in scope_ids if character_id in cast_by_id
    ]
    if not names:
        return texts["turn.voice.none"]
    return texts.fill("turn.voice.selected", names=", ".join(names))


def render_roll_directive(
    roll: Roll,
    explanation: str | None,
    actor: str | None = None,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> str:
    lines = [
        texts.fill(
            "turn.dice.outcome",
            outcome=_ROLL_BAND_LABELS.get(roll.band, roll.band),
            who=actor or "the author's character",
            using=f" using {roll.domain}" if roll.domain else "",
            meaning=texts[f"turn.dice.{roll.band}"],
        ),
        texts.fill("turn.dice.rolled", value=str(roll.value), die=str(roll.die)),
    ]
    if explanation:
        lines.append(explanation.strip())
    return "\n".join(lines)


def _are(names: Sequence[str]) -> str:
    return "is" if len(names) == 1 else "are"


def render_turn_directives(
    *,
    controlled_character: Character | None,
    npc_scope: NpcScope,
    npc_scope_ids: Sequence[str],
    cast_by_id: dict[str, Character],
    agency_mode: AgencyMode,
    roll: Roll | None,
    roll_explanation: str | None,
    length: LengthInstruction | None,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> str:
    lines = ["# TURN DIRECTIVES (this turn only)", ""]

    if controlled_character is not None:
        lines.append(texts.fill("turn.directives.held", held=controlled_character.name))
    else:
        lines.append(texts["turn.directives.no_one"])

    held_id = controlled_character.id if controlled_character is not None else None
    reserved = [
        character.name
        for character_id, character in cast_by_id.items()
        if voicing(character, character_id, held_id) == "author"
    ]
    if reserved:
        lines.append(
            texts.fill(
                "turn.directives.author_only", names=_join_names(reserved), **{"is": _are(reserved)}
            )
        )

    lines.append(render_npc_scope_directive(npc_scope, npc_scope_ids, cast_by_id, held_id, texts))
    if agency_mode == "dice" and roll is None:
        lines.append(texts["turn.agency.dice_no_roll"])
    else:
        lines.append(texts[f"turn.agency.{agency_mode}"])
    if roll is not None:
        lines.append(
            render_roll_directive(
                roll,
                roll_explanation,
                controlled_character.name if controlled_character else None,
                texts,
            )
        )
    if length is not None:
        which = "override" if length.is_override else "standing"
        lines.append(texts.fill(f"turn.length.{which}", length=length.directive))

    return _joined(lines)


def render_author_turn(
    *,
    speaker_id: str,
    speaker: Character | None,
    text: str,
    ooc: str | None,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> str:
    lines = ["# THE AUTHOR'S TURN", ""]
    lines.append(render_speaker_tag(speaker_id, speaker, texts))
    lines.append(text.strip())
    if ooc and ooc.strip():
        lines.append("")
        lines.append(texts["turn.ooc"])
        lines.append(ooc.strip())
    return _joined(lines)


def render_speaker_tag(
    speaker_id: str, speaker: Character | None, texts: PromptTexts = DEFAULT_TEXTS
) -> str:
    if speaker_id == NARRATOR_SPEAKER_ID:
        return texts["turn.speaker.narrator"]
    if speaker_id == DIRECTOR_SPEAKER_ID:
        return texts["turn.speaker.director"]
    name = character_display_name(speaker, speaker_id)
    return f"[{name}]"


def roll_reminder(roll: Roll | None, texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """The outcome, restated where the model reads last."""
    if roll is None:
        return ""
    # First clause only: "does not work", not the whole instruction again.
    meaning = texts[f"turn.dice.{roll.band}"].split(". ")[0]
    return " " + texts.fill(
        "turn.remember.dice", outcome=_ROLL_BAND_LABELS[roll.band], meaning=meaning
    )


_PERSON_WORD = {"first": "I", "second": "you"}


def narration_reminder(
    person: NarrativePerson | None,
    tense: NarrativeTense | None,
    name: str | None,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> str:
    """Person and tense for the REMEMBER line, with who "you" or "I" is and an
    example sentence in the story's own tense. Empty when neither is set."""
    if person is None and tense is None:
        return ""
    parts = (f"the {person} person" if person else "", f"the {tense} tense" if tense else "")
    how = " and ".join(part for part in parts if part)
    who = f': {name} is "{_PERSON_WORD[person]}"' if person in _PERSON_WORD and name else ""
    if person is not None:
        key = f"turn.narration.{person}_{tense or 'past'}"
        example = texts.fill(key, name=name or "she") if person == "third" else texts[key]
    else:
        example = texts[f"turn.narration.{tense}"]
    return texts.fill("turn.narration", how=how, who=who, example=example)


def narration_phrase(
    person: NarrativePerson | None, tense: NarrativeTense | None, held: str | None = None
) -> str:
    """ "in the past tense, in the second person (John is "you")": how text
    that joins the story (a private scene's summary) is to be narrated."""
    person = person or "third"
    who = f' ({held} is "{_PERSON_WORD[person]}")' if person in _PERSON_WORD and held else ""
    return f"in the {tense or 'past'} tense, in the {person} person{who}"


def render_reminder(
    controlled_character: Character | None,
    length: LengthInstruction | None = None,
    perspective: Perspective = "whole_story",
    roll: Roll | None = None,
    author_only_names: Sequence[str] = (),
    private: bool = False,
    direction_begins: bool = False,
    person: NarrativePerson | None = None,
    tense: NarrativeTense | None = None,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> str:
    """The last line before generation — recency is the point (§3.3)."""
    narration = narration_reminder(
        person, tense, controlled_character.name if controlled_character else None, texts
    )
    slots = {
        "narration": f"{narration} " if narration else "",
        "direction": f" {texts['turn.remember.direction']}" if direction_begins else "",
        "length": f" {length.reminder}" if length is not None and length.reminder else "",
        "others": (
            " "
            + texts.fill(
                "turn.remember.author_only",
                names=_join_names(list(author_only_names)),
                **{"is": _are(author_only_names)},
            )
            if author_only_names
            else ""
        ),
    }
    if controlled_character is None:
        return texts.fill("turn.remember.no_one", **slots)
    presence = texts["turn.remember.presence"]
    if perspective == "with_character":
        stay = texts.fill("turn.remember.stay_with", held=controlled_character.name)
        presence = f"{stay} {presence}"
    if private:
        presence += " " + texts["turn.remember.private"]
    return texts.fill(
        "turn.remember.held",
        held=controlled_character.name,
        presence=presence,
        dice=roll_reminder(roll, texts),
        **slots,
    )


def render_history_node(
    node: Node, cast_by_id: dict[str, Character], texts: PromptTexts = DEFAULT_TEXTS
) -> str:
    """One node as it appears in the verbatim history window."""
    if node.kind == "assistant":
        return node.content.strip()

    speaker = cast_by_id.get(node.speaker_id)
    lines = [render_speaker_tag(node.speaker_id, speaker, texts), node.content.strip()]
    if node.ooc and node.ooc.strip():
        lines.append(texts["turn.ooc"])
        lines.append(node.ooc.strip())
    return _joined(lines)


def render_question(
    question: str,
    earlier: Sequence[tuple[str, str]] = (),
    texts: PromptTexts = DEFAULT_TEXTS,
) -> str:
    """The tail of an aside: the rules for answering, earlier Q&A, the question."""
    lines = [texts["question.rules"]]
    if earlier:
        lines += ["", "## Earlier in this conversation"]
        for asked, answered in earlier:
            lines += ["", f"The author asked: {asked.strip()}", f"You answered: {answered.strip()}"]
    lines += ["", "## The question", "", question.strip()]
    return "\n".join(lines)
