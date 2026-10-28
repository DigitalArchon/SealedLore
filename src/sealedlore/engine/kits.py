"""A world kit put together (models/kit.py; the author, Oct 26 2026: the
DDA method for the insides of places, item tables and small stamped
pieces). Pure: the same kit, place and seed always make the same thing.

- `stock`: what a container holds, rolled from its item table.
- `kit_layout`: a building's inside from one of its kind's templates, by
  weight, its chunks stamped in, turned and mirrored so its main door is on
  the side the map's door is, as a `PlaceLayout` the area builder reads like
  any plan (its furniture in `spec["pieces"]`, the floor's loot in
  `spec["loot"]`).
- `yard_pieces`: things stamped on open ground round the buildings of an
  outdoor area.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import zlib
from collections import deque
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from sealedlore.engine.world_art import TILES
from sealedlore.models.area import Found
from sealedlore.models.kit import (
    BuildingKind,
    ContainerRule,
    ItemEntry,
    Kit,
    LegendEntry,
    Nest,
    Template,
    YardPiece,
)
from sealedlore.models.world import LevelLayout, PlaceLayout

# What one container holds, at most, and how deep tables may nest.
MAX_CONTENTS = 8
MAX_DEPTH = 6
# A block of one legend character wider or longer than this is cut in pieces.
MAX_PIECE = 6
# A variant fits a place whose size is within this factor of its own.
SIZE_FIT = 1.6
SIDES = ("north", "east", "south", "west")
# Out of doors: the ground a yard piece may stand on, and how far from its
# building's wall, in tiles.
YARD_GROUND = frozenset(".,s")
YARD_REACH = 6
WALKABLE_OUT = frozenset("._=+,:s&b")


def rng_for(*parts: object) -> random.Random:
    """A random stream fixed by what it is for: the story's seed, the place,
    the thing."""
    return random.Random(zlib.crc32(":".join(str(p) for p in parts).encode()))


def _words(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", text.lower()))


def has_words(text: str, word: str) -> bool:
    """`word` (or its plural) as whole words of `text`."""
    word = _words(word)
    return bool(word) and re.search(rf"\b{re.escape(word)}(e?s)?\b", _words(text)) is not None


_has = has_words


# --- What containers hold (CDDA's item groups) -----------------------------------


def _count(count: int | tuple[int, int], rng: random.Random) -> int:
    if isinstance(count, (tuple, list)):
        lo, hi = int(count[0]), int(count[1])
        return rng.randint(min(lo, hi), max(lo, hi))
    return int(count)


def roll(kit: Kit, table_id: str, rng: random.Random, _depth: int = 0) -> list[Found]:
    """A table rolled: in a collection each entry comes on its own chance,
    in a distribution exactly one comes, by weight (an entry with neither an
    item nor a table is nothing). A table names other tables as entries."""
    table = kit.tables.get(table_id)
    if table is None or _depth > MAX_DEPTH:
        return []
    out: list[Found] = []
    if table.type == "distribution":
        entries = [e for e in table.entries if e.chance > 0]
        total = sum(e.chance for e in entries)
        if not total:
            return []
        pick = rng.uniform(0, total)
        for entry in entries:
            pick -= entry.chance
            if pick <= 0:
                out += _entry(kit, entry, rng, _depth)
                break
        return out
    for entry in table.entries:
        if rng.uniform(0, 100) < entry.chance:
            out += _entry(kit, entry, rng, _depth)
    return out


def _entry(kit: Kit, entry: ItemEntry, rng: random.Random, depth: int) -> list[Found]:
    """An entry's items: its item, `count` of it; or its table rolled
    `count` times."""
    count = _count(entry.count, rng)
    if count <= 0:
        return []
    if entry.table:
        return [
            f
            for _ in range(min(count, MAX_CONTENTS))
            for f in roll(kit, entry.table, rng, depth + 1)
        ]
    if not entry.item:
        return []
    return [Found(name=entry.item, count=count, kind=entry.kind, note=entry.note)]


def merged(found: Iterable[Found]) -> list[Found]:
    """Finds of one name as one, their counts summed, in the order first met."""
    out: dict[str, Found] = {}
    for f in found:
        k = f.name.casefold()
        if k in out:
            out[k] = out[k].model_copy(update={"count": out[k].count + f.count})
        else:
            out[k] = f
    return list(out.values())


# An abandoned place was picked over long ago (the author, Oct 28 2026:
# "there's a big difference between a lived in home and an abandoned one in
# reality"): of what a table rolls, what is worth having mostly went, the
# rest half stays, and counts are cut in half.
PICKED_VALUABLE = 15
PICKED_PLAIN = 55
VALUABLE_KINDS = frozenset({"food", "drink", "medicine", "weapon", "ammunition", "fuel"})
# What an abandoned place is, for the keeper, the Look and the player.
ABANDONED_WORDS = (
    "This place is abandoned: what is left is worn, aged and weathered, and anything worth "
    "having was taken long ago."
)


def picked_over(
    found: Sequence[Found], rng: random.Random, kind: Callable[[Found], str]
) -> list[Found]:
    """What is left of `found` in a place nobody has lived in for years:
    each thing kept by its kind's chance, its count halved."""
    out: list[Found] = []
    for f in found:
        keep = PICKED_VALUABLE if kind(f) in VALUABLE_KINDS else PICKED_PLAIN
        if rng.uniform(0, 100) >= keep:
            continue
        out.append(f.model_copy(update={"count": max(1, (f.count + 1) // 2)}))
    return out


def stock(kit: Kit, table: str, chance: int, rng: random.Random) -> list[Found]:
    """What a container stocked from `table` holds: nothing at all unless its
    `chance` comes up, else the table rolled, at most MAX_CONTENTS kinds."""
    if not table or (chance < 100 and rng.uniform(0, 100) >= chance):
        return []
    return merged(roll(kit, table, rng))[:MAX_CONTENTS]


def container_rule(kit: Kit, name: str) -> ContainerRule | None:
    """The first rule whose words name this thing (a hand-drawn plan's
    "battered wooden dresser" is a dresser), or None: it holds nothing the
    tables decide."""
    for rule in kit.containers:
        if any(_has(name, word) for word in rule.match):
            return rule
    return None


# --- Which template --------------------------------------------------------------


def kind_for(kit: Kit, name: str, what: str = "") -> str | None:
    """The kind of building a place is: the first kind one of whose words
    its name holds, else its words."""
    for text in (name, what):
        for kind, building in kit.buildings.items():
            if any(_has(text, word) for word in building.match):
                return kind
    return None


def _width_m(template: Template) -> float:
    return max(len(template.rows[0]) if template.rows else 0, len(template.rows)) * template.tile_m


def choose_template(building: BuildingKind, size_m: float, rng: random.Random) -> Template | None:
    """One of the variants by weight, among those whose size fits the
    place's (within SIZE_FIT), else the nearest in size."""
    variants = [t for t in building.variants if t.weight > 0 and t.rows]
    if not variants:
        return None
    fits = [t for t in variants if 1 / SIZE_FIT <= size_m / max(_width_m(t), 0.1) <= SIZE_FIT]
    if not fits:
        return min(variants, key=lambda t: (abs(_width_m(t) - size_m), t.id))
    total = sum(t.weight for t in fits)
    pick = rng.uniform(0, total)
    for t in fits:
        pick -= t.weight
        if pick <= 0:
            return t
    return fits[-1]


# --- Turning and mirroring --------------------------------------------------------


def turned_side(side: str, rot: int, mirror: bool) -> str:
    """Where a side of the template ends up: mirrored east to west first,
    then turned a quarter clockwise `rot` times."""
    i = SIDES.index(side)
    if mirror and side in ("east", "west"):
        i = (i + 2) % 4
    return SIDES[(i + rot) % 4]


def turned_point(x: int, y: int, w: int, h: int, rot: int, mirror: bool) -> tuple[int, int]:
    """A tile of a w × h grid after the same turn."""
    if mirror:
        x = w - 1 - x
    for _ in range(rot % 4):
        x, y, w, h = h - 1 - y, x, h, w
    return x, y


def _turned_grid(grid: list[list[Cell]], rot: int, mirror: bool) -> list[list[Cell]]:
    if mirror:
        grid = [list(reversed(row)) for row in grid]
    for _ in range(rot % 4):
        grid = [list(row) for row in zip(*reversed(grid), strict=True)]
    return grid


def orientation(template: Template, want: str | None, rng: random.Random) -> tuple[int, bool]:
    """The turn that puts the template's main door on `want` (any of its
    doors, if no turn puts the main one there), keeping its shape (no
    quarter turn) when it can; mirrored or not by chance. No door wanted:
    as drawn, or mirrored."""
    turns = [(r, m) for r in range(4) for m in (False, True)]
    if want not in SIDES:
        return (0, rng.random() < 0.5)
    for doors in (template.doors[:1], template.doors):
        fit = [(r, m) for r, m in turns if any(turned_side(d, r, m) == want for d in doors)]
        if fit:
            keep = [(r, m) for r, m in fit if r % 2 == 0] or fit
            return keep[rng.randrange(len(keep))]
    return (0, False)


# --- Putting a template together --------------------------------------------------


@dataclass
class Cell:
    tile: str
    # The legend entry this tile is part of, under a key unique to the
    # template or chunk it came from (so two chunks' "b" stay two things).
    key: str = ""


@dataclass
class _Composed:
    grid: list[list[Cell]]
    legend: dict[str, LegendEntry] = field(default_factory=dict)
    labels: dict[str, tuple[int, int]] = field(default_factory=dict)


def _stamp(
    kit: Kit,
    into: _Composed,
    rows: Sequence[str],
    legend: Mapping[str, LegendEntry],
    labels: Mapping[str, tuple[int, int]],
    nested: Sequence[Nest],
    at: tuple[int, int],
    tag: str,
    rng: random.Random,
    *,
    keep_blank: bool,
    depth: int = 0,
    shared: Mapping[str, LegendEntry] | None = None,
) -> None:
    """Rows laid over `into` at `at`: legend characters become their
    entries (keyed by `tag`), a space keeps what is under it when stamping a
    chunk, and the chunks named in `nested` go on after, by weight, reading
    the template's shared legend (`shared`) under their own."""
    shared = legend if shared is None else shared
    ox, oy = at
    height = len(into.grid)
    width = len(into.grid[0]) if into.grid else 0
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            gx, gy = ox + x, oy + y
            if not (0 <= gx < width and 0 <= gy < height):
                continue
            if ch == " " and keep_blank:
                continue
            if ch in legend:
                key = f"{tag}:{ch}"
                into.legend[key] = legend[ch]
                into.grid[gy][gx] = Cell(legend[ch].floor, key)
            else:
                into.grid[gy][gx] = Cell(ch if ch in TILES else "_")
    for name, (lx, ly) in labels.items():
        into.labels[name] = (ox + lx, oy + ly)
    if depth >= MAX_DEPTH:
        return
    for n, nest in enumerate(nested):
        total = sum(max(0, w) for _c, w in nest.chunks)
        if not total:
            continue
        pick = rng.uniform(0, total)
        chosen = ""
        for name, weight in nest.chunks:
            pick -= max(0, weight)
            if pick <= 0:
                chosen = name
                break
        chunk = kit.chunks.get(chosen)
        if chunk is None:
            continue  # "" or an unknown chunk: nothing stamped
        _stamp(
            kit,
            into,
            chunk.rows,
            {**shared, **chunk.legend},
            chunk.labels,
            chunk.nested,
            (ox + nest.x, oy + nest.y),
            f"{tag}.{n}.{chosen}",
            rng,
            keep_blank=True,
            depth=depth + 1,
            shared=shared,
        )


def _legend(kit: Kit, template: Template, own: Mapping[str, LegendEntry]) -> dict[str, LegendEntry]:
    out: dict[str, LegendEntry] = {}
    for name in template.palettes:
        out.update(kit.palettes.get(name, {}))
    out.update(own)
    return out


@dataclass
class Extracted:
    rows: list[str]
    pieces: list[dict]
    loot: list[dict]


def extract(grid: list[list[Cell]], legend: Mapping[str, LegendEntry]) -> Extracted:
    """The tiles, the furniture (each block of one entry's tiles one piece,
    cut in rows where it isn't a rectangle or is longer than MAX_PIECE) and
    the loot spots."""
    height = len(grid)
    width = len(grid[0]) if grid else 0
    rows = ["".join(c.tile for c in row) for row in grid]
    pieces: list[dict] = []
    loot: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for y in range(height):
        for x in range(width):
            cell = grid[y][x]
            if not cell.key or (x, y) in seen:
                continue
            entry = legend[cell.key]
            if entry.loot and not entry.thing:
                seen.add((x, y))
                loot.append({"x": x, "y": y, "table": entry.loot, "chance": entry.chance})
                continue
            block = _component(grid, x, y, cell.key)
            seen |= block
            for x0, y0, w, h in _rectangles(block):
                pieces.append(
                    {
                        "name": entry.thing,
                        "x": x0,
                        "y": y0,
                        "w": w,
                        "h": h,
                        "stock": entry.stock,
                        "chance": entry.chance,
                    }
                )
    return Extracted(rows, pieces, loot)


def _component(grid: list[list[Cell]], x: int, y: int, key: str) -> set[tuple[int, int]]:
    out = {(x, y)}
    todo = deque([(x, y)])
    while todo:
        cx, cy = todo.popleft()
        for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
            if (nx, ny) in out or not (0 <= ny < len(grid) and 0 <= nx < len(grid[0])):
                continue
            if grid[ny][nx].key == key:
                out.add((nx, ny))
                todo.append((nx, ny))
    return out


def _rectangles(block: set[tuple[int, int]]) -> list[tuple[int, int, int, int]]:
    """A block as rectangles: itself when it is one no longer than
    MAX_PIECE each way, else its rows' runs in MAX_PIECE lengths."""
    xs = [x for x, _ in block]
    ys = [y for _, y in block]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    w, h = x1 - x0 + 1, y1 - y0 + 1
    if len(block) == w * h and w <= MAX_PIECE and h <= MAX_PIECE:
        return [(x0, y0, w, h)]
    out = []
    for y in range(y0, y1 + 1):
        run = sorted(x for x, yy in block if yy == y)
        start = prev = None
        for x in run + [None]:
            if x is not None and prev is not None and x == prev + 1 and x - start < MAX_PIECE:
                prev = x
                continue
            if start is not None:
                out.append((start, y, prev - start + 1, 1))
            start = prev = x
    return out


def _turned_labels(
    labels: Mapping[str, tuple[int, int]], w: int, h: int, rot: int, mirror: bool
) -> dict[str, list[int]]:
    return {n: list(turned_point(x, y, w, h, rot, mirror)) for n, (x, y) in labels.items()}


# A building's state that leaves nothing of its inside.
_GUTTED = re.compile(
    r"\b(burn(?:t|ed)(?: out| down)?|gutted|collaps(?:ed|ing)|ruined|razed|fire-damaged)\b",
    re.IGNORECASE,
)
GUTTED_RUBBLE = 0.3
GUTTED_PILES = 3
GUTTED_LOOT = 2


def _gutted(floor, rng: random.Random):
    """A floor as a fire left it: walls and doors as they were, the floor
    rubble in part, no stairs up, the furniture gone but for piles of
    scrap where some of it stood, and the ruins table rolled at a couple
    of spots."""
    index, lname, ex, labels = floor
    rows = [list(r) for r in ex.rows]
    for row in rows:
        for x, tile in enumerate(row):
            if tile == "_" and rng.random() < GUTTED_RUBBLE:
                row[x] = ":"
            elif tile == "&":
                row[x] = ":"
    spots = [(p["x"], p["y"]) for p in ex.pieces]
    rng.shuffle(spots)
    pieces = [
        {"name": "scrap-pile", "x": x, "y": y, "w": 1, "h": 1, "stock": "", "chance": 0}
        for x, y in spots[:GUTTED_PILES]
    ]
    loot = [
        {"x": x, "y": y, "table": "ruins", "chance": 60}
        for x, y in spots[GUTTED_PILES : GUTTED_PILES + GUTTED_LOOT]
    ]
    # No named rooms in a shell: a label with no room to hold it showed as a
    # place mark on the floor ("shop floor", live).
    return (index, lname, Extracted(["".join(r) for r in rows], pieces, loot), {})


def kit_layout(
    kit: Kit,
    place_id: str,
    name: str,
    what: str = "",
    *,
    size_m: float,
    door: str | None,
    seed: str,
    state: str = "",
) -> PlaceLayout | None:
    """A building's inside from the kit: its kind (by name, else words), a
    variant by weight among those near its size, its chunks by weight,
    turned so its main door is on `door` (the side the map's door is on).
    None when the kit has no kind for it. The same seed makes the same
    inside."""
    kind = kind_for(kit, name, what)
    if kind is None:
        return None
    rng = rng_for(seed, place_id, kind)
    template = choose_template(kit.buildings[kind], size_m, rng)
    if template is None:
        return None
    rot, mirror = orientation(template, door, rng)
    width = max(len(r) for r in template.rows)
    height = len(template.rows)
    floors: list[tuple[int, str, Extracted, dict[str, list[int]]]] = []
    for index, rows, own, labels, nested, lname in [
        (0, template.rows, template.legend, template.labels, template.nested, ""),
        *[
            (lv.index, lv.rows, {**template.legend, **lv.legend}, lv.labels, lv.nested, lv.name)
            for lv in template.levels
        ],
    ]:
        composed = _Composed([[Cell(" ") for _ in range(width)] for _ in range(height)])
        _stamp(
            kit,
            composed,
            rows,
            _legend(kit, template, own),
            labels,
            nested,
            (0, 0),
            f"{template.id}#{index}",
            rng,
            keep_blank=False,
        )
        grid = _turned_grid(composed.grid, rot, mirror)
        floors.append(
            (
                index,
                lname,
                extract(grid, composed.legend),
                _turned_labels(composed.labels, width, height, rot, mirror),
            )
        )
    if _GUTTED.search(state or ""):
        # A building burnt or fallen has nothing of its furniture left: its
        # ground floor a shell of rubble with a few piles of scrap and what
        # the ruins table rolls, its upper floors gone (live, Oct 26 2026: a
        # farmhouse "burnt to its frame" had the kit's whole inside, a rifle
        # by the door and a stocked pantry).
        floors = [_gutted(floors[0], rng)]
    ground = floors[0]
    spec = {
        "ground": "floor",
        "kit": kit.name,
        "template": template.id,
        "turn": [rot, mirror],
        "tile_m": template.tile_m,
        "pieces": ground[2].pieces,
        "loot": ground[2].loot,
    }
    made = hashlib.sha1(
        json.dumps([f[2].rows for f in floors] + [f[2].pieces for f in floors]).encode()
    ).hexdigest()[:12]
    levels = [
        LevelLayout(
            index=index,
            name=lname,
            place_id=place_id,
            state=state,
            spec={"kit": kit.name, "pieces": ex.pieces, "loot": ex.loot},
            rows=ex.rows,
            labels=labels,
            model=f"kit:{kit.name}",
            made_at=f"kit:{made}",
        )
        for index, lname, ex, labels in floors[1:]
    ]
    return PlaceLayout(
        place_id=place_id,
        state=state,
        spec=spec,
        rows=ground[2].rows,
        labels=ground[3],
        model=f"kit:{kit.name}",
        made_at=f"kit:{made}",
        levels=levels,
    )


# --- Yards ------------------------------------------------------------------------


def yard_pieces(
    kit: Kit,
    rows: Sequence[str],
    buildings: Mapping[str, tuple[int, int, int, int]],
    names: Mapping[str, str],
    tile_m: float,
    seed: str,
    *,
    keep: Collection[tuple[int, int]] = (),
    blocked: Collection[tuple[int, int]] = (),
    reach_from: Sequence[tuple[int, int]] = (),
) -> list[dict]:
    """Things stamped on open ground round each building of an outdoor
    area (CDDA's yard chunks: a woodpile, a wreck, a kennel), up to
    `kit.yard_per_building` each, by weight, sized from metres to the
    area's tiles; never on `keep` (in front of the doors) or `blocked`
    (what stands there), and never cutting off ground that `reach_from`
    could reach before."""
    height = len(rows)
    width = len(rows[0]) if rows else 0
    taken = set(blocked)
    out: list[dict] = []

    def walkable(x: int, y: int) -> bool:
        return 0 <= y < height and 0 <= x < width and rows[y][x] in WALKABLE_OUT

    def reach(block: set[tuple[int, int]]) -> set[tuple[int, int]]:
        seen = {s for s in reach_from if walkable(*s) and s not in block}
        todo = deque(seen)
        while todo:
            x, y = todo.popleft()
            for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if n not in seen and n not in block and walkable(*n):
                    seen.add(n)
                    todo.append(n)
        return seen

    before = reach(taken) if reach_from else set()
    for place_id, (bx0, by0, bx1, by1) in sorted(buildings.items()):
        name = names.get(place_id, "")
        pool = [
            p
            for p in kit.yard
            if p.weight > 0 and (not p.near or any(_has(name, w) for w in p.near))
        ]
        if not pool:
            continue
        rng = rng_for(seed, place_id, "yard")
        count = rng.randint(1, max(1, kit.yard_per_building))
        for n in range(count):
            piece = _pick(pool, rng)
            w = max(1, round(piece.w / max(tile_m, 0.1)))
            h = max(1, round(piece.h / max(tile_m, 0.1)))
            spots = []
            for y in range(by0 - YARD_REACH, by1 + YARD_REACH + 1):
                for x in range(bx0 - YARD_REACH, bx1 + YARD_REACH + 1):
                    fp = [(x + dx, y + dy) for dy in range(h) for dx in range(w)]
                    if any(
                        not (0 <= fy < height and 0 <= fx < width)
                        or rows[fy][fx] not in YARD_GROUND
                        or (fx, fy) in taken
                        or (fx, fy) in keep
                        or (bx0 - 1 <= fx <= bx1 + 1 and by0 - 1 <= fy <= by1 + 1)
                        for fx, fy in fp
                    ):
                        continue
                    spots.append((rng.random(), x, y, fp))
            spots.sort()
            for _r, x, y, fp in spots[:24]:
                trial = taken | set(fp)
                if reach_from and (before - set(fp)) - reach(trial):
                    continue  # it would cut ground off
                taken = trial
                before = before - set(fp)
                out.append(
                    {
                        "name": piece.thing,
                        "x": x,
                        "y": y,
                        "w": w,
                        "h": h,
                        "stock": piece.stock,
                        "chance": piece.chance,
                        "yard": place_id,
                        "n": n,
                    }
                )
                break
    return out


def _pick(pool: Sequence[YardPiece], rng: random.Random) -> YardPiece:
    total = sum(p.weight for p in pool)
    pick = rng.uniform(0, total)
    for p in pool:
        pick -= p.weight
        if pick <= 0:
            return p
    return pool[-1]


# --- Checking a kit ---------------------------------------------------------------


def kit_problems(kit: Kit, has_picture: Callable[[str], bool] | None = None) -> list[str]:
    """What is wrong with a kit, each as a sentence: rows of unequal length,
    a character no legend or tile knows, a table or chunk named and not
    there, a door side with no door on it, a level not on the template's
    grid; with `has_picture`, a thing with no picture of its own."""
    out: list[str] = []
    for tid, table in kit.tables.items():
        for e in table.entries:
            if e.table and e.table not in kit.tables:
                out.append(f"table {tid}: no table {e.table!r}")
    for rule in kit.containers:
        if rule.table not in kit.tables:
            out.append(f"container rule {rule.match}: no table {rule.table!r}")
    for piece in kit.yard:
        if piece.stock and piece.stock not in kit.tables:
            out.append(f"yard piece {piece.thing!r}: no table {piece.stock!r}")
    for key, outdoor in kit.outdoor.items():
        for obj in outdoor.objects:
            if obj.stock and obj.stock not in kit.tables:
                out.append(f"outdoor {key}: {obj.name!r} has no table {obj.stock!r}")
            if has_picture and not has_picture(obj.name):
                out.append(f"outdoor {key}: no picture of {obj.name!r}")
        for table, _chance in outdoor.loot:
            if table not in kit.tables:
                out.append(f"outdoor {key}: no loot table {table!r}")
        for building in outdoor.buildings:
            if kind_for(kit, building.name) is None:
                out.append(f"outdoor {key}: no kind of building for {building.name!r}")

    def legend_ok(where: str, legend: Mapping[str, LegendEntry]) -> None:
        for ch, entry in legend.items():
            if len(ch) != 1:
                out.append(f"{where}: legend key {ch!r} is not one character")
            if ch in TILES and ch != " ":
                out.append(f"{where}: legend key {ch!r} is a tile")
            for t in (entry.stock, entry.loot):
                if t and t not in kit.tables:
                    out.append(f"{where}: no table {t!r}")
            if has_picture and entry.thing and not has_picture(entry.thing):
                out.append(f"{where}: no picture of {entry.thing!r}")

    def rows_ok(where: str, rows: Sequence[str], legend: Mapping[str, LegendEntry], blank: bool):
        if len({len(r) for r in rows}) > 1:
            out.append(f"{where}: rows of unequal length")
        for ch in sorted({c for r in rows for c in r}):
            if ch not in TILES and ch not in legend and not (blank and ch == " "):
                out.append(f"{where}: {ch!r} is no tile and not in the legend")

    def nests_ok(where: str, nested: Sequence[Nest]):
        for nest in nested:
            for name, _w in nest.chunks:
                if name and name not in kit.chunks:
                    out.append(f"{where}: no chunk {name!r}")

    for pid, palette in kit.palettes.items():
        legend_ok(f"palette {pid}", palette)
    shared = {ch: e for palette in kit.palettes.values() for ch, e in palette.items()}
    for cid, chunk in kit.chunks.items():
        legend_ok(f"chunk {cid}", chunk.legend)
        rows_ok(f"chunk {cid}", chunk.rows, {**shared, **chunk.legend}, True)
        nests_ok(f"chunk {cid}", chunk.nested)
    for kind, building in kit.buildings.items():
        for t in building.variants:
            where = f"{kind}/{t.id}"
            for p in t.palettes:
                if p not in kit.palettes:
                    out.append(f"{where}: no palette {p!r}")
            legend = _legend(kit, t, t.legend)
            legend_ok(where, t.legend)
            rows_ok(where, t.rows, legend, False)
            nests_ok(where, t.nested)
            for side in t.doors:
                if not _door_on(t.rows, side):
                    out.append(f"{where}: no door on its {side} wall")
            for lv in t.levels:
                lw = f"{where} level {lv.index}"
                legend_ok(lw, lv.legend)
                rows_ok(lw, lv.rows, {**legend, **lv.legend}, False)
                nests_ok(lw, lv.nested)
                if len(lv.rows) != len(t.rows) or any(len(r) != len(t.rows[0]) for r in lv.rows):
                    out.append(f"{lw}: not on the template's grid")
    return out


def _door_on(rows: Sequence[str], side: str) -> bool:
    if not rows:
        return False
    edge = {
        "north": rows[0],
        "south": rows[-1],
        "west": "".join(r[0] for r in rows),
        "east": "".join(r[-1] for r in rows),
    }[side]
    return "+" in edge
