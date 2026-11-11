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

The SDK checks the router's release with sigstore-python, which fetches too (guard_sigstore, done
by guard() for the SDK). Its trust root comes from Sigstore's TUF repository through TUF's own
client (tuf.ngclient), whose fetcher asks urllib3, and urllib3 follows a redirect anywhere, https
to plain http included: sigstore's TrustUpdater is given a fetcher of TUF's own kind that asks
urllib3 to follow none, and follows one itself only as the SDK's other fetches may, a few at most.
One it may not follow is a TUF download error, so the trust root's refresh fails, and with it the
attestation: nothing is sent. sigstore's own users of requests (Rekor, Fulcio, a timestamp
authority, OIDC: none of them fetch while an attestation is verified, all of them while signing)
get the same stand-in as the SDK's. These rest on sigstore's and TUF's internals (sigstore 4.5,
tuf 7.0, Nov 2026): where they aren't as this expects (another version), guard_sigstore raises
Unguarded rather than leave a fetch unguarded, and the attestation is refused.

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
# Most redirects one fetch of sigstore's trust root follows (each judged by redirect_problem)
TUF_MAX_REDIRECTS = 5
# sigstore's modules never loaded to be guarded: its command line, which the SDK never runs
_SIGSTORE_SKIPPED = ("sigstore.__main__", "sigstore._cli")


class Unguarded(RuntimeError):
    """The SDK's fetches can't be held to the redirect rule (sigstore's or TUF's internals aren't
    where this expects): nothing may be attested, and so nothing sent."""


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
    """Have the SDK's modules fetch through a GuardedSession, and (for the SDK, tinfoil) what it
    verifies with too (guard_sigstore). Returns the names of the modules changed (none on a second
    call: a module already guarded is left as it is). Raises Unguarded when sigstore's fetches
    can't be guarded.

    Call it after importing the SDK and before it fetches anything (before attesting). Every
    module of the package is loaded first; one that can't be loaded is left, as the SDK could
    not use it either."""
    root = sys.modules.get(package)
    if root is None:
        return []
    changed = _guard_requests(is_local, root, package)
    if package == "tinfoil":
        changed += guard_sigstore(is_local)
    return changed


def guard_sigstore(is_local: Callable[[str], bool]) -> list[str]:
    """sigstore-python, as the SDK verifies with it: its users of requests given the stand-in,
    and its TUF client the guarded fetcher (_guard_tuf). Nothing, where sigstore isn't installed:
    then nothing fetches through it. Raises Unguarded when its TUF client isn't as expected."""
    try:
        root = importlib.import_module("sigstore")
    except ImportError:
        return []
    return _guard_requests(is_local, root, "sigstore", skipped=_SIGSTORE_SKIPPED) + _guard_tuf(is_local)


def _guard_requests(
    is_local: Callable[[str], bool], root: types.ModuleType, package: str, skipped: tuple = ()
) -> list[str]:
    """`package`'s modules, every one loaded first (but `skipped`), given the requests stand-in."""
    import requests

    try:
        for found in pkgutil.walk_packages(
            getattr(root, "__path__", []), package + ".", onerror=lambda _name: None
        ):
            if found.name not in sys.modules and found.name not in skipped:
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


def _fetcher_class(is_local: Callable[[str], bool]) -> type:
    """TUF's own urllib3 fetcher (its proxies from the environment, its User-Agent: sigstore's), asking
    urllib3 to follow no redirect, and following one itself only where redirect_problem allows, at most
    TUF_MAX_REDIRECTS. One it may not follow is a DownloadError, as TUF expects of a failed fetch."""
    import urllib3
    from tuf.api import exceptions
    from tuf.ngclient import Urllib3Fetcher

    class GuardedFetcher(Urllib3Fetcher):
        def _fetch(self, url: str) -> Any:
            for _hop in range(TUF_MAX_REDIRECTS + 1):
                try:
                    response = self._proxy_env.request(
                        "GET", url, preload_content=False, timeout=urllib3.Timeout(self.socket_timeout),
                        redirect=False,
                    )
                except urllib3.exceptions.MaxRetryError as e:
                    if isinstance(e.reason, urllib3.exceptions.TimeoutError):
                        raise exceptions.SlowRetrievalError from e
                    raise
                if 300 <= response.status < 400:
                    location = response.get_redirect_location()
                    response.drain_conn()
                    response.release_conn()
                    if not location:
                        raise exceptions.DownloadHTTPError(
                            f"HTTP {response.status} with no address to go to", response.status
                        )
                    target = urljoin(url, location)
                    problem = redirect_problem(url, target, is_local)
                    if problem:
                        raise exceptions.DownloadError(problem)
                    url = target
                    continue
                if response.status >= 400:
                    response.close()
                    raise exceptions.DownloadHTTPError(
                        f"HTTP error occurred with status {response.status}", response.status
                    )
                return self._chunks(response)
            raise exceptions.DownloadError(
                f"Sigstore's trust root was sent on more than {TUF_MAX_REDIRECTS} times; not followed"
            )

    return GuardedFetcher


def _guard_tuf(is_local: Callable[[str], bool]) -> list[str]:
    """sigstore's TrustUpdater builds tuf.ngclient.Updater, by the name its module imported, with no
    fetcher, so TUF's own follows any redirect: that name becomes an Updater that is given the guarded
    fetcher when none is. Raises Unguarded where sigstore's or TUF's internals aren't as expected."""
    import inspect

    try:
        place = importlib.import_module("sigstore._internal.tuf")
        from tuf.api import exceptions
        from tuf.ngclient import Updater, Urllib3Fetcher
    except Exception as e:
        raise Unguarded(f"sigstore's TUF client couldn't be found ({type(e).__name__}: {e})") from e
    current = getattr(place, "Updater", None)
    if getattr(current, "fetches_guarded", False):
        return []
    problems = []
    if current is not Updater:
        problems.append("sigstore._internal.tuf.Updater isn't tuf.ngclient.Updater")
    if not callable(getattr(place, "TrustUpdater", None)):
        problems.append("sigstore._internal.tuf has no TrustUpdater")
    try:
        names = list(inspect.signature(Updater.__init__).parameters)
    except (TypeError, ValueError):
        names = []
    if names[1:7] != ["metadata_dir", "metadata_base_url", "target_dir", "target_base_url", "fetcher", "config"]:
        problems.append("tuf.ngclient.Updater doesn't take (metadata_dir, …, fetcher, config) as expected")
    proxies = getattr(Urllib3Fetcher(), "_proxy_env", None)
    if not callable(getattr(proxies, "request", None)) or not callable(getattr(Urllib3Fetcher, "_chunks", None)):
        problems.append("tuf.ngclient.Urllib3Fetcher has no _proxy_env.request or _chunks")
    if not (isinstance(exceptions.DownloadError, type) and isinstance(exceptions.DownloadHTTPError, type)):
        problems.append("tuf.api.exceptions has no DownloadError or DownloadHTTPError")
    if problems:
        raise Unguarded(
            "Sigstore's trust root can't be fetched with redirects held to https, so nothing is attested: "
            + "; ".join(problems)
        )
    fetcher_class = _fetcher_class(is_local)

    class GuardedUpdater(Updater):
        """tuf.ngclient.Updater, given the guarded fetcher when no fetcher is given."""

        fetches_guarded = True

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            if len(args) < 5 and kwargs.get("fetcher") is None:
                config = kwargs.get("config") if len(args) < 6 else args[5]
                kwargs["fetcher"] = fetcher_class(app_user_agent=getattr(config, "app_user_agent", None))
            super().__init__(*args, **kwargs)

    place.Updater = GuardedUpdater
    return [place.__name__]
