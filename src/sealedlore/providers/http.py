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


def make_client(base_url: str, **kwargs: Any) -> httpx.Client:
    """An httpx client for `base_url`: no proxies for a local endpoint, and
    the redirect guard. `kwargs` go to `httpx.Client` as they are."""
    kwargs.setdefault("trust_env", not is_local(base_url))
    hooks = dict(kwargs.pop("event_hooks", None) or {})
    hooks["response"] = [*hooks.get("response", ()), refuse_insecure_redirect]
    return httpx.Client(event_hooks=hooks, **kwargs)
