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

from sealedlore.engine.session_plot import SessionNotice
from sealedlore.engine.world import (
    build_world_read_messages,
    describe_delta,
    established,
    parse_world_reply,
    place_delta,
    render_map_context,
    resolve_reply,
    world_at,
)
from sealedlore.models.character import Character
from sealedlore.models.node import DIRECTOR_SPEAKER_ID, NARRATOR_SPEAKER_ID, Node
from sealedlore.models.world import WorldMap
from sealedlore.providers.base import ChatRequest, ProviderError

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

    def _world_on(self, path) -> WorldMap:
        """The map at the end of `path`: the story's imported map under it,
        less what the plot is keeping hidden at that point."""
        return world_at(
            path,
            self.story.world_base,
            start=self.story.world_start,
            hidden=self.hidden_ids(path) if self.story.plot is not None else (),
        )

    def map_context(self, path, held_id: str | None) -> str:
        """The local map for the storyteller at the end of `path`, whenever
        the story keeps a map (the author's call: playing without one saves
        the tokens). A plot file's places in its own words, with distances;
        what the story added, as the reads found it. Never in a private
        scene. Measured in SealedLoreDocs design/map.md."""
        if not self.map_on or self.in_private:
            return ""
        base = self.story.world_base
        text = render_map_context(
            established(self._world_on(path), base),
            self.map_held_name(held_id),
            self.texts,
            fixed={p.id for p in base.places} if base is not None else (),
        )
        # Map-first: where everyone stands, exactly (engine/session_area.py).
        positions = self.positions_context(path, held_id)
        return f"{text}\n\n{positions}" if text and positions else text or positions

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
        if self.story.chat:
            label = "The user's message"
            note = self.texts.fill("map_read.chat", name=self.chat_name)
        elif author is None:
            label = "The author's turn"
        elif author.speaker_id in (DIRECTOR_SPEAKER_ID, NARRATOR_SPEAKER_ID):
            kind = "Direction" if author.speaker_id == DIRECTOR_SPEAKER_ID else "Narration"
            label = f"The author's turn: {kind} (what it says happens)"
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
                note="\n".join(n for n in (note, self.read_spots(path[:-1])) if n),
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
        people = self.map_people(self._path_to(node.id))
        delta, refused = resolve_reply(pending.before, reply, source=pending.source, people=people)
        place_delta(pending.before, delta, self.map_held_name(node.meta.controlled_character_id))
        if refused:
            self._log("world_refused", {"request_log_ref": log_ref, "refused": refused})
        node.meta.world = None if delta.empty() else delta
        # Map-first: everyone settled on the map the passage left.
        self.area_after_read(node, reply, pending.source)
        self.save()
        # Whatever the map still needs drawn goes to the art model now, in
        # the background (session_world_art.py): the passage never waits.
        self.start_map_art()
        yield SessionNotice(f"Map: {describe_delta(pending.before, delta)}")
