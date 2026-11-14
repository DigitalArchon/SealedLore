"""The HTTP side shared by picture and video endpoints: requests that Stop
reaches at once, and retries that never pay twice.

Stop works as it does for text (`openai_compat`): the socket of the request
in flight is captured through httpcore's trace hook as its connection opens,
and `cancel()` shuts it down, which wakes a thread blocked reading it. So the
client keeps no idle connections.

Retries: a GET (a listing, a job's status, a download) is retried after a
rate limit or a momentary outage, as text requests are. A request that
*makes* something is retried only after a 429, which says it was refused
before anything was made; after a 502 or 503 it may have been accepted, and
a video sent twice is paid for twice. A 429 that says to wait longer than
`RETRY_MAX_SECONDS` is a spending cap (nano-gpt's per-key daily limit
answers 429 with Retry-After until midnight UTC), so it is not waited out:
it is `SpendingCapReached`, and the author is told.

Redirects: only a request that carries neither the key nor a body (a
download from a signed storage link) follows one, under `make_client`'s
guard. A 307 or 308 re-sends a request's body, and for a picture or a video
that is the prompt and every reference picture: followed, any https host the
endpoint named got them. One that isn't followed is said, and where it
pointed, never taken for a reply with nothing in it (DA Media Browser's
`follows_redirects`, Nov 2026).
"""

from __future__ import annotations

import socket
import threading
from typing import Any
from urllib.parse import urlparse

import httpx

from sealedlore.models.config import _same_host, shown
from sealedlore.providers.base import ProviderError, StreamCancelled, checked_key
from sealedlore.providers.http import make_client, reply_json, traced_socket
from sealedlore.providers.privacy import MEDIA_WAY_OUT, plain_refusal
from sealedlore.providers.streaming import (
    RETRY_ATTEMPTS,
    RETRY_MAX_SECONDS,
    _retry_delay,
    _seconds,
    _shut_down,
    read_body,
    read_start,
)

# A reply past this is refused, with nothing more read: no picture or video
# comes near it (a picture is a few megabytes, several sent inline as base64
# a few tens; a generated video clip tens of megabytes), and a download is
# held in memory whole before it is stored.
MEDIA_MAX_BYTES = 256 * 1024 * 1024
# Headers about the bytes as they came, which no longer describe them once
# read and decoded.
_REREAD_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})


class SpendingCapReached(ProviderError):
    """The key's spending limit is reached; waiting a few seconds won't help."""


class MediaTimedOut(ProviderError):
    """Nothing came back within the time allowed. For a request that makes
    something, the service may still make it, and charge for it."""


def spending_cap(error: ProviderError) -> bool:
    return error.status_code == 429 and (error.retry_after or 0) > RETRY_MAX_SECONDS


def _cap_message(error: ProviderError) -> str:
    hours = (error.retry_after or 0) / 3600
    when = f" It resets in about {hours:.0f} hours." if hours >= 1 else ""
    return (
        "The endpoint refused: this key has reached its spending limit (HTTP 429)."
        f"{when} Raise the limit in the service's dashboard, or wait."
    )


class MediaHttp:
    """One job's HTTP: cancellable, with retries that fit what is asked."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: float,
        client: httpx.Client | None = None,
        key_withheld: str = "",
        sealed_api: str = "auto",
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        # Why the endpoint's key isn't sent (`ResolvedMedia.key_withheld`): a
        # request that would carry it refuses in these words.
        self.key_withheld = key_withheld
        # The sealed protocol an endpoint at this address speaks
        # (`ResolvedMedia.sealed_api`): nothing goes there in the clear.
        self.sealed_api = sealed_api
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._owns_client = client is None
        self._lock = threading.Lock()
        self._cancelled = False
        self._socket: socket.socket | None = None
        self._wake = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = make_client(
                self.base_url,
                timeout=httpx.Timeout(self.timeout_seconds, connect=30.0),
                # A fresh connection per request, so cancel() can find its socket.
                limits=httpx.Limits(max_keepalive_connections=0),
                # Each request says whether it follows one (`follows_redirects`).
                follow_redirects=False,
            )
        return self._client

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        if self.key_withheld:
            raise ProviderError(self.key_withheld)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            whose = urlparse(self.base_url).hostname or "this endpoint"
            headers["Authorization"] = f"Bearer {checked_key(self.api_key, whose)}"
        headers.update(extra or {})
        return headers

    def _trace(self, event_name: str, info: dict[str, Any]) -> None:
        sock = traced_socket(event_name, info)
        if sock is None:
            return
        with self._lock:
            self._socket = sock
            cancelled = self._cancelled
        if cancelled:
            _shut_down(sock)

    def cancel(self) -> None:
        """Stop waiting. What was already asked for may still be made, and billed."""
        with self._lock:
            self._cancelled = True
            sock = self._socket
        self._wake.set()
        _shut_down(sock)

    def wait(self, seconds: float) -> None:
        """Sleep between polls; a cancel ends it with StreamCancelled."""
        if self._wake.wait(seconds) or self._cancelled:
            raise StreamCancelled()

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def request(
        self,
        method: str,
        url: str,
        *,
        makes: bool = False,
        authorised: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send and return a successful response; ProviderError otherwise.

        `makes`: the request creates something that is paid for, so it is
        retried only after a 429 (see the module docstring). `authorised`:
        False for a download from a storage host, which must never be handed
        the key."""
        for attempt in range(RETRY_ATTEMPTS + 1):
            try:
                return self._once(method, url, authorised=authorised, **kwargs)
            except ProviderError as exc:
                if spending_cap(exc):
                    raise SpendingCapReached(
                        _cap_message(exc), status_code=429, body=exc.body
                    ) from exc
                delay = _retry_delay(exc)
                if makes and exc.status_code != 429:
                    delay = None
                if delay is None or attempt == RETRY_ATTEMPTS:
                    raise
            self.wait(delay)
        raise AssertionError("unreachable")

    def _once(self, method: str, url: str, *, authorised: bool, **kwargs: Any) -> httpx.Response:
        if self._cancelled:
            raise StreamCancelled()
        # Never to an end-to-end encrypted endpoint (providers/privacy.py): a
        # picture's prompt and references would go to it in the clear.
        refusal = plain_refusal(url, api=self.sealed_api, way_out=MEDIA_WAY_OUT)
        if refusal:
            raise ProviderError(refusal)
        headers = kwargs.pop("headers", None)
        if headers is None:
            headers = self.headers() if authorised else {}
        if any(name.lower() == "authorization" for name in headers) and not _same_host(
            url, self.base_url
        ):
            # The key is for this endpoint alone (scheme, host and port): an
            # address from a reply or a stored record that points elsewhere,
            # or at plain http, never gets it.
            raise ProviderError(
                f"{_where(url)} isn't this endpoint's own address, so the key wasn't sent there"
            )
        follow = follows_redirects(headers, kwargs)
        try:
            with self._get_client().stream(
                method,
                url,
                headers=headers,
                extensions={"trace": self._trace},
                follow_redirects=follow,
                **kwargs,
            ) as streamed:
                # Read a piece at a time, and only so far: read whole, a reply
                # (or a refusal) could be as large as the other end liked.
                failed = streamed.status_code >= 400
                try:
                    content = (
                        read_start(streamed) if failed else read_body(streamed, MEDIA_MAX_BYTES)
                    )
                except ProviderError as exc:
                    raise ProviderError(f"{_where(url)}: {exc}") from None
                response = httpx.Response(
                    streamed.status_code,
                    # Already decoded: the rebuilt reply mustn't decode it again.
                    headers=[
                        (name, value)
                        for name, value in streamed.headers.multi_items()
                        if name.lower() not in _REREAD_HEADERS
                    ],
                    content=content,
                    request=streamed.request,
                )
        except (httpx.HTTPError, OSError) as exc:
            if self._cancelled:
                raise StreamCancelled() from exc
            if isinstance(exc, httpx.TimeoutException) and not isinstance(
                exc, httpx.ConnectTimeout
            ):
                raise MediaTimedOut(
                    f"{_where(url)} sent no reply within {_minutes(self.timeout_seconds)}"
                ) from exc
            raise ProviderError(f"{_where(url)} failed: {exc or type(exc).__name__}") from exc
        if self._cancelled:
            raise StreamCancelled()
        if 300 <= response.status_code < 400:
            raise _redirected(url, response, followed=follow)
        if response.status_code >= 400:
            error = ProviderError(
                f"{_where(url)} returned HTTP {response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
                body=response.text,
            )
            error.retry_after = _seconds(response.headers.get("retry-after"))
            raise error
        return response

    def json(self, method: str, url: str, **kwargs: Any) -> Any:
        response = self.request(method, url, **kwargs)
        try:
            return reply_json(response)
        except ValueError as exc:
            raise ProviderError(
                f"{_where(url)} returned a non-JSON body", body=response.text[:2000]
            ) from exc


# What a request carries that a redirect would hand on to wherever it points.
_KEY_HEADERS = frozenset({"authorization", "x-api-key"})
# httpx drops Authorization when a redirect leaves the origin; any other
# header carrying the key it would send on.
_DROPPED_ELSEWHERE = frozenset({"authorization"})
_BODY_FIELDS = ("json", "content", "data", "files")


def follows_redirects(headers: dict[str, str], kwargs: dict[str, Any]) -> bool:
    """Whether a redirect of this request may be followed at all: never with a
    body (a 307/308 sends it on, the prompt and pictures with it). A download
    may, where `make_client`'s guard lets it (a signed link's storage often
    redirects), with the key only where httpx leaves it at its own origin:
    in Authorization, never in a header it would carry elsewhere."""
    if any(kwargs.get(field) is not None for field in _BODY_FIELDS):
        return False
    return not any(name.lower() in _KEY_HEADERS - _DROPPED_ELSEWHERE for name in headers)


def _redirected(url: str, response: httpx.Response, *, followed: bool) -> ProviderError:
    """A 3xx at the end of a request, as the error to raise: a redirect not
    followed, and where it pointed, or one with nowhere to go."""
    code = response.status_code
    location = response.headers.get("location")
    if followed or not location:
        return ProviderError(
            f"{_where(url)} answered HTTP {code} with nothing in it", status_code=code
        )
    try:
        target = str(response.request.url.join(location))
    except httpx.InvalidURL:
        target = location
    return ProviderError(
        f"{_where(url)} redirected (HTTP {code}) to {shown(_where(target))[:200]}. Nothing "
        "was sent there: a request that carries the key, the prompt or a picture is never "
        "sent on to another address. If the endpoint has moved, put its new address in "
        "Settings.",
        status_code=code,
    )


def _minutes(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:.0f} seconds"
    return f"{seconds / 60:.0f} minutes"


def _where(url: str) -> str:
    """A URL for a message: no query, which may carry a signature."""
    return url.split("?", 1)[0]
