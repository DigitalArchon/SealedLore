"""Writing an image prompt from the story, for the author to approve. Pure.

The request rides on the storyteller's cached prefix (`assemble_question`
with `rendered`), so whoever writes it has read the whole story: what people
look like, what they wear, where they are. It is told which reference
pictures exist and chooses the ones in the picture, in order, naming them
"image 1", "image 2" in the prompt the way Seedream's multi-image editing
reads them.

Nothing here sends anything: the prompt goes to the author, who may change
every word before approving it, and what they approve is what is sent.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.models.image import RefUse

IMAGE_PROMPT_MAX_TOKENS = 3000
# The writer is asked for about this many words; the author can go longer.
IMAGE_PROMPT_WORDS = "80 to 200"


@dataclass(frozen=True)
class RefChoice:
    """A reference picture the writer may use, with where its owner stands."""

    key: str  # "R1", "R2": the writer's handle, never shown to the image model
    use: RefUse
    present: bool


@dataclass(frozen=True)
class ImagePromptDraft:
    prompt: str
    references: list[RefUse]
    # Keys the writer named that weren't offered, or were over the limit.
    dropped: list[str]


def ref_choices(uses: Sequence[tuple[RefUse, bool]]) -> list[RefChoice]:
    return [
        RefChoice(key=f"R{index}", use=use, present=present)
        for index, (use, present) in enumerate(uses, start=1)
    ]


def _catalogue(choices: Sequence[RefChoice]) -> str:
    lines = []
    for choice in choices:
        use = choice.use
        kind = "lore" if use.owner_kind == "lore" else "character"
        where = ", in the scene now" if choice.present else ""
        caption = f": {use.caption}" if use.caption else ""
        lines.append(f"- {choice.key} — {use.owner_name} ({kind}{where}){caption}")
    return "\n".join(lines)


def build_image_question(
    *,
    direction: str,
    held_name: str | None,
    style: str,
    choices: Sequence[RefChoice],
    max_refs: int,
    fixed: Sequence[RefChoice] | None = None,
    anchor_is_leaf: bool = True,
    chat: bool = False,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> str:
    """The request for an image prompt, in place of an author's question.
    In a simple chat (`chat`) it speaks of the conversation, not a story."""
    asked = "asked" if direction.strip() else "unasked"
    if chat:
        subject = texts[f"pictures.subject.chat_{asked}"]
    else:
        moment = texts["pictures.moment." + ("last" if anchor_is_leaf else "earlier")]
        subject = texts.fill(f"pictures.subject.story_{asked}", moment=moment)
    kind = "chat" if chat else "story"
    parts = [
        "# IMAGE PROMPT",
        texts.fill(f"pictures.opening.{kind}", subject=subject),
        "",
        texts.fill(f"pictures.rules.{kind}", words=IMAGE_PROMPT_WORDS),
    ]
    if held_name:
        parts.append(texts.fill("pictures.held", held=held_name))
    parts.append(
        texts.fill("pictures.style", style=style.strip())
        if style.strip()
        else texts["pictures.style.free"]
    )
    if fixed is not None:
        if fixed:
            parts += [
                "",
                "# REFERENCE PICTURES",
                texts["pictures.refs.fixed"],
                _catalogue(fixed),
                texts["pictures.refs.fixed_after"],
            ]
        else:
            parts += ["", texts["pictures.refs.none"]]
    elif choices:
        parts += [
            "",
            "# REFERENCE PICTURES",
            texts["pictures.refs.offered"],
            _catalogue(choices),
            texts.fill("pictures.refs.offered_after", max=str(max_refs)),
        ]
    else:
        parts += ["", texts["pictures.refs.nothing"]]
    if direction.strip():
        parts += ["", "# THE AUTHOR ASKS FOR", direction.strip()]
    return "\n".join(parts)


_KEY = re.compile(r"R\d+")


def parse_image_prompt(
    text: str, choices: Sequence[RefChoice], *, max_refs: int
) -> ImagePromptDraft:
    """The writer's prompt and the references it chose, in its order.

    Raises ValueError when there is no prompt to show.
    """
    data = extract_json(text, dict, "image prompt")
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("the reply held no prompt")
    by_key = {choice.key: choice for choice in choices}
    chosen: list[RefUse] = []
    dropped: list[str] = []
    raw = data.get("references")
    for item in raw if isinstance(raw, list) else []:
        key = item.strip() if isinstance(item, str) else ""
        match = _KEY.search(key)
        key = match.group(0) if match else key
        choice = by_key.get(key)
        if choice is None or any(use.ref_id == choice.use.ref_id for use in chosen):
            dropped.append(str(item))
            continue
        if len(chosen) >= max_refs:
            dropped.append(key)
            continue
        chosen.append(choice.use)
    return ImagePromptDraft(prompt=prompt.strip(), references=chosen, dropped=dropped)


_IMAGE_NUMBER = re.compile(r"\bimage\s+(\d+)\b", re.IGNORECASE)


def mentioned_numbers(prompt: str) -> set[int]:
    """The "image N" a prompt refers to, for the dialog's check."""
    return {int(number) for number in _IMAGE_NUMBER.findall(prompt)}
