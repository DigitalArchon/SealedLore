"""What a private scene's model is told, and how the scene is handed back. Pure.

A private scene is played on a model the author trusts with it: often smaller
and weaker at following long instructions than the story's own. So it gets a
compact version of the rules by default (the author may choose the full rules
or write their own, `PrivatePrompt`), always with the story's facts and the
per-turn reminders around it: a custom prompt changes how the model writes,
never who the author's character is or who is in the room.

When the scene ends, the private model — and only it — summarises the
scene's own messages for the story's model: plot kept, intimacy kept vague.
"""

from __future__ import annotations

from collections.abc import Sequence

from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.rules import narration_phrase
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.private import PrivatePrompt
from sealedlore.models.story import Story

SECTION_PRIVATE_RULES = "system.private_rules"
SECTION_PRIVATE_NOTES = "system.private_notes"


def private_rules_sections(
    story: Story, full: Sequence[tuple[str, str]], texts: PromptTexts = DEFAULT_TEXTS
) -> list[tuple[str, str]]:
    """The system block's rules for a private scene, in place of `full`
    (today's rules, style, perspective and world sections, as (name, text))."""
    prompt: PrivatePrompt = story.private_prompt
    if prompt.mode == "full":
        sections = list(full)
    elif prompt.mode == "custom" and prompt.custom.strip():
        sections = [(SECTION_PRIVATE_RULES, prompt.custom.strip())]
    else:
        # Compact: the short rules, with the story's own style and perspective.
        keep = {"system.style", "system.perspective"}
        sections = [(SECTION_PRIVATE_RULES, texts["private.rules"].strip())]
        sections += [(name, text) for name, text in full if name in keep]
    if prompt.notes.strip():
        sections.append((SECTION_PRIVATE_NOTES, "# FOR THIS SCENE\n" + prompt.notes.strip()))
    return sections


def build_private_summary_messages(
    scene_text: str,
    cast_names: Sequence[str],
    person: str,
    held: str | None = None,
    tense: str | None = None,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """The scene's own messages and nothing earlier: no chapters, no handoff.

    Live, the private model kept someone in the room whom the author's turn
    had sent out, and its summaries then had them witness the private part.
    Naming whose turns are the author's, and that they are true, is the fix
    the author asked for (the scene read stays paused in a private scene).
    """
    names = ", ".join(cast_names) or "none on file"
    held_line = (
        texts.fill("private.summary.held", held=held) if held else texts["private.summary.no_one"]
    )
    # The summary joins the story as a passage, so it is told in the story's
    # own person and tense.
    system = texts.fill(
        "private.summary", narration=narration_phrase(person, tense, held), held_line=held_line
    ).strip()
    user = f"PEOPLE IN THIS STORY: {names}\n\nTHE PRIVATE SCENE:\n\n{scene_text}"
    return [
        PromptMessage(role="system", parts=(ContentPart(system),)),
        PromptMessage(role="user", parts=(ContentPart(user),)),
    ]


def build_private_condense_messages(
    previous: str | None,
    scene_text: str,
    cast_names: Sequence[str],
    person: str,
    *,
    held: str | None = None,
    words: int = 600,
    tense: str | None = None,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """The rolling summary of a private scene's oldest messages, on the
    private model and nobody else (`PrivateRuntime._condense`)."""
    names = ", ".join(cast_names) or "none on file"
    held_line = (
        texts.fill("private.condense.held", held=held) if held else texts["private.condense.no_one"]
    )
    system = texts.fill(
        "private.condense",
        narration=narration_phrase(person, tense, held),
        held_line=held_line,
        words=str(words),
    ).strip()
    user = (
        f"PEOPLE IN THIS STORY: {names}\n\n"
        f"THE SUMMARY SO FAR:\n\n{previous.strip() if previous else '(nothing yet)'}\n\n"
        f"THE MESSAGES THAT COME NEXT:\n\n{scene_text}"
    )
    return [
        PromptMessage(role="system", parts=(ContentPart(system),)),
        PromptMessage(role="user", parts=(ContentPart(user),)),
    ]


def build_handoff_messages(
    story_text: str, words: int, texts: PromptTexts = DEFAULT_TEXTS
) -> list[PromptMessage]:
    system = texts.fill("private.handoff", words=str(words)).strip()
    return [
        PromptMessage(role="system", parts=(ContentPart(system),)),
        PromptMessage(role="user", parts=(ContentPart(f"THE STORY SO FAR:\n\n{story_text}"),)),
    ]
