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

In "auto" mode pictures are drawn without asking, unless more than
`Config.map_pictures_ask_over` would be drawn at once (the author, Oct 19
2026: an old story opened after map pictures went automatic drew 33 in two
minutes, its catch-up all at once). "At once" counts what this run wants
and what the other run is still drawing unasked; each run sees everything
it wants before its own cap (6 or 12 a run), so a burst split over runs
still counts whole. Those then wait as in "ask", and new ones join them
while any of them wait; what auto let through once isn't counted again.

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
    # In "auto": what each run is drawing without having asked (by job key,
    # until drawn or the run ends), and what auto let through once (not
    # counted against the limit again, for as long as the story is open).
    _pictures_unasked: dict[Run, set[str]] | None = None
    _pictures_let_through: set[str] | None = None
    # In "auto": what waited because there were too many at once (Ask's own
    # waiting isn't held against auto once the author switches to it).
    _pictures_held_over: set[str] | None = None

    def _approval(self) -> threading.Lock:
        if self._approval_lock is None:
            self._approval_lock = threading.Lock()
            self._pictures_waiting = {"map": [], "tile": []}
            self._pictures_approved = {}
            self._pictures_declined = set()
            self._pictures_unasked = {"map": set(), "tile": set()}
            self._pictures_let_through = set()
            self._pictures_held_over = set()
        return self._approval_lock

    @property
    def map_pictures_asking(self) -> bool:
        """Map pictures are on, and each drawing waits for the author."""
        return self.config.map_pictures_mode == "ask"

    @property
    def map_pictures_ask_over(self) -> int:
        """In "auto": how many pictures may be drawn at once before they
        wait for the author; 0 when nothing would ("ask", "off", or auto
        set never to ask)."""
        if self.config.map_pictures_mode != "auto":
            return 0
        return max(0, self.config.map_pictures_ask_over)

    def _held_for_approval(
        self, run: Run, jobs: list[PictureJob], *, limit: int | None = None
    ) -> list[PictureJob]:
        """What a run may draw now, at most `limit`, from `jobs`: everything
        it wants drawn, before its own cap. The approved first, as the author
        approved them (each approval used up when drawn); an approval for a
        picture the run no longer wants is dropped. In "ask" mode the rest
        wait. In "auto" the rest are drawn, unless they come to more than
        `map_pictures_ask_over` at once: then what auto hasn't let through
        before waits as in "ask". Never what the author declined."""
        mode = self.config.map_pictures_mode
        over = self.map_pictures_ask_over
        with self._approval():
            approved, declined = self._pictures_approved, self._pictures_declined
            let_through = self._pictures_let_through
            wanted = {job.key for job in jobs}
            for key in [k for k, w in approved.items() if w.run == run and k not in wanted]:
                del approved[key]
            ready = [approved[job.key].job for job in jobs if job.key in approved]
            rest = [job for job in jobs if job.key not in approved and job.key not in declined]
            go, wait = rest, []
            if mode == "ask":
                go, wait = [], rest
            elif over:
                fresh = [job for job in rest if job.key not in let_through]
                held = self._pictures_held_over
                elsewhere = sum(
                    len(keys) for other, keys in self._pictures_unasked.items() if other != run
                )
                if fresh and (
                    any(job.key in held for job in fresh) or len(fresh) + elsewhere > over
                ):
                    go, wait = [job for job in rest if job.key in let_through], fresh
                    held.update(job.key for job in fresh)
                else:
                    let_through.update(job.key for job in rest)
            draw = (ready + go)[:limit]
            for job in draw:
                approved.pop(job.key, None)
            self._pictures_unasked[run] = {job.key for job in go}
            if not wait:
                self._pictures_waiting[run] = []
        if wait:
            priced = [self._waiting_picture(run, job) for job in wait]  # outside the lock
            with self._approval():
                self._pictures_waiting[run] = [
                    w
                    for w in priced
                    if w.job.key not in self._pictures_approved
                    and w.job.key not in self._pictures_declined
                ]
        return draw

    def _picture_drawn(self, run: Run, key: str) -> None:
        """A run has drawn (or given up on) one of its pictures."""
        with self._approval():
            self._pictures_unasked[run].discard(key)

    def _pictures_run_ended(self, run: Run) -> None:
        """A run is over: nothing of it is being drawn any more."""
        with self._approval():
            self._pictures_unasked[run] = set()

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
        the picture run's first: every picture when map pictures ask first,
        and in "auto" more at once than it draws without asking."""
        mode = self.config.map_pictures_mode
        if mode == "off" or not self.map_pictures_on:
            return []
        with self._approval():
            waiting = [w for run in RUNS for w in self._pictures_waiting[run]]
            if mode == "auto":
                # Only what the limit held back: what waited under Ask goes
                # with the next run now.
                waiting = [w for w in waiting if w.job.key in self._pictures_held_over]
            return waiting

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
