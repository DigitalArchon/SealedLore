"""Tinfoil's SDK fetches over https only (providers/sdk_guard.py): a redirect off
https, or from the internet into this computer or its network, is not followed.

The SDK makes bare requests.get and requests.post calls (GitHub's release and Sigstore
bundle, AMD's and Intel's certificates, the enclave's attestation), and requests follows a
redirect anywhere, https to plain http included. sdk_guard.guard hands each of its modules a
stand-in for requests whose session judges every redirect first, without changing the SDK
on disk. Here the SDK is a fake one, written to a folder of the test's own, in the shapes the
real one takes requests in; nothing reaches the network. Ported from DA Assistant's tests of
the original (sealed.guard_sdk_requests, Nov 2026).
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest import mock

import pytest

requests = pytest.importorskip("requests")

from sealedlore.models.config import is_local_host  # noqa: E402
from sealedlore.providers import private_mode, sdk_guard  # noqa: E402

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
def network(tmp_path: Path, monkeypatch):
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
    import sealedlore

    # The sealedlore under test, whichever checkout that is.
    src = str(Path(sealedlore.__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([src, os.environ.get("PYTHONPATH", "")])}
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120, env=env
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip() == "guarded"
