"""What map-first play still needs drawn (engine/session_area.py runs it):
pure. Under the same standing approval and daily limit as map pictures:

- each building the story is in or beside is furnished (a model plans its
  rooms, engine/furnish.py), once per state;
- each kind of thing in an area the story reaches gets a sprite when its
  tile set has none (text to image with the world named; measured: with the
  set's sprites as references Klein copied one 12 times in 12);
- each character gets a walking sprite, from their portrait when they have
  one (an edit of it, the portrait alone: nine of nine kept their looks),
  else from how their card describes them;
- a story's own tile set, when the author asks for one: they describe the
  look they want, a designer model (`DESIGNER_MODEL`, Claude Opus 5.5 by the
  author's ruling, Oct 16 2026) writes the set's words and a prompt tailored
  to every picture (`map.tileset.system`), the author sees and edits them
  with the price, and each is drawn exactly as approved.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence

from sealedlore.engine.area import Area
from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.tile_sets import (
    DECK_FLOOR_ROLE,
    DECK_FLOOR_WORDS,
    GROUND_WORDS,
    OUTDOOR_SPRITES,
    SPRITES,
    TEXTURES,
    TileSetInfo,
    deck_floor_wanted,
    ground_role,
    ground_wanted,
    item_slugs,
    roof_role,
)
from sealedlore.engine.world_pictures import PictureJob
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.config import RecommendedModel

# The roles a story's own tile set must have words for; the rest are drawn
# when the world has them.
CORE_ROLES = ("floor", "cap", "face", "rug", "out-ground", "out-road", "out-wall")
# Sprites are drawn square at this size, textures at TEXTURE_SIZE.
SPRITE_SIZE = "768x768"
TEXTURE_SIZE = "1024x1024"
# A run draws at most this many pictures, nearest the story first.
RUN_LIMIT = 12
# The designer's prompt for a role is kept in the set's words under this
# prefix ("prompt:floor"), so the set carries what each picture was drawn from.
PROMPT_KEY = "prompt:"
# How long a prompt may be, and the words of one asset.
WORDS_LIMIT = 200
PROMPT_LIMIT = 1500

# The model that designs a story's own tile set when none is chosen
# (`Config.tileset_model`), on NanoGPT: the author's ruling (Oct 16 2026,
# "use Opus 5.5 to manage the calls required to create them"). On another
# endpoint the map art model designs it.
DESIGNER_MODEL = "anthropic/claude-opus-5.5"
DESIGNER_MODELS: tuple[RecommendedModel, ...] = (
    RecommendedModel(
        id=DESIGNER_MODEL,
        label="Claude Opus 5.5",
        tag="default",
        note="The author's choice to design a set: it writes the set's words and a prompt "
        "tailored to every picture from your description and the world. One call a set, "
        "a few cents.",
    ),
    RecommendedModel(
        id="z-ai/glm-5.3-flash",
        label="GLM 5.3 Flash",
        note="The map art model, included in a NanoGPT subscription: it wrote a set's words "
        "before there was a designer. Not judged against Opus for this.",
    ),
)


# The style every pixel-art prompt names when the set's words have none.
# Never a game's name (the author, Oct 16 2026: the world should feel like
# the player's own); a set's own words say what suits its world.
DEFAULT_STYLE = "in a style that suits the world's mood"


def world_words(own: Mapping[str, str], setting: str) -> str:
    """The world in a few words, for every prompt: the story's own tile set's
    words, else the setting's first sentence."""
    if own.get("world"):
        return own["world"]
    text = " ".join(setting.split())
    end = text.find(". ")
    first = text[:end] if 0 < end < 160 else text[:160]
    return first.rstrip(". ") or "the story's world"


def style_words(own: Mapping[str, str]) -> str:
    """The pixel art's style in a few words, for every prompt: the tile set's
    own ("gritty and grim, muted rust and ash tones"; a shipped set's manifest
    or the words written for a story's own), else `DEFAULT_STYLE`."""
    return " ".join(str(own.get("style") or "").split()) or DEFAULT_STYLE


def item_jobs(
    areas: Iterable[Area],
    has_sprite: Callable[[str], bool],
    world: str,
    texts: PromptTexts = DEFAULT_TEXTS,
    *,
    style: str = DEFAULT_STYLE,
    failed: Callable[[str], bool] = lambda _k: False,
    skip: Collection[str] = (),
) -> list[PictureJob]:
    """A sprite for each kind of thing in these areas that has none; none
    for a thing in `skip` (by id: the world designer's in a region the
    author's character hasn't been in, `regions.unexplored_ids`)."""
    out: list[PictureJob] = []
    seen: set[str] = set()
    for area in areas:
        for placement in area.placements:
            if placement.id in skip:
                continue
            slugs = item_slugs(placement.name)
            if not slugs or slugs[0] in seen or has_sprite(placement.name):
                continue
            seen.add(slugs[0])
            key = f"item:{slugs[0]}"
            if failed(key):
                continue
            what = placement.name[0].lower() + placement.name[1:]
            out.append(
                PictureJob(
                    key=key,
                    subject="item",
                    subject_id=slugs[0],
                    name=placement.name,
                    prompt=texts.fill("map.tile.sprite", what=what, world=world, style=style),
                )
            )
    return out


def roof_jobs(
    areas: Iterable[Area],
    has_texture: Callable[[str], bool],
    world: str,
    texts: PromptTexts = DEFAULT_TEXTS,
    *,
    style: str = DEFAULT_STYLE,
    failed: Callable[[str], bool] = lambda _k: False,
) -> list[PictureJob]:
    """A roof texture for each material a building in these areas is built
    of that the set has no roof of (`Area.roofs`): drawn on the fly, so a
    stone watchtower is roofed in stone, not the farmhouse's shingles. Until
    it is in, the set's roof tinted towards the material stands in."""
    out: list[PictureJob] = []
    seen: set[str] = set()
    for area in areas:
        for material in area.roofs.values():
            role = roof_role(material)
            key = f"texture:{role}"
            if not material or role in seen or has_texture(role) or failed(key):
                continue
            seen.add(role)
            out.append(
                PictureJob(
                    key=key,
                    subject="texture",
                    subject_id=role,
                    name=f"{material} roof",
                    prompt=texts.fill(
                        "map.tile.texture",
                        # The material, not "a roof": a roof is a building's
                        # (judged blind, 11 of 12 were the material, against 10).
                        what=f"{material} roofing",
                        world=world,
                        view="from directly overhead",
                        style=style,
                    ),
                )
            )
    return out


def ground_jobs(
    areas: Iterable[Area],
    info: TileSetInfo,
    has_texture: Callable[[str], bool],
    world: str,
    texts: PromptTexts = DEFAULT_TEXTS,
    *,
    style: str = DEFAULT_STYLE,
    failed: Callable[[str], bool] = lambda _k: False,
) -> list[PictureJob]:
    """A ground for each look an outdoor area here is in that the story's
    tile set has none of, and a deck's floor for a deck in a set with no
    deck: drawn on the fly in the set's style (the author, Oct 17 2026: the
    set is kept for the whole story, and new pictures are drawn when a
    different type of area is met). Until one is in, the set's own ground
    tinted towards the look stands in."""
    out: list[PictureJob] = []
    seen: set[str] = set()
    for area in areas:
        if area.indoor:
            if not deck_floor_wanted(info, area.look):
                continue
            role, what = DECK_FLOOR_ROLE, DECK_FLOOR_WORDS
        else:
            if not ground_wanted(info, area.look):
                continue
            role, what = ground_role(area.look), GROUND_WORDS[area.look]
        key = f"texture:{role}"
        if role in seen or has_texture(role) or failed(key):
            continue
        seen.add(role)
        out.append(
            PictureJob(
                key=key,
                subject="texture",
                subject_id=role,
                name=f"{area.look} ground" if not area.indoor else "deck floor",
                prompt=texts.fill(
                    "map.tile.texture",
                    what=what,
                    world=world,
                    view="from directly overhead",
                    style=style,
                ),
            )
        )
    return out


def walker_jobs(
    people: Sequence[tuple[str, str | None, str]],
    has_walker: Callable[[str], bool],
    world: str,
    texts: PromptTexts = DEFAULT_TEXTS,
    *,
    style: str = DEFAULT_STYLE,
    failed: Callable[[str], bool] = lambda _k: False,
    portraits: bool = True,
) -> list[PictureJob]:
    """A walking sprite for each person (name, portrait file or None, how
    their card describes them) who has none. Each records what it is drawn
    from (`from_picture`: the portrait's file, "" for the words), so one
    drawn before a portrait came is known to be drawn again. `portraits`
    False: the model takes no picture, so every one is drawn from words."""
    out: list[PictureJob] = []
    for name, portrait, looks in people:
        if not portraits:
            portrait = None
        key = f"walker:{name}"
        if has_walker(name) or failed(key):
            continue
        if portrait:
            prompt = texts.fill("map.tile.portrait", style=style)
        else:
            what = f"{name}, {looks}" if looks else name
            prompt = texts.fill("map.tile.person", what=what, world=world, style=style)
        out.append(
            PictureJob(
                key=key,
                subject="walker",
                subject_id=name,
                name=name,
                prompt=prompt,
                reference=portrait or None,
                from_picture=portrait or "",
            )
        )
    return out


def tileset_messages(
    setting: str,
    texts: PromptTexts = DEFAULT_TEXTS,
    *,
    description: str = "",
    pictures: Sequence[tuple[str, str]] = (),
) -> list[PromptMessage]:
    """The designer's request: the world, and what the author asked the set
    to look like when they said ("gritty rain-slick cyberpunk alleys").
    `pictures`: (a data URL, what the author said it shows) for each picture
    of the world they added; each goes after its words, headed by
    `map.tileset.pictures`. Without any, the request is as it always was."""
    asked = " ".join(description.split())
    text = setting
    if asked:
        text = f"The world: {setting}\n\nThe look the author wants for the set: {asked}"
    parts = [ContentPart(text=text)]
    if pictures:
        parts.append(ContentPart(text=texts["map.tileset.pictures"]))
        for number, (url, caption) in enumerate(pictures, 1):
            said = " ".join(caption.split()) or "(the author said nothing of it)"
            parts.append(ContentPart(text=f"Picture {number}: {said}", image=url))
    return [
        PromptMessage(role="system", parts=(ContentPart(text=texts["map.tileset.system"]),)),
        PromptMessage(role="user", parts=tuple(parts)),
    ]


def pick_messages(
    setting: str, sets: Mapping[str, TileSetInfo], texts: PromptTexts = DEFAULT_TEXTS
) -> list[PromptMessage]:
    """The request for the shipped set closest to a world: the world and
    where it begins, then each set by name with the world it was made for,
    its style, its ground and the words it was chosen by."""
    lines = []
    for name, info in sorted(sets.items()):
        words = info.words
        about = [
            f"{info.title or name}.",
            f"Made for: {words['world']}." if words.get("world") else "",
            f"Style: {words['style']}." if words.get("style") else "",
            f"Ground: {words['out-ground']}." if words.get("out-ground") else "",
            "Its outside is space (a station or ship)." if info.deck else "",
            f"Fits: {', '.join(info.keywords)}." if info.keywords else "",
        ]
        lines.append(f"- {name}: " + " ".join(a for a in about if a))
    text = f"THE WORLD:\n{setting.strip()}\n\nTHE TILE SETS:\n" + "\n".join(lines)
    return [
        PromptMessage(role="system", parts=(ContentPart(text=texts["map.tileset.pick"]),)),
        PromptMessage(role="user", parts=(ContentPart(text=text),)),
    ]


def parse_pick(text: str, names: Iterable[str]) -> tuple[str, str]:
    """(the set chosen, why) from the reply; ValueError when it names no set
    there is. A name matched ignoring case and spacing."""
    reply = extract_json(text, dict, "choice of tile set")
    said = " ".join(str(reply.get("set") or "").lower().split())
    found = next((n for n in names if " ".join(n.lower().split()) == said), None)
    if found is None:
        raise ValueError(f"no tile set is called {reply.get('set')!r}")
    why = " ".join(str(reply.get("why") or "").split())[:WORDS_LIMIT]
    return found, why


def with_style(prompt: str, style: str) -> str:
    """A prompt naming the set's style, as every pixel-art prompt must (the
    set reads as one): the designer's as written when it names it, else with
    the style added at the end."""
    prompt = " ".join(prompt.split())
    if not prompt or not style or style.lower() in prompt.lower():
        return prompt
    return f"{prompt.rstrip('.')}. Style: {style}."


def parse_tile_words(text: str) -> dict[str, str]:
    """A tile set's words from the designer's reply, by role, every role
    there ("decor" and "trees" become decor0-2 and out-tree0-1), and under
    "prompt:<role>" the prompt it tailored to each picture when it gave one
    (`"prompts"`), each naming the style (`with_style`). ValueError when a
    core role is missing."""
    data = extract_json(text, dict, "tile set")
    words: dict[str, str] = {}

    def said(value: object, limit: int = WORDS_LIMIT) -> str:
        return " ".join(str(value or "").split())[:limit]

    words["world"] = said(data.get("world"))
    words["style"] = said(data.get("style"))
    for role in ("floor", "cap", "face", "rug", *[r for r in TEXTURES if r.startswith("out-")]):
        words[role] = said(data.get(role))
    for i, item in enumerate((data.get("decor") or [])[:3]):
        words[f"decor{i}"] = said(item)
    for i, item in enumerate((data.get("trees") or [])[:2]):
        words[f"out-tree{i}"] = said(item)
    for role in ("out-bush", "out-rock", "out-rubble", "out-fence", "out-door"):
        words[role] = said(data.get(role))
    # Only what every map needs: a world with no roofs, trees or fences
    # (a steppe, a city under the sea) still gets its set; the rest draw
    # when given, else the program's plain stand-ins.
    missing = [r for r in CORE_ROLES if not words.get(r)]
    if missing or not words["world"]:
        raise ValueError("the tile set is missing " + ", ".join(missing or ["the world"]))
    light = data.get("light")
    if isinstance(light, list) and len(light) == 3 and all(isinstance(c, int) for c in light):
        words["light"] = ",".join(str(max(0, min(255, c))) for c in light)
    words["lava"] = "yes" if data.get("lava") is True else ""
    prompts = data.get("prompts")
    if isinstance(prompts, dict):
        style = style_words(words)
        for role in (*TEXTURES, *SPRITES):
            prompt = said(prompts.get(role), PROMPT_LIMIT)
            if prompt and words.get(role):
                words[f"{PROMPT_KEY}{role}"] = with_style(prompt, style)
    return words


def set_prompt(words: Mapping[str, str], role: str) -> str | None:
    """The prompt designed for a role's picture, or None for the template."""
    return words.get(f"{PROMPT_KEY}{role}") or None


def tile_jobs(
    words: Mapping[str, str], texts: PromptTexts = DEFAULT_TEXTS
) -> list[tuple[str, str, str]]:
    """(role, prompt, size) for each picture of a story's own tile set: the
    prompt designed for it (`set_prompt`, sent exactly as the author approved
    it) when there is one, else the role's template filled in."""
    world = words.get("world", "")
    style = style_words(words)
    out: list[tuple[str, str, str]] = []
    for role in TEXTURES:
        if not words.get(role):
            continue  # a world without it: the program's stand-in draws
        view = (
            "straight from the front" if role in ("face", "out-wall") else "from directly overhead"
        )
        prompt = set_prompt(words, role) or texts.fill(
            "map.tile.texture", what=words[role], world=world, view=view, style=style
        )
        out.append((role, prompt, TEXTURE_SIZE))
    if words.get("rug"):
        prompt = set_prompt(words, "rug") or texts.fill(
            "map.tile.rug", what=words["rug"], world=world, style=style
        )
        out.append(("rug", prompt, SPRITE_SIZE))
    for i in range(3):
        role = f"decor{i}"
        if words.get(role):
            prompt = set_prompt(words, role) or texts.fill(
                "map.tile.sprite", what=words[role], world=world, style=style
            )
            out.append((role, prompt, SPRITE_SIZE))
    for role in OUTDOOR_SPRITES:
        if words.get(role):
            prompt = set_prompt(words, role) or texts.fill(
                "map.tile.nature", what=words[role], world=world, style=style
            )
            out.append((role, prompt, SPRITE_SIZE))
    return out


def tile_set_price(
    jobs: Sequence[tuple[str, str, str]], price: Callable[[str], float | None]
) -> float | None:
    """What a set's pictures cost together at the model's listed prices,
    each at its size; None when any size has no listed price (never a guess)."""
    total = 0.0
    for _role, _prompt, size in jobs:
        each = price(size)
        if each is None:
            return None
        total += each
    return total
