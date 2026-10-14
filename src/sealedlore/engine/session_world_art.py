"""The map's art, drawn in the background (engine/world_art.py,
storage/world_art.py).

After each map read, whatever the map still needs drawn goes to the map art
model on a thread with the provider's `detached()` clients (never the
shared sibling: the next passage's reads use it), nearest the story first.
Each piece is checked, kept and saved as it comes back, so nothing waits for
the art and nothing done is lost if the story is closed meanwhile. A
provider without `detached` (the scripted mock) draws inline.

Never in a private scene or a chat, and never two runs at once.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from sealedlore.engine.world_art import (
    LAYOUT_MAX_TOKENS,
    SPRITE_MAX_TOKENS,
    ArtJob,
    adopt,
    finish,
    plan_art,
    setting_of,
)
from sealedlore.ids import utc_now_iso
from sealedlore.models.world import WorldArt
from sealedlore.providers.base import ChatProvider, ChatRequest, ProviderError
from sealedlore.storage.world_art import load_world_art, save_world_art

# Pieces drawn at once in the background: enough to keep up with a passage,
# few enough to leave the subscription's burst for the story's own calls.
ART_WORKERS = 2


class WorldArtRuntime:
    _art: WorldArt | None = None
    _art_lock: threading.Lock | None = None
    _art_thread: threading.Thread | None = None
    _art_clients: list[ChatProvider] | None = None
    _art_stop = False
    # Called (on the art thread) after each piece is kept: the window's cue to
    # redraw the map.
    on_map_art: Callable[[], None] | None = None

    def _lock(self) -> threading.Lock:
        if self._art_lock is None:
            self._art_lock = threading.Lock()
        return self._art_lock

    @property
    def world_art(self) -> WorldArt:
        """The story's map art, loaded on first use. Read it under
        `map_art_copy()` from another thread."""
        with self._lock():
            if self._art is None:
                self._art = (
                    WorldArt() if self.memory_only else load_world_art(self.story.id, self.root)
                )
            return self._art

    def map_art_copy(self) -> WorldArt:
        art = self.world_art
        with self._lock():
            return art.model_copy(deep=True)

    @property
    def map_art_running(self) -> bool:
        thread = self._art_thread
        return thread is not None and thread.is_alive()

    def map_art_jobs(self, limit: int = 12) -> list[ArtJob]:
        """What the map at the leaf still needs drawn, nearest the story first."""
        if not self.map_on or not self.config.map_art or self.open_span is not None:
            return []
        world = self.world()
        held = self._character(self.story.held_character_id)
        here = world.where.get(held.name) if held is not None else None
        return plan_art(
            world,
            self.map_art_copy(),
            here=here,
            setting=setting_of(self.story.world_bible, self.story.title),
            texts=self.texts,
            limit=limit,
        )

    def start_map_art(self) -> bool:
        """Draw what the map needs, in the background. False when there is
        nothing to draw, a run is under way, or the map can't be drawn now."""
        if self.map_art_running:
            return False
        jobs = self.map_art_jobs()
        if not jobs:
            return False
        model = self.map_art_model
        requests = [self._art_request(job, model) for job in jobs]
        detached = getattr(self.provider, "detached", None)
        if not callable(detached):
            self._draw(list(zip(jobs, requests, strict=True)), [self.provider])
            return True
        clients = [detached() for _ in range(ART_WORKERS)]
        self._art_clients = clients
        self._art_stop = False
        thread = threading.Thread(
            target=self._draw,
            args=(list(zip(jobs, requests, strict=True)), clients),
            name="map-art",
            daemon=True,
        )
        self._art_thread = thread
        thread.start()
        return True

    def stop_map_art(self, wait_seconds: float = 0.0) -> None:
        """Stop drawing: what is out is cancelled, what came back is kept."""
        self._art_stop = True
        for client in self._art_clients or []:
            try:
                client.cancel()
            except Exception:  # noqa: BLE001 - stopping must never fail
                pass
        thread = self._art_thread
        if thread is not None and wait_seconds > 0:
            thread.join(wait_seconds)

    def _art_request(self, job: ArtJob, model: str) -> ChatRequest:
        tokens = SPRITE_MAX_TOKENS if job.kind == "sprite" else LAYOUT_MAX_TOKENS
        return ChatRequest(
            model=model,
            extra_body=self.route_for("map_art"),
            messages=job.messages,
            params=self.side_params(model, max_tokens=tokens),
        )

    def _draw(self, work: list[tuple[ArtJob, ChatRequest]], clients: list[ChatProvider]) -> None:
        """Each job on one of `clients`, in turn; each result kept and saved."""
        queue = list(work)
        queue_lock = threading.Lock()

        def worker(client: ChatProvider) -> None:
            while not self._art_stop:
                with queue_lock:
                    if not queue:
                        return
                    job, request = queue.pop(0)
                self._draw_one(job, request, client)

        if len(clients) == 1:
            worker(clients[0])
            return
        helpers = [threading.Thread(target=worker, args=(c,), daemon=True) for c in clients[1:]]
        for helper in helpers:
            helper.start()
        worker(clients[0])
        for helper in helpers:
            helper.join()

    def _draw_one(self, job: ArtJob, request: ChatRequest, client: ChatProvider) -> None:
        log_ref = self._log(
            "map_art_request",
            {"job": job.key, "payload": client.build_payload(request)},
        )
        try:
            text, completed = client.complete(request)
            self._log(
                "map_art_response",
                {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
            )
            self.unreported_usage.append(self.priced(completed.usage, request.model))
            drawn = finish(job, text, model=request.model, made_at=utc_now_iso())
        except (ProviderError, ValueError) as exc:
            if self._art_stop:
                return  # stopped, not failed
            art = self.world_art
            with self._lock():
                art.failures[job.key] = art.failures.get(job.key, 0) + 1
                self._save_art(art)
            self._log("map_art_refused", {"request_log_ref": log_ref, "reason": str(exc)[:300]})
            return
        art = self.world_art
        with self._lock():
            adopt(art, drawn)
            self._save_art(art)
        callback = self.on_map_art
        if callback is not None:
            callback()

    def _save_art(self, art: WorldArt) -> None:
        if not self.memory_only:
            save_world_art(self.story.id, art, self.root)
