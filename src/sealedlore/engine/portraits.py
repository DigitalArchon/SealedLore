"""Pictures for the cards (the author, Oct 15 2026): a portrait of each
character and a picture of each lore entry, offered when a story starts, on
the Cast and Lore pages, and under Story; then drawn again, or edited, from
there.

Pure: the writer's request and its reply, a prompt built from the card when
the writer gives none, an edit's prompt, and the size asked for. The session
makes the call (engine/session_portraits.py); each picture is then an
ordinary approved image request (engine/images.py), and the author sees and
may change every prompt before anything is drawn.
"""

from __future__ import annotations

from collections.abc import Sequence

from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.world_pictures import looks_of, picture_size
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.character import Character
from sealedlore.models.lore import LoreEntry
from sealedlore.models.story import Story
from sealedlore.providers.images import ImageModelInfo

# The words a prompt runs to: enough for a face and clothes, under the
# 77-token reach of the SDXL models' text encoder for the gist of it.
PORTRAIT_WORDS = "50 to 90"
PORTRAIT_MAX_TOKENS = 4000
# Cards written for in one call. All of a sample's thirteen in one call, GLM
# 5.3 spent the whole 4,000 tokens reasoning and wrote no prompt at all
# (Oct 2026), and every card fell back to its own text.
PORTRAIT_BATCH = 4
# The setting the writer is given: the world's opening, not its whole bible.
SETTING_CHARS = 1500
# The opening the writer is given, for the season and the weather.
OPENING_CHARS = 600
# A lore entry as the writer is given it.
LORE_CHARS = 900
# A portrait's shape: the tall size nearest 3:4, at most this high; a model
# with no tall size gets the largest square up to PORTRAIT_SIDE.
PORTRAIT_RATIO = 3 / 4
PORTRAIT_TALLEST = 1536
PORTRAIT_SIDE = 1024

Card = Character | LoreEntry


def portrait_style(image_style: str, texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """The story's picture style, or the portrait default."""
    return " ".join(image_style.split()).rstrip(".") or texts["portraits.style"]


def where_it_begins(story: Story) -> str:
    """The starting scene and the opening, briefly: the season and the
    weather are often said only there (Doomsville's winter is: drawn from
    the world text alone, its people wore tank tops in the sun)."""
    scene = story.setup.starting_scene
    parts = [p for p in (scene.location, scene.time_of_day, scene.situation) if p]
    opening = " ".join(story.setup.opening_text.split())[:OPENING_CHARS]
    return " ".join([*(p.rstrip(".") + "." for p in parts), opening]).strip()


def _character_card(character: Character) -> str:
    lines = [f"## {character.name}"]
    if character.canon:
        lines.append(f"From: {character.canon}")
    for text in (character.summary, character.full_description):
        if text and text.strip():
            lines.append(text.strip())
    return "\n".join(lines)


def _lore_card(entry: LoreEntry) -> str:
    return f"## {entry.title}\n" + " ".join(entry.content.split())[:LORE_CHARS]


def build_portrait_messages(
    characters: Sequence[Character],
    *,
    world_bible: str,
    title: str,
    image_style: str,
    begins: str = "",
    lore: Sequence[LoreEntry] = (),
    texts: PromptTexts = DEFAULT_TEXTS,
) -> list[PromptMessage]:
    """The writer's request: its instructions, the setting, where the story
    begins (`where_it_begins`), and each card."""
    parts = [
        texts.fill(
            "portraits.system",
            style=portrait_style(image_style, texts),
            no_writing=texts["portraits.no_writing"],
        )
    ]
    if characters:
        parts.append(texts.fill("portraits.characters", words=PORTRAIT_WORDS))
    if lore:
        parts.append(texts.fill("portraits.lore", words=PORTRAIT_WORDS))
    parts.append(texts["portraits.reply"])
    world = " ".join((world_bible or "").split())[:SETTING_CHARS] or title
    user = "THE SETTING:\n" + world + "\n\n"
    if begins:
        user += "WHERE THE STORY BEGINS:\n" + begins + "\n\n"
    if characters:
        user += "THE CHARACTERS:\n\n" + "\n\n".join(_character_card(c) for c in characters)
        user += "\n\n"
    if lore:
        user += "THE LORE ENTRIES:\n\n" + "\n\n".join(_lore_card(e) for e in lore)
    return [
        PromptMessage(role="system", parts=(ContentPart(text="\n\n".join(parts)),)),
        PromptMessage(role="user", parts=(ContentPart(text=user.rstrip()),)),
    ]


def fallback_prompt(card: Card, *, image_style: str, texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """A prompt from the card alone, for the author to edit."""
    if isinstance(card, LoreEntry):
        looks = looks_of(card.content, None) or card.title
    else:
        looks = looks_of(card.summary, card.full_description) or "A person."
    return texts.fill(
        "portraits.fallback",
        style=portrait_style(image_style, texts),
        looks=looks if looks.endswith((".", "…", "!", "?")) else looks + ".",
        no_writing=texts["portraits.no_writing"],
    )


def parse_portrait_prompts(
    text: str, characters: Sequence[Character], lore: Sequence[LoreEntry] = ()
) -> dict[str, str]:
    """The writer's prompts by card id. A name it gave that is no card asked
    about is dropped; whoever it left out has no entry (the caller builds
    theirs from the card). A lore entry with nothing to see has "". Raises
    ValueError on a reply with no JSON."""
    reply = extract_json(text, dict, "picture prompts")
    prompts: dict[str, str] = {}
    for key, field, cards in (
        ("portraits", "name", {c.name.strip().lower(): c.id for c in characters}),
        ("lore", "title", {e.title.strip().lower(): e.id for e in lore}),
    ):
        for item in reply.get(key) or []:
            if not isinstance(item, dict):
                continue
            card_id = cards.get(str(item.get(field) or "").strip().lower())
            prompt = " ".join(str(item.get("prompt") or "").split())
            if card_id is not None and card_id not in prompts:
                if prompt or key == "lore":
                    prompts[card_id] = prompt
    return prompts


def edit_prompt(change: str, *, person: bool, texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """An edit's prompt: the author's change, then what to keep."""
    change = " ".join(change.split())
    if change and not change.endswith((".", "!", "?")):
        change += "."
    return texts.fill(
        "pictures.edit",
        change=change,
        keep=texts["pictures.edit.person" if person else "pictures.edit.thing"],
        no_writing=texts["portraits.no_writing"],
    )


def portrait_size(model: ImageModelInfo | None) -> str:
    """A portrait is taller than wide (the author, Oct 15 2026: shown at the
    left of the card, beside its text): the tall size the model lists
    nearest 3:4, up to PORTRAIT_TALLEST high, the larger on a tie; else the
    largest square up to PORTRAIT_SIDE; else its first size, or 1024x1536."""
    tall: list[tuple[float, int, str]] = []
    square: list[tuple[int, str]] = []
    for size in model.resolutions if model is not None else ():
        parts = size.replace("*", "x").split("x")
        if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
            continue
        width, height = int(parts[0]), int(parts[1])
        if width < height <= PORTRAIT_TALLEST:
            tall.append((abs(width / height - PORTRAIT_RATIO), -width * height, size))
        elif width == height <= PORTRAIT_SIDE:
            square.append((width, size))
    if tall:
        return min(tall)[2]
    if square:
        return max(square)[1]
    if model is not None and model.resolutions:
        return "1k" if "1k" in model.resolutions else model.resolutions[0]
    return "1024x1536"


def card_size(card: Card, model: ImageModelInfo | None) -> str:
    """A portrait's square, or a lore picture's landscape."""
    if isinstance(card, LoreEntry):
        return picture_size(model, landscape=True)
    return portrait_size(model)
