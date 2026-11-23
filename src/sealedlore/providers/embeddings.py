"""Client for any OpenAI-compatible /embeddings endpoint. See §7.

Deliberately not an abstract base class (§12): `EmbeddingBackend` is a
Protocol, so the real client and the test double share a shape without a class
hierarchy nothing else needs.

The endpoint is configured separately from the chat one because it may be a
local server — which is also why `require_https` permits plain http on
loopback. Blanks inherit from the chat provider, since one endpoint commonly
serves both.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from sealedlore.call_log import CALLS, EMBEDDINGS, plain_protection
from sealedlore.models.config import EmbeddingProviderConfig
from sealedlore.models.node import Usage
from sealedlore.providers.base import ProviderError, checked_key, stated_cost, stated_count
from sealedlore.providers.http import make_client, reply_json
from sealedlore.providers.privacy import EMBEDDINGS_WAY_OUT, plain_refusal


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: tuple[tuple[float, ...], ...]
    model: str
    dimensions: int
    usage: Usage = field(default_factory=Usage)


class EmbeddingBackend(Protocol):
    """What retrieval needs from an embeddings endpoint."""

    def embed(self, texts: Sequence[str], *, as_query: bool = False) -> EmbeddingResult: ...


def build_embedding_payload(
    texts: Sequence[str], config: EmbeddingProviderConfig
) -> dict[str, Any]:
    """The exact request body, so the log and the tests can see it."""
    payload: dict[str, Any] = {
        "model": config.model,
        "input": list(texts),
        "encoding_format": "float",
    }
    # Only send `dimensions` when asked for: models without Matryoshka support
    # reject it outright rather than ignoring it.
    if config.dimensions > 0:
        payload["dimensions"] = config.dimensions
    return payload


def parse_embedding_response(body: dict[str, Any], *, expected: int) -> list[tuple[float, ...]]:
    """Pull vectors out in input order.

    `index` is authoritative rather than list position: the spec allows a
    provider to return them out of order, and a silently shuffled batch would
    attach every vector to the wrong lore entry.
    """
    rows = body.get("data")
    if not isinstance(rows, list):
        raise ProviderError(f"embeddings response has no data array: {str(body)[:200]!r}")

    ordered: list[tuple[int, tuple[float, ...]]] = []
    for position, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ProviderError("embeddings response contained a non-object row")
        vector = row.get("embedding")
        if not isinstance(vector, list) or not vector:
            raise ProviderError("embeddings response contained a row with no embedding")
        index = row.get("index")
        position = index if isinstance(index, int) else position
        ordered.append((position, _numbers(vector)))

    ordered.sort(key=lambda pair: pair[0])
    vectors = [vector for _, vector in ordered]
    if len(vectors) != expected:
        raise ProviderError(f"asked for {expected} embeddings, got {len(vectors)}")

    widths = {len(vector) for vector in vectors}
    if len(widths) > 1:
        raise ProviderError(f"embeddings came back with mixed dimensions: {sorted(widths)}")
    return vectors


def _numbers(vector: list[Any]) -> tuple[float, ...]:
    """One row's vector as floats, every one a finite number, or the
    endpoint's failure. llama.cpp and Ollama write a NaN as `null`, and JSON
    lets any server write NaN or 1e999: `float()` of the first raised a
    TypeError that no caller takes for an endpoint failing (retrieval falls
    back to keywords only on ProviderError), so every turn was lost; the
    others would have been kept with the story and scored NaN against every
    turn after."""
    numbers: list[float] = []
    for value in vector:
        number: float | None = None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                number = float(value)
            except OverflowError:  # a whole number past what a float holds
                number = None
        if number is None or not math.isfinite(number):
            raise ProviderError(
                "embeddings response contained an embedding that isn't all finite numbers "
                f"(it holds {str(value)[:40]!r})"
            )
        numbers.append(number)
    return tuple(numbers)


class OpenAICompatibleEmbeddings:
    def __init__(
        self, config: EmbeddingProviderConfig, *, client: httpx.Client | None = None
    ) -> None:
        self.config = config
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = make_client(
                self.config.base_url, timeout=httpx.Timeout(self.config.timeout_seconds)
            )
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def detached(self) -> OpenAICompatibleEmbeddings:
        """A copy with a client of its own, for embedding in the background
        beside the turns (engine/session_lore_vectors.py); its owner closes it."""
        return OpenAICompatibleEmbeddings(self.config)

    @property
    def endpoint(self) -> str:
        return self.config.base_url.rstrip("/") + "/embeddings"

    def _headers(self) -> dict[str, str]:
        if self.config.key_withheld:
            raise ProviderError(self.config.key_withheld)
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {checked_key(self.config.api_key, 'embeddings')}"
        return headers

    def embed(self, texts: Sequence[str], *, as_query: bool = False) -> EmbeddingResult:
        """Embed a batch, in order, prefixed if the model wants it."""
        if not texts:
            return EmbeddingResult(vectors=(), model=self.config.model, dimensions=0)

        prefix = self.config.query_prefix if as_query else self.config.document_prefix
        prepared = [f"{prefix}{text}" if prefix else text for text in texts]

        vectors: list[tuple[float, ...]] = []
        usage = Usage()
        size = max(self.config.batch_size, 1)
        for start in range(0, len(prepared), size):
            batch = prepared[start : start + size]
            body = self._post(batch)
            vectors.extend(parse_embedding_response(body, expected=len(batch)))
            usage = _add_usage(usage, body.get("usage"))

        return EmbeddingResult(
            vectors=tuple(vectors),
            model=self.config.model,
            dimensions=len(vectors[0]) if vectors else 0,
            usage=usage,
        )

    def _post(self, batch: Sequence[str]) -> dict[str, Any]:
        # Never to an end-to-end encrypted endpoint, nor one of Chutes' TEE
        # models (providers/privacy.py): the text to embed would go to it in
        # the clear.
        refusal = plain_refusal(
            self.endpoint,
            "the text to embed",
            api=self.config.sealed_api,
            model=self.config.model,
            way_out=EMBEDDINGS_WAY_OUT,
        )
        if refusal:
            raise ProviderError(refusal)
        payload = build_embedding_payload(batch, self.config)
        # Kept in the run's call log (call_log.py), with nothing embedded.
        call = CALLS.start(
            EMBEDDINGS,
            self.config.model,
            endpoint="Embeddings",
            address=self.endpoint,
            protection=plain_protection(self.endpoint),
        )
        try:
            body = self._sent(payload)
        except Exception as exc:
            CALLS.finish(call, "failed", detail=str(exc).splitlines()[0] if str(exc) else "")
            raise
        usage = body.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        CALLS.finish(
            call,
            "done",
            detail=f"{len(batch)} text{'s' if len(batch) != 1 else ''}",
            tokens_in=stated_count(usage.get("prompt_tokens")),
            cost=stated_cost(usage.get("cost")),
        )
        return body

    def _sent(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._get_client().post(self.endpoint, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.endpoint} unreachable: {exc}") from exc

        if response.status_code >= 400:
            raise ProviderError(
                f"{self.endpoint} returned HTTP {response.status_code}",
                status_code=response.status_code,
                body=response.text,
            )
        try:
            body = reply_json(response)
        except ValueError as exc:
            raise ProviderError(f"{self.endpoint} returned a non-JSON body") from exc
        if not isinstance(body, dict):
            raise ProviderError(
                f"{self.endpoint} returned {type(body).__name__}, expected an object"
            )
        return body


def _add_usage(total: Usage, raw: Any) -> Usage:
    if not isinstance(raw, dict):
        return total
    # Only a count that is one, and a charge that is one (`stated_count`):
    # the vectors are good whatever the endpoint wrote beside them.
    tokens = stated_count(raw.get("prompt_tokens")) or stated_count(raw.get("total_tokens")) or 0
    spend = stated_cost(raw.get("cost")) or 0.0
    return Usage(prompt_tokens=total.prompt_tokens + tokens, cost=total.cost + spend)
