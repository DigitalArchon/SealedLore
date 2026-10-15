"""The map's art, drawn in the background (engine/world_art.py,
storage/world_art.py).

After each map read, whatever the map still needs drawn goes to the map art
model on a thread with the provider's `detached()` clients (never the
shared sibling: the next passage's reads use it), nearest the story first.
Each piece is checked, kept and saved as it comes back, so nothing waits for
the art and nothing done is lost if the story is closed meanwhile. A
provider without `detached` (the scripted mock) draws inline.

Map pictures from an image model (`Config.map_pictures`, the author's
standing approval: engine/world_pictures.py) go on a thread of their own,
one at a time, each counted against the story's daily limit before it is
sent, logged with its prompt, and its file stripped of hidden data like any
picture.

Never in a private scene or a chat, and never two runs at once.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime

from sealedlore.engine.world_art import (
    LAYOUT_MAX_TOKENS,
    SPRITE_MAX_TOKENS,
    ArtJob,
    adopt,
    cleaned_art,
    finish,
    land_under,
    plan_art,
    plan_ground,
    setting_of,
)
from sealedlore.engine.world_pictures import (
    MAP_PICTURES_DIR,
    Person,
    PictureJob,
    isometric_job,
    isometric_notes,
    isometric_state,
    isometric_writer_messages,
    looks_of,
    picture_size,
    plan_pictures,
)
from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.world import MapPicture, WorldArt
from sealedlore.providers.base import ChatProvider, ChatRequest, ProviderError
from sealedlore.providers.images import (
    ImageClient,
    ImageModelInfo,
    build_image_payload,
    data_url,
    list_image_models,
    loggable_payload,
    parse_image_models,
)
from sealedlore.storage.image_meta import MetadataError, picture_kind, strip_metadata
from sealedlore.storage.images import MEDIA_TYPES
from sealedlore.storage.world_art import load_world_art, save_world_art

# The isometric view's scene: a paragraph, after the writer's reasoning.
ISOMETRIC_SCENE_TOKENS = 4000

# Pieces drawn at once in the background: enough to keep up with a passage,
# few enough to leave the subscription's burst for the story's own calls.
ART_WORKERS = 2


class WorldArtRuntime:
    _art: WorldArt | None = None
    _art_lock: threading.Lock | None = None
    _art_thread: threading.Thread | None = None
    _art_clients: list[ChatProvider] | None = None
    _art_stop = False
    _picture_thread: threading.Thread | None = None
    _picture_client: ImageClient | None = None
    _picture_model_info: ImageModelInfo | None = None
    # Each run's pieces: how many to draw and how many are done (drawn,
    # failed or stopped), for the map's progress (`map_art_progress`).
    _art_total = 0
    _art_done = 0
    _picture_total = 0
    _picture_done = 0
    _isometric_thread: threading.Thread | None = None
    _isometric_area: str | None = None
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
                    WorldArt()
                    if self.memory_only
                    else cleaned_art(load_world_art(self.story.id, self.root))
                )
            return self._art

    def map_art_copy(self) -> WorldArt:
        art = self.world_art
        with self._lock():
            return art.model_copy(deep=True)

    def map_art_progress(self) -> tuple[int, int]:
        """(done, of) for the art and pictures being drawn now; (0, 0) when
        nothing is. The author asked to see the map in progress and done."""
        done = total = 0
        thread = self._art_thread
        if thread is not None and thread.is_alive():
            done, total = done + self._art_done, total + self._art_total
        thread = self._picture_thread
        if thread is not None and thread.is_alive():
            done, total = done + self._picture_done, total + self._picture_total
        if self.isometric_painting is not None:
            total += 1
        return min(done, total), total

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
            sprites=not self.map_pictures_on,
        )

    # --- pictures from an image model -------------------------------------------

    @property
    def map_pictures_on(self) -> bool:
        """The author has turned map pictures on and there is somewhere to
        send them; never in a private scene."""
        if not (self.map_on and self.config.map_art and self.config.map_pictures):
            return False
        media = self.config.images()
        return self.open_span is None and media is not None and bool(media.api_key)

    def map_pictures_left_today(self, art: WorldArt | None = None) -> int:
        art = art or self.map_art_copy()
        return max(0, self.config.map_picture_daily_limit - art.pictures_by_day.get(_today(), 0))

    def map_picture_jobs(self, limit: int = 6) -> list[PictureJob]:
        """What the map at the leaf still needs from the image model."""
        if not self.map_pictures_on:
            return []
        art = self.map_art_copy()
        room = self.map_pictures_left_today(art)
        if room <= 0:
            return []
        info = self._picture_model()
        held = self._character(self.story.held_character_id)
        people = [
            Person(c.name, looks_of(c.summary, c.full_description))
            for c in (*self.visible_cast(), *self.visible_supporting())
        ]
        return plan_pictures(
            self.world(),
            art,
            people=people,
            held=held.name if held is not None else None,
            setting=setting_of(self.story.world_bible, self.story.title),
            edits=info is not None and info.accepts_images,
            texts=self.texts,
            limit=min(limit, room),
        )

    def _picture_model(self) -> ImageModelInfo | None:
        """The map picture model's listing entry, from the config's copy of the
        listing (Settings fetches it); None when it isn't there."""
        model = self.config.map_picture_model
        if self._picture_model_info is not None and self._picture_model_info.id == model:
            return self._picture_model_info
        found = self._listed_image_model(model)
        self._picture_model_info = found
        return found

    def _listed_image_model(self, model: str) -> ImageModelInfo | None:
        return next(
            (m for m in parse_image_models({"data": self.config.image_models}) if m.id == model),
            None,
        )

    # --- The isometric view (Map → Isometric) ----------------------------------------

    @property
    def isometric_painting(self) -> str | None:
        """The area whose isometric view is being painted now, or None."""
        thread = self._isometric_thread
        return self._isometric_area if thread is not None and thread.is_alive() else None

    def isometric_offer(self) -> tuple[str, float | None, str | None]:
        """(model, its listed price for the view, why it can't be painted now
        or None). The author asks for each view: it is never painted on its
        own (Oct 15 2026)."""
        model = self.config.map_isometric_model
        info = self._listed_image_model(model)
        price = info.price(picture_size(info, landscape=True)) if info is not None else None
        if not self.map_on or self.open_span is not None:
            return model, price, "Not in a private scene."
        media = self.config.images()
        if media is None or not media.api_key:
            return model, price, "Set up an image endpoint in Settings → Images first."
        if info is not None and not info.accepts_images:
            return model, price, f"{model} can't be given the map to paint from."
        if self.map_pictures_left_today() <= 0:
            return model, price, "Today's map pictures for this story are used up."
        if self.isometric_painting is not None:
            return model, price, "A view is being painted."
        return model, price, None

    def start_isometric(self, area_id: str, render: Callable[[], bytes | None]) -> bool:
        """Paint the isometric view of an area, in the background: `render`
        gives the picture of its plan (gui/map_render.py: the engine draws
        nothing), called on that thread."""
        _model, _price, problem = self.isometric_offer()
        world = self.world()
        if problem is not None or area_id not in world.places:
            return False
        media = self.config.images()
        assert media is not None
        art = self.map_art_copy()
        area = world.places[area_id]
        layout = art.layout_for(area_id, area.state) or art.layout_for(area_id)
        ground = str(layout.spec.get("ground", "")) if layout is not None else ""
        look = plan_ground(world, art, area, ground)
        if look == "metal" and "space" in (ground, land_under(world, art, area)):
            look = "space"  # a deck in space, not plating on the ground
        texts = self.texts

        def job(places: str | None) -> PictureJob:
            return isometric_job(world, art, area_id, look=look, texts=texts, places=places)

        client = ImageClient(media.base_url, media.api_key, api=media.api)
        self._isometric_area = area_id
        thread = threading.Thread(
            target=self._paint_isometric,
            args=(job, isometric_notes(world, art, area_id), client, render),
            name="map-isometric",
            daemon=True,
        )
        self._isometric_thread = thread
        thread.start()
        return True

    def _paint_isometric(
        self,
        make_job: Callable[[str | None], PictureJob],
        notes: str,
        client: ImageClient,
        render: Callable[[], bytes | None],
    ) -> None:
        try:
            try:
                scene = self._isometric_scene(notes)
            except Exception:  # noqa: BLE001 - painted from the map's own words instead
                scene = None
            job = make_job(scene)
            plan = render()
            if plan is None:
                self._log("map_picture_error", {"job": job.key, "error": "no plan to paint from"})
                return
            model = self.config.map_isometric_model
            info = self._listed_image_model(model)
            if info is None:  # not in the config's listing: ask the endpoint once
                listing = list_image_models(client.base_url, api=client.api)
                info = next((m for m in listing if m.id == model), None)
            size = picture_size(info, landscape=True)
            self._picture_one(job, client, size, model=model, info=info, plan=plan)
        finally:
            client.close()
            callback = self.on_map_art
            if callback is not None:
                callback()  # the map learns it is done, painted or not

    def use_own_isometric(self, area_id: str, data: bytes, suffix: str) -> bool:
        """The author's own picture (already prepared: stripped, cropped) as
        an area's isometric view, of the map as it is now. Not in a private
        scene, and only for a place on the map."""
        if not self.map_on or self.open_span is not None:
            return False
        world = self.world()
        area = world.places.get(area_id)
        if area is None:
            return False
        picture_id = new_id()
        relative = f"{MAP_PICTURES_DIR}/{picture_id}{suffix}"
        self.pictures.write(relative, data)
        picture = MapPicture(
            id=picture_id,
            subject="isometric",
            subject_id=area_id,
            name=area.name,
            state=isometric_state(world, area_id),
            file=relative,
            made_at=utc_now_iso(),
        )
        art = self.world_art
        with self._lock():
            art.pictures.append(picture)
            self._save_art(art)
        return True

    def _isometric_scene(self, notes: str) -> str | None:
        """What is on the area in what a picture shows, from the map's own
        words, by the prompt writer (map.picture.isometric.writer), or None
        when it fails: the view is then painted from the words as they are.
        On the painting's thread."""
        model = self.image_prompt_model
        detached = getattr(self.provider, "detached", None)
        writer = detached() if callable(detached) else self.provider
        try:
            params = self.side_params(model, max_tokens=ISOMETRIC_SCENE_TOKENS, temperature=0.3)
            request = ChatRequest(
                model=model,
                extra_body=self.route_for("image_prompt", model),
                messages=isometric_writer_messages(notes, self.texts),
                params=params,
            )
            log_ref = self._log(
                "isometric_prompt_request", {"payload": writer.build_payload(request)}
            )
            try:
                text, completed = writer.complete(request)
            except Exception as exc:  # painted from the map's own words instead
                self._log("isometric_prompt_error", {"request_log_ref": log_ref, "error": str(exc)})
                return None
            self._log(
                "isometric_prompt_response",
                {"request_log_ref": log_ref, "text": text, "usage": completed.raw_usage},
            )
            self.unreported_usage.append(self.priced(completed.usage, model))
            scene = " ".join(text.split())
            return scene if scene and completed.finish_reason != "length" else None
        finally:
            if writer is not self.provider:
                writer.close()

    def start_map_pictures(self) -> bool:
        """Draw what the map needs from the image model, in the background."""
        thread = self._picture_thread
        if thread is not None and thread.is_alive():
            return False
        jobs = self.map_picture_jobs()
        media = self.config.images()
        if not jobs or media is None:
            return False
        self._picture_total, self._picture_done = len(jobs), 0
        client = ImageClient(media.base_url, media.api_key, api=media.api)
        self._picture_client = client
        self._art_stop = False
        info = self._picture_model()
        thread = threading.Thread(
            target=self._picture_all,
            args=(jobs, client, info),
            name="map-pictures",
            daemon=True,
        )
        self._picture_thread = thread
        thread.start()
        return True

    def _picture_all(
        self, jobs: list[PictureJob], client: ImageClient, info: ImageModelInfo | None
    ) -> None:
        try:
            if info is None:  # not in the config's listing: ask the endpoint once
                listing = list_image_models(client.base_url, api=client.api)
                info = next((m for m in listing if m.id == self.config.map_picture_model), None)
                self._picture_model_info = info
            sizes = {False: picture_size(info), True: picture_size(info, landscape=True)}
            for job in jobs:
                if self._art_stop:
                    return
                self._picture_one(job, client, sizes[job.subject == "postcard"])
                self._picture_done += 1
        finally:
            client.close()

    def _picture_one(
        self,
        job: PictureJob,
        client: ImageClient,
        size: str,
        *,
        model: str | None = None,
        info: ImageModelInfo | None = None,
        plan: bytes | None = None,
    ) -> None:
        """One map picture: counted against the day's limit first, drawn,
        stripped, kept. `model` and `info` stand in for the map picture
        model's; `plan` is the picture of the map to paint from (the
        isometric view), sent before any reference."""
        model = model or self.config.map_picture_model
        art = self.world_art
        with self._lock():
            if self.map_pictures_left_today(art) <= 0:
                return
            day = _today()
            art.pictures_by_day[day] = art.pictures_by_day.get(day, 0) + 1
            self._save_art(art)
        references: list[str] = []
        names: list[str] = []
        if plan is not None:
            try:
                plan = strip_metadata(plan)
            except MetadataError:
                plan = None
            if plan is not None:
                references.append(data_url(plan, MEDIA_TYPES[picture_kind(plan)]))
                names.append(f"the map of {job.name}")
        if job.reference:
            data = self.pictures.read(job.reference)
            try:
                data = strip_metadata(data) if data is not None else None
            except MetadataError:
                data = None  # never sent unstripped: drawn afresh instead
            if data is not None:
                references.append(data_url(data, MEDIA_TYPES[picture_kind(data)]))
                names.append(f"{job.name}: {job.reference}")
        payload = build_image_payload(model, job.prompt, size, 1, references, api=client.api)
        log_ref = self._log(
            "map_picture_request",
            {"job": job.key, "payload": loggable_payload(payload, names)},
        )
        try:
            reply = client.generate(payload)
            data = strip_metadata(reply.images[0])
        except (ProviderError, MetadataError, IndexError) as exc:
            if self._art_stop:
                return
            with self._lock():
                art.failures[job.key] = art.failures.get(job.key, 0) + 1
                self._save_art(art)
            self._log("map_picture_error", {"request_log_ref": log_ref, "error": str(exc)[:300]})
            return
        info = info or self._picture_model_info
        cost = reply.cost
        usage: dict = {"cost": cost or 0.0, "cost_reported": cost is not None}
        if cost is None and info is not None and info.price(size) is not None:
            usage = {"cost": info.price(size), "cost_estimated": True}
        self._log("map_picture_response", {"request_log_ref": log_ref, "usage": usage})
        picture_id = new_id()
        relative = f"{MAP_PICTURES_DIR}/{picture_id}{picture_kind(data) or '.png'}"
        self.pictures.write(relative, data)
        picture = MapPicture(
            id=picture_id,
            subject=job.subject,
            subject_id=job.subject_id,
            name=job.name,
            state=job.state,
            file=relative,
            from_picture=job.from_picture,
            prompt=job.prompt,
            model=model,
            made_at=utc_now_iso(),
        )
        with self._lock():
            art.pictures.append(picture)
            self._save_art(art)
        callback = self.on_map_art
        if callback is not None:
            callback()

    def start_map_art(self) -> bool:
        """Draw what the map needs, in the background. False when there is
        nothing to draw, a run is under way, or the map can't be drawn now."""
        pictures = self.start_map_pictures()
        if self.map_art_running:
            return pictures
        jobs = self.map_art_jobs()
        if not jobs:
            return pictures
        model = self.map_art_model
        self._art_total, self._art_done = len(jobs), 0
        detached = getattr(self.provider, "detached", None)
        if not callable(detached):
            self._draw(jobs, model, [self.provider])
            return True
        clients = [detached() for _ in range(ART_WORKERS)]
        self._art_clients = clients
        self._art_stop = False
        # The requests are built on the art thread (`_draw_one`): their
        # reasoning level may need the model listing, fetched on first use,
        # and a story opening on its map starts this from the GUI thread.
        thread = threading.Thread(
            target=self._draw,
            args=(jobs, model, clients),
            name="map-art",
            daemon=True,
        )
        self._art_thread = thread
        thread.start()
        return True

    def redraw_plan(self, area_id: str) -> bool:
        """Draw a place's plan again, when the author asks (Map → Redraw
        plan…, Oct 15 2026): plans are drawn once and kept, so one drawn
        badly (Deep Space 9's, before decks were asked for, was rooms
        scattered in space) stayed so. Every state's plan of the place goes,
        and its failures, and the art runs again; the inside plans of the
        buildings in it stay, fitted over the boxes the new plan draws. False
        when it can't be now (no map, a private scene, art already running)."""
        if not self.map_on or self.open_span is not None or self.map_art_running:
            return False
        art = self.world_art
        with self._lock():
            if not art.layouts.pop(area_id, None):
                return False
            prefix = f"layout:{area_id}"
            for key in [k for k in art.failures if k == prefix or k.startswith(prefix + ":")]:
                del art.failures[key]
            saved = art.model_copy(deep=True)
        self._save_art(saved)
        self.start_map_art()
        return True

    def stop_map_art(self, wait_seconds: float = 0.0) -> None:
        """Stop drawing: what is out is cancelled, what came back is kept."""
        self._art_stop = True
        for client in self._art_clients or []:
            try:
                client.cancel()
            except Exception:  # noqa: BLE001 - stopping must never fail
                pass
        client = self._picture_client
        if client is not None:
            client.cancel()
        for thread in (self._art_thread, self._picture_thread, self._isometric_thread):
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

    def _draw(self, jobs: list[ArtJob], model: str, clients: list[ChatProvider]) -> None:
        """Each job on one of `clients`, in turn; each result kept and saved."""
        queue = list(jobs)
        queue_lock = threading.Lock()

        def worker(client: ChatProvider) -> None:
            while not self._art_stop:
                with queue_lock:
                    if not queue:
                        return
                    job = queue.pop(0)
                self._draw_one(job, model, client)
                with queue_lock:
                    self._art_done += 1

        if len(clients) == 1:
            worker(clients[0])
            return
        helpers = [threading.Thread(target=worker, args=(c,), daemon=True) for c in clients[1:]]
        for helper in helpers:
            helper.start()
        worker(clients[0])
        for helper in helpers:
            helper.join()

    def _draw_one(self, job: ArtJob, model: str, client: ChatProvider) -> None:
        request = self._art_request(job, model)
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


def _today() -> str:
    """The UTC day, as the daily limit counts it."""
    return datetime.now(UTC).date().isoformat()
