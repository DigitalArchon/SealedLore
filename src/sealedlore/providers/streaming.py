"""The HTTP machinery a streaming chat endpoint needs, whatever it speaks.

`OpenAICompatibleProvider` and `AnthropicProvider` differ in what they send
and how they read what comes back; everything around that is the same and
lives here: one httpx client per provider (a fresh connection per request,
so the socket can be captured and shut down to cancel), a sibling for a call
made alongside one of this one's, a detached copy for background work, and
the wait before a retry on a rate limit or a momentary outage.
"""

from __future__ import annotations

import socket
import sys
import threading
from abc import abstractmethod
from collections.abc import Iterator
from typing import Any

import httpx

from sealedlore.models.config import ProviderConfig
from sealedlore.providers.base import (
    ChatProvider,
    ChatRequest,
    ProviderError,
    StreamCancelled,
    StreamEvent,
)
from sealedlore.providers.http import make_client

# A rate limit or a momentary outage, before anything has streamed, is waited
# out rather than failing the turn. Measured on nano-gpt's subscription: a
# burst of about 25 calls in a few seconds gets 429 with Retry-After: 5, and
# the next call a few seconds later goes through. Live, DeepSeek's "Service
# temporarily unavailable" (503) failed 3 of 21 turns outright. Anthropic's
# own API says "overloaded" as 529.
RETRY_ATTEMPTS = 3
RETRY_STATUS = frozenset({429, 502, 503, 529})
RETRY_DEFAULT_SECONDS = 5.0
RETRY_MAX_SECONDS = 20.0


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


class StreamingHttpProvider(ChatProvider):
    """A provider that streams one request at a time over its own client.

    A subclass renders the request (`_stream` sends it and yields the
    events) and names its `endpoint`; cancelling, retrying, siblings and
    closing are the same for every protocol.
    """

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
        self._sibling: StreamingHttpProvider | None = None
        # Set by cancel(), so a wait before a retry ends at once.
        self._wake = threading.Event()

    def _twin(self) -> StreamingHttpProvider:
        """Another provider on the same endpoint, of this one's kind."""
        return type(self)(self.config)

    def sibling(self) -> StreamingHttpProvider:
        """A second client on the same endpoint, for a call made alongside one
        of this one's (the two reads after a passage). Each provider tracks one
        request's socket, so a concurrent call needs its own; `cancel` and
        `close` reach both. Built once."""
        if self._sibling is None:
            self._sibling = self._twin()
        self._sibling.route_watch = self.route_watch
        self._sibling.reasoning_watch = self.reasoning_watch
        return self._sibling

    def detached(self) -> StreamingHttpProvider:
        """An independent client for background work that outlives a turn
        (archival, merges). Unlike `sibling`, a Stop doesn't reach it and it
        is never shared, so it can't collide with the reads after the next
        passage. The caller closes it."""
        detached = self._twin()
        detached.route_watch = self.route_watch
        detached.reasoning_watch = self.reasoning_watch
        return detached

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
    @abstractmethod
    def endpoint(self) -> str:
        """Where a chat request goes."""

    def _http_error(self, url: str, response: httpx.Response) -> ProviderError:
        """A response that refused the request, as the error to raise: its
        status, its body (the endpoint's own words) and how long it asked
        us to wait."""
        body = response.read().decode("utf-8", errors="replace")
        error = ProviderError(
            f"{url} returned HTTP {response.status_code}",
            status_code=response.status_code,
            body=body,
        )
        error.retry_after = _seconds(response.headers.get("retry-after"))
        return error

    @abstractmethod
    def _stream(self, request: ChatRequest) -> Iterator[StreamEvent]:
        """Send one request and yield its events, the last a StreamCompleted."""

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
