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

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from sealedlore.engine.marks import footprint, place_at
from sealedlore.engine.session_area import is_opening
from sealedlore.engine.session_plot import SessionNotice
from sealedlore.engine.takeable import FIXED_MARK, named_in
from sealedlore.engine.world import (
    build_world_read_messages,
    describe_delta,
    established,
    key,
    parse_world_reply,
    place_delta,
    render_map_context,
    resolve_reply,
    unknown_words,
    within_ids,
    world_at,
)
from sealedlore.engine.world_layout import CHILD_SHARE, size_of
from sealedlore.models.character import Character
from sealedlore.models.node import DIRECTOR_SPEAKER_ID, NARRATOR_SPEAKER_ID, Node
from sealedlore.models.world import WorldDelta, WorldMap
from sealedlore.providers.base import ChatRequest, ProviderError

# At most this many fixed things named on REMEMBER, the nearest first.
MAX_FIXED_NAMED = 3

# Room for a model that reasons a little before its JSON (DeepSeek V4.1 Flash
# answered in ~500 tokens on the longest test story).
WORLD_MAX_TOKENS = 4000
# A chat's user on the map when the chat names nobody.
CHAT_MAP_NAME = "You"


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
        """The story keeps a map: the author turned it on (a simple chat may too)."""
        return self.story.defaults.world_map

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

    @property
    def map_model(self) -> str:
        """The model that reads the map: its own, else the scene model."""
        return self.role_model("map")

    @property
    def map_art_model(self) -> str:
        """The model that draws the map: its own, else the map model."""
        return self.role_model("map_art")

    def world(self, node_id: str | None = None) -> WorldMap:
        """The map at a message (the leaf by default), on its path."""
        return self._world_on(self.path() if node_id is None else self._path_to(node_id))

    def _world_on(self, path, *, whole: bool = False) -> WorldMap:
        """The map at the end of `path`: the story's imported map under it,
        less what the plot is keeping hidden at that point (`whole`: with
        it, for placing new places clear of what is hidden)."""
        hidden = self.hidden_ids(path) if self.story.plot is not None and not whole else ()
        return world_at(path, self.story.world_base, start=self.story.world_start, hidden=hidden)

    def map_context(self, path, held_id: str | None, *, turn_node_id: str | None = None) -> str:
        """The local map for the storyteller at the end of `path`, whenever
        the story keeps a map (the author's call: playing without one saves
        the tokens). A plot file's places in its own words, with distances;
        what the story added, as the reads found it. Never in a private
        scene. Measured in SealedLoreDocs design/map.md."""
        if not self.map_on or self.in_private:
            return ""
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
            fixed={p.id for p in base.places} if base is not None else (),
            mark_fixed=stays,
        )
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
        parts = [text, positions, self.marks_context(path, turn_node_id=turn_node_id)]
        # Someone not named yet by their description, never "Unknown 1".
        return unknown_words("\n\n".join(p for p in parts if p), shown)

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
        model = self.map_model
        request = ChatRequest(
            model=model,
            extra_body=self.route_for("map"),
            messages=build_world_read_messages(
                world=before,
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
                        self.read_minutes(author, opening=is_opening(path[:-1])),
                        self.read_marks(author),
                        self.read_unknowns(before),
                        direction,
                    )
                    if n
                ),
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
            source=f"{author_text}\n\n{node.content}",
            request=request,
            log_ref=log_ref,
        )

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
        yield SessionNotice(f"Reading the map with {pending.request.model}…")
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
            yield SessionNotice(
                f"Couldn't read the map from this passage ({exc}). It is as it was."
            )
            return
        finally:
            self.map_reading = None
        path = self._path_to(node.id)
        author = path[-2] if len(path) > 1 and path[-2].kind == "user" else None
        people = self.map_people(path)
        # The author's Direction or Narration may change what is on the map
        # already (remove, rename, resize, move, put inside); a known place
        # it puts at a spot the author marked goes exactly there.
        says = author is not None and self._author_says(author)
        delta, refused = resolve_reply(
            pending.before,
            reply,
            source=pending.source,
            people=people,
            author_says=says,
            marked=self._marked_places(reply, author),  # refused, and logged, in character
            # A thing that can't be taken moves on the author's word alone
            # (the author, Oct 17 2026): Direction, Narration or Fiat.
            moves_fixed=says or (author is not None and self._under_fiat(author)),
        )
        held_name = self.map_held_name(node.meta.controlled_character_id)
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
        place_delta(whole if whole is not None else pending.before, delta, held_name, near)
        # Someone sent to a marked spot, once every new place has its spot.
        self.apply_marks_at(pending.before, delta, reply, author)
        if refused:
            self._log("world_refused", {"request_log_ref": log_ref, "refused": refused})
        node.meta.world = None if delta.empty() else delta
        if says:
            self._redraw_resized(pending.before, delta)
        # Map-first: everyone settled on the map the passage left.
        self.area_after_read(node, reply, pending.source)
        self.save()
        # Whatever the map still needs drawn goes to the art model now, in
        # the background (session_world_art.py): the passage never waits.
        self.start_map_art()
        yield SessionNotice(f"Map: {describe_delta(pending.before, delta)}")

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
        named = [
            name
            for name in self.fixed_here(path, held_id, turn_node_id=turn_node_id)
            if named_in(name, text)
        ][:MAX_FIXED_NAMED]
        if not named:
            return ""
        things = ", ".join(f"the {name}" for name in named)
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
