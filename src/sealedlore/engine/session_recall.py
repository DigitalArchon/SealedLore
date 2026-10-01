"""Recall on the session's side: embedding the candidates and choosing. A mixin for `StorySession`.

The pure part (what can be recalled, ranking, choosing, rendering) is
`engine/recall.py`. Here: the vectors, cached beside lore's in
`bundle.embeddings` by content hash and model (`kind="recall"`, so pruning
either never drops the other's), the query vector shared with lore's
retrieval, what a recent Question carries into the turns after it, and the
report the UI shows. Nothing is recalled in a simple chat or a private scene
(no network there). A missing or failing embeddings endpoint is a reason on
the report, never a failed turn: the keywords rank alone.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from sealedlore.engine.recall import (
    RecallItem,
    RecallReport,
    rank_recall,
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

    def _recall_candidates(self, history: Sequence[Node]) -> list[RecallItem] | None:
        """What could be recalled on this path, or None where recall never runs."""
        mode = self.config.recall
        if mode == "off" or self.story.chat or self.in_private:
            return None
        return recall_items(
            self.split(history),
            self.bundle.summaries,
            self.visible_cast(),
            mode=mode,
            texts=self.texts,
        )

    def recall_for(self, query: str, history: Sequence[Node]) -> RecallReport:
        """What to recall for this query on this path (`Config.recall`)."""
        if self.config.recall != "off" and not self.story.chat and self.in_private:
            return RecallReport(reason="not in a private scene")
        items = self._recall_candidates(history)
        if not items or not query.strip():
            return RecallReport()
        similarity, reason = self._recall_similarity(query, items)
        report = select_recall(
            items,
            rank_recall(query, items, similarity),
            k=self.config.recall_k,
            token_cap=self.config.recall_token_cap,
            count_tokens=self._count_lore_tokens,
        )
        return replace(report, reason=reason) if reason else report

    def _recall_similarity(
        self, query: str, items: Sequence[RecallItem]
    ) -> tuple[dict[str, float], str | None]:
        """Each item's similarity to the query, or nothing and why (keywords
        then rank alone)."""
        if self.embeddings is None:
            return {}, "keywords only: no embeddings endpoint"
        try:
            vectors = self._recall_vectors(items)
            target = self.query_vector(query)
        except ProviderError as exc:
            return {}, f"keywords only: {exc}"
        if target is None:
            return {}, "keywords only: the endpoint returned no vector"
        scored = [item for item in items if item.id in vectors]
        try:
            similarities = cosine_scores(target, [vectors[item.id] for item in scored])
        except ValueError as exc:
            return {}, f"keywords only: {exc}"
        return {item.id: score for item, score in zip(scored, similarities, strict=True)}, None

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

    def _carried(self, history: Sequence[Node]) -> tuple[RecallItem, ...]:
        """What a Question asked on this path within the last
        `recall_carry_turns` author turns recalled, as it stands now.

        The author asks about a detail and is answered from the chapters
        recalled for it; the turns that follow should know it too, or play
        contradicts the answer. Read from the path (the asides anchored on it),
        so a branch or Regenerate gets the same, and nothing is stored but what
        each Question recalled. A chapter no longer merged away (its part taken
        apart) is in the prompt already and is not carried."""
        turns = self.config.recall_carry_turns
        if not turns or not history:
            return ()
        items = self._recall_candidates(history)
        if not items:
            return ()
        place = {node.id: index for index, node in enumerate(history)}
        users_after = [0] * (len(history) + 1)
        for index in range(len(history) - 1, -1, -1):
            users_after[index] = users_after[index + 1] + (history[index].kind == "user")
        wanted: list[str] = []
        for aside in reversed(self.bundle.asides):  # the most recent Question first
            index = place.get(aside.anchor_node_id or "")
            if index is None or aside.private_span is not None or not aside.recalled:
                continue
            if users_after[index + 1] >= turns:
                continue
            wanted += [item_id for item_id in aside.recalled if item_id not in wanted]
        by_id = {item.id: item for item in items}
        chosen: list[RecallItem] = []
        used = 0
        for item_id in wanted:
            item = by_id.get(item_id)
            if item is None:
                continue
            cost = self._count_lore_tokens(item.text)
            if used + cost > self.config.recall_token_cap:
                continue
            chosen.append(replace(item, carried=True))
            used += cost
        return tuple(sorted(chosen, key=lambda item: item.position))

    def _recall_for_turn(
        self, query: str, history: Sequence[Node], *, question: bool = False
    ) -> tuple[RecallItem, ...]:
        """Recall for a turn or Question about to be sent; keep the report for the UI.

        A Question recalls for itself. A story turn carries what a recent
        Question recalled (`_carried`), and recalls for itself only with
        `Config.recall_in` "everywhere"."""
        if question:
            report = self.recall_for(query, history)
            self.last_recall = report
            return report.items
        carried = self._carried(history)
        if self.config.recall_in != "everywhere":
            self.last_recall = RecallReport(items=carried) if carried else None
            return carried
        report = self.recall_for(query, history)
        have = {item.id for item in carried}
        items = sorted(
            [*carried, *(item for item in report.items if item.id not in have)],
            key=lambda item: item.position,
        )
        self.last_recall = replace(report, items=tuple(items))
        return tuple(items)
