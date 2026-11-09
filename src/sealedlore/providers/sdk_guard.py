"""Tinfoil's SDK fetches over https only: no redirect off https, and none from the internet
into this computer or its network.

The SDK (tinfoil, pinned) fetches with bare requests.get and requests.post: the router's
latest release and its Sigstore bundle from GitHub's proxy (tinfoil.github), AMD's VCEK and
Intel's collateral (attestation.verify_sev, .collateral_tdx), an enclave's attestation
(attestation.attestation, .bundle). requests follows a redirect anywhere, https to plain http
included, so a 30x from any of them, or from whatever answered at their address, sent the next
fetch in the clear, through no check of the app's. What the SDK fetches is signature-checked,
so what that cost was what a fetch in the clear tells the network (which enclave, when), and
plain http where the app promises https only.

guard() hands each of the SDK's modules a stand-in for requests whose functions send through a
Session that judges every redirect before following it: only to https, and never from a server
out on the internet into this computer or its network. Session is that session's class, and the
rest is requests' own. The SDK is not changed on disk, and requests itself is untouched for
anything else. A function taken from requests by name is replaced too, and every module of the
package is loaded first, so that one another version imports only when it needs it is not
missed.

The same module in each of the author's apps that use the SDK (DA Vibe Manager, DA Private
Coder, DA Toolkit, SealedLore), each passing its own reading of "this computer or its network";
DA Assistant's sealed.guard_sdk_requests (Nov 2026) is the original, holding redirects to its
netguard instead.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
import types
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlparse

# What a module may have taken from requests by name, as well as requests itself.
_REQUESTS_NAMES = ("request", "get", "options", "head", "post", "put", "patch", "delete", "Session")


def redirect_problem(source: str, target: str, is_local: Callable[[str], bool]) -> str | None:
    """Why the SDK may not follow a redirect from `source` to `target`, or None."""
    shown = str(target)[:120]
    try:
        aimed, came = urlparse(target), urlparse(source)
        aimed_host, came_host = aimed.hostname or "", came.hostname or ""
    except ValueError:
        return f"the attestation was sent on to {shown!r}, which isn't an address; not followed"
    if aimed.scheme.lower() != "https":
        return (
            f"the attestation was sent on to {shown!r}, which isn't https; not followed: "
            "nothing of it goes over plain http"
        )
    if is_local(aimed_host) and not is_local(came_host):
        return (
            f"a server on the internet sent the attestation on to {shown!r}, on this computer "
            "or its network; not followed"
        )
    return None


def _session_class(is_local: Callable[[str], bool]) -> type:
    import requests

    class RedirectRefused(requests.exceptions.InvalidURL):
        """A redirect the SDK may not follow: a requests exception, so the SDK and its
        callers already handle it as a failed fetch, and the attestation fails."""

    class GuardedSession(requests.Session):
        refused = RedirectRefused

        def get_redirect_target(self, response: Any) -> str | None:
            target = super().get_redirect_target(response)
            if target:
                problem = redirect_problem(response.url, urljoin(response.url, target), is_local)
                if problem:
                    raise RedirectRefused(problem, response=response)
            return target

    return GuardedSession


class _GuardedRequests(types.ModuleType):
    """requests, as the SDK is given it: its functions each send through a GuardedSession, and
    Session is that class; everything else (exceptions, Response, the rest) is requests' own."""

    def __init__(self, real: types.ModuleType, session_class: type):
        super().__init__(real.__name__, real.__doc__)
        self._real = real
        self.Session = session_class

    def __getattr__(self, name: str) -> Any:
        if name == "_real":
            raise AttributeError(name)
        return getattr(self._real, name)

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        with self.Session() as session:
            return session.request(method=method, url=url, **kwargs)

    def get(self, url: str, params: Any = None, **kwargs: Any) -> Any:
        return self.request("get", url, params=params, **kwargs)

    def options(self, url: str, **kwargs: Any) -> Any:
        return self.request("options", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("allow_redirects", False)
        return self.request("head", url, **kwargs)

    def post(self, url: str, data: Any = None, json: Any = None, **kwargs: Any) -> Any:
        return self.request("post", url, data=data, json=json, **kwargs)

    def put(self, url: str, data: Any = None, **kwargs: Any) -> Any:
        return self.request("put", url, data=data, **kwargs)

    def patch(self, url: str, data: Any = None, **kwargs: Any) -> Any:
        return self.request("patch", url, data=data, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Any:
        return self.request("delete", url, **kwargs)


def guard(is_local: Callable[[str], bool], package: str = "tinfoil") -> list[str]:
    """Have the SDK's modules fetch through a GuardedSession. Returns the names of the modules
    changed (none on a second call: a module already guarded is left as it is).

    Call it after importing the SDK and before it fetches anything (before attesting). Every
    module of the package is loaded first; one that can't be loaded is left, as the SDK could
    not use it either."""
    import requests

    root = sys.modules.get(package)
    if root is None:
        return []
    try:
        for found in pkgutil.walk_packages(
            getattr(root, "__path__", []), package + ".", onerror=lambda _name: None
        ):
            if found.name not in sys.modules:
                try:
                    importlib.import_module(found.name)
                except Exception:
                    pass
    except Exception:  # a package that can't be walked: what is loaded is still guarded below
        pass
    stand_in: _GuardedRequests | None = None
    changed: list[str] = []
    for name, module in list(sys.modules.items()):
        if module is None or not (name == package or name.startswith(package + ".")):
            continue
        touched = False
        if getattr(module, "requests", None) is requests:
            stand_in = stand_in or _GuardedRequests(requests, _session_class(is_local))
            module.requests = stand_in
            touched = True
        for attribute in _REQUESTS_NAMES:
            if getattr(module, attribute, None) is getattr(requests, attribute):
                stand_in = stand_in or _GuardedRequests(requests, _session_class(is_local))
                setattr(module, attribute, getattr(stand_in, attribute))
                touched = True
        if touched:
            changed.append(name)
    return changed
