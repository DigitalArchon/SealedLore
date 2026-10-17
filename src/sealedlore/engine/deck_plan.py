"""A station's or ship's decks, laid out by the program from a brief.

Live (Oct 17 2026), a ring station drawn by the layout model came back as
one rectangle of floor with small walled boxes scattered on it, at 7.5 m a
tile, and its other decks didn't line up. The model is good at saying what a
place holds and poor at drawing it in tiles. So for a place in space the
model writes only a brief (`map.deck.brief`): the hull's shape, the decks,
and each deck's rooms by name, use and size. The program lays it out:

- one skeleton of corridors round a central lobby, shared by every deck, so
  the lifts stand in the same tiles on all of them (engine/levels.py links
  stairs tiles by where they stand);
- each deck's rooms packed along the corridors, a wall between neighbours
  and a door onto the corridor, the larger rooms first;
- the hull round what is built and space outside it, so the silhouette
  follows the rooms (as small ships' plans in pixel-art games do);
- viewports in the hull walls of the rooms that look out (a promenade, a
  bridge, a lounge), portholes along the corridors that run by the hull;
- an airlock at the end of each corridor on a deck that docks ships.

Pure: no Qt, no network, the same layout for the same brief and place.
"""

from __future__ import annotations

import math
import random
import re
import zlib
from dataclasses import dataclass, field
from typing import Any

from sealedlore.engine.jsonreply import extract_json
from sealedlore.engine.levels import LEVEL_RANGE, MAX_LEVELS

# Metres a deck's tile stands for: near a person's stride, so a corridor is
# a tile or two and a cabin a few tiles across (a 7.5 m tile, the drawn
# plan's, made every room a few tiles and every person a room wide).
DECK_TILE_M = 2.5

# The uses a room can have, the renderer's zones (gui/area_render.py): each
# its floor, its light and its accent.
USES = (
    "command",
    "medical",
    "engineering",
    "cargo",
    "hangar",
    "docking",
    "quarters",
    "dining",
    "promenade",
    "science",
    "security",
    "lounge",
    "garden",
    "storage",
    "airlock",
)
# A room's use from its words, the first that matches. Also used for rooms in
# a plan the model drew, by their names and purposes.
_USE_WORDS: tuple[tuple[str, str], ...] = (
    ("airlock", r"airlocks?|hatch(?:es)?|docking ports?"),
    ("hangar", r"hangars?|shuttle ?bays?|flight decks?|launch bays?|landing bays?"),
    ("docking", r"docking|docks?|berths?|pylons?|umbilicals?|moorings?"),
    (
        "command",
        r"command|bridge|operations|ops|control|helm|cic|conn|station master|"
        r"captain'?s|tactical|navigation|comms?|communications",
    ),
    ("medical", r"med\w*|infirmary|sick ?bay|clinic|hospital|surgery|triage"),
    (
        "engineering",
        r"engineering|engines?|reactors?|fusion|power|core|drives?|generators?|machine|"
        r"machinery|life support|workshop|fabricat\w+|repair",
    ),
    ("cargo", r"cargo|holds?|freight|warehouses?|loading|depot"),
    (
        "quarters",
        r"quarters|cabins?|bunks?|habitat|habs?|dorms?|dormitor\w+|apartments?|suites?|"
        r"residen\w+|barracks|sleeping|bedrooms?",
    ),
    (
        "dining",
        r"mess|galley|bars?|tavern|cantina|restaurants?|caf[eé]s?|canteen|kitchens?|"
        r"diner|pub|saloon|tea ?room",
    ),
    ("promenade", r"promenade|concourse|market\w*|bazaar|plaza|mall|shops?|arcade|atrium"),
    ("science", r"labs?|laborator\w+|science|research|sensors?|astrometric\w*|stellar"),
    ("security", r"security|brig|armou?ry|cells?|guard\w*|detention|prison|lock-?up"),
    (
        "lounge",
        r"lounges?|observation|viewing|recreation|rec room|gym\w*|chapel|shrine|temple|"
        r"holo\w*|theat(?:re|er)|club|casino|ballroom|salon",
    ),
    ("garden", r"gardens?|hydroponic\w*|arboretum|greenhouse|farms?|orchard"),
    ("storage", r"storage|store ?rooms?|stores|suppl\w+|maintenance|utility|closets?|lockers?"),
)
_USE_RES = tuple((use, re.compile(rf"\b(?:{words})\b", re.I)) for use, words in _USE_WORDS)

# Interior tiles (along the corridor, away from it) of each size.
SIZES: dict[str, tuple[int, int]] = {
    "small": (4, 3),
    "medium": (6, 4),
    "large": (9, 6),
    "huge": (14, 7),
}
_SIZE_ORDER = ("small", "medium", "large", "huge")
# The size a use takes when the brief gives none, and the least it may be.
_USE_SIZE = {
    "hangar": ("large", "large"),
    "promenade": ("huge", "large"),
    "cargo": ("large", "medium"),
    "command": ("medium", "medium"),
    "engineering": ("large", "medium"),
    "airlock": ("small", "small"),
}
# Rooms that look out: viewports in their hull walls.
VIEW_USES = frozenset({"promenade", "lounge", "command", "dining", "docking", "science"})
# Rooms that must face open space: those that look out, and those ships
# come and go by.
OUT_USES = VIEW_USES | {"hangar", "docking", "airlock"}
SHAPES = ("ring", "cross", "spine")
_SHAPE_WORDS = (
    ("ring", r"ring|wheel|torus|toroid|donut|doughnut|circle|disc|disk|cylinder|block|grid"),
    ("spine", r"spine|ship|long|needle|hull|wedge|arrow|narrow|bow"),
    ("cross", r"cross|plus|star|arms?|hub|spokes?|x"),
)
MAX_ROOMS = 14
CORRIDOR = 2  # tiles across
LOBBY = 6  # the central lobby's interior, tiles across
FLOOR = "_"


def use_of(*words: str) -> str:
    """A room's use from its words (a name, a purpose, the brief's own use
    word), or "" when none says."""
    text = " ".join(w for w in words if w)
    for use, pattern in _USE_RES:
        if pattern.search(text):
            return use
    return ""


# --- The brief ----------------------------------------------------------------------


@dataclass(frozen=True)
class BriefRoom:
    name: str
    use: str
    size: str


@dataclass
class BriefDeck:
    index: int
    name: str
    rooms: list[BriefRoom] = field(default_factory=list)


@dataclass
class Brief:
    shape: str
    decks: list[BriefDeck]

    def as_dict(self) -> dict[str, Any]:
        return {
            "shape": self.shape,
            "decks": [
                {
                    "index": d.index,
                    "name": d.name,
                    "rooms": [{"name": r.name, "use": r.use, "size": r.size} for r in d.rooms],
                }
                for d in self.decks
            ],
        }


def _clean(text: Any, limit: int = 40) -> str:
    return " ".join(str(text or "").split())[:limit]


def _shape(word: Any) -> str:
    text = _clean(word).lower()
    if text in SHAPES:
        return text
    for shape, words in _SHAPE_WORDS:
        if re.search(rf"\b(?:{words})\b", text):
            return shape
    return "ring"


def _size(word: Any, use: str) -> str:
    default, least = _USE_SIZE.get(use, ("medium", "small"))
    text = _clean(word).lower()
    size = text if text in SIZES else default
    if _SIZE_ORDER.index(size) < _SIZE_ORDER.index(least):
        size = least
    if use == "quarters" and size in ("large", "huge"):
        size = "medium"  # a deck of cabins, not a hall
    if use == "airlock":
        size = "small"
    return size


def parse_brief(text: str, *, named: tuple[tuple[str, str], ...] = ()) -> Brief:
    """The brief in a reply, held to the rules: a known shape, decks with whole
    indexes in the levels' range (one each, the main deck 0), up to
    MAX_ROOMS rooms a deck, each room once on the station with a known use
    and size. `named` (name, what) are the places the map already puts in
    it: each one missing from the reply goes on the main deck. ValueError
    for a reply with no rooms at all."""
    data = extract_json(text, dict, "decks")
    raw = data.get("decks")
    if not isinstance(raw, list) or not raw:
        raise ValueError("the brief has no decks")
    seen: set[str] = set()
    decks: dict[int, BriefDeck] = {}
    for position, entry in enumerate(raw):
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry.get("index", position))
        except (TypeError, ValueError, OverflowError):
            index = position
        if index in decks:
            continue
        deck = BriefDeck(index=index, name=_clean(entry.get("name")) or f"Deck {index}")
        for item in entry.get("rooms") if isinstance(entry.get("rooms"), list) else []:
            if not isinstance(item, dict) or len(deck.rooms) >= MAX_ROOMS:
                continue
            name = _clean(item.get("name"))
            if not name or name.lower() in seen:
                continue
            use = use_of(_clean(item.get("use"), 60)) or use_of(name) or "storage"
            deck.rooms.append(BriefRoom(name, use, _size(item.get("size"), use)))
            seen.add(name.lower())
        decks[index] = deck
    decks = {i: d for i, d in decks.items() if d.rooms}
    if not decks:
        raise ValueError("the brief has no rooms")
    if 0 not in decks:  # the deck nearest the main one is the main one
        nearest = min(decks, key=lambda i: (abs(i), i))
        decks = {i - nearest: BriefDeck(i - nearest, d.name, d.rooms) for i, d in decks.items()}
    lo, hi = LEVEL_RANGE
    kept = sorted((i for i in decks if lo <= i <= hi), key=lambda i: (abs(i), i))
    kept = kept[: MAX_LEVELS + 1]
    main = decks[0]
    for name, what in named:
        if name.lower() not in seen and len(main.rooms) < MAX_ROOMS:
            use = use_of(name, what) or "storage"
            main.rooms.append(BriefRoom(_clean(name), use, _size("", use)))
            seen.add(name.lower())
    return Brief(_shape(data.get("shape")), [decks[i] for i in sorted(kept)])


# --- The layout -----------------------------------------------------------------------


@dataclass
class DeckRoom:
    name: str
    use: str
    box: tuple[int, int, int, int]  # interior, inclusive
    door: tuple[int, int]
    view: bool = False


@dataclass
class Deck:
    index: int
    name: str
    rows: list[str]
    rooms: list[DeckRoom]
    labels: dict[str, list[int]]

    def room_records(self) -> list[dict[str, Any]]:
        return [{"name": r.name, "use": r.use, "box": list(r.box)} for r in self.rooms]


@dataclass
class DeckPlan:
    shape: str
    decks: dict[int, Deck]
    lifts: list[tuple[int, int]]
    tile_m: float = DECK_TILE_M
    dropped: list[str] = field(default_factory=list)


Rect = tuple[int, int, int, int]  # x0, y0, x1, y1 inclusive


def _skeleton(shape: str, s: int, cx: int, cy: int) -> tuple[list[Rect], Rect, list[int]]:
    """(corridor rects, the lobby, which rects may be cut back at their open
    end) for a shape at scale `s` round the canvas's middle. Each corridor
    is CORRIDOR wide and meets the lobby or another corridor floor to floor."""
    half = LOBBY // 2
    lobby = (cx - half, cy - half, cx + half - 1, cy + half - 1)
    c0, c1 = cx - 1, cx  # the corridor's two tiles across, centred
    rects: list[Rect] = []
    trimmable: list[int] = []
    if shape == "spine":
        length = 2 * s
        rects.append((cx - length, cy - 1, lobby[0] - 1, cy))  # aft
        rects.append((lobby[2] + 1, cy - 1, cx + length, cy))  # fore
        trimmable += [0, 1]
        for x in (cx - length * 2 // 3, cx + length * 2 // 3):
            branch = max(4, s // 2)
            rects.append((x - 1, cy - 1 - branch, x, cy - 2))
            rects.append((x - 1, cy + 1, x, cy + branch))
            trimmable += [len(rects) - 2, len(rects) - 1]
    elif shape == "cross":
        rects.append((c0, lobby[1] - s, c1, lobby[1] - 1))  # north
        rects.append((c0, lobby[3] + 1, c1, lobby[3] + s))  # south
        rects.append((lobby[0] - s, cy - 1, lobby[0] - 1, cy))  # west
        rects.append((lobby[2] + 1, cy - 1, lobby[2] + s, cy))  # east
        trimmable += [0, 1, 2, 3]
    else:  # ring: a loop round the lobby, spokes out to it
        ry = max(LOBBY + 5, round(s * 0.55))
        rx = max(s, round(ry / 0.6))
        top, bottom = cy - ry, cy + ry - 1
        left, right = cx - rx, cx + rx - 1
        rects.append((left, top, right, top + 1))
        rects.append((left, bottom - 1, right, bottom))
        rects.append((left, top + 2, left + 1, bottom - 2))
        rects.append((right - 1, top + 2, right, bottom - 2))
        rects.append((c0, top + 2, c1, lobby[1] - 1))
        rects.append((c0, lobby[3] + 1, c1, bottom - 2))
        rects.append((left + 2, cy - 1, lobby[0] - 1, cy))
        rects.append((lobby[2] + 1, cy - 1, right - 2, cy))
    return rects, lobby, trimmable


class _Canvas:
    """Who owns each tile while a deck is packed: -1 nobody, 0 the skeleton,
    k > 0 the k-th room; with a running count of floor for O(1) checks."""

    def __init__(self, width: int, height: int) -> None:
        self.w, self.h = width, height
        self.own = [[-1] * width for _ in range(height)]
        self._sums: list[list[int]] | None = None

    def fill(self, rect: Rect, owner: int) -> None:
        x0, y0, x1, y1 = rect
        for y in range(max(0, y0), min(self.h, y1 + 1)):
            for x in range(max(0, x0), min(self.w, x1 + 1)):
                self.own[y][x] = owner
        self._sums = None

    def clear(self, rect: Rect) -> None:
        self.fill(rect, -1)

    def floor_in(self, rect: Rect) -> int:
        x0, y0, x1, y1 = rect
        if x0 < 0 or y0 < 0 or x1 >= self.w or y1 >= self.h:
            return 1 << 20  # off the canvas counts as full
        if self._sums is None:
            sums = [[0] * (self.w + 1) for _ in range(self.h + 1)]
            for y in range(self.h):
                run = 0
                row, above, below = self.own[y], sums[y], sums[y + 1]
                for x in range(self.w):
                    run += row[x] >= 0
                    below[x + 1] = above[x + 1] + run
            self._sums = sums
        s = self._sums
        return s[y1 + 1][x1 + 1] - s[y0][x1 + 1] - s[y1 + 1][x0] + s[y0][x0]

    def owner(self, x: int, y: int) -> int:
        return self.own[y][x] if 0 <= x < self.w and 0 <= y < self.h else -1


@dataclass
class _Slot:
    room: Rect  # interior
    door: tuple[int, int]
    out: tuple[int, int]  # the way the room faces from its door: away from the corridor
    edge: tuple[int, str] = (0, "n")  # the corridor rect it stands against, and which side


def _slots(canvas: _Canvas, rects: list[Rect], w: int, d: int) -> list[_Slot]:
    """Every place a room of `w` along by `d` deep fits against a corridor:
    its interior a wall away from the corridor's floor, its door in that
    wall in front of floor, nothing built within a tile of it."""
    found: list[_Slot] = []
    for k, (x0, y0, x1, y1) in enumerate(rects):
        for side in "nsew":
            if side in "ns":
                wall_y = y0 - 1 if side == "n" else y1 + 1
                iy0, iy1 = (wall_y - d, wall_y - 1) if side == "n" else (wall_y + 1, wall_y + d)
                for p in range(x0 - w + 2, x1):
                    room = (p, iy0, p + w - 1, iy1)
                    door_x = min(max(p + w // 2, x0), x1)
                    if not (p <= door_x <= p + w - 1):
                        continue
                    grown = (room[0] - 1, room[1] - 1, room[2] + 1, room[3] + 1)
                    if canvas.floor_in(grown):
                        continue
                    out = (0, -1 if side == "n" else 1)
                    found.append(_Slot(room, (door_x, wall_y), out, (k, side)))
            else:
                # Against a north-south edge the room's length runs down it
                # and its depth away from it, as against an east-west one.
                wall_x = x0 - 1 if side == "w" else x1 + 1
                ix0, ix1 = (wall_x - d, wall_x - 1) if side == "w" else (wall_x + 1, wall_x + d)
                for p in range(y0 - w + 2, y1):
                    room = (ix0, p, ix1, p + w - 1)
                    door_y = min(max(p + w // 2, y0), y1)
                    if not (p <= door_y <= p + w - 1):
                        continue
                    grown = (room[0] - 1, room[1] - 1, room[2] + 1, room[3] + 1)
                    if canvas.floor_in(grown):
                        continue
                    out = (-1 if side == "w" else 1, 0)
                    found.append(_Slot(room, (wall_x, door_y), out, (k, side)))
    return found


def _score(
    canvas: _Canvas, slot: _Slot, view: bool, cx: int, cy: int, load: dict[tuple[int, str], int]
) -> float:
    """Lower is better: sharing walls with rooms already built, near the
    middle, a corridor side with fewer rooms on it yet (live, every room
    went to one side and the rest of the station was bare corridor), and
    for a room that looks out, nothing built beyond it."""
    x0, y0, x1, y1 = slot.room
    shared = 0
    for x in range(x0 - 2, x1 + 3):
        shared += canvas.owner(x, y0 - 2) > 0
        shared += canvas.owner(x, y1 + 2) > 0
    for y in range(y0 - 1, y1 + 2):
        shared += canvas.owner(x0 - 2, y) > 0
        shared += canvas.owner(x1 + 2, y) > 0
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    score = -1.0 * shared + 0.06 * math.hypot(mx - cx, my - cy) + 2.5 * load.get(slot.edge, 0)
    if view:
        # Out to the canvas's edge: a courtyard of space inside a ring is
        # no place for an airlock or a docking bay.
        dx, dy = slot.out
        far_x, far_y = canvas.w - 1, canvas.h - 1
        if dx == 0:
            beyond = (x0, 0, x1, y0 - 2) if dy < 0 else (x0, y1 + 2, x1, far_y)
        else:
            beyond = (0, y0, x0 - 2, y1) if dx < 0 else (x1 + 2, y0, far_x, y1)
        bx0, by0, bx1, by1 = beyond
        clipped = (max(0, bx0), max(0, by0), min(canvas.w - 1, bx1), min(canvas.h - 1, by1))
        if clipped[0] <= clipped[2] and clipped[1] <= clipped[3]:
            score += 6 * min(1, canvas.floor_in(clipped))
    return score


def _dims(room: BriefRoom, rng: random.Random) -> tuple[int, int]:
    w, d = SIZES[room.size]
    if room.use != "airlock":
        w += rng.choice((-1, 0, 0, 1))
        d += rng.choice((0, 0, 1)) if room.size != "small" else 0
    return max(3, w), max(2, d)


def _need(deck: BriefDeck) -> int:
    """The corridor frontage a deck's rooms take, a wall between each."""
    return sum(SIZES[r.size][0] + 1 for r in deck.rooms if r.use != "airlock")


def _pack(
    brief_deck: BriefDeck, shape: str, s: int, size: tuple[int, int], seed: int, most: int
) -> tuple[_Canvas, list[DeckRoom], list[Rect], Rect, list[str]] | None:
    """One deck packed on the shared skeleton, or None when a room won't fit
    (the caller grows the skeleton). A ring station's deck of few rooms (a
    spire's top, a small engineering deck) is a cross of short corridors
    round the same lobby, so its lifts still line up, not the whole ring
    with nothing on it."""
    width, height = size
    cx, cy = width // 2, height // 2
    need = _need(brief_deck)
    if shape == "ring" and need < 0.6 * most:
        shape, s = "cross", max(6, round(need / 8 * 1.2) + 2)
    rects, lobby, trimmable = _skeleton(shape, s, cx, cy)
    canvas = _Canvas(width, height)
    canvas.fill(lobby, 0)
    for rect in rects:
        canvas.fill(rect, 0)
    rng = random.Random(seed * 31 + brief_deck.index)
    order = sorted(
        brief_deck.rooms,
        key=lambda r: (-_SIZE_ORDER.index(r.size), r.use not in OUT_USES, r.name),
    )
    rooms: list[DeckRoom] = []
    load: dict[tuple[int, str], int] = {}
    edges = [lobby, *rects]

    def place(name: str, use: str, w: int, d: int, view: bool, outward: bool) -> bool:
        best: tuple[float, _Slot] | None = None
        for shape_wd in ((w, d), (d + 1, max(2, w - 1))) if w > d + 2 else ((w, d),):
            for slot in _slots(canvas, edges, *shape_wd):
                score = _score(canvas, slot, outward, cx, cy, load) + rng.random() * 0.01
                if best is None or score < best[0]:
                    best = (score, slot)
        if best is None:
            return False
        slot = best[1]
        canvas.fill(slot.room, len(rooms) + 1)
        load[slot.edge] = load.get(slot.edge, 0) + 1
        rooms.append(DeckRoom(name, use, slot.room, slot.door, view))
        return True

    for room in order:
        if room.use == "airlock":
            continue  # after the corridors are cut back, below
        w, d = _dims(room, rng)
        view = room.use in VIEW_USES
        if not place(room.name, room.use, w, d, view, room.use in OUT_USES):
            return None
    # Corridors cut back to the last door or junction along them (two tiles
    # beyond it): the branches first, a branch with no door taken away, then
    # the corridors from the lobby, out to every branch still joined to them
    # (cut to their own doors alone, a ship's spine left its branches and
    # their rooms cut off).
    doors = [r.door for r in rooms]

    def touches(a: Rect, b: Rect) -> bool:
        return a[0] - 1 <= b[2] and b[0] - 1 <= a[2] and a[1] - 1 <= b[3] and b[1] - 1 <= a[3]

    def joined(i: int) -> bool:
        return touches(rects[i], lobby)

    removed: set[int] = set()
    for i in sorted(trimmable, key=joined):  # branches (not at the lobby) first
        x0, y0, x1, y1 = rect = rects[i]
        horizontal = (x1 - x0) > (y1 - y0)
        along = [
            (dx if horizontal else dy)
            for dx, dy in doors
            if (x0 - 1 <= dx <= x1 + 1 and y0 - 1 <= dy <= y1 + 1)
        ]
        for k, other in enumerate(rects):
            if k != i and k not in removed and not joined(k) and touches(rect, other):
                along += [other[0], other[2]] if horizontal else [other[1], other[3]]
        canvas.clear(rect)
        if not along and not joined(i):
            removed.add(i)
            rects[i] = (0, 0, -1, -1)
            edges[i + 1] = rects[i]
            continue
        inner_at_start = (x0 > cx) if horizontal else (y0 > cy)  # the end nearer the lobby
        lo, hi = (x0, x1) if horizontal else (y0, y1)
        if inner_at_start:
            keep = (lo, min(hi, max(max(along, default=lo) + 2, lo + 2)))
        else:
            keep = (max(lo, min(min(along, default=hi) - 2, hi - 2)), hi)
        new = (keep[0], y0, keep[1], y1) if horizontal else (x0, keep[0], x1, keep[1])
        canvas.fill(new, 0)
        rects[i] = new
        edges[i + 1] = new
    for i in removed:
        rects[i] = (0, 0, -1, -1)
    rects = [r for r in rects if r[2] >= r[0]]
    # Airlocks: the brief's own, and two on a deck that docks ships (the
    # main deck too), each a small lock facing out with a hatch in its far wall.
    named_locks = [r for r in brief_deck.rooms if r.use == "airlock"]
    docks = brief_deck.index == 0 or any(r.use in ("docking", "hangar") for r in brief_deck.rooms)
    locks = [r.name for r in named_locks] or (["Airlock", "Airlock"] if docks else [])
    notes: list[str] = []
    for k, name in enumerate(locks):
        if not place(
            name if name != "Airlock" or len(locks) == 1 else f"Airlock {k + 1}",
            "airlock",
            2,
            2,
            False,
            True,
        ):
            notes.append(f"no room on the hull for {name}")
    return canvas, rooms, rects, lobby, notes


def _rows(
    canvas: _Canvas,
    rooms: list[DeckRoom],
    lifts: list[tuple[int, int]],
    corridor_rects: list[Rect],
) -> list[str]:
    """The deck's tiles: floor where anything is built, walls round it,
    doors, viewports, portholes, the lifts, the airlocks' hatches."""
    w, h = canvas.w, canvas.h
    grid = [[FLOOR if canvas.own[y][x] >= 0 else " " for x in range(w)] for y in range(h)]
    for y in range(h):
        for x in range(w):
            if grid[y][x] != " ":
                continue
            if any(
                0 <= x + dx < w and 0 <= y + dy < h and canvas.own[y + dy][x + dx] >= 0
                for dy in (-1, 0, 1)
                for dx in (-1, 0, 1)
            ):
                grid[y][x] = "#"

    def outside(x: int, y: int) -> bool:
        return not (0 <= x < w and 0 <= y < h) or grid[y][x] == " "

    for room in rooms:
        x0, y0, x1, y1 = room.box
        if room.view:
            ring = [(x, y0 - 1) for x in range(x0, x1 + 1)]
            ring += [(x, y1 + 1) for x in range(x0, x1 + 1)]
            ring += [(x0 - 1, y) for y in range(y0, y1 + 1)]
            ring += [(x1 + 1, y) for y in range(y0, y1 + 1)]
            for x, y in ring:
                if grid[y][x] == "#" and any(
                    outside(x + dx, y + dy) for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0))
                ):
                    grid[y][x] = "W"
    for room in rooms:
        dx, dy = room.door
        grid[dy][dx] = "+"
        if room.use == "airlock":
            # The hatch: the lock's far wall, away from its door.
            x0, y0, x1, y1 = room.box
            if dx < x0:
                hatch = (x1 + 1, y0)
            elif dx > x1:
                hatch = (x0 - 1, y0)
            elif dy < y0:
                hatch = (x0, y1 + 1)
            else:
                hatch = (x0, y0 - 1)
            hx, hy = hatch
            if 0 <= hx < w and 0 <= hy < h and grid[hy][hx] == "#":
                grid[hy][hx] = "+"
    # Portholes along corridors that run by the hull: every fifth wall tile.
    for x0, y0, x1, y1 in corridor_rects:
        for y in range(y0 - 1, y1 + 2):
            for x in range(x0 - 1, x1 + 2):
                if not (0 <= x < w and 0 <= y < h) or grid[y][x] != "#":
                    continue
                if (x + y) % 5 == 0 and any(
                    outside(x + ddx, y + ddy) for ddx, ddy in ((0, -1), (0, 1), (-1, 0), (1, 0))
                ):
                    grid[y][x] = "W"
    for lx, ly in lifts:
        grid[ly][lx] = "&"
    return ["".join(r) for r in grid]


def generate(brief: Brief, place_id: str) -> DeckPlan:
    """Every deck of the brief laid out on one skeleton, cropped to one frame
    so a tile is the same spot on every deck."""
    seed = zlib.crc32(place_id.encode("utf-8"))
    need = max(_need(d) for d in brief.decks)
    s = max(8, round(need / (10 if brief.shape == "ring" else 8) * 1.2) + 3)
    for _attempt in range(6):
        span = 2 * s + 2 * max(SIZES["huge"][1], 8) + 12
        size = (span + (2 * s if brief.shape == "spine" else 0), span)
        packed: dict[int, Any] = {}
        failed = False
        for deck in brief.decks:
            result = _pack(deck, brief.shape, s, size, seed, need)
            if result is None:
                failed = True
                break
            packed[deck.index] = result
        if not failed:
            break
        s = round(s * 1.25) + 1
    else:
        raise ValueError("the decks' rooms would not fit")
    width, height = size
    cx, cy = width // 2, height // 2
    lifts = [(cx - 1, cy), (cx, cy)]
    built: dict[int, tuple[list[str], list[DeckRoom]]] = {}
    dropped: list[str] = []
    for deck in brief.decks:
        canvas, rooms, rects, lobby, notes = packed[deck.index]
        built[deck.index] = (_rows(canvas, rooms, lifts, rects), rooms)
        dropped += notes
    # One frame for every deck: what any deck builds, a tile of space round it.
    xs: list[int] = []
    ys: list[int] = []
    for rows, _rooms in built.values():
        for y, row in enumerate(rows):
            for x, ch in enumerate(row):
                if ch != " ":
                    xs.append(x)
                    ys.append(y)
    fx0, fy0 = max(0, min(xs) - 1), max(0, min(ys) - 1)
    fx1, fy1 = min(width - 1, max(xs) + 1), min(height - 1, max(ys) + 1)
    decks: dict[int, Deck] = {}
    for deck in brief.decks:
        rows, rooms = built[deck.index]
        cropped = [row[fx0 : fx1 + 1] for row in rows[fy0 : fy1 + 1]]
        moved = [
            DeckRoom(
                r.name,
                r.use,
                (r.box[0] - fx0, r.box[1] - fy0, r.box[2] - fx0, r.box[3] - fy0),
                (r.door[0] - fx0, r.door[1] - fy0),
                r.view,
            )
            for r in rooms
        ]
        labels = {
            r.name: [(r.box[0] + r.box[2]) // 2, (r.box[1] + r.box[3]) // 2]
            for r in moved
            if r.use != "airlock"
        }
        decks[deck.index] = Deck(deck.index, deck.name, cropped, moved, labels)
    return DeckPlan(
        brief.shape,
        decks,
        [(x - fx0, y - fy0) for x, y in lifts],
        dropped=dropped,
    )
