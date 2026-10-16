"""Streaming client for any OpenAI-compatible /chat/completions endpoint.

Default target is nano-gpt (https://nano-gpt.com/api/v1); anything speaking
the same protocol works, including OpenRouter. Endpoint-specific request
fields go through ProviderConfig.extra_body rather than growing a flag here.
The HTTP machinery (one client, cancel, retries, siblings) is
`providers/streaming.py`, shared with the native Anthropic client.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx

from sealedlore.models.config import on_nanogpt
from sealedlore.providers.base import (
    ChatRequest,
    ProviderError,
    ReasoningDelta,
    StreamCancelled,
    StreamCompleted,
    StreamEvent,
    TextDelta,
    parse_usage,
)
from sealedlore.providers.private_catalog import (
    fetch_private_ids,
    offers_private_mode,
    private_entries,
)
from sealedlore.providers.reasoning_defaults import fetch_reasoning_defaults
from sealedlore.providers.streaming import StreamingHttpProvider
from sealedlore.providers.tee import is_private_mode
from sealedlore.providers.wire import build_chat_payload, cache_headers

SSE_DATA_PREFIX = "data:"
SSE_DONE = "[DONE]"


def with_reported_cost(usage: dict[str, Any], pricing: dict[str, Any]) -> dict[str, Any]:
    """Fill a missing `cost` from a separate pricing record, when one came.

    The usage block stays as the endpoint sent it otherwise, so the API log
    keeps the verbatim record plus the one field we could vouch for.
    """
    if not pricing or isinstance(usage.get("cost"), (int, float)):
        return usage
    cost = pricing.get("cost", pricing.get("amount"))
    if not isinstance(cost, (int, float)):
        return usage
    return {**usage, "cost": cost, "cost_source": "x_nanogpt_pricing"}


class OpenAICompatibleProvider(StreamingHttpProvider):
    @property
    def endpoint(self) -> str:
        return self.config.base_url.rstrip("/") + "/chat/completions"

    def fetch_model_prices(self) -> dict[str, dict[str, Any]]:
        """GET /models (detailed): nano-gpt lists prices only with ?detailed=true."""
        url = self.config.base_url.rstrip("/") + "/models"
        try:
            response = self._get_client().get(
                url, params={"detailed": "true"}, headers=self._headers(), timeout=30.0
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"couldn't fetch the models list: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(
                f"{url} returned HTTP {response.status_code}", status_code=response.status_code
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError("the models list wasn't JSON") from exc
        items = data.get("data", []) if isinstance(data, dict) else data
        listing = {
            item["id"]: item
            for item in items
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        if offers_private_mode(self.config.base_url):
            # The end-to-end encrypted models, listed apart (private_catalog).
            listing.update(private_entries(fetch_private_ids(self.config.base_url), listing))
        return listing

    def fetch_reasoning_defaults(self) -> dict[str, dict[str, Any]] | None:
        """NanoGPT states each model's default reasoning in its web listing;
        no other endpoint is known to."""
        if not on_nanogpt(self.config.base_url):
            return None
        return fetch_reasoning_defaults(self.config.base_url)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def build_payload(self, request: ChatRequest) -> dict[str, Any]:
        return build_chat_payload(request, provider_extra_body=self.config.extra_body)

    # --- what a subclass may change about one request (providers/private_mode.py)

    def _prepare(self, request: ChatRequest) -> tuple[str, dict[str, Any], dict[str, str]]:
        """The URL, body and headers of one request."""
        if is_private_mode(request.model):
            # A Private Mode model is only ever sent sealed to its attested
            # enclave; this client would send it in the clear.
            raise ProviderError(
                f"{request.model} is end-to-end encrypted and can only be sent through "
                "the encrypted client; nothing was sent"
            )
        return (
            self.endpoint,
            self.build_payload(request),
            {**self._headers(), **cache_headers(request)},
        )

    def _accept(self, response: httpx.Response) -> None:
        """Called on a successful response before its body is read."""

    def _finish(self, finish_reason: str | None, saw_done: bool) -> None:
        """Called once the stream has ended, before it counts as complete."""

    sealed_replies = False

    def _stream(self, request: ChatRequest) -> Iterator[StreamEvent]:
        url, payload, headers = self._prepare(request)
        usage_raw: dict[str, Any] = {}
        pricing: dict[str, Any] = {}
        finish_reason: str | None = None
        response_id: str | None = None
        saw_done = False
        reasoning_chars = 0

        with self._get_client().stream(
            "POST",
            url,
            json=payload,
            headers=headers,
            extensions={"trace": self._trace},
        ) as response:
            if response.status_code >= 400:
                raise self._http_error(url, response)
            self._accept(response)

            for line in response.iter_lines():
                if not line or not line.startswith(SSE_DATA_PREFIX):
                    continue
                data = line[len(SSE_DATA_PREFIX) :].strip()
                if data == SSE_DONE:
                    saw_done = True
                    break

                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise ProviderError(f"malformed stream chunk: {data[:200]!r}") from exc
                if not isinstance(chunk, dict):
                    raise ProviderError(f"malformed stream chunk: {data[:200]!r}")

                error = chunk.get("error")
                if error:
                    message = error.get("message") if isinstance(error, dict) else str(error)
                    raise ProviderError(f"provider reported an error: {message}")

                if response_id is None and isinstance(chunk.get("id"), str):
                    response_id = chunk["id"]
                if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
                    usage_raw = chunk["usage"]
                # nano-gpt's own record of the charge. Its `usage.cost` comes
                # and goes between responses; this object is the fallback.
                if isinstance(chunk.get("x_nanogpt_pricing"), dict):
                    pricing = chunk["x_nanogpt_pricing"]

                for choice in chunk.get("choices") or []:
                    if not isinstance(choice, dict):
                        continue
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    delta = choice.get("delta")
                    if not isinstance(delta, dict):
                        continue

                    reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                    if reasoning:
                        reasoning_chars += len(reasoning)
                        yield ReasoningDelta(text=reasoning)

                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        yield TextDelta(text=content)
                    elif isinstance(content, list):
                        # Some proxies echo Anthropic-style block deltas back.
                        for block in content:
                            text = block.get("text") if isinstance(block, dict) else None
                            if text:
                                yield TextDelta(text=text)

        if self._cancelled:
            # Shut down mid-stream, the connection just reads as ended.
            raise StreamCancelled()
        self._finish(finish_reason, saw_done)
        usage_raw = with_reported_cost(usage_raw, pricing)
        completed = StreamCompleted(
            usage=parse_usage(usage_raw),
            finish_reason=finish_reason,
            raw_usage=usage_raw,
            response_id=response_id,
            sealed=self.sealed_replies,
            reasoning_tokens=max(_reasoning_tokens(usage_raw), reasoning_chars // 4),
        )
        self._report_route(request, completed)
        self._report_reasoning(request, completed)
        yield completed


def _reasoning_tokens(usage: dict[str, Any]) -> int:
    """The reply's reported reasoning tokens, wherever the endpoint put them."""
    details = usage.get("completion_tokens_details")
    count = details.get("reasoning_tokens") if isinstance(details, dict) else None
    if not isinstance(count, int):
        count = usage.get("reasoning_tokens")
    return count if isinstance(count, int) and not isinstance(count, bool) else 0
