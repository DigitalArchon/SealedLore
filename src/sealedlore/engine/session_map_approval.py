"""Map pictures the author approves before they are drawn (Settings →
Images → Map pictures: "Ask before drawing"; the author, Oct 17 2026: map
pictures on by default, with a choice between approving each drawing and
having it fully automatic).

In "ask" mode a run that would have sent pictures to the image model sends
none: what it would have drawn waits (`map_pictures_waiting`), each with
its model, size and listed price, until the author approves it on the Map
page, as it stands or with prompts edited and some left out (what they
approve is what is sent), or says not now. Pictures declined aren't asked
about again while the story is open. Approved ones are drawn by the next
run, each once: an approval is for one drawing, so one that fails is asked
about again.

Avatars, roofs and the things of the play view are the tile run's
(engine/session_area.py); places, things on the map and postcards the
picture run's (engine/session_world_art.py). Each run calls
`_held_for_approval` with what it would draw.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from sealedlore.engine.tile_draw import SPRITE_SIZE, TEXTURE_SIZE
from sealedlore.engine.world_pictures import PictureJob, picture_size

Run = Literal["map", "tile"]
RUNS: tuple[Run, ...] = ("map", "tile")


@dataclass(frozen=True)
class WaitingPicture:
    """A picture the map wants, waiting for the author: the job as the run
    would send it, by which model, at which size, at what listed price
    (None: the listing gives none)."""

    job: PictureJob
    run: Run
    model: str
    size: str
    price: float | None


class MapApprovalRuntime:
    _approval_lock: threading.Lock | None = None
    # Under `_approval()`: what each run would draw and the author hasn't
    # answered; what they approved (by job key, with the run it is for); what
    # they declined (job keys, for as long as the story is open).
    _pictures_waiting: dict[Run, list[WaitingPicture]] | None = None
    _pictures_approved: dict[str, WaitingPicture] | None = None
    _pictures_declined: set[str] | None = None

    def _approval(self) -> threading.Lock:
        if self._approval_lock is None:
            self._approval_lock = threading.Lock()
            self._pictures_waiting = {"map": [], "tile": []}
            self._pictures_approved = {}
            self._pictures_declined = set()
        return self._approval_lock

    @property
    def map_pictures_asking(self) -> bool:
        """Map pictures are on, and each drawing waits for the author."""
        return self.config.map_pictures_mode == "ask"

    def _held_for_approval(self, run: Run, jobs: list[PictureJob]) -> list[PictureJob]:
        """What a run may draw now. In "ask" mode: the jobs among `jobs` the
        author approved, as they approved them (each approval used up), the
        rest left waiting; an approval for a picture this run no longer
        wants is dropped. Otherwise `jobs` as they are."""
        if not self.map_pictures_asking:
            return jobs
        priced = [self._waiting_picture(run, job) for job in jobs]  # outside the lock
        wanted = {job.key for job in jobs}
        with self._approval():
            approved = self._pictures_approved
            for key in [k for k, w in approved.items() if w.run == run and k not in wanted]:
                del approved[key]
            now = [approved.pop(job.key).job for job in jobs if job.key in approved]
            drawing = {job.key for job in now}
            self._pictures_waiting[run] = [
                w
                for w in priced
                if w.job.key not in drawing and w.job.key not in self._pictures_declined
            ]
        return now

    def _waiting_picture(self, run: Run, job: PictureJob) -> WaitingPicture:
        """A job with the model, size and price it would be drawn at: as
        `_tile_run` and `_picture_all` send it."""
        if run == "tile":
            model = self.config.map_picture_model
            size = TEXTURE_SIZE if job.subject == "texture" else SPRITE_SIZE
            info = self._listed_image_model(model)
        else:
            model = self.config.map_picture_model
            info = self._picture_model()
            size = picture_size(info, landscape=job.subject == "postcard")
        price = info.price(size) if info is not None else None
        return WaitingPicture(job=job, run=run, model=model, size=size, price=price)

    def map_pictures_waiting(self) -> list[WaitingPicture]:
        """What the map wants drawn and waits for the author to approve,
        the picture run's first; nothing unless map pictures ask first."""
        if not self.map_pictures_asking or not self.map_pictures_on:
            return []
        with self._approval():
            return [w for run in RUNS for w in self._pictures_waiting[run]]

    @property
    def map_pictures_approved_pending(self) -> bool:
        """Approved pictures still to be drawn (a run was busy when they
        were approved): the window starts the next run once it is free."""
        with self._approval():
            return bool(self._pictures_approved)

    def approve_map_pictures(self, jobs: Sequence[PictureJob], *, start: bool = True) -> bool:
        """Draw these, as the author approved them (prompts and all), in
        the background; the rest of what waited stays waiting. True when a
        run started now (else the next run takes them; `start` False: the
        caller can't start one now)."""
        if not jobs:
            return False
        with self._approval():
            runs = {w.job.key: w.run for run in RUNS for w in self._pictures_waiting[run]}
            for job in jobs:
                run = runs.get(job.key)
                if run is None:
                    continue  # no longer waiting (answered elsewhere, or drawn)
                self._pictures_approved[job.key] = self._repriced(job, run)
            approved = self._pictures_approved
            for run in RUNS:
                self._pictures_waiting[run] = [
                    w for w in self._pictures_waiting[run] if w.job.key not in approved
                ]
        return self.start_map_art() if start else False

    def _repriced(self, job: PictureJob, run: Run) -> WaitingPicture:
        # The job as approved (its prompt perhaps edited); the price as before.
        found = next(w for w in self._pictures_waiting[run] if w.job.key == job.key)
        return WaitingPicture(
            job=job, run=run, model=found.model, size=found.size, price=found.price
        )

    def decline_map_pictures(self, keys: Iterable[str]) -> None:
        """Not these: not asked about again while the story is open."""
        keys = set(keys)
        with self._approval():
            self._pictures_declined |= keys
            for run in RUNS:
                self._pictures_waiting[run] = [
                    w for w in self._pictures_waiting[run] if w.job.key not in keys
                ]
