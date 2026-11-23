"""The vectors the turn's ranking of lore from the full entry reads, made
ahead of the turns. A mixin for `StorySession`.

Every storyteller but Claude is given, with a turn, the parts of a condensed
always-on entry's full text that the turn is about (engine/lore_whole_back.py,
engine/model_tuning.py; the author, Nov 23 2026). They are ranked by
similarity, and each part's vector used to be made inside the turn that first
wanted it: under SillyTavern's rules nothing else is embedded before, and a
big lorebook's parts held the first turn up to a minute with the built-in
model. Now they are made ahead:

- when: once condensed versions are written or the always-on entries change
  (the window, a moment after), when a story opens, and after a passage (the
  slot the chapters and the cards are prepared in);
- only while the parts would be sent: an embeddings backend, a storyteller
  they go to, something sent condensed; never in a chat, and never in a
  private scene, where nothing else is called (entering one stops a job out);
- what: every vector the turn's ranking reads and lacks, the entries' with
  the parts' (under SillyTavern's rules nothing else embeds the entries):
  the entries chosen per turn, the always-on ones sent whole, and the parts
  of those sent condensed, never their whole texts, which nothing ranks
  (`_lore_vector_plan`);
- how: the texts are read by the story's one writer (the worker after a
  passage, the window when nothing runs), the requests go out one at a time
  on a thread with the embeddings backend's `detached()` copy, and the
  vectors are taken in by the writer at the start of the next turn or by the
  window's poll, each only while some entry or part still has its text. A
  backend without `detached` (the tests' mock) embeds then and there.

A turn that comes before they are in ranks by words alone and the Lore page
says so (`lore_vectors_note`): it never waits for them, and never makes them
itself while a job is out (`take_lore_vectors_made`). A failure is a notice,
never a turn. A text the endpoint refuses (HTTP 400, 413 or 422: past its
context) is left out and the rest made; it isn't sent again until its text or
the embeddings model changes (in memory, for the session), and the Lore page
says which couldn't be made, those ranking by words alone. Closing the story
ends the thread once the request out has come back; Stop on a turn leaves it
running, and the window sets one going once a stopped job ends, when the
passage it stopped never got to. Embedding isn't in the API log, so nothing
is logged."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sealedlore.engine.session_merge import _close
from sealedlore.models.embedding import EmbeddingCacheEntry
from sealedlore.models.lore import LoreEntry
from sealedlore.providers.base import ProviderError
from sealedlore.threads import BackgroundThread

# What the Lore page says when lore from the full entry was ranked by words
# for want of vectors (`lore_vectors_note`).
PARTS_BEING_MADE = "ranked by words: the parts' vectors are being made"
LORE_BEING_MADE = "ranked by words: the lore's vectors are being made"
# Why a turn chose its lore by keywords alone while a job out is making the
# entries' vectors (`RetrievalReport.fallback_reason`).
LORE_VECTORS_COMING = "the lore's vectors are being made"
# The endpoint's answers that refuse the texts asked for, not every request
# (past its context, a body too large): those texts aren't asked for again.
REFUSED_STATUSES = frozenset({400, 413, 422})


@dataclass
class VectorPlan:
    """What the lore's vectors are made of now (`_lore_vector_plan`): each
    item's text and its hash, by id (entries and parts), the entries
    themselves, which ids are parts, and the vectors kept for `model`."""

    model: str
    cache: dict[tuple[str, str], EmbeddingCacheEntry]
    texts_of: dict[str, str]
    entries: dict[str, LoreEntry]
    part_ids: set[str]
    hashes: dict[str, str]

    def has(self, item_id: str) -> bool:
        return (self.hashes[item_id], self.model) in self.cache


@dataclass
class _VectorJob:
    model: str
    # The texts to embed, by content hash, in the requests they go in.
    batches: list[list[tuple[str, str]]]
    # What came back, by content hash: written by the job's thread under
    # `lock`, read by the story's writer under it (`take_lore_vectors_made`).
    vectors: dict[str, tuple[float, ...]] = field(default_factory=dict)
    # The texts the endpoint refused, by content hash: why, in its words.
    refused: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def hashes(self) -> set[str]:
        """Every text it was set going with, by content hash."""
        return {digest for batch in self.batches for digest, _text in batch}


class LoreVectorsRuntime:
    _vectors_job: _VectorJob | None = None
    # Why the last job couldn't make them, until one does.
    _vectors_failed: str | None = None
    # The texts the endpoint refused, by (content hash, model): why. Not
    # asked for again until the text or the model changes; this session only.
    _vectors_refused: dict[tuple[str, str], str] | None = None

    def _refused_vectors(self) -> dict[tuple[str, str], str]:
        if self._vectors_refused is None:
            self._vectors_refused = {}
        return self._vectors_refused

    def refused_lore_ids(self, plan: VectorPlan) -> dict[str, str]:
        """The items of `plan` whose text the endpoint refused, for its
        model: why, by id."""
        refused = self._vectors_refused
        if not refused:
            return {}
        return {
            item_id: refused[(digest, plan.model)]
            for item_id, digest in plan.hashes.items()
            if (digest, plan.model) in refused
        }

    def refused_note(self, refused: dict[str, str], titles: dict[str, str]) -> str:
        """The Lore page's word for the items whose vectors the endpoint
        refused (`refused`, id → why), named by `titles` (id → its entry's
        title): those are ranked by words alone."""
        names: dict[str, int] = {}
        for item_id in refused:
            title = titles.get(item_id, "")
            names[title] = names.get(title, 0) + 1
        listed = ", ".join(
            f"“{title}”" + (f" ({count} parts)" if count > 1 else "")
            for title, count in names.items()
        )
        why = next(iter(refused.values()))
        return f"the vectors of {listed} couldn't be made ({why}): those rank by words alone"

    def lore_vectors_wanted(self) -> bool:
        """Whether vectors are wanted for lore from the full entry: an
        embeddings backend, parts going with the turns (`lore_from_full_on`:
        not in a chat, not for a Claude storyteller), something sent
        condensed, and no private scene open."""
        if self.embeddings is None or self.in_private or not self.lore_from_full_on():
            return False
        return bool(self.lore_layout().condensed)

    def lore_vectors_out(self) -> bool:
        return self._vectors_job is not None

    def lore_vectors_ready(self) -> bool:
        job = self._vectors_job
        return job is not None and job.done.is_set()

    def lore_vectors_note(self, *, parts: bool) -> str:
        """The Lore page's word when the turn ranked by words because
        vectors were missing (`parts`: some part's)."""
        whose = "the parts' vectors" if parts else "the lore's vectors"
        if self._vectors_failed:
            return f"ranked by words: {whose} couldn't be made ({self._vectors_failed})"
        return PARTS_BEING_MADE if parts else LORE_BEING_MADE

    def start_lore_vectors(self) -> bool:
        """Have the vectors the turn's ranking lacks made, in the background
        where the embeddings backend has `detached`. False when there was
        nothing to do or a job is still out."""
        if self._vectors_job is not None or not self.lore_vectors_wanted():
            return False
        plan = self._lore_vector_plan()
        refused = self.refused_lore_ids(plan)
        texts: dict[str, str] = {}
        for item_id, text in plan.texts_of.items():
            if not plan.has(item_id) and item_id not in refused:
                texts.setdefault(plan.hashes[item_id], text)
        if not texts:
            return False
        hashes = list(texts)
        batches: list[list[tuple[str, str]]] = []
        start = 0
        for batch in self.embedding_batches([texts[digest] for digest in hashes]):
            batches.append(list(zip(hashes[start : start + len(batch)], batch, strict=True)))
            start += len(batch)
        job = _VectorJob(model=plan.model, batches=batches)
        backend = self.embeddings
        detached = getattr(backend, "detached", None)
        client = detached() if callable(detached) else None
        self._vectors_job = job
        if client is None:
            _embed(job, backend)
            return True

        def background() -> None:
            try:
                _embed(job, client)
            finally:
                if client is not backend:
                    _close(client)

        BackgroundThread(target=background, name="lore-vectors", daemon=True).start()
        return True

    def adopt_lore_vectors(self) -> list[str]:
        """Take finished vectors in, each while some entry or part still has
        the text it was made of; the notices to show. The worker calls it at
        the start of a turn, the window when nothing runs: never both at
        once. A private scene doesn't hold it back: nothing is called."""
        job = self._vectors_job
        if job is None or not job.done.is_set():
            return []
        self._vectors_job = None
        plan = self._lore_vector_plan()
        if self._keep_job_vectors(job, plan):
            self.save()
        notices = []
        refused = {
            (digest, job.model): why
            for digest, why in job.refused.items()
            if (digest, job.model) not in self._refused_vectors()
        }
        if refused:
            self._refused_vectors().update(refused)
            ids = {
                item_id: why
                for item_id, digest in plan.hashes.items()
                if (why := refused.get((digest, job.model))) is not None
            }
            if ids and plan.model == job.model:
                titles = self._lore_titles(set(ids))
                notices.append(
                    "Some of the lore's vectors couldn't be made: "
                    + self.refused_note(ids, titles)
                    + ". They are asked for again once their text or the embeddings model "
                    "changes."
                )
        if job.error is not None:
            self._vectors_failed = job.error
            return [
                *notices,
                f"The lore's vectors couldn't be made ({job.error}): lore from the full "
                "entries is ranked by words until they are. They are tried again after the "
                "next passage.",
            ]
        if not job.stop.is_set():
            self._vectors_failed = None
        return notices

    def _keep_job_vectors(self, job: _VectorJob, plan: VectorPlan) -> bool:
        """What `job` has made so far kept with the story, each while some
        entry or part still has its text and none is kept already; whether
        any was."""
        if plan.model != job.model:
            return False
        with job.lock:
            made = dict(job.vectors)
        holders: dict[str, list[str]] = {}
        for item_id, digest in plan.hashes.items():
            holders.setdefault(digest, []).append(item_id)
        added = False
        for digest, vector in made.items():
            if digest not in holders or (digest, plan.model) in plan.cache:
                continue
            row = EmbeddingCacheEntry(
                content_hash=digest,
                model=plan.model,
                dimensions=len(vector),
                vector=list(vector),
            )
            self.bundle.embeddings.append(row)
            plan.cache[(digest, plan.model)] = row
            for item_id in holders[digest]:
                if item_id in plan.entries:
                    plan.entries[item_id].embedding_hash = digest
            added = True
        return added

    def take_lore_vectors_made(self) -> tuple[set[str], set[str]]:
        """For a turn's own ranking (or the lore pick), while a job is out:
        what it has made so far kept, the job left running. (The content
        hashes it was set going with and hasn't made, which the turn leaves
        to it; the ids of the entries and parts it is still making.) Nothing,
        with no job out. The turn never makes what the job is making, and
        never waits for it: what failed is said when the job is taken in."""
        job = self._vectors_job
        if job is None:
            return set(), set()
        plan = self._lore_vector_plan()
        if self._keep_job_vectors(job, plan):
            self.save()
        with job.lock:
            left = job.hashes - set(job.vectors)
            coming = set() if job.done.is_set() else left - set(job.refused)
        making = {item_id for item_id, digest in plan.hashes.items() if digest in coming}
        return left, making

    def _lore_titles(self, ids: set[str]) -> dict[str, str]:
        """Each id's entry title (a part's, its entry's)."""
        by_id = {entry.id: entry.title for entry in self.bundle.lore}
        return {item_id: by_id.get(item_id.split("#", 1)[0], "") for item_id in ids}

    def stop_lore_vectors(self) -> None:
        """End a job out once its request has come back: what it made by
        then is still taken in."""
        job = self._vectors_job
        if job is not None:
            job.stop.set()

    def _drop_lore_vectors(self, wait_seconds: float) -> None:
        """The story is being left: a job out is stopped and given
        `wait_seconds`, and what it had made by then is kept."""
        job = self._vectors_job
        if job is None:
            return
        job.stop.set()
        job.done.wait(wait_seconds)
        if job.done.is_set():
            self.adopt_lore_vectors()
        else:
            self._vectors_job = None


def _words(exc: Exception) -> str:
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


def _refusal(exc: Exception) -> bool:
    """Whether the endpoint refused the texts asked for (one past its
    context), rather than every request: unreachable, overloaded, a key."""
    return isinstance(exc, ProviderError) and exc.status_code in REFUSED_STATUSES


def _embed(job: _VectorJob, backend: Any) -> None:
    """The job's requests, one at a time, until done or stopped. A request
    the endpoint refuses is asked for a text at a time, so only the text it
    refuses is left out (`job.refused`) and the rest made; one whose texts
    weren't the cause (unreachable, overloaded, a key) ends the job, as every
    request after it would fail alike."""
    try:
        for batch in job.batches:
            if job.stop.is_set():
                return
            if not _embed_batch(job, backend, batch):
                return
    finally:
        job.done.set()


def _embed_batch(job: _VectorJob, backend: Any, batch: list[tuple[str, str]]) -> bool:
    """One request's vectors into the job; False once the job must end."""
    try:
        texts: Sequence[str] = [text for _, text in batch]
        result = backend.embed(texts)
        if len(result.vectors) != len(batch):
            raise ProviderError(f"asked for {len(batch)} embeddings, got {len(result.vectors)}")
    except Exception as exc:  # noqa: BLE001 - a notice when taken in, never a turn
        if not _refusal(exc):
            job.error = _words(exc)
            return False
        if len(batch) == 1:
            with job.lock:
                job.refused[batch[0][0]] = _words(exc)
            return True
        for one in batch:
            if job.stop.is_set() or not _embed_batch(job, backend, [one]):
                return False
        digests = [digest for digest, _ in batch]
        with job.lock:
            if all(digest in job.refused for digest in digests):
                # Every one refused alone too: the request, not a text (a
                # model the endpoint won't embed with), so none is kept from
                # being asked for again, and the job ends as for any failure.
                for digest in digests:
                    job.refused.pop(digest, None)
                job.error = _words(exc)
                return False
        return True
    with job.lock:
        for (digest, _), vector in zip(batch, result.vectors, strict=True):
            job.vectors[digest] = tuple(vector)
    return True
