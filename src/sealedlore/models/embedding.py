"""Cached lore embeddings (embeddings.json).

Keyed by content hash and model name so a re-embed only happens when the
lore text or the embedding model changes.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sealedlore.ids import utc_now_iso


class EmbeddingCacheEntry(BaseModel):
    content_hash: str
    model: str
    dimensions: int
    vector: list[float]
    created_at: str = Field(default_factory=utc_now_iso)
