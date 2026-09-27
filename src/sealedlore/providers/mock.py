"""A scripted provider for tests and for driving the CLI without an endpoint.

Tests never hit the network (§12), so this is what they run against. It also
records the requests it was given, which is what makes prompt-assembly
assertions possible end-to-end.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Iterator, Sequence
from typing import Any

from sealedlore.models.node import Usage
from sealedlore.providers.base import (
    ChatProvider,
    ChatRequest,
    ReasoningDelta,
    StreamCancelled,
    StreamCompleted,
    StreamEvent,
    TextDelta,
)
from sealedlore.providers.embeddings import EmbeddingResult
from sealedlore.providers.wire import build_chat_payload

DEFAULT_RESPONSE = (
    "The door gives with a groan of swollen wood. Beyond it the undercroft "
    "breathes cold air up the stairwell, and something shifts in the dark that "
    "is too heavy to be a rat."
)


class MockChatProvider(ChatProvider):
    def __init__(
        self,
        responses: Sequence[str] | None = None,
        *,
        usage: Usage | None = None,
        reasoning: str | None = None,
        chunk_size: int = 32,
        error: Exception | None = None,
    ) -> None:
        self.responses = list(responses) if responses else [DEFAULT_RESPONSE]
        self.usage = usage or Usage(prompt_tokens=1200, completion_tokens=180, cost=0.004)
        self.reasoning = reasoning
        self.chunk_size = chunk_size
        self.error = error
        self.requests: list[ChatRequest] = []
        self.payloads: list[dict[str, Any]] = []
        self._call_count = 0
        self._cancelled = False
        # Seconds to wait before each chunk, to stand in for a slow endpoint.
        self.delay = 0.0

    def cancel(self) -> None:
        self._cancelled = True

    def reset_cancel(self) -> None:
        self._cancelled = False

    @property
    def last_request(self) -> ChatRequest | None:
        return self.requests[-1] if self.requests else None

    def build_payload(self, request: ChatRequest) -> dict[str, Any]:
        return build_chat_payload(request)

    def stream(self, request: ChatRequest) -> Iterator[StreamEvent]:
        self.requests.append(request)
        self.payloads.append(self.build_payload(request))

        if self.error is not None:
            raise self.error

        text = self.responses[self._call_count % len(self.responses)]
        self._call_count += 1

        if self.reasoning:
            yield ReasoningDelta(text=self.reasoning)

        for start in range(0, len(text), self.chunk_size):
            waited = 0.0
            while waited < self.delay and not self._cancelled:
                time.sleep(0.01)
                waited += 0.01
            if self._cancelled:
                raise StreamCancelled()
            yield TextDelta(text=text[start : start + self.chunk_size])

        # Log the usage in the wire's own field names, so story totals read
        # from the API log agree with what the session saw.
        raw = {
            "prompt_tokens": self.usage.prompt_tokens,
            "completion_tokens": self.usage.completion_tokens,
            "cache_read_input_tokens": self.usage.cache_read_tokens,
            "cache_creation_input_tokens": self.usage.cache_creation_tokens,
        }
        if self.usage.cost:
            raw["cost"] = self.usage.cost
        yield StreamCompleted(usage=self.usage, finish_reason="stop", raw_usage=raw)


class MockEmbeddings:
    """Deterministic bag-of-words vectors, so retrieval tests can assert order.

    Real embeddings are opaque and unstable across versions; hashing tokens
    into a small vector gives tests something they can reason about — texts
    sharing words score higher than texts that don't — without a network call.
    """

    def __init__(
        self,
        *,
        dimensions: int = 64,
        model: str = "mock-embed",
        error: Exception | None = None,
        usage: Usage | None = None,
    ) -> None:
        self.dimensions = dimensions
        self.model = model
        self.error = error
        self.usage = usage or Usage(prompt_tokens=10)
        self.calls: list[tuple[tuple[str, ...], bool]] = []

    def embed(self, texts: Sequence[str], *, as_query: bool = False) -> EmbeddingResult:
        self.calls.append((tuple(texts), as_query))
        if self.error is not None:
            raise self.error

        vectors = tuple(self._vector(text) for text in texts)
        return EmbeddingResult(
            vectors=vectors,
            model=self.model,
            dimensions=self.dimensions,
            usage=self.usage,
        )

    def _vector(self, text: str) -> tuple[float, ...]:
        buckets = [0.0] * self.dimensions
        for word in re.findall(r"[a-z0-9']+", text.lower()):
            buckets[hash_bucket(word, self.dimensions)] += 1.0
        norm = math.sqrt(sum(value * value for value in buckets))
        if norm == 0:
            buckets[0] = 1.0
            norm = 1.0
        return tuple(value / norm for value in buckets)


def hash_bucket(word: str, dimensions: int) -> int:
    """Stable across processes, unlike hash() with randomised seeds."""
    digest = hashlib.sha1(word.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % dimensions
