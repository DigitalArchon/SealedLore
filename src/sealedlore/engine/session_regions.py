"""The world designer at work (engine/regions.py): the regions round the
author's character designed on a thread of its own, ahead of them, on the
story's model unless Settings names another; each kept with the map's art as
it comes in. A mixin of `StorySession`, beside the map's art.
"""

from __future__ import annotations

import threading
from typing import Any

from sealedlore.engine.regions import (
    REGION_MAX_TOKENS,
    build_region,
    cell_key,
    cell_of,
    cell_rect,
    designed_delta,
    designed_point,
    edge_lines,
    next_label,
    origin_of,
    parse_region,
    place_lines,
    region_messages,
    standing_region,
    taken_lines,
    waves,
    where_words,
)
from sealedlore.engine.world_art import MAX_FAILURES, holding_place
from sealedlore.ids import utc_now_iso
from sealedlore.models.area import AreaState
from sealedlore.models.world import WorldArt, WorldDelta, WorldMap
from sealedlore.providers.base import ChatProvider, ChatRequest, ProviderError
from sealedlore.threads import BackgroundThread

# Regions designed at once, a client each: a wave borders nothing else in
# itself (`regions.waves`), so the four beside the player's go together.
REGION_WORKERS = 4
# What the designer is told of the world and the story, at most.
SETTING_LIMIT = 4000
LORE_ENTRIES, LORE_LIMIT = 10, 300
STORY_PASSAGES, STORY_LIMIT = 4, 4000


class RegionRuntime:
    _region_thread: threading.Thread | None = None
    _region_clients: list[ChatProvider] | None = None
    _designed_cache: tuple[Any, WorldDelta | None] | None = None
    # The regions being designed now, and how many are done, for the Map
    # page's progress (`map_art_progress`; the review, Oct 20 2026: ~35 s of
    # nothing shown while a ring was designed).
    _region_total: int = 0
    _region_done: int = 0

    @property
    def regions_on(self) -> bool:
        """Regions are designed: a map played on, drawn (Draw the map), the
        story begun, never in a private scene (its story reaches no model
        but the private one)."""
        return bool(
            self.map_first
            and self.config.map_art
            and self.open_span is None
            and not self._map_closed
            and self.path()
        )

    @property
    def region_model(self) -> str:
        """The designer's model: Settings' own, else the story's."""
        return self.config.region_model or self.model

    @property
    def regions_running(self) -> bool:
        thread = self._region_thread
        return thread is not None and thread.is_alive()

    def designed_delta(self) -> WorldDelta | None:
        """The designer's regions as the map folds them in, kept while the
        same (the map is folded several times a redraw)."""
        art = self.world_art
        stamp = (tuple((k, r.made_at) for k, r in sorted(art.regions.items())), tuple(art.explored))
        cached = self._designed_cache
        if cached is not None and cached[0] == stamp:
            return cached[1]
        delta = designed_delta(art)
        self._designed_cache = (stamp, delta)
        return delta

    def regions_missing(self) -> bool:
        """Whether a region round the author's character is still to be
        designed (cheap: asked after every walk)."""
        if not self.regions_on or self.regions_running:
            return False
        art = self.world_art
        if not art.region_origin:
            return True
        point = self.held_point()
        if point is None:
            return False
        origin = (art.region_origin[0], art.region_origin[1])
        world = self.world()
        return any(
            self._cell_wanted(world, art, origin, cell)
            for wave in waves(origin, point)
            for cell in wave
        )

    def _cell_wanted(self, world: WorldMap, art: WorldArt, origin, cell: tuple[int, int]) -> bool:
        """Whether a cell of the grid is still to be designed: not designed,
        not failed too often, and not lying wholly inside one of the story's
        own settlements or sites (the town's plan is what is there; live,
        Oct 18 2026, the designer made a second "Marrowgate" inside the town)."""
        key = cell_key(*cell)
        if key in art.regions or art.failures.get(f"region:{key}", 0) >= MAX_FAILURES:
            return False
        designed_ids = {r.place_id for r in art.regions.values()}
        holder = holding_place(world, *cell_rect(origin, *cell))
        return not (
            holder is not None
            and holder.id not in designed_ids
            and holder.kind in ("settlement", "site")
        )

    def designed_place_ids(self) -> set[str]:
        """Every place the world designer put on the map."""
        return {p.id for r in self.world_art.regions.values() for p in r.places}

    def standing_regions(
        self, state: AreaState | None, world: WorldMap
    ) -> dict[str, tuple[float, float, float]]:
        """The region of the grid each person on the map stands in (their
        tile, else where the designer put them), for the map read
        (`world.resolve_reply(standing_in=)`); none in a story without the
        grid."""
        art = self.map_art_copy()
        if not art.region_origin:
            return {}
        spots = state.spots if state is not None else {}
        out: dict[str, tuple[float, float, float]] = {}
        for name in {*world.where, *spots}:
            spot = spots.get(name)
            point = (
                self._spot_point(spot, world, art)
                if spot is not None
                else designed_point(art, name)
            )
            if point is not None:
                out[name] = standing_region(art, point)
        return out

    def mark_explored(self, state: AreaState | None = None) -> None:
        """The region the author's character stands in is explored: out of
        the fog on the maps from now on."""
        art = self.world_art
        if not art.region_origin:
            return
        point = self.held_point(state)
        if point is None:
            return
        key = cell_key(*cell_of((art.region_origin[0], art.region_origin[1]), *point))
        if key in art.explored:
            return
        with self._lock():
            if key not in art.explored:
                art.explored.append(key)
                self._save_art(art)

    def _start_region_design(self) -> bool:
        """Design what is missing round the author's character, on a thread;
        True when it started. The grid is laid from where the story starts
        the first time."""
        if not self.regions_on or self.regions_running:
            return False
        world = self.world()
        point = self.held_point(world=world)
        if point is None:
            return False
        art = self.world_art
        if not art.region_origin:
            with self._lock():
                if not art.region_origin:
                    art.region_origin = list(origin_of(world, art))
                    self._save_art(art)
        self.mark_explored()
        origin = (art.region_origin[0], art.region_origin[1])
        wanted = [
            [cell for cell in wave if self._cell_wanted(world, art, origin, cell)]
            for wave in waves(origin, point)
        ]
        wanted = [wave for wave in wanted if wave]
        detached = getattr(self.provider, "detached", None)
        if not wanted or not callable(detached):
            return False
        clients = [detached() for _ in range(REGION_WORKERS)]
        self._region_clients = clients
        self._region_total, self._region_done = sum(len(wave) for wave in wanted), 0
        thread = BackgroundThread(
            target=self._design_waves,
            args=(wanted, self.region_model, clients),
            name="map-regions",
            daemon=True,
        )
        self._region_thread = thread
        thread.start()
        return True

    def _design_waves(
        self, wanted: list[list[tuple[int, int]]], model: str, clients: list[ChatProvider]
    ) -> None:
        """Each wave's regions side by side, a wave at a time."""
        self._doing("regions", f"designing the land around you, by {model}")
        try:
            for wave in wanted:
                if self._map_closed or self._art_stop:
                    return
                helpers = [
                    BackgroundThread(
                        target=self._design_one, args=(cell, model, client), daemon=True
                    )
                    for cell, client in zip(wave, clients, strict=False)
                ]
                for helper in helpers:
                    helper.start()
                for helper in helpers:
                    helper.join()
        finally:
            self._doing("regions", None)
            for client in clients:
                close = getattr(client, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001 - closing must never fail
                        pass

    def _region_setting(self) -> str:
        """The world as the story describes it, and its visible lore."""
        if self.story.chat:
            return (self.story.chat_prompt or self.story.title)[:SETTING_LIMIT]
        text = (self.story.world_bible or self.story.title or "")[:SETTING_LIMIT]
        lore = [
            f"- {e.title}: {' '.join((e.content or '').split())[:LORE_LIMIT]}"
            for e in self.visible_lore()[:LORE_ENTRIES]
            if getattr(e, "enabled", True)
        ]
        return text + ("\n\nLore:\n" + "\n".join(lore) if lore else "")

    def _region_story(self) -> str:
        """Where the story stands and what happened lately: the scene's
        place and situation, and the last few passages."""
        scene = self.story.scene
        lines = []
        if scene.location:
            lines.append(f"Where it stands: {scene.location}.")
        if scene.situation:
            lines.append(f"The situation: {scene.situation}")
        passages = [n.content for n in self.path() if n.kind == "assistant" and n.content]
        recent = "\n\n".join(passages[-STORY_PASSAGES:])
        if len(recent) > STORY_LIMIT:
            recent = "…" + recent[-STORY_LIMIT:]
        if recent:
            lines.append("Lately:\n" + recent)
        return "\n".join(lines) or "Nothing yet: the story is beginning."

    def _region_request(
        self, world: WorldMap, art: WorldArt, cell: tuple[int, int], model: str
    ) -> ChatRequest:
        origin = (art.region_origin[0], art.region_origin[1])
        rect = cell_rect(origin, *cell)
        west, north, side = rect
        drawn = [p for p in art.terrain if p.ground and not p.designed] or [
            p for p in art.terrain if p.ground
        ]
        ground = ""
        if drawn:
            cx, cy = west + side / 2, north - side / 2
            nearest = min(drawn, key=lambda p: (p.x - cx) ** 2 + (p.y - cy) ** 2)
            ground = self.texts.fill("map.terrain.nearby", ground=nearest.ground) + "."
        designed = self.designed_place_ids()
        messages = region_messages(
            self.texts,
            setting=self._region_setting(),
            story=self._region_story(),
            where=where_words(world, rect, self.held_point(world=world)),
            places=place_lines(world, rect, designed=designed),
            edges=edge_lines(art, origin, *cell),
            ground=ground,
            taken=taken_lines(
                art, story=[p.name for p in world.places.values() if p.id not in designed]
            ),
        )
        return ChatRequest(
            model=model,
            extra_body=self.route_for("story", model),
            messages=messages,
            params=self.side_params(model, max_tokens=REGION_MAX_TOKENS),
        )

    def _design_one(self, cell: tuple[int, int], model: str, client: ChatProvider) -> None:
        try:
            self._design_cell(cell, model, client)
        finally:
            with self._lock():
                self._region_done += 1

    def _design_cell(self, cell: tuple[int, int], model: str, client: ChatProvider) -> None:
        key = cell_key(*cell)
        failure = f"region:{key}"
        log_ref = None
        try:
            world, art = self.world(), self.map_art_copy()
            request = self._region_request(world, art, cell, model)
            log_ref = self._log(
                "map_region_request", {"region": key, "payload": client.build_payload(request)}
            )
            reply, completed = client.complete(request)
            self._log(
                "map_region_response",
                {"request_log_ref": log_ref, "text": reply, "usage": completed.raw_usage},
            )
            self.unreported_usage.append(self.priced(completed.usage, request.model))
            design = parse_region(reply)
        except (ProviderError, ValueError) as exc:
            if self._map_closed or self._art_stop:
                return  # stopped, not failed
            art = self.world_art
            with self._lock():
                art.failures[failure] = art.failures.get(failure, 0) + 1
                self._save_art(art)
            self._log("map_region_error", {"request_log_ref": log_ref, "error": str(exc)[:300]})
            return
        except Exception as exc:  # noqa: BLE001 - a thread dies silently otherwise
            self._log("map_region_error", {"request_log_ref": log_ref, "error": repr(exc)[:300]})
            return
        if self._map_closed or self._closed:
            return
        # Where the player stands, before the lock (it reads the map's art).
        player = self.held_point(world=world)
        # The map as it is now, for the labels: the story may have made an
        # unknown while the designer thought.
        latest = self.world()
        art = self.world_art
        with self._lock():
            if key in art.regions or not art.region_origin:
                return
            origin = (art.region_origin[0], art.region_origin[1])
            rect = cell_rect(origin, *cell)
            designed_ids = {r.place_id for r in art.regions.values()}
            holder = holding_place(world, *rect)
            holder_id = holder.id if holder is not None and holder.id not in designed_ids else None
            region, patch = build_region(
                design,
                key=key,
                rect=rect,
                holder_id=holder_id,
                labels_from=next_label(art, latest),
                model=model,
                made_at=utc_now_iso(),
                story=[p for p in world.places.values() if p.id not in designed_ids],
                player=player,
            )
            art.regions[key] = region
            art.terrain.append(patch)
            self._save_art(art)
        callback = self.on_map_art
        if callback is not None:
            callback()
