"""The HTTP machinery a streaming chat endpoint needs, whatever it speaks.

`OpenAICompatibleProvider` and `AnthropicProvider` differ in what they send
and how they read what comes back; everything around that is the same and
lives here: one httpx client per provider (a fresh connection per request,
so the socket can be captured and shut down to cancel), a sibling for a call
made alongside one of this one's, a detached copy for background work, and
the wait before a retry on a rate limit or a momentary outage.
"""

from __future__ import annotations

import codecs
import re
import socket
import sys
import threading
from abc import abstractmethod
from collections.abc import Iterator
from typing import Any

import httpx

from sealedlore.call_log import CALLS, TEXT, plain_protection, route_words
from sealedlore.models.config import ProviderConfig, sealed_service, shown
from sealedlore.providers.base import (
    ChatProvider,
    ChatRequest,
    ProviderError,
    StreamCancelled,
    StreamCompleted,
    StreamEvent,
)
from sealedlore.providers.http import ERROR_BODY_BYTES, make_client, traced_socket
from sealedlore.providers.privacy import SERVICE_NAMES, plain_refusal, privacy_for

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
# Read from an endpoint no further than this. A stream's line (one event) is
# a few kilobytes, a picture sent inline a few megabytes: a longer one is no
# reply, and reading on would hold memory for as long as the endpoint sent.
MAX_LINE_CHARS = 32 * 1024 * 1024
# A piece of a stream is made text, and looked through for its lines, this
# many bytes at a time. A piece comes as large as what was received unpacks
# to (gzip, about a thousandfold: providers/http.py), and made text whole and
# split at every line end, a megabyte of compressed line ends was a list of
# a thousand million empty lines.
READ_SLICE = 64 * 1024
_LINE_ENDS = re.compile(r"[\r\n]+")


def read_lines(response: httpx.Response, body: Any = None) -> Iterator[str]:
    """The stream's lines as `iter_lines` gives them (split at CRLF, CR or
    LF), but that a run of empty lines comes as one: to a reader of events a
    blank line ends an event and more of them are nothing, and a stream of
    nothing but line ends (96 KB of gzip is 96 MB of them) is then read at
    the cost of looking through it, with no line built for any. Each byte is
    also fed to `body` (a hash) as it arrives, decoded of any transfer
    encoding. A piece is read `READ_SLICE` at a time and each slice looked
    through once, so a long line costs its length and no more, and a line
    past MAX_LINE_CHARS is a ProviderError."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    line: list[str] = []  # the line not yet ended, in the pieces it came in
    length = 0
    held_cr = False  # a CR at a slice's end may be the first half of a CRLF
    blank = False  # the line last given was an empty one

    def ended(text: str, final: bool) -> Iterator[str]:
        nonlocal length, held_cr, blank
        if held_cr:
            text = "\r" + text
        held_cr = not final and text.endswith("\r")
        if held_cr:
            text = text[:-1]
        at = 0
        for run in _LINE_ENDS.finditer(text):
            stop, after = run.span()
            if stop > at:
                kept(text[at:stop])
            # How many lines the run ends: a CRLF is one line end, not two.
            ends = after - stop
            if ends > 1:
                ends -= text.count("\r\n", stop, after)
            if length:
                yield line[0] if len(line) == 1 else "".join(line)
                line.clear()
                length = 0
                blank = False
                ends -= 1
            if ends and not blank:
                yield ""
                blank = True
            at = after
        if at < len(text):
            kept(text[at:])

    def kept(part: str) -> None:
        nonlocal length
        length += len(part)
        if length > MAX_LINE_CHARS:
            raise ProviderError(
                f"the endpoint sent a line of more than {MAX_LINE_CHARS // (1024 * 1024)} MB "
                "with no end; not read"
            )
        line.append(part)

    for chunk in response.iter_bytes():
        if body is not None:
            body.update(chunk)
        for start in range(0, len(chunk), READ_SLICE):
            yield from ended(decoder.decode(chunk[start : start + READ_SLICE]), final=False)
    yield from ended(decoder.decode(b"", final=True), final=True)
    if length:
        yield "".join(line)


def read_body(response: httpx.Response, most: int) -> bytes:
    """A response's body (decoded of any transfer encoding) to `most` bytes,
    a piece at a time; past that, a ProviderError, with nothing more read."""
    declared = response.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and len(declared) < 19 and int(declared) > most:
        raise ProviderError(_too_large(most))
    kept = bytearray()
    for piece in response.iter_bytes():
        kept += piece
        if len(kept) > most:
            raise ProviderError(_too_large(most))
    return bytes(kept)


def read_start(response: httpx.Response, most: int = ERROR_BODY_BYTES) -> bytes:
    """The start of a response's body, `most` bytes at most: all of a
    refusal that is ever read. Read whole, a refusal could be as large as
    the endpoint liked."""
    kept = bytearray()
    try:
        for piece in response.iter_bytes():
            kept += piece[: most - len(kept)]
            if len(kept) >= most:
                break
    except httpx.HTTPError:
        pass  # the words as far as they came
    return bytes(kept)


def _too_large(most: int) -> str:
    return f"the endpoint sent more than {most // (1024 * 1024)} MB; not read"


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

    # The end-to-end encrypted protocol of a sealed endpoint this client seals
    # every request in (models/config.py `SEALED_APIS`: Tinfoil's), or "" for
    # any other, which never reaches such an endpoint. Chutes' client is one
    # of those: Chutes' hosts aren't sealed endpoints, and it seals each
    # request itself (providers/chutes_provider.py).
    seals = ""

    def _twin(self) -> StreamingHttpProvider:
        """Another provider on the same endpoint, of this one's kind."""
        return type(self)(self.config)

    def sibling(self) -> StreamingHttpProvider:
        """A second client on the same endpoint, for a call made alongside one
        of this one's (the reads after a passage). Each provider tracks one
        request's socket, so a concurrent call needs its own; a third call
        goes on the sibling's own sibling (`base.side_by_side`). `cancel` and
        `close` reach every one down the line. Built once."""
        if self._sibling is None:
            self._sibling = self._twin()
        self._sibling.route_watch = self.route_watch
        self._sibling.reasoning_watch = self.reasoning_watch
        self._sibling.tee_watch = self.tee_watch
        return self._sibling

    def detached(self) -> StreamingHttpProvider:
        """An independent client for background work that outlives a turn
        (archival, merges). Unlike `sibling`, a Stop doesn't reach it and it
        is never shared, so it can't collide with the reads after the next
        passage. The caller closes it."""
        detached = self._twin()
        detached.route_watch = self.route_watch
        detached.reasoning_watch = self.reasoning_watch
        detached.tee_watch = self.tee_watch
        return detached

    def _get_client(self) -> httpx.Client:
        # Judged by the address, whatever the model: a plain client never
        # reaches an end-to-end encrypted endpoint, not even with a listing
        # (it carries the key). Only the sealing client of that endpoint's
        # own protocol (`seals`) may. (One of Chutes' TEE models is refused
        # by the request's own model, `plain_refusal(model=)`.)
        if sealed_service(self.endpoint, self.config.api) != self.seals:
            refusal = plain_refusal(self.endpoint, api=self.config.api)
            if refusal:
                raise ProviderError(refusal)
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
        sock = traced_socket(event_name, info)
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
        status, its body (the endpoint's own words, as far as ERROR_BODY_BYTES)
        and how long it asked us to wait."""
        body = read_start(response).decode("utf-8", errors="replace")
        error = ProviderError(
            f"{url} returned HTTP {response.status_code}",
            status_code=response.status_code,
            body=body,
        )
        error.retry_after = _seconds(response.headers.get("retry-after"))
        return error

    def _redirected(self, url: str, response: httpx.Response) -> ProviderError:
        """A redirect (a 308 from www to the bare host, say), as the error to
        raise. The client follows none: a redirect re-sends the prompt, and the
        key, to wherever it points. So it is said, and where to, for the
        author to put that address in Settings if the endpoint has moved; and
        it is never taken for a reply with nothing in it."""
        location = response.headers.get("location")
        target = None
        if location:
            try:
                target = str(response.request.url.join(location))
            except httpx.InvalidURL:
                target = location
        where = f" to {shown(target)[:200]}" if target else ""
        return ProviderError(
            f"{url} redirected (HTTP {response.status_code}){where}. Nothing was sent there: if "
            "the endpoint has moved, put its new address in Settings → Endpoints.",
            status_code=response.status_code,
        )

    def _no_stream(self, url: str, response: httpx.Response) -> ProviderError:
        """An answer that carried no event stream at all (a web page where the
        API was expected, a sign-in page on a local network), as the error to
        raise: nothing in it was a reply, and an empty passage would say
        nothing of why. A body with SSE `data:` lines is a stream whatever its
        content type says; only one with none comes here."""
        kind = response.headers.get("content-type", "").split(";")[0].strip() or "no content type"
        return ProviderError(
            f"{url} answered HTTP {response.status_code} ({kind[:80]}) with no reply in it, not "
            "the stream a chat endpoint sends. Check the endpoint's address in Settings → "
            "Endpoints: a web page answers like this where the API was expected (an address "
            "without its /v1, say).",
            status_code=response.status_code,
        )

    @abstractmethod
    def _stream(self, request: ChatRequest) -> Iterator[StreamEvent]:
        """Send one request and yield its events, the last a StreamCompleted."""

    def stream(self, request: ChatRequest) -> Iterator[StreamEvent]:
        """The request, sent: every call kept in the run's call log
        (call_log.py), whatever becomes of it, with nothing that was said."""
        call = CALLS.start(
            TEXT,
            request.model,
            endpoint=self.config.name,
            address=self.endpoint,
            protection=self._protection(request),
            route=route_words(request.extra_body.get("provider")),
            purpose=request.purpose,
        )
        completed: StreamCompleted | None = None
        try:
            for event in self._sent(request):
                if isinstance(event, StreamCompleted):
                    completed = event
                yield event
        except StreamCancelled:
            CALLS.finish(call, "stopped")
            raise
        except GeneratorExit:
            # Closed early: a Stop keeping what came, or a caller done with it.
            CALLS.finish(call, "stopped", **_counts(completed))
            raise
        except Exception as exc:
            CALLS.finish(call, "failed", detail=str(exc).splitlines()[0] if str(exc) else "")
            raise
        if completed is None:
            CALLS.finish(call, "stopped")
            return
        CALLS.finish(
            call,
            "done",
            detail=completed.finish_reason or "",
            protection=self._protection(request, completed),
            **_counts(completed),
        )

    def _protection(self, request: ChatRequest, completed: StreamCompleted | None = None) -> str:
        """How the call's content travels, as the call log says it."""
        privacy = privacy_for(request.model, self.config)
        name = SERVICE_NAMES.get(privacy.service, privacy.service)
        if privacy.sealed:
            words = f"End-to-end encrypted ({name})"
            if completed is not None and completed.sealed:
                words += ", the reply opened with the attested key"
            return words
        if privacy.private:
            return f"TEE ({name}): over HTTPS to an enclave it attests separately"
        return plain_protection(self.endpoint)

    def _sent(self, request: ChatRequest) -> Iterator[StreamEvent]:
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


def _counts(completed: StreamCompleted | None) -> dict[str, Any]:
    """A reply's tokens and its stated cost, for the call log."""
    if completed is None:
        return {}
    usage = completed.usage
    return {
        "tokens_in": usage.prompt_tokens,
        "tokens_out": usage.completion_tokens,
        "cache_read": usage.cache_read_tokens,
        "cache_written": usage.cache_creation_tokens,
        "cost": usage.cost if usage.cost_reported else None,
    }
