"""Recall on the session's side: embedding the candidates and choosing. A mixin for `StorySession`.

The pure part (what can be recalled, choosing, rendering) is
`engine/recall.py`. Here: the vectors, cached beside lore's in
`bundle.embeddings` by content hash and model (`kind="recall"`, so pruning
either never drops the other's), the query vector shared with lore's
retrieval, and the report the UI shows. Nothing is recalled in a simple chat
or a private scene (no network there), and a missing or failing embeddings
endpoint is a reason on the report, never a failed turn.
"""

from __future__ import annotations

from collections.abc import Sequence

from sealedlore.engine.recall import (
    RecallItem,
    RecallReport,
    recall_embedding_text,
    recall_items,
    select_recall,
)
from sealedlore.engine.retrieval import content_hash, cosine_scores
from sealedlore.models.embedding import EmbeddingCacheEntry
from sealedlore.models.node import Node
from sealedlore.providers.base import ProviderError

RECALL_KIND = "recall"
# What one embeddings request may hold, by estimate (see `embed_in_batches`).
EMBED_BATCH_TOKENS = 5_000


class RecallRuntime:
    last_recall: RecallReport | None = None
    _query_vectors: dict[tuple[str, str], tuple[float, ...]] | None = None

    def query_vector(self, query: str) -> tuple[float, ...] | None:
        """The query's vector, embedded once however many selections use it.
        Raises ProviderError."""
        settings = self.config.embeddings()
        model = settings.model if settings else ""
        memo = self._query_vectors if self._query_vectors is not None else {}
        self._query_vectors = memo
        key = (query, model)
        if key not in memo:
            result = self.embeddings.embed([query], as_query=True)
            if not result.vectors:
                return None
            if len(memo) > 8:
                memo.clear()
            memo[key] = tuple(result.vectors[0])
        return memo[key]

    def recall_for(self, query: str, history: Sequence[Node]) -> RecallReport:
        """What to recall for this query on this path (`Config.recall`)."""
        mode = self.config.recall
        if mode == "off" or self.story.chat:
            return RecallReport()
        if self.in_private:
            return RecallReport(reason="not in a private scene")
        items = recall_items(
            self.split(history),
            self.bundle.summaries,
            self.visible_cast(),
            mode=mode,
            texts=self.texts,
        )
        if not items:
            return RecallReport()
        if self.embeddings is None:
            return RecallReport(reason="no embeddings endpoint")
        if not query.strip():
            return RecallReport()
        try:
            vectors = self._recall_vectors(items)
            target = self.query_vector(query)
        except ProviderError as exc:
            return RecallReport(reason=str(exc))
        if target is None:
            return RecallReport(reason="endpoint returned no vector")
        scored = [item for item in items if item.id in vectors]
        try:
            similarities = cosine_scores(target, [vectors[item.id] for item in scored])
        except ValueError as exc:
            return RecallReport(reason=str(exc))
        report = select_recall(
            items,
            {item.id: score for item, score in zip(scored, similarities, strict=True)},
            k=self.config.recall_k,
            threshold=self.config.recall_threshold,
            token_cap=self.config.recall_token_cap,
            count_tokens=self._count_lore_tokens,
        )
        return report

    def embed_in_batches(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """Embed `texts` in requests the endpoint will take. Raises ProviderError.

        NanoGPT counts a whole request against bge-m3's 8k context: 32 archived
        exchanges in one were refused ("Input is too large", HTTP 400), 16 went
        through. So a request holds at most `EMBED_BATCH_TOKENS` by estimate."""
        vectors: list[tuple[float, ...]] = []
        batch: list[str] = []
        size = 0
        for text in texts:
            cost = self.estimator.estimate(text, None)
            if batch and size + cost > EMBED_BATCH_TOKENS:
                vectors.extend(self.embeddings.embed(batch).vectors)
                batch, size = [], 0
            batch.append(text)
            size += cost
        if batch:
            vectors.extend(self.embeddings.embed(batch).vectors)
        return vectors

    def _recall_vectors(self, items: Sequence[RecallItem]) -> dict[str, tuple[float, ...]]:
        """Embed what isn't cached yet; return item id -> vector."""
        settings = self.config.embeddings()
        model = settings.model if settings else ""
        wanted = settings.dimensions if settings else 0
        cache = {
            (row.content_hash, row.model): row
            for row in self.bundle.embeddings
            if not wanted or row.dimensions == wanted
        }
        hashes = {item.id: content_hash(recall_embedding_text(item)) for item in items}
        pending = [item for item in items if (hashes[item.id], model) not in cache]
        if pending:
            vectors = self.embed_in_batches([recall_embedding_text(i) for i in pending])
            for item, vector in zip(pending, vectors, strict=True):
                row = EmbeddingCacheEntry(
                    content_hash=hashes[item.id],
                    model=model,
                    dimensions=len(vector),
                    vector=list(vector),
                    kind=RECALL_KIND,
                )
                self.bundle.embeddings.append(row)
                cache[(row.content_hash, row.model)] = row
            # Text no candidate holds any more (a rebuilt chapter, a branch
            # left) goes, so the file can shrink; lore's rows are its own.
            live = set(hashes.values())
            self.bundle.embeddings = [
                row
                for row in self.bundle.embeddings
                if row.kind != RECALL_KIND or row.content_hash in live
            ]
            self.save()
        return {
            item_id: tuple(cache[(digest, model)].vector)
            for item_id, digest in hashes.items()
            if (digest, model) in cache
        }

    def _recall_for_turn(
        self, query: str, history: Sequence[Node], *, question: bool = False
    ) -> tuple[RecallItem, ...]:
        """Recall for a turn or question about to be sent; keep the report for the UI.
        A story turn recalls only with `Config.recall_in` "everywhere"."""
        if not question and self.config.recall_in != "everywhere":
            return ()
        report = self.recall_for(query, history)
        self.last_recall = report
        return report.items
