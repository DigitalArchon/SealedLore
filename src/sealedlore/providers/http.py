"""One place to build an httpx client, so every endpoint gets the same care.

Three things every client here needs and none should have to remember:

- **A local server stays local.** httpx honours `HTTP_PROXY` and friends by
  default (`trust_env`), which would route "http://localhost:11434" through a
  proxy in clear text unless `NO_PROXY` happened to say otherwise. A local
  endpoint (this computer or the local network) is built with
  `trust_env=False`; anything else keeps the
  environment's proxies and certificate settings.
- **A redirect never downgrades.** A 307/308 re-sends the whole body — a
  prompt, or every reference picture — to wherever `Location` points. The
  response hook refuses one whose target isn't https (or http to a local server),
  whether or not the client follows redirects, and one from a server on the
  internet into this computer or the local network.
- **A transport of its own still goes through the proxy.** httpx takes no
  proxy from the environment for a client given its own transport, so such a
  client is told the one the environment gives its address
  (`environment_proxy`), by the same rule as everything else.
- **What comes back is read only so far.** Only gzip and deflate are asked
  for and read (`ACCEPT_ENCODING`, `refuse_unbounded_encoding`), and a reply
  read whole is read to `REPLY_MAX_BYTES` at most, decoded bytes counted
  (`_Client`). (The DA apps' standard, Nov 14 2026.)
"""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.request
from typing import Any
from urllib.parse import urlparse

import httpx

from sealedlore.models.config import is_local_host
from sealedlore.providers.base import ProviderError
from sealedlore.storage.atomic import outside_json

# The httpcore trace events that complete with the stream a request will be
# read from, whatever opened it: "connection." for a direct connection,
# "proxy." for TLS through an HTTP proxy's tunnel, "socks." through SOCKS.
_SOCKET_STEPS = (".connect_tcp.complete", ".start_tls.complete")


def traced_socket(event_name: str, info: dict[str, Any]) -> socket.socket | None:
    """For httpcore's `trace` extension: the socket the request in flight is
    read from, once a connect or TLS step has completed, so a Stop can shut
    it down (a thread blocked reading a socket wakes only then). The latest
    step's supersedes the one before: TLS wraps the TCP socket and detaches
    it. Only "connection." steps were caught once, and behind HTTPS_PROXY the
    TLS socket came from the proxy's own step: Stop shut down the detached
    TCP socket (a swallowed EBADF) and waited for the next chunk."""
    if not event_name.endswith(_SOCKET_STEPS):
        return None
    stream = info.get("return_value")
    extra = getattr(stream, "get_extra_info", None)
    sock = extra("socket") if callable(extra) else None
    return sock if isinstance(sock, socket.socket) else None


def is_local(url: str) -> bool:
    """A server on this computer or the local network (`is_local_host`)."""
    return is_local_host(urlparse(url).hostname or "")


def environment_proxy(url: str) -> str | None:
    """The proxy the environment gives `url` (HTTPS_PROXY, HTTP_PROXY or
    ALL_PROXY, less what NO_PROXY names), read as httpx reads them for a
    client `make_client` builds; None for a server on this computer or the
    local network, which is never reached through one. For a client given its
    own transport, which httpx gives no proxy: the end-to-end encrypted
    request alone of all the app's went direct, from this machine's own
    address, beside a preflight that had gone through the proxy (DA
    Assistant's and DA Media Browser's, Nov 2026)."""
    target = httpx.URL(url)
    if is_local_host(target.host):
        return None
    proxies = urllib.request.getproxies()
    for entry in (proxies.get("no") or "").split(","):
        entry = entry.strip()
        if entry == "*":
            return None
        if entry and _passes_by(entry, target):
            return None
    proxy = proxies.get(target.scheme) or proxies.get("all")
    if not proxy:
        return None
    return proxy if "://" in proxy else f"http://{proxy}"


def _passes_by(entry: str, target: httpx.URL) -> bool:
    """Whether one NO_PROXY entry keeps `target` off the proxy, as httpx reads
    it (curl's rules): "example.com" is it and its subdomains, ".example.com"
    its subdomains alone, an address or "localhost" that host alone, and
    "scheme://host:port" that scheme, host and port. One that can't be read
    keeps nothing off the proxy."""
    if "://" in entry:
        pattern = entry
    elif _is_address(entry, ipaddress.IPv4Address):
        pattern = f"all://{entry}"
    elif _is_address(entry, ipaddress.IPv6Address):
        pattern = f"all://[{entry}]"
    elif entry.lower() == "localhost":
        pattern = f"all://{entry}"
    else:
        pattern = f"all://*{entry}"
    try:
        named = httpx.URL(pattern)
    except httpx.InvalidURL:
        return False
    if named.scheme != "all" and named.scheme != target.scheme:
        return False
    host = named.host
    if host.startswith("*."):
        matched = re.fullmatch(rf".+\.{re.escape(host[2:])}", target.host)
    elif host.startswith("*") and host != "*":
        matched = re.fullmatch(rf"(.+\.)?{re.escape(host[1:])}", target.host)
    else:
        matched = host in ("", "*") or host == target.host
    return bool(matched) and (named.port is None or named.port == target.port)


def _is_address(entry: str, kind: type[ipaddress.IPv4Address | ipaddress.IPv6Address]) -> bool:
    try:
        kind(entry.split("/")[0])
    except ValueError:
        return False
    return True


def is_secure(url: str) -> bool:
    """https anywhere, or http on this computer or the local network: the same
    rule as `require_https`."""
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and is_local_host(parsed.hostname or "")


def refuse_insecure_redirect(response: httpx.Response) -> None:
    """A response hook: a redirect to anywhere the app wouldn't send to directly,
    or from a server out on the internet into this computer or the local
    network (a card's link must not reach the router's admin page)."""
    if not response.is_redirect:
        return
    location = response.headers.get("location")
    if location is None:
        return
    # httpx runs the hooks before it builds the next request, so `Location` is
    # read here as written, and a relative one ("/c/2") is resolved the way the
    # client will resolve it.
    try:
        target = str(response.request.url.join(location))
    except httpx.InvalidURL:
        target = location
    source = str(response.request.url)
    if not is_secure(target):
        raise ProviderError(
            f"{response.request.url.host} redirected to {target[:120]!r}; refusing to "
            "send over an insecure connection"
        )
    if is_local(target) and not is_local(source):
        raise ProviderError(
            f"{response.request.url.host} redirected to {target[:120]!r}, a server on this "
            "computer or your local network; refused"
        )


def reply_json(response: httpx.Response) -> Any:
    """A reply's body as JSON, held to the depth limit (`outside_json`), in
    place of `response.json()`, which reads any depth: past it, a
    `json.JSONDecodeError` like any body that isn't JSON."""
    return outside_json(response.content)


# What a reply may be compressed as, asked for on every request and the only
# ones read: gzip and deflate expand at most about a thousandfold, so a piece
# of a reply decoded is a few dozen megabytes at worst before a cap counting
# decoded bytes sees it. Brotli's and zstd's expansion has no such bound, and
# httpx asks for and decodes them whenever their packages are installed, which
# a source install may well have.
ACCEPT_ENCODING = "gzip, deflate"
# What httpx would decode: one of gzip or deflate, once, is read; brotli,
# zstd, or any two stacked (each layer multiplies) are refused. A name httpx
# doesn't know it passes through as it is, and so does this.
_DECODED = frozenset({"gzip", "deflate", "br", "zstd"})
_READ = frozenset({"gzip", "deflate"})
# A reply read whole (a listing, a JSON answer, an attestation) is read this
# far and no further, its decoded bytes counted: a stream is read a line at a
# time (streaming.read_lines), a picture or a video to its own limit
# (media_http.MEDIA_MAX_BYTES).
REPLY_MAX_BYTES = 32 * 1024 * 1024
# A refusal's body (HTTP 400 and up), as far as it is ever read: any
# endpoint's own words fit. Read whole, it could be as large as the endpoint
# liked; the rest is left unread (streaming.read_start reads a stream's so).
ERROR_BODY_BYTES = 64 * 1024


class UnreadEncoding(httpx.DecodingError):
    """A reply compressed some way that isn't read (`refuse_unbounded_encoding`)."""


class ReplyTooLarge(httpx.DecodingError):
    """A reply read whole that ran past REPLY_MAX_BYTES, decoded."""


def refuse_unbounded_encoding(response: httpx.Response) -> None:
    """A response hook: a body compressed as anything but gzip or deflate,
    once, is refused before a byte of it is read or decoded."""
    coded = [
        name
        for name in (
            part.strip().lower()
            for part in response.headers.get_list("content-encoding", split_commas=True)
        )
        if name in _DECODED
    ]
    if len(coded) > 1 or any(name not in _READ for name in coded):
        raise UnreadEncoding(
            f"{response.request.url.host} sent its reply compressed as {', '.join(coded)}, "
            "which isn't read: only gzip or deflate, once",
            request=response.request,
        )


class _Client(httpx.Client):
    """`make_client`'s client: a reply read whole (`get`, `post`, anything
    but `stream`) is read a piece at a time to REPLY_MAX_BYTES, decoded bytes
    counted, and refused past it, with nothing more read; a refusal's body
    (400 and up) only to ERROR_BODY_BYTES, its start kept and the rest left
    unread. httpx would read either all into memory first, however large."""

    def send(
        self, request: httpx.Request, *, stream: bool = False, **kwargs: Any
    ) -> httpx.Response:
        response = super().send(request, stream=True, **kwargs)
        if stream:
            return response
        refused = response.status_code >= 400
        try:
            declared = response.headers.get("content-length", "")
            if (
                not refused
                and request.method != "HEAD"  # whose length is of a body never sent
                and declared.isascii()
                and declared.isdigit()
                and int(declared) > REPLY_MAX_BYTES
            ):
                raise ReplyTooLarge(_too_large(request), request=request)
            body = bytearray()
            for piece in response.iter_bytes():
                body += piece
                if refused and len(body) >= ERROR_BODY_BYTES:
                    del body[ERROR_BODY_BYTES:]
                    break  # its words as far as they go; the rest unread
                if len(body) > REPLY_MAX_BYTES:
                    raise ReplyTooLarge(_too_large(request), request=request)
        except BaseException:
            response.close()
            raise
        if refused:
            response.close()
        # As httpx's own `read` keeps it: what `content`, `text` and `json`
        # give (tests/test_reply_limits.py holds this to the httpx in use).
        response._content = bytes(body)
        return response


def _too_large(request: httpx.Request) -> str:
    return f"{request.url.host} sent more than {REPLY_MAX_BYTES // (1024 * 1024)} MB; not read"


def make_client(base_url: str, **kwargs: Any) -> httpx.Client:
    """An httpx client for `base_url`: no proxies for a local endpoint, the
    redirect guard, only gzip and deflate, and a reply read whole read to
    REPLY_MAX_BYTES. `kwargs` go to `httpx.Client` as they are."""
    kwargs.setdefault("trust_env", not is_local(base_url))
    headers = httpx.Headers(kwargs.pop("headers", None))
    headers.setdefault("Accept-Encoding", ACCEPT_ENCODING)
    hooks = dict(kwargs.pop("event_hooks", None) or {})
    hooks["response"] = [
        *hooks.get("response", ()),
        refuse_insecure_redirect,
        refuse_unbounded_encoding,
    ]
    return _Client(headers=headers, event_hooks=hooks, **kwargs)
