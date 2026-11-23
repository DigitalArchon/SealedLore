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
  the parts' (under SillyTavern's rules nothing else embeds the entries);
- how: the texts are read by the story's one writer (the worker after a
  passage, the window when nothing runs), the requests go out one at a time
  on a thread with the embeddings backend's `detached()` copy, and the
  vectors are taken in by the writer at the start of the next turn or by the
  window's poll, each only while some entry or part still has its text. A
  backend without `detached` (the tests' mock) embeds then and there.

A turn that comes before they are in ranks by words alone and the Lore page
says so (`lore_vectors_note`): it never waits for them. A failure is a notice,
never a turn; Stop and closing the story end the thread once the request out
has come back. Embedding isn't in the API log, so nothing is logged."""

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
    # What came back, by content hash.
    vectors: dict[str, tuple[float, ...]] = field(default_factory=dict)
    error: str | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)


class LoreVectorsRuntime:
    _vectors_job: _VectorJob | None = None
    # Why the last job couldn't make them, until one does.
    _vectors_failed: str | None = None

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
        texts: dict[str, str] = {}
        for item_id, text in plan.texts_of.items():
            if not plan.has(item_id):
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
        if plan.model == job.model:
            holders: dict[str, list[str]] = {}
            for item_id, digest in plan.hashes.items():
                holders.setdefault(digest, []).append(item_id)
            added = False
            for digest, vector in job.vectors.items():
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
            if added:
                self.save()
        if job.error is not None:
            self._vectors_failed = job.error
            return [
                f"The lore's vectors couldn't be made ({job.error}): lore from the full "
                "entries is ranked by words until they are. They are tried again after the "
                "next passage."
            ]
        if not job.stop.is_set():
            self._vectors_failed = None
        return []

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


def _embed(job: _VectorJob, backend: Any) -> None:
    """The job's requests, one at a time, until done or stopped."""
    try:
        for batch in job.batches:
            if job.stop.is_set():
                return
            texts: Sequence[str] = [text for _, text in batch]
            result = backend.embed(texts)
            if len(result.vectors) != len(batch):
                raise ProviderError(f"asked for {len(batch)} embeddings, got {len(result.vectors)}")
            for (digest, _), vector in zip(batch, result.vectors, strict=True):
                job.vectors[digest] = tuple(vector)
    except Exception as exc:  # noqa: BLE001 - a notice when taken in, never a turn
        job.error = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    finally:
        job.done.set()
