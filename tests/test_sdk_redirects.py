"""Tinfoil's SDK fetches over https only (providers/sdk_guard.py): a redirect off
https, or from the internet into this computer or its network, is not followed.

The SDK makes bare requests.get and requests.post calls (GitHub's release and Sigstore
bundle, AMD's and Intel's certificates, the enclave's attestation), and requests follows a
redirect anywhere, https to plain http included. sdk_guard.guard hands each of its modules a
stand-in for requests whose session judges every redirect first, without changing the SDK
on disk. Here the SDK is a fake one, written to a folder of the test's own, in the shapes the
real one takes requests in; nothing reaches the network. Ported from DA Assistant's tests of
the original (sealed.guard_sdk_requests, Nov 2026).

The SDK verifies the router's release with sigstore-python, whose trust root comes from
Sigstore's TUF repository through TUF's own client and urllib3, which follows a redirect
anywhere too: those tests (Nov 2026, from DA Vibe Manager's 4b03b1a) are at the end.
"""

from __future__ import annotations

import importlib
import io
import os
import pkgutil
import subprocess
import sys
import textwrap
import types
from pathlib import Path
from unittest import mock

import pytest

requests = pytest.importorskip("requests")
import urllib3  # noqa: E402 - requests' own

from sealedlore.models.config import is_local_host  # noqa: E402
from sealedlore.providers import private_mode, sdk_guard  # noqa: E402
from sealedlore.providers.base import ProviderError  # noqa: E402

FILES = {
    "__init__.py": "from .client import SecureClient\n",
    "client.py": """
        from .github import fetch_latest_release

        class SecureClient:
            def __init__(self, **kwargs):
                self.enclave = "enclave.example"

            def verify(self):
                return fetch_latest_release("owner/repo")
        """,
    # As tinfoil.github and tinfoil.attestation.* take it.
    "github.py": """
        import requests

        def fetch_latest_release(repo):
            response = requests.get(f"https://github-proxy.example/repos/{repo}", timeout=5)
            response.raise_for_status()
            return response.text
        """,
    # A function taken by name, and Session in an annotation made at import.
    "byname.py": """
        from typing import Optional

        import requests
        from requests import post

        HttpSession = Optional[requests.Session]

        def send(url, session: HttpSession = None):
            if session is not None:
                return session.get(url, timeout=5).text
            return post(url, json={}, timeout=5).text
        """,
    # A module the package doesn't import until it is wanted.
    "later/__init__.py": "",
    "later/collateral.py": """
        import requests

        def fetch(url):
            return requests.get(url, timeout=5).text
        """,
}

RELEASE = "https://github-proxy.example/repos/owner/repo"
# sigstore's command line, which the SDK never runs: never loaded to be guarded.
SIGSTORE_SKIPPED = ("sigstore.__main__", "sigstore._cli")


class Network:
    """Each URL answered as `answers` says (a redirect to another, or 200), and
    every request that reached it recorded."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.answers: dict[str, str] = {}

    def send(self, request):
        self.sent.append(request.url)
        response = requests.Response()
        response.url, response.request = request.url, request
        target = self.answers.get(request.url)
        if target:
            response.status_code = 302
            response.headers["Location"] = target
            response._content = b""
        else:
            response.status_code = 200
            response._content = b"fine"
        return response


@pytest.fixture
def sigstore_unguarded(tmp_path: Path, monkeypatch):
    """sigstore-python as it is before guard(), and put back as it was after the test:
    guarding changes it for the rest of the process, as the app wants. Every module of it
    is loaded first (but its command line, which the SDK never runs), outside any test's
    copy of sys.modules. Its TUF data goes to the test's own folder, never the home's."""
    try:
        import platformdirs
        import sigstore
        from tuf.ngclient import Updater
    except ImportError:  # without sigstore, guard() has nothing of it to guard
        yield
        return
    for kind in ("data", "cache"):
        monkeypatch.setattr(
            platformdirs,
            f"user_{kind}_dir",
            lambda appname=None, *_a, _kind=kind, **_k: str(tmp_path / _kind / (appname or "")),
        )
    for found in pkgutil.walk_packages(sigstore.__path__, "sigstore.", onerror=lambda _n: None):
        if found.name not in sys.modules and found.name not in SIGSTORE_SKIPPED:
            try:
                importlib.import_module(found.name)
            except Exception:  # noqa: BLE001 - one the SDK couldn't load either
                pass
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "sigstore" or name.startswith("sigstore.")):
            continue
        if isinstance(getattr(module, "requests", None), types.ModuleType):
            monkeypatch.setattr(module, "requests", requests)
    monkeypatch.setattr(importlib.import_module("sigstore._internal.tuf"), "Updater", Updater)
    yield


@pytest.fixture
def network(tmp_path: Path, monkeypatch, sigstore_unguarded):
    """The fake SDK in place of the real one (which comes back afterwards), and
    the network stood in for."""
    for name, text in FILES.items():
        path = tmp_path / "tinfoil" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    net = Network()
    with mock.patch.dict(sys.modules):
        for name in [n for n in sys.modules if n == "tinfoil" or n.startswith("tinfoil.")]:
            del sys.modules[name]
        monkeypatch.syspath_prepend(str(tmp_path))
        importlib.invalidate_caches()
        importlib.import_module("tinfoil")
        monkeypatch.setattr(
            requests.adapters.HTTPAdapter,
            "send",
            lambda _adapter, request, **_kw: net.send(request),
        )
        yield net
        monkeypatch.undo()


def module(name: str):
    return importlib.import_module(f"tinfoil.{name}")


def guard() -> list[str]:
    return sdk_guard.guard(is_local_host)


def test_unguarded_the_sdk_follows_a_redirect_into_the_clear(network):
    # What the SDK does by itself, and why: the plain http fetch went out.
    network.answers[RELEASE] = "http://evil.example/release"
    assert module("github").fetch_latest_release("owner/repo") == "fine"
    assert network.sent == [RELEASE, "http://evil.example/release"]


def test_guarded_it_is_refused_and_nothing_is_sent_there(network):
    guard()
    network.answers[RELEASE] = "http://evil.example/release"
    with pytest.raises(requests.RequestException, match="isn't https; not followed"):
        module("github").fetch_latest_release("owner/repo")
    assert network.sent == [RELEASE]


def test_from_the_internet_into_this_computers_network_is_refused(network):
    guard()
    network.answers[RELEASE] = "https://192.168.1.1/admin"
    with pytest.raises(requests.RequestException, match="on this computer or its network"):
        module("github").fetch_latest_release("owner/repo")
    assert network.sent == [RELEASE]


def test_a_redirect_that_stays_on_https_is_followed_a_relative_one_too(network):
    guard()
    network.answers[RELEASE] = "https://objects.example/release"
    assert module("github").fetch_latest_release("owner/repo") == "fine"
    assert network.sent == [RELEASE, "https://objects.example/release"]
    network.sent.clear()
    network.answers = {RELEASE: "/elsewhere"}
    assert module("github").fetch_latest_release("owner/repo") == "fine"
    assert network.sent == [RELEASE, "https://github-proxy.example/elsewhere"]


def test_a_function_taken_by_name_and_a_module_loaded_later_are_guarded(network):
    given = guard()
    assert "tinfoil.byname" in given and "tinfoil.later.collateral" in given
    for send in (module("byname").send, module("later.collateral").fetch):
        network.sent.clear()
        network.answers["https://kds.example/vcek"] = "http://kds.example/vcek"
        with pytest.raises(requests.RequestException, match="isn't https"):
            send("https://kds.example/vcek")
        assert network.sent == ["https://kds.example/vcek"]


def test_a_session_of_its_own_is_guarded_too(network):
    guard()
    session = module("byname").requests.Session()
    assert isinstance(session, requests.Session)
    network.answers["https://pcs.example/tcb"] = "http://pcs.example/tcb"
    with pytest.raises(requests.RequestException, match="isn't https"):
        module("byname").send("https://pcs.example/tcb", session)


def test_the_rest_of_requests_is_requests_own_and_twice_is_once(network):
    guard()
    stand_in = module("github").requests
    assert stand_in.RequestException is requests.RequestException
    assert stand_in.Response is requests.Response
    assert guard() == []
    assert module("github").requests is stand_in
    assert sys.modules["requests"] is requests, "the app's own requests is untouched"


def test_attesting_guards_the_sdk_first(network):
    network.answers[RELEASE] = "http://evil.example/release"
    with pytest.raises(requests.RequestException, match="isn't https"):
        private_mode.tinfoil_verify("https://nano-gpt.com/api/v1/private/tinfoil", "s" * 64)
    assert network.sent == [RELEASE]


def test_without_the_sdk_there_is_nothing_to_guard(network):
    del sys.modules["tinfoil"]
    assert guard() == []


def test_the_real_sdk_is_guarded_where_it_fetches():
    """tinfoil itself, in a process of its own (guarding changes its modules for
    the rest of that process): every module of it that fetches is given the
    stand-in, and a redirect to plain http through it is refused."""
    pytest.importorskip("tinfoil")
    script = textwrap.dedent(
        """
        import requests
        from requests.adapters import BaseAdapter

        import tinfoil
        from sealedlore.models.config import is_local_host
        from sealedlore.providers import sdk_guard

        changed = sdk_guard.guard(is_local_host)
        # Where tinfoil 0.15.0 fetches: the router's release, AMD's and
        # Intel's certificates and collateral, the enclave's attestation.
        fetching = {
            "tinfoil.github",
            "tinfoil.attestation.attestation",
            "tinfoil.attestation.bundle",
            "tinfoil.attestation.collateral_tdx",
            "tinfoil.attestation.verify_sev",
        }
        assert fetching <= set(changed), changed
        from tinfoil import github
        assert github.requests is not requests

        class Redirecting(BaseAdapter):
            def send(self, request, **kwargs):
                response = requests.Response()
                response.url, response.request = request.url, request
                response.status_code = 302
                response.headers["Location"] = "http://plain.example/"
                return response

            def close(self):
                pass

        session = github.requests.Session()
        session.mount("https://", Redirecting())
        try:
            session.get("https://github-proxy.example/release")
        except requests.RequestException as refused:
            assert "isn't https" in str(refused)
        else:
            raise AssertionError("followed")
        print("guarded")
        """
    )
    assert in_a_process_of_its_own(script) == "guarded"


def in_a_process_of_its_own(script: str, **env: str) -> str:
    """What `script` prints, run by this Python with the sealedlore under test (whichever
    checkout that is) first on its path."""
    import sealedlore

    src = str(Path(sealedlore.__file__).resolve().parents[1])
    env = {
        **os.environ,
        **env,
        "PYTHONPATH": os.pathsep.join([src, os.environ.get("PYTHONPATH", "")]),
    }
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120, env=env
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout.strip()


# --- sigstore-python, which the SDK verifies the router's release with (Nov 2026) ----------
#
# Its trust root comes from Sigstore's TUF repository through TUF's own client, whose fetcher
# asks urllib3, and urllib3 follows a redirect anywhere, https to plain http included. Here
# urllib3's connection pools are answered by a fake, so that urllib3's own redirect handling
# runs above it as it would for real; nothing reaches the network.

TUF_URL = "https://tuf.example/14.root.json"
SIGSTORE_TUF = "https://tuf-repo-cdn.sigstore.dev"
# sigstore 4.5's users of requests (Rekor's, Fulcio's, a timestamp authority's and OIDC's
# clients): none fetches while an attestation is verified, all of them while signing.
SIGSTORE_REQUESTS = (
    "sigstore.oidc",
    "sigstore._internal.rekor",
    "sigstore._internal.rekor.client",
    "sigstore._internal.rekor.client_v2",
    "sigstore._internal.fulcio.client",
    "sigstore._internal.timestamp",
)


class Wire:
    """urllib3's connection pools: each URL answered as `answers` says (a redirect to
    another, or with a status of its own, else 200), a host in `everywhere` sent on
    wherever it is asked for, and every request that reached a pool recorded."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.agents: list[str] = []
        self.answers: dict[str, str | int] = {}
        self.everywhere: dict[str, str] = {}

    def urlopen(self, pool, method: str, url: str, **kw):
        whole = url if "://" in url else f"{pool.scheme}://{pool.host}{url}"
        self.sent.append(whole)
        self.agents.append((kw.get("headers") or {}).get("User-Agent", ""))
        answer = self.answers.get(whole) or self.everywhere.get(pool.host)
        if isinstance(answer, int):
            return self._response(whole, answer, b"")
        if answer:
            return self._response(whole, 302, b"", Location=answer)
        return self._response(whole, 200, b"fine")

    @staticmethod
    def _response(url: str, status: int, body: bytes, **headers: str):
        return urllib3.HTTPResponse(
            body=io.BytesIO(body),
            status=status,
            headers=headers,
            preload_content=False,
            request_url=url,
        )


@pytest.fixture
def wire(monkeypatch) -> Wire:
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    fake = Wire()
    monkeypatch.setattr(
        urllib3.connectionpool.HTTPConnectionPool,
        "urlopen",
        lambda pool, method, url, **kw: fake.urlopen(pool, method, url, **kw),
    )
    return fake


def fetched(fetcher, url: str = TUF_URL) -> bytes:
    return b"".join(fetcher.fetch(url))


def guarded_fetcher():
    return sdk_guard._fetcher_class(is_local_host)(app_user_agent="sigstore-python/4.5.0")


def test_tufs_own_fetcher_follows_one_into_the_clear_and_the_guarded_one_does_not(wire):
    from tuf.api import exceptions
    from tuf.ngclient import Urllib3Fetcher

    # What TUF's client does by itself, and why: urllib3 followed it to plain http.
    wire.answers[TUF_URL] = "http://evil.example/14.root.json"
    assert fetched(Urllib3Fetcher()) == b"fine"
    assert wire.sent == [TUF_URL, "http://evil.example/14.root.json"]
    for target, said in (
        ("http://evil.example/14.root.json", "isn't https; not followed"),
        ("https://192.168.1.1/admin", "on this computer or its network"),
    ):
        wire.sent.clear()
        wire.answers[TUF_URL] = target
        # A failed fetch, as TUF handles one: the refresh fails, and nothing goes there.
        with pytest.raises(exceptions.DownloadError, match=said):
            fetched(guarded_fetcher())
        assert wire.sent == [TUF_URL]


def test_the_guarded_fetcher_follows_one_that_stays_on_https_and_a_relative_one_too(wire):
    import tuf

    wire.answers[TUF_URL] = "https://cdn.example/14.root.json"
    assert fetched(guarded_fetcher()) == b"fine"
    assert wire.sent == [TUF_URL, "https://cdn.example/14.root.json"]
    wire.sent.clear()
    wire.answers[TUF_URL] = "/elsewhere/14.root.json"
    assert fetched(guarded_fetcher()) == b"fine"
    assert wire.sent == [TUF_URL, "https://tuf.example/elsewhere/14.root.json"]
    # Sigstore's User-Agent, then TUF's, as TUF's own fetcher sends them.
    assert set(wire.agents) == {f"sigstore-python/4.5.0 python-tuf/{tuf.__version__}"}


def test_the_guarded_fetcher_follows_only_a_few(wire):
    from tuf.api import exceptions

    hops = [TUF_URL] + [f"https://hop{n}.example/14.root.json" for n in range(10)]
    wire.answers.update(zip(hops, hops[1:], strict=False))
    with pytest.raises(exceptions.DownloadError, match="more than 5 times; not followed"):
        fetched(guarded_fetcher())
    assert sdk_guard.TUF_MAX_REDIRECTS == 5
    assert wire.sent == hops[:6]


def test_the_guarded_fetchers_failures_are_tufs_own(wire):
    """TUF reads a 404 for the next root as "no newer root": the guarded fetcher fails as
    TUF's own does, with the status, and a redirect with nowhere to go is a failure too."""
    from tuf.api import exceptions

    wire.answers[TUF_URL] = 404
    with pytest.raises(exceptions.DownloadHTTPError) as refused:
        fetched(guarded_fetcher())
    assert refused.value.status_code == 404
    wire.answers[TUF_URL] = 304
    with pytest.raises(exceptions.DownloadHTTPError, match="no address to go to"):
        fetched(guarded_fetcher())


def test_sigstores_trust_root_is_refreshed_through_the_guarded_fetcher(
    wire, sigstore_unguarded, tmp_path: Path
):
    """sigstore's TrustUpdater builds TUF's Updater with no fetcher: unguarded, a refresh
    sent on into the clear went there; guarded, the refresh fails, and the attestation with
    it, and nothing is fetched there. Its TUF data stays in the test's own folder."""
    from sigstore._internal import tuf as sigstore_tuf
    from sigstore._utils import read_embedded
    from sigstore.errors import TUFError
    from tuf.ngclient import Updater, UpdaterConfig

    wire.everywhere["tuf-repo-cdn.sigstore.dev"] = "http://evil.example/root.json"
    with pytest.raises(TUFError):
        sigstore_tuf.TrustUpdater(SIGSTORE_TUF)
    assert "http://evil.example/root.json" in wire.sent  # what it does by itself
    assert (tmp_path / "cache" / "sigstore-python" / "tuf").is_dir()
    wire.sent.clear()
    assert "sigstore._internal.tuf" in sdk_guard.guard_sigstore(is_local_host)
    with pytest.raises(TUFError):
        sigstore_tuf.TrustUpdater(SIGSTORE_TUF)
    assert wire.sent and all(url.startswith(SIGSTORE_TUF + "/") for url in wire.sent)
    # The Updater it builds has the guarded fetcher, and keeps one it is given.
    made = sigstore_tuf.Updater(
        metadata_dir=str(tmp_path / "m"),
        metadata_base_url=SIGSTORE_TUF,
        target_dir=str(tmp_path / "t"),
        target_base_url=SIGSTORE_TUF + "/targets/",
        config=UpdaterConfig(app_user_agent="sigstore-python/4.5.0"),
        bootstrap=read_embedded("root.json", SIGSTORE_TUF),
    )
    assert isinstance(made, Updater) and type(made._fetcher).__name__ == "GuardedFetcher"
    mine = guarded_fetcher()
    given = sigstore_tuf.Updater(
        str(tmp_path / "m"),
        SIGSTORE_TUF,
        fetcher=mine,
        bootstrap=read_embedded("root.json", SIGSTORE_TUF),
    )
    assert given._fetcher is mine
    assert sdk_guard.guard_sigstore(is_local_host) == []  # twice is once


def test_sigstores_own_requests_users_are_guarded_as_the_sdks_are(network):
    given = guard()  # the SDK, and what it verifies with
    assert set(SIGSTORE_REQUESTS) <= set(given)
    for name in SIGSTORE_REQUESTS:
        assert isinstance(sys.modules[name].requests, sdk_guard._GuardedRequests), name
    assert "sigstore._cli" not in given  # its command line: never run, never loaded
    rekor = "https://rekor.example/api/v1/log"
    network.answers[rekor] = "http://rekor.example/api/v1/log"
    client = sys.modules["sigstore._internal.rekor.client"]
    with pytest.raises(requests.RequestException, match="isn't https"):
        client.requests.Session().get(rekor)
    assert network.sent == [rekor]


def test_with_sigstores_internals_elsewhere_nothing_is_attested(network, monkeypatch):
    """Another sigstore or TUF, its TUF client not where the guard expects: the attestation
    is refused, saying why, rather than its trust root fetched unguarded; nothing is sent."""
    from sigstore._internal import tuf as sigstore_tuf

    monkeypatch.setattr(sigstore_tuf, "Updater", object)
    with pytest.raises(sdk_guard.Unguarded, match="isn't tuf.ngclient.Updater"):
        guard()
    enclave = private_mode.Enclave("https://nano-gpt.com/api/v1/private/tinfoil")
    with pytest.raises(ProviderError, match="couldn't be attested: Sigstore's trust root can't"):
        enclave.current()
    assert network.sent == []


def test_what_the_guard_rests_on_is_where_it_expects(sigstore_unguarded):
    """sigstore 4.5 and tuf 7.0, as the AppImage locks them: the guard takes them as they
    are, and a version that moves any of this fails here first (and in the app refuses to
    attest: Unguarded). A new user of requests in sigstore is named here, to be looked at."""
    import inspect

    from sigstore._internal import tuf as sigstore_tuf
    from tuf.api import exceptions
    from tuf.ngclient import Updater, Urllib3Fetcher
    from tuf.ngclient._internal.proxy import ProxyEnvironment

    assert sigstore_tuf.Updater is Updater and callable(sigstore_tuf.TrustUpdater)
    made = inspect.getsource(sigstore_tuf.TrustUpdater.__init__)
    assert "Updater(" in made and "fetcher" not in made  # it gives none: the guard's is used
    assert list(inspect.signature(Updater.__init__).parameters)[1:7] == [
        "metadata_dir",
        "metadata_base_url",
        "target_dir",
        "target_base_url",
        "fetcher",
        "config",
    ]
    assert isinstance(Urllib3Fetcher()._proxy_env, ProxyEnvironment)
    assert callable(Urllib3Fetcher._chunks)
    assert issubclass(exceptions.DownloadHTTPError, exceptions.DownloadError)
    users = sorted(
        name
        for name, module in sys.modules.items()
        if module is not None
        and (name == "sigstore" or name.startswith("sigstore."))
        and getattr(module, "requests", None) is requests
    )
    assert users == sorted(SIGSTORE_REQUESTS)
    assert sdk_guard._guard_tuf(is_local_host) == ["sigstore._internal.tuf"]


def test_the_real_sdk_refreshes_sigstores_trust_root_over_https_only(tmp_path: Path):
    """tinfoil and sigstore themselves, in a process of their own: guarded as tinfoil_verify
    guards them, the trust root's refresh that tinfoil.sigstore starts (Verifier.production)
    is sent on to plain http by Sigstore's CDN, and fails without going there. Its TUF data
    in the test's own folder."""
    pytest.importorskip("tinfoil")
    script = textwrap.dedent(
        """
        import io, os

        import platformdirs
        import urllib3

        home = os.environ["SL_TUF_HOME"]
        platformdirs.user_data_dir = lambda name=None, *a, **k: os.path.join(home, "data", name)
        platformdirs.user_cache_dir = lambda name=None, *a, **k: os.path.join(home, "cache", name)
        for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            os.environ.pop(name, None)
            os.environ.pop(name.upper(), None)
        sent = []

        def urlopen(pool, method, url, **kw):
            whole = url if "://" in url else f"{pool.scheme}://{pool.host}{url}"
            sent.append(whole)
            status, headers = 200, {}
            if pool.host == "tuf-repo-cdn.sigstore.dev":
                status, headers = 302, {"Location": "http://plain.example/root.json"}
            return urllib3.HTTPResponse(body=io.BytesIO(b"fine"), status=status,
                                        headers=headers, preload_content=False,
                                        request_url=whole)

        urllib3.connectionpool.HTTPConnectionPool.urlopen = urlopen

        import tinfoil
        from sealedlore.models.config import is_local_host
        from sealedlore.providers import sdk_guard

        changed = sdk_guard.guard(is_local_host)
        from sigstore.errors import TUFError
        from sigstore.verify import Verifier

        try:
            Verifier.production()
        except TUFError:
            pass
        else:
            raise AssertionError("refreshed")
        assert sent, "nothing fetched"
        assert all(url.startswith("https://tuf-repo-cdn.sigstore.dev/") for url in sent), sent
        assert "sigstore._internal.tuf" in changed, changed
        assert "sigstore._internal.rekor.client" in changed, changed
        print("guarded")
        """
    )
    assert in_a_process_of_its_own(script, SL_TUF_HOME=str(tmp_path)) == "guarded"
    assert (tmp_path / "cache" / "sigstore-python" / "tuf").is_dir()
