"""The story's map art, held once for the session (`models/world.WorldArt`:
the plans, sprites, pictures and tile set a visual story's predesigned map
comes with, loaded from the story's folder): one copy under a lock, read
through a shared snapshot per version and written through `_save_art`.
Split out of the map-drawing runtime when that was archived (Oct 24 2026);
nothing here asks a model for anything."""

from __future__ import annotations

import threading

from sealedlore.engine.area import Area
from sealedlore.engine.backdrops import current_backdrop
from sealedlore.engine.world_art import cleaned_art
from sealedlore.models.world import MapPicture, WorldArt
from sealedlore.storage.world_art import load_world_art, save_world_art


class ArtRuntime:
    _art_version: int = 0
    _art_kept: tuple[tuple, WorldArt] | None = None
    _art: WorldArt | None = None
    _art_lock: threading.Lock | None = None

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

    def _art_read(self) -> WorldArt:
        """The art as it stands, for reading only: one copy per version of
        it (`_save_art` bumps it), shared by every read until the art
        changes. Every change is made on `world_art` under the lock, never
        on a copy; a copy of a whole sample's art cost 40 ms, and opening a
        story took 219 of them (Oct 19 2026). Writers take `map_art_copy`."""
        art = self.world_art
        # The version, and the shape of the art beside it: a layout, patch,
        # picture or set added or replaced is seen even when nobody saved
        # (tests poke the art directly; the app saves every change).
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

    def _save_art(self, art: WorldArt) -> None:
        # Every change to the art comes through here: the settled area state
        # (`AreaRuntime.area_state`) is kept until it does.
        self._art_version += 1
        # Never into a story deleted meanwhile, nor once the story is closed
        # (`StorySession.close`), under its lock.
        with self._closing:
            if not self._closed and not self.memory_only and self.pictures.alive():
                save_world_art(self.story.id, art, self.root)

    def backdrop_of(self, area: Area) -> MapPicture | None:
        """The painting the play view lays its sprites over for this area
        (the sample's backdrop of the area as it stands), or None."""
        return current_backdrop(self.world(), self._art_read(), area)
