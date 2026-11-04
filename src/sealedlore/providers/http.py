"""One place to build an httpx client, so every endpoint gets the same care.

Two things every client here needs and none should have to remember:

- **A local server stays local.** httpx honours `HTTP_PROXY` and friends by
  default (`trust_env`), which would route "http://localhost:11434" through a
  proxy in clear text unless `NO_PROXY` happened to say otherwise. A local
  endpoint (this computer or the local network) is built with
  `trust_env=False`; anything else keeps the
  environment's proxies and certificate settings.
- **A redirect never downgrades.** A 307/308 re-sends the whole body — a
  prompt, or every reference picture — to wherever `Location` points. The
  response hook refuses one whose target isn't https (or http to a local server),
  whether or not the client follows redirects.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from sealedlore.models.config import is_local_host
from sealedlore.providers.base import ProviderError


def is_local(url: str) -> bool:
    """A server on this computer or the local network (`is_local_host`)."""
    return is_local_host(urlparse(url).hostname or "")


def is_secure(url: str) -> bool:
    """https anywhere, or http on this computer or the local network: the same
    rule as `require_https`."""
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and is_local_host(parsed.hostname or "")


def refuse_insecure_redirect(response: httpx.Response) -> None:
    """A response hook: a redirect to anywhere the app wouldn't send to directly."""
    if not response.is_redirect:
        return
    target = response.next_request.url if response.next_request is not None else None
    if target is None:
        target = response.headers.get("location")
    if target is not None and not is_secure(str(target)):
        raise ProviderError(
            f"{response.request.url.host} redirected to {str(target)[:120]!r}; refusing to "
            "send over an insecure connection"
        )


def make_client(base_url: str, **kwargs: Any) -> httpx.Client:
    """An httpx client for `base_url`: no proxies for a local endpoint, and
    the redirect guard. `kwargs` go to `httpx.Client` as they are."""
    kwargs.setdefault("trust_env", not is_local(base_url))
    hooks = dict(kwargs.pop("event_hooks", None) or {})
    hooks["response"] = [*hooks.get("response", ()), refuse_insecure_redirect]
    return httpx.Client(event_hooks=hooks, **kwargs)
