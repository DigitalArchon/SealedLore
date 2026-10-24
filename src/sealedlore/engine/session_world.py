"""The map's side of the session (models/world.py, engine/world.py): the read
after each passage, in a story with the map on.

It goes out beside the scene read on a sibling client, as the plot's clock
read does, and its result is applied on the worker thread after theirs. A
failed read never costs the turn: the map stays as it was and a notice says
so. Never in a private scene: `read_after_turn` isn't run there, and the
approved summary is read like any passage.

A simple chat may keep a map too (the author, Oct 2026: the map should grow
from a chat as it does from a story). A chat has no cast and holds nobody,
so the map tracks the user by one name (`Story.chat_character`, "You" when
blank, `chat_name`), the read is told who "I" and "you" are, and the only
people it is given is that name. The map read is the one side call a chat
makes (session_plot.py `_read_after_turn`).
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from sealedlore.engine.area import BRIDGE_PART, crossing_middle, tile_point
from sealedlore.engine.chronicle import introduced, variant_named
from sealedlore.engine.marks import footprint, place_at
from sealedlore.engine.regions import for_the_read
from sealedlore.engine.scene_state import scene_at
from sealedlore.engine.session_area import is_opening
from sealedlore.engine.session_plot import SessionNotice
from sealedlore.engine.takeable import FIXED_MARK, named_in, the
from sealedlore.engine.validators import mentions
from sealedlore.engine.world import (
    MAP_LIST_LIMIT,
    _chain,
    _names_place,
    _other_sex,
    build_world_read_messages,
    describe_delta,
    established,
    fold_onto,
    hide,
    key,
    parse_world_reply,
    place_delta,
    render_map_context,
    render_place_record,
    resolve_reply,
    several,
    unknown_words,
    within_ids,
    world_under,
)
from sealedlore.engine.world_layout import CHILD_SHARE, size_of
from sealedlore.models.character import Character
from sealedlore.models.node import DIRECTOR_SPEAKER_ID, NARRATOR_SPEAKER_ID, Node
from sealedlore.models.story import keeps_map
from sealedlore.models.world import WorldDelta, WorldMap
from sealedlore.providers.base import ChatRequest, ProviderError

# At most this many fixed things named on REMEMBER, the nearest first.
MAX_FIXED_NAMED = 3

# Room for a model that reasons a little before its JSON (DeepSeek V4.1 Flash
# answered in ~500 tokens on the longest test story).
WORLD_MAX_TOKENS = 4000
# A chat's user on the map when the chat names nobody.
CHAT_MAP_NAME = "You"

# The fold of a path is kept (`WorldRuntime._fold`): a fold of a 500-message
# path took 25 ms, and a redraw folded it five times (the review, Oct 20
# 2026). The last messages are compared by what they hold, since the reads,
# the turn being stamped and the author's corrections write their deltas in
# place; the rest by which delta each message carries.
FOLD_TAIL = 6
FOLDS_KEPT = 8


# What opens where it stands: fixed, but opening it moves nothing, and "if
# anyone tries to move one, the attempt fails" on REMEMBER read as the toll
# gate not opening when the author walked up to it (Oct 18 2026).
OPENS_IN_PLACE = re.compile(
    r"\b(?:doors?|gates?|gateways?|hatch(?:es)?|trapdoors?|windows?|shutters?|"
    r"portcullis(?:es)?|drawbridges?|lids?)\b",
    re.IGNORECASE,
)


# How someone left, as the scene read put it, that takes them out of the
# building (`leave_with_scene_read`), and that keeps them in it.
_WENT_OUT = re.compile(
    r"\b(?:out|outside|away|off|home|ashore|left|leaves?|leaving|depart\w*|exit\w*|"
    r"took (?:his|her|their) leave|took leave)\b",
    re.IGNORECASE,
)
_WENT_IN = re.compile(
    r"\b(?:upstairs|downstairs|inside|indoors|further in|deeper in|back room|cellar|"
    r"loft|attic)\b",
    re.IGNORECASE,
)


def _label_words(name: str) -> str:
    """A place's name as a plan's label gives it: no case, no "the", nothing
    in brackets ("the toll house" is the plan's "toll house")."""
    return key(name.split("(")[0])


@dataclass
class _WorldRead:
    """A map read that has been prepared but not yet answered."""

    node: Node
    before: WorldMap
    source: str
    request: ChatRequest
    log_ref: str


class WorldRuntime:
    # The map model a read is out to now, or None: the Map page says so
    # while the author waits (the author's castle, Oct 15 2026: a minute
    # with no sign that anything was happening).
    map_reading: str | None = None

    @property
    def map_on(self) -> bool:
        """The story keeps a map: the author turned it on (a simple chat may
        too); a visual story is played on its map whatever the flag says."""
        return self.story.visual or keeps_map(self.story.defaults)

    @property
    def chat_name(self) -> str:
        """A simple chat's name for the user on its map."""
        return self.story.chat_character.strip() or CHAT_MAP_NAME

    def map_held_name(self, held_id: str | None) -> str | None:
        """Whom the map follows: the character `held_id` names, or a chat's
        user. None when nobody is held in a story."""
        if self.story.chat:
            return self.chat_name
        held = self._character(held_id) if held_id else None
        return held.name if held is not None else None

    def map_people(self, path=None) -> list[Character]:
        """Whom a map read may place by name: the story's visible cast and
        supporting cards; a chat's user alone (a chat's stray cards are
        nobody's business)."""
        if self.story.chat:
            return [Character(id="", name=self.chat_name)]
        return [*self.visible_cast(path), *self.visible_supporting()]

    def read_unknowns(self, world: WorldMap) -> str:
        """The map read told that an unnamed person may be placed by a
        description, and which people the map has not named yet."""
        people = ", ".join(f"{label} ({said})" for label, said in world.unknowns.items())
        listed = self.texts.fill("map_read.unknowns.listed", people=people) if people else ""
        return self.texts.fill("map_read.unknowns", listed=listed)

    def _where_drawn(self, world: WorldMap, delta: WorldDelta, held: str | None = None) -> None:
        """A new place the read adds that a plan already draws (a label of its
        name) stands where the plan draws it: its parent's plan, or, when the
        read gave it none, the plan of where the author's character is or a
        place round it, which is then its parent. Live (Oct 18 2026) the toll
        bridge's plan drew its toll house by the road; the read added "the
        toll house" when Wren knocked at it, the map put it on the river at
        the bridge's middle (and, given no parent, beside Wren just off the
        bridge's plan), and Wren, at its door, was stood out in open country."""
        made = set(delta.added or [])
        # Where the author's character is, then each place round it.
        around: list[str] = []
        here = world.where.get(held or "")
        while here and here not in around and here in world.places:
            around.append(here)
            here = world.places[here].inside
        art = None
        for place in delta.places:
            if place.id not in made or place.x is not None:
                continue
            for parent_id in [place.inside] if place.inside else around:
                parent = world.places.get(parent_id or "")
                if parent is None or parent.x is None:
                    continue
                art = art or self.map_art_copy()
                if art.layout_shown(parent.id, parent.state) is None:
                    continue
                area = self.area(parent.id, world, art)
                if area is None or area.indoor:
                    continue
                wanted = _label_words(place.name)
                spot = next(
                    (xy for n, xy in area.labels.items() if _label_words(n) == wanted), None
                )
                if spot is None and BRIDGE_PART.search(place.name):
                    # Part of the bridge (its middle arch, a parapet): on the
                    # crossing. Live (Oct 18 2026) "the middle arch" went to
                    # the bank, a hundred metres from the river.
                    spot = crossing_middle(area)
                point = tile_point(world, area, *spot) if spot is not None else None
                if point is not None:
                    place.x, place.y = point
                    place.inside = parent.id
                    break

    @property
    def map_model(self) -> str:
        """The model that reads the map: its own, else the scene model."""
        return self.role_model("map")

    @property
    def map_art_model(self) -> str:
        """The model that draws the map: its own, else the map model."""
        return self.role_model("map_art")

    def world(self, node_id: str | None = None) -> WorldMap:
        """The map at a message (the leaf by default), on its path: a copy,
        the caller's to change."""
        return self._world_on(self.path() if node_id is None else self._path_to(node_id))

    def _world_on(self, path, *, whole: bool = False) -> WorldMap:
        """The map at the end of `path`: the story's imported map under it,
        less what the plot is keeping hidden at that point (`whole`: with
        it, for placing new places clear of what is hidden). A copy of the
        kept fold (`_fold`), the caller's to change."""
        return self._fold(path, whole=whole).model_copy(deep=True)

    def _world_read(self, path=None) -> WorldMap:
        """The map at the end of `path` (the leaf's by default) for reading
        only: the kept fold itself, shared by every reader until what it is
        folded from changes. Never change it; `world()` gives a copy."""
        return self._fold(self.path() if path is None else path)

    def _fold(self, path: Sequence[Node], *, whole: bool = False) -> WorldMap:
        """The map folded from `path`, kept: one fold per path and what it
        is folded from (the imported map, the start, the designer's regions,
        what the plot hides, each message's delta and the author's
        corrections on it), shared, never to be changed. The fold of all but
        the last FOLD_TAIL messages is kept by which delta each carries (a
        delta replaced is a new one), and goes on from there; the last are
        compared by what they hold, since they are written in place."""
        hidden = self.hidden_ids(path) if self.story.plot is not None and not whole else ()
        designed = self.designed_delta()
        base = self.story.world_base
        head = list(path[:-FOLD_TAIL]) if len(path) > FOLD_TAIL else []
        tail = list(path[len(head) :])
        head_key = (
            id(designed),
            base.model_dump_json() if base is not None else "",
            self.story.world_start,
            tuple(
                (n.id, id(n.meta.world), id(n.meta.world_fixes), *map(id, n.meta.world_fixes))
                for n in head
            ),
        )
        tail_key = tuple(
            (
                n.id,
                n.meta.world.model_dump_json() if n.meta.world is not None else "",
                *(f.model_dump_json() for f in n.meta.world_fixes),
            )
            for n in tail
        )
        # A visual story's keeper sets a place's state on the live state (no
        # passage carries a delta there): read as stored, never settled.
        live = self.story.area_live if self.story.visual else None
        states = tuple(sorted(live.state.place_states.items())) if live is not None else ()
        key = (head_key, tail_key, tuple(sorted(hidden)), states)
        folds = self._folds()
        lock = self._fold_lock()
        with lock:
            found = folds["all"].get(key)
            if found is not None:
                folds["all"].move_to_end(key)
                return found[0]
            kept = folds["head"].get(head_key)
            if kept is not None:
                folds["head"].move_to_end(head_key)
        if kept is None:
            under = world_under(base, start=self.story.world_start, designed=designed)
            world = fold_onto(under, head)
            # What the key's ids are of, held while it is kept, so that no id
            # in it is given to another object meanwhile.
            held = [
                designed,
                *(n.meta.world for n in head),
                *(n.meta.world_fixes for n in head),
                *(f for n in head for f in n.meta.world_fixes),
            ]
            kept = (world, held)
            with lock:
                folds["head"][head_key] = kept
                while len(folds["head"]) > FOLDS_KEPT:
                    folds["head"].popitem(last=False)
        world = hide(fold_onto(kept[0].model_copy(deep=True), tail), hidden)
        for place_id, state in states:
            if place_id in world.places:
                world.places[place_id].state = state
        with lock:
            folds["all"][key] = (world, kept[1])
            while len(folds["all"]) > FOLDS_KEPT:
                folds["all"].popitem(last=False)
        return world

    def _folds(self) -> dict[str, OrderedDict]:
        found = self.__dict__.get("_world_folds")
        if found is None:
            found = self.__dict__.setdefault(
                "_world_folds", {"head": OrderedDict(), "all": OrderedDict()}
            )
        return found

    def _is_kept_fold(self, world: WorldMap) -> bool:
        """Whether `world` is a kept fold (`_world_read`), shared and never
        changed, rather than a copy."""
        with self._fold_lock():
            return any(kept[0] is world for kept in self._folds()["all"].values())

    def _fold_lock(self) -> threading.Lock:
        return self.__dict__.setdefault("_world_fold_lock", threading.Lock())

    def record_context(self, path, held_id: str | None, *, turn_node_id: str | None = None) -> str:
        """The text-only record in the map's place (`render_place_record`),
        for a story that keeps no map, when `Config.place_record` is on:
        what the reads established, with nothing the program worked out.
        The world designer's regions are left out."""
        if not self.config.place_record:
            return ""
        turn = self._turn_node(path, turn_node_id)
        shown = self._world_on([*path, turn]) if turn is not None else self._world_on(path)
        regions = self.world_art.regions.values()
        return render_place_record(
            established(shown, self.story.world_base),
            self.map_held_name(held_id),
            self.texts,
            exclude=self.designed_place_ids() | {r.place_id for r in regions},
        )

    def map_context(self, path, held_id: str | None, *, turn_node_id: str | None = None) -> str:
        """The local map for the storyteller at the end of `path`, whenever
        the story keeps a map (the author's call: playing without one saves
        the tokens). A plot file's places in its own words, with distances;
        what the story added, as the reads found it. Never in a private
        scene. Measured in SealedLoreDocs design/map.md."""
        if self.in_private:
            return ""
        if not self.map_on:
            return self.record_context(path, held_id, turn_node_id=turn_node_id)
        base = self.story.world_base
        # The turn being sent moved the author's character (its walking, its
        # travel): the map it is told is the one with the turn on it, or "X
        # is at" named the place they left (the second review).
        turn = self._turn_node(path, turn_node_id)
        shown = self._world_on([*path, turn]) if turn is not None else self._world_on(path)
        stays = self.fixed_stays(turn)
        text = render_map_context(
            established(shown, base),
            self.map_held_name(held_id),
            self.texts,
            # Where the file or the world designer put a place is where it is:
            # told with its distance (engine/regions.py).
            fixed=({p.id for p in base.places} if base is not None else set())
            | self.designed_place_ids(),
            mark_fixed=stays,
            # Every designed region in full (the author's ruling, Oct 19
            # 2026); other places are cut to the nearest MAP_LIST_LIMIT.
            whole={r.place_id for r in self.world_art.regions.values()},
        )
        # Where everyone stands as the turn is written, as the positions
        # below have it: a retake's leaf is elsewhere in the story.
        if turn is not None and turn.meta.area is not None:
            standing = turn.meta.area
        else:
            live = self._live() if self.map_first else None
            standing = live if live is not None else self._snapshot_on(path)
        drawn = self.drawn_context(shown, self.map_held_name(held_id), standing)
        if text and drawn:
            text += "\n" + drawn
        # Map-first: where everyone stands, exactly (engine/session_area.py),
        # and the spots the author's turn marks. `turn_node_id` is the turn
        # being sent (a retake's leaf is the previous take, so it can't be
        # found from the leaf).
        positions = self.positions_context(
            path, held_id, turn_node_id=turn_node_id, mark_fixed=stays
        )
        if stays and (FIXED_MARK in text or FIXED_MARK in positions):
            # Said once, under whichever list marked something.
            if positions:
                positions += "\n" + self.texts["map.fixed"]
            else:
                text += "\n" + self.texts["map.fixed"]
        if positions and self.texts["map.current"]:
            # The map wins where the card, the roster or the history disagree.
            positions += "\n" + self.texts["map.current"]
        parts = [text, positions, self.marks_context(path, turn_node_id=turn_node_id)]
        # Someone not named yet by their description, never "Unknown 1".
        return unknown_words("\n\n".join(p for p in parts if p), shown)

    def drawn_context(self, world: WorldMap, held: str | None, state=None) -> str:
        """`map.drawn`: the buildings the plan of the place the author's
        character is in labels that the map has no place for yet, so the
        story uses their names rather than inventing others beside them.
        `state`: where everyone stands as the turn is written (else now).
        "" indoors, on open country, or with nothing drawn and unnamed."""
        if not self.map_first or self.in_private or held is None:
            return ""
        state = state if state is not None else self.area_state()
        if state is None:
            return ""
        spot = state.spots.get(held)
        area = self.area(spot.area, world) if spot is not None else None
        if area is None or area.indoor or area.window or not area.labels:
            return ""
        place = world.places.get(area.place_id or "")
        if place is None:
            return ""
        known = {key(p.name) for p in world.places.values()} | {
            key(a) for p in world.places.values() for a in p.aliases
        }
        names = [
            n
            for n in area.labels
            if _label_words(n) not in known and key(n) not in known and key(n) != key(place.name)
        ][:MAP_LIST_LIMIT]
        if not names:
            return ""
        return self.texts.fill("map.drawn", place=place.name, names=", ".join(names))

    def _prepare_world_read(self) -> _WorldRead | None:
        """The map read's request for the latest passage, logged; None when
        the map is off or there is nothing to read."""
        if not self.map_on:
            return None
        result = self.last_result
        if result is None or result.node is None or result.stopped_early:
            return None
        node = result.node
        if not node.content.strip():
            return None
        path = self._path_to(node.id)
        before = self._world_on(path[:-1])
        author = path[-2] if len(path) > 1 and path[-2].kind == "user" else None
        held = self.map_held_name(node.meta.controlled_character_id)
        author_text = author.content if author is not None else ""
        note = ""
        # On the author's Direction or Narration the read is told, in the
        # user message only, what it may change about places already on the
        # map (`map_read.direction`); every other read is sent as measured.
        direction = ""
        if self.story.chat:
            label = "The user's message"
            note = self.texts.fill("map_read.chat", name=self.chat_name)
        elif author is None:
            label = "The author's turn"
        elif self._author_says(author):
            kind = "Direction" if author.speaker_id == DIRECTOR_SPEAKER_ID else "Narration"
            label = f"The author's turn: {kind} (what it says happens)"
            direction = self.texts["map_read.direction"]
        else:
            speaker = self._character(author.speaker_id)
            name = speaker.name if speaker is not None else (held or "")
            label = f"The author's turn, as {name}"
        people = [c.name for c in self.map_people(path)]
        # Of the world designer's regions, the read is shown the one the
        # author's character stands in (engine/regions.py `for_the_read`).
        shown = before
        if self.world_art.regions:
            point = self.held_point(self._snapshot_on(path[:-1]), before, held=held)
            shown = for_the_read(before, self.map_art_copy(), point)
        begins = self.read_begins_at(path[:-1])
        # Its words count as a quote: the read quoted the location, or (live)
        # the whole line saying it.
        begins_at = self.texts.fill("map_read.begins_at", location=begins) if begins else ""
        model = self.map_model
        request = ChatRequest(
            model=model,
            extra_body=self.route_for("map"),
            messages=build_world_read_messages(
                world=shown,
                people=people,
                held=held,
                author_label=label,
                author_turn=author_text,
                passage=node.content,
                texts=self.texts,
                note="\n\n".join(
                    n
                    for n in (
                        note,
                        self.read_spots(path[:-1], held),
                        self.read_things_here(path[:-1], held),
                        self.read_minutes(author, opening=is_opening(path[:-1])),
                        begins_at,
                        self.read_marks(author),
                        self.read_unknowns(shown),
                        direction,
                    )
                    if n
                ),
                last=self.texts["map_read.outside"] if self.map_first else "",
            ),
            params=self.side_params(model, max_tokens=WORLD_MAX_TOKENS),
        )
        log_ref = self._log(
            "world_request",
            {"node_id": node.id, "payload": self.provider.build_payload(request)},
        )
        self.map_reading = model
        return _WorldRead(
            node=node,
            before=before,
            # The setup's words count as a quote on the opening's read.
            source=f"{author_text}\n\n{node.content}" + (f"\n\n{begins_at}" if begins else ""),
            request=request,
            log_ref=log_ref,
        )

    def read_begins_at(self, before: Sequence[Node]) -> str:
        """Where Setup says the story begins, for the read of the opening of
        a story that grows its map (`map_read.begins_at`); "" otherwise."""
        if self.story.chat or self.story.world_base is not None or not is_opening(before):
            return ""
        return " ".join((self.story.setup.starting_scene.location or "").split())

    def update_world_after_turn(
        self, pending: _WorldRead | None = None, fetched: dict[str, object] | None = None
    ) -> Iterator[SessionNotice]:
        """Read what the latest passage added to the map or changed.

        `fetched` is the reply (or the error) of a call already made beside
        the scene read.
        """
        pending = pending or self._prepare_world_read()
        if pending is None:
            return
        node, log_ref = pending.node, pending.log_ref
        yield SessionNotice(
            f"Reading the map with {pending.request.model}…",
            waiting_since=time.monotonic() if fetched is None else None,
        )
        try:
            if fetched is None:
                text, completed = self.provider.complete(pending.request)
            elif "error" in fetched:
                raise fetched["error"]  # type: ignore[misc]
            else:
                text, completed = fetched["reply"]  # type: ignore[misc]
            self._log(
                "world_response",
                {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
            )
            self.unreported_usage.append(self.priced(completed.usage, pending.request.model))
            reply = parse_world_reply(text)
        except (ProviderError, ValueError) as exc:
            self.map_reading = None
            yield self._failed_notice(
                f"Couldn't read the map from this passage ({exc}). It is as it was.",
                exc,
                node=node,
            )
            return
        finally:
            self.map_reading = None
        path = self._path_to(node.id)
        author = path[-2] if len(path) > 1 and path[-2].kind == "user" else None
        people = self.map_people(path)
        held_name = self.map_held_name(node.meta.controlled_character_id)
        # Map-first: what the read says of the play view's own things (a
        # plan's objects, the furniture) is kept on the area, not added to
        # the map's list as something new.
        whole_reply = reply
        reply, placements = self.placement_entries(reply, path[:-1], pending.before, held_name)
        # The author's Direction or Narration may change what is on the map
        # already (remove, rename, resize, move, put inside); a known place
        # it puts at a spot the author marked goes exactly there.
        says = author is not None and self._author_says(author)
        # A fixed thing moves on the author's word alone (the author, Oct 17
        # 2026): Direction, Narration or Fiat.
        moves_fixed = says or (author is not None and self._under_fiat(author))
        delta, refused = resolve_reply(
            pending.before,
            reply,
            source=pending.source,
            people=people,
            author_says=says,
            marked=self._marked_places(reply, author),  # refused, and logged, in character
            moves_fixed=moves_fixed,
            # Nobody is put in another region of the world designer's grid
            # than they stand in unless the passage names the place.
            standing_in=self.standing_regions(self._snapshot_on(path[:-1]), pending.before)
            if self.map_first
            else None,
            # The company, who may go together where one of them may go.
            together=self._present_on_map(scene_at(path[:-1], self.story.setup.starting_scene))
            if self.map_first
            else (),
        )
        # Map-first: what the author marked is put exactly there, and what
        # has nothing else to go on goes beside where they stand (the turn's
        # character, not whoever is held now).
        self.apply_marks(pending.before, delta, reply, author)
        self._nest_moved(pending.before, delta)
        near = None
        if self.map_first:
            near = self.held_point(self._snapshot_on(path[:-1]), pending.before, held=held_name)
        # Placed clear of the plot's hidden places too (revealed, one shared
        # a spot with a place the read put beside the character).
        whole = self._world_on(path[:-1], whole=True) if self.story.plot is not None else None
        self._where_drawn(pending.before, delta, held_name)
        place_delta(whole if whole is not None else pending.before, delta, held_name, near)
        # Someone sent to a marked spot, once every new place has its spot.
        self.apply_marks_at(pending.before, delta, reply, author)
        # Someone the plot's event brought in, introduced without a name.
        self._name_brought(delta, author, pending.before, node.content)
        node.meta.world = None if delta.empty() else delta
        if says:
            self._redraw_resized(pending.before, delta)
        # Map-first: everyone settled on the map the passage left, and the
        # things on the play view where the passage left them.
        refused += self.area_after_read(
            node,
            whole_reply,
            pending.source,
            placements=placements,
            moves_fixed=moves_fixed,
        )
        if refused:
            self._log("world_refused", {"request_log_ref": log_ref, "refused": refused})
        self.save()
        # Whatever the map still needs drawn goes to the art model now, in
        # the background (session_world_art.py): the passage never waits.
        self.start_map_art()
        yield SessionNotice(f"Map: {describe_delta(pending.before, delta)}")

    def _name_brought(
        self, delta: WorldDelta, author: Node | None, before: WorldMap, passage: str
    ) -> None:
        """The one person the passage brought in unnamed, on a turn the plot
        began an event bringing in one character the map hasn't placed: that
        character. Live (Oct 17 2026), the director began the first bandit
        meeting with Evan Stevens's card, the passage introduced "the young
        man with the scoped rifle", the map kept him as an unknown and the
        roster said Evan was elsewhere; asked his name, Sonnet made up "Kyle".
        """
        plot = self.story.plot
        if author is None or plot is None or not delta.unknowns:
            return
        new = [label for label in delta.unknowns if label not in before.unknowns]
        begun = [d for d in author.meta.direction if d.kind == "begin"]
        if len(new) != 1 or not begun:
            return
        people = {c.id: c for c in [*self.cast, *self.supporting]}
        brought: list[Character] = []
        for direction in begun:
            event = next((e for e in plot.events if e.id == direction.event_id), None)
            if event is None:
                continue
            variant = variant_named(event, direction.variant)
            brought += [people[i] for i in introduced(event, variant) if i in people]
        placed = {*before.where, *delta.where}
        unique = {c.id: c for c in brought}.values()
        brought = [c for c in unique if c.name not in placed and not mentions(passage, c)]
        if len(brought) != 1:
            return
        label, name = new[0], brought[0].name
        # One person who could be them: never a group ("the watchmen" became
        # Elena Ruiz, live, Oct 20 2026), nor someone of the other sex.
        said = delta.unknowns[label]
        if several(said) or _other_sex(said, self._card_words(name)):
            return
        delta.unknowns.pop(label)
        if label in delta.where:
            delta.where[name] = delta.where.pop(label)
        if label in delta.outside:
            delta.outside[name] = delta.outside.pop(label)

    def join_named_newcomer(self) -> str | None:
        """After all of a passage's reads: the one person the map read put in
        the author's character's place unnamed, and the one the scene read
        brought into the scene by name whom the map hasn't placed, are one
        person, named (the map's `named`, and the label's spot kept under the
        name). Live (Oct 18 2026) the map shop's dealer stepped out "a woman,
        perhaps forty": the map kept her as Unknown 3, the scene read knew her
        for Sable Cours, and both stood in the shop. Her name, or None."""
        if not self.map_first or self.in_private or self.story.chat:
            return None
        result = self.last_result
        node = result.node if result is not None else None
        delta = node.meta.world if node is not None else None
        if node is None or delta is None or not delta.unknowns:
            return None
        path = self._path_to(node.id)
        before = self._world_on(path[:-1])
        new = [label for label in delta.unknowns if label not in before.unknowns]
        held = self.map_held_name(node.meta.controlled_character_id)
        if len(new) != 1 or held is None:
            return None
        label = new[0]
        after = self._world_on(path)
        if after.where.get(label) is None or after.where.get(label) != after.where.get(held):
            return None
        scene_before = scene_at(path[:-1], self.story.setup.starting_scene)
        came = set(self._present_on_map()) - set(self._present_on_map(scene_before))
        unplaced = [n for n in came if n != held and n not in after.where]
        if len(unplaced) != 1 or _other_sex(delta.unknowns[label], self._card_words(unplaced[0])):
            return None
        name = unplaced[0]
        delta.unknowns.pop(label)
        delta.named.pop(label, None)
        if label in delta.where:
            delta.where[name] = delta.where.pop(label)
        if label in delta.outside:
            delta.outside[name] = delta.outside.pop(label)
        state = node.meta.area
        if state is not None and label in state.spots:
            state.spots[name] = state.spots.pop(label)
        self.save()
        return name

    def follow_company(self) -> str | None:
        """After all of a passage's reads: the author's character goes where
        the company they were with went, when the map read moved the others
        and forgot them (its prompt says always to give them, and live it
        didn't): someone with them before the passage was moved to a new
        place, the scene read has both of them still in the scene, the
        scene's location changed this passage, and its words name that
        place. Live (Oct 18 2026) Wren and Tobias walked three hours to
        Saltmarsh's north gate; the map moved Tobias there and left Wren in
        the toll house. Also someone the passage before moved away from
        them, the author's character left behind since: a Narration took
        Adelaide and Silas up the rail to the Skyport, the read moved Silas,
        and the scene's location named the platform only on the next
        passage. A walk on the author's own turn counts from where it took
        them: only who was there with them is their company, so nobody
        standing elsewhere pulls them back off it. The place's name, or
        None."""
        if not self.map_first or self.in_private or self.story.chat:
            return None
        result = self.last_result
        node = result.node if result is not None else None
        delta = node.meta.world if node is not None else None
        held = self.map_held_name(node.meta.controlled_character_id) if node else None
        if node is None or held is None or (delta is not None and held in delta.where):
            return None
        # A read that changed nothing left no delta: the company may still
        # have gone on the passage before.
        delta = delta if delta is not None else WorldDelta()
        path = self._path_to(node.id)
        before = self._world_on(path[:-1])
        was = before.where.get(held)
        scene_before = scene_at(path[:-1], self.story.setup.starting_scene)
        scene_after = scene_at(path, self.story.setup.starting_scene)
        moved_on = (scene_after.location or "").strip().lower()
        if was is None or not moved_on or moved_on == (scene_before.location or "").strip().lower():
            return None
        together = set(self._present_on_map(scene_after))
        if held not in together:
            return None
        after = self._world_on(path)
        # Who was with them: at their place before this passage, or before
        # the one before it with the author's character not moved since.
        company = {n for n, pid in before.where.items() if pid == was}
        previous = [i for i, n in enumerate(path[:-1]) if n.kind == "assistant"]
        if previous:
            earlier = self._world_on(path[: previous[-1]])
            if earlier.where.get(held) == was:
                company |= {n for n, pid in earlier.where.items() if pid == was}
        # The places the read moved them to this passage first, then where
        # it already had them.
        moves = [(n, after.where[n]) for n in delta.where if n in after.where] + [
            (n, pid) for n, pid in after.where.items() if n not in delta.where
        ]
        for name, place_id in moves:
            place = after.places.get(place_id)
            if (
                name == held
                or name not in together
                or name not in company
                or place is None
                or place.kind == "region"
                or place_id == was
                or _label_words(place.name) not in moved_on
            ):
                continue
            node.meta.world = delta
            delta.where[held] = place_id
            if name in delta.outside:
                delta.outside[held] = delta.outside[name]
            if node.meta.area is not None:
                node.meta.area = self.settle(
                    node.meta.area.model_copy(deep=True),
                    self._world_on(path),
                    self.map_art_copy(),
                    walked=False,
                )
            self.save()
            return place.name
        return None

    def leave_with_scene_read(self) -> list[str]:
        """After all of a passage's reads: whoever the scene read says went
        out (a departure with how they went: "took his leave", "out into the
        Row"), whom the map read left standing in the building with the
        author's character, is put outside it, at its door (as `settle`
        stands anyone the map has outside). Live (Oct 18 2026) the inspector
        took his leave of the workshop, the scene read dropped him, the map
        read said nothing, and for four passages the storyteller was told he
        stood 10 m away, within earshot.

        The map wins over the scene card, so only a departure the read
        states counts: dropped from the roster without a word of how (a
        card reseeded, someone forgotten) moves nobody, nor going further in
        (upstairs, to another of its rooms). Made as the author's
        corrections are (`world_fixes` on the passage), so the map's Undo
        takes it back; the author's character stays where they stand. Who
        was moved."""
        if not self.map_first or self.in_private or self.story.chat:
            return []
        result = self.last_result
        node = result.node if result is not None else None
        scene = node.meta.scene if node is not None else None
        if node is None or scene is None or not node.meta.scene_read:
            return []
        if scene.set_at_node_id != node.id or self.correction_problem() is not None:
            return []
        path = self.path()
        if not path or path[-1].id != node.id:
            return []
        held = self.map_held_name(node.meta.controlled_character_id)
        if held is None or held not in set(self._present_on_map(scene)):
            return []
        after = self.world()
        mine = after.places.get(after.where.get(held) or "")
        if mine is None:
            return []
        # The building they are in: out of a room of it is out of it.
        indoors = []
        for place in _chain(after, mine.id):
            if place.kind not in ("building", "room", "vehicle"):
                break
            indoors.append(place)
        if not indoors:
            return []  # out of doors, where they went is anyone's guess
        building = indoors[-1]
        within = within_ids(after, building.id) | {building.id}
        rooms = [after.places[pid] for pid in within if pid in after.places]
        delta = node.meta.world
        moved: list[str] = []
        for change in scene.changes:
            how = change.detail or ""
            if change.kind != "left" or not _WENT_OUT.search(how) or _WENT_IN.search(how):
                continue
            if any(_names_place(how, room) for room in rooms if room.id != building.id):
                continue  # to another of its rooms
            name = self._map_person(change.name, after) or change.name
            if name == held or name in moved:
                continue
            if delta is not None and (name in delta.where or name in delta.left):
                continue  # the map read placed them itself
            if after.where.get(name) not in within or name in after.outside:
                continue  # not in there with the author's character
            moved.append(name)
        if not moved:
            return []
        self._begin_correction(", ".join(moved) + " put outside")
        fix = self._fix()
        for name in moved:
            fix.where[name] = building.id
            fix.outside[name] = True
        self._done(self.settle(self._state(), self.world(), self.map_art_copy(), walked=True))
        return moved

    def _card_words(self, name: str) -> str:
        """What a character's card says of them, for telling their sex."""
        for card in [*self.cast, *self.supporting]:
            if card.name == name:
                return " ".join(
                    getattr(card, field, "") or "" for field in ("summary", "full_description")
                )
        return ""

    # --- the author's Direction and the map -----------------------------------------

    @staticmethod
    def _author_says(author: Node) -> bool:
        """The author's turn states what is so: Direction or Narration."""
        return author.speaker_id in (DIRECTOR_SPEAKER_ID, NARRATOR_SPEAKER_ID)

    def fixed_reminder(
        self, path, held_id: str | None, *, turn_node_id: str | None = None, text: str = ""
    ) -> str:
        """The REMEMBER clause (`turn.remember.fixed`) for what the author's
        turn names in their area, or here, that can't be moved this turn
        (the nearest `MAX_FIXED_NAMED`: "bench" names every bench); "" on a
        turn that may move it, or when it names none. Recency: told only in
        the positions, Sonnet 4.6 still had the bench "grind a foot and stop"
        (SealedLoreDocs design/map.md, "Fixed things")."""
        if not self.map_on or self.in_private or not text.strip():
            return ""
        turn = self._turn_node(path, turn_node_id)
        if not self.fixed_stays(turn):
            return ""
        scored = [
            (named_in(name, text), name)
            for name in self.fixed_here(path, held_id, turn_node_id=turn_node_id)
            if not OPENS_IN_PLACE.search(name)
        ]
        best = max((score for score, _name in scored), default=0)
        if not best:
            return ""
        # The ones the turn names best: "the heavy wooden shelf unit" over
        # every other shelf in the house (live).
        named = [name for score, name in scored if score == best][:MAX_FIXED_NAMED]
        things = ", ".join(the(name) for name in named)
        return self.texts.fill("turn.remember.fixed", things=things)

    def fixed_stays(self, turn: Node | None) -> bool:
        """Whether the storyteller is told what can't be taken stays put: on
        an in-character turn not under Fiat, and a Question (`turn` None);
        never on Direction or Narration, or under Fiat, which may move it
        (the map read moves it then too: `resolve_reply(moves_fixed=)`)."""
        if turn is None:
            return True
        return not (self._author_says(turn) or self._under_fiat(turn))

    def _under_fiat(self, author: Node) -> bool:
        """The author's turn was sent under Fiat: what they write happens.
        A turn from before the outcome was kept on it has the story's."""
        mode = author.meta.agency_mode or self.story.defaults.agency_mode
        return mode == "fiat"

    def _marked_places(
        self, reply: dict[str, Any], author: Node | None
    ) -> dict[str, tuple[float, float]]:
        """The spots the reply's place entries put places at, by the entry's
        name as compared (`key`) and by the name it was known under ("was"):
        the turn's own marks, resolved as `apply_marks` resolves them for a
        new place. For `resolve_reply`, which moves a known place there on
        the author's Direction."""
        marks = self._turn_marks(author)
        out: dict[str, tuple[float, float]] = {}
        if not marks:
            return out
        for entry in reply.get("places") or []:
            if not isinstance(entry, dict):
                continue
            mark = self._mark_of(marks, entry.get("mark"), words=True)
            if mark is None:
                continue
            for field in ("name", "was"):
                name = key(str(entry.get(field) or ""))
                if name:
                    out[name] = (mark.x, mark.y)
        return out

    def _nest_moved(self, before: WorldMap, delta: WorldDelta) -> None:
        """A known place the author's Direction moved to a marked spot lies
        in whatever the map has there (as a new place at a mark does), never
        in itself or in what is inside it, unless the spot is still within
        the place it was in; its size is kept, within what the holder
        allows. A place moved by a link has no spot yet: `place_delta`
        places it from the link."""
        moved = [p for p in delta.places if p.id in delta.moved and p.x is not None]
        if not moved:
            return
        art = self.map_art_copy()
        for place in moved:
            x, y = place.x, place.y
            holder = before.places.get(place.inside or "")
            box = footprint(before, art, holder.id) if holder is not None else None
            if box is not None and box[0] <= x <= box[2] and box[1] <= y <= box[3]:
                continue
            excluded = {place.id, *within_ids(before, place.id)}
            others = before.model_copy(
                update={"places": {k: v for k, v in before.places.items() if k not in excluded}}
            )
            place.inside = place_at(others, art, x, y)
            holder = before.places.get(place.inside or "")
            if holder is not None:
                place.size = min(size_of(place), size_of(holder) * CHILD_SHARE)

    def _redraw_resized(self, before: WorldMap, delta: WorldDelta) -> None:
        """A place the author's Direction resized has its plan drawn again,
        as Redraw plan… does (the plan it has stays until the new one is
        drawn; session_world_art.py `redraw_plan`), so the plan's grid
        follows the size. One with no plan yet is drawn at its size when
        it is drawn."""
        resized = [
            p.id
            for p in delta.places
            if p.id in before.places
            and p.size is not None
            and p.size != size_of(before.places[p.id])
        ]
        if not resized:
            return
        art = self.world_art
        with self._lock():
            queued = False
            for pid in resized:
                if art.layout_for(pid) is None or pid in art.redraw:
                    continue
                art.redraw.append(pid)
                art.failures.pop(f"layout:{pid}:redraw", None)
                queued = True
            if queued:
                self._save_art(art)
