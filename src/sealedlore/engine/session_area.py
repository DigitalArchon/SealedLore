"""Map-first play's side of the session (engine/area.py, models/area.py;
SealedLoreDocs design/map-first.md). A mixin for `StorySession`.

Every story that keeps a map is played on it (the author, Oct 16 2026: all
map stories move to map-first). Where people stand is a snapshot on the
nodes (`NodeMeta.area`); walking between turns is the story's
(`Story.area_live`) until the next turn is sent, which carries it: the
turn's snapshot, and a map delta putting the author's character where they
walked to, so the read and the art see it as any move.

The program keeps the clock (the author's ruling): walking within an area
takes little time, travelling between areas the way's distance, and with a
plot the walking moves the plot's clock (`walk_into_plot`), so travel
passes its days. The storyteller is never told the day; it is told how far
the character walked since the last passage, and where everyone stands.

Nobody moves on their own (the author's ruling, for now): the cast stand
where the story last put them until a passage moves them, and the read's
"at" may put them at one of the spots near the author's character.
"""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from sealedlore.engine.area import (
    WALK_M_PER_S,
    Area,
    LaidThing,
    _front,
    anchor_of,
    anchor_tile,
    anchors_near,
    apply_things,
    area_of,
    area_rect,
    bare,
    berth,
    beside_box,
    build_area,
    build_level,
    build_window,
    carried_by,
    clamped_tile,
    exit_side,
    front_of,
    in_earshot,
    inside_door,
    lay_things,
    metres_per_tile,
    now_words,
    path,
    point_tile,
    render_positions,
    room_words,
    sees,
    spot_in_words,
    stairs_landing,
    stand_near,
    thing_tile,
    tile_point,
    walk_metres,
    walk_seconds,
    walked_words,
    where_words,
)
from sealedlore.engine.chronicle_read import stated_skip
from sealedlore.engine.furnish import FURNISH_MAX_TOKENS, furnish_messages, parse_furnishing
from sealedlore.engine.image_variants import fitting_model
from sealedlore.engine.levels import level_name, parse_level, place_of_area
from sealedlore.engine.map_edit import apply_tile_edits
from sealedlore.engine.map_presence import MAP_HAS_THEM, reconcile_scene
from sealedlore.engine.marks import (
    chain_words,
    compass,
    footprint,
    mark_token,
    marks_in,
    nearest_place,
    place_at,
)
from sealedlore.engine.regions import designed_point, designed_spot, into_place
from sealedlore.engine.session_world_art import _today, job_words
from sealedlore.engine.takeable import fixed_thing, same_thing, the, thing_class
from sealedlore.engine.tile_draw import (
    DESIGNER_MODEL,
    RUN_LIMIT,
    SPRITE_SIZE,
    TEXTURE_SIZE,
    ground_jobs,
    item_jobs,
    parse_pick,
    parse_tile_words,
    pick_messages,
    roof_jobs,
    style_words,
    tile_jobs,
    tile_set_price,
    tileset_messages,
    walker_jobs,
    world_words,
)
from sealedlore.engine.tile_sets import (
    OUTDOOR_SPRITES,
    SPRITES,
    TEXTURES,
    TileSetInfo,
    choose_set,
    item_slugs,
    own_key,
    own_set_key,
    own_set_name,
    slug,
)
from sealedlore.engine.validators import locate_quote
from sealedlore.engine.world import (
    _NOTHING_SAID,
    _chain,
    _find_thing,
    _names_place,
    applied,
    find_place,
    key,
    unknown_for,
)
from sealedlore.engine.world_art import (
    ground_of,
    has_inside,
    is_window,
    outside_part,
    parse_window,
    window_id,
    window_rect,
)
from sealedlore.engine.world_layout import (
    CHILD_SHARE,
    KIND_SIZE,
    describe_metres,
    moves,
    position,
    size_of,
)
from sealedlore.engine.world_pictures import MAP_PICTURES_DIR, PictureJob, looks_of
from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.area import (
    CARRY_VERBS,
    RECORD_VERBS,
    AreaState,
    LiveArea,
    MapAction,
    Mark,
    Spot,
    ThingRecord,
)
from sealedlore.models.config import on_nanogpt
from sealedlore.models.node import DIRECTOR_SPEAKER_ID, NARRATOR_SPEAKER_ID, Node, Usage
from sealedlore.models.scene import SceneState
from sealedlore.models.world import (
    MapPicture,
    OwnTileSet,
    WorldArt,
    WorldDelta,
    WorldMap,
    WorldPlace,
)
from sealedlore.providers.base import ChatProvider, ChatRequest, ProviderError
from sealedlore.providers.images import ImageClient, build_image_payload, data_url, loggable_payload
from sealedlore.providers.wire import without_pictures
from sealedlore.storage import tile_sets as shipped
from sealedlore.storage.area_cache import load_edge_cache, save_edge_cache
from sealedlore.storage.image_meta import MetadataError, picture_kind, strip_metadata
from sealedlore.storage.images import MEDIA_TYPES

# Seconds taken going through a door.
DOOR_SECONDS = 3
# A way between areas winds: the straight line times this.
WINDING = 1.25
# Give to…: how far a thing can be handed over (beside them, or this far).
GIVE_REACH_M = 4.0
# Someone placed with the author's character stands this close to them.
BESIDE = 3
# How far from a building's door someone "at" it may stand before they are
# stood at it again.
AT_A_PLACE = 8
# Tiles from a door's front, outside, that count as at the door (shown at it
# from inside: `at_the_door`).
AT_THE_DOOR = 2.5
MAX_READ_MINUTES = 7 * 24 * 60
# Where the clock starts when the opening's read gives no time it can use:
# morning, not the midnight a count from nothing meant.
DEFAULT_START_MINUTE = 9 * 60
_CLOCK_TIME = re.compile(r"^\s*(\d{1,2})[:.h](\d{2})\s*$")


def is_opening(before: Sequence[Node]) -> bool:
    """Whether the passage after `before` is the story's first."""
    return not any(n.kind == "assistant" for n in before)


def opening_minute(value: Any) -> int:
    """The minute of the day a read's "starts_at" names ("07:30", "19.00"),
    else DEFAULT_START_MINUTE."""
    found = _CLOCK_TIME.match(str(value or ""))
    if found is None:
        return DEFAULT_START_MINUTE
    hour, minute = int(found.group(1)), int(found.group(2))
    if hour > 23 or minute > 59:
        return DEFAULT_START_MINUTE
    return hour * 60 + minute


# Past an outdoor area's edge: how far straight on another area may start
# and still be walked into, the way a place must lie to be offered, and how
# many are.
EDGE_REACH = 120.0
EDGE_ARC = 60
EDGE_CHOICES = 8
# Landing past an edge: how far along the far edge, and how far in, a tile
# to stand on is looked for.
EDGE_LANDING_ALONG = 3
EDGE_LANDING_IN = 2
EDGE_STEPS = {"north": (0, 1), "south": (0, -1), "east": (1, 0), "west": (-1, 0)}


@dataclass
class TileAsset:
    """One asset of the tile set in use, as the editor lists it."""

    role: str
    label: str
    group: str
    # "the set's", "drawn for this story", "changed for this story".
    source: str
    # What it shows, in words: what a redraw is drawn from.
    words: str


@dataclass
class TileSetCard:
    """One tile set as the gallery shows it (gui/tile_set_gallery.py)."""

    # What `set_tile_set` takes: a shipped set's name, or "own:<key>".
    name: str
    title: str
    in_use: bool
    # Drawn for this story (may be removed), or shipped with SealedLore.
    own: bool
    style: str
    keywords: list[str]
    # Its assets by role, for the thumbnails.
    read: Any


# The roles a gallery card shows thumbnails of: textures, then sprites.
CARD_ROLES = (
    "floor",
    "out-ground",
    "out-road",
    "out-roof",
    "rug",
    "decor0",
    "out-tree0",
    "out-door",
)


ASSET_LABELS = {
    "floor": "Floor",
    "cap": "Wall tops",
    "face": "Wall faces",
    "out-ground": "Ground",
    "out-road": "Roads",
    "out-water": "Water",
    "out-pave": "Paving",
    "out-canopy": "Tree canopy",
    "out-roof": "Roofs",
    "out-wall": "Outer walls",
    "rug": "Rug",
    "decor0": "Decoration 1",
    "decor1": "Decoration 2",
    "decor2": "Decoration 3",
    "out-tree0": "Tree 1",
    "out-tree1": "Tree 2",
    "out-bush": "Bush",
    "out-rock": "Rock",
    "out-rubble": "Rubble",
    "out-fence": "Fence",
    "out-door": "Door",
}


# How far along an outdoor area's edge a journey looks for the road it
# comes in by, in tiles.
ARRIVAL_ROAD_REACH = 16
# The AI's choice of tile set (`start_tile_set_pick`): how much of the
# world's text it is shown, and the room for its short answer (reasoning
# models think first).
PICK_SETTING_LIMIT = 6000
PICK_MAX_TOKENS = 4000
# What a thing's state says became of it (the map's own things, and what the
# read found the passage did to one on the play view): used up or gone
# (off the floor), or broken (still there, drawn broken).
GONE = re.compile(
    r"\b(?:gone|eaten|drunk|used up|consumed|burnt up|burned up|vanished|lost|"
    r"taken away|removed)\b"
)
BROKEN = re.compile(
    r"\b(?:broken|smashed|shattered|wrecked|destroyed|ruined|cracked|splintered|"
    r"snapped|collapsed|burnt|burned)\b"
)
# What says a thing stayed where it was, in the read's state or its quote
# ("bolted to the bench, did not move"): the read listed such things with a
# spot or a place-word, and the code moved them (the replay, Oct 17 2026).
STAYED = re.compile(
    r"\b(?:did not|didn't|does not|doesn't|would not|wouldn't|won't|will not|could not|"
    r"couldn't|can't|cannot)\s+(?:move|budge|shift|give|slide|stir|go)\b|"
    r"\b(?:unmoved|immovable|unmovable|motionless)\b|\bnot (?:moved|an inch|a hair)\b|"
    r"\b(?:bolted|riveted|nailed|fixed|screwed) (?:to|down|through|fast|into|onto)\b|"
    r"\b(?:held|holding|holds|stays?|stayed|stuck|wedged) (?:fast|put)\b"
)
# A thing is moved on the map only when it went this far: an inch or a
# scrape is not a move (the judges' "budged").
MOVE_MIN_M = 1.5
# The things laid on built areas, kept while what they are laid from is the
# same (`_laid_things`).
LAID_KEPT = 32
# The widest tile a thing is laid on: drawn about three tiles tall, a thing
# on land at 10-40 m a tile stood as tall as a church (AfterDark, Oct 18
# 2026: a bottle in the fields outside the farmhouse). Coarser than this, it
# shows once its place has a plan of its own.
THING_TILE_MAX_M = 5.0
# The play view's things the map read is told of, the nearest first.
THINGS_TOLD = 30
# A moored vehicle's length on the play view at most, in tiles (drawn to its
# footprint: a ship at a quay's walking scale is still a big boat, its
# sprite not stretched past what it was drawn at), and its beam a third of
# that. It lies afloat when its words say it floats.
MOORED_MAX_TILES = 16
AFLOAT = re.compile(
    r"\b(?:boat|ship|skiff|barge|canoe|raft|ferry|galleon|sloop|schooner|brig|yacht|"
    r"dinghy|punt|junk|dhow|vessel|steamer|launch|lighter|keelboat|cog|longship|trawler)s?\b",
    re.IGNORECASE,
)


def world_key(world: WorldMap) -> tuple:
    """What settling reads of the map, as a key: who is where (and outside),
    each place's spot, size, parent and state, each thing's whereabouts.
    Dumping the whole map as JSON took 25 ms a call, 133 calls an open."""
    return (
        tuple(sorted(world.where.items())),
        tuple(sorted(world.outside)),
        world.start,
        tuple((p.id, p.x, p.y, p.size, p.inside, p.kind, p.state) for p in world.places.values()),
        tuple((t.id, t.at, t.carried_by, t.state) for t in world.things.values()),
    )


def _doors_of(area: Area) -> set[tuple[int, int]]:
    """The tiles people must not be stood on: the area's exits, its
    buildings' doors and the doorways between its rooms."""
    doors = set(area.exits) | {(e.x, e.y) for e in area.entrances}
    for d in area.doorways:
        doors |= set(d.tiles())
    return doors


class AreaRuntime:
    _area_cache: dict | None = None
    _people_kept: tuple[tuple, list[str]] | None = None
    _edge_memo: dict[str, tuple[list[str], list[str]]] | None = None
    # The settled state last computed, with what it was computed from
    # (`area_state`): the window asks where everyone stands on every refresh
    # (133 times opening Brasshaven, Oct 19 2026, 44 s of a 63 s open), and
    # the answer only changes when the leaf, the walking, the map or its art
    # does.
    _settled: tuple[tuple, AreaState] | None = None
    _asset_thread: threading.Thread | None = None
    _asset_client: ImageClient | None = None
    _asset_role: str | None = None
    # The roles queued after the one being drawn (several avatars at once),
    # under `_queue_lock()`; `_asset_closing` once the run takes no more.
    _asset_queue: list[tuple[str, str]] = []
    _asset_queue_lock: threading.Lock | None = None
    _asset_closing = False
    # The picture jobs of the tile run under way (`tile_art_jobs`'s keys).
    _tile_keys: set[str] = set()
    _tile_thread: threading.Thread | None = None
    _tile_client: ImageClient | None = None
    _tile_text: ChatProvider | None = None
    _tile_total = 0
    _tile_done = 0
    # A run was asked for while one was drawing (`map_art_wanted_again`).
    _tile_again = False
    # The cards whose portrait is being drawn now (by id; the window keeps
    # it): their avatar waits for it rather than being drawn from their
    # words first and again once it lands (live, six avatars paid twice).
    portraits_coming: frozenset[str] = frozenset()
    _own_set_thread: threading.Thread | None = None
    _own_set_client: ImageClient | None = None
    _own_set_total = 0
    _own_set_done = 0
    # The designer's call for a new own set (`start_tile_set_design`): its
    # thread, and its words (with a prompt per picture) or why it failed.
    _design_thread: threading.Thread | None = None
    _design_text: ChatProvider | None = None
    _design_words: dict[str, str] | None = None
    _design_error: str | None = None

    # --- what is played on ---------------------------------------------------

    @property
    def map_first(self) -> bool:
        """The story is played on its map: every story that keeps one."""
        return self.map_on

    def area_people(self) -> list[str]:
        """Who may stand on the map, by name: kept while the leaf, the cards
        and what the plot hides are the same (asked 387 times an open,
        2 ms each, Oct 19 2026)."""
        key = (
            self.story.active_leaf_id,
            self.story.chat,
            tuple((c.id, c.name) for c in self.cast),
            tuple((c.id, c.name) for c in self.supporting),
            tuple(sorted(self.hidden_ids())) if self.story.plot is not None else (),
        )
        kept = self._people_kept
        if kept is not None and kept[0] == key:
            return list(kept[1])
        names = [c.name for c in self.map_people()]
        self._people_kept = (key, names)
        return list(names)

    def tile_set_name(self, art: WorldArt | None = None) -> str:
        """The tile set the map is drawn in (`engine.tile_sets.choose_set`):
        a shipped set's name or "own:<key>"."""
        art = art or self._art_read()
        sets = {name: info for name, (info, _folder) in shipped.shipped_sets().items()}
        looks = [
            ground_of(layout.spec) for states in art.layouts.values() for layout in states.values()
        ]
        own = [key for key, found in art.tile_sets.items() if found.assets]
        return choose_set(art.tile_set, self._setting_words(), looks, sets, own)

    def _setting_words(self) -> str:
        """What a tile set is chosen by: the whole of the world's text and
        where the story starts (a chat's system prompt), not the art's short
        setting. Live, a snowbound fur-trading post's first lines named none
        of the sets' words, and the burnt-out wasteland's set (first by name)
        drew it; its world text named winter and cabins further on."""
        if self.story.chat:
            return self.story.chat_prompt or ""
        start = self.story.setup.starting_scene
        parts = [self.story.world_bible or "", start.location or "", start.situation or ""]
        return "\n".join(p for p in parts if p)

    def tile_set(self, art: WorldArt | None = None) -> tuple[TileSetInfo, Any]:
        """(the set's manifest, a reader of its assets by role): the story's
        own drawings first (an item drawn for this story, its own set), then
        the shipped set's files."""
        art = art or self._art_read()
        name = self.tile_set_name(art)
        sets = shipped.shipped_sets()
        info, folder = sets.get(name, (None, None))
        own: dict[str, str] = {}
        found = art.own_set_for(name)
        if found is not None:
            info = _own_info(name, found[1])
            own = dict(found[1].assets)
        if info is None:
            info = TileSetInfo(name="")
        items = {p.subject_id: p.file for p in art.pictures if p.subject == "item"}
        # Textures drawn for the story on the fly: a roof of a material the
        # set has none of ("out-roof-stone", `roof_jobs`).
        textures = {p.subject_id: p.file for p in art.pictures if p.subject == "texture"}
        changed = dict(art.tile_overrides)
        reader = self.pictures.read

        def read(role: str) -> bytes | None:
            if role in changed:
                # The author's change to this one asset: theirs, or ("") the
                # set's own, past anything drawn for the story: the shipped
                # set's where it has one, else what the story drew (an own
                # set's floor, an item of the story's: once lost for good).
                if changed[role]:
                    data = reader(changed[role])
                    if data:
                        return data
                elif folder is not None:
                    data = shipped.read_asset(folder, role)
                    if data:
                        return data
            if role.startswith("items/") and role[6:] in items:
                data = reader(items[role[6:]])
                if data:
                    return data
            if role in textures:
                data = reader(textures[role])
                if data:
                    return data
            if role in own:
                data = reader(own[role])
                if data:
                    return data
            return shipped.read_asset(folder, role) if folder is not None else None

        def from_story(role: str) -> bool:
            """Whether the asset read is one drawn or chosen for the story
            (not pre-made to wrap, as the shipped sets' are)."""
            if changed.get(role):
                return True
            if changed.get(role) == "" and folder is not None and shipped.read_asset(folder, role):
                return False
            return (
                role in own or role in textures or (role.startswith("items/") and role[6:] in items)
            )

        found_names: list[str] = []

        def listed() -> list[str]:
            """Every thing the set and the story have a picture of, by slug:
            what a thing with none borrows from (`closest_item`)."""
            if found_names:
                return found_names
            names = set(items)
            names.update(r[6:] for r in own if r.startswith("items/"))
            if folder is not None:
                names.update(r[6:] for r in shipped.asset_names(folder, "items"))
            found_names.extend(sorted(names))
            return found_names

        read.from_story = from_story  # type: ignore[attr-defined]
        read.items = listed  # type: ignore[attr-defined]
        return info, read

    def generic_people(self) -> list[str]:
        """The tile set's generic people, by role."""
        name = self.tile_set_name()
        found = shipped.shipped_sets().get(name)
        return shipped.asset_names(found[1], "people") if found else []

    def walker_file(self, name: str, art: WorldArt | None = None) -> str | None:
        """The file of a person's walking sprite, if one is drawn."""
        art = art or self._art_read()
        changed = art.tile_overrides.get(f"walker/{name}")
        if changed is not None:
            return changed or None  # "": the set's generic people
        found = art.picture_for("walker", name)
        return found.file if found is not None else None

    def avatar_due(self, name: str) -> bool:
        """The map will draw this person's avatar itself, under map pictures
        (`tile_art_jobs`): they have none, or one it drew from their words or
        from a portrait they no longer lead with. Not when the author chose
        it (drew it, chose a file or one drawn before, or the set's own
        figure), nor a sample's: a new portrait asks before drawing over
        those."""
        if not self.map_pictures_on:
            return False
        card = next(
            (c for c in (*self.visible_cast(), *self.visible_supporting()) if c.name == name),
            None,
        )
        portrait = card.reference_images[0].file if card and card.reference_images else None
        return not _avatar_stands(self._art_read(), name, portrait)

    def walker_asset(self, name: str) -> tuple[bytes | None, str]:
        """A person's avatar on the map as the Cast page shows it: its file,
        or None when a stand-in walks for them, and where it came from (as
        `TileAsset.source`; "" for none)."""
        art = self._art_read()
        file = self.walker_file(name, art)
        data = self.pictures.read(file) if file else None
        if data is None:
            return None, ""
        changed = art.tile_overrides.get(f"walker/{name}")
        return data, "changed for this story" if changed else "drawn for this story"

    def tile_style(self, art: WorldArt | None = None) -> str:
        """The pixel art's style in a few words, for every picture drawn for
        the map: the tile set's own (`tile_draw.style_words`)."""
        art = art or self._art_read()
        info, _read = self.tile_set(art)
        return style_words(self._set_words(art, info))

    def _set_words(self, art: WorldArt, info: TileSetInfo) -> dict[str, str]:
        """The words every prompt names the world and style by: the set in
        use's (a shipped manifest's, or the own set's, which `tile_set` put
        on its info); an own set's words only while it is the one in use
        (they leaked into a shipped set's prompts; the second review)."""
        return dict(info.words)

    def area(
        self,
        area_id: str | None,
        world: WorldMap | None = None,
        art: WorldArt | None = None,
        *,
        things: Mapping[str, ThingRecord] | None = None,
    ) -> Area | None:
        """The area of `area_id`, a place's plan or a window of open
        country (engine/area.py `build_window`), built from the map at the
        leaf (kept while nothing it is built from changes), with what became
        of its things laid over it (`apply_things`: `things`, the records of
        the state in hand, else those live or at the leaf)."""
        world = world if world is not None else self.world()
        art = art if art is not None else self._art_read()
        built = self._built_area(area_id, world, art)
        if built is None:
            return None
        records = things if things is not None else self._things_now()
        laid = lay_things(built, self._laid_things(built, world, art, records))
        return apply_things(laid, records) if records else laid

    def _laid_things(
        self, area: Area, world: WorldMap, art: WorldArt, records: Mapping[str, ThingRecord]
    ) -> list[LaidThing]:
        """The map's things in this area that no record puts anywhere (what a
        read or a plot file put at a place): each laid near where its place
        is in the area (a building's door, a room's middle, a label), else
        near the way in, on a tile of its own (the author, Oct 17 2026:
        things the story adds must show on the play view; they were on the
        map's list only, and never drawn). Kept while the same."""
        found = []
        for t in sorted(world.things.values(), key=lambda t: t.id):
            if area.tile_m > THING_TILE_MAX_M:
                break  # a vehicle is drawn to its size; a thing would tower
            if t.carried_by or not t.at or GONE.search(t.state.lower()):
                continue
            record = records.get(t.id)
            if record is not None and (record.state in ("taken", "gone") or record.spot):
                continue  # where its record says (`apply_things`)
            found.append(t)
        # The vehicles docked here, laid first, whatever else there is.
        docked = [] if area.indoor else self._docked(area, world, art)
        if not found and not docked:
            return []
        cache_key = (
            area.id,
            id(area),
            tuple((t.id, t.name, t.at, t.state) for t in found),
            tuple((v.id, v.name, v.inside, v.size, has_inside(art, v)) for v in docked),
        )
        cache = self.__dict__.setdefault("_laid_cache", {})
        kept = cache.get(cache_key)
        if kept is not None:
            return kept
        taken: set[tuple[int, int]] = set()
        out: list[LaidThing] = self._moored(area, world, art, docked, taken)
        for t in found:
            if area_of(world, art, t.at) != area.id or _twin(area, t.name, world) is not None:
                continue  # elsewhere, or drawn here already (a plan's object of its name)
            near = anchor_of(area, world, t.at) or self._way_in(area)
            lies = designed_spot(art, t.id) if area.window else None
            if lies is not None:
                near = point_tile(world, area, *lies) or near  # where the designer put it
            x, y = thing_tile(area, near, taken)
            taken.add((x, y))
            broken = BROKEN.search(t.state.lower()) is not None
            out.append(LaidThing(id=t.id, name=t.name, x=x, y=y, broken=broken))
        if len(cache) > LAID_KEPT:
            cache.clear()
        cache[cache_key] = out
        return out

    def _docked(self, area: Area, world: WorldMap, art: WorldArt) -> list[WorldPlace]:
        """The vehicles the map has docked at a place in this area."""
        return [
            v
            for v in sorted(world.places.values(), key=lambda p: p.id)
            if v.kind == "vehicle"
            and v.inside in world.places
            and area_of(world, art, v.inside) == area.id
        ]

    def _moored(
        self,
        area: Area,
        world: WorldMap,
        art: WorldArt,
        docked: list[WorldPlace],
        taken: set[tuple[int, int]],
    ) -> list[LaidThing]:
        """The vehicles docked in this area (a barge made fast at the quay, a
        cart in the yard), laid where they dock: a boat afloat by the shore
        (`berth`), else on the ground; as long as they are, up to
        `MOORED_MAX_TILES`, a third as wide; their names on the tile beside
        them nearest where they dock, so whoever is aboard stands there, and
        one with an inside of its own boarded there (the author, Oct 18 2026:
        a moored vehicle is drawn where it docks; the bargeman and his barge
        were invisible on the quay)."""
        out: list[LaidThing] = []
        for v in docked:
            near = anchor_of(area, world, v.inside) or self._spot_in(world, area, v.inside)
            if near is None:
                near = self._way_in(area)
            tiles = round(size_of(v) / max(area.tile_m, 0.1))
            w = max(1, min(tiles, MOORED_MAX_TILES, area.width // 2))
            h = max(1, min(round(w / 3), area.height // 3))
            afloat = AFLOAT.search(f"{v.name} {v.what}") is not None
            spot = berth(area, near, w, h, taken) if afloat else None
            if spot is None:
                spot = thing_tile(area, near, taken, w=w, h=h)
            x, y = spot
            taken |= {(x + dx, y + dy) for dx in range(w) for dy in range(h)}
            beside = beside_box(area, (x, y, w, h), near, taken | area.blocked())
            out.append(
                LaidThing(
                    id=v.id,
                    name=v.name,
                    x=x,
                    y=y,
                    w=w,
                    h=h,
                    vehicle=True,
                    label_at=beside,
                    boards=has_inside(art, v),
                )
            )
        return out

    def _things_now(self) -> Mapping[str, ThingRecord]:
        """The records live, else at the leaf (never through `settle`, which
        asks for areas itself)."""
        live = self._live()
        state = live if live is not None else self._snapshot_on(self.path())
        return state.things if state is not None else {}

    def built_area(self, area_id: str | None) -> Area | None:
        """The area as its plan and the author's own tiles make it, before
        anything became of its things: their places as the plan put them."""
        return self._built_area(area_id)

    def _built_area(
        self, area_id: str | None, world: WorldMap | None = None, art: WorldArt | None = None
    ) -> Area | None:
        """The area as built from its plan, with the author's own tiles laid
        over it (Edit Mode, engine/map_edit.py), kept while the same."""
        art = art if art is not None else self._art_read()
        base = self._base_area(area_id, world, art)
        edits = art.tile_edits.get(area_id or "")
        if base is None or edits is None or not edits.tiles:
            return base
        cache = self.__dict__.setdefault("_edited_cache", {})
        cache_key = (id(base), tuple(edits.size), tuple(sorted(edits.tiles.items())))
        found = cache.get(cache_key)
        if found is None or found[0] is not base:
            if len(cache) > 24:
                cache.clear()
            found = (base, apply_tile_edits(base, edits))
            cache[cache_key] = found
        return found[1]

    def _base_area(
        self, area_id: str | None, world: WorldMap | None = None, art: WorldArt | None = None
    ) -> Area | None:
        """The area as its plan builds it, before the author's own tiles."""
        if area_id is None:
            return None
        world = world if world is not None else self.world()
        art = art if art is not None else self._art_read()
        if is_window(area_id):
            return self._window(area_id, world, art)
        parsed = parse_level(area_id)
        level = parsed[1] if parsed is not None else 0
        place = world.places.get(parsed[0] if parsed is not None else area_id)
        if place is None:
            return None
        layout = art.layout_shown(place.id, place.state)
        if layout is None:
            return None
        if level and layout.level(level) is None:
            return None
        plan = art.furnishing_for(area_id, place.state)
        children = tuple(
            sorted(
                (c.id, c.name, c.x, c.y, c.size, c.kind)
                for c in world.places.values()
                if c.inside == place.id
            )
        )
        inner = tuple(
            sorted(
                c.id
                for c in world.places.values()
                if c.inside == place.id and art.layout_shown(c.id, c.state) is not None
            )
        )
        cache_key = (
            place.id,
            place.state,
            layout.made_at,
            len(layout.rows),
            # A level arriving puts stairs on the plan (engine/levels.py).
            tuple((lv.index, lv.made_at) for lv in layout.levels),
            level,
            plan.made_at if plan is not None else "",
            self.story.defaults.map_density,
            children,
            inner,
            tuple(self.area_people()),
        )
        if self._area_cache is None:
            self._area_cache = {}
        found = self._area_cache.get(cache_key)
        if found is None:
            build = build_area if not level else build_level
            args = (place.id,) if not level else (place.id, level)
            found = build(
                world,
                art,
                *args,
                density=self.story.defaults.map_density,
                people=self.area_people(),
            )
            if len(self._area_cache) > 160:  # one per window of open country round a long walk
                self._area_cache.clear()
            self._area_cache[cache_key] = found
        return found

    def _window(self, area_id: str, world: WorldMap, art: WorldArt) -> Area | None:
        """A window of open country, kept while the land under it, the
        places in it and the people are the same."""
        parsed = parse_window(area_id)
        if parsed is None:
            return None
        # What a window is built from, as a key: the land (every patch, as
        # drawn), the map's places and the people. Looking the patch up and
        # scanning the places for every lookup cost 8 ms, 217 lookups a
        # settle with 39 people on 27 regions (Oct 19 2026).
        cache_key = (
            area_id,
            tuple((p.id, p.made_at, len(p.rows), p.ground) for p in art.terrain),
            self._art_version,
            world_key(world),
            tuple(sorted(art.layouts)),
            tuple(self.area_people()),
        )
        if self._area_cache is None:
            self._area_cache = {}
        found = self._area_cache.get(cache_key)
        if found is None:
            memo = self._edge_cache()
            before = len(memo) if memo is not None else 0
            found = build_window(world, art, area_id, people=self.area_people(), edge_memo=memo)
            if memo is not None and len(memo) > before:
                self._save_edge_cache(memo)
            if len(self._area_cache) > 160:  # one per window of open country round a long walk
                self._area_cache.clear()
            self._area_cache[cache_key] = found
        return found

    def _edge_cache(self) -> dict[str, tuple[list[str], list[str]]] | None:
        """The story's kept edge paths (storage/area_cache.py), read once;
        kept in memory only for a memory-only chat."""
        if self._edge_memo is None:
            self._edge_memo = {} if self.memory_only else load_edge_cache(self.story.id, self.root)
        return self._edge_memo

    def _save_edge_cache(self, memo: dict[str, tuple[list[str], list[str]]]) -> None:
        if self.memory_only or not self.pictures.alive():
            return
        try:
            save_edge_cache(self.story.id, memo, self.root)
        except OSError:
            pass  # a cache: the map is the same without it

    # --- where everyone stands -------------------------------------------------

    def _snapshot_on(self, nodes: Sequence[Node]) -> AreaState | None:
        for node in reversed(nodes):
            if node.meta.area is not None:
                return node.meta.area
        return None

    def _live(self) -> AreaState | None:
        live = self.story.area_live
        if live is None:
            return None
        if live.leaf_id != self.story.active_leaf_id or (
            live.held is not None and live.held != self.held_name()
        ):
            # A moved leaf (a retake, a branch) or another character's
            # walking: dropped, not kept to come back on a switch back.
            self.story.area_live = None
            return None
        return live.state

    def restore_area(self, node: Node) -> None:
        """Take back this turn: where everyone stood as the turn carried it
        (the walking and travel before it, applied) is live again at the
        leaf the turn comes off, so the turn sent again carries it again.
        It used to be lost with the node, and the same turn moved nobody."""
        if not self.map_first or node.meta.area is None:
            return
        state = node.meta.area.model_copy(deep=True)
        if state.action is not None:
            # Applied already: its time never counted twice, its destination
            # (a place with no plan yet has no spot to stand on) kept.
            state.action = state.action.model_copy(update={"seconds": 0})
        state.marks = []
        # What the turn itself did to a thing (Take, Destroy, Eat, Drop,
        # Give) goes with it: the records before the turn stand, and the
        # turn's own come back when it is sent again with the menu's words,
        # not when it is rewritten without them.
        before = self._snapshot_on(self._path_to(node.parent_id)) if node.parent_id else None
        state.things = dict(before.things) if before is not None else {}
        self.story.area_live = LiveArea(
            leaf_id=node.parent_id,
            held=self.map_held_name(node.meta.controlled_character_id),
            state=state,
        )

    def area_state(self) -> AreaState:
        """Where everyone stands now: the walking since the last turn, else
        the snapshot at the leaf, settled onto the map as it stands. Kept
        between calls while nothing it is settled from has changed: the
        leaf, who is held, the live walking or the snapshot, the map as
        folded, and the art (its version, bumped on every save)."""
        live = self._live()
        state = live if live is not None else self._snapshot_on(self.path())
        world = self.world()
        art = self.world_art
        key = (
            self.story.active_leaf_id,
            self.held_name(),
            live is None,
            state.model_dump_json() if state is not None else "",
            world_key(world),
            self._art_version,
            len(art.layouts),
            len(art.terrain),
            len(art.regions),
            # Whoever the scene has here stands with the author's character.
            tuple(self._present_on_map()),
            tuple(self.area_people()),
            self.story.defaults.map_density,
        )
        kept = self._settled
        if kept is not None and kept[0] == key:
            return kept[1].model_copy(deep=True)
        state = state.model_copy(deep=True) if state is not None else AreaState()
        if live is None:
            state.walked, state.walked_from, state.walked_words, state.action = 0, "", "", None
        settled = self.settle(state, world, self._art_read())
        self._settled = (key, settled)
        return settled.model_copy(deep=True)

    def held_name(self) -> str | None:
        return self.map_held_name(self.story.held_character_id)

    def settle(
        self, state: AreaState, world: WorldMap, art: WorldArt, *, walked: bool = True
    ) -> AreaState:
        """Everyone on a tile of the area the map has them in: kept where
        they stand while that is still where the map puts them, else stood
        at their place's spot (a building's door, a room), beside the
        author's character when the map has them in the same place, or near
        the way in; in open country (a window of the land, engine/area.py)
        at their own point on the map. `walked`: the author's character's
        own walking stands as walked (the map catches up when the turn is
        sent); off, the place the map names wins (a read moved them)."""
        held = self.held_name()
        where = dict(world.where)
        if held is not None and held not in where and world.start is not None:
            where[held] = world.start
        if held in where:
            # Whoever the scene has here and the map hasn't placed is with the
            # author's character (live, Brasshaven's Silas stood in the
            # workshop's doorway and the map read never named him: he was
            # nowhere on the map, told to nobody, and Talk couldn't find him).
            for name in self._present_on_map():
                if name != held and name not in where:
                    where[name] = where[held]
        spots: dict[str, Spot] = {}
        order = ([held] if held in where else []) + [n for n in where if n != held]
        # The world designer's regions (engine/regions.py): a whole region is
        # no one's company, and those it put there are no one's companions.
        designed_regions = {r.place_id for r in art.regions.values()}
        for name in order:
            place_id = where[name]
            spot = state.spots.get(name)
            # Where they stand on the map, for the window of open country
            # there: their tile's point; with the author's character, theirs.
            point = self._spot_point(spot, world, art)
            if point is None and spot is None:
                point = designed_point(art, name)  # where the world designer put them
            # Moved by a read into a place in another region of the world
            # designer's grid: into it, not at their own point in the region
            # they left (engine/regions.py `into_place`).
            point = into_place(world, art, place_id, point)
            mine = spots.get(held or "")
            # Being in the same region of open country as the author's
            # character is not being in their company: a zombie at the far
            # fence stays where it is (the live test of the world designer,
            # Oct 18 2026, stood two beside John).
            wide = place_id in designed_regions or designed_point(art, name) is not None
            if point is None and name != held and place_id == where.get(held or "") and not wide:
                point = self._spot_point(mine, world, art)
            elif (
                name != held
                and mine is not None
                and place_id == where.get(held or "")
                and not wide
                and spot is not None
                and spot.area != mine.area
                and (here := self.area(mine.area, world, art)) is not None
                and here.window
            ):
                # With the author's character at a place in open country: on
                # their window, beside them, not by their own last spot (live,
                # Oct 18 2026: Tobias, walking the river road with Wren, stood
                # on the next window over, the road's spot on the line between).
                point = self._spot_point(mine, world, art)
            target = area_of(world, art, place_id, point)
            # Where they are stood: their place, or, outside a building, the
            # building (its door's front on the ground round it).
            stand_at = place_id
            outside = False
            if name in world.outside or outside_part(world, world.places.get(place_id)):
                # At a building but outside it (its door, porch, yard): on the
                # ground round it, at its door (`_stand` finds its front).
                around = self._outside_of(world, art, place_id, target, spot, point)
                if around is not None:
                    target, stand_at = around
                    outside = True
            # Stood on the land only until their place had a plan: there now.
            arrived = (
                spot is not None
                and spot.for_place == place_id
                and target is not None
                and target != spot.area
            )
            if (
                name == held
                and walked
                and spot is not None
                and not arrived
                and self.area(spot.area, world, art, things=state.things) is not None
            ):
                spots[name] = spot
                continue
            if target is None:
                continue
            area = self.area(target, world, art, things=state.things)
            if area is None:
                continue
            # On the land for a place that is no area of its own yet (no
            # plan): they go into it once it has one (`Spot.for_place`); and
            # someone outside a building stood on the land because the place
            # round it has no plan yet goes onto that plan once it has (the
            # lab test, Oct 18 2026: Maren, at an inn's stable yard before the
            # village was drawn, stayed on the open country after).
            waiting = (
                place_id
                if (area.window or (_has_an_inside(world, place_id) and not outside))
                and place_id not in (area.place_id, area.id)
                else ""
            )
            if (
                spot is not None
                and spot.area == target
                # The author's character stays where they walked; a read that
                # moved them (`walked` off) moves them like anyone (live, Oct
                # 18 2026: read at the north gate, Wren stayed at the counting
                # house's door across the town).
                and ((name == held and walked) or self._still_at(area, world, spot, stand_at))
            ):
                spots[name] = (
                    spot
                    if spot.for_place == waiting
                    else spot.model_copy(update={"for_place": waiting})
                )
                continue
            if (
                spot is not None
                and spot.area != target
                and place_of_area(spot.area) == target
                and self.area(spot.area, world, art) is not None
            ):
                spots[name] = spot  # on another level of the place: still in it
                continue
            near_me = (
                name != held
                and mine is not None
                and mine.area == target
                and place_id == where.get(held or "")
                and not wide
            )
            # On a window, where they stand on the map; but someone moved to
            # a place there with a spot of its own (not a road or a region)
            # goes to that spot: kept at their own point, Wren read outside
            # the map shop stayed 700 m off on the land (Oct 18 2026).
            moved_here = (
                spot is not None
                and spot.area == target
                and world.places.get(stand_at) is not None
                and world.places[stand_at].kind not in ("route", "region")
            )
            new = self._stand(
                world,
                art,
                target,
                stand_at,
                name,
                spots,
                mine if near_me else None,
                point=point if area.window and not moved_here else None,
            )
            if new is not None:
                if outside and new.for_place != waiting:
                    new = new.model_copy(update={"for_place": waiting})
                spots[name] = new
        # Nobody stands in fire or a wall a redrawn plan put under them
        # (kept there, every walk was refused; the second review).
        # Nor in a doorway: a walk into a door ends short of whoever stands
        # in it (live, Oct 18 2026: the reeve's clerk in the door, Ines
        # could not leave the hall).
        for name, spot in list(spots.items()):
            area = self.area(spot.area, world, art, things=state.things)
            if area is None:
                continue
            doors = _doors_of(area) if name != held else set()
            if area.walkable(spot.x, spot.y) and (spot.x, spot.y) not in doors:
                continue
            taken = {
                (s.x, s.y) for n, s in spots.items() if n != name and s.area == spot.area
            } | doors
            x, y = stand_near(area, (spot.x, spot.y), taken, seed=name)
            spots[name] = Spot(area=spot.area, x=x, y=y, for_place=spot.for_place)
        state.spots = spots
        return state

    def _outside_of(
        self,
        world: WorldMap,
        art: WorldArt,
        place_id: str,
        target: str | None,
        spot: Spot | None,
        point: tuple[float, float] | None,
    ) -> tuple[str, str] | None:
        """For someone the map has outside `place_id`: the area round the
        building they are outside of, and that building, when `target` is
        the building's own inside (the place itself, or a porch or doorway
        the read put inside it). Its parent's ground when it stands in one,
        else the window of open country it stands in (AfterDark, Oct 18
        2026: Suzuka knocking at a farmhouse alone on the land was stood
        inside it, and at its "porch overhang", which the read had put
        inside the farmhouse, in the far hall out of John's sight). None
        when they are not at a building's inside."""
        building_id = place_of_area(target)
        if building_id is None or building_id not in world.places:
            return None
        chain, at = [], world.places.get(place_id)
        while at is not None and at.id not in chain:
            chain.append(at.id)
            at = world.places.get(at.inside) if at.inside else None
        if building_id not in chain:
            return None
        building = world.places[building_id]
        if building.kind not in ("building", "room", "vehicle"):
            # The area is the ground round it (a toll bridge's, for a toll
            # house with no plan of its own yet): they stand on it, at the
            # house. Live (Oct 18 2026) Wren, at the toll house door, was
            # stood outside the whole bridge, out in open country.
            return None
        # Their own point, if they stand on the ground already; the
        # building's spot when they stood inside it.
        if spot is None or place_of_area(spot.area) == building_id:
            point = position(world, building_id)
        if building.inside:
            around = area_of(world, art, building.inside, point)
        elif point is not None:
            around = window_id(*window_rect(art, *point))
        else:
            around = None
        if around is None or place_of_area(around) == building_id:
            return None
        return around, building_id

    def at_the_door(self) -> list[tuple[str, int, int, int, int]]:
        """Who stands at a door of the building the author's character is
        inside, from outside: (name, the tile just beyond the door's wall,
        the door tile), in that building's area, so the play view shows them
        at the door as seen through it (AfterDark, Oct 18 2026: Suzuka
        knocking at the farmhouse door, John inside, was on no view at all:
        the play view shows one area, and she stood in the one round it)."""
        if not self.map_first or self.in_private:
            return []
        state = self.area_state()
        held = self.held_name()
        me = state.spots.get(held or "")
        area = self.area(me.area) if me is not None else None
        if area is None or not area.indoor or area.level or not area.exits:
            return []
        building = place_of_area(area.id)
        world = self.world()
        steps = {"north": (0, -1), "south": (0, 1), "west": (-1, 0), "east": (1, 0)}
        out: list[tuple[str, int, int, int, int]] = []
        used: dict[tuple[int, int], int] = {}
        for name, spot in state.spots.items():
            if name == held or spot.area == area.id:
                continue
            around = self.area(spot.area, world)
            if around is None or building not in around.buildings:
                continue
            box = around.buildings[building]
            doors = [e for e in around.entrances if e.place_id == building]
            near = [
                (math.hypot(spot.x - fx, spot.y - fy), door)
                for door in doors
                for fx, fy in (_front(door, box),)
            ]
            if not near:
                continue
            distance, door = min(near, key=lambda pair: pair[0])
            if distance > AT_THE_DOOR:
                continue
            side = door.side if door.side in steps else "south"
            exits = [e for e in area.exits if exit_side(area, *e) == side] or list(area.exits)
            ex, ey = exits[0]
            dx, dy = steps[side]
            k = used.get((ex, ey), 0)
            used[(ex, ey)] = k + 1
            # Side by side along the wall, the second to the right of the first.
            shift = (k + 1) // 2 * (1 if k % 2 else -1)
            bx, by = ex + dx + (shift if dy else 0), ey + dy + (shift if dx else 0)
            out.append((name, bx, by, ex, ey))
        return out

    def _present_on_map(self, scene: SceneState | None = None) -> list[str]:
        """The people the scene (the story's now, unless given) has here, by
        the names the map uses."""
        if self.story.chat:
            return []
        scene = scene if scene is not None else self.story.scene
        names = [c.name for c in self.visible_cast() if c.id in scene.present_character_ids]
        for other in scene.present_others:
            person = self._map_person(other)
            names.append(person or other)
        return list(dict.fromkeys(names))

    def _still_at(self, area: Area, world: WorldMap, spot: Spot, place_id: str) -> bool:
        anchor = anchor_of(area, world, place_id)
        if anchor is None and not area.indoor and place_id not in (area.place_id, area.id):
            anchor = self._spot_in(world, area, place_id)
        if anchor is None:
            return True
        return math.hypot(spot.x - anchor[0], spot.y - anchor[1]) <= AT_A_PLACE

    def _stand(
        self,
        world: WorldMap,
        art: WorldArt,
        area_id: str,
        place_id: str,
        name: str,
        spots: dict[str, Spot],
        beside: Spot | None,
        *,
        point: tuple[float, float] | None = None,
    ) -> Spot | None:
        """A tile for `name` in the area: beside `beside`, else at their
        place's spot in it (a door's front, a label), else, on a window of
        open country, at `point` (where they stand on the map), else near
        the way in."""
        area = self.area(area_id, world, art)
        if area is None:
            return None
        taken = {(s.x, s.y) for s in spots.values() if s.area == area_id} | _doors_of(area)
        if beside is not None:
            near = (beside.x, beside.y)
            x, y = stand_near(area, near, taken | {near}, seed=name, reach=BESIDE)
            return Spot(area=area_id, x=x, y=y)
        near = anchor_of(area, world, place_id)
        if near is None and area.window:
            at = point if point is not None else position(world, place_id)
            near = clamped_tile(world, area, *at) if at is not None else None
        if near is None and not area.indoor and place_id not in (area.place_id, area.id):
            # A place the plan doesn't mark but whose spot it covers (a cairn
            # standing on the plan drawn round a boundary stone): there.
            near = self._spot_in(world, area, place_id)
        if near is None:
            near = self._way_in(area)
        x, y = stand_near(area, near, taken, seed=name, reach=0 if area.window else 2)
        waiting = (area.window or _has_an_inside(world, place_id)) and place_id not in (
            area.place_id,
            area.id,
        )
        return Spot(area=area_id, x=x, y=y, for_place=place_id if waiting else "")

    @staticmethod
    def _spot_in(world: WorldMap, area: Area, place_id: str) -> tuple[int, int] | None:
        """The tile at a place's spot on the map, when the area covers it."""
        at = position(world, place_id)
        rect = area_rect(world, area)
        if at is None or rect is None:
            return None
        west, north, width, height = rect
        if not (west <= at[0] <= west + width and north - height <= at[1] <= north):
            return None
        return clamped_tile(world, area, *at)

    def _spot_point(
        self, spot: Spot | None, world: WorldMap, art: WorldArt
    ) -> tuple[float, float] | None:
        """Where a spot is on the map, in metres; None for none."""
        if spot is None:
            return None
        area = self.area(spot.area, world, art)
        return tile_point(world, area, spot.x, spot.y) if area is not None else None

    @staticmethod
    def _way_in(area: Area) -> tuple[int, int]:
        if area.level:
            # A level is come into by its stairs (engine/levels.py): a step
            # off the stairs nearest the plan's own level.
            stairs = sorted(area.stairs_entrances(), key=lambda e: e.side != "down")
            if stairs:
                return stairs_landing(area, (stairs[0].x, stairs[0].y))
        if area.indoor:
            inside = inside_door(area)
            if inside is not None:
                return inside
            if area.rooms:
                room = area.rooms[0]
                return int(room.cx), int(room.cy)
        # Outdoors: in front of a building's door, else at a label one can
        # stand on (the first label was a building's centre, under its roof;
        # the second review), else the middle.
        for entrance in area.entrances:
            box = area.buildings.get(entrance.place_id)
            if box is not None:
                fx, fy = _front(entrance, box)
                if area.walkable(fx, fy):
                    return fx, fy
        for x, y in area.labels.values():
            if area.walkable(x, y):
                return x, y
        return area.width // 2, area.height // 2

    def _set_live(self, state: AreaState) -> None:
        self.story.area_live = LiveArea(
            leaf_id=self.story.active_leaf_id, held=self.held_name(), state=state
        )
        self.save()
        self.mark_explored(state)  # a region walked into is out of the fog

    def here_place(self) -> str | None:
        """The place the author's character stands in now: the live spot's
        (walked, through a door, since the last turn), else the map's."""
        if not self.map_first or self.in_private:
            return None
        world = self.world()
        me = self.area_state().spots.get(self.held_name() or "")
        return self.place_of_spot(me, world) or world.where.get(self.held_name() or "")

    def place_of_spot(self, spot: Spot | None, world: WorldMap) -> str | None:
        """The place a spot is in, for the map's `where`: the area's place
        (a building's inside is that building); on a window of open country
        the place holding the window (a region), or None in no place (the
        map's `where` has no word for nowhere: it keeps the last place)."""
        if spot is None:
            return None
        if spot.area in world.places:
            return spot.area
        parsed = parse_level(spot.area)
        if parsed is not None and parsed[0] in world.places:
            return parsed[0]
        if is_window(spot.area):
            area = self.area(spot.area, world)
            if area is not None and area.place_id in world.places:
                return area.place_id
        return None

    # --- moving ----------------------------------------------------------------

    def walk_route(self, x: int, y: int, *, beside: bool = False) -> list[tuple[int, int]]:
        """The tiles the author's character walks to reach (x, y) in their
        area, or to the nearest free tile beside it (`beside`: a person or a
        thing). [] when there is no way."""
        state = self.area_state()
        held = self.held_name()
        me = state.spots.get(held or "")
        area = self.area(me.area if me else None)
        if me is None or area is None:
            return []
        others = {(s.x, s.y) for n, s in state.spots.items() if n != held and s.area == me.area}
        goal = (x, y)
        if beside or goal in others:  # never onto someone's tile
            goal = stand_near(area, goal, others | area.blocked() | {goal})
        return path(area, (me.x, me.y), goal, blocked=others)

    def walked(self, steps: Sequence[tuple[int, int]]) -> AreaState:
        """The author's character has walked these tiles: where they stand,
        and the clock, moved on (live until the next turn is sent)."""
        state = self.area_state()
        held = self.held_name()
        me = state.spots.get(held or "")
        area = self.area(me.area if me else None)
        if me is None or area is None or not steps:
            return state
        # A walk is this character's, in this area, from where they stand:
        # one finishing after another story or character was taken up, or
        # after a read moved them, is not (the review, Oct 16 2026).
        others = {(s.x, s.y) for n, s in state.spots.items() if n != held and s.area == me.area}
        if (
            tuple(steps[0]) != (me.x, me.y)
            or not all(area.walkable(x, y) for x, y in steps)
            or (len(steps) > 1 and tuple(steps[-1]) in others)
        ):
            return state
        seconds = walk_seconds(walk_metres(area, steps))
        x, y = steps[-1]
        state.spots[held] = Spot(area=me.area, x=x, y=y)
        self._add_time(state, seconds, area.name, area.name)
        self._set_live(state)
        return state

    def _add_time(self, state: AreaState, seconds: int, frm: str, to: str) -> None:
        state.seconds += seconds
        state.walked += seconds
        state.walked_from = state.walked_from or frm
        state.walked_words = walked_words(state.walked, state.walked_from, to)

    def door_here(self) -> tuple[str, str] | None:
        """(where it leads, its name) when the author's character stands in a
        door that leads into or out of a building."""
        state = self.area_state()
        held = self.held_name()
        me = state.spots.get(held or "")
        area = self.area(me.area if me else None)
        if me is None or area is None:
            return None
        world = self.world()
        entrance = area.entrance_at(me.x, me.y)
        if entrance is not None:
            return entrance.place_id, self.area_title(entrance.place_id, world)
        if area.indoor and (me.x, me.y) in area.exits and area.parent is not None:
            # Open country is named for the region it lies in, when it does.
            around = self.area(area.parent, world)
            holder = world.places.get(around.place_id) if around and around.window else None
            return area.parent, holder.name if holder else self.area_title(area.parent, world)
        return None

    def area_title(self, area_id: str, world: WorldMap | None = None) -> str:
        """An area's name for a message: a place's, a level's with its
        level ("The farmhouse, upper floor"), open country's."""
        world = world if world is not None else self.world()
        parsed = parse_level(area_id)
        place = world.places.get(parsed[0] if parsed is not None else area_id)
        if place is None:
            return "open country" if is_window(area_id) else area_id
        if parsed is None:
            return place.name
        layout = self._art_read().layout_shown(place.id, place.state)
        return f"{place.name}, {level_name(layout, parsed[1]).lower()}"

    def go_through(self) -> AreaState | None:
        """Through the door the author's character stands in: into the
        building, or out of it to stand outside its door; up or down the
        stairs they stand on, to a step off the stairs on the other level."""
        door = self.door_here()
        if door is None:
            return None
        state = self.area_state()
        held = self.held_name()
        me = state.spots[held]
        world, art = self.world(), self._art_read()
        here = self.area(me.area, world, art)
        there = self.area(door[0], world, art)
        if here is None or there is None:
            return None
        taken = {(s.x, s.y) for n, s in state.spots.items() if n != held and s.area == there.id}
        stairs = here.entrance_at(me.x, me.y)
        if stairs is not None and stairs.stairs:
            x, y = stairs_landing(there, (me.x, me.y), taken)
            state.spots[held] = Spot(area=there.id, x=x, y=y)
            self._add_time(
                state,
                DOOR_SECONDS,
                self.area_title(here.id, world),
                self.area_title(there.id, world),
            )
            self._set_live(state)
            return state
        if not here.indoor:
            entrance = here.entrance_at(me.x, me.y)
            spot = inside_door(there, entrance.side if entrance else None) or self._way_in(there)
        else:
            side = exit_side(here, me.x, me.y)
            spot = front_of(there, here.place_id, side)  # a moored vehicle's gangway too
            if spot is None:
                # The outside plan has no box for this building (a plan kept
                # as rows alone): outside it where the map has it, not at
                # the area's middle.
                building = world.places.get(here.place_id)
                point = (
                    point_tile(world, there, building.x, building.y)
                    if building is not None and building.x is not None and building.y is not None
                    else None
                )
                spot = point or self._way_in(there)
        x, y = stand_near(there, spot, taken)
        state.spots[held] = Spot(area=there.id, x=x, y=y)
        self._add_time(state, DOOR_SECONDS, here.name, there.name)
        self._set_live(state)
        return state

    def travel_choices(self) -> list[tuple[str, str, int]]:
        """(place id, name, seconds on foot) for each known place the
        author's character could set off for: the areas the map has drawn,
        and the known places with a spot of their own, nearest first."""
        world = self.world()
        art = self._art_read()
        state = self.area_state()
        me = state.spots.get(self.held_name() or "")
        here = me.area if me is not None else world.where.get(self.held_name() or "")
        if here is None:
            return []
        here = place_of_area(here) or here  # a level of a building is the building
        # From where they stand, not from the area's middle: the way's length
        # is what the clock moves by and the storyteller is told.
        start = self.held_point(state, world, art) or position(world, here)
        out = []
        # Not where they are already: this area, what is in it, and (inside a
        # building) the area outside it.
        outer = world.places[here].inside if here in world.places else None
        local = area_of(world, art, outer) if outer else None
        roots = {here} | ({local} if local else set())
        current = {
            p.id
            for p in world.places.values()
            if p.id in roots or roots & {c.id for c in self._containers_of(world, p.id)}
        }
        for place in world.places.values():
            if place.id in current or place.id not in world.seen:
                continue
            if place.kind in ("room", "route", "region"):
                continue
            target = area_of(world, art, place.id)
            if target is not None and target in current:
                continue
            spot = position(world, place.id)
            if start is None or spot is None:
                continue
            metres = math.hypot(spot[0] - start[0], spot[1] - start[1]) * WINDING
            if metres < 50:
                continue
            out.append((place.id, place.name, int(metres / WALK_M_PER_S)))
        out.sort(key=lambda item: item[2])
        return out

    @staticmethod
    def _containers_of(world: WorldMap, place_id: str) -> list:
        out = []
        seen: set[str] = set()
        current = world.places.get(place_id)
        while current is not None and current.id not in seen:
            seen.add(current.id)
            out.append(current)
            current = world.places.get(current.inside or "")
        return out

    def travel(self, place_id: str) -> str:
        """Set off for a place: the author's character is stood there, at the
        way in, and the clock moved on by the way's length on foot. The
        turn's text for the composer (the author's ruling: the menu fills
        the composer)."""
        world, art = self.world(), self._art_read()
        place = world.places.get(place_id)
        if place is None:
            return ""
        choices = {pid: seconds for pid, _n, seconds in self.travel_choices()}
        seconds = choices.get(place_id, 0)
        state = self.area_state()
        held = self.held_name()
        target = area_of(world, art, place_id)
        spot = None
        if target is not None:
            landing = self._arrival(world, art, target, held or "")
            spot = (
                Spot(area=target, x=landing[0], y=landing[1])
                if landing is not None
                else self._stand(world, art, target, place_id, held or "", dict(state.spots), None)
            )
        text = self.texts.fill("map.turn.travel", name=place.name)
        # Nothing moves until the turn is sent (`stamp_turn`): a turn
        # rewritten travels nowhere. A place not drawn yet: the turn's delta
        # says where they went, and the map draws it after the passage.
        state.action = MapAction(
            verb="travel",
            target=place.name,
            target_id=place_id,
            text=text,
            spot=spot,
            seconds=seconds,
        )
        self._set_live(state)
        return text

    def _arrival(
        self, world: WorldMap, art: WorldArt, area_id: str, held: str
    ) -> tuple[int, int] | None:
        """Where a journey to an outdoor area lands: at its edge toward where
        the traveller set out from, on the road or through the gate there
        when one comes in near it. Live (Oct 17 2026), a walk up the north
        road was told it ended "in Hellsville, outside The diner", and the
        storyteller wrote the barricade at the south gate. None for a
        building's inside, or a journey from within the area."""
        area = self.area(area_id, world, art)
        me = self.held_point()
        if area is None or area.indoor or me is None or point_tile(world, area, *me) is not None:
            return None
        edge = clamped_tile(world, area, *me)
        if edge is None:
            return None
        blocked = area.blocked()
        roads = [
            (math.hypot(x - edge[0], y - edge[1]), x, y)
            for y in range(
                max(0, edge[1] - ARRIVAL_ROAD_REACH),
                min(area.height, edge[1] + ARRIVAL_ROAD_REACH + 1),
            )
            for x in range(
                max(0, edge[0] - ARRIVAL_ROAD_REACH),
                min(area.width, edge[0] + ARRIVAL_ROAD_REACH + 1),
            )
            if area.rows[y][x] in "=+" and area.walkable(x, y, blocked)
        ]
        if roads:
            _d, x, y = min(roads)
            return x, y
        return stand_near(area, edge, set(), seed=held)

    def map_action(self, verb: str, target: str, target_id: str = "", *, to: str = "") -> str:
        """The turn a menu choice writes (`map.turn.<verb>`), kept with the
        live state so the storyteller is told what it means when it is sent
        as written (or added to). `to`: who a Give hands the thing to."""
        state = self.area_state()
        # Someone not named yet goes into the turn as the passage described
        # them ("I walk over to the teenage girl"), not by their label.
        unknowns = self.world().unknowns
        target, to = unknowns.get(target, target), unknowns.get(to, to)
        if verb in ("use", "take", "destroy", "eat", *CARRY_VERBS):
            target = bare(target)  # "the stove", not "the a stove"
        if verb == "give":
            text = self.texts.fill("map.turn.give", name=target, who=to)
        else:
            text = self.texts.fill(f"map.turn.{verb}", name=target)
        state.action = MapAction(
            verb=verb,  # type: ignore[arg-type]
            target=target,
            target_id=target_id,
            text=text,
            to=to,
        )
        self._set_live(state)
        return text

    # --- what became of the things ------------------------------------------------

    def carried_things(self, who: str | None = None) -> list[tuple[str, str]]:
        """What `who` (the author's character by default) carries: (the
        thing's placement id, its name), in the order taken."""
        who = who if who is not None else self.held_name()
        if not self.map_first or who is None:
            return []
        state = self.area_state()
        return [
            (pid, bare(r.name))
            for pid, r in state.things.items()
            if r.state == "taken" and r.by == who
        ]

    def people_in_reach(self) -> list[str]:
        """Who the author's character could hand something to: in their
        area, beside them or within `GIVE_REACH_M`."""
        state = self.area_state()
        held = self.held_name()
        me = state.spots.get(held or "")
        area = self.area(me.area if me else None, things=state.things)
        if me is None or area is None:
            return []
        out = []
        for name, spot in state.spots.items():
            if name == held or spot.area != me.area:
                continue
            dx, dy = abs(spot.x - me.x), abs(spot.y - me.y)
            if max(dx, dy) <= 1 or math.hypot(dx, dy) * area.tile_m <= GIVE_REACH_M:
                out.append(name)
        return out

    def _record_action(self, state: AreaState, user_node: Node, world: WorldMap) -> None:
        """The turn's Take, Destroy, Eat, Drop or Give, done (the author's
        ruling: in code, no model call; the menu choice the author sends is
        the fact): the record on the turn's snapshot, and the map's own
        thing, when it has one by that name, carried, broken or gone on the
        turn's delta (never by changing the folded map). Not on a failed
        roll: the attempt is the storyteller's to tell, and nothing changes
        hands."""
        action = state.action
        held = self.held_name()
        if action is None or action.verb not in RECORD_VERBS or held is None:
            return
        roll = user_node.meta.roll
        if roll is not None and roll.band in ("failure", "critical_failure"):
            return
        if not action.target_id:
            return
        name = action.target
        record: ThingRecord | None = None
        if action.verb == "take":
            record = ThingRecord(state="taken", name=name, by=held)
        elif action.verb == "give" and action.to:
            record = ThingRecord(state="taken", name=name, by=action.to)
        elif action.verb == "destroy":
            record = ThingRecord(state="broken", name=name)
        elif action.verb == "eat":
            record = ThingRecord(state="gone", name=name)
        elif action.verb == "drop":
            me = state.spots.get(held)
            area = self.area(me.area, world, things=state.things) if me is not None else None
            if me is None or area is None:
                return
            taken = {(s.x, s.y) for n, s in state.spots.items() if s.area == me.area}
            x, y = stand_near(area, (me.x, me.y), taken)
            record = ThingRecord(state="dropped", name=name, spot=Spot(area=me.area, x=x, y=y))
        if record is None:
            return
        state.things[action.target_id] = record
        thing = _find_thing(world, name)
        if thing is None:
            return  # the area's record is enough for a thing the map never listed
        copy = thing.model_copy(deep=True)
        if record.state == "taken":
            copy.carried_by, copy.at = record.by, None
        elif record.state == "broken":
            copy.state = "broken"
        elif record.state == "gone":
            copy.carried_by, copy.at, copy.state = None, None, "gone"
        elif record.state == "dropped":
            copy.carried_by = None
            copy.at = self.place_of_spot(record.spot, world) or world.where.get(held)
        delta = user_node.meta.world or WorldDelta()
        delta.things = [t for t in delta.things if t.id != copy.id] + [copy]
        user_node.meta.world = delta

    def people_here(self) -> list[tuple[str, Spot]]:
        state = self.area_state()
        me = state.spots.get(self.held_name() or "")
        if me is None:
            return []
        return [(n, s) for n, s in state.spots.items() if s.area == me.area]

    # --- turns -----------------------------------------------------------------

    def stamp_turn(self, user_node: Node) -> None:
        """The walking done since the last turn goes with this one: its
        snapshot, and a move to where the author's character walked to (the
        map's `where`), so the read, the fog and the art follow."""
        if not self.map_first or self.in_private:
            return
        self.pin_tile_set()
        live = self._live()
        # Every turn carries where everyone stands: walked since the last
        # turn, or as they were.
        state = live.model_copy(deep=True) if live is not None else self.area_state()
        if state.action is not None and state.action.text not in user_node.content:
            state.action = None  # the author wrote a turn of their own
        user_node.meta.area = state
        held = self.held_name()
        world = self.world()
        to = None
        action = state.action
        if action is not None and action.verb == "travel" and held is not None:
            # Set off, now that the turn goes: there, as far as the map has
            # drawn it, the way's length on the clock.
            here = state.spots.get(held)
            frm = self.area(here.area, world) if here is not None else None
            if action.spot is not None:
                state.spots[held] = action.spot
            else:
                state.spots.pop(held, None)
            self._add_time(state, action.seconds, frm.name if frm else "", action.target)
            to = action.target_id or None
        if self.story.plot is None and user_node.speaker_id in (
            DIRECTOR_SPEAKER_ID,
            NARRATOR_SPEAKER_ID,
        ):
            # "Three weeks pass": the author's own statement moves the
            # map's clock, as `_author_skip` moves the plot's.
            now = state.seconds // 60
            skip = stated_skip(user_node.content, now, next_time=True)
            if skip is not None and skip[0] > now:
                state.seconds += (skip[0] - now) * 60
        # The spots the turn marks, resolved on the map it was written on.
        state.marks = self.resolve_marks(user_node.content, world, self._art_read(), state)
        # What the turn does to a thing (Take, Destroy, Eat, Drop, Give).
        self._record_action(state, user_node, world)
        spot = state.spots.get(held or "")
        to = to or self.place_of_spot(spot, world)
        # Before any read has placed them, where the story starts (as `settle`).
        here = world.where.get(held or "", world.start)
        # The building they are outside of: the place itself, or the one its
        # doorstep, porch or yard belongs to (`outside_part`).
        here_place = world.places.get(here) if here else None
        part = here_place is not None and outside_part(world, here_place)
        building = here_place.inside if part else here
        at_door = held in world.outside or building != here
        inside_it = (
            spot is not None and building is not None and place_of_area(spot.area) == building
        )
        if (
            held in world.outside
            and spot is not None
            and not inside_it
            and here in world.places
            and world.places[here].inside == to
        ):
            # Outside a building, on the ground round it: still at it while
            # still at its door (the farm for the farmhouse's porch lost the
            # "just outside" the storyteller was told).
            around = self.area(spot.area, world)
            if around is not None and self._still_at(around, world, spot, here):
                to = here
        if (
            held is not None
            and held not in world.outside
            and spot is not None
            and here in world.places
            and (action is None or action.verb != "travel")
            and is_window(spot.area)
            and _has_an_inside(world, here)
        ):
            # Walked out of its door onto open country: at it, outside, while
            # still by its door (out into a farm's yard, they are at the farm).
            # AfterDark (Oct 18 2026): out of the farmhouse onto open country,
            # which is no place, the map still had John inside.
            around = self.area(spot.area, world)
            if (
                around is not None
                and here in around.buildings
                and self._still_at(around, world, spot, here)
            ):
                delta = user_node.meta.world or WorldDelta()
                delta.outside[held] = True
                user_node.meta.world = delta
                to = here
        if held is not None and at_door and inside_it:
            # Walked in from its door, or from its doorstep, porch or yard: in
            # it now (the lab test, Oct 18 2026: Maren, read on her doorstep,
            # walked in and was put back outside on the next turn).
            delta = user_node.meta.world or WorldDelta()
            delta.outside[held] = False
            if building != here:
                delta.where[held] = building
                to = building
            user_node.meta.world = delta
        elif held is not None and held in world.outside and to == world.where.get(held):
            if user_node.speaker_id == self.story.held_character_id and _GOES_IN.search(
                user_node.content
            ):
                # The turn takes them in ("I step inside"): in it, just
                # inside its door when its inside is drawn. Live (Oct 18
                # 2026) the map read gave no move (Wren was at the toll
                # house already, at its door), so she stayed outside talking
                # to Birch while Tobias, in the doorway, stood inside.
                delta = user_node.meta.world or WorldDelta()
                delta.outside[held] = False
                user_node.meta.world = delta
                inside = self.area(to, world) if to else None
                door = inside_door(inside) if inside is not None and inside.indoor else None
                if door is not None:
                    taken = {(s.x, s.y) for n, s in state.spots.items() if s.area == inside.id}
                    x, y = stand_near(inside, door, taken, seed=held, reach=2)
                    state.spots[held] = Spot(area=inside.id, x=x, y=y)
        if held is not None and to is not None and world.where.get(held) != to:
            area = area_of(world, self._art_read(), world.where.get(held))
            if area != to or to not in world.places:
                delta = user_node.meta.world or WorldDelta()
                delta.where[held] = to
                user_node.meta.world = delta
        self.story.area_live = None

    @staticmethod
    def walk_minutes(user_node: Node) -> int:
        """The minutes a turn's walking and travel took, whole."""
        state = user_node.meta.area
        if state is None or state.walked <= 0:
            return 0
        return max(0, int(round(state.walked / 60)))

    def walk_into_plot(self, user_node: Node, chronicle) -> int:
        """The minutes this turn's walking took, added to the plot's clock
        (as the author's skip is: the read counts on from there)."""
        minutes = AreaRuntime.walk_minutes(user_node)
        if minutes <= 0:
            return 0
        chronicle.minutes += minutes
        chronicle.elapsed += minutes
        return minutes

    def area_after_read(
        self,
        node: Node,
        reply: dict[str, Any],
        source: str,
        *,
        placements: Sequence[tuple[dict[str, Any], str]] = (),
        moves_fixed: bool = False,
    ) -> list[str]:
        """After the map read: everyone settled on the map the passage left,
        anyone it put at one of the spots near the author's character stood
        there, what the passage did to the things on the play view kept
        (`_things_after_read`: `placements` are the read's entries for
        things the play view has that the map's list hasn't, each with its
        placement id), and the clock moved on by the time the passage told
        (without a plot; with one, the plot's clock read keeps time). What
        it refused, for the log."""
        if not self.map_first:
            return []
        path = self._path_to(node.id)
        before = self._snapshot_on(path[:-1])
        state = before.model_copy(deep=True) if before is not None else AreaState()
        marks = list(state.marks)
        state.walked, state.walked_from, state.walked_words, state.action = 0, "", "", None
        state.marks = []
        world, art = self._world_on(path), self._art_read()
        held = self.held_name()
        # The read placed the author's character (with a quote): the place
        # it names wins over where they stand, and they are stood at its
        # spot; otherwise they stay where they walked to (a window of open
        # country has no word in the map's `where`, so a stale place there
        # must not pull them back).
        moved = held is not None and held in (node.meta.world.where if node.meta.world else {})
        # Someone named by this passage stands where they stood unnamed.
        for label, name in (node.meta.world.named if node.meta.world else {}).items():
            if label in state.spots:
                state.spots.setdefault(name, state.spots.pop(label))
        state = self.settle(state, world, art, walked=not moved)
        me = state.spots.get(held or "")
        # The things first, so someone the read stands "at the bench" stands
        # where the bench is now.
        refused = self._things_after_read(
            state,
            node,
            reply,
            source,
            world=world,
            before=self._world_on(path[:-1]),
            art=art,
            held=held,
            placements=placements,
            moves_fixed=moves_fixed,
            marks=marks,
        )
        at = reply.get("at") if isinstance(reply.get("at"), dict) else {}
        by_point = {(m.x, m.y): m for m in marks}
        # Only someone the read also placed (with a quote): `at` alone moved
        # people the passage left where they were (the second review).
        placed = {
            self._map_person(str(entry.get("who") or ""), world)
            for entry in (reply.get("where") or [])
            if isinstance(entry, dict)
        }
        for name, spot_name in list(at.items()):
            found = marks_in(str(spot_name or ""))
            mark = by_point.get((found[0].x, found[0].y)) if found else None
            person = self._map_person(str(name), world)
            if mark is None or person is None or person not in placed:
                continue
            at.pop(name)
            spot = self._mark_spot(mark, world, art)
            current = state.spots.get(person)
            if spot is None or current is None or current.area != spot.area:
                continue  # not where the map has them: settled at their place
            marked = self.area(spot.area, world, art, things=state.things)
            taken = {
                (s.x, s.y) for n, s in state.spots.items() if n != person and s.area == spot.area
            }
            x, y = stand_near(marked, (spot.x, spot.y), taken)  # type: ignore[arg-type]
            state.spots[person] = Spot(area=spot.area, x=x, y=y)
        area = self.area(me.area, world, art, things=state.things) if me is not None else None
        if area is not None and me is not None:
            for name, spot_name in at.items():
                person = self._map_person(str(name), world)
                tile = anchor_tile(area, str(spot_name), (me.x, me.y))
                if person is None or tile is None or person not in placed:
                    continue
                current = state.spots.get(person)
                if current is not None and current.area != area.id:
                    continue
                taken = {
                    (s.x, s.y) for n, s in state.spots.items() if n != person and s.area == area.id
                }
                x, y = stand_near(area, tile, taken | {tile})
                state.spots[person] = Spot(area=area.id, x=x, y=y)
        if self.story.plot is None and is_opening(path[:-1]):
            # The clock starts at the time of day the opening is set at (it
            # started at midnight whatever the opening said).
            state.seconds += opening_minute(reply.get("starts_at")) * 60
        if self.story.plot is None:
            minutes = reply.get("minutes")
            quote = str(reply.get("minutes_quote") or "")
            if (
                isinstance(minutes, (int, float))
                and not isinstance(minutes, bool)
                and minutes > 0
                and quote
                and locate_quote(source, quote) is not None
            ):
                state.seconds += int(min(minutes, MAX_READ_MINUTES)) * 60
        node.meta.area = state
        return refused

    # --- what the passage did to the things on the play view ----------------------

    def _things_after_read(
        self,
        state: AreaState,
        node: Node,
        reply: dict[str, Any],
        source: str,
        *,
        world: WorldMap,
        before: WorldMap,
        art: WorldArt,
        held: str | None,
        placements: Sequence[tuple[dict[str, Any], str]],
        moves_fixed: bool,
        marks: Sequence[Mark] = (),
    ) -> list[str]:
        """The records kept in step with what the passage did (the author,
        Oct 17 2026: things the story adds must show on the play view, and
        furniture the program laid is no different to any other thing): the
        map's things it took, used up, broke, brought or moved, and the
        play view's own things the read named (`placements`). A thing put
        down where the author's character is lies at the spot the read gave,
        else beside whoever carried it, else beside the character, else at
        its place; one elsewhere is laid where its place is when that area
        is built. A fixed thing (engine/takeable.py) moves only on the turn
        that may move it (`moves_fixed`); what is refused is returned."""
        refused: list[str] = []
        me = state.spots.get(held or "")
        here = self.area(me.area, world, art, things=state.things) if me is not None else None
        entries = [e for e in reply.get("things") or [] if isinstance(e, dict)]

        def said_spot(entry: dict[str, Any]) -> str:
            """Where the read says a thing now stands: its "spot", else where
            its "state" says it went (`spot_in_words`)."""
            spot = str(entry.get("spot") or "").strip()
            if spot and not spot.lower().startswith("carried by"):
                return spot
            if here is None or me is None:
                return ""
            return spot_in_words(here, str(entry.get("state") or ""), (me.x, me.y))

        def entry_for(name: str) -> dict[str, Any]:
            """The read's entry for the map's thing of this name: by its name,
            else by a shortening of it (as `resolve_reply` found it)."""
            wanted = key(bare(name))
            for e in entries:
                if key(bare(str(e.get("name") or ""))) == wanted:
                    return e
            return next((e for e in entries if same_thing(str(e.get("name") or ""), name)), {})

        people = {(s.x, s.y) for n, s in state.spots.items() if me and s.area == me.area}
        # A thing put at a spot the author's turn marked lies on its tile.
        by_point = {(m.x, m.y): m for m in marks}
        marked: dict[str, Spot] = {}
        for e in entries:
            for field in ("at", "spot"):
                found = marks_in(str(e.get(field) or ""))
                mark = by_point.get((found[0].x, found[0].y)) if found else None
                if mark is not None and mark.spot is not None:
                    marked[key(str(e.get("name") or ""))] = mark.spot
                    break

        def tile_in(
            area: Area, near: tuple[int, int], pid: str, w: int = 1, h: int = 1, said=False
        ):
            """A tile for it in `area`; `said`: where the story or the author
            put it (a doorway it blocks is a doorway), else clear of the ways."""
            without = replace(area, placements=[p for p in area.placements if p.id != pid])
            x, y = thing_tile(without, near, people, w=w, h=h, clear=not said)
            return Spot(area=area.id, x=x, y=y)

        delta = node.meta.world
        built = self._built_area(me.area, world, art) if me is not None else None
        for t in delta.things if delta is not None else []:
            old = before.things.get(t.id)
            # A thing the area draws already (a plan's object of its name,
            # taken or not) is kept under that one's id, as the menu's choices
            # keep it; the map's listing it for the first time moves nothing.
            twin = _twin(built, t.name, world) if built is not None else None
            pid = twin.id if twin is not None else t.id
            record = state.things.get(pid)
            words = t.state.lower()
            if t.carried_by:
                state.things[pid] = ThingRecord(state="taken", name=t.name, by=t.carried_by)
                continue
            if not t.at or GONE.search(words):
                state.things[pid] = ThingRecord(state="gone", name=t.name)
                continue
            broken = BROKEN.search(words) is not None
            kind = "broken" if broken else "dropped"
            spot_name = said_spot(entry_for(t.name))
            if old is None:
                moved = twin is None
            else:
                moved = old.at != t.at or old.carried_by is not None
            target = area_of(world, art, t.at)
            at_mark = marked.get(key(str(entry_for(t.name).get("name") or "")))
            if at_mark is not None and at_mark.area != target:
                at_mark = None
            if (spot_name or at_mark) and not moved and fixed_thing(t.name) and not moves_fixed:
                refused.append(
                    f"thing {t.name}: fixed in place, so it moves only on the author's "
                    "Direction or Narration, or a turn under Fiat"
                )
                spot_name, at_mark = "", None
            if at_mark is not None:
                # On the marked tile, in whichever area it lies.
                there = here if here is not None and here.id == at_mark.area else None
                there = there or self.area(at_mark.area, world, art, things=state.things)
                if there is not None:
                    state.things[pid] = ThingRecord(
                        state=kind,
                        name=t.name,
                        spot=tile_in(there, (at_mark.x, at_mark.y), pid, said=True),
                    )
                    continue
            entry = entry_for(t.name)
            # Too heavy to carry, "carried by" someone here: dragged beside them.
            dragged = None
            said_by = str(entry.get("at") or "")
            if said_by.lower().startswith("carried by") and thing_class(t.name) in (
                "movable",
                "fixed",
            ):
                holder = self._map_person(said_by[len("carried by") :].strip(), world)
                by = state.spots.get(holder or "")
                if by is not None and here is not None and by.area == here.id:
                    dragged = (by.x, by.y)
                if dragged and fixed_thing(t.name) and not moves_fixed:
                    refused.append(
                        f"thing {t.name}: fixed in place, so it moves only on the author's "
                        "Direction or Narration, or a turn under Fiat"
                    )
                    dragged = None
            stayed = STAYED.search(f"{entry.get('state') or ''} {entry.get('quote') or ''}".lower())
            shown = next((p for p in here.placements if p.id == pid), None) if here else None
            if (
                spot_name
                and not moved
                and (
                    stayed is not None
                    or (shown is not None and _names_room_of(here, spot_name, shown))
                )
            ):
                spot_name = ""
            if stayed is not None and not moved:
                dragged = None
            if here is not None and target == here.id and (spot_name or moved or dragged):
                near = anchor_tile(here, spot_name, (me.x, me.y)) if spot_name else None
                near = near or dragged
                carrier = state.spots.get(old.carried_by) if old and old.carried_by else None
                if near is None and carrier is not None and carrier.area == here.id:
                    near = (carrier.x, carrier.y)
                if near is None and t.at == world.where.get(held or ""):
                    near = (me.x, me.y)
                if near is None:
                    near = anchor_of(here, world, t.at) or (me.x, me.y)
                said = bool(spot_name) and anchor_tile(here, spot_name, (me.x, me.y)) is not None
                spot = tile_in(here, near, pid, said=said)
                if not moved and shown is not None and not _went_far(here, shown, spot):
                    continue  # it stays where it lies
                state.things[pid] = ThingRecord(state=kind, name=t.name, spot=spot)
            elif moved:
                state.things.pop(pid, None)  # laid at its place when its area is built
            elif record is not None and record.spot is not None and broken:
                state.things[pid] = record.model_copy(update={"state": "broken"})
        refused += self._placements_after_read(
            state, placements, source, world, art, here, me, moves_fixed, tile_in, said_spot
        )
        return refused

    def _placements_after_read(
        self,
        state: AreaState,
        placements: Sequence[tuple[dict[str, Any], str]],
        source: str,
        world: WorldMap,
        art: WorldArt,
        here: Area | None,
        me: Spot | None,
        moves_fixed: bool,
        tile_in,
        said_spot,
    ) -> list[str]:
        """The read's entries for the play view's own things (a plan's
        objects, the furniture, by placement id): taken by someone (or, too
        heavy to carry, moved beside them), moved to a spot or to another
        place, broken or used up; each held to its quote, and a fixed thing
        moved only on the turn that may move it."""
        refused: list[str] = []
        if here is None or me is None:
            return refused
        by_id = {p.id: p for p in here.placements}
        for entry, pid in placements:
            p = by_id.get(pid)
            quote = str(entry.get("quote") or "")
            if p is None or not quote or locate_quote(source, quote) is None:
                refused.append(f"thing {entry.get('name')}: no quote found")
                continue
            kind = thing_class(p.name)
            at = str(entry.get("at") or "").strip()
            if str(entry.get("spot") or "").lower().startswith("carried by"):
                at = str(entry.get("spot"))  # "spot": "carried by John", once
            spot_name = said_spot(entry)
            words = str(entry.get("state") or "").lower()
            record: ThingRecord | None = None
            moved_to: Spot | None = None
            if at.lower().startswith("carried by"):
                holder = self._map_person(at[len("carried by") :].strip(), world)
                where = state.spots.get(holder or "")
                if holder is not None and kind == "carried":
                    record = ThingRecord(state="taken", name=p.name, by=holder)
                elif where is not None and where.area == here.id:
                    moved_to = tile_in(here, (where.x, where.y), pid, p.w, p.h)
            elif spot_name or (at and anchor_tile(here, at, (me.x, me.y)) is not None):
                near = anchor_tile(here, spot_name or at, (me.x, me.y))
                if near is not None:
                    moved_to = tile_in(here, near, pid, p.w, p.h, said=True)
            elif at:
                moved_to = self._placed_elsewhere(world, art, at, here, pid, p, tile_in)
            if moved_to is not None and (
                STAYED.search(f"{words} {quote}".lower()) is not None
                or not _went_far(here, p, moved_to)
                or _names_room_of(here, spot_name, p)
            ):
                moved_to = None  # it stayed (said so, or named where it stands)
            if (record is not None or moved_to is not None) and kind == "fixed" and not moves_fixed:
                refused.append(
                    f"thing {p.name}: fixed in place, so it moves only on the author's "
                    "Direction or Narration, or a turn under Fiat"
                )
                record, moved_to = None, None
            current = state.things.get(pid)
            if GONE.search(words):
                record = ThingRecord(state="gone", name=p.name)
            elif moved_to is not None:
                broken = BROKEN.search(words) is not None or (
                    current is not None and current.state == "broken"
                )
                record = ThingRecord(
                    state="broken" if broken else "dropped",
                    name=p.name,
                    spot=moved_to,
                    w=p.w,
                    h=p.h,
                )
            elif record is None and BROKEN.search(words):
                record = ThingRecord(
                    state="broken",
                    name=p.name,
                    spot=current.spot if current is not None else None,
                    w=p.w,
                    h=p.h,
                )
            if record is not None:
                state.things[pid] = record
        return refused

    def _placed_elsewhere(
        self, world: WorldMap, art: WorldArt, at: str, here: Area, pid: str, p, tile_in
    ) -> Spot | None:
        """Where a thing of the play view goes when the read puts it at a
        place in another area (one that has a plan): at that place there.
        The place it is in already moves it nowhere."""
        place_id = find_place(world, at, [])
        if place_id is None:
            return None
        target = area_of(world, art, place_id)
        if target == here.id:
            return None  # within the area a move is to a spot ("spot"), not a place
        area = self.area(target, world, art)
        if area is None:
            return None
        near = anchor_of(area, world, place_id) or self._way_in(area)
        return tile_in(area, near, pid, p.w, p.h)

    def placement_entries(
        self, reply: dict[str, Any], path: Sequence[Node], world: WorldMap, held: str | None
    ) -> tuple[dict[str, Any], list[tuple[dict[str, Any], str]]]:
        """The read's thing entries that name one of the play view's own things
        around the author's character (a plan's object or a piece of
        furniture: the map's list hasn't got it), each with its placement
        id, and the reply without them, for `resolve_reply` to read the
        rest. The map's own things win a name; of several with one name,
        the nearest the author's character."""
        entries = reply.get("things")
        if not self.map_first or not isinstance(entries, list) or not entries:
            return reply, []
        state = self._snapshot_on(path)
        me = state.spots.get(held or "") if state is not None else None
        area = self.area(me.area, world, things=state.things) if me and state else None
        if area is None:
            return reply, []
        own = [p for p in area.placements if p.id not in world.things]
        kept: list[Any] = []
        found: list[tuple[dict[str, Any], str]] = []
        for entry in entries:
            name = str(entry.get("name") or "") if isinstance(entry, dict) else ""
            wanted = key(bare(name))
            # The map's thing by its exact name, then the play view's; then a
            # shortening of either ("Second workbench"), the map's first.
            exact = any(key(bare(t.name)) == wanted for t in world.things.values())
            same = [p for p in own if key(bare(p.name)) == wanted] if not exact else []
            if not same and not exact and _find_thing(world, name) is None:
                same = [p for p in own if same_thing(name, p.name)]
            if not name or not same:
                kept.append(entry)
                continue
            # Of several of one name, the one beside the author's character
            # (the spot is where it went, not where it was: live, the bench
            # nearest the door was moved and the one shoved stayed).
            best = min(same, key=lambda p: math.hypot(p.centre[0] - me.x, p.centre[1] - me.y))
            found.append((entry, best.id))
        if not found:
            return reply, []
        return {**reply, "things": kept}, found

    def read_things_here(self, path: Sequence[Node], held: str | None = None) -> str:
        """The things on the play view around the author's character, nearest
        first, for the map read (`map_read.things_here`); "" when the story
        isn't played on a map or there are none."""
        if not self.map_first:
            return ""
        state = self._snapshot_on(path)
        me = state.spots.get(held or self.held_name() or "") if state is not None else None
        area = self.area(me.area, things=state.things) if me is not None and state else None
        if me is None or area is None or not area.placements:
            return ""
        counts: dict[str, list[float]] = {}
        for p in area.placements:
            d = math.hypot(p.centre[0] - me.x, p.centre[1] - me.y)
            counts.setdefault(bare(p.name), []).append(d)
        ordered = sorted(counts.items(), key=lambda item: min(item[1]))[:THINGS_TOLD]
        things = "; ".join(
            the(name) + (f" ({len(ds)})" if len(ds) > 1 else "") for name, ds in ordered
        )
        return self.texts.fill("map_read.things_here", things=things)

    def _map_person(self, name: str, world: WorldMap | None = None) -> str | None:
        """Who `name` is on the map: someone in the story, else (with the
        map) someone not named yet, by their label or description, else
        someone the map keeps by a name with no card."""
        from sealedlore.engine.scene_state import same_person
        from sealedlore.models.character import Character

        for person in self.map_people():
            if same_person(name, person):
                return person.name
        if world is None or not name:
            return None
        found = unknown_for(name, world)
        if found is not None:
            return found
        # Named by a passage with no card ("Unknown 1" named Ruth, live): the
        # map keeps them by that name, and without this the read could never
        # stand them anywhere again.
        return next(
            (
                known
                for known in world.where
                if known not in world.unknowns and same_person(name, Character(id="", name=known))
            ),
            None,
        )

    def read_spots(self, path: Sequence[Node], held: str | None = None) -> str:
        """The spots near the author's character (`held`: the passage's, else
        the one held now), for the map read (`map_read.spots`); "" when the
        story isn't played on a map."""
        if not self.map_first:
            return ""
        before = self._snapshot_on(path)
        if before is None:
            return ""
        me = before.spots.get(held or self.held_name() or "")
        area = self.area(me.area if me else None)
        if me is None or area is None:
            return ""
        spots = anchors_near(area, me.x, me.y)
        return self.texts.fill("map_read.spots", spots=", ".join(spots)) if spots else ""

    def read_minutes(self, author: Node | None, *, opening: bool = False) -> str:
        """The story time the passage covers, asked of the map read where the
        map keeps the clock (no plot), with the walk already counted named
        (`map_read.minutes`, `map_read.walked`); "" otherwise. `opening`: the
        story's first passage, which also says the time of day it starts at
        (`map_read.starts_at`)."""
        if not self.map_first or self.story.plot is not None:
            return ""
        walked = self.walk_minutes(author) if author is not None else 0
        already = self.texts.fill("map_read.walked", minutes=str(walked)) if walked > 0 else ""
        asked = self.texts.fill("map_read.minutes", walked=already)
        if opening:
            given = (self.story.setup.starting_scene.time_of_day or "").strip()
            hint = self.texts.fill("map_read.starts_at.hint", time=given) if given else ""
            asked += self.texts.fill("map_read.starts_at", hint=hint)
        return asked

    def positions_context(
        self,
        nodes: Sequence[Node],
        held_id: str | None,
        *,
        turn_node_id: str | None = None,
        mark_fixed: bool = False,
    ) -> str:
        """Where everyone stands, for the storyteller's map section (design
        §5): from the turn being sent (its snapshot, found by `turn_node_id`:
        on a retake the leaf is the previous take, so the leaf can't say) or,
        for a Question, where they stand now, with no turn's action or walk."""
        if not self.map_first or self.in_private:
            return ""
        held = self.map_held_name(held_id) if held_id is not None else self.held_name()
        if held is None:
            return ""
        turn = self._turn_node(nodes, turn_node_id)
        if turn is not None and turn.meta.area is not None:
            state = turn.meta.area
        else:
            turn = None
            live = self._live()
            state = live if live is not None else self._snapshot_on(nodes)
        if state is None:
            return ""
        world = self._world_on([*nodes, turn] if turn is not None else nodes)
        art = self._art_read()
        me = state.spots.get(held)
        area = self.area(me.area if me else None, world, art, things=state.things)
        # The time of day (never the day) and the weather the story stated.
        # Without a plot, nothing before the opening says the time: the
        # opening's read starts the clock (it was "the small hours" for every
        # opening, and a retake of it was told another time).
        before = [n for n in nodes if turn is None or n.id != turn.id]
        clock = None if self.story.plot is None and is_opening(before) else self.map_clock()
        now = now_words(clock[1] if clock else None, world.weather)
        action_line = ""
        action = state.action
        if turn is not None and action is not None and action.text in turn.content:
            if action.verb == "give":
                action_line = self.texts.fill("map.action.give", name=action.target, who=action.to)
            else:
                action_line = self.texts.fill(f"map.action.{action.verb}", name=action.target)
        if me is None or area is None:
            # Nowhere to stand yet (a place whose plan isn't drawn): the
            # journey and the choice still stand, or the storyteller wrote
            # the departure from the place left (the second review).
            if turn is None or (state.walked < 30 and not action_line):
                return ""
            place = world.places.get(world.where.get(held) or "")
            lines = [
                self.texts["map.positions"],
                *([now] if now else []),
                f"- {held}: at {place.name} (just arrived; nothing of it is drawn yet)."
                if place is not None
                else f"- {held}: on the way.",
            ]
            if state.walked >= 30 and state.walked_words:
                lines.append(f"Since the last passage: {state.walked_words}.")
            if action_line:
                lines.append(action_line)
            return "\n".join(lines)
        text = render_positions(
            area,
            world,
            state.spots,
            held,
            walked=state.walked_words if turn is not None and state.walked >= 30 else "",
            action_line=action_line,
            things=state.things,
            texts=self.texts,
            elsewhere=self._on_other_levels(area, state.spots, held, world, art),
            level_names=self._level_names(area, world, art),
            now=now,
            mark_fixed=mark_fixed,
        )
        if not text:
            return ""
        heading = self.texts["map.positions"]
        return text.replace(text.split("\n", 1)[0], heading, 1)

    def map_scene(
        self, nodes: Sequence[Node], held_id: str | None, *, turn_node_id: str | None = None
    ) -> SceneState | None:
        """The scene card as the storyteller is sent it in a story played on
        the map (engine/map_presence.py): the story's card, with the map's
        word on who is where, on the turn's map as the positions are: in the
        scene is whoever the map has in sight or within earshot of the
        author's character. None when the map doesn't place the author's
        character (the card as it is)."""
        if not self.map_first or self.in_private or self.story.chat:
            return None
        held = self.map_held_name(held_id) if held_id is not None else self.held_name()
        if held is None:
            return None
        turn = self._turn_node(nodes, turn_node_id)
        if turn is not None and turn.meta.area is not None:
            state = turn.meta.area
        else:
            turn = None
            live = self._live()
            state = live if live is not None else self._snapshot_on(nodes)
        me = state.spots.get(held) if state is not None else None
        if state is None or me is None:
            return None
        world = self._world_on([*nodes, turn] if turn is not None else nodes)
        area = self.area(me.area, world, self._art_read(), things=state.things)
        if area is None:
            return None
        mine = (me.x, me.y)
        here: list[str] = []
        away: dict[str, str] = {}
        for name, spot in state.spots.items():
            if name == held:
                continue
            theirs = (spot.x, spot.y)
            near = sees(area, mine, theirs) or in_earshot(area, mine, theirs)
            if spot.area == me.area and near:
                here.append(name)  # in the scene: in sight or within earshot
            elif name in world.unknowns:
                continue  # someone not named yet, out of sight: nobody to list
            elif spot.area == me.area:
                room = area.room_at(*theirs)
                at = room_words(area, room) if room is not None else area.name
                away[name] = f"{at}, out of sight and earshot"
            else:
                place = world.places.get(world.where.get(name) or "")
                away[name] = place.name if place is not None else self.area_title(spot.area, world)
        return reconcile_scene(
            self.story.scene,
            here=here,
            away=away,
            cast=self.visible_cast(),
            unknowns=world.unknowns,
            held_id=held_id if held_id is not None else self.story.held_character_id,
        )

    def scene_without_map(self) -> SceneState:
        """The scene card for this story played on without its map (the
        author, Oct 19 2026: someone enjoying a story who no longer wants
        to pay for the map goes on "from where they are ... just as a normal
        story"): the card the storyteller is sent now (`map_scene`, with the
        walking since the last turn), its Location the map's places where
        the author's character is, anyone elsewhere noted where the map has
        them, and who carries what in the situation, since nothing else
        will say so. The story's own card when the map places nobody."""
        path = self.path()
        held_id = self.story.held_character_id
        scene = self.map_scene(path, held_id) if path else None
        if scene is None:
            return self.story.scene.model_copy(deep=True)
        world = self.world()
        held = self.map_held_name(held_id)
        where = world.where.get(held or "") or world.start
        now = self.here_place()
        if now is not None and now != where and now not in {p.id for p in _chain(world, where)}:
            where = now  # walked on since the last turn
        places = _chain(world, where)
        own = self.story.scene.location or ""
        if places and own and _names_place(own, places[0]):
            # The card's own words name the place and are often finer ("the
            # dining room inside Ashcombe Hall", where the map has the Hall).
            scene.location = own
        elif places:
            outside = held in world.outside and where == world.where.get(held or "")
            chain = ", in ".join(p.name for p in places[:3])
            scene.location = ("Just outside " if outside else "") + chain
        for entry in scene.offstage_but_nearby:
            if entry.note and entry.note.startswith(MAP_HAS_THEM):
                entry.note = "at " + entry.note[len(MAP_HAS_THEM) :]
        state = self.area_state()
        names = [held or "", *(c.name for c in self.visible_cast()), *state.spots]
        carries = [
            self.texts.fill("map.carries", name=name, things=", ".join(things))
            for name in dict.fromkeys(n for n in names if n)
            if (things := carried_by(state.things, name))
        ]
        if carries:
            scene.situation = " ".join([scene.situation or "", *carries]).strip()
        return scene

    def fixed_here(
        self, nodes: Sequence[Node], held_id: str | None, *, turn_node_id: str | None = None
    ) -> list[str]:
        """What is fixed in place (engine/takeable.py `fixed_thing`: built
        in, rooted, bolted down) in the author's character's area on the
        turn's map, nearest first, the map's things laid there among them,
        by name (without its article). The whole
        area, not only what is in reach: asked to drag the bench over, the
        sergeant's companion did, from across the room (Oct 17 2026)."""
        held = self.map_held_name(held_id) if held_id is not None else self.held_name()
        if held is None:
            return []
        turn = self._turn_node(nodes, turn_node_id)
        if turn is not None and turn.meta.area is not None:
            state = turn.meta.area
        else:
            turn = None
            state = self._live() or self._snapshot_on(nodes)
        world = self._world_on([*nodes, turn] if turn is not None else nodes)
        names: list[str] = []
        me = state.spots.get(held) if state is not None else None
        area = self.area(me.area, world, self._art_read(), things=state.things) if me else None
        if me is not None and area is not None:
            nearest = sorted(
                area.placements,
                key=lambda p: math.hypot(p.centre[0] - me.x, p.centre[1] - me.y),
            )
            for p in nearest:
                if fixed_thing(p.name):
                    names.append(re.sub(r"^(the|a|an)\s+", "", p.name, flags=re.IGNORECASE))
        here = world.where.get(held)
        for thing in world.things.values():
            if here and thing.at == here and not thing.carried_by and fixed_thing(thing.name):
                names.append(re.sub(r"^(the|a|an)\s+", "", thing.name, flags=re.IGNORECASE))
        return list(dict.fromkeys(names))

    def _level_names(self, area: Area, world: WorldMap, art: WorldArt) -> dict[str, str]:
        """Each level the area's stairs lead to, named for the storyteller
        ("the upper floor", "the ground floor"), by its area id."""
        place = world.places.get(area.place_id)
        if place is None:
            return {}
        layout = art.layout_shown(place.id, place.state)
        deck = area.look == "space"
        out = {}
        for e in area.stairs_entrances():
            parsed = parse_level(e.place_id)
            index = parsed[1] if parsed is not None else 0
            out[e.place_id] = f"the {level_name(layout, index, deck=deck).lower()}"
        return out

    def _on_other_levels(
        self, area: Area, spots: Mapping[str, Spot], held: str, world: WorldMap, art: WorldArt
    ) -> list[str]:
        """'Also in The farmhouse, on another level: Jane (upper floor)': the
        people on the other levels of the place the author's character is
        in, so the storyteller knows who is in the building though not in
        the room."""
        if area.window or (not area.level and not area.stairs_entrances()):
            return []
        place = world.places.get(area.place_id)
        if place is None:
            return []
        layout = art.layout_shown(place.id, place.state)
        deck = area.look == "space"
        found = []
        for name, spot in spots.items():
            if name == held or spot.area == area.id or place_of_area(spot.area) != area.place_id:
                continue
            parsed = parse_level(spot.area)
            index = parsed[1] if parsed is not None else 0
            found.append(f"{name} ({level_name(layout, index, deck=deck).lower()})")
        if not found:
            return []
        return [f"Also in {place.name}, on another level: " + ", ".join(found[:8]) + "."]

    def _turn_node(self, nodes: Sequence[Node], turn_node_id: str | None) -> Node | None:
        """The author's turn being sent after `nodes`: the one named, else
        the leaf once it is attached there (a send; a retake names its turn,
        since its leaf is the previous take)."""
        if turn_node_id is not None:
            turn = next((n for n in self.nodes if n.id == turn_node_id), None)
            if turn is not None and turn.kind == "user":
                return turn
        full = self.path()
        if len(full) != len(nodes) + 1 or full[-1].kind != "user":
            return None
        if nodes and full[-2].id != nodes[-1].id:
            return None
        return full[-1]

    # --- an area's edges -----------------------------------------------------------

    def edge_here(self) -> str | None:
        """The side ("north"…) of an outdoor area whose edge the author's
        character stands on, or None."""
        state = self.area_state()
        me = state.spots.get(self.held_name() or "")
        area = self.area(me.area if me else None)
        if me is None or area is None or area.indoor:
            return None
        if me.y == 0:
            return "north"
        if me.y == area.height - 1:
            return "south"
        if me.x == 0:
            return "west"
        if me.x == area.width - 1:
            return "east"
        return None

    def _beyond(self, side: str) -> tuple[Spot, float] | None:
        """The first tile of another area straight on past this edge, and
        how far it is: a place's plan within EDGE_REACH metres (across a
        gap of open country, as before windows), else the window of open
        country just past the edge (the author: the world is limitless)."""
        world, art = self.world(), self._art_read()
        state = self.area_state()
        me = state.spots.get(self.held_name() or "")
        area = self.area(me.area if me else None, world, art)
        start = tile_point(world, area, me.x, me.y) if me and area else None
        if start is None or area is None:
            return None
        dx, dy = EDGE_STEPS[side]
        step = max(metres_per_tile(world, area), 1.0)
        reach = step
        window: tuple[Spot, float] | None = None
        while reach <= EDGE_REACH:
            x, y = start[0] + dx * reach, start[1] + dy * reach
            spot = self._mark_spot(Mark(token="", x=x, y=y), world, art)
            if spot is not None and spot.area != area.id:
                there = self.area(spot.area, world, art)
                if there is not None and not there.indoor:
                    landing = self._landing(there, spot, side)
                    if there.window:
                        if window is None and landing is not None:
                            window = Spot(area=spot.area, x=landing[0], y=landing[1]), reach
                    elif landing is None:
                        return None  # water, trees or rock bar the way there
                    else:
                        return Spot(area=spot.area, x=landing[0], y=landing[1]), reach
            reach += step
        return window

    @staticmethod
    def _landing(area: Area, spot: Spot, side: str) -> tuple[int, int] | None:
        """A tile to stand on at the far area's edge near `spot`: along the
        edge a few tiles either way and a tile or two in, walkable, never
        past what bars it (`stand_near` would cross a river to the nearest
        dry tile; the review, Oct 16 2026)."""
        along = (1, 0) if side in ("north", "south") else (0, 1)
        inward = {"north": (0, 1), "south": (0, -1), "east": (-1, 0), "west": (1, 0)}[side]
        blocked = area.blocked()
        for depth in range(0, EDGE_LANDING_IN + 1):
            for offset in sorted(range(-EDGE_LANDING_ALONG, EDGE_LANDING_ALONG + 1), key=abs):
                x = spot.x + along[0] * offset + inward[0] * depth
                y = spot.y + along[1] * offset + inward[1] * depth
                if area.walkable(x, y, blocked):
                    return x, y
        return None

    def edge_choices(self, side: str) -> list[tuple[str, str, int]]:
        """Where the author's character can go on past this edge: ("area",
        its name, seconds) for an area straight on that adjoins this one,
        then ("place id", name, seconds on foot) for the known places that
        lie that way, nearest first."""
        out: list[tuple[str, str, int]] = []
        beyond = self._beyond(side)
        world = self.world()
        art = self._art_read()
        if beyond is not None:
            spot, metres = beyond
            there = self.area(spot.area, world, art)
            name = there.name if there is not None else "open country"
            if there is not None and there.within:
                name = f"{name} ({there.within})"
            out.append(("area", name, int(metres / WALK_M_PER_S)))
        me = self.held_point()
        if me is None:
            return out
        want = {"north": 0, "east": 90, "south": 180, "west": 270}[side]
        adjoining = beyond[0].area if beyond is not None else None
        for place_id, name, seconds in self.travel_choices():
            spot = position(world, place_id)
            if spot is None:
                continue
            if adjoining is not None and (
                place_id == adjoining
                or (not is_window(adjoining) and area_of(world, art, place_id) == adjoining)
            ):
                continue  # offered once, as the walk on (a plan's; a window is wide)
            angle = (math.degrees(math.atan2(spot[0] - me[0], spot[1] - me[1])) + 360) % 360
            if min(abs(angle - want), 360 - abs(angle - want)) <= EDGE_ARC:
                out.append((place_id, name, seconds))
        return out[:EDGE_CHOICES]

    def cross_edge(self, side: str) -> AreaState | None:
        """On past the edge into the area that adjoins it, as through a door:
        no turn written, the clock moved on by the gap."""
        beyond = self._beyond(side)
        if beyond is None:
            return None
        spot, metres = beyond
        world, art = self.world(), self._art_read()
        state = self.area_state()
        held = self.held_name()
        here = self.area(state.spots[held].area, world, art)
        there = self.area(spot.area, world, art)
        if here is None or there is None:
            return None
        taken = {(s.x, s.y) for n, s in state.spots.items() if n != held and s.area == spot.area}
        x, y = (spot.x, spot.y)
        if (x, y) in taken:
            x, y = stand_near(there, (x, y), taken)
        state.spots[held] = Spot(area=spot.area, x=x, y=y)
        self._add_time(state, walk_seconds(metres), here.name, there.name)
        self._set_live(state)
        return state

    # --- marked spots (engine/marks.py) ------------------------------------------

    def held_point(
        self,
        state: AreaState | None = None,
        world: WorldMap | None = None,
        art=None,
        *,
        held: str | None = None,
    ) -> tuple[float, float] | None:
        """Where the author's character (`held`: the turn's, else the one
        held now) stands, in metres (east, north): their tile on the map,
        else their place's spot."""
        world = world if world is not None else self.world()
        art = art if art is not None else self._art_read()
        held = held or self.held_name()
        if state is None:
            state = self.area_state() if self.map_first else AreaState()
        me = state.spots.get(held or "")
        area = self.area(me.area, world, art) if me is not None else None
        if me is not None and area is not None:
            point = tile_point(world, area, me.x, me.y)
            if point is not None:
                return point
        here = world.where.get(held or "") or world.start
        return position(world, here) if here else None

    def mark_of_tile(self, area_id: str, x: int, y: int) -> str:
        """The token for a tile of an area, for the composer."""
        world = self.world()
        area = self.area(area_id, world)
        point = tile_point(world, area, x, y) if area is not None else None
        return mark_token(*point) if point is not None else ""

    def resolve_marks(
        self, text: str, world: WorldMap, art: WorldArt, state: AreaState | None
    ) -> list[Mark]:
        """The marks a turn's text holds, each with the place it lies in, its
        tile, and what is there in words for the storyteller."""
        found = marks_in(text)
        if not found:
            return []
        held = self.held_name()
        me = self.held_point(state, world, art)
        out = []
        for mark in found:
            place_id = place_at(world, art, mark.x, mark.y)
            spot = None
            parts: list[str] = []
            area_id = area_of(world, art, place_id, (mark.x, mark.y))
            area = self.area(area_id, world, art) if area_id else None
            tile = point_tile(world, area, mark.x, mark.y) if area is not None else None
            if area is not None and tile is not None:
                spot = Spot(area=area.id, x=tile[0], y=tile[1])
            if place_id:
                parts.append(chain_words(world, place_id))
            else:
                near = nearest_place(world, mark.x, mark.y)
                words = "open country"
                if near is not None:
                    other = world.places[near[0]]
                    way = compass(mark.x - other.x, mark.y - other.y)  # type: ignore[operator]
                    words += f", {describe_metres(near[1])} {way} of {other.name}"
                if area is not None and area.within:  # the region it lies in
                    words += f", in {area.within}"
                parts.append(words)
            if area is not None and tile is not None:
                where = where_words(area, *tile)
                if where:
                    parts.append(where)
            if me is not None and held:
                apart = math.hypot(mark.x - me[0], mark.y - me[1])
                if apart < 2:
                    parts.append(f"where {held} stands")
                else:
                    way = compass(mark.x - me[0], mark.y - me[1])
                    parts.append(f"{describe_metres(apart)} {way} of {held}")
            out.append(
                Mark(
                    token=mark.token,
                    x=mark.x,
                    y=mark.y,
                    place_id=place_id,
                    spot=spot,
                    words="; ".join(p for p in parts if p),
                )
            )
        return out

    def marks_context(self, nodes: Sequence[Node], *, turn_node_id: str | None = None) -> str:
        """The marks of the turn being sent, for the storyteller's map
        section (`map.marks`); "" when it marks none."""
        if not self.map_on or self.in_private:
            return ""
        turn = self._turn_node(nodes, turn_node_id)
        if turn is None or turn.meta.area is None or not turn.meta.area.marks:
            return ""
        lines = [f"- {m.token}: {m.words}." for m in turn.meta.area.marks]
        return self.texts.fill("map.marks", marks="\n".join(lines))

    def read_marks(self, author: Node | None) -> str:
        """The author's turn's marks, for the map read (`map_read.marks`)."""
        if author is None or author.meta.area is None or not author.meta.area.marks:
            return ""
        marks = author.meta.area.marks
        lines = [f"- {m.token}: {m.words}" for m in marks]
        return self.texts.fill("map_read.marks", marks="\n".join(lines), example=marks[0].token)

    def apply_marks(
        self, before: WorldMap, delta: WorldDelta, reply: dict[str, Any], author: Node | None
    ) -> None:
        """What the read put at a marked spot is put there exactly: a new
        place on the point (inside whatever there holds it), and anyone sent
        to one in the place it lies in."""
        marks = self._turn_marks(author)
        if not marks:
            return
        new = {key(p.name): p for p in delta.places if p.x is None and p.id not in before.places}
        art = self._art_read()
        for entry in reply.get("places") or []:
            if not isinstance(entry, dict):
                continue
            mark = self._mark_of(marks, entry.get("mark"), words=True)
            place = new.get(key(str(entry.get("name") or "")))
            if mark is None or place is None:
                continue
            known = {**before.places, **{p.id: p for p in delta.places}}
            holder = known.get(place.inside or "")
            if holder is not None and holder.id not in before.places:
                # Inside a place this same read made: held to its own mark
                # when it has one, else as the read says.
                keep = holder.x is None or (
                    abs(mark.x - holder.x) <= size_of(holder) / 2
                    and abs(mark.y - holder.y) <= size_of(holder) / 2  # type: ignore[operator]
                )
            else:
                box = footprint(before, art, holder.id) if holder is not None else None
                keep = box is not None and box[0] <= mark.x <= box[2] and box[1] <= mark.y <= box[3]
            if not keep:
                place.inside = place_at(before, art, mark.x, mark.y)
                holder = known.get(place.inside or "")
            size = place.size if place.size is not None else KIND_SIZE.get(place.kind, 100.0)
            if holder is not None:
                size = min(size, size_of(holder) * CHILD_SHARE)
            place.size = size
            if moves(known, place):
                continue  # aboard a vehicle: it goes where the vehicle goes
            place.x, place.y = mark.x, mark.y
        # A thing the read puts at a marked spot is at the place there.
        after = applied(before, delta)
        for entry in reply.get("things") or []:
            if not isinstance(entry, dict):
                continue
            mark = self._mark_of(marks, entry.get("at"))
            if mark is None:
                continue
            name = key(str(entry.get("name") or ""))
            thing = next((t for t in delta.things if key(t.name) == name), None)
            if thing is not None and thing.at is None and not thing.carried_by:
                thing.at = place_at(after, art, mark.x, mark.y)

    @staticmethod
    def _turn_marks(author: Node | None) -> list[Mark]:
        return list(author.meta.area.marks) if author is not None and author.meta.area else []

    @staticmethod
    def _mark_of(marks: Sequence[Mark], value: Any, *, words: bool = False) -> Mark | None:
        """The mark a read's value names: by its coordinate; with `words`,
        also by the spot's own description when one spot is marked (live,
        the read copied "in the kitchen and eating area" for its coordinate),
        never for "none" or the like, nor for words the spot wasn't given."""
        text = str(value or "").strip()
        found = marks_in(text)
        by_point = {(m.x, m.y): m for m in marks}
        if found:
            return by_point.get((found[0].x, found[0].y))
        if not words or len(marks) != 1 or not text or _NOTHING_SAID.match(text.strip(" .")):
            return None
        given = {w for w in re.findall(r"[a-z]{3,}", marks[0].words.lower())}
        said = {w for w in re.findall(r"[a-z]{3,}", text.lower())}
        return marks[0] if len(given & said) >= 2 else None

    def apply_marks_at(
        self, before: WorldMap, delta: WorldDelta, reply: dict[str, Any], author: Node | None
    ) -> None:
        """Someone the read sends to a marked spot (its coordinate, never a
        named spot's words: those are `area_after_read`'s) is put in the
        place it lies in, once every new place has its spot."""
        marks = self._turn_marks(author)
        at = reply.get("at") if isinstance(reply.get("at"), dict) else {}
        if not marks or not at:
            return
        art = self._art_read()
        after = applied(before, delta)
        for name, value in at.items():
            mark = self._mark_of(marks, value)
            person = self._map_person(str(name))
            if mark is None or person is None or person not in delta.where:
                continue  # placed by the read with a quote, or not at all
            place_id = place_at(after, art, mark.x, mark.y)
            if place_id is not None:
                delta.where[person] = place_id

    def _mark_spot(self, mark: Mark, world: WorldMap, art: WorldArt) -> Spot | None:
        """The tile a mark lies on, on the map as it is now: in the place
        there, or on the window of open country there."""
        place_id = place_at(world, art, mark.x, mark.y)
        area = self.area(area_of(world, art, place_id, (mark.x, mark.y)), world, art)
        tile = point_tile(world, area, mark.x, mark.y) if area is not None else None
        return Spot(area=area.id, x=tile[0], y=tile[1]) if tile else None

    def spot_of_point(self, x: float, y: float) -> Spot | None:
        """The tile a point on the map lies on: in the innermost place there
        with an area (a building's inside, not its roof)."""
        return self._mark_spot(Mark(token="", x=x, y=y), self.world(), self._art_read())

    def go_to_point(self, x: float, y: float) -> str:
        """Set off for a spot on the map: the author's character stands on it
        (on the nearest tile they can, in the area it lies in), and the clock
        moves on by the way's length on foot. The turn's text."""
        world, art = self.world(), self._art_read()
        place_id = place_at(world, art, x, y)
        held = self.held_name()
        if held is None:
            return ""
        state = self.area_state()
        me = state.spots.get(held)
        here = self.area(me.area, world, art) if me is not None else None
        spot = self._mark_spot(Mark(token="", x=x, y=y), world, art)
        if here is not None and spot is not None and spot.area == here.id:
            return ""  # in this area: walking is the way
        start = self.held_point(state, world, art)
        metres = math.hypot(x - start[0], y - start[1]) * WINDING if start else 0.0
        seconds = int(metres / WALK_M_PER_S)
        area = self.area(spot.area, world, art) if spot is not None else None
        landing = None
        if spot is not None and area is not None:
            taken = {
                (s.x, s.y) for n, s in state.spots.items() if n != held and s.area == spot.area
            }
            sx, sy = stand_near(area, (spot.x, spot.y), taken)
            landing = Spot(area=spot.area, x=sx, y=sy)
        place = world.places.get(place_id or "")
        # Open country: named by what holds it (a region), else as such.
        name = place.name if place is not None else (area.within if area else "") or "open country"
        token = mark_token(x, y)
        text = self.texts.fill("map.turn.go", name=name, mark=token)
        # Applied when the turn is sent (`stamp_turn`), as Travel… is.
        state.action = MapAction(
            verb="travel",
            target=name,
            target_id=place_id or "",
            text=text,
            spot=landing,
            seconds=seconds,
        )
        self._set_live(state)
        return text

    # --- the clock ---------------------------------------------------------------

    def map_clock(self) -> tuple[int, int] | None:
        """(day, minute of the day) on the map's clock: the plot's, when the
        story has a plot; else the walking and the time the passages told.
        Shown to the author; never to the storyteller."""
        if self.story.plot is not None:
            try:
                minutes = self.chronicle.minutes
            except AttributeError:
                return None
        else:
            minutes = self.area_state().seconds // 60
        return minutes // (24 * 60) + 1, minutes % (24 * 60)

    # --- drawing what play needs (engine/tile_draw.py) ---------------------------

    def _near_areas(self, world: WorldMap, art: WorldArt) -> list[Area]:
        """The areas the story is in or next to: the author's character's,
        the buildings in it with an inside, the one outside it."""
        state = self.area_state()
        me = state.spots.get(self.held_name() or "")
        here = self.area(me.area if me else None, world, art)
        if here is None:
            return []
        found = [here]
        outer = self.area(here.parent, world, art) if here.indoor else None
        if outer is not None:
            found.append(outer)
        for area in list(found):
            for entrance in area.entrances:
                inner = self.area(entrance.place_id, world, art)
                if inner is not None and inner not in found:
                    found.append(inner)
        return found

    def tile_art_jobs(self) -> tuple[list[Area], list[PictureJob]]:
        """(buildings still to furnish, sprites still to draw), nearest the
        story first. Furnishing is a text model's (Draw the map); sprites are
        map pictures (the standing approval and its daily limit)."""
        if not self.map_first or not self.config.map_art or self.open_span is not None:
            return [], []
        world, art = self.world(), self._art_read()
        areas = self._near_areas(world, art)
        furnish = [
            a
            for a in areas
            if a.indoor
            and a.rooms
            and a.place_id in world.places
            and art.furnishing_for(a.id, world.places[a.place_id].state) is None
            and art.failures.get(f"furnish:{a.id}", 0) < 2
        ]
        if not self.map_pictures_on:
            return furnish, []
        room = self.map_pictures_left_today(art)
        if room <= 0 or self.tile_set_waiting():
            return furnish, []
        info, read = self.tile_set(art)
        source_items = {p.subject_id for p in art.pictures if p.subject == "item"}

        def has_sprite(name: str) -> bool:
            return any(s in source_items or read(f"items/{s}") for s in item_slugs(name))

        def failed(job_key: str) -> bool:
            return art.failures.get(job_key, 0) >= 2

        words = world_words(self._set_words(art, info), self.map_setting())
        style = style_words(self._set_words(art, info))
        jobs = item_jobs(areas, has_sprite, words, self.texts, style=style, failed=failed)
        here = {name for name, spot in self.area_state().spots.items()}
        people = []
        # Not before the story begins: Start offers portraits, and live the
        # avatars were drawn from the cards' words while Setup and Start were
        # still open, then again from the portraits.
        begun = bool(self.path())
        coming = self.portraits_coming | (self.portraits_landed() if begun else set())
        for card in (*self.visible_cast(), *self.visible_supporting()) if begun else ():
            if card.id in coming:
                continue  # drawn from the portrait once it lands and is taken
            portrait = card.reference_images[0].file if card.reference_images else None
            people.append((card.name, portrait, looks_of(card.summary, card.full_description)))
        people.sort(key=lambda p: p[0] not in here)

        portraits = {name: portrait for name, portrait, _looks in people}

        def has_walker(name: str) -> bool:
            return _avatar_stands(art, name, portraits.get(name))

        roofs = roof_jobs(
            areas,
            lambda role: read(role) is not None,
            words,
            self.texts,
            style=style,
            failed=failed,
        )
        # Ground the set has none of (sand under a snow set), and a deck's
        # floor in a set with no deck: drawn in the set's style, which is
        # kept for the whole story.
        grounds = ground_jobs(
            areas,
            info,
            lambda role: read(role) is not None,
            words,
            self.texts,
            style=style,
            failed=failed,
        )
        jobs = (
            walker_jobs(
                people,
                has_walker,
                words,
                self.texts,
                style=style,
                failed=failed,
                portraits=self.map_portraits_go(),
            )
            + grounds
            + roofs
            + jobs
        )
        # The author's own draws of the same thing or figure, being drawn or
        # waiting their turn (Objects… queues several).
        running = self._asset_thread is not None and self._asset_thread.is_alive()
        asked = {role for role, _words in self._asset_queue} if running else set()
        drawing = self.tile_asset_drawing
        if drawing is not None:
            asked.add(drawing)
        if asked:
            keys = {_asset_key(role) for role in asked}
            jobs = [j for j in jobs if j.key not in keys]
        return furnish, jobs[: min(RUN_LIMIT, room)]

    def _start_tile_art(self) -> bool:
        """Furnish and draw what the areas near the story need, on a thread
        of its own (called with the map's art, under its start lock)."""
        if self._map_closed:
            return False
        thread = self._tile_thread
        if thread is not None and thread.is_alive():
            # Asked again while one runs (a portrait landed while the avatars
            # were drawn from the words): the window starts the next run once
            # this one is done (`map_art_wanted_again`).
            self._tile_again = True
            return False
        self._tile_again = False
        furnish, pictures = self.tile_art_jobs()
        # Asking first: only what the author approved is drawn, the rest waits.
        pictures = self._held_for_approval("tile", pictures)
        if not furnish and not pictures:
            return False
        self._tile_total, self._tile_done = len(furnish) + len(pictures), 0
        self._tile_keys = {job.key for job in pictures}
        detached = getattr(self.provider, "detached", None)
        self._tile_text = detached() if callable(detached) and furnish else None
        media = self.config.images()
        client = (
            ImageClient(media.base_url, media.api_key, api=media.api)
            if pictures and media is not None
            else None
        )
        self._tile_client = client
        self._art_stop = False
        if self._tile_text is None and furnish:
            # A provider without `detached` (the scripted mock): inline.
            self._tile_run(furnish, [], None, self.provider)
            return True
        thread = threading.Thread(
            target=self._tile_run,
            args=(furnish, pictures if client is not None else [], client, self._tile_text),
            name="map-tiles",
            daemon=True,
        )
        self._tile_thread = thread
        thread.start()
        return True

    @property
    def tile_art_running(self) -> bool:
        """The play view's pieces are being furnished or drawn, or a run to
        draw them is due next (`_tile_again`)."""
        thread = self._tile_thread
        return (thread is not None and thread.is_alive()) or self._tile_again

    def start_tile_art(self) -> bool:
        """What the play view needs now (a building walked into), drawn in
        the background."""
        with self._starting():
            return self._start_tile_art()

    def _tile_run(
        self,
        furnish: list[Area],
        pictures: list[PictureJob],
        client: ImageClient | None,
        text: ChatProvider | None,
    ) -> None:
        try:
            for area in furnish:
                if self._art_stop:
                    return
                if text is not None:
                    self._doing("tile", f"furnishing {area.name}, by {self.map_art_model}")
                    self._tile_job(f"furnish:{area.id}", self._furnish_one, area, text)
                self._tile_done += 1
            if furnish:
                # What was furnished now has pieces with no picture: they are
                # drawn by the next run, which the window starts as soon as
                # this one ends (`map_art_wanted_again`). Before, they waited
                # for whatever next started a run (AfterDark, Oct 18 2026: the
                # pantry's, bedroom's and workshop's pieces were drawn four
                # minutes later, when Settings was saved).
                self._tile_again = True
            if client is None:
                return
            for job in pictures:
                if self._art_stop:
                    return
                model = self.config.map_picture_model
                self._doing("tile", f"{job_words(job)}, by {self.model_name(model)}")
                self._tile_job(
                    job.key,
                    self._picture_one,
                    job,
                    client,
                    TEXTURE_SIZE if job.subject == "texture" else SPRITE_SIZE,
                    model=model,
                    info=self._listed_image_model(model),
                    extra={"enable_prompt_expansion": False},
                )
                self._tile_done += 1
        finally:
            self._doing("tile", None)
            if client is not None:
                client.close()
            _close_quietly(text)

    def _tile_job(self, key: str, work, *args, **kwargs) -> None:
        """One job of the tile run: an exception it doesn't expect (a disk
        full, a reply of the wrong shape) used to end the thread silently,
        uncounted, and the job was asked again every passage. Logged and
        counted as a failure instead."""
        try:
            work(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if self._art_stop or self._closed:
                return
            with self._lock():
                art = self.world_art
                art.failures[key] = art.failures.get(key, 0) + 1
                self._save_art(art)
            self._log("map_tile_error", {"job": key, "error": f"{type(exc).__name__}: {exc}"[:300]})

    def _furnish_one(self, area: Area, client: ChatProvider) -> None:
        """One building furnished: the plan asked of the map art model, held
        to the area's rooms, kept with the map's art."""
        world = self.world()
        place = world.places.get(area.place_id)
        if place is None:
            return
        model = self.map_art_model
        # A level of the plan is furnished on its own, under its area id
        # (engine/levels.py), told which level it is.
        job_key = f"furnish:{area.id}"
        request = ChatRequest(
            model=model,
            extra_body=self.route_for("map_art"),
            messages=furnish_messages(
                area, place, self.map_setting(), self.texts, level=area.level_name
            ),
            params=self.side_params(model, max_tokens=FURNISH_MAX_TOKENS),
        )
        log_ref = self._log(
            "map_furnish_request",
            {
                "place_id": place.id,
                "area_id": area.id,
                "payload": self.provider.build_payload(request),
            },
        )
        art = self.world_art
        try:
            reply, completed = client.complete(request)
            self._log(
                "map_furnish_response",
                {"request_log_ref": log_ref, "text": reply, "usage": completed.raw_usage},
            )
            self.unreported_usage.append(self.priced(completed.usage, model))
            plan = parse_furnishing(
                reply,
                area,
                place_id=area.id,
                state=place.state,
                model=model,
                made_at=utc_now_iso(),
            )
        except (ProviderError, ValueError) as exc:
            if self._art_stop:
                return
            with self._lock():
                art.failures[job_key] = art.failures.get(job_key, 0) + 1
                self._save_art(art)
            self._log("map_furnish_error", {"request_log_ref": log_ref, "error": str(exc)[:300]})
            return
        # `_lock()` first, never inside `_closing`: `_save_art` takes
        # `_closing` under `_lock()`, and the picture thread saves that way
        # at the same time (an ABBA deadlock, found in the second review).
        if self._art_stop or self._closed:
            return
        with self._lock():
            art.furnishings.setdefault(area.id, {})[place.state] = plan
            self._save_art(art)
        callback = self.on_map_art
        if callback is not None:
            callback()

    # --- the tile set (engine/tile_sets.py, engine/tile_draw.py) -----------------

    def tile_set_choices(self, art: WorldArt | None = None) -> list[tuple[str, str]]:
        """(name, title) of every set the map may be drawn in: the shipped
        ones, and each of this story's own ("own:<key>")."""
        out = [(name, info.title or name) for name, (info, _f) in shipped.shipped_sets().items()]
        art = art or self._art_read()
        for set_key, found in art.tile_sets.items():
            if found.assets:
                out.append((own_set_name(set_key), found.title or "This story's own"))
        return out

    def tile_set_cards(self) -> list[TileSetCard]:
        """Every set as the gallery shows it: the shipped sets, then this
        story's own, each with a reader of its assets by role."""
        art = self._art_read()
        in_use = self.tile_set_name(art)
        out: list[TileSetCard] = []
        for name, (info, folder) in shipped.shipped_sets().items():
            out.append(
                TileSetCard(
                    name=name,
                    title=info.title or name,
                    in_use=name == in_use,
                    own=False,
                    style=style_words(info.words),
                    keywords=list(info.keywords),
                    read=lambda role, folder=folder: shipped.read_asset(folder, role),
                )
            )
        reader = self.pictures.read
        for set_key, found in art.tile_sets.items():
            if not found.assets:
                continue
            name = own_set_name(set_key)
            assets = dict(found.assets)
            out.append(
                TileSetCard(
                    name=name,
                    title=found.title or "This story's own",
                    in_use=name == in_use,
                    own=True,
                    style=style_words(found.words),
                    keywords=[],
                    read=lambda role, assets=assets: (
                        reader(assets[role]) if role in assets else None
                    ),
                )
            )
        return out

    def set_tile_set(self, name: str) -> None:
        """The author's choice of tile set ("" leaves it to the AI: the
        shipped set closest to the world, `start_tile_set_pick`, kept from
        then on as any choice is; the setting's words choose while it
        thinks, and if it can't)."""
        art = self.world_art
        with self._lock():
            art.tile_set, art.tile_set_auto, art.tile_set_why = name, False, ""
            if not name:
                self._pin_tile_set(art)
            self._save_art(art)
        if not name:
            self.start_tile_set_pick(again=True)

    def pin_tile_set(self) -> None:
        """The tile set is chosen once the story has begun and kept for it
        (the author, Oct 17 2026: "tilesets shouldn't swap mid game. A
        tileset should be chosen at the start and used in that game, and new
        items generated when needed if a different type of area is
        encountered"): the program's choice is written to the art, so a plan
        that comes in later can't change it. Live, a lighthouse story was
        never given a set and chose one afresh on every draw. The author's
        own choice is never touched."""
        if not self.map_first or self.in_private or not self.path():
            return
        art = self.world_art
        # The author's own set being drawn is chosen once it is in; the AI's
        # choice is written when it comes back.
        if art.tile_set or self.tile_set_waiting() or self.start_tile_set_pick():
            return
        with self._lock():
            if art.tile_set or not self._pin_tile_set(art):
                return
            self._save_art(art)

    def _pin_tile_set(self, art: WorldArt) -> bool:
        """The program's choice written to `art` (under the lock); False
        when nothing is shipped to choose from."""
        name = self.tile_set_name(art)
        if not name:
            return False
        art.tile_set, art.tile_set_auto = name, True
        return True

    # --- the AI's choice of the closest set ---------------------------------------

    @property
    def tile_set_picking(self) -> bool:
        """The AI is choosing the set now (`start_tile_set_pick`)."""
        thread = getattr(self, "_pick_thread", None)
        return thread is not None and thread.is_alive()

    def tile_set_waiting(self) -> bool:
        """The map's set isn't settled yet: the AI is choosing it, or the
        author's own set is being drawn to be used. Its items and avatars
        wait (`tile_art_jobs`), or they are drawn in the style of a set the
        map is about to leave."""
        thread = getattr(self, "_own_set_thread", None)
        return self.tile_set_picking or (thread is not None and thread.is_alive())

    def start_tile_set_pick(self, *, again: bool = False) -> bool:
        """The map model chooses the shipped set closest to the world, on a
        thread (the author, Oct 18 2026: "have the AI choose the closest
        set"), once a session unless asked `again`; True when it started.
        Only while the program chooses: never over the author's choice, a
        sample's set or one of the story's own. Its answer is the program's
        choice, pinned with why (`WorldArt.tile_set_why`); if it fails, the
        setting's words choose as before."""
        if not self.map_first or self.in_private or self._map_closed or self.tile_set_picking:
            return False
        if getattr(self, "_pick_tried", False) and not again:
            return False
        art = self._art_read()
        if art.tile_set and not art.tile_set_auto:
            return False
        sets = {name: info for name, (info, _folder) in shipped.shipped_sets().items()}
        setting = self._setting_words().strip() or self.map_setting()
        # On a client of its own: a provider with none (the scripted mock)
        # leaves the choice to the setting's words.
        detached = getattr(self.provider, "detached", None)
        if len(sets) < 2 or not setting or not callable(detached):
            return False
        self._pick_tried = True
        text = detached()
        model = self.map_model
        request = ChatRequest(
            model=model,
            extra_body=self.route_for("map", model),
            messages=pick_messages(setting[:PICK_SETTING_LIMIT], sets, self.texts),
            params=self.side_params(model, max_tokens=PICK_MAX_TOKENS),
        )
        thread = threading.Thread(
            target=self._pick_run,
            args=(text, request, list(sets)),
            name="map-tileset-pick",
            daemon=True,
        )
        self._pick_thread = thread
        thread.start()
        return True

    def _pick_run(self, text: ChatProvider, request: ChatRequest, names: list[str]) -> None:
        chosen: tuple[str, str] | None = None
        try:
            log_ref = self._log(
                "map_tileset_pick_request", {"payload": self.provider.build_payload(request)}
            )
            try:
                reply, completed = text.complete(request)
                self._log(
                    "map_tileset_pick_response",
                    {"request_log_ref": log_ref, "text": reply, "usage": completed.raw_usage},
                )
                self.unreported_usage.append(self.priced(completed.usage, request.model))
                chosen = parse_pick(reply, names)
            except (ProviderError, ValueError) as exc:
                self._log(
                    "map_tileset_pick_error",
                    {"request_log_ref": log_ref, "error": str(exc)[:300]},
                )
        except Exception as exc:  # noqa: BLE001 - a thread dies silently otherwise
            self._log("map_tileset_pick_error", {"error": f"{type(exc).__name__}: {exc}"[:300]})
        finally:
            _close_quietly(text)
        if self._map_closed or self._closed:
            return
        art = self.world_art
        with self._lock():  # never inside `_closing` (see `_furnish_one`)
            if art.tile_set and not art.tile_set_auto:
                return  # the author chose meanwhile
            if chosen is not None:
                art.tile_set, art.tile_set_auto, art.tile_set_why = chosen[0], True, chosen[1]
            elif not self.path() or not self._pin_tile_set(art):
                return
            self._save_art(art)
        self._tile_again = True  # what waited for the set is drawn in it now
        callback = self.on_map_art
        if callback is not None:
            callback()

    def remove_own_tile_set(self, name: str) -> bool:
        """One of the story's own sets taken out, its pictures deleted from
        the story (never a shipped set); the map falls back to the program's
        choice if it was drawn in it."""
        key = own_set_key(name)
        if key is None:
            return False
        art = self.world_art
        with self._lock():
            found = art.tile_sets.pop(key, None)
            if found is None:
                return False
            if own_set_key(art.tile_set) == key:
                art.tile_set, art.tile_set_auto = "", False
                self._pin_tile_set(art)
            self._save_art(art)
        for file in found.assets.values():
            if file:
                self.pictures.remove(file)
        return True

    def tile_sets_at_start(self) -> list[tuple[str, str]]:
        """(name, title) of the sets Start offers for a new world's map, its
        look chosen before anything is drawn; none when the story keeps no
        map, or its map has its set already (a sample's, or the author's)."""
        if not self.map_first or self.in_private:
            return []
        art = self._art_read()
        if art.tile_set and not art.tile_set_auto:
            return []
        return self.tile_set_choices(art)

    # --- the offer when a story's map begins ---------------------------------------

    def tile_set_offer(self, art: WorldArt | None = None) -> str | None:
        """The line the play view shows once, when the map begins and no set
        has been chosen: which set the map is drawn in and that the gallery
        has others; None once answered, or once a set is chosen."""
        if self.story.tile_set_offered or self.story.chat:
            return None
        art = art or self._art_read()
        if art.tile_set and not art.tile_set_auto:
            return None
        if self.tile_set_picking:
            return "The AI is choosing the tile set closest to this world…"
        titles = dict(self.tile_set_choices(art))
        name = self.tile_set_name(art)
        title = titles.get(name, name)
        if not title:
            return None
        if art.tile_set_why:
            why = art.tile_set_why.rstrip(".")
            return (
                f"The map is drawn in {title}, the set the AI found closest to this world: "
                f"{why}. Choose another, or design one for this world."
            )
        return f"The map is drawn in {title}. Choose another, or design one for this world."

    def tile_sets_offered(self) -> None:
        """The offer was answered (the gallery opened, or the line dismissed):
        not shown again for this story."""
        if not self.story.tile_set_offered:
            self.story.tile_set_offered = True
            self.save()

    # --- a new own set: designed, approved, drawn ----------------------------------

    @property
    def tileset_designer(self) -> str:
        """The model that designs a set: the author's (`Config.tileset_model`),
        else Claude Opus 5.5 on NanoGPT (the author's ruling), else the map
        art model."""
        chosen = (self.config.tileset_model or "").strip()
        if chosen:
            return chosen
        provider = self.config.active_provider()
        if provider is not None and on_nanogpt(provider.base_url):
            return DESIGNER_MODEL
        return self.map_art_model

    def own_tile_set_offer(self) -> tuple[int, float | None, str | None]:
        """(pictures a whole set is, the listed price for them, why a set
        can't be designed or drawn now)."""
        count = len(TEXTURES) + 1 + 3 + len(OUTDOOR_SPRITES)
        model = self._listed_image_model(self.config.map_picture_model)
        texture = model.price(TEXTURE_SIZE) if model is not None else None
        sprite = model.price(SPRITE_SIZE) if model is not None else None
        total = (
            texture * len(TEXTURES) + sprite * (count - len(TEXTURES))
            if texture is not None and sprite is not None
            else None
        )
        if self.in_private or self.tee_chat:
            return count, total, "Not in a private scene or a private chat."
        media = self.config.images()
        if media is None or not media.api_key:
            return count, total, "Pictures have no endpoint or key (Settings → Images)."
        thread = getattr(self, "_own_set_thread", None)
        if thread is not None and thread.is_alive():
            return count, total, "A tile set is being drawn for this story now."
        thread = getattr(self, "_design_thread", None)
        if thread is not None and thread.is_alive():
            return count, total, "A tile set is being designed now."
        return count, total, None

    def tile_set_price(self, words: Mapping[str, str]) -> tuple[int, float | None]:
        """(the pictures a design is, their price together at the tile
        model's listed prices, or None when it lists none)."""
        jobs = tile_jobs(words, self.texts)
        info = self._listed_image_model(self.config.map_picture_model)
        price = tile_set_price(jobs, info.price if info is not None else lambda _s: None)
        return len(jobs), price

    def start_tile_set_design(
        self,
        description: str,
        model: str = "",
        pictures: Sequence[tuple[bytes, str]] = (),
    ) -> bool:
        """The designer writes a set's words and a prompt for every picture
        from the world and the author's description, on a thread (nothing is
        drawn: the author sees and approves them first, `tile_set_design`).
        `pictures`: the author's pictures of the world, each with what they
        said it shows (the author, Oct 18 2026: "This is a photo of what a
        typical bar looks like in this setting"), stripped and sized by the
        window; shown to the designer only, never to the image model."""
        if self.own_tile_set_offer()[2] is not None or self._map_closed:
            return False
        detached = getattr(self.provider, "detached", None)
        text = detached() if callable(detached) else self.provider
        self._design_text = text
        self._design_words, self._design_error = None, None
        designer = " ".join(model.split()) or self.tileset_designer
        shown = [
            (data_url(data, MEDIA_TYPES[picture_kind(data)]), caption)
            for data, caption in pictures
            if picture_kind(data) in MEDIA_TYPES
        ]
        thread = threading.Thread(
            target=self._design_run,
            args=(text, designer, " ".join(description.split()), shown),
            name="map-tileset-design",
            daemon=True,
        )
        self._design_thread = thread
        thread.start()
        return True

    def cancel_tile_set_design(self) -> None:
        """The designer's call cut short (the author closed the designer
        while it wrote): its reply, if any, is dropped."""
        text = self._design_text
        cancel = getattr(text, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:  # noqa: BLE001 - stopping must never fail
                pass
        self._design_words = None

    @property
    def tile_set_design(self) -> tuple[str, dict[str, str] | None, str | None]:
        """("writing", "done", "failed" or "", the words with a prompt per
        picture once written, why it failed)."""
        thread = self._design_thread
        if thread is not None and thread.is_alive():
            return "writing", None, None
        if self._design_words is not None:
            return "done", dict(self._design_words), None
        if self._design_error is not None:
            return "failed", None, self._design_error
        return "", None, None

    def _design_run(
        self,
        text: ChatProvider,
        model: str,
        description: str,
        pictures: Sequence[tuple[str, str]] = (),
    ) -> None:
        try:
            request = ChatRequest(
                model=model,
                extra_body=self.route_for("map_art", model),
                messages=tileset_messages(
                    self.map_setting(), self.texts, description=description, pictures=pictures
                ),
                params=self.side_params(model, max_tokens=FURNISH_MAX_TOKENS),
            )
            # The pictures' bytes stay out of the log, as a picture request's
            # references do; what the author said of each is kept.
            log_ref = self._log(
                "map_tileset_request",
                {"payload": without_pictures(self.provider.build_payload(request))},
            )
            try:
                reply, completed = text.complete(request)
                self._log(
                    "map_tileset_response",
                    {"request_log_ref": log_ref, "text": reply, "usage": completed.raw_usage},
                )
                self.unreported_usage.append(self.priced(completed.usage, model))
                self._design_words = parse_tile_words(reply)
            except (ProviderError, ValueError) as exc:
                self._log(
                    "map_tileset_error", {"request_log_ref": log_ref, "error": str(exc)[:300]}
                )
                self._design_error = str(exc)[:300]
        except Exception as exc:  # noqa: BLE001 - a thread dies silently otherwise
            self._log("map_tileset_error", {"error": f"{type(exc).__name__}: {exc}"[:300]})
            self._design_error = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            _close_quietly(text)
            self._design_text = None

    def start_own_tile_set(self, words: Mapping[str, str], title: str) -> bool:
        """Draw a tile set for this story from a design the author approved
        (the words, and the prompt of every picture, with the price shown):
        each is drawn exactly as approved, into a new named set, and the map
        is drawn in it once the set is whole."""
        if self.own_tile_set_offer()[2] is not None or self._map_closed:
            return False
        design = {k: " ".join(v.split()) for k, v in words.items()}
        if not tile_jobs(design, self.texts):
            return False
        media = self.config.images()
        client = ImageClient(media.base_url, media.api_key, api=media.api)
        self._own_set_client = client
        self._art_stop = False
        thread = threading.Thread(
            target=self._own_set_run,
            args=(design, " ".join(title.split()), client),
            name="map-tileset",
            daemon=True,
        )
        self._own_set_thread = thread
        self._own_set_done, self._own_set_total = 0, 1
        thread.start()
        return True

    def _own_set_run(self, words: dict[str, str], title: str, client: ImageClient) -> None:
        try:
            jobs = tile_jobs(words, self.texts)
            self._own_set_total = len(jobs)
            self._own_set_done = 0
            assets: dict[str, str] = {}
            model = self.config.map_picture_model
            info = self._listed_image_model(model)
            for role, prompt, size in jobs:
                if self._art_stop:
                    return
                file = self._draw_tile(client, role, prompt, size, model, info)
                if file is not None:
                    assets[role] = file
                self._own_set_done += 1
            missing = [role for role in TEXTURES if words.get(role) and role not in assets]
            if missing:
                # Not whole: the set the map has stays, and the log says why.
                self._log("map_tileset_error", {"error": "not drawn: " + ", ".join(missing)})
                return
            art = self.world_art
            if self._art_stop or self._closed:
                return
            with self._lock():  # never inside `_closing` (see `_furnish_one`)
                key = own_key(title or words.get("world", ""), art.tile_sets)
                art.tile_sets[key] = OwnTileSet(
                    title=title or "This story's own",
                    assets=assets,
                    words=words,
                    made_at=utc_now_iso(),
                )
                art.tile_set, art.tile_set_auto = own_set_name(key), False
                self._save_art(art)
            self._tile_again = True  # what waited for the set is drawn in it now
            callback = self.on_map_art
            if callback is not None:
                callback()
        except Exception as exc:  # noqa: BLE001 - a thread dies silently otherwise
            self._log("map_tileset_error", {"error": f"{type(exc).__name__}: {exc}"[:300]})
        finally:
            client.close()

    def _draw_tile(
        self,
        client: ImageClient,
        role: str,
        prompt: str,
        size: str,
        model: str,
        info,
        reference: bytes | None = None,
    ) -> str | None:
        """One picture of the story's own tile set (or one asset redrawn),
        logged, stripped and kept under images/map/tileset/; its file, or
        None. `reference`: a picture it is drawn from (a portrait)."""
        references = []
        if reference is not None:
            references.append(data_url(reference, MEDIA_TYPES[picture_kind(reference)]))
        # The model's text or edit version, for whether a picture goes with it.
        model = fitting_model(model, len(references), self.image_listing())
        payload = build_image_payload(model, prompt, size, 1, references, api=client.api)
        if client.api == "nanogpt":
            payload["enable_prompt_expansion"] = False
        log_ref = self._log(
            "map_picture_request",
            {
                "job": f"tile:{role}",
                "payload": loggable_payload(payload, ["the portrait"] if reference else []),
            },
        )
        try:
            reply = client.generate(payload)
            data = strip_metadata(reply.images[0])
        except (ProviderError, MetadataError, IndexError) as exc:
            self._log("map_picture_error", {"request_log_ref": log_ref, "error": str(exc)[:300]})
            return None
        if reply.cost is not None:
            usage: dict = {"cost": reply.cost, "cost_reported": True}
        elif info is not None and info.price(size) is not None:
            usage = {"cost": info.price(size), "cost_estimated": True}
        else:
            usage = {"cost_reported": False}
        name = slug(role) or "asset"
        relative = f"{MAP_PICTURES_DIR}/tileset/{name}-{new_id()[:8]}{picture_kind(data) or '.png'}"
        with self._closing:
            if self._art_stop or self._closed or not self.pictures.alive():
                return None
            self._log("map_picture_response", {"request_log_ref": log_ref, "usage": usage})
            self.pictures.write(relative, data)
        self.unreported_usage.append(
            Usage(
                cost=float(usage.get("cost", 0.0)),
                cost_estimated=bool(usage.get("cost_estimated")),
                cost_reported=bool(usage.get("cost_reported")),
            )
        )
        return relative

    # --- one asset at a time (Look ▾ → Edit the tile set…) -------------------------

    def tile_set_assets(self) -> list[TileAsset]:
        """Every asset the map is drawn with, as the editor lists them: the
        set's ground, walls and decoration, every thing it or the story has
        a sprite for (and every thing in the areas near the story), and
        every character's walking sprite."""
        art = self._art_read()
        info, read = self.tile_set(art)
        name = self.tile_set_name(art)
        found = shipped.shipped_sets().get(name)
        folder = found[1] if found else None
        words = dict(info.words)
        own = art.own_set_for(name)
        own_assets = own[1].assets if own is not None else {}
        story_items = {p.subject_id for p in art.pictures if p.subject == "item"}
        out: list[TileAsset] = []

        def source(role: str, drawn: bool) -> str:
            changed = art.tile_overrides.get(role)
            if changed:
                return "changed for this story"
            if changed is None and (drawn or role in own_assets):
                return "drawn for this story"
            return "the set's"

        for role in (*TEXTURES, *SPRITES):
            out.append(
                TileAsset(
                    role=role,
                    label=ASSET_LABELS.get(role, role),
                    group="Ground and walls" if role in TEXTURES else "Decoration and nature",
                    source=source(role, False),
                    words=words.get(role, ""),
                )
            )
        slugs = set(shipped.asset_names(folder, "items")) if folder is not None else set()
        slugs = {s.removeprefix("items/") for s in slugs} | story_items
        for area in self._near_areas(self.world(), art):
            for placement in area.placements:
                keys = item_slugs(placement.name)
                if keys and not any(k in slugs for k in keys):
                    slugs.add(keys[0])
        for key_ in sorted(slugs):
            role = f"items/{key_}"
            out.append(
                TileAsset(
                    role=role,
                    label=key_.replace("-", " "),
                    group="Things",
                    source=source(role, key_ in story_items),
                    words=key_.replace("-", " "),
                )
            )
        for card in (*self.visible_cast(), *self.visible_supporting()):
            role = f"walker/{card.name}"
            out.append(
                TileAsset(
                    role=role,
                    label=card.name,
                    group="People",
                    source=source(role, art.picture_for("walker", card.name) is not None),
                    words=looks_of(card.summary, card.full_description),
                )
            )
        return out

    def tile_asset_bytes(self, role: str) -> bytes | None:
        """The picture an asset is drawn with now."""
        if role.startswith("walker/"):
            file = self.walker_file(role[7:])
            return self.pictures.read(file) if file else None
        _info, read = self.tile_set()
        return read(role)

    def _asset_job(self, role: str, words: str) -> tuple[str, str, str, bytes | None, str]:
        """(prompt, size, model, the picture it is drawn from, that
        picture's file or "") for redrawing one asset from these words."""
        art = self._art_read()
        info, _read = self.tile_set(art)
        world = world_words(self._set_words(art, info), self.map_setting())
        style = style_words(self._set_words(art, info))
        what = " ".join(words.split()) or role.split("/")[-1].replace("-", " ")
        if role in TEXTURES:
            view = (
                "straight from the front"
                if role in ("face", "out-wall")
                else "from directly overhead"
            )
            prompt = self.texts.fill(
                "map.tile.texture", what=what, world=world, view=view, style=style
            )
            return prompt, TEXTURE_SIZE, self.config.map_picture_model, None, ""
        if role == "rug":
            prompt = self.texts.fill("map.tile.rug", what=what, world=world, style=style)
        elif role in OUTDOOR_SPRITES:
            prompt = self.texts.fill("map.tile.nature", what=what, world=world, style=style)
        elif role.startswith("walker/"):
            name = role[7:]
            card = next(
                (c for c in (*self.visible_cast(), *self.visible_supporting()) if c.name == name),
                None,
            )
            portrait = card.reference_images[0].file if card and card.reference_images else None
            looks = looks_of(card.summary, card.full_description) if card else ""
            # No words of the author's own: the card's, and so the portrait.
            what = " ".join(words.split()) or looks
            data = (
                self.pictures.read(portrait)
                if portrait and what == looks and self.map_portraits_go()
                else None
            )
            if data is not None:
                try:
                    data = strip_metadata(data)
                except MetadataError:
                    data = None  # never sent unstripped: drawn from the words
            if data is not None and portrait:
                prompt = self.texts.fill("map.tile.portrait", style=style)
                return prompt, SPRITE_SIZE, self.config.map_picture_model, data, portrait
            who = f"{name}, {what}" if what else name
            prompt = self.texts.fill("map.tile.person", what=who, world=world, style=style)
        else:
            prompt = self.texts.fill("map.tile.sprite", what=what, world=world, style=style)
        return prompt, SPRITE_SIZE, self.config.map_picture_model, None, ""

    def map_portraits_go(self) -> bool:
        """Avatars are drawn from the portrait: unless the listing says the map
        picture model takes no picture, when they are drawn from the card's
        words (a portrait sent to such a model would be dropped, or refused).
        A model the listing doesn't know is taken as taking one (FLUX.3
        Klein, the default, before the listing is read)."""
        family = self.image_family(self.config.map_picture_model)
        return family is None or family.takes_pictures

    def tile_asset_offer(self, role: str, words: str) -> tuple[str, float | None, str | None]:
        """(the model it would be drawn by, its listed price, why it can't be
        drawn now)."""
        _prompt, size, model, _ref, _source = self._asset_job(role, words)
        listed = self._listed_image_model(model)
        price = listed.price(size) if listed is not None else None
        if self.in_private or self.tee_chat:
            return model, price, "Not in a private scene or a private chat."
        if role.startswith("walker/") and role[7:] in self._hidden_names():
            # Their picture or name would be the first a model heard of them.
            return model, price, "Not until the story brings them in (the plot keeps them hidden)."
        media = self.config.images()
        if media is None or not media.api_key:
            return model, price, "Pictures have no endpoint or key (Settings → Images)."
        if self.map_pictures_left_today(self._art_read()) <= 0:
            return model, price, "Today's map pictures are all drawn (Settings → Images)."
        run = self._tile_thread
        if run is not None and run.is_alive() and _asset_key(role) in self._tile_keys:
            return model, price, "It is being drawn now, under the standing approval."
        return model, price, None

    def _hidden_names(self) -> set[str]:
        """The names of the cards the plot keeps hidden now."""
        hidden = self.hidden_ids()
        return {c.name for c in (*self.cast, *self.supporting) if c.id in hidden}

    def _queue_lock(self) -> threading.Lock:
        if self._asset_queue_lock is None:
            self._asset_queue_lock = threading.Lock()
        return self._asset_queue_lock

    def redraw_tile_asset(self, role: str, words: str) -> bool:
        """Draw one asset again from these words (the author asked, with the
        price shown), in the background; the map takes it when it is in."""
        return self.redraw_tile_assets([(role, words)])

    def redraw_tile_assets(self, wanted: Sequence[tuple[str, str]]) -> bool:
        """Several assets drawn again, one after another on one thread (the
        avatars of every card a new portrait landed on): each counted
        against the day as it is sent, the map taking each as it is in.
        Asked while a run is drawing, they join its queue: live, portraits
        landing one after another were each offered an avatar, and every
        offer after the first was dropped ("Another asset is being drawn
        now"), so the author's own character got none. False when none of
        them can be drawn now."""
        hidden = {f"walker/{name}" for name in self._hidden_names()}
        wanted = [(role, " ".join(words.split())) for role, words in wanted if role not in hidden]
        if not wanted or self._map_closed:
            return False
        with self._queue_lock():
            thread = self._asset_thread
            if thread is not None and thread.is_alive() and not self._asset_closing:
                queued = set(self._asset_queue)
                self._asset_queue.extend(w for w in wanted if w not in queued)
                return True
        if self.tile_asset_offer(wanted[0][0], wanted[0][1])[2] is not None:
            return False
        media = self.config.images()
        client = ImageClient(media.base_url, media.api_key, api=media.api)
        self._asset_client = client
        self._art_stop = False
        art = self.world_art
        self._asset_queue = list(wanted[1:])
        self._asset_closing = False

        def run() -> None:
            try:
                role, words = wanted[0]
                while True:
                    if self._art_stop or self._closed:
                        return
                    self._asset_role = role
                    self._draw_asset(client, art, role, words)
                    with self._queue_lock():
                        if not self._asset_queue:
                            # Nothing more can join this run: a later ask
                            # starts its own.
                            self._asset_closing = True
                            return
                        role, words = self._asset_queue.pop(0)
                    with self._lock():
                        if self.map_pictures_left_today(art) <= 0:
                            return  # the day's pictures all drawn: the rest wait
                        day = _today()
                        art.pictures_by_day[day] = art.pictures_by_day.get(day, 0) + 1
                        self._save_art(art)
            finally:
                client.close()
                with self._queue_lock():
                    self._asset_closing = True
                    self._asset_queue = []

        with self._lock():
            day = _today()
            art.pictures_by_day[day] = art.pictures_by_day.get(day, 0) + 1
            self._save_art(art)
        thread = threading.Thread(target=run, name="map-tile-asset", daemon=True)
        self._asset_thread = thread
        self._asset_role = wanted[0][0]
        thread.start()
        return True

    def _draw_asset(self, client: ImageClient, art: WorldArt, role: str, words: str) -> None:
        """One asset of a redraw run: drawn, kept as the author's change,
        said to the window either way (the Cast page's avatar waits on it)."""
        try:
            prompt, size, model, reference, source = self._asset_job(role, words)
            file = self._draw_tile(
                client, role, prompt, size, model, self._listed_image_model(model), reference
            )
            if file is None or self._art_stop or self._closed:
                return
            with self._lock():  # never inside `_closing` (see `_furnish_one`)
                art.tile_overrides[role] = file
                if role.startswith("walker/"):
                    # Kept with the others drawn for them: Media lists every
                    # avatar and the author chooses among them (Oct 17 2026).
                    art.pictures.append(
                        _walker_picture(
                            role[7:], file, prompt=prompt, model=model, from_picture=source
                        )
                    )
                self._save_art(art)
        except Exception as exc:  # noqa: BLE001 - a thread dies silently otherwise
            self._log(
                "map_picture_error",
                {"job": f"tile:{role}", "error": f"{type(exc).__name__}: {exc}"[:300]},
            )
        finally:
            callback = self.on_map_art
            if callback is not None and not self._closed:
                callback()

    @property
    def tile_asset_drawing(self) -> str | None:
        """The asset being drawn now, or None."""
        thread = self._asset_thread
        return self._asset_role if thread is not None and thread.is_alive() else None

    def use_own_tile_asset(self, role: str, data: bytes) -> None:
        """A picture of the author's own for one asset (already stripped by
        the crop dialog), kept with the story's pictures."""
        kind = picture_kind(data) or ".png"
        relative = f"{MAP_PICTURES_DIR}/tileset/{slug(role) or 'asset'}-{new_id()[:8]}{kind}"
        self.pictures.write(relative, strip_metadata(data))
        art = self.world_art
        with self._lock():
            art.tile_overrides[role] = relative
            if role.startswith("walker/"):
                art.pictures.append(_walker_picture(role[7:], relative))
            self._save_art(art)

    def walker_pictures(self, name: str) -> list[MapPicture]:
        """Every avatar drawn or chosen for a person, newest first (the one
        in use is `walker_file`)."""
        art = self._art_read()
        found = [p for p in art.pictures if p.subject == "walker" and p.subject_id == name]
        return sorted(found, key=lambda p: p.made_at, reverse=True)

    def use_walker_picture(self, name: str, picture_id: str) -> bool:
        """One of the avatars kept for a person walks for them (the Cast
        page's choice, or Media's); False for a picture that isn't theirs."""
        art = self.world_art
        picture = next(
            (
                p
                for p in art.pictures
                if p.id == picture_id and p.subject == "walker" and p.subject_id == name
            ),
            None,
        )
        if picture is None:
            return False
        with self._lock():
            art.tile_overrides[f"walker/{name}"] = picture.file
            self._save_art(art)
        return True

    def reset_tile_asset(self, role: str) -> None:
        """Back to the set's own picture for this asset."""
        art = self.world_art
        with self._lock():
            art.tile_overrides[role] = ""
            self._save_art(art)


def _went_far(area: Area, p, spot: Spot) -> bool:
    """Whether a thing at `spot` is `MOVE_MIN_M` or more from where it stood,
    or in another area."""
    if spot.area != area.id:
        return True
    return math.hypot(spot.x - p.x, spot.y - p.y) * area.tile_m >= MOVE_MIN_M


def _names_room_of(area: Area, spot: str, p) -> bool:
    """Whether the spot named is the room the thing already stands in (the
    read gave where it is, not where it went)."""
    if not spot:
        return False
    room = area.room_at(int(p.centre[0]), int(p.centre[1]))
    if room is None:
        return False
    wanted = key(bare(spot))
    return wanted in (key(bare(room_words(area, room))), key(room.name), key(room.purpose))


def _twin(area: Area, name: str, world: WorldMap):
    """The area's own drawn thing of this name (a plan's object or a piece
    of furniture, not one of the map's things laid on it), or None: a map's
    thing that is one of those is shown as it, not laid beside it."""
    wanted = key(bare(name))
    own = [p for p in area.placements if p.id not in world.things]
    exact = next((p for p in own if key(bare(p.name)) == wanted), None)
    if exact is not None:
        return exact
    # The map's "Pickup truck" is the barn plan's "rusted pickup truck on
    # flat tyres" (live, both were drawn).
    return next((p for p in own if same_thing(name, p.name)), None)


def _avatar_stands(art: WorldArt, name: str, portrait: str | None) -> bool:
    """Whether a person's avatar is as it should be, with `portrait` (a
    file) the one their card leads with: the author's own choice stands (a
    redraw, their own picture, or the set's figure: ""); else every
    character has an avatar drawn from the portrait they have now (the
    author, Oct 17 2026), so one drawn here from their words before a
    portrait came, or from a portrait they no longer lead with, doesn't. A
    sample's, or one from before this was kept (`from_picture` None),
    stands."""
    if art.tile_overrides.get(f"walker/{name}") is not None:
        return True
    drawn = art.picture_for("walker", name)
    if drawn is None:
        return False
    return drawn.from_picture is None or not portrait or drawn.from_picture == portrait


def _walker_picture(
    name: str, file: str, *, prompt: str = "", model: str = "", from_picture: str | None = None
) -> MapPicture:
    """The record of an avatar drawn or chosen for a person, kept with the
    map's pictures (`subject` "walker"); `model` "" for the author's own.
    `from_picture`: the portrait it was drawn from, "" from words."""
    return MapPicture(
        subject="walker",
        subject_id=name,
        name=name,
        file=file,
        from_picture=from_picture,
        prompt=prompt,
        model=model,
        made_at=utc_now_iso(),
    )


def _own_info(name: str, found: OwnTileSet) -> TileSetInfo:
    """A story's own set as a manifest, from its words: its title, the light
    it gave, whether its water is molten."""
    words = dict(found.words)
    try:
        light = tuple(int(c) for c in words.get("light", "").split(","))
    except ValueError:
        light = ()
    return TileSetInfo(
        name=name,
        title=found.title or "This story's own",
        words=words,
        lava=words.get("lava") == "yes",
        **({"light": light} if len(light) == 3 else {}),
    )


def _asset_key(role: str) -> str:
    """The tile run's job key for an editor role ("walker/Jane" is
    "walker:Jane", "items/a-chest" is "item:a-chest")."""
    if role.startswith("walker/"):
        return f"walker:{role[7:]}"
    if role.startswith("items/"):
        return f"item:{role[6:]}"
    return role


# An in-character turn that takes the author's character in through the
# door they stand at (`stamp_turn`).
_GOES_IN = re.compile(
    # "I", then a few words of the same clause (never another subject: a
    # name, "him", "to" before the verb), then the way in.
    r"\b(?:I|[Ww]e)\b(?:\s+(?!to\b|him\b|her\b|them\b)[a-z']+){0,6}?\s+"
    r"(?:(?:step|go|walk|come|duck|slip|head|move|get|climb|edge)(?:s|ed)?\s+(?:back\s+)?"
    r"(?:in|inside|indoors)\b(?!\s+front)|enter(?:s|ed)?\b|went\s+in\b)",
)


def _has_an_inside(world: WorldMap, place_id: str) -> bool:
    """A building, room or vehicle: stood on the ground round it until its
    inside is drawn, then inside (`Spot.for_place`). Live (Oct 18 2026)
    Wren went into Cade's shop before its inside was drawn and stayed on the
    town's ground at its door, with Cade inside, once it was."""
    place = world.places.get(place_id)
    return place is not None and place.kind in ("building", "room", "vehicle")


def _close_quietly(client: ChatProvider | None) -> None:
    """A detached text client closed once its run is over (they were never)."""
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - closing is best effort
            pass


def item_sprite_key(name: str) -> str:
    """The slug an item's own sprite is kept under (`MapPicture.subject_id`)."""
    return item_slugs(name)[0] if item_slugs(name) else ""
