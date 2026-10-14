"""Portraits of the characters (the author, Oct 15 2026): offered when a
story starts and on the Cast page, drawn by `Config.portrait_model`.

Pure: who can have one, the writer's request and its reply, a prompt built
from the card when the writer gives none, and the size asked for. The
session makes the call (engine/session_portraits.py); each portrait is then
an ordinary approved image request (engine/images.py), and the author sees
and may change every prompt before anything is drawn.
"""

from __future__ import annotations

from collections.abc import Sequence

from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.world_pictures import looks_of
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.character import Character
from sealedlore.providers.images import ImageModelInfo

# The words a portrait prompt runs to: enough for a face and clothes, under
# the 77-token reach of the SDXL models' text encoder for the gist of it.
PORTRAIT_WORDS = "50 to 90"
PORTRAIT_MAX_TOKENS = 3000
# The setting the writer is given: the world's opening, not its whole bible.
SETTING_CHARS = 1500
# The largest square a portrait is asked for.
PORTRAIT_SIDE = 1024


def portrait_style(image_style: str, texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """The story's picture style, or the portrait default."""
    return " ".join(image_style.split()).rstrip(".") or texts["portraits.style"]


def _card(character: Character) -> str:
    lines = [f"## {character.name}"]
    if character.canon:
        lines.append(f"From: {character.canon}")
    for text in (character.summary, character.full_description):
        if text and text.strip():
            lines.append(text.strip())
    return "\n".join(lines)


def build_portrait_messages(
    characters: Sequence[Character],
    *,
    world_bible: str,
    title: str,
    image_style: str,
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """The writer's request: its instructions, the setting and each card."""
    system = texts.fill(
        "portraits.system",
        words=PORTRAIT_WORDS,
        style=portrait_style(image_style, texts),
        no_writing=texts["portraits.no_writing"],
    )
    world = " ".join((world_bible or "").split())[:SETTING_CHARS] or title
    user = "THE SETTING:\n" + world + "\n\nTHE CHARACTERS:\n\n"
    user += "\n\n".join(_card(c) for c in characters)
    return [
        PromptMessage(role="system", parts=(ContentPart(text=system),)),
        PromptMessage(role="user", parts=(ContentPart(text=user),)),
    ]


def fallback_prompt(
    character: Character, *, image_style: str, texts: PromptTexts = DEFAULT_TEXTS
) -> str:
    """A prompt from the card alone, for the author to edit."""
    looks = looks_of(character.summary, character.full_description) or "A person."
    return texts.fill(
        "portraits.fallback",
        style=portrait_style(image_style, texts),
        looks=looks if looks.endswith((".", "…", "!", "?")) else looks + ".",
        no_writing=texts["portraits.no_writing"],
    )


def parse_portrait_prompts(text: str, characters: Sequence[Character]) -> dict[str, str]:
    """The writer's prompts by character id. A name it gave that is no one
    asked about is dropped; whoever it left out has no entry (the caller
    builds theirs from the card). Raises ValueError on a reply with no JSON."""
    reply = extract_json(text, dict, "portrait prompts")
    by_name = {c.name.strip().lower(): c.id for c in characters}
    prompts: dict[str, str] = {}
    for item in reply.get("portraits") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().lower()
        prompt = " ".join(str(item.get("prompt") or "").split())
        character_id = by_name.get(name)
        if character_id is not None and prompt and character_id not in prompts:
            prompts[character_id] = prompt
    return prompts


def portrait_size(model: ImageModelInfo | None) -> str:
    """The largest square the model lists up to PORTRAIT_SIDE (a portrait
    is shown large on the Cast page); else its first size, or 1024x1024."""
    best: tuple[int, str] | None = None
    for size in model.resolutions if model is not None else ():
        parts = size.replace("*", "x").split("x")
        if len(parts) == 2 and parts[0] == parts[1] and parts[0].isdigit():
            side = int(parts[0])
            if side <= PORTRAIT_SIDE and (best is None or side > best[0]):
                best = (side, size)
    if best is not None:
        return best[1]
    if model is not None and model.resolutions:
        return "1k" if "1k" in model.resolutions else model.resolutions[0]
    return "1024x1024"
