"""The map's art, drawn in the background (engine/world_art.py,
storage/world_art.py).

After each map read, whatever the map still needs drawn goes to the map art
model on a thread with the provider's `detached()` clients (never the
shared sibling: the next passage's reads use it), nearest the story first.
Each piece is checked, kept and saved as it comes back, so nothing waits for
the art and nothing done is lost if the story is closed meanwhile. A
provider without `detached` (the scripted mock) draws inline.

Map pictures from an image model (`Config.map_pictures_mode`: the author's
standing approval, or each run's pictures approved first,
engine/session_map_approval.py; engine/world_pictures.py) go on a thread of their own,
one at a time, each counted against the story's daily limit before it is
sent, logged with its prompt, and its file stripped of hidden data like any
picture.

Never in a private scene, and never two runs at once. A simple chat that
keeps a map is drawn like a story (its system prompt stands for the setting);
map pictures from an image model stay off in a TEE chat, which is private
throughout, since the image endpoint is not.
"""

from __future__ import annotations

import json
import math
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from sealedlore.engine.area import now_words
from sealedlore.engine.image_variants import fitting_model
from sealedlore.engine.regions import unexplored_ids
from sealedlore.engine.takeable import the
from sealedlore.engine.world_art import (
    LAYOUT_MAX_TOKENS,
    MAX_FAILURES,
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
    isometric_size,
    isometric_state,
    looks_of,
    picture_size,
    plan_pictures,
    size_pixels,
    views_of,
)
from sealedlore.ids import new_id, utc_now_iso
from sealedlore.models.node import Usage
from sealedlore.models.world import MapPicture, WorldArt, WorldMap
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
from sealedlore.storage.images import MEDIA_TYPES, WORLD_ART_FILE
from sealedlore.storage.world_art import load_world_art, save_world_art
from sealedlore.threads import BackgroundThread

# Pieces drawn at once in the background: enough to keep up with a passage,
# few enough to leave the subscription's burst for the story's own calls.
ART_WORKERS = 2
# "All" the map wants, for a listing with no cap of its own.
ALL_WANTED = 10_000
# Map pictures one run of the picture thread draws; the rest go to the next.
PICTURE_RUN = 6


def job_words(job: PictureJob) -> str:
    """What a map picture is of, in a few words for the Map page."""
    if job.subject == "item":
        return the(job.name)
    if job.subject == "walker":
        return f"{job.name}'s avatar"
    if job.subject == "postcard":
        return f"a picture of {job.name}"
    if job.subject == "texture":
        return "a ground or roof texture"
    if job.subject == "isometric":
        return f"painting {job.name}"
    if job.subject == "backdrop":
        return f"the backdrop of {job.name}"
    return job.name


class WorldArtRuntime:
    _art_version: int = 0
    _art_kept: tuple[tuple, WorldArt] | None = None
    _art: WorldArt | None = None
    _art_lock: threading.Lock | None = None
    _art_thread: threading.Thread | None = None
    _art_clients: list[ChatProvider] | None = None
    _art_stop = False
    # Stopped for good: the story was left (`stop_map_art`, from `close`).
    _map_closed = False
    _picture_thread: threading.Thread | None = None
    _picture_client: ImageClient | None = None
    _picture_model_info: ImageModelInfo | None = None
    # Each run's pieces: how many to draw and how many are done (drawn,
    # failed or stopped), for the map's progress (`map_art_progress`).
    _art_total = 0
    _art_done = 0
    _picture_total = 0
    _picture_done = 0
    _pictures_again = False
    _isometric_thread: threading.Thread | None = None
    _isometric_area: str | None = None
    _isometric_client: ImageClient | None = None
    # The view's scene writer, while it writes: cancelled with the rest.
    # Held while a run is checked for and started: the worker (after a
    # passage) and the GUI thread (Redraw plan…, a story opening) both start
    # runs, and two started at once drew and paid for everything twice.
    _start_lock: threading.Lock | None = None
    # Called (on the art thread) after each piece is kept: the window's cue to
    # redraw the map.
    on_map_art: Callable[[], None] | None = None

    def _lock(self) -> threading.Lock:
        if self._art_lock is None:
            self._art_lock = threading.Lock()
        return self._art_lock

    def _starting(self) -> threading.Lock:
        if self._start_lock is None:
            self._start_lock = threading.Lock()
        return self._start_lock

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

    def _art_read(self) -> WorldArt:
        """The art as it stands, for reading only: one copy per version of
        it (`_save_art` bumps it), shared by every read until the art
        changes. Every change is made on `world_art` under the lock, never
        on a copy; a copy of 27 designed regions cost 40 ms, and opening a
        story took 219 of them (Oct 19 2026). Writers take `map_art_copy`."""
        art = self.world_art
        # The version, and the shape of the art beside it: a layout, patch,
        # region, picture or set added or replaced is seen even when nobody
        # saved (tests poke the art directly; the app saves every change).
        mark = (
            self._art_version,
            tuple((k, id(v)) for k, v in art.layouts.items()),
            tuple(id(p) for p in art.terrain),
            tuple((k, id(v)) for k, v in art.regions.items()),
            len(art.pictures),
            tuple((k, id(v)) for k, v in art.furnishings.items()),
            tuple((k, id(v)) for k, v in art.tile_sets.items()),
            tuple(sorted(art.tile_overrides.items())),
            art.tile_set,
        )
        kept = self._art_kept
        if kept is not None and kept[0] == mark:
            return kept[1]
        copy = self.map_art_copy()
        self._art_kept = (mark, copy)
        return copy

    def backup_files(self) -> dict[str, bytes]:
        """Everything a backup carries beside the bundle: the pictures and
        their records, and for a chat kept in memory only, its map's art,
        which on disk sits in the story's folder with them and from memory
        was lost (the review, Oct 15 2026)."""
        files = self.pictures.all_files()
        if self.memory_only:
            art = self.map_art_copy()
            if art != WorldArt():
                files[WORLD_ART_FILE] = json.dumps(art.model_dump(), indent=2).encode("utf-8")
        return files

    def restore_memory_art(self, files: dict[str, bytes]) -> dict[str, bytes]:
        """A memory chat's map art from its backup's files (taken out of
        them; the rest are its pictures)."""
        files = dict(files)
        data = files.pop(WORLD_ART_FILE, None)
        if data is not None:
            try:
                art = cleaned_art(WorldArt.model_validate_json(data))
            except ValueError:
                art = None
            if art is not None:
                with self._lock():
                    self._art = art
        return files

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
        thread = getattr(self, "_tile_thread", None)
        if thread is not None and thread.is_alive():
            done, total = done + self._tile_done, total + self._tile_total
        thread = getattr(self, "_own_set_thread", None)
        if thread is not None and thread.is_alive():
            done, total = done + self._own_set_done, total + self._own_set_total
        thread = getattr(self, "_region_thread", None)  # the world designer (session_regions.py)
        if thread is not None and thread.is_alive():
            done, total = done + self._region_done, total + self._region_total
        if self.isometric_painting is not None:
            total += 1
        if getattr(self, "tile_asset_drawing", None) is not None:
            total += 1
        return min(done, total), total

    def _doing(self, run: str, words: str | None) -> None:
        """What a run is drawing now, for the Map page (None: nothing)."""
        now = self.__dict__.setdefault("_now", {})
        if words:
            now[run] = words
        else:
            now.pop(run, None)

    def model_name(self, model: str) -> str:
        """A picture model as the listing names it, else its id."""
        info = self._listed_image_model(model)
        return info.name if info is not None and info.name else model

    def map_art_now(self) -> str:
        """What the map's runs are drawing now, and with what (the author,
        Oct 18 2026: ten pictures by FLUX.3 Klein appeared while a painting
        by Seedream was asked for, and nothing said they were the room's
        furniture): "the gun cabinet, by FLUX.3 Image Klein; painting the
        farmhouse, by Seedream 5.0 Pro"; "" when nothing is drawing."""
        now = dict(self.__dict__.get("_now", {}))
        alive = {
            "tile": getattr(self, "_tile_thread", None),
            "pictures": self._picture_thread,
            "painting": self._isometric_thread,
            "backdrop": getattr(self, "_backdrop_thread", None),
            "regions": getattr(self, "_region_thread", None),
        }
        return "; ".join(
            words
            for run, words in now.items()
            if alive.get(run) is not None and alive[run].is_alive()
        )

    @property
    def map_art_wanted_again(self) -> bool:
        """A picture or tile run was asked for while one was drawing, and
        nothing has run since: the window starts one once the map is idle
        (live, portraits landed while the avatars were being drawn from the
        cards' words, and nothing drew them again from the portraits)."""
        return self._pictures_again or getattr(self, "_tile_again", False)

    @property
    def map_art_running(self) -> bool:
        thread = self._art_thread
        return thread is not None and thread.is_alive()

    def backdrop_running(self) -> bool:
        thread = getattr(self, "_backdrop_thread", None)
        return thread is not None and thread.is_alive()

    def map_art_failed(self) -> list[str]:
        """What the map gave up drawing (a job refused MAX_FAILURES times:
        `plan_art` never asks for it again), by name, for the Map page to say
        (live, a rift's sprite was refused twice and nothing said so: "it
        said it was generating an image, but no image appeared")."""
        art = self.map_art_copy()
        world = self.world()
        out: list[str] = []
        for key, count in art.failures.items():
            if count < MAX_FAILURES:
                continue
            kind, _, rest = key.partition(":")
            subject_id = rest.split(":")[0]
            place = world.places.get(subject_id)
            thing = world.things.get(subject_id)
            name = place.name if place else thing.name if thing else ""
            if kind == "layout":
                out.append(f"the plan of {name}" if name else "a plan")
            elif kind == "terrain":
                out.append("a stretch of land")
            elif kind == "furnish":
                out.append(f"the furnishing of {name}" if name else "a furnishing")
            else:
                out.append(name or key)
        return list(dict.fromkeys(out))

    def retry_map_art(self) -> bool:
        """Forget what the map gave up on and draw again (the author's
        Try again on the Map page); False when nothing had failed or the
        map can't be drawn now."""
        art = self.world_art
        with self._lock():
            failed = [k for k, c in art.failures.items() if c >= MAX_FAILURES]
            if not failed:
                return False
            for key in failed:
                art.failures.pop(key, None)
            self._save_art(art)
        return self.start_map_art()

    def map_art_jobs(self, limit: int = 12) -> list[ArtJob]:
        """What the map at the leaf still needs drawn, nearest the story first."""
        if not self.map_on or not self.config.map_art or self.open_span is not None:
            return []
        world = self.world()
        held = self.map_held_name(self.story.held_character_id)
        here = world.where.get(held) if held is not None else None
        return plan_art(
            world,
            self.map_art_copy(),
            here=here,
            setting=self.map_setting(),
            texts=self.texts,
            limit=limit,
            sprites=not self.map_pictures_on,
            # Map-first: where the character stands, for the window of open
            # country under them and the ones its edges lead to; none once the
            # world designer lays open country out (engine/regions.py).
            point=self.held_point(world=world) if self.map_first and not self.regions_on else None,
        )

    def map_setting(self) -> str:
        """The setting, briefly, for the art: the world's opening words; a
        chat's system prompt, which is all a chat says of its world."""
        text = self.story.chat_prompt if self.story.chat else self.story.world_bible
        return setting_of(text, self.story.title)

    # --- pictures from an image model -------------------------------------------

    @property
    def map_pictures_on(self) -> bool:
        """Map pictures are on (drawn on their own, or each asked for:
        `map_pictures_asking`) and there is somewhere to send them; never in
        a private scene, nor in a TEE chat (private throughout, which the
        image endpoint is not)."""
        if not (self.map_on and self.config.map_art and self.config.map_pictures_mode != "off"):
            return False
        if self.tee_chat:
            return False
        media = self.config.images()
        return self.open_span is None and media is not None and bool(media.api_key)

    def map_pictures_left_today(self, art: WorldArt | None = None) -> int:
        art = art or self.map_art_copy()
        return max(0, self.config.map_picture_daily_limit - art.pictures_by_day.get(_today(), 0))

    def map_picture_jobs(self, limit: int | None = PICTURE_RUN) -> list[PictureJob]:
        """What the map at the leaf still needs from the image model, at
        most `limit` (None: all of it; the day's limit caps the run, not the
        wanting: `_start_map_pictures`)."""
        if not self.map_pictures_on:
            return []
        art = self.map_art_copy()
        if limit is None:
            limit = ALL_WANTED
        info = self._picture_model()
        # A chat's user has no card to draw an avatar from: the map's own
        # figure stands for them. Played on the map, everyone has an avatar
        # (the tile run's walkers), and the plan shows that: a picture of
        # each person besides was a second one, paid twice (Oct 18 2026).
        people = (
            []
            if self.story.chat or self.map_first
            else [
                Person(c.name, looks_of(c.summary, c.full_description))
                for c in (*self.visible_cast(), *self.visible_supporting())
            ]
        )
        return plan_pictures(
            self.world(),
            art,
            people=people,
            held=self.map_held_name(self.story.held_character_id),
            setting=self.map_setting(),
            edits=info is not None and self.takes_pictures(info.id),
            texts=self.texts,
            limit=limit,
            style=self.tile_style(art),
            # Nothing the world designer made until the author's character
            # has been in its region (the review, Oct 20 2026).
            hidden=unexplored_ids(art),
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
        price = info.price(isometric_size(info)) if info is not None else None
        if not self.map_on or self.open_span is not None:
            return model, price, "Not in a private scene."
        if self.tee_chat:
            # Private throughout; the image endpoint isn't (as map pictures).
            return model, price, "Not in a TEE chat: the image endpoint isn't private."
        media = self.config.images()
        if media is None or not media.api_key:
            return model, price, "Set up an image endpoint in Settings → Images first."
        if info is not None and not self.takes_pictures(model):
            return model, price, f"{model} can't be given the map to paint from."
        if self.map_pictures_left_today() <= 0:
            return model, price, "Today's map pictures for this story are used up."
        if self.isometric_painting is not None:
            return model, price, "A view is being painted."
        return model, price, None

    def start_isometric(
        self, area_id: str, render: Callable[[tuple[int, int]], bytes | None]
    ) -> bool:
        """Paint the isometric view of an area, in the background: `render`
        gives the program's isometric render of its plan at a size in pixels
        (gui/map_iso.py: the engine draws nothing), called on that thread."""
        with self._starting():
            return self._start_isometric(area_id, render)

    def _start_isometric(
        self, area_id: str, render: Callable[[tuple[int, int]], bytes | None]
    ) -> bool:
        _model, _price, problem = self.isometric_offer()
        world = self.world()
        if problem is not None or area_id not in world.places or self._map_closed:
            return False
        media = self.config.images()
        assert media is not None
        art = self.map_art_copy()
        area = world.places[area_id]
        layout = art.layout_shown(area_id, area.state)
        ground = str(layout.spec.get("ground", "")) if layout is not None else ""
        look = plan_ground(world, art, area, ground)
        if look == "metal" and "space" in (ground, land_under(world, art, area)):
            look = "space"  # a deck in space, not plating on the ground
        when, people, things = self.painting_scene(area_id, world, art)
        job = isometric_job(
            world,
            art,
            area_id,
            look=look,
            style=self.tile_style(art),
            texts=self.texts,
            when=when,
            people=people,
            things=things,
        )
        client = ImageClient(media.base_url, media.api_key, api=media.api)
        self._isometric_area = area_id
        self._isometric_client = client  # for `stop_map_art` to cancel
        thread = BackgroundThread(
            target=self._paint_isometric,
            args=(job, client, render),
            name="map-isometric",
            daemon=True,
        )
        self._isometric_thread = thread
        thread.start()
        return True

    def _paint_isometric(
        self,
        job: PictureJob,
        client: ImageClient,
        render: Callable[[tuple[int, int]], bytes | None],
    ) -> None:
        try:
            model = self.config.map_isometric_model
            self._doing("painting", f"painting {job.name}, by {self.model_name(model)}")
            info = self._listed_image_model(model)
            if info is None:  # not in the config's listing: ask the endpoint once
                listing = list_image_models(client.base_url, api=client.api)
                info = next((m for m in listing if m.id == model), None)
            size = isometric_size(info)
            # The render is made at the size the model will paint: one shape
            # in and out (the author's rule for image-to-image).
            plan = render(size_pixels(size))
            if plan is None:
                self._log("map_picture_error", {"job": job.key, "error": "no plan to paint from"})
                return
            self._picture_one(job, client, size, model=model, info=info, plan=plan)
        finally:
            self._doing("painting", None)
            client.close()
            self._isometric_client = None
            callback = self.on_map_art
            if callback is not None:
                callback()  # the map learns it is done, painted or not

    def painting_scene(
        self, area_id: str, world: WorldMap | None = None, art: WorldArt | None = None
    ) -> tuple[str, list[str], list[str]]:
        """What a painting of `area_id` shows besides the map (the author,
        Oct 17 2026: the characters and items present at the time it is
        taken): the time of day and the weather (`area.now_words`, never
        the day), the people there by what their cards say they look like
        (never their names: no writing on the picture), and the things
        standing about by name, those nearest the author's character
        first. Everyone standing in the area on the play view; in a story
        played off the grid, everyone the map places there."""
        world = world if world is not None else self.world()
        art = art if art is not None else self.map_art_copy()
        clock = self.map_clock()
        when = now_words(clock[1] if clock else None, world.weather)
        cards = {c.name: c for c in self.map_people()}
        names: list[str] = []
        things: list[str] = []
        area = self.area(area_id, world, art) if self.map_first else None
        if area is not None:
            state = self.area_state()
            held = self.held_name()
            me = state.spots.get(held or "")
            here = (me.x, me.y) if me is not None and me.area == area.id else None
            names = [name for name, spot in state.spots.items() if spot.area == area.id]
            placements = sorted(
                area.placements,
                key=lambda p: (
                    math.hypot(p.centre[0] - here[0], p.centre[1] - here[1]) if here else 0.0
                ),
            )
            things = [p.name for p in placements]
        else:
            inside = {area_id} | {p.id for p in world.places.values() if p.inside == area_id}
            names = [who for who, at in world.where.items() if at in inside]
            things = [t.name for t in world.things.values() if t.at in inside]
        looks: list[str] = []
        for name in names:
            card = cards.get(name)
            words = looks_of(card.summary, card.full_description) if card is not None else ""
            if words:
                looks.append(words)
        return when, looks, things

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
            self._choose_view(art, area_id, picture.state, picture.id)
            self._save_art(art)
        return True

    def choose_isometric(self, area_id: str, picture_id: str) -> bool:
        """Show this one of an area's isometric views over the map, for the
        map as it is there now (the author, Oct 15 2026: with half a dozen
        painted, swap between them). False for a picture that isn't one of
        the area's views, or in a private scene."""
        if not self.map_on or self.open_span is not None:
            return False
        world = self.world()
        if area_id not in world.places:
            return False
        art = self.world_art
        with self._lock():
            if not any(p.id == picture_id for p in views_of(art, area_id)):
                return False
            self._choose_view(art, area_id, isometric_state(world, area_id), picture_id)
            self._save_art(art)
        return True

    @staticmethod
    def _choose_view(art: WorldArt, area_id: str, state: str, picture_id: str) -> None:
        """Under the art lock."""
        art.chosen_views.setdefault(area_id, {})[state] = picture_id

    def start_map_pictures(self) -> bool:
        """Draw what the map needs from the image model, in the background."""
        with self._starting():
            return self._start_map_pictures()

    def _start_map_pictures(self) -> bool:
        if self._map_closed:
            return False
        thread = self._picture_thread
        if thread is not None and thread.is_alive():
            self._pictures_again = True
            return False
        self._pictures_again = False
        # Asking first (or more at once than auto draws unasked): only what
        # the author approved is drawn, the rest waits. All the map wants is
        # counted; a run draws PICTURE_RUN of it, within the day's room.
        jobs = self._held_for_approval(
            "map",
            self.map_picture_jobs(None),
            limit=PICTURE_RUN,
            room=self.map_pictures_left_today(),
        )
        media = self.config.images()
        if not jobs or media is None:
            return False
        self._picture_total, self._picture_done = len(jobs), 0
        client = ImageClient(media.base_url, media.api_key, api=media.api)
        self._picture_client = client
        self._art_stop = False
        info = self._picture_model()
        thread = BackgroundThread(
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
                self._doing(
                    "pictures",
                    f"{job_words(job)}, by {self.model_name(self.config.map_picture_model)}",
                )
                self._picture_one(job, client, sizes[job.subject == "postcard"])
                self._picture_done += 1
        finally:
            self._doing("pictures", None)
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
        extra: dict | None = None,
        finish: Callable[[bytes], bytes] | None = None,
    ) -> None:
        """One map picture: counted against the day's limit first, drawn,
        stripped, kept. `model` and `info` stand in for the map picture
        model's; `plan` is the picture of the map to paint from (the
        isometric view, a backdrop), sent before any reference; `finish`
        cuts a backdrop's padding off (and it is stripped again after)."""
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
        # The model's text or edit version, for whether a picture goes with it.
        model = fitting_model(model, len(references), self.image_listing())
        payload = build_image_payload(model, job.prompt, size, 1, references, api=client.api)
        if extra and client.api == "nanogpt":
            payload.update(extra)
        log_ref = self._log(
            "map_picture_request",
            {"job": job.key, "payload": loggable_payload(payload, names)},
        )
        try:
            reply = client.generate(payload)
            data = strip_metadata(reply.images[0])
            if finish is not None:
                data = strip_metadata(finish(data))
        except (ProviderError, MetadataError, IndexError, ValueError) as exc:
            if self._art_stop:
                return
            with self._lock():
                art.failures[job.key] = art.failures.get(job.key, 0) + 1
                self._save_art(art)
            self._log("map_picture_error", {"request_log_ref": log_ref, "error": str(exc)[:300]})
            return
        if info is None and model == self.config.map_picture_model:
            info = self._picture_model_info  # an unlisted isometric model stays unpriced
        cost = reply.cost
        if cost is not None:
            usage: dict = {"cost": cost, "cost_reported": True}
        elif info is not None and info.price(size) is not None:
            usage = {"cost": info.price(size), "cost_estimated": True}
        else:
            # Neither reported nor listed: unpriced, never a reported $0.00
            # (it was logged as cost 0.0 and counted so; the review, Oct 15 2026).
            usage = {"cost_reported": False}
        picture_id = new_id()
        relative = f"{MAP_PICTURES_DIR}/{picture_id}{picture_kind(data) or '.png'}"
        with self._closing:
            if self._art_stop or self._closed or not self.pictures.alive():
                # Closed meanwhile (another story is open, or this one deleted):
                # nothing written, or the story's folder came back as a ghost
                # and a reopened story's map art was written over by this
                # one's. Under the lock, so a close can't slip in before the
                # write (the folder's images/map/ came back by itself).
                return
            self._log("map_picture_response", {"request_log_ref": log_ref, "usage": usage})
            self.pictures.write(relative, data)
        # Into the session's own readout, as a story picture's cost goes.
        self.unreported_usage.append(
            Usage(
                cost=float(usage.get("cost", 0.0)),
                cost_estimated=bool(usage.get("cost_estimated")),
                cost_reported=bool(usage.get("cost_reported")),
            )
        )
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
            plan=job.plan,
            scene=job.scene,
        )
        with self._lock():
            art.pictures.append(picture)
            if job.subject == "isometric":
                # The author asked for it: the view over the map now.
                self._choose_view(art, job.subject_id, job.state, picture.id)
            self._save_art(art)
        callback = self.on_map_art
        if callback is not None:
            callback()

    def start_map_art(self) -> bool:
        """Draw what the map needs, in the background. False when there is
        nothing to draw, a run is under way, or the map can't be drawn now."""
        with self._starting():
            return self._start_map_art()

    def _start_map_art(self) -> bool:
        if self._map_closed:
            return False
        # Before anything is drawn in it: the set is kept from here on.
        self.pin_tile_set()
        # The region the author's character stands in is explored before
        # anything is planned (a read may have moved them into another): what
        # the designer put there may be drawn now, and nothing elsewhere.
        self.mark_explored()
        pictures = self._start_map_pictures()
        # Map-first play's furnishing and sprites (engine/session_area.py).
        pictures = self._start_tile_art() or pictures
        # The world designer's regions round the author's character
        # (engine/session_regions.py), on their own thread.
        pictures = self._start_region_design() or pictures
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
        thread = BackgroundThread(
            target=self._draw,
            args=(jobs, model, clients),
            name="map-art",
            daemon=True,
        )
        self._art_thread = thread
        thread.start()
        return True

    def redraw_problem(self, area_id: str) -> str | None:
        """Why a place's plan can't be drawn again now, or None."""
        if not self.map_on or self.open_span is not None:
            return "Not in a private scene."
        if not self.config.map_art:
            return "Draw the map is off (Settings → Models): nothing would draw it."
        if self.map_art_running:
            return "Wait for the map's drawing to finish."
        if self.world_art.layout_for(area_id) is None:
            return "It has no plan yet."
        return None

    def redraw_plan(self, area_id: str) -> bool:
        """Draw a place's plan again, when the author asks (Map → Redraw
        plan…, Oct 15 2026): plans are drawn once and kept, so one drawn
        badly (Deep Space 9's, before decks were asked for, was rooms
        scattered in space) stayed so. The plan it has stays until the new
        one is drawn, which then replaces every state's plan of the place
        (`adopt(redraw=True)`); one that can't be drawn leaves the old plan
        for good. Its views go out of date (`view_current`). The inside
        plans of the buildings in it stay, fitted over the boxes the new
        plan draws. False when it can't be now (`redraw_problem`)."""
        if self.redraw_problem(area_id) is not None:
            return False
        art = self.world_art
        with self._lock():
            if area_id not in art.redraw:
                art.redraw.append(area_id)
            art.failures.pop(f"layout:{area_id}:redraw", None)
            self._save_art(art)
        self.start_map_art()
        return True

    def stop_map_art(self, wait_seconds: float = 0.0) -> None:
        """Stop drawing, for good (the story is being left): what is out is
        cancelled, what came back is kept, and nothing starts again."""
        self._map_closed = True
        self._art_stop = True
        for client in self._art_clients or []:
            try:
                client.cancel()
            except Exception:  # noqa: BLE001 - stopping must never fail
                pass
        tile_clients = [
            *(getattr(self, "_region_clients", None) or []),
            getattr(self, "_tile_client", None),
            getattr(self, "_tile_text", None),
            getattr(self, "_own_set_client", None),
            getattr(self, "_asset_client", None),
        ]
        backdrop_clients = [
            getattr(self, "_backdrop_client", None),
            getattr(self, "_backdrop_text", None),
        ]
        for client in (
            self._picture_client,
            self._isometric_client,
            *tile_clients,
            *backdrop_clients,
        ):
            if client is not None:
                try:
                    client.cancel()
                except Exception:  # noqa: BLE001 - stopping must never fail
                    pass
        threads = (
            self._art_thread,
            self._picture_thread,
            self._isometric_thread,
            getattr(self, "_tile_thread", None),
            getattr(self, "_region_thread", None),
            getattr(self, "_own_set_thread", None),
            getattr(self, "_asset_thread", None),
            getattr(self, "_backdrop_thread", None),
        )
        for thread in threads:
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
        helpers = [BackgroundThread(target=worker, args=(c,), daemon=True) for c in clients[1:]]
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
        if self._map_closed:
            return  # the story was left: a session opened since owns its art
        art = self.world_art
        with self._lock():
            adopt(art, drawn, redraw=job.key.endswith(":redraw"))
            self._save_art(art)
        callback = self.on_map_art
        if callback is not None:
            callback()

    def _save_art(self, art: WorldArt) -> None:
        # Every change to the art comes through here: the settled area state
        # (`AreaRuntime.area_state`) is kept until it does.
        self._art_version += 1
        # Never into a story deleted meanwhile: the write made its folder
        # again, an unreadable ghost in the list. Nor once the story is
        # closed (`StorySession.close`), under its lock: a painting still out
        # wrote the day's count into a folder being removed.
        with self._closing:
            if not self._closed and not self.memory_only and self.pictures.alive():
                save_world_art(self.story.id, art, self.root)


def _today() -> str:
    """The UTC day, as the daily limit counts it."""
    return datetime.now(UTC).date().isoformat()
