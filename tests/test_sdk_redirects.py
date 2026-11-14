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
    # As tinfoil.github and tinfoil.attestation.* take it, and where they cache (guard_files).
    "github.py": """
        import requests

        _GITHUB_CACHE_DIR = "/home/someone/.cache/tinfoil"

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
    "attestation/__init__.py": "",
    "attestation/verify_sev.py": "_VCEK_CACHE_DIR = '/home/someone/.cache/tinfoil'\n",
    "attestation/collateral_tdx.py": "_TDX_CACHE_DIR = '/home/someone/.cache/tinfoil'\n",
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


class Body(io.BytesIO):
    """A reply's body on the wire: how far it was read is kept when it is closed."""

    at = 0

    def close(self) -> None:
        if not self.closed:
            self.at = self.tell()
        super().close()

    def read_so_far(self) -> int:
        return self.at if self.closed else self.tell()


class Network:
    """Each URL answered as `answers` says (a redirect to another, or 200), or as
    `replies` says (a status, a body and headers, as an adapter builds a response
    from the wire: read only as far as it is read), and every request that reached
    it recorded, with its headers."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.headers: list[dict] = []
        self.answers: dict[str, str] = {}
        self.replies: dict[str, tuple[int, Body, dict]] = {}

    def send(self, request):
        self.sent.append(request.url)
        self.headers.append(dict(request.headers))
        if request.url in self.replies:
            status, body, headers = self.replies[request.url]
            raw = urllib3.HTTPResponse(
                body=body,
                status=status,
                headers=headers,
                preload_content=False,
                decode_content=True,
                request_method=request.method,
            )
            return requests.adapters.HTTPAdapter().build_response(request, raw)
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


def test_a_refused_redirect_is_named_as_one(network):
    """What a refused redirect raises says so (`redirect_refused`, RedirectNotFollowed):
    the app finds it among the causes of what the SDK raised (not_followed), where
    sigstore says only "Failed to refresh TUF metadata" (DA Assistant's original)."""
    guard()
    network.answers[RELEASE] = "http://evil.example/release"
    with pytest.raises(requests.exceptions.InvalidURL) as refused:
        module("github").fetch_latest_release("owner/repo")
    assert isinstance(refused.value, sdk_guard.RedirectNotFollowed)
    assert refused.value.redirect_refused is True
    try:
        try:
            raise refused.value
        except requests.RequestException as e:
            raise RuntimeError("Failed to refresh TUF metadata") from e
    except RuntimeError as wrapped:
        assert sdk_guard.not_followed(wrapped) == str(refused.value)
    assert sdk_guard.not_followed(RuntimeError("anything else")) == ""
    assert sdk_guard.not_followed(None) == ""


@pytest.mark.parametrize(
    "target, said",
    [
        ("https://exa mple.com/release", "no spaces, backslashes or control characters"),
        ("https://objects.example\\@evil.example/", "no spaces, backslashes or control characters"),
        ("https://objects.example:99999/release", "host or port can't be read"),
        ("https://[fd00::5/release", "host or port can't be read"),
        ("https://127.0.0.1%2eevil.example/release", "no % escapes"),
        ("https://0.0.0.0/release", "the address a server listens on"),
        ("https://[::]:8443/release", "the address a server listens on"),
    ],
)
def test_a_redirect_is_held_to_the_whole_rule_for_an_address(network, target, said):
    """Not to its scheme alone (DA Assistant's original held it to netguard's whole
    rule): an address that can't be read one way, or 0.0.0.0, which reaches this
    computer, is not followed, and nothing is sent there."""
    guard()
    network.answers[RELEASE] = target
    with pytest.raises(sdk_guard.RedirectNotFollowed, match=said):
        module("github").fetch_latest_release("owner/repo")
    assert network.sent == [RELEASE]


def test_and_to_the_apps_own_rule_for_an_address(network, monkeypatch):
    """The app's rule (`check`) is asked too, and whatever it raises for an address is a
    refusal (DA Media Browser's raises a URLError); attesting passes SealedLore's
    (require_https)."""

    def refusing(url: str) -> str:
        if "objects.example" in url:
            raise ValueError("not an address of ours")
        if "files.example" in url:
            raise OSError("<urlopen error not one of ours either>")
        return url

    sdk_guard.guard(is_local_host, check=refusing)
    network.answers[RELEASE] = "https://objects.example/release"
    with pytest.raises(
        sdk_guard.RedirectNotFollowed, match="an address refused .not an address of ours"
    ):
        module("github").fetch_latest_release("owner/repo")
    assert network.sent == [RELEASE]
    network.answers[RELEASE] = "https://files.example/release"
    with pytest.raises(sdk_guard.RedirectNotFollowed, match="not one of ours either"):
        module("github").fetch_latest_release("owner/repo")
    assert network.sent == [RELEASE, RELEASE]
    passed = []
    monkeypatch.setattr(sdk_guard, "guard", lambda *a, **k: passed.append(k.get("check")) or [])
    with pytest.raises(requests.RequestException):
        private_mode.tinfoil_verify("https://nano-gpt.com/api/v1/private/tinfoil", "s" * 64)
    assert passed == [private_mode.require_https]


def test_every_request_asks_for_gzip_or_deflate_alone(network):
    """requests asks for br and zstd too where brotli or zstandard is installed, whose
    ratios are unbounded per chunk: the SDK's requests never do, whatever they say."""
    guard()
    module("github").fetch_latest_release("owner/repo")
    module("byname").requests.get(RELEASE, headers={"Accept-Encoding": "br, zstd"}, timeout=5)
    assert [h["Accept-Encoding"] for h in network.headers] == ["gzip, deflate"] * 2
    assert sdk_guard.ACCEPT_ENCODING == "gzip, deflate"


@pytest.mark.parametrize("coding", ["br", "zstd", "gzip, gzip", "compress"])
def test_a_reply_in_any_other_coding_is_refused_unread(network, coding):
    guard()
    body = Body(b"x" * 1000)
    network.replies[RELEASE] = (200, body, {"Content-Encoding": coding})
    with pytest.raises(requests.exceptions.ContentDecodingError, match="a coding not asked for"):
        module("github").fetch_latest_release("owner/repo")
    assert body.read_so_far() == 0


def test_a_whole_reply_is_read_to_32_mb_decoded(network, monkeypatch):
    """Counted as decoded: a gzip reply that unpacks past the limit is refused, having
    read little more than the limit of it; one declaring more is refused unread."""
    import gzip

    guard()
    monkeypatch.setattr(sdk_guard, "REPLY_MAX_BYTES", 1024 * 1024)
    assert module("github").fetch_latest_release("owner/repo") == "fine"
    packed = gzip.compress(b"\0" * (8 * 1024 * 1024))
    body = Body(packed)
    network.replies[RELEASE] = (200, body, {"Content-Encoding": "gzip"})
    with pytest.raises(requests.RequestException, match="more than 1 MB; not read on"):
        module("github").fetch_latest_release("owner/repo")
    assert len(packed) < 1024 * 1024  # so the limit was met by what it unpacked to
    body = Body(b"x" * 100)
    network.replies[RELEASE] = (200, body, {"Content-Length": str(2 * 1024 * 1024)})
    with pytest.raises(requests.RequestException, match="more than 1 MB; not read"):
        module("github").fetch_latest_release("owner/repo")
    assert body.read_so_far() == 0
    whole = io.BytesIO(b"y" * (1024 * 1024))
    network.replies[RELEASE] = (200, whole, {})
    assert module("github").fetch_latest_release("owner/repo") == "y" * (1024 * 1024)
    assert sdk_guard.REPLY_MAX_BYTES == 1024 * 1024
    monkeypatch.undo()
    assert sdk_guard.REPLY_MAX_BYTES == 32 * 1024 * 1024


def test_an_errors_or_a_redirects_body_is_read_to_64_kb(network):
    """Its start kept, the rest unread: requests reads a redirect's body whole before
    following it, and an error's whenever its text is asked for."""
    guard()
    error = Body(b"e" * (1024 * 1024))
    network.replies[RELEASE] = (500, error, {})
    with pytest.raises(requests.HTTPError) as failed:
        module("github").fetch_latest_release("owner/repo")
    assert len(failed.value.response.content) == sdk_guard.ERROR_BODY_BYTES == 64 * 1024
    assert error.read_so_far() == 64 * 1024 + 1
    moved = Body(b"r" * (1024 * 1024))
    network.replies[RELEASE] = (302, moved, {"Location": "https://objects.example/release"})
    response = module("byname").requests.get(RELEASE, timeout=5)
    assert response.text == "fine"
    assert len(response.history[0].content) == 64 * 1024
    assert moved.read_so_far() == 64 * 1024 + 1
    assert network.sent[-2:] == [RELEASE, "https://objects.example/release"]


def test_a_streamed_reply_is_the_callers_to_read(network):
    guard()
    body = Body(b"s" * 1000)
    network.replies[RELEASE] = (200, body, {})
    response = module("byname").requests.get(RELEASE, stream=True, timeout=5)
    assert body.read_so_far() == 0
    assert response.content == b"s" * 1000


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


def test_the_sdks_caches_are_pointed_where_nothing_can_be_made(network):
    """guard_files: the SDK's three cache folders, which it reads at each use, at one under the
    null device; twice is once."""
    changed = guard()
    assert {"tinfoil.github", "tinfoil.attestation.verify_sev"} <= set(changed)
    for name, attribute in sdk_guard._SDK_CACHES:
        assert (
            getattr(sys.modules[name], attribute)
            == sdk_guard.nowhere()
            == os.path.join(os.devnull, "tinfoil")
        )
    assert not os.path.isdir(os.devnull)
    assert sdk_guard.guard_files() == []


def test_an_sdk_whose_caches_arent_where_expected_is_not_attested(network, monkeypatch):
    """Another version, its cache elsewhere: refused, saying why, rather than left to write."""
    monkeypatch.delattr(module("attestation.verify_sev"), "_VCEK_CACHE_DIR")
    with pytest.raises(sdk_guard.Unguarded, match="verify_sev has no _VCEK_CACHE_DIR"):
        guard()
    enclave = private_mode.Enclave("https://nano-gpt.com/api/v1/private/tinfoil")
    with pytest.raises(ProviderError, match="couldn't be attested: the SDK's caches can't be kept"):
        enclave.current()
    assert network.sent == []


WRITES = """
import io, json, os, sys, types

import requests

home = os.environ["HOME"]
from tinfoil import github
from tinfoil.attestation import collateral_tdx, verify_sev
from sealedlore.models.config import is_local_host
from sealedlore.providers import sdk_guard

if sys.argv[1] == "guarded":
    sdk_guard.guard(is_local_host)


from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from sealedlore.providers import dcap

# Any certificate stands for AMD's VCEK here: from_report keeps it before the chain is checked.
CERTIFICATE = x509.load_pem_x509_certificate(dcap.INTEL_ROOT_PEM).public_bytes(Encoding.DER)


def served(adapter, request, **_kw):
    response = requests.Response()
    response.status_code, response.url, response.request = 200, request.url, request
    response._content = (json.dumps({"attestations": [{"bundle": {"b": 1}}]}).encode()
                         if "attestations" in request.url else CERTIFICATE)
    return response


requests.adapters.HTTPAdapter.send = served
# The router's Sigstore bundle (the direct path), AMD's VCEK (named after the chip), and Intel's
# collateral for a TDX router (the relay's path too)
github.fetch_attestation_bundle("tinfoilsh/confidential-model-router", "ab" * 32)
signer = types.SimpleNamespace(signingKey=verify_sev.ReportSigner.VcekReportSigner)
report = types.SimpleNamespace(productName="Genoa", chip_id=bytes(64), reported_tcb=0,
                               signer_info_parsed=signer)
verify_sev.CertificateChain.from_report(report)
collateral_tdx._write_cache(collateral_tdx._get_tcb_info_cache_path("00906ed50000"),
                            collateral_tdx.CacheEntry(body=b"{}"))
written = sorted(
    os.path.relpath(os.path.join(top, name), home)
    for top, _dirs, names in os.walk(home)
    for name in names
)
print(json.dumps(written))
"""


def test_the_real_sdk_writes_nothing_of_its_own(tmp_path: Path):
    """tinfoil itself, in a process of its own with a home of the test's own: unguarded, its
    bundle, VCEK and TDX caches land in ~/.cache/tinfoil; guarded as tinfoil_verify guards it,
    nothing does, nor ~/.tinfoil (the cache secret is always passed)."""
    import json

    pytest.importorskip("tinfoil")
    found = {}
    for how in ("unguarded", "guarded"):
        home = tmp_path / how
        home.mkdir()
        env = {
            "HOME": str(home),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
        }
        script = WRITES.replace('sys.argv[1] == "guarded"', repr(how) + ' == "guarded"')
        found[how] = json.loads(in_a_process_of_its_own(script, **env))
    tinfoil_files = [name for name in found["unguarded"] if name.startswith(".cache/tinfoil/")]
    assert len(tinfoil_files) == 3, found["unguarded"]
    assert any(name.startswith(".cache/tinfoil/VCEK_Genoa_") for name in tinfoil_files)
    assert found["guarded"] == [], "nothing of the SDK's own"
    assert not (tmp_path / "guarded" / ".cache" / "tinfoil").exists()
    assert not (tmp_path / "guarded" / ".tinfoil").exists()


def test_what_keeping_the_sdks_files_rests_on_is_where_it_expects():
    """tinfoil 0.15, as the locks pin it: each cache folder a module attribute read at each
    use, each write passed over when it fails (OSError) and each read only of a file that is
    there. A version that moves any of this fails here first (and in the app refuses to attest:
    Unguarded)."""
    import inspect

    pytest.importorskip("tinfoil")
    from tinfoil import github
    from tinfoil.attestation import collateral_tdx, verify_sev

    for name, attribute in sdk_guard._SDK_CACHES:
        assert isinstance(getattr(importlib.import_module(name), attribute), str), name
    for function, attribute in (
        (github._ensure_github_cache_dir, "_GITHUB_CACHE_DIR"),
        (github._attestation_bundle_cache_path, "_GITHUB_CACHE_DIR"),
        (verify_sev._ensure_vcek_cache_dir, "_VCEK_CACHE_DIR"),
        (verify_sev.CertificateChain._vcek_cache_path, "_VCEK_CACHE_DIR"),
        (collateral_tdx._ensure_cache_dir, "_TDX_CACHE_DIR"),
        (collateral_tdx._get_tcb_info_cache_path, "_TDX_CACHE_DIR"),
    ):
        assert attribute in inspect.getsource(function), function  # read at each use
    for function in (
        github._ensure_github_cache_dir,
        github.fetch_attestation_bundle,
        verify_sev._ensure_vcek_cache_dir,
        verify_sev.CertificateChain.from_report,
        collateral_tdx._ensure_cache_dir,
        collateral_tdx._write_cache,
    ):
        assert "except OSError" in inspect.getsource(function), function
    for function in (
        github.fetch_attestation_bundle,
        verify_sev.CertificateChain.from_report,
        collateral_tdx._read_cache,
    ):
        assert "os.path.isfile(" in inspect.getsource(function), function


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


def test_the_trust_roots_refused_redirect_is_named_as_one_and_held_to_the_whole_rule(wire):
    """As the SDK's requests (DA Assistant's original): a TUF download error that says it
    is a refused redirect, for an address that can't be read or 0.0.0.0 as for plain http,
    and for one sent on too many times; the app's own rule asked too."""
    from tuf.api import exceptions

    for target in (
        "http://evil.example/14.root.json",
        "https://exa mple.com/14.root.json",
        "https://127.0.0.1%2eevil.example/14.root.json",
        "https://0.0.0.0/14.root.json",
        "https://cdn.example:99999/14.root.json",
    ):
        wire.sent.clear()
        wire.answers[TUF_URL] = target
        with pytest.raises(exceptions.DownloadError) as refused:
            fetched(guarded_fetcher())
        assert isinstance(refused.value, sdk_guard.RedirectNotFollowed), target
        assert not isinstance(refused.value, exceptions.DownloadHTTPError)
        assert wire.sent == [TUF_URL], target
    hops = [TUF_URL] + [f"https://hop{n}.example/14.root.json" for n in range(10)]
    wire.answers.update(zip(hops, hops[1:], strict=False))
    with pytest.raises(sdk_guard.RedirectNotFollowed, match="more than 5 times"):
        fetched(guarded_fetcher())

    def refusing(url: str) -> str:
        raise ValueError("not an address of ours")

    wire.answers = {TUF_URL: "https://cdn.example/14.root.json"}
    wire.sent.clear()
    with pytest.raises(sdk_guard.RedirectNotFollowed, match="not an address of ours"):
        fetched(
            sdk_guard._fetcher_class(is_local_host, refusing)(
                app_user_agent="sigstore-python/4.5.0"
            )
        )
    assert wire.sent == [TUF_URL]


def test_the_guarded_fetcher_goes_through_the_environments_proxy_and_holds_it_there(
    wire, monkeypatch
):
    """TUF's own proxy handling (its ProxyEnvironment, from HTTPS_PROXY), as DA
    Assistant's original was tested through a SOCKS proxy: the guarded fetcher's requests
    go through the proxy's manager, and a redirect off https is refused there too."""
    from tuf.api import exceptions
    from tuf.ngclient._internal import proxy as tuf_proxy

    made = []

    class Manager(urllib3.PoolManager):
        def __init__(self, proxy_url, **kwargs):
            super().__init__(**kwargs)
            self.proxy_url, self.through = proxy_url, []
            made.append(self)

        def urlopen(self, method, url, redirect=True, **kwargs):
            self.through.append(url)
            return super().urlopen(method, url, redirect=redirect, **kwargs)

    monkeypatch.setattr(tuf_proxy, "ProxyManager", Manager)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    fetcher = guarded_fetcher()
    wire.answers[TUF_URL] = "https://cdn.example/14.root.json"
    assert fetched(fetcher) == b"fine"
    assert [manager.proxy_url for manager in made] == ["http://proxy.example:3128"]
    assert made[0].through == [TUF_URL, "https://cdn.example/14.root.json"]
    wire.sent.clear()
    wire.answers[TUF_URL] = "http://evil.example/14.root.json"
    with pytest.raises(exceptions.DownloadError, match="isn't https"):
        fetched(fetcher)
    assert made[0].through[-1] == TUF_URL and wire.sent == [TUF_URL]


def test_a_fetcher_given_as_none_in_its_place_is_the_guarded_one_too(
    sigstore_unguarded, tmp_path: Path
):
    """By name or in its place (DA Assistant's original guarded both): None is no fetcher,
    and TUF's own would fetch unguarded."""
    from sigstore._internal import tuf as sigstore_tuf
    from sigstore._utils import read_embedded

    sdk_guard.guard_sigstore(is_local_host)
    root = read_embedded("root.json", SIGSTORE_TUF)
    placed = sigstore_tuf.Updater(
        str(tmp_path / "m"), SIGSTORE_TUF, None, None, None, bootstrap=root
    )
    assert type(placed._fetcher).__name__ == "GuardedFetcher"
    named = sigstore_tuf.Updater(str(tmp_path / "n"), SIGSTORE_TUF, fetcher=None, bootstrap=root)
    assert type(named._fetcher).__name__ == "GuardedFetcher"
    mine = guarded_fetcher()
    kept = sigstore_tuf.Updater(str(tmp_path / "k"), SIGSTORE_TUF, None, None, mine, bootstrap=root)
    assert kept._fetcher is mine


def test_the_enclave_says_which_redirect_was_refused(wire, sigstore_unguarded):
    """sigstore says only "Failed to refresh TUF metadata": the refusal, its cause, is
    said too (DA Assistant's original), and nothing is fetched where it pointed."""
    from sigstore._internal import tuf as sigstore_tuf

    wire.everywhere["tuf-repo-cdn.sigstore.dev"] = "http://evil.example/root.json"
    sdk_guard.guard_sigstore(is_local_host)

    def verifier(_relay, _secret):
        sigstore_tuf.TrustUpdater(SIGSTORE_TUF)

    enclave = private_mode.Enclave("https://nano-gpt.com/api/v1/private/tinfoil", verifier=verifier)
    with pytest.raises(ProviderError) as refused:
        enclave.current()
    assert (
        "couldn't be attested: Failed to refresh TUF metadata (the attestation was sent on to "
        "'http://evil.example/root.json', which isn't https; not followed"
    ) in str(refused.value)
    assert wire.sent and all(url.startswith(SIGSTORE_TUF + "/") for url in wire.sent)


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
    # the guarded fetcher's requests go through ProxyEnvironment, which makes a proxy's
    # manager by that name
    assert "ProxyManager(" in inspect.getsource(ProxyEnvironment.get_pool_manager)
    assert "get_pool_manager(" in inspect.getsource(ProxyEnvironment.request)
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
