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

A redirect's target is held to the whole rule for an address, not to its scheme alone: one that
can't be read one way (a space, a backslash or a control character in it, a port no address has, no
host, a "%" escape in its host, which a parser may decode into another name), 0.0.0.0 (which reaches
this computer), and anything the app's own rule refuses (`check`, given by the app). What a refused
redirect raises is a RedirectNotFollowed (`redirect_refused`), whatever the library: not_followed()
finds it among the causes of what the SDK or sigstore raised, which say only "Failed to refresh TUF
metadata".

And the SDK writes nothing (guard_files): it caches the release's Sigstore bundle, AMD's VCEK
(named after the physical chip) and, for a TDX router, Intel's collateral and its hardware
measurements, under the user's cache folder (~/.cache/tinfoil), through module attributes it reads
at each use; each is pointed at a folder that can't be made, so every write fails as the SDK lets
one fail, and nothing is read back. Where those attributes aren't as this expects, Unguarded.

And what the SDK's requests read is bounded (the DA apps' security standard, Nov 2026): each asks
for gzip or deflate alone (requests asks for br and zstd too whenever brotli or zstandard is
installed, whose ratios are unbounded per chunk), a reply in any other coding is refused unread, a
reply not streamed is read to REPLY_MAX_BYTES at most, decoded, and an error's or a redirect's body
to ERROR_BODY_BYTES, its start kept and the rest unread.

The same module in each of the author's apps that use the SDK (DA Assistant, DA Media Browser,
DA Vibe Manager, DA Private Coder, DA Toolkit, SealedLore), each passing its own reading of "this
computer or its network" and its own rule for an address: SealedLore's (providers/sdk_guard.py) is
the canonical copy, which each app's tests/test_shared_code.py holds its own to. DA Assistant's
sealed.guard_sdk_requests (Nov 2026) was the original, whose stricter parts it took (Nov 14 2026).
"""

from __future__ import annotations

import importlib
import ipaddress
import os
import pkgutil
import re
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
# The codings the SDK's requests ask for, and the only ones a reply may come in: never br or zstd,
# whose ratios are unbounded per chunk
ACCEPT_ENCODING = "gzip, deflate"
_TAKEN_CODINGS = ("", "identity", "gzip", "x-gzip", "deflate")
# The most of a reply the SDK's requests read, decoded: a whole one not streamed (a release, a Sigstore
# bundle, certificates and collateral: a few hundred KB), and an error's or a redirect's body, of which
# only the start is kept
REPLY_MAX_BYTES = 32 * 1024 * 1024
ERROR_BODY_BYTES = 64 * 1024
# The most read at once: a limit and one byte more where that is less, so little past it is read
_PIECE = 1024 * 1024
# The SDK's own caches (tinfoil 0.15): (module, attribute) of each folder it writes into, read at
# each use (guard_files)
_SDK_CACHES = (
    ("tinfoil.github", "_GITHUB_CACHE_DIR"),
    ("tinfoil.attestation.verify_sev", "_VCEK_CACHE_DIR"),
    ("tinfoil.attestation.collateral_tdx", "_TDX_CACHE_DIR"),
)
# What no address has in it, and parsers may read apart ("https://host\@evil.example/")
_NOT_IN_AN_ADDRESS = re.compile(r"[\\\s\x00-\x1f\x7f]")


class Unguarded(RuntimeError):
    """The SDK's fetches can't be held to the redirect rule (sigstore's or TUF's internals aren't
    where this expects): nothing may be attested, and so nothing sent."""


class RedirectNotFollowed(Exception):
    """A base of what a redirect the guard refused raises (with a requests exception for the SDK's
    fetches, a TUF DownloadError for Sigstore's trust root), naming it as one: `redirect_refused`,
    and not_followed() finds it among the causes of what the SDK raised."""

    redirect_refused = True


def not_followed(error: BaseException | None) -> str:
    """The words of a redirect the guard refused: `error`, or the first of its causes that is one
    (sigstore says only "Failed to refresh TUF metadata", the refusal its cause), or ''."""
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, RedirectNotFollowed):
            return str(error)
        error = error.__cause__ or error.__context__
    return ""


def _unreadable(target: str) -> str | None:
    """Why `target` can't be read as one address, or isn't one to reach anything at, or None."""
    if _NOT_IN_AN_ADDRESS.search(target):
        return "an address has no spaces, backslashes or control characters in it"
    try:
        aimed = urlparse(target)
        host = aimed.hostname or ""
        _ = aimed.port  # raises on a port no address can have
    except ValueError:
        return "its host or port can't be read"
    if not host:
        return "it names no host"
    if "%" in host and ":" not in host.split("%", 1)[0]:  # only an IPv6 address has a zone
        return "a host has no % escapes in it"
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None
    if address.is_unspecified or address in ipaddress.ip_network("0.0.0.0/8"):
        return "it is the address a server listens on, which reaches this computer"
    return None


def _joined(source: str, location: str) -> str:
    """`location` read against `source`, as the redirect would go there; as it is, where it can't be
    read (redirect_problem then says so)."""
    try:
        return urljoin(source, location)
    except ValueError:
        return str(location)


def redirect_problem(
    source: str,
    target: str,
    is_local: Callable[[str], bool],
    check: Callable[[str], Any] | None = None,
) -> str | None:
    """Why the SDK may not follow a redirect from `source` to `target`, or None. `check` is the
    app's own rule for an address: whatever it raises for one (a ValueError, a URLError) is a
    refusal."""
    shown = str(target)[:120]
    unreadable = _unreadable(str(target))
    if unreadable:
        return f"the attestation was sent on to {shown!r}, which isn't an address ({unreadable}); not followed"
    aimed, came = urlparse(target), urlparse(source)
    try:
        came_host = came.hostname or ""
    except ValueError:
        came_host = ""
    if aimed.scheme.lower() != "https":
        return (
            f"the attestation was sent on to {shown!r}, which isn't https; not followed: "
            "nothing of it goes over plain http"
        )
    if check is not None:
        try:
            check(target)
        except Exception as e:  # the app's rule refused it, whatever it raises
            return f"the attestation was sent on to {shown!r}, an address refused ({str(e)[:200]}); not followed"
    if is_local(aimed.hostname or "") and not is_local(came_host):
        return (
            f"a server on the internet sent the attestation on to {shown!r}, on this computer "
            "or its network; not followed"
        )
    return None


def _read(response: Any, limit: int, cut: bool, too_large: type) -> None:
    """`response`'s body read, decoded, to `limit` bytes at most and kept as requests keeps one it
    has read: past the limit, raise `too_large` (or, `cut`, keep the start and leave the rest
    unread). One an adapter of its own has read already is held to the same."""
    host = urlparse(str(response.url or "")).hostname or "the server"
    if response._content is False and response.raw is None:  # an adapter's own, with no body
        response._content, response._content_consumed = b"", True
    if response._content is False:
        declared = str(response.headers.get("Content-Length") or "").strip()
        method = str(getattr(response.request, "method", "") or "").upper()
        if not cut and method != "HEAD" and declared.isdigit() and int(declared) > limit:
            response.close()
            raise too_large(f"{host} answered with more than {limit // 1024 // 1024} MB; not read", response=response)
        pieces, size = [], 0
        for piece in response.iter_content(min(limit + 1, _PIECE)):
            pieces.append(piece)
            size += len(piece)
            if size > limit:
                break
        body = b"".join(pieces)
        response._content, response._content_consumed = body[:limit], True
        if size > limit:
            if response.raw is not None:
                response.raw.close()  # the rest unread, the connection not kept
            response.close()
    else:
        body = response._content or b""
        response._content = body[:limit]
    if len(body) > limit and not cut:
        raise too_large(f"{host} answered with more than {limit // 1024 // 1024} MB; not read on", response=response)


class _Reading:
    """An adapter of a GuardedSession's, as the session sends through it: every request asks for
    ACCEPT_ENCODING alone, a reply in any other coding is refused unread, and a reply's body is read
    so far only: a whole one REPLY_MAX_BYTES, an error's or a redirect's ERROR_BODY_BYTES (requests
    reads a redirect's body whole before following it). A streamed reply is the caller's to read."""

    def __init__(self, adapter: Any, too_large: type, undecodable: type):
        self._adapter, self._too_large, self._undecodable = adapter, too_large, undecodable

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)

    def send(self, request: Any, stream: bool = False, **kwargs: Any) -> Any:
        request.headers["Accept-Encoding"] = ACCEPT_ENCODING
        response = self._adapter.send(request, stream=True, **kwargs)
        coding = str(response.headers.get("Content-Encoding") or "").strip().lower()
        if coding not in _TAKEN_CODINGS:
            response.close()
            host = urlparse(str(response.url or "")).hostname or "the server"
            raise self._undecodable(
                f"{host} answered in a coding not asked for ({coding[:40]!r}); not read", response=response
            )
        if 300 <= response.status_code < 400 or response.status_code >= 400:
            _read(response, ERROR_BODY_BYTES, True, self._too_large)
        elif not stream:
            _read(response, REPLY_MAX_BYTES, False, self._too_large)
        return response


def _session_class(is_local: Callable[[str], bool], check: Callable[[str], Any] | None = None) -> type:
    import requests

    class RedirectRefused(RedirectNotFollowed, requests.exceptions.InvalidURL):
        """A redirect the SDK may not follow: a requests exception, so the SDK and its
        callers already handle it as a failed fetch, and the attestation fails."""

    class TooLarge(requests.exceptions.RequestException):
        """A reply larger than any the SDK fetches: not read on, a failed fetch."""

    class Undecodable(requests.exceptions.ContentDecodingError):
        """A reply in a coding not asked for (ACCEPT_ENCODING): refused unread, a failed fetch."""

    class GuardedSession(requests.Session):
        refused, too_large, undecodable = RedirectRefused, TooLarge, Undecodable

        def get_redirect_target(self, response: Any) -> str | None:
            target = super().get_redirect_target(response)
            if target:
                problem = redirect_problem(response.url, _joined(response.url, target), is_local, check)
                if problem:
                    raise RedirectRefused(problem, response=response)
            return target

        def get_adapter(self, url: str) -> Any:
            return _Reading(super().get_adapter(url), TooLarge, Undecodable)

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


def guard(
    is_local: Callable[[str], bool], package: str = "tinfoil", check: Callable[[str], Any] | None = None
) -> list[str]:
    """Have the SDK's modules fetch through a GuardedSession, and (for the SDK, tinfoil) what it
    verifies with too (guard_sigstore). Returns the names of the modules changed (none on a second
    call: a module already guarded is left as it is). Raises Unguarded when sigstore's fetches
    can't be guarded.

    `is_local` is the app's reading of "this computer or its network", `check` its rule for an
    address it uses (SealedLore's require_https, the other apps' netguard.check): it raises for
    one the app wouldn't send to, and a redirect there isn't followed.

    Call it after importing the SDK and before it fetches anything (before attesting). Every
    module of the package is loaded first; one that can't be loaded is left, as the SDK could
    not use it either."""
    root = sys.modules.get(package)
    if root is None:
        return []
    changed = guard_files() if package == "tinfoil" else []
    changed += _guard_requests(is_local, root, package, check=check)
    if package == "tinfoil":
        changed += guard_sigstore(is_local, check)
    return changed


def nowhere() -> str:
    """A folder that can't be made, on any system: one under the null device, which is no folder."""
    return os.path.join(os.devnull, "tinfoil")


def guard_files() -> list[str]:
    """Have the SDK (tinfoil) write nothing: each of its cache folders (_SDK_CACHES) pointed at one that
    can't be made (`nowhere`), so every write it makes fails, which it lets pass (OSError, ignored), and
    nothing is read from one. Returns the modules changed (none on a second call). Raises Unguarded
    where the SDK's caches aren't where this expects (another version): then nothing is attested."""
    if "tinfoil" not in sys.modules:
        return []
    target = nowhere()
    if os.path.isdir(os.path.dirname(target)):
        raise Unguarded(f"the SDK's caches can't be kept from the disk: {os.devnull} is a folder here")
    changed: list[str] = []
    for name, attribute in _SDK_CACHES:
        try:
            module = importlib.import_module(name)
        except Exception as e:
            raise Unguarded(
                f"the SDK's caches can't be kept from the disk: {name} couldn't be loaded ({type(e).__name__})"
            ) from e
        current = getattr(module, attribute, None)
        if not isinstance(current, str):
            raise Unguarded(f"the SDK's caches can't be kept from the disk: {name} has no {attribute}")
        if current != target:
            setattr(module, attribute, target)
            changed.append(name)
    return changed


def guard_sigstore(is_local: Callable[[str], bool], check: Callable[[str], Any] | None = None) -> list[str]:
    """sigstore-python, as the SDK verifies with it: its users of requests given the stand-in,
    and its TUF client the guarded fetcher (_guard_tuf). Nothing, where sigstore isn't installed:
    then nothing fetches through it. Raises Unguarded when its TUF client isn't as expected."""
    try:
        root = importlib.import_module("sigstore")
    except ImportError:
        return []
    return _guard_requests(is_local, root, "sigstore", skipped=_SIGSTORE_SKIPPED, check=check) + _guard_tuf(
        is_local, check
    )


def _guard_requests(
    is_local: Callable[[str], bool],
    root: types.ModuleType,
    package: str,
    skipped: tuple = (),
    check: Callable[[str], Any] | None = None,
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
            stand_in = stand_in or _GuardedRequests(requests, _session_class(is_local, check))
            module.requests = stand_in
            touched = True
        for attribute in _REQUESTS_NAMES:
            if getattr(module, attribute, None) is getattr(requests, attribute):
                stand_in = stand_in or _GuardedRequests(requests, _session_class(is_local, check))
                setattr(module, attribute, getattr(stand_in, attribute))
                touched = True
        if touched:
            changed.append(name)
    return changed


def _fetcher_class(is_local: Callable[[str], bool], check: Callable[[str], Any] | None = None) -> type:
    """TUF's own urllib3 fetcher (its proxies from the environment, its User-Agent: sigstore's), asking
    urllib3 to follow no redirect, and following one itself only where redirect_problem allows, at most
    TUF_MAX_REDIRECTS. One it may not follow is a DownloadError, as TUF expects of a failed fetch."""
    import urllib3
    from tuf.api import exceptions
    from tuf.ngclient import Urllib3Fetcher

    class NotFollowed(RedirectNotFollowed, exceptions.DownloadError):
        """A redirect of Sigstore's trust root not followed: a failed fetch to TUF."""

    class GuardedFetcher(Urllib3Fetcher):
        refused = NotFollowed

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
                    response.close()  # its body unread, the connection not kept
                    response.release_conn()
                    if not location:
                        raise exceptions.DownloadHTTPError(
                            f"HTTP {response.status} with no address to go to", response.status
                        )
                    target = _joined(url, location)
                    problem = redirect_problem(url, target, is_local, check)
                    if problem:
                        raise NotFollowed(problem)
                    url = target
                    continue
                if response.status >= 400:
                    response.close()
                    raise exceptions.DownloadHTTPError(
                        f"HTTP error occurred with status {response.status}", response.status
                    )
                return self._chunks(response)
            raise NotFollowed(f"Sigstore's trust root was sent on more than {TUF_MAX_REDIRECTS} times; not followed")

    return GuardedFetcher


def _guard_tuf(is_local: Callable[[str], bool], check: Callable[[str], Any] | None = None) -> list[str]:
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
    fetcher_class = _fetcher_class(is_local, check)

    class GuardedUpdater(Updater):
        """tuf.ngclient.Updater, given the guarded fetcher when no fetcher is given, by name or in its
        place (None in either is none: TUF's own would fetch)."""

        fetches_guarded = True

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            # (metadata_dir, metadata_base_url, target_dir, target_base_url, fetcher, config)
            args = list(args)
            config = args[5] if len(args) > 5 else kwargs.get("config")
            if (args[4] if len(args) > 4 else kwargs.get("fetcher")) is None:
                guarded = fetcher_class(app_user_agent=getattr(config, "app_user_agent", None))
                if len(args) > 4:
                    args[4] = guarded
                else:
                    kwargs["fetcher"] = guarded
            super().__init__(*args, **kwargs)

    place.Updater = GuardedUpdater
    return [place.__name__]
