"""A station's or ship's decks, laid out by the program from a brief.

Live (Oct 17 2026), a ring station drawn by the layout model came back as
one rectangle of floor with small walled boxes scattered on it, at 7.5 m a
tile, and its other decks didn't line up. The model is good at saying what a
place holds and poor at drawing it in tiles. So for a place in space the
model writes only a brief (`map.deck.brief`): the hull's shape, the decks,
and each deck's rooms by name, use and size. The program lays it out:

- one skeleton of corridors round a lift lobby, shared by every deck, so
  the lifts stand in the same tiles on all of them (engine/levels.py links
  stairs tiles by where they stand): a block's loop and spokes round it in
  the middle, a ring's loop with the lobby on its side;
- each deck's rooms packed along the corridors, a wall between neighbours
  and a door onto the corridor, the larger rooms first;
- every gap the rooms leave inside filled with service rooms (storerooms,
  machinery), but a ring's one open middle (the author, Oct 17 2026: the
  courtyards of stars between a ring's spokes read as rooms of space);
- the hull round what is built and space outside it, so the silhouette
  follows the rooms (as small ships' plans in pixel-art games do);
- viewports in the hull walls of the rooms that look out (a promenade, a
  bridge, a lounge), portholes along the corridors that run by the hull;
- airlocks off the main corridors, facing open space, on a deck that docks
  ships;
- the hull's mass round it all (`HULL`, `ROCK`, `ENGINE`, painted by
  gui/deck_look.py): a station's plating closed round its rooms, a ship's
  hull tapering to its bridge with engines at the stern, an outpost's
  asteroid rock; the blind judges (Oct 17 2026) found the bare outline
  boxy, a ring's courtyards holes, and a freighter with no bow or stern;
- set pieces in the rooms that make a station what it is (a reactor in
  engineering, consoles facing a bridge's viewport, a shuttle in a hangar,
  a mine's shaft, a garden's beds), placed before the furniture.

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
# The hull seen from above. A block is solid, rooms all through it (most
# stations); a ring is a ring round one open middle; a cross is arms out
# from a core; a spine is long and narrow, bow to stern.
SHAPES = ("block", "ring", "cross", "spine")
_SHAPE_WORDS = (
    ("ring", r"ring|wheel|torus|toroid|donut|doughnut|annulus|hoop"),
    ("spine", r"spine|ship|long|needle|hull|wedge|arrow|narrow|bow"),
    ("cross", r"cross|plus|star|arms?|hub|spokes?|x"),
    ("block", r"block|cylinder|drum|disc|disk|circle|grid|cube|box|sphere|slab|solid"),
)
# What the hull is: a built station, a ship (a bow and engines), or rock
# (an outpost dug into an asteroid, a moon, a cliff).
HULLS = ("station", "ship", "rock")
_HULL_KIND_WORDS = (
    (
        "rock",
        r"rock|asteroids?|planetoids?|moons?|comets?|cliffs?|caverns?|caves?|dug|carved|"
        r"hollow\w*|tunnel\w*|crust|bedrock",
    ),
    (
        "ship",
        r"ships?|starships?|vessels?|freighters?|craft|shuttles?|cruisers?|frigates?|"
        r"haulers?|tugs?|carriers?|corvettes?|destroyers?|yachts?|transports?",
    ),
    ("station", r"stations?|starbases?|habitats?|platforms?|outposts?|orbitals?|bases?"),
)
# The hull's mass, as `Deck.hull` marks it (the deck's rows keep " " there:
# nobody walks or sees through it any differently).
HULL, ROCK, ENGINE = "H", "R", "E"
# How far plating reaches past the outer walls, and the notches it closes.
RIM = 1
CLOSE = 3
# The bare corridor before a ship's bridge, so its hull narrows to the bow.
NECK = 5
# Set pieces the program draws itself (gui/deck_look.py), and those it
# places as furniture the set has sprites for.
DRAWN_PIECES = ("reactor", "shaft", "beds")
REACTOR_WORDS = re.compile(r"\b(drive|jump|warp|hyperdrive)\b", re.I)
SHAFT_WORDS = re.compile(
    r"\b(mine|mines|mining|shaft|drill\w*|pit|ore ?head|bore\w*|dig\w*)\b", re.I
)
CONSOLE = "a curved command console bank"
CHAIR = "a stout captain's chair"
SHUTTLE = "a small shuttlecraft"
HOLO = "a holo-projection meeting table"
MAX_ROOMS = 14
CORRIDOR = 2  # tiles across
LOBBY = 6  # the central lobby's interior, tiles across
FLOOR = "_"
# What the program puts in the gaps the rooms leave inside a station (the
# author, Oct 17 2026: an enclosed patch of stars inside a station read as a
# room of space): service rooms off the nearest corridor, each at most
# FILL_MAX tiles along and at least FILL_MIN across; smaller gaps plating.
FILLERS = (
    "Storeroom",
    "Machinery Room",
    "Maintenance Bay",
    "Spare Parts Store",
    "Pump Room",
    "Life Support",
    "Utility Room",
    "Supply Store",
    "Air Plant",
    "Water Reclamation",
    "Equipment Locker",
    "Recycling",
)
FILL_MAX = 9
FILL_MIN = 3
# A ring keeps its middle open: the hole is held back from the rooms, a
# medium room's depth and its walls in from the ring's inner corridor.
RING_BAND = 6
RING_SPREAD = 12.0


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
    hull: str = "station"

    def as_dict(self) -> dict[str, Any]:
        return {
            "shape": self.shape,
            "hull": self.hull,
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
    return "block"


def hull_kind(*words: str) -> str:
    """What a hull is from words (the brief's own "hull", a place's name and
    what is said of it), "" when none says."""
    text = " ".join(w for w in words if w)
    for kind, pattern in _HULL_KIND_WORDS:
        if re.search(rf"\b(?:{pattern})\b", text, re.I):
            return kind
    return ""


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
    shape = _shape(data.get("shape"))
    hull = hull_kind(_clean(data.get("hull"))) or ("ship" if shape == "spine" else "station")
    return Brief(shape, [decks[i] for i in sorted(kept)], hull)


# --- The layout -----------------------------------------------------------------------


@dataclass
class Piece:
    """A set piece in a room: drawn by the program (`piece` one of
    DRAWN_PIECES) or a piece of furniture from the set's sprites ("")."""

    name: str
    box: tuple[int, int, int, int]  # inclusive
    piece: str = ""

    def record(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "box": list(self.box)}
        if self.piece:
            out["piece"] = self.piece
        return out


@dataclass
class DeckRoom:
    name: str
    use: str
    box: tuple[int, int, int, int]  # interior, inclusive
    door: tuple[int, int]
    view: bool = False
    out: tuple[int, int] = (0, -1)  # the way it faces from its door
    pieces: list[Piece] = field(default_factory=list)
    filler: bool = False  # a service room the program put in a gap


@dataclass
class Deck:
    index: int
    name: str
    rows: list[str]
    rooms: list[DeckRoom]
    labels: dict[str, list[int]]
    hull: list[str] = field(default_factory=list)

    def room_records(self) -> list[dict[str, Any]]:
        out = []
        for r in self.rooms:
            record: dict[str, Any] = {"name": r.name, "use": r.use, "box": list(r.box)}
            if r.pieces:
                record["pieces"] = [p.record() for p in r.pieces]
            out.append(record)
        return out


@dataclass
class DeckPlan:
    shape: str
    decks: dict[int, Deck]
    lifts: list[tuple[int, int]]
    tile_m: float = DECK_TILE_M
    dropped: list[str] = field(default_factory=list)
    hull: str = "station"


Rect = tuple[int, int, int, int]  # x0, y0, x1, y1 inclusive


def _ring_radii(s: int) -> tuple[int, int]:
    """A ring's half-width and half-height at scale `s`: near round, so it
    reads as a wheel, and wide enough inside for its rooms and its hole."""
    ry = max(LOBBY + RING_BAND + 1, round(s * 0.75))
    return ry, ry


def _hub(shape: str, s: int, cx: int, cy: int) -> tuple[int, int]:
    """Where the lobby and its lifts stand: the middle, but on a ring its
    southern side (the middle is the ring's hole)."""
    if shape == "ring":
        return cx, cy + _ring_radii(s)[1] - 1
    return cx, cy


def _skeleton(
    shape: str, s: int, cx: int, cy: int
) -> tuple[list[Rect], Rect, list[int], Rect | None]:
    """(corridor rects, the lobby, which rects may be cut back at their open
    end, the hole kept open) for a shape at scale `s` round the canvas's
    middle. Each corridor is CORRIDOR wide and meets the lobby or another
    corridor floor to floor."""
    half = LOBBY // 2
    hx, hy = _hub(shape, s, cx, cy)
    lobby = (hx - half, hy - half, hx + half - 1, hy + half - 1)
    c0, c1 = cx - 1, cx  # the corridor's two tiles across, centred
    rects: list[Rect] = []
    trimmable: list[int] = []
    hole: Rect | None = None
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
    elif shape == "ring":
        # A loop round one open middle, the lobby across its southern side:
        # no spokes, which cut the middle into courtyards of space.
        rx, ry = _ring_radii(s)
        top, bottom = cy - ry, cy + ry - 1
        left, right = cx - rx, cx + rx - 1
        rects.append((left, top, right, top + 1))
        rects.append((left, bottom - 1, right, bottom))
        rects.append((left, top + 2, left + 1, bottom - 2))
        rects.append((right - 1, top + 2, right, bottom - 2))
        inset = 2 + RING_BAND + 1
        hole = (left + inset, top + inset, right - inset, lobby[1] - RING_BAND - 1)
    else:  # block: a loop round the lobby, spokes out to it
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
    return rects, lobby, trimmable, hole


HELD = -2  # a ring's hole, kept from the rooms and never built


class _Canvas:
    """Who owns each tile while a deck is packed: -1 nobody, HELD a ring's
    hole, 0 the skeleton, k > 0 the k-th room; with a running count of what
    is taken (floor or held) for O(1) checks."""

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
                    run += row[x] != -1
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
    canvas: _Canvas,
    slot: _Slot,
    view: bool,
    cx: int,
    cy: int,
    load: dict[Any, int],
    spread: float = 2.5,
    region: Any = None,
) -> float:
    """Lower is better: sharing walls with rooms already built, near the
    middle, a corridor side (or `region`) with fewer rooms in it yet
    (`spread` for each; live, every room went to one side and the rest of
    the station was bare corridor), and for a room that looks out, nothing
    built beyond it."""
    x0, y0, x1, y1 = slot.room
    shared = 0
    for x in range(x0 - 2, x1 + 3):
        shared += canvas.owner(x, y0 - 2) > 0
        shared += canvas.owner(x, y1 + 2) > 0
    for y in range(y0 - 1, y1 + 2):
        shared += canvas.owner(x0 - 2, y) > 0
        shared += canvas.owner(x1 + 2, y) > 0
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    crowd = load.get(slot.edge if region is None else region, 0)
    score = -1.0 * shared + 0.06 * math.hypot(mx - cx, my - cy) + spread * crowd
    if view:
        # Out to the canvas's edge: a ring's hole is no place for an airlock
        # or a docking bay.
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
) -> tuple[_Canvas, list[DeckRoom], list[Rect], Rect, list[str], str] | None:
    """One deck packed on the shared skeleton, or None when a room won't fit
    (the caller grows the skeleton). A block's or a ring's deck of few rooms
    (a spire's top, a small engineering deck) is a cross of short corridors
    round the same lobby, so its lifts still line up, not the whole loop
    with nothing on it. The gaps the rooms leave inside are filled
    (`_fill`), a ring's hole apart."""
    width, height = size
    cx, cy = width // 2, height // 2
    hx, hy = _hub(shape, s, cx, cy)
    need = _need(brief_deck)
    if shape in ("block", "ring") and need < 0.6 * most:
        shape, s = "cross", max(6, round(need / 8 * 1.2) + 2)
        rects, lobby, trimmable, hole = _skeleton(shape, s, hx, hy)
    else:
        rects, lobby, trimmable, hole = _skeleton(shape, s, cx, cy)
    # Rooms gather near the lobby, a ring's round its middle (its inner side).
    gx, gy = (cx, cy) if shape == "ring" else (hx, hy)
    # A ring's rooms all round it, not bunched on one side by their walls:
    # counted by where they stand round its middle (which quarter, inside
    # the loop or out), not by the corridor they are on (the
    # ends of the loop's sides took the rooms the side itself was spared).
    spread = RING_SPREAD if shape == "ring" else 2.5
    loop = rects[0][0], rects[0][1], rects[0][2], rects[1][3]

    def region(slot: _Slot) -> Any:
        if shape != "ring":
            return None
        x0, y0, x1, y1 = slot.room
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        quarter = round(math.atan2(my - cy, mx - cx) / (math.pi / 2)) % 4
        inside = loop[0] < mx < loop[2] and loop[1] < my < loop[3]
        return quarter, inside

    canvas = _Canvas(width, height)
    if hole is not None:
        canvas.fill(hole, HELD)
    canvas.fill(lobby, 0)
    for rect in rects:
        canvas.fill(rect, 0)
    rng = random.Random(seed * 31 + brief_deck.index)
    order = sorted(
        brief_deck.rooms,
        key=lambda r: (
            not (shape == "spine" and r.use in ("command", "engineering")),
            -_SIZE_ORDER.index(r.size),
            r.use not in OUT_USES,
            r.name,
        ),
    )
    rooms: list[DeckRoom] = []
    load: dict[Any, int] = {}
    edges = [lobby, *rects]
    # The corridors an airlock may stand off: the main ones, never a branch
    # or the lobby (live, two locks stood on a freighter's lobby and a third
    # at the end of a branch, a knot of stubs the judges faulted).
    mains = {1, 2} if shape == "spine" else {1, 2, 3, 4}
    signature = shape == "spine" and any(r.use == "command" for r in brief_deck.rooms)
    fore_end = rects[1][2] if shape == "spine" else width

    def pull(use: str, name: str, slot: _Slot) -> float:
        """A ship's bridge at its bow (the fore corridor's end) and its engines
        at the stern (the aft one's); a station's command near its core."""
        mx = (slot.room[0] + slot.room[2]) / 2
        if shape == "spine":
            if use == "command":
                return -9.0 if slot.edge == (2, "e") else 0.2 * (width - mx)
            if use == "engineering":
                return -6.0 if slot.edge == (1, "w") else 0.12 * mx
            if signature and (slot.edge == (2, "e") or slot.room[2] > fore_end - NECK):
                return 9.0  # the bow is the bridge's, and the neck before it
            return 0.0
        if use == "command":
            my = (slot.room[1] + slot.room[3]) / 2
            return 0.3 * math.hypot(mx - hx, my - hy)
        return 0.0

    def lock_score(name: str, slot: _Slot) -> float | None:
        if slot.edge[0] not in mains:
            return None
        x0, y0, x1, y1 = slot.room
        dx, dy = slot.out
        if dx == 0:
            beyond = (x0 - 1, 0, x1 + 1, y0 - 2) if dy < 0 else (x0 - 1, y1 + 2, x1 + 1, height - 1)
        else:
            beyond = (0, y0 - 1, x0 - 2, y1 + 1) if dx < 0 else (x1 + 2, y0 - 1, width - 1, y1 + 1)
        bx0, by0, bx1, by1 = beyond
        clipped = (max(0, bx0), max(0, by0), min(width - 1, bx1), min(height - 1, by1))
        if clipped[0] <= clipped[2] and clipped[1] <= clipped[3] and canvas.floor_in(clipped):
            return None  # it must open onto space
        score = 0.0
        sx, sy = slot.door
        score += 3.0 * sum(
            max(abs(sx - ox), abs(sy - oy)) <= 2 for ox, oy in (r.door for r in rooms)
        )
        lower = name.lower()
        if shape == "spine":
            score += 0.4 * abs((x0 + x1) / 2 - cx)  # amidships
            side = slot.edge[1]
            if "port" in lower:
                score += 0 if side == "n" else 4
            elif "starboard" in lower:
                score += 0 if side == "s" else 4
        return score

    # A ship's rooms either side of its spine (live, the shared walls drew
    # all of a freighter's rooms below it, and its hull was lopsided).
    sides = {"n": 0, "s": 0}

    def place(name: str, use: str, w: int, d: int, view: bool, outward: bool) -> bool:
        best: tuple[float, _Slot] | None = None
        for shape_wd in ((w, d), (d + 1, max(2, w - 1))) if w > d + 2 else ((w, d),):
            for slot in _slots(canvas, edges, *shape_wd):
                if use == "airlock":
                    extra = lock_score(name, slot)
                    if extra is None:
                        continue
                else:
                    extra = pull(use, name, slot)
                    side = slot.edge[1]
                    if shape == "spine" and side in sides:
                        extra += 4.0 * (sides[side] - sides["s" if side == "n" else "n"])
                score = _score(canvas, slot, outward, gx, gy, load, spread, region(slot))
                score += extra
                score += rng.random() * 0.01
                if best is None or score < best[0]:
                    best = (score, slot)
        if best is None:
            return False
        slot = best[1]
        canvas.fill(slot.room, len(rooms) + 1)
        where = slot.edge if region(slot) is None else region(slot)
        load[where] = load.get(where, 0) + 1
        if slot.edge[1] in sides and use != "airlock":
            sides[slot.edge[1]] += 1
        rooms.append(DeckRoom(name, use, slot.room, slot.door, view, slot.out))
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
        inner_at_start = (x0 > hx) if horizontal else (y0 > hy)  # the end nearer the lobby
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
    _fill(canvas, rooms)
    return canvas, rooms, rects, lobby, notes, shape


def _gaps(canvas: _Canvas) -> list[set[tuple[int, int]]]:
    """Every patch of space inside the deck that no way out to open space
    reaches (four ways, past the walls round what is built), but a ring's
    held hole."""
    w, h = canvas.w, canvas.h
    own = canvas.own
    built = [[own[y][x] >= 0 for x in range(w)] for y in range(h)]
    near = [[False] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            if built[y][x]:
                for yy in range(max(0, y - 1), min(h, y + 2)):
                    for xx in range(max(0, x - 1), min(w, x + 2)):
                        near[yy][xx] = True
    space = {(x, y) for y in range(h) for x in range(w) if not near[y][x]}
    seen: set[tuple[int, int]] = set()
    gaps: list[set[tuple[int, int]]] = []
    for start in sorted(space, key=lambda t: (t[1], t[0])):
        if start in seen:
            continue
        piece, todo, edge = set(), [start], False
        seen.add(start)
        while todo:
            x, y = todo.pop()
            piece.add((x, y))
            edge = edge or x in (0, w - 1) or y in (0, h - 1)
            for t in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if t in space and t not in seen:
                    seen.add(t)
                    todo.append(t)
        if not edge and not any(own[y][x] == HELD for x, y in piece):
            gaps.append(piece)
    return gaps


def _largest_rect(tiles: set[tuple[int, int]]) -> Rect | None:
    """The largest rectangle wholly within `tiles` with both sides at least
    FILL_MIN, or None."""
    if not tiles:
        return None
    x0 = min(x for x, _ in tiles)
    x1 = max(x for x, _ in tiles)
    y0 = min(y for _, y in tiles)
    y1 = max(y for _, y in tiles)
    heights = [0] * (x1 - x0 + 1)
    best: Rect | None = None
    best_area = 0
    for y in range(y0, y1 + 1):
        for i in range(len(heights)):
            heights[i] = heights[i] + 1 if (x0 + i, y) in tiles else 0
        for i in range(len(heights)):
            tall = heights[i]
            for j in range(i, len(heights)):
                tall = min(tall, heights[j])
                if tall < FILL_MIN:
                    break
                area = tall * (j - i + 1)
                if j - i + 1 >= FILL_MIN and area > best_area:
                    best_area, best = area, (x0 + i, y - tall + 1, x0 + j, y)
    return best


def _cuts(lo: int, hi: int) -> list[tuple[int, int]]:
    """lo..hi cut into runs of at most FILL_MAX with a wall between."""
    n = hi - lo + 1
    k = max(1, math.ceil((n + 1) / (FILL_MAX + 1)))
    base, extra = divmod(n - (k - 1), k)
    out, start = [], lo
    for i in range(k):
        end = start + base + (i < extra) - 1
        out.append((start, end))
        start = end + 2
    return out


def _fill(canvas: _Canvas, rooms: list[DeckRoom]) -> None:
    """The gaps inside a deck filled with service rooms (the author, Oct 17
    2026: a station is rooms and machinery all through, or, a ring, rooms
    round one hole; a courtyard of stars between a block's spokes read as a
    room of space). Each gap is carved into rooms of at most FILL_MAX a side,
    a wall between, each with a door onto a corridor, else onto a service
    room or a room beside it; what is too small for a room or gets no door
    is left for the hull's plating. Named by the caller (`FILLERS`)."""
    pieces: list[Rect] = []
    for gap in _gaps(canvas):
        left = set(gap)
        while (rect := _largest_rect(left)) is not None:
            x0, y0, x1, y1 = rect
            pieces += [(a, c, b, d) for a, b in _cuts(x0, x1) for c, d in _cuts(y0, y1)]
            left -= {(x, y) for y in range(y0 - 1, y1 + 2) for x in range(x0 - 1, x1 + 2)}
    if not pieces:
        return
    doors = {r.door for r in rooms}
    service: set[int] = set()

    def door_for(box: Rect, worst: int) -> tuple[tuple[int, int], tuple[int, int]] | None:
        x0, y0, x1, y1 = box
        sides = (
            ([(x, y0 - 1, x, y0 - 2) for x in range(x0, x1 + 1)], (0, 1), (x0 + x1) / 2),
            ([(x, y1 + 1, x, y1 + 2) for x in range(x0, x1 + 1)], (0, -1), (x0 + x1) / 2),
            ([(x0 - 1, y, x0 - 2, y) for y in range(y0, y1 + 1)], (1, 0), (y0 + y1) / 2),
            ([(x1 + 1, y, x1 + 2, y) for y in range(y0, y1 + 1)], (-1, 0), (y0 + y1) / 2),
        )
        best: tuple[tuple[int, float], tuple[int, int], tuple[int, int]] | None = None
        for tiles, out, middle in sides:
            for wx, wy, bx, by in tiles:
                if canvas.owner(wx, wy) != -1 or (wx, wy) in doors:
                    continue
                other = canvas.owner(bx, by)
                if other == 0:
                    rank = 0
                elif other in service:
                    rank = 1
                elif other > 0 and rooms[other - 1].use != "airlock":
                    rank = 2
                else:
                    continue
                along = wx if out[1] else wy
                key = (rank, abs(along - middle))
                if rank <= worst and (best is None or key < best[0]):
                    best = (key, (wx, wy), out)
        return None if best is None else (best[1], best[2])

    def placed(box: Rect, worst: int) -> bool:
        found = door_for(box, worst)
        if found is None:
            return False
        door, out = found
        canvas.fill(box, len(rooms) + 1)
        service.add(len(rooms) + 1)
        doors.add(door)
        rooms.append(DeckRoom("", "storage", box, door, False, out, filler=True))
        return True

    # Corridors first, then a service room beside, then any room beside.
    waiting = pieces
    for worst in (0, 1, 2):
        progress = True
        while waiting and progress:
            later = [box for box in waiting if not placed(box, worst)]
            progress = len(later) < len(waiting)
            waiting = later


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


# --- The hull's mass and the set pieces ---------------------------------------------

Tile = tuple[int, int]


def _disc(r: int) -> list[Tile]:
    return [
        (dx, dy)
        for dy in range(-r, r + 1)
        for dx in range(-r, r + 1)
        if dx * dx + dy * dy <= r * r + r
    ]


def _dilate(tiles: set[Tile], r: int) -> set[Tile]:
    if r <= 0:
        return set(tiles)
    disc = _disc(r)
    return {(x + dx, y + dy) for x, y in tiles for dx, dy in disc}


def _erode(tiles: set[Tile], r: int) -> set[Tile]:
    if r <= 0:
        return set(tiles)
    disc = _disc(r)
    return {(x, y) for x, y in tiles if all((x + dx, y + dy) in tiles for dx, dy in disc)}


def _closed(tiles: set[Tile], r: int) -> set[Tile]:
    """Notches narrower than about 2r filled, a hole's corners rounded."""
    return _erode(_dilate(tiles, r), r)


def _built(rows: list[str]) -> set[Tile]:
    return {(x, y) for y, row in enumerate(rows) for x, ch in enumerate(row) if ch != " "}


def _ship(built: set[Tile], bridge: Rect | None, cy: int) -> dict[Tile, str]:
    """A ship's hull: as wide either side of its spine (rows cy-1 and cy) as
    its widest part there, widening from the bow and from the stern towards
    its middle and never narrowing in between; chamfered down to its bridge
    at the bow (east), or to a point past its last room when the bridge is on
    another deck; engines out of a flat stern (west)."""
    if not built:
        return {}
    xmin = min(x for x, _ in built)
    xmax = max(x for x, _ in built)
    north = dict.fromkeys(range(xmin, xmax + 1), 0)
    south = dict.fromkeys(range(xmin, xmax + 1), 0)
    for x, y in built:
        if y < cy:
            north[x] = max(north[x], cy - 1 - y)
        else:
            south[x] = max(south[x], y - cy)

    def envelope(raw: dict[int, int]) -> dict[int, int]:
        smooth = {x: max(raw.get(k, 0) for k in range(x - 3, x + 4)) for x in raw}
        west: dict[int, int] = {}
        run = 0
        for x in range(xmin, xmax + 1):
            run = max(run, smooth[x])
            west[x] = run
        out: dict[int, int] = {}
        run = 0
        for x in range(xmax, xmin - 1, -1):
            run = max(run, smooth[x])
            out[x] = min(west[x], run) + RIM
        return out

    # Each side as wide as its own rooms, and at least near the other's:
    # a hull a little lopsided, never a slab of plating on the bare side.
    n_side, s_side = envelope(north), envelope(south)
    n_half = {x: max(n_side[x], round(0.5 * s_side[x])) for x in n_side}
    s_half = {x: max(s_side[x], round(0.5 * n_side[x])) for x in s_side}
    if bridge is not None:
        # Down to the bridge along the neck before it, 45 degrees; never
        # inside a room's own walls.
        bn, bs = cy - 1 - (bridge[1] - 1), bridge[3] + 1 - cy
        for x in n_half:
            chamfer = 2 * max(0, bridge[0] - 1 - x)  # 2 to 1: a bow that reads as one
            n_half[x] = max(min(n_half[x], bn + chamfer), north[x] + RIM)
            s_half[x] = max(min(s_half[x], bs + chamfer), south[x] + RIM)
    else:
        for k in range(1, max(n_half[xmax], s_half[xmax]) + 1):
            n_half[xmax + k] = max(0, n_half[xmax] - k)
            s_half[xmax + k] = max(0, s_half[xmax] - k)
    stern = xmin - RIM
    n_half[stern], s_half[stern] = n_half[xmin], s_half[xmin]
    out: dict[Tile, str] = {}
    for x in n_half:
        for y in range(cy - 1 - n_half[x], cy + s_half[x] + 1):
            out[(x, y)] = HULL
    if bridge is not None:  # nothing past the bridge's own walls
        for t in [t for t in out if t[0] > bridge[2] + 1]:
            del out[t]
    # Engines: two or three nozzles out of the stern.
    top, bottom = cy - 1 - n_half[stern], cy + s_half[stern]
    e = (bottom - top) // 2
    count = 3 if e >= 7 else 2
    for k in range(count):
        y = round(top + (bottom - top) * (k + 1) / (count + 1))
        for dy in (-1, 0) if e < 7 else (-1, 0, 1):
            for x in range(stern - 3, stern):
                out[(x, y + dy)] = ENGINE
    return out


def _rock(core: set[Tile], seed: int) -> dict[Tile, str]:
    """An asteroid round what is dug into it: rock a few tiles out from it,
    further in some directions than others."""
    if not core:
        return {}
    rng = random.Random(seed)
    xs = [x for x, _ in core]
    ys = [y for _, y in core]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    waves = [(rng.uniform(0, math.tau), rng.uniform(0.6, 1.4), n) for n in (2, 3, 5)]

    def reach(x: int, y: int) -> float:
        a = math.atan2(y - my, x - mx)
        return 4.0 + sum(amp * math.sin(n * a + ph) for ph, amp, n in waves)

    out: dict[Tile, str] = {}
    frontier = set(core)
    seen = set(core)
    for step in range(1, 9):
        nxt: set[Tile] = set()
        for x, y in frontier:
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                t = (x + dx, y + dy)
                if t in seen:
                    continue
                seen.add(t)
                if step <= reach(*t):
                    nxt.add(t)
                    out[t] = ROCK
        frontier = nxt
    for t in core:
        out[t] = ROCK
    return out


def _masses(
    rows_by_deck: dict[int, list[str]],
    shapes: dict[int, str],
    hull: str,
    bridges: dict[int, Rect | None],
    cy: int,
    seed: int,
) -> dict[int, dict[Tile, str]]:
    """Each deck's hull mass, from what that deck builds (the play view shows
    one deck at a time; one outline for every deck put plating where the
    others had rooms): a station's closed round its rooms with a rim, a
    ship's shaped to a bow and a stern, an outpost's rock; never where the
    deck builds or where its windows look out."""
    out: dict[int, dict[Tile, str]] = {}
    for index, rows in rows_by_deck.items():
        own = _built(rows)
        if hull == "rock":
            marks = _rock(_closed(own, CLOSE), seed + index)
        elif hull == "ship" and shapes[index] == "spine":
            marks = _ship(_closed(own, CLOSE), bridges.get(index), cy)
        else:
            marks = {t: HULL for t in _station(own, hole=shapes[index] == "ring")}
        out[index] = {t: m for t, m in marks.items() if t not in own}
    return out


def _station(built: set[Tile], hole: bool = False) -> set[Tile]:
    """A station's hull: a rounded body round all it builds (a superellipse,
    so a ring reads as a wheel and a cross as a module, not a box), closed
    round its rooms. A ring's one hole in the middle is left open to space,
    its corners rounded; any other gap is plating (the author: a patch of
    stars inside a station reads as a room of space)."""
    if not built:
        return set()
    xs = [x for x, _ in built]
    ys = [y for _, y in built]
    mx, my = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    a, b = max(1.0, (max(xs) - min(xs)) / 2), max(1.0, (max(ys) - min(ys)) / 2)
    n = 3.0
    k = max((abs(x - mx) / a) ** n + (abs(y - my) / b) ** n for x, y in built) ** (1 / n)
    ax, by = a * k + RIM + 0.5, b * k + RIM + 0.5
    body = {
        (x, y)
        for y in range(int(my - by) - 1, int(my + by) + 2)
        for x in range(int(mx - ax) - 1, int(mx + ax) + 2)
        if (abs(x - mx) / ax) ** n + (abs(y - my) / by) ** n <= 1
    }
    closed = _closed(built, 2)
    # The courtyards: what the deck encloses and doesn't build, reached from
    # nowhere outside it.
    x0, x1, y0, y1 = min(xs) - 1, max(xs) + 1, min(ys) - 1, max(ys) + 1
    outside: set[Tile] = set()
    todo = [(x, y) for x in range(x0, x1 + 1) for y in (y0, y1)]
    todo += [(x, y) for y in range(y0, y1 + 1) for x in (x0, x1)]
    while todo:
        t = todo.pop()
        if t in outside or t in built or not (x0 <= t[0] <= x1 and y0 <= t[1] <= y1):
            continue
        outside.add(t)
        x, y = t
        todo += [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
    yards = {
        (x, y)
        for y in range(y0, y1 + 1)
        for x in range(x0, x1 + 1)
        if (x, y) not in built and (x, y) not in outside and (x, y) not in closed
    }
    # The hole: the largest yard, when it is large enough to read as open
    # space; the rest is plating.
    kept: set[Tile] = set()
    if hole:
        for piece in _pieces_of(yards):
            if len(piece) >= 12 and len(piece) > len(kept):
                kept = piece
    return (body | _dilate(closed, RIM)) - kept


def _pieces_of(tiles: set[Tile]) -> list[set[Tile]]:
    """`tiles` in its four-way connected pieces."""
    out: list[set[Tile]] = []
    seen: set[Tile] = set()
    for start in sorted(tiles, key=lambda t: (t[1], t[0])):
        if start in seen:
            continue
        piece, todo = set(), [start]
        seen.add(start)
        while todo:
            x, y = todo.pop()
            piece.add((x, y))
            for t in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if t in tiles and t not in seen:
                    seen.add(t)
                    todo.append(t)
        out.append(piece)
    return out


def _bays(rows: list[str], rooms: list[DeckRoom], depth: int = 2) -> set[Tile]:
    """The space before the outer wall of each room that must look out or
    that ships use (a viewport, a bay's doors, a lock's hatch), kept clear
    of the hull's mass: two tiles out, the wall's length. The wall is the
    one it faces out of, or for a room that looks out the side with the most
    windows when that one has none."""
    h, w = len(rows), len(rows[0]) if rows else 0

    def at(x: int, y: int) -> str:
        return rows[y][x] if 0 <= x < w and 0 <= y < h else " "

    clear: set[Tile] = set()
    for room in rooms:
        if room.use not in OUT_USES:
            continue
        x0, y0, x1, y1 = room.box
        walls = {
            (0, -1): [(x, y0 - 1) for x in range(x0 - 1, x1 + 2)],
            (0, 1): [(x, y1 + 1) for x in range(x0 - 1, x1 + 2)],
            (-1, 0): [(x0 - 1, y) for y in range(y0 - 1, y1 + 2)],
            (1, 0): [(x1 + 1, y) for y in range(y0 - 1, y1 + 2)],
        }
        windows = {
            side: sum(at(x, y) in "W+" and at(x + side[0], y + side[1]) == " " for x, y in tiles)
            for side, tiles in walls.items()
        }
        side = room.out
        if room.use in VIEW_USES and not windows[side]:
            side = max(windows, key=lambda k: windows[k])
            if not windows[side]:
                continue
        dx, dy = side
        for x, y in walls[side]:
            for k in range(1, depth + 1):
                t = (x + dx * k, y + dy * k)
                if at(*t) != " ":
                    break  # out as far as the deck's own next wall
                clear.add(t)
    return clear


def _pockets(rows: list[str], mass: dict[Tile, str], hole: bool = False) -> set[Tile]:
    """The space left shut in on a deck (a gap a window looks into, a bay
    stopped by the deck's own walls): every patch the frame's edge doesn't
    reach past the walls and the mass, but a ring's one hole (the largest,
    when it reads as open space). The hull's mass takes them."""
    h, w = len(rows), len(rows[0]) if rows else 0
    space = {(x, y) for y in range(h) for x in range(w) if rows[y][x] == " " and (x, y) not in mass}
    shut: list[set[Tile]] = []
    for piece in _pieces_of(space):
        if not any(x in (0, w - 1) or y in (0, h - 1) for x, y in piece):
            shut.append(piece)
    if hole and shut:
        largest = max(shut, key=len)
        if len(largest) >= 12:
            shut.remove(largest)
    return {t for piece in shut for t in piece}


def _attached(mass: dict[Tile, str], built: set[Tile]) -> dict[Tile, str]:
    """The mass with any scrap a bay cut off it taken away: each piece kept
    only where it touches what the deck builds."""
    out: dict[Tile, str] = {}
    seen: set[Tile] = set()
    for start in mass:
        if start in seen:
            continue
        piece, todo = [], [start]
        seen.add(start)
        while todo:
            x, y = todo.pop()
            piece.append((x, y))
            for t in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if t in mass and t not in seen:
                    seen.add(t)
                    todo.append(t)
        touches = any(
            (x + dx, y + dy) in built for x, y in piece for dx in (-1, 0, 1) for dy in (-1, 0, 1)
        )
        if touches:
            out.update((t, mass[t]) for t in piece)
    return out


def _settled(rows: list[str], mass: dict[Tile, str]) -> list[str]:
    """Windows that look onto the hull's mass made plain wall: a viewport
    onto plating is no viewport."""
    h, w = len(rows), len(rows[0]) if rows else 0
    grid = [list(r) for r in rows]

    def open_space(x: int, y: int) -> bool:
        return (not (0 <= x < w and 0 <= y < h) or rows[y][x] == " ") and (x, y) not in mass

    for y in range(h):
        for x in range(w):
            if grid[y][x] == "W" and not any(
                open_space(x + dx, y + dy) for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0))
            ):
                grid[y][x] = "#"
    return ["".join(r) for r in grid]


def _pieces(room: DeckRoom) -> list[Piece]:
    """The set pieces of a room that make it what it is: a reactor or a drive
    core in engineering, a mine's shaft, a garden's beds, a shuttle on a
    hangar's pad, a holo table in a lab, a bridge's consoles along the wall
    it looks out of with the command chair before them. Each keeps a
    walkway round it."""
    x0, y0, x1, y1 = room.box
    rw, rh = x1 - x0 + 1, y1 - y0 + 1

    def centred(w: int, h: int) -> tuple[int, int, int, int]:
        bx, by = x0 + (rw - w) // 2, y0 + (rh - h) // 2
        return (bx, by, bx + w - 1, by + h - 1)

    if SHAFT_WORDS.search(room.name) and rw >= 5 and rh >= 4:
        n = 3 if min(rw, rh) >= 6 else 2
        return [Piece("the mine shaft", centred(n, n), "shaft")]
    if room.use == "engineering" and rw >= 5 and rh >= 4:
        n = 3 if min(rw, rh) >= 7 else 2
        name = "the drive core" if REACTOR_WORDS.search(room.name) else "the reactor core"
        return [Piece(name, centred(n, n), "reactor")]
    if room.use == "garden" and rw >= 5 and rh >= 4:
        beds: list[Piece] = []
        if rw >= rh:
            for y in range(y0 + 1, y1, 2):
                beds.append(Piece("a planter bed", (x0 + 1, y, x1 - 1, y), "beds"))
        else:
            for x in range(x0 + 1, x1, 2):
                beds.append(Piece("a planter bed", (x, y0 + 1, x, y1 - 1), "beds"))
        return beds
    if room.use == "hangar" and rw >= 7 and rh >= 5:
        return [Piece(SHUTTLE, centred(4, 3))]
    if room.use == "science" and rw >= 5 and rh >= 4:
        return [Piece(HOLO, centred(2, 2))]
    if room.use == "command" and rw >= 4 and rh >= 3:
        dx, dy = room.out
        out: list[Piece] = []
        if dy:
            row = y0 if dy < 0 else y1
            for x in range(x0 + 1, x1 - 1, 3):
                out.append(Piece(CONSOLE, (x, row, min(x + 1, x1 - 1), row)))
            chair_y = row + (2 if dy < 0 else -2)
            if y0 <= chair_y <= y1:
                out.append(Piece(CHAIR, ((x0 + x1) // 2, chair_y, (x0 + x1) // 2, chair_y)))
        else:
            col = x0 if dx < 0 else x1
            for y in range(y0 + 1, y1 - 1, 3):
                out.append(Piece(CONSOLE, (col, y, col, min(y + 1, y1 - 1))))
            chair_x = col + (2 if dx < 0 else -2)
            if x0 <= chair_x <= x1:
                out.append(Piece(CHAIR, (chair_x, (y0 + y1) // 2, chair_x, (y0 + y1) // 2)))
        return out
    return []


def generate(brief: Brief, place_id: str, *, hull: str = "", ring: bool = True) -> DeckPlan:
    """Every deck of the brief laid out on one skeleton, its hull's mass round
    it and its set pieces in its rooms, cropped to one frame so a tile is the
    same spot on every deck. `hull` overrides the brief's ("rock" when the
    place's own words say it is dug into an asteroid); `ring` False makes a
    brief's ring a block (the author, Oct 17 2026: a ring only when it is
    one; live, GLM 5.3 Flash answered "ring" for one station in three whose
    words named no shape)."""
    hull = hull if hull in HULLS else brief.hull
    if brief.shape == "ring" and not ring:
        brief = Brief("block", brief.decks, brief.hull)
    seed = zlib.crc32(place_id.encode("utf-8"))
    need = max(_need(d) for d in brief.decks)
    s = max(8, round(need / (10 if brief.shape == "ring" else 8) * 1.2) + 3)
    for _attempt in range(6):
        span = 2 * s + 2 * max(SIZES["huge"][1], 8) + 12 + 16
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
    hx, hy = _hub(brief.shape, s, cx, cy)
    lifts = [(hx - 1, hy), (hx, hy)]
    # The service rooms named, each name once on the station.
    taken = {r.name.lower() for d in brief.decks for r in d.rooms}
    spare = (f"{name} {n + 1}" if n else name for n in range(1000) for name in FILLERS)
    for deck in brief.decks:
        for room in packed[deck.index][1]:
            if room.filler:
                room.name = next(n for n in spare if n.lower() not in taken)
                taken.add(room.name.lower())
    built: dict[int, tuple[list[str], list[DeckRoom]]] = {}
    shapes: dict[int, str] = {}
    bridges: dict[int, Rect | None] = {}
    dropped: list[str] = []
    for deck in brief.decks:
        canvas, rooms, rects, lobby, notes, shape = packed[deck.index]
        built[deck.index] = (_rows(canvas, rooms, lifts, rects), rooms)
        shapes[deck.index] = shape
        fore = [r for r in rooms if r.use == "command" and r.box[0] > cx and r.out == (1, 0)]
        bridges[deck.index] = fore[0].box if fore else None
        dropped += notes
    masses = _masses({i: rows for i, (rows, _r) in built.items()}, shapes, hull, bridges, cy, seed)
    for index, (rows, rooms) in list(built.items()):
        # A bay runs out to open space: a lounge's viewports and a lock's
        # hatch at the hull's surface, not onto a slot in the plating (cut
        # two tiles deep, a ship's locks opened onto boxes of stars).
        bays = _bays(rows, rooms, depth=max(width, height))
        kept = {t: m for t, m in masses[index].items() if t not in bays or m == ENGINE}
        mass = _attached(kept, _built(rows))
        fill = ROCK if hull == "rock" else HULL
        mass.update((t, fill) for t in _pockets(rows, mass, hole=shapes[index] == "ring"))
        masses[index] = mass
        built[index] = (_settled(rows, mass), rooms)
    # One frame for every deck: what any deck builds and its hull, a tile of
    # space round it (a ship's engines a few more, for their glow).
    xs: list[int] = []
    ys: list[int] = []
    for index, (rows, _rooms) in built.items():
        for y, row in enumerate(rows):
            for x, ch in enumerate(row):
                if ch != " ":
                    xs.append(x)
                    ys.append(y)
        for x, y in masses.get(index, {}):
            xs.append(x)
            ys.append(y)
    glow = 4 if any(m == ENGINE for ms in masses.values() for m in ms.values()) else 0
    fx0, fy0 = max(0, min(xs) - 1 - glow), max(0, min(ys) - 1)
    fx1, fy1 = min(width - 1, max(xs) + 1), min(height - 1, max(ys) + 1)
    decks: dict[int, Deck] = {}
    for deck in brief.decks:
        rows, rooms = built[deck.index]
        cropped = [row[fx0 : fx1 + 1] for row in rows[fy0 : fy1 + 1]]
        moved = []
        for r in rooms:
            room = DeckRoom(
                r.name,
                r.use,
                (r.box[0] - fx0, r.box[1] - fy0, r.box[2] - fx0, r.box[3] - fy0),
                (r.door[0] - fx0, r.door[1] - fy0),
                r.view,
                r.out,
                filler=r.filler,
            )
            room.pieces = _pieces(room)
            moved.append(room)
        mass = masses.get(deck.index, {})
        hull_rows = [
            "".join(mass.get((x, y), " ") for x in range(fx0, fx1 + 1)) for y in range(fy0, fy1 + 1)
        ]
        labels = {
            r.name: [(r.box[0] + r.box[2]) // 2, (r.box[1] + r.box[3]) // 2]
            for r in moved
            if r.use != "airlock"
        }
        decks[deck.index] = Deck(deck.index, deck.name, cropped, moved, labels, hull_rows)
    return DeckPlan(
        brief.shape,
        decks,
        [(x - fx0, y - fy0) for x, y in lifts],
        dropped=dropped,
        hull=hull,
    )
