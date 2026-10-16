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
- a story's own tile set, when the author asks for one, from words a model
  writes from the world (`map.tileset.system`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence

from sealedlore.engine.area import Area
from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, PromptTexts
from sealedlore.engine.tile_sets import OUTDOOR_SPRITES, TEXTURES, item_slugs
from sealedlore.engine.world_pictures import PictureJob
from sealedlore.messages import ContentPart, PromptMessage

# The roles a story's own tile set must have words for; the rest are drawn
# when the world has them.
CORE_ROLES = ("floor", "cap", "face", "rug", "out-ground", "out-road", "out-wall")
# Sprites are drawn square at this size, textures at TEXTURE_SIZE.
SPRITE_SIZE = "768x768"
TEXTURE_SIZE = "1024x1024"
# A run draws at most this many pictures, nearest the story first.
RUN_LIMIT = 12


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
) -> list[PictureJob]:
    """A sprite for each kind of thing in these areas that has none."""
    out: list[PictureJob] = []
    seen: set[str] = set()
    for area in areas:
        for placement in area.placements:
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


def walker_jobs(
    people: Sequence[tuple[str, str | None, str]],
    has_walker: Callable[[str], bool],
    world: str,
    texts: PromptTexts = DEFAULT_TEXTS,
    *,
    style: str = DEFAULT_STYLE,
    failed: Callable[[str], bool] = lambda _k: False,
) -> list[PictureJob]:
    """A walking sprite for each person (name, portrait file or None, how
    their card describes them) who has none."""
    out: list[PictureJob] = []
    for name, portrait, looks in people:
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
            )
        )
    return out


def tileset_messages(setting: str, texts: PromptTexts = DEFAULT_TEXTS) -> list[PromptMessage]:
    return [
        PromptMessage(role="system", parts=(ContentPart(text=texts["map.tileset.system"]),)),
        PromptMessage(role="user", parts=(ContentPart(text=setting),)),
    ]


def parse_tile_words(text: str) -> dict[str, str]:
    """A tile set's words from the model's reply, by role, every role there
    ("decor" and "trees" become decor0-2 and out-tree0-1). ValueError when
    one is missing."""
    data = extract_json(text, dict, "tile set")
    words: dict[str, str] = {}

    def said(value: object) -> str:
        return " ".join(str(value or "").split())[:200]

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
    return words


def tile_jobs(
    words: Mapping[str, str], texts: PromptTexts = DEFAULT_TEXTS
) -> list[tuple[str, str, str]]:
    """(role, prompt, size) for each picture of a story's own tile set."""
    world = words.get("world", "")
    style = style_words(words)
    out: list[tuple[str, str, str]] = []
    for role in TEXTURES:
        if not words.get(role):
            continue  # a world without it: the program's stand-in draws
        view = (
            "straight from the front" if role in ("face", "out-wall") else "from directly overhead"
        )
        prompt = texts.fill(
            "map.tile.texture", what=words[role], world=world, view=view, style=style
        )
        out.append((role, prompt, TEXTURE_SIZE))
    if words.get("rug"):
        prompt = texts.fill("map.tile.rug", what=words["rug"], world=world, style=style)
        out.append(("rug", prompt, SPRITE_SIZE))
    for i in range(3):
        role = f"decor{i}"
        if words.get(role):
            prompt = texts.fill("map.tile.sprite", what=words[role], world=world, style=style)
            out.append((role, prompt, SPRITE_SIZE))
    for role in OUTDOOR_SPRITES:
        if words.get(role):
            prompt = texts.fill("map.tile.nature", what=words[role], world=world, style=style)
            out.append((role, prompt, SPRITE_SIZE))
    return out
