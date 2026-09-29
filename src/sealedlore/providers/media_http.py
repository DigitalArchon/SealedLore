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
"""

from __future__ import annotations

import socket
import threading
from typing import Any

import httpx

from sealedlore.providers.base import ProviderError, StreamCancelled
from sealedlore.providers.http import make_client
from sealedlore.providers.openai_compat import (
    RETRY_ATTEMPTS,
    RETRY_MAX_SECONDS,
    _retry_delay,
    _seconds,
    _shut_down,
)


class SpendingCapReached(ProviderError):
    """The key's spending limit is reached; waiting a few seconds won't help."""


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
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
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
                # A signed download URL may redirect; the guard in make_client
                # refuses one that would leave https.
                follow_redirects=True,
            )
        return self._client

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(extra or {})
        return headers

    def _trace(self, event_name: str, info: dict[str, Any]) -> None:
        if event_name not in ("connection.connect_tcp.complete", "connection.start_tls.complete"):
            return
        stream = info.get("return_value")
        sock = stream.get_extra_info("socket") if stream is not None else None
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
        headers = kwargs.pop("headers", None)
        if headers is None:
            headers = self.headers() if authorised else {}
        try:
            response = self._get_client().request(
                method, url, headers=headers, extensions={"trace": self._trace}, **kwargs
            )
        except (httpx.HTTPError, OSError) as exc:
            if self._cancelled:
                raise StreamCancelled() from exc
            raise ProviderError(f"{_where(url)} failed: {exc or type(exc).__name__}") from exc
        if self._cancelled:
            raise StreamCancelled()
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
            return response.json()
        except ValueError as exc:
            raise ProviderError(
                f"{_where(url)} returned a non-JSON body", body=response.text[:2000]
            ) from exc


def _where(url: str) -> str:
    """A URL for a message: no query, which may carry a signature."""
    return url.split("?", 1)[0]
