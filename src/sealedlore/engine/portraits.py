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

import re
from collections.abc import Sequence

from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.places import place_size
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
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
# A full-length portrait's: the tall size nearest 2:3, at most this high (a
# whole figure wants the height); a model with no tall size gets the largest
# square up to FULL_SIDE, so a face a sixth of the picture high still has
# pixels (FLUX.3 Klein lists squares alone).
FULL_RATIO = 2 / 3
FULL_TALLEST = 2560
FULL_SIDE = 1536

# How much of a character a portrait shows (the author, Nov 3 2026): head
# and shoulders by default, the upper body, or the whole figure.
FRAMINGS = ("head", "torso", "full")
DEFAULT_FRAMING = "head"
FRAMING_LABELS = {"head": "Head and shoulders", "torso": "Upper body", "full": "Full body"}

Card = Character | LoreEntry


# What a card says of someone, at most, in a prompt.
LOOKS_CHARS = 420


def looks_of(summary: str | None, description: str | None) -> str:
    """What a card says of someone, shortened at a sentence for a prompt."""
    text = " ".join(" ".join(p.split()) for p in (summary, description) if p)
    if len(text) <= LOOKS_CHARS:
        return text
    cut = text[:LOOKS_CHARS]
    end = cut.rfind(". ")
    return cut[: end + 1] if end > LOOKS_CHARS // 2 else cut.rstrip() + "…"


def picture_size(model: ImageModelInfo | None, *, landscape: bool = False) -> str:
    """The smallest square size the model lists at 512 or more; `landscape`
    (a place's picture, the background), the widest landscape size up to 1360
    across. Else the model's first size; "1024x1024" (landscape "1024x768")
    when it isn't listed."""
    sizes = list(model.resolutions) if model is not None else []
    shapes = []
    for size in sizes:
        parts = size.replace("*", "x").split("x")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            shapes.append((int(parts[0]), int(parts[1]), size))
    if landscape:
        wide = sorted(s for s in shapes if s[0] > s[1] and s[0] <= 1360)
        if wide:
            return wide[-1][2]
        return sizes[0] if sizes else "1024x768"
    big_enough = sorted(s for s in shapes if s[0] == s[1] and s[0] >= 512)
    if big_enough:
        return big_enough[0][2]
    return sizes[0] if sizes else "1024x1024"


def portrait_style(
    image_style: str,
    texts: PromptTexts = DEFAULT_TEXTS,
    *,
    lore: bool = False,
    framing: str = DEFAULT_FRAMING,
) -> str:
    """The style a card's picture is drawn in: `image_style` (the story's,
    engine/picture_styles.py) or the default for portraits, or for a lore
    entry's picture (a place or a thing: told "portrait photograph, head and
    shoulders", the writer was asked to draw Hellsville so). A portrait's
    has its shot after it (`framing`, one of FRAMINGS), so the prompt says
    how much of them to show whoever wrote it."""
    default = texts["portraits.style.lore" if lore else "portraits.style"]
    style = " ".join(image_style.split()).rstrip(".") or default.rstrip(".")
    if lore:
        return style
    assert framing in FRAMINGS, framing
    return f"{style}, {texts[f'portraits.shot.{framing}']}"


def portrait_lead(
    image_style: str,
    texts: PromptTexts = DEFAULT_TEXTS,
    *,
    lore: bool = False,
    framing: str = DEFAULT_FRAMING,
    reference: int = 0,
) -> str:
    """The line a card's prompt opens with: `portrait_style`, after the
    style picture's sentence when it goes as image `reference` (0: none)."""
    lead = portrait_style(image_style, texts, lore=lore, framing=framing)
    if reference:
        return f"{texts.fill('pictures.style.reference', number=str(reference))} {lead}"
    return lead


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
    framing: str = DEFAULT_FRAMING,
    note: str = "",
) -> list[PromptMessage]:
    """The writer's request: its instructions, the setting, where the story
    begins (`where_it_begins`), and each card. `image_style`: the story's
    (engine/picture_styles.py, "" for the default), and `note` its style's
    note to the writer, if any; `framing`: how much of each character the
    portraits show."""
    parts = [
        texts.fill(
            "portraits.system",
            # A call is characters or lore entries, never both (`write_card_prompts`).
            style=portrait_style(image_style, texts, lore=not characters, framing=framing),
            no_writing=texts["portraits.no_writing"],
        )
    ]
    if characters:
        parts.append(
            texts.fill(
                "portraits.characters",
                words=PORTRAIT_WORDS,
                framing=texts[f"portraits.framing.{framing}"],
            )
        )
    if lore:
        parts.append(texts.fill("portraits.lore", words=PORTRAIT_WORDS))
    if note:
        parts.append(note)
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


def fallback_prompt(
    card: Card,
    *,
    image_style: str,
    texts: PromptTexts = DEFAULT_TEXTS,
    framing: str = DEFAULT_FRAMING,
    reference: int = 0,
) -> str:
    """A prompt from the card alone, for the author to edit; `reference`, the
    style picture's number when it goes with it (`portrait_lead`)."""
    if isinstance(card, LoreEntry):
        looks = looks_of(card.content, None) or card.title
    else:
        looks = looks_of(card.summary, card.full_description) or "A person."
    return texts.fill(
        "portraits.fallback",
        style=portrait_lead(
            image_style,
            texts,
            lore=isinstance(card, LoreEntry),
            framing=framing,
            reference=reference,
        ),
        looks=looks if looks.endswith((".", "…", "!", "?")) else looks + ".",
        no_writing=texts["portraits.no_writing"],
    )


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def with_lead(prompt: str, lead: str, no_writing: str, *, core: str = "") -> str:
    """A written prompt with its style line first (`lead`, `portrait_style`)
    and ending saying no writing. Told to copy the style, the writer (GLM
    5.3, Nov 3 2026) left it out or shortened it in 16 prompts of 31 ("Anime
    style portrait", the cel shading gone), and those drew in no chosen
    style; so the line is put first here, and the writer writes what
    follows. A prompt already opening with the whole line (its words, any
    case or punctuation) is left as it is. Blank stays blank (a lore entry
    with nothing to see). `core`: the style's own line, when `lead` is it
    after the style picture's sentence: a prompt that opens with the core
    (the writer copied it) has only the sentence put in front, not the
    style twice."""
    text = " ".join(prompt.split())
    if not text:
        return text
    lead_words, start = _words(lead), _words(text)
    core_words = _words(core)
    if lead_words and start[: len(lead_words)] != lead_words:
        if core_words and start[: len(core_words)] == core_words and lead.endswith(core):
            sentence = lead[: len(lead) - len(core)].strip()
            text = f"{sentence} {text}"
        else:
            text = f"{lead.rstrip('.')}. {text}"
    if no_writing and no_writing.lower() not in text.lower():
        text = f"{text} {no_writing}"
    return text


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


# What an edit keeps: a character's picture the person, a lore entry's (or the
# background) the place or thing, a picture of the story the whole scene.
EDIT_KEEPS = ("person", "thing", "scene")


def edit_prompt(change: str, *, keep: str, texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """An edit's prompt: the author's change, then what to keep (one of
    EDIT_KEEPS)."""
    assert keep in EDIT_KEEPS, keep
    change = " ".join(change.split())
    if change and not change.endswith((".", "!", "?")):
        change += "."
    return texts.fill(
        "pictures.edit",
        change=change,
        keep=texts[f"pictures.edit.{keep}"],
        no_writing=texts["portraits.no_writing"],
    )


def restyle_prompt(
    style: str,
    *,
    keep: str,
    change: str = "",
    texts: PromptTexts = DEFAULT_TEXTS,
    reference: int = 0,
) -> str:
    """The prompt that redraws a picture in `style` (its words), with any
    other `change`, keeping what `keep` (one of EDIT_KEEPS) says; with the
    style's own picture as image `reference` when it goes too (0: none)."""
    assert keep in EDIT_KEEPS, keep
    words = " ".join(style.split()).rstrip(".")
    change = " ".join(change.split())
    if change and not change.endswith((".", "!", "?")):
        change += "."
    opening = (
        texts.fill("pictures.restyle.reference", number=str(reference), style=words)
        if reference
        else texts.fill("pictures.restyle", style=words)
    )
    parts = [
        opening,
        change,
        texts[f"pictures.restyle.{keep}"],
        texts["portraits.no_writing"],
    ]
    return " ".join(part for part in parts if part)


def reframe_change(framing: str, texts: PromptTexts = DEFAULT_TEXTS) -> str:
    """The change that shows `framing` (one of FRAMINGS) of someone."""
    assert framing in FRAMINGS, framing
    return texts[f"pictures.reframe.{framing}"]


def size_like(model: ImageModelInfo | None, width: int, height: int) -> str:
    """The size the model lists nearest a picture's own shape (`width` by
    `height` pixels), the larger on a tie: an edit keeps the picture's shape.
    Else a portrait's or a landscape picture's size, as the shape leans."""
    if width <= 0 or height <= 0:
        return picture_size(model, landscape=True)
    wanted = width / height
    shapes: list[tuple[float, int, str]] = []
    for size in model.resolutions if model is not None else ():
        parts = size.replace("*", "x").split("x")
        if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
            continue
        w, h = int(parts[0]), int(parts[1])
        if w and h:
            shapes.append((abs(w / h - wanted), -w * h, size))
    if shapes:
        return min(shapes)[2]
    return portrait_size(model) if height > width else picture_size(model, landscape=True)


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


def full_length_size(model: ImageModelInfo | None) -> str:
    """A full-length portrait's: the tall size the model lists nearest 2:3,
    up to FULL_TALLEST high, the larger on a tie; else the largest square up
    to FULL_SIDE; else a portrait's."""
    tall: list[tuple[float, int, str]] = []
    square: list[tuple[int, str]] = []
    for size in model.resolutions if model is not None else ():
        parts = size.replace("*", "x").split("x")
        if len(parts) != 2 or not (parts[0].isdigit() and parts[1].isdigit()):
            continue
        width, height = int(parts[0]), int(parts[1])
        if width < height <= FULL_TALLEST:
            tall.append((abs(width / height - FULL_RATIO), -width * height, size))
        elif width == height <= FULL_SIDE:
            square.append((width, size))
    if tall:
        return min(tall)[2]
    if square:
        return max(square)[1]
    return portrait_size(model)


def card_size(card: Card, model: ImageModelInfo | None, framing: str = DEFAULT_FRAMING) -> str:
    """A portrait's tall size (`full_length_size` for the whole figure), or
    a lore picture's landscape, else the largest square up to PLACE_SIDE
    (engine/places.py: FLUX.3 Klein lists squares alone, and a lore picture
    used to take its first, smallest size)."""
    if isinstance(card, LoreEntry):
        return place_size(model)
    if framing == "full":
        return full_length_size(model)
    return portrait_size(model)
