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
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass

from sealedlore.engine.world import (
    fold_onto,
    hide,
    key,
    world_under,
)
from sealedlore.models.character import Character
from sealedlore.models.node import Node
from sealedlore.models.world import WorldMap
from sealedlore.providers.base import ChatRequest

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
        return self.story.visual

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
        designed = None
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

    # --- the author's Direction and the map -----------------------------------------
