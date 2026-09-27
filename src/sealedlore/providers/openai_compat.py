"""Streaming client for any OpenAI-compatible /chat/completions endpoint.

Default target is nano-gpt (https://nano-gpt.com/api/v1); anything speaking
the same protocol works, including OpenRouter. Endpoint-specific request
fields go through ProviderConfig.extra_body rather than growing a flag here.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from collections.abc import Iterator
from typing import Any

import httpx

from sealedlore.models.config import ProviderConfig
from sealedlore.providers.base import (
    ChatProvider,
    ChatRequest,
    ProviderError,
    ReasoningDelta,
    StreamCancelled,
    StreamCompleted,
    StreamEvent,
    TextDelta,
    parse_usage,
)
from sealedlore.providers.http import make_client
from sealedlore.providers.private_catalog import (
    fetch_private_ids,
    offers_private_mode,
    private_entries,
)
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


def _shut_down(sock: socket.socket | None) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass  # already closed, or never connected
    if sys.platform == "win32":
        # Winsock doesn't wake a thread blocked reading a socket that is shut
        # down (tests/test_cancel.py, on GitHub's Windows runner); closing it
        # does, and the read's error is then taken as the stop it is.
        try:
            sock.close()
        except OSError:
            pass


# A rate limit or a momentary outage, before anything has streamed, is waited
# out rather than failing the turn. Measured on nano-gpt's subscription: a
# burst of about 25 calls in a few seconds gets 429 with Retry-After: 5, and
# the next call a few seconds later goes through. Live, DeepSeek's "Service
# temporarily unavailable" (503) failed 3 of 21 turns outright.
RETRY_ATTEMPTS = 3
RETRY_STATUS = frozenset({429, 502, 503, 529})
RETRY_DEFAULT_SECONDS = 5.0
RETRY_MAX_SECONDS = 20.0


def _seconds(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None  # an HTTP date; the default will do


def _retry_delay(error: ProviderError) -> float | None:
    """How long to wait before trying again, or None if this isn't worth a retry."""
    if error.status_code not in RETRY_STATUS:
        return None
    wait = error.retry_after or RETRY_DEFAULT_SECONDS
    return min(max(wait, 1.0), RETRY_MAX_SECONDS)


class OpenAICompatibleProvider(ChatProvider):
    def __init__(self, config: ProviderConfig, *, client: httpx.Client | None = None) -> None:
        self.config = config
        self._client = client
        self._owns_client = client is None
        # Cancellation (see cancel()): the socket of the request in flight,
        # captured through httpcore's trace hook as the connection opens.
        self._lock = threading.Lock()
        self._in_flight = False
        self._cancelled = False
        self._socket: socket.socket | None = None
        self._sibling: OpenAICompatibleProvider | None = None
        # Set by cancel(), so a wait before a retry ends at once.
        self._wake = threading.Event()

    def sibling(self) -> OpenAICompatibleProvider:
        """A second client on the same endpoint, for a call made alongside one
        of this one's (the two reads after a passage). Each provider tracks one
        request's socket, so a concurrent call needs its own; `cancel` and
        `close` reach both. Built once."""
        if self._sibling is None:
            self._sibling = OpenAICompatibleProvider(self.config)
        return self._sibling

    def detached(self) -> OpenAICompatibleProvider:
        """An independent client for background work that outlives a turn
        (archival, merges). Unlike `sibling`, a Stop doesn't reach it and it
        is never shared, so it can't collide with the reads after the next
        passage. The caller closes it."""
        return OpenAICompatibleProvider(self.config)

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = make_client(
                self.config.base_url,
                timeout=httpx.Timeout(self.config.timeout_seconds),
                # A fresh connection per request, so the trace hook always sees
                # the socket being opened: a pooled connection is reused without
                # a trace event, and its socket couldn't be shut down to cancel.
                # One TLS handshake per call is nothing beside a model's latency.
                limits=httpx.Limits(max_keepalive_connections=0),
            )
        return self._client

    def cancel(self) -> None:
        """Abort the request in flight at once, whatever it is waiting on.

        Closing a socket doesn't wake a thread blocked reading it; shutting it
        down does — measured at about a millisecond, both while waiting for
        the first byte and mid-stream. Called from the GUI thread.
        """
        with self._lock:
            self._cancelled = True
            sock = self._socket if self._in_flight else None
        self._wake.set()
        _shut_down(sock)
        if self._sibling is not None:
            self._sibling.cancel()

    def reset_cancel(self) -> None:
        with self._lock:
            self._cancelled = False
        self._wake.clear()
        if self._sibling is not None:
            self._sibling.reset_cancel()

    def _trace(self, event_name: str, info: dict[str, Any]) -> None:
        if event_name not in ("connection.connect_tcp.complete", "connection.start_tls.complete"):
            return
        stream = info.get("return_value")
        sock = stream.get_extra_info("socket") if stream is not None else None
        if sock is None:
            return
        with self._lock:
            # The TLS socket supersedes the TCP one it wraps.
            self._socket = sock
            cancelled = self._cancelled
        if cancelled:
            _shut_down(sock)

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None
        if self._sibling is not None:
            self._sibling.close()

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

    def stream(self, request: ChatRequest) -> Iterator[StreamEvent]:
        with self._lock:
            if self._cancelled:
                raise StreamCancelled()  # stopped before the request went out
            self._in_flight, self._socket = True, None
        try:
            # Timeouts and dropped connections are provider failures like any
            # other: callers that must never lose the author's turn catch
            # ProviderError. A cancelled request can surface as an error or as
            # an early, clean end of the stream; either way it is a stop.
            for attempt in range(RETRY_ATTEMPTS + 1):
                started = False
                try:
                    for event in self._stream(request):
                        started = True
                        yield event
                    return
                except ProviderError as exc:
                    delay = _retry_delay(exc)
                    if started or delay is None or attempt == RETRY_ATTEMPTS:
                        raise
                except (httpx.HTTPError, OSError) as exc:
                    if self._cancelled:
                        raise StreamCancelled() from exc
                    reason = str(exc) or type(exc).__name__
                    raise ProviderError(f"{self.endpoint} failed: {reason}") from exc
                if self._wake.wait(delay) or self._cancelled:
                    raise StreamCancelled()
        finally:
            with self._lock:
                self._in_flight, self._socket = False, None

    def _stream(self, request: ChatRequest) -> Iterator[StreamEvent]:
        url, payload, headers = self._prepare(request)
        usage_raw: dict[str, Any] = {}
        pricing: dict[str, Any] = {}
        finish_reason: str | None = None
        response_id: str | None = None
        saw_done = False

        with self._get_client().stream(
            "POST",
            url,
            json=payload,
            headers=headers,
            extensions={"trace": self._trace},
        ) as response:
            if response.status_code >= 400:
                body = response.read().decode("utf-8", errors="replace")
                error = ProviderError(
                    f"{url} returned HTTP {response.status_code}",
                    status_code=response.status_code,
                    body=body,
                )
                error.retry_after = _seconds(response.headers.get("retry-after"))
                raise error
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
        yield StreamCompleted(
            usage=parse_usage(usage_raw),
            finish_reason=finish_reason,
            raw_usage=usage_raw,
            response_id=response_id,
            sealed=self.sealed_replies,
        )
