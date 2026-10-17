"""The levels of a place's plan: pure (the author, Oct 2026: "different
decks for space stations or levels for buildings, dungeons, castles").

A plan (`PlaceLayout`) is the place's ground floor or main deck; its other
floors and decks are `PlaceLayout.levels`, each drawn on the same grid by a
job of its own once the plan's words called for them (engine/world_art.py
`plan_art`). Each level is an area of its own (engine/area.py `build_level`)
whose id is the place's with the level's index after `LEVEL_SEP`
("house#1", "station#-2"); the plan's own level keeps the place id, so
nothing about a plan without levels changes.

Stairs are the doors between levels: a stairs tile (`STAIRS`) on one level
leads to the level nearest in index that has stairs in the same tile. The
rules are held here whatever the model drew: a level with no stairs that
reach the level nearer the ground gets them, over the other level's stairs
or on a floor tile both have (`linked_levels`, said in its repairs), and a
plan that lists levels but drew no stairs gets one tile of them before any
level is asked for (`stairs_spot`). Not measured live.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sealedlore.models.world import PlaceLayout, WorldPlace

LEVEL_SEP = "#"
STAIRS = "&"
# How many further levels a plan may list, and the indexes allowed (0 is the
# plan itself; negative is below it).
MAX_LEVELS = 6
LEVEL_RANGE = (-3, 6)
# What a level's name defaults to when the model gave none.
_DEFAULT_NAMES = {1: "Upper floor", -1: "Cellar"}

# The words that call for levels: the model is asked to list them only
# when the place's words (its name, what the story said of it, its state)
# say so, and the plan's listing counts only then.
_LEVEL_WORDS = re.compile(
    r"\b(stor(?:e)?ys?|floors?|upstairs|downstairs|cellars?|basements?|attics?|lofts?|"
    r"decks?|levels?|tiers?|towers?|keeps?|dungeons?|stair(?:s|case|cases|way|ways|well|wells)?|"
    r"ladders?|lifts?|elevators?|mines?|crypts?|catacombs?|undercroft|belfry|turrets?|"
    r"stations?|starships?|spaceships?|multi-?level|two-level|multi-?stor(?:e)?y)\b",
    re.I,
)
# Places that are enclosed whatever their kind says (a station is a site, a
# dungeon an area): their plan is inside, so it may have levels.
_ENCLOSED_WORDS = re.compile(
    r"\b(decks?|stations?|starships?|spaceships?|dungeons?|crypts?|catacombs?|mines?|"
    r"undercroft|keeps?|towers?)\b",
    re.I,
)
_INSIDE_KINDS = frozenset({"building", "room", "vehicle"})


# --- Area ids ---------------------------------------------------------------------


def level_area_id(place_id: str, index: int) -> str:
    """The area id of a place's level: the place id itself for the plan's
    own level (0), else "<place_id>#<index>"."""
    return place_id if not index else f"{place_id}{LEVEL_SEP}{index}"


def parse_level(area_id: str | None) -> tuple[str, int] | None:
    """(place id, level index) an area id names, or None for a place's own
    plan, a window of open country, or anything else."""
    if not area_id or LEVEL_SEP not in str(area_id):
        return None
    place_id, _, tail = str(area_id).rpartition(LEVEL_SEP)
    if not place_id or not re.fullmatch(r"-?\d+", tail):
        return None
    index = int(tail)
    return (place_id, index) if index else None


def is_level(area_id: str | None) -> bool:
    return parse_level(area_id) is not None


def place_of_area(area_id: str | None) -> str | None:
    """The place an area id stands in, for a plan or one of its levels: a
    window's id comes back as it is (engine/area.py resolves those)."""
    parsed = parse_level(area_id)
    return parsed[0] if parsed is not None else area_id


# --- Whether a place has levels, and which ---------------------------------------


def levels_called_for(place: WorldPlace) -> bool:
    """Whether the place's words call for more than one level, so the layout
    model is asked to list them: a building, room or vehicle whose words
    name floors, a cellar, decks or the like, or any place whose words make
    it an enclosed one with levels (a station, a dungeon, a keep)."""
    words = f"{place.name} {place.what} {place.state}"
    if not _LEVEL_WORDS.search(words):
        return False
    return place.kind in _INSIDE_KINDS or _ENCLOSED_WORDS.search(words) is not None


def listed_levels(spec: dict[str, Any]) -> list[tuple[int, str]]:
    """The further levels a plan's reply lists, (index, name) each, held to
    the rules: whole indexes within LEVEL_RANGE, never 0, one per index,
    MAX_LEVELS at most, nearest the plan first."""
    raw = _raw_levels(spec)
    if not raw:
        return []
    found: dict[int, str] = {}
    for entry in raw:
        if isinstance(entry, dict):
            index, name = entry.get("index"), entry.get("name")
        elif isinstance(entry, (int, float)) and not isinstance(entry, bool):
            index, name = entry, None
        else:
            continue
        try:
            index = int(index)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            continue
        if index == 0 or not (LEVEL_RANGE[0] <= index <= LEVEL_RANGE[1]) or index in found:
            continue
        found[index] = " ".join(str(name or "").split())[:40] or default_level_name(index)
    ordered = sorted(found.items(), key=lambda item: (abs(item[0]), item[0]))
    return ordered[:MAX_LEVELS]


def _raw_levels(spec: dict[str, Any]) -> list:
    """The reply's `levels` list: on the object, as asked, else wherever a
    feature carries one. Live (Oct 17 2026), GLM 5.3 Flash put a watchtower's
    three levels on its stairs feature, and the plan was read as having none:
    its stairs led nowhere."""
    if not isinstance(spec, dict):
        return []
    raw = spec.get("levels")
    if isinstance(raw, list):
        return raw
    nested: list = []
    features = spec.get("features")
    for feature in features if isinstance(features, list) else []:
        if isinstance(feature, dict) and isinstance(feature.get("levels"), list):
            nested.extend(feature["levels"])
    return nested


def default_level_name(index: int) -> str:
    if index in _DEFAULT_NAMES:
        return _DEFAULT_NAMES[index]
    return f"Level {index}" if index > 0 else f"Lower level {-index}"


def main_level_name(deck: bool) -> str:
    """What the plan's own level is called beside its others."""
    return "main deck" if deck else "ground floor"


def level_name(layout: PlaceLayout | None, index: int, *, deck: bool = False) -> str:
    """A level's name as the plan has it, for the title and the storyteller."""
    if not index:
        return main_level_name(deck)
    found = layout.level(index) if layout is not None else None
    if found is not None and found.name:
        return found.name
    if layout is not None:
        for listed, name in listed_levels(layout.spec):
            if listed == index:
                return name
    return default_level_name(index)


# --- Stairs ---------------------------------------------------------------------------


def stairs_tiles(rows: Sequence[str]) -> set[tuple[int, int]]:
    return {(x, y) for y, row in enumerate(rows) for x, ch in enumerate(row) if ch == STAIRS}


def fitted_rows(rows: Sequence[str], width: int, height: int, fill: str = " ") -> list[str]:
    """`rows` on a grid `width` by `height`: cut or padded with `fill`
    (a level drawn on the wrong grid is held to the plan's)."""
    out = [str(r)[:width].ljust(width, fill) for r in list(rows)[:height]]
    while len(out) < height:
        out.append(fill * width)
    return out


def stairs_spot(rows: Sequence[str]) -> tuple[int, int] | None:
    """Where the program puts stairs in a plan that lists levels but drew
    none: the floor tile nearest the grid's middle whose neighbours are
    floor or wall and that stands next to no door, so it blocks no doorway.
    None when the plan has no such floor."""
    height = len(rows)
    width = max((len(r) for r in rows), default=0)
    if not width or not height:
        return None

    def at(x: int, y: int) -> str:
        return rows[y][x] if 0 <= y < height and 0 <= x < len(rows[y]) else " "

    cx, cy = (width - 1) / 2, (height - 1) / 2
    best: tuple[float, int, int] | None = None
    for y in range(height):
        for x in range(width):
            if at(x, y) != "_":
                continue
            around = [at(x + dx, y + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))]
            if any(ch not in "_#W" for ch in around):
                continue
            score = (x - cx) ** 2 + (y - cy) ** 2
            if best is None or score < best[0]:
                best = (score, x, y)
    return (best[1], best[2]) if best is not None else None


def with_stairs(rows: Sequence[str], spot: tuple[int, int]) -> list[str]:
    x, y = spot
    out = list(rows)
    out[y] = out[y][:x] + STAIRS + out[y][x + 1 :]
    return out


@dataclass
class LevelLinks:
    """Every level's rows with the stairs the rules put in, and where each
    stairs tile leads: `links[n][(x, y)]` is the index of the level the
    stairs on level n at (x, y) go to. A stairs tile with no counterpart is
    in no link: just a tile one can stand on."""

    rows: dict[int, list[str]] = field(default_factory=dict)
    links: dict[int, dict[tuple[int, int], int]] = field(default_factory=dict)
    repairs: list[str] = field(default_factory=list)


def _link(rows: dict[int, list[str]]) -> dict[int, dict[tuple[int, int], int]]:
    """Each stairs tile to the level nearest in index with stairs in the
    same tile (a tie goes to the level nearer the plan's own)."""
    tiles = {n: stairs_tiles(r) for n, r in rows.items()}
    links: dict[int, dict[tuple[int, int], int]] = {n: {} for n in rows}
    for n, mine in tiles.items():
        for tile in mine:
            others = [m for m, theirs in tiles.items() if m != n and tile in theirs]
            if others:
                links[n][tile] = min(others, key=lambda m: (abs(m - n), abs(m), m))
    return links


def _order(indexes: Sequence[int]) -> list[int]:
    return sorted((n for n in indexes if n), key=lambda n: (abs(n), n))


def linked_levels(layout: PlaceLayout) -> LevelLinks:
    """The plan and its drawn levels, each held to the plan's grid, with
    their stairs linked, and the rule kept that every drawn level can be
    reached from the level nearer the plan's own (1 from 0, 2 from 1, -1
    from 0): a level whose stairs reach it nowhere gets stairs over that
    level's own unlinked stairs, else that level gets them over this one's,
    else both get a tile of stairs on floor they share, nearest the middle.
    A level whose nearer neighbour isn't drawn yet waits for it."""
    height = len(layout.rows)
    width = max((len(r) for r in layout.rows), default=0)
    rows: dict[int, list[str]] = {0: list(layout.rows)}
    for level in layout.levels:
        if level.index:
            rows[level.index] = fitted_rows(level.rows, width, height)
    out = LevelLinks(rows=rows)
    if len(rows) < 2:
        out.links = _link(rows)
        return out
    for n in _order(list(rows)):
        toward = n - (1 if n > 0 else -1)
        if toward not in rows:
            continue
        links = _link(rows)
        if toward in links[n].values():
            continue
        name = f"level {n}"
        spot = _spot_over(rows, links, n, toward)
        if spot is not None:
            rows[n] = with_stairs(rows[n], spot)
            out.repairs.append(
                f"put stairs on {name} at {spot[0]},{spot[1]}, over level {toward}'s"
            )
            continue
        spot = _spot_over(rows, links, toward, n)
        if spot is not None:
            rows[toward] = with_stairs(rows[toward], spot)
            out.repairs.append(
                f"put stairs on level {toward} at {spot[0]},{spot[1]}, over {name}'s"
            )
            continue
        spot = _shared_floor(rows[n], rows[toward])
        if spot is None:
            out.repairs.append(f"no floor shared by {name} and level {toward}: no stairs between")
            continue
        rows[n] = with_stairs(rows[n], spot)
        rows[toward] = with_stairs(rows[toward], spot)
        out.repairs.append(f"added stairs between {name} and level {toward} at {spot[0]},{spot[1]}")
    out.links = _link(rows)
    return out


def _spot_over(
    rows: dict[int, list[str]],
    links: dict[int, dict[tuple[int, int], int]],
    n: int,
    other: int,
) -> tuple[int, int] | None:
    """A stairs tile of level `other` that leads nowhere yet, over floor on
    level `n`: where level n's stairs go, nearest the middle."""
    mine = rows[n]
    height, width = len(mine), max((len(r) for r in mine), default=0)
    cx, cy = (width - 1) / 2, (height - 1) / 2
    free = [
        t
        for t in stairs_tiles(rows[other])
        if t not in links[other]
        and 0 <= t[1] < height
        and 0 <= t[0] < len(mine[t[1]])
        and mine[t[1]][t[0]] == "_"
    ]
    if not free:
        return None
    return min(free, key=lambda t: ((t[0] - cx) ** 2 + (t[1] - cy) ** 2, t[1], t[0]))


def _shared_floor(a: Sequence[str], b: Sequence[str]) -> tuple[int, int] | None:
    """A floor tile both levels have, beside no door on either, nearest the
    middle; None when there is none."""
    height = min(len(a), len(b))
    width = min(max((len(r) for r in a), default=0), max((len(r) for r in b), default=0))
    if not height or not width:
        return None

    def at(rows: Sequence[str], x: int, y: int) -> str:
        return rows[y][x] if 0 <= y < len(rows) and 0 <= x < len(rows[y]) else " "

    cx, cy = (width - 1) / 2, (height - 1) / 2
    best: tuple[float, int, int] | None = None
    for y in range(height):
        for x in range(width):
            if at(a, x, y) != "_" or at(b, x, y) != "_":
                continue
            near = [(x + dx, y + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))]
            if any(at(a, *t) == "+" or at(b, *t) == "+" for t in near):
                continue
            score = (x - cx) ** 2 + (y - cy) ** 2
            if best is None or score < best[0]:
                best = (score, x, y)
    return (best[1], best[2]) if best is not None else None


def stairs_words(tiles: Sequence[tuple[int, int]]) -> str:
    """'x 12, y 7; x 13, y 7', for the level's request."""
    return "; ".join(f"x {x}, y {y}" for x, y in sorted(tiles, key=lambda t: (t[1], t[0])))
