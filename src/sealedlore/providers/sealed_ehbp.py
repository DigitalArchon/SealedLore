"""Chat sealed end to end to Tinfoil's attested router, whoever relays it.

The part of an end-to-end encrypted client that doesn't depend on who carries
the ciphertext: NanoGPT's relay (providers/private_mode.py) or Tinfoil's own
API (providers/tinfoil_direct.py). Both reach Tinfoil's router
(`tinfoilsh/confidential-model-router`), attested with Tinfoil's own Python
SDK, pinned by hash.

1. **Attest** (`Enclave.current`, `sdk_verify`): the SDK's `SecureClient`
   fetches an attestation bundle and checks it against the hardware vendors'
   roots (AMD SEV-SNP or Intel TDX) and Sigstore's record of the router
   release. The report binds the enclave's HPKE key (and its TLS key). From
   a bundle the SDK accepts any genuine release, so the latest one is looked
   up from Tinfoil directly and an older one is reported (`latest_release`),
   not refused: rollouts lag. The SDK's fetches, and those of the Sigstore
   library it verifies with (its TUF trust root included), follow no redirect
   off https, nor one from the internet into this computer or its network,
   and its caches are pointed where nothing can be made; where it can't be
   held to that, nothing is attested (providers/sdk_guard.py).
2. **Seal** the body with EHBP (`ehbp.EHBPTransport`: HPKE X25519, AES-GCM)
   to the attested key, and decrypt the reply. Only a success the transport
   opened is taken (`_accept`): EHBP refuses a 2xx that isn't sealed but
   passes any other status through as it came, so a reply someone in between
   made itself (a 302 with an answer in it) is refused here, as is a stream
   that ends without its final chunk. What a relay or service writes in an
   error is never sealed, so it is said as theirs (`_http_error`). Nothing is
   ever sent unsealed (`_NonEmptyBodyTransport`).

The router scopes its prompt cache to the API identity and a secret sealed in
the body: ours, random per provider and never written (`Enclave.cache_secret`),
so no one in between can time our cache. The attestation is redone before a
request once it is `MAX_AGE_SECONDS` old (laptop sleep, a key rotated
underneath us), on a clock that runs on while the machine sleeps
(`dcap.since_boot`), and once more when the enclave says the key has changed
(a 422).
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from sealedlore.models.config import ProviderConfig, is_local_host, require_https, without_password
from sealedlore.models.private import Attestation
from sealedlore.providers.base import ChatRequest, ProviderError, StreamEvent, relay_said
from sealedlore.providers.dcap import since_boot
from sealedlore.providers.http import environment_proxy, make_client
from sealedlore.providers.openai_compat import OpenAICompatibleProvider

ROUTER_REPO = "tinfoilsh/confidential-model-router"
MAX_AGE_SECONDS = 5 * 60
# What a relay or service writes in an error, as much as is said of it
# (`relay_said`: on one line, without what a reader can't see).
RELAY_SAID_CHARS = 200
# The router-only field that scopes the enclave's prompt cache: sealed with
# the body, never seen by anyone in between.
CACHE_SECRET_FIELD = "user_cache_secret"
REQUIRED_STEPS = ("verifyCode", "verifyEnclave", "compareMeasurements")


class ModelRetired(ProviderError):
    """The service no longer offers this end-to-end encrypted model.
    Attesting says nothing about it (the enclave is Tinfoil's router,
    whatever the model), so without this a retired model attested fine and
    its first message came back as a bare refusal."""

    def __init__(self, model: str, offered: Sequence[str] = (), **kwargs: Any) -> None:
        super().__init__(
            f"{model} is no longer offered by NanoGPT's Private Mode; choose another private model",
            **kwargs,
        )
        self.model = model
        # What the service's list does offer: what a scene may move to.
        self.offered = list(offered)


@dataclass(frozen=True)
class Verified:
    """What an attestation proved: the key to seal to, and the record."""

    hpke_public_key: str
    enclave: str
    attestation: Attestation


def origin_of(base_url: str) -> str:
    """The scheme, host and port of `base_url`, without a user name and
    password written into it: httpx (and requests, in Tinfoil's SDK) sends
    those as Basic credentials, over the key's header. The services take the
    API key, never those. (DA Toolkit's relay_url, Nov 2026.)"""
    parsed = urlparse(without_password(base_url))
    return f"{parsed.scheme}://{parsed.netloc}"


def _hardware(document: dict[str, Any]) -> str:
    kind = str(((document.get("enclaveMeasurement") or {}).get("measurement") or {}).get("type"))
    if "sev-snp" in kind:
        return "AMD SEV-SNP"
    if "tdx" in kind:
        return "Intel TDX"
    return kind


def windows_roots() -> None:
    """On Windows, let OpenSSL's default contexts trust certifi's roots too.

    Inside `SecureClient.verify`, Sigstore's TUF client opens its own urllib3
    pool, which trusts OpenSSL's defaults: on Windows, the certificates already
    in the Windows store. Windows fetches most roots only when something asks
    for them through its own TLS, so a fresh install had 16, without GTS Root
    R1, the root Sigstore's CDN chains to: every `private/` model was refused
    ("Failed to refresh TUF metadata"; Windows 11 26H2, Nov 10 2026). Chrome
    and Firefox keep their own roots and never fetch it for Windows, and a
    company may turn the fetching off. SSL_CERT_FILE adds certifi's bundle,
    the one httpx and the SDK's own requests already trust, to the Windows
    store's (the store still counts, a company's root included). The
    AppImage's AppRun does the same on Linux. Left alone when set, so the
    user's own bundle stands, and never on another system."""
    if sys.platform != "win32" or os.environ.get("SSL_CERT_FILE"):
        return
    import certifi  # noqa: PLC0415 - only where it is needed

    os.environ["SSL_CERT_FILE"] = certifi.where()


def sdk_verify(
    bundle_url: str,
    cache_secret: str,
    *,
    base_url: str | None = None,
) -> Verified:
    """Attest Tinfoil's router from the attestation bundle `bundle_url`
    serves (network; worker thread): NanoGPT's relay, or Tinfoil's own
    `atc.tinfoil.sh`. Never a router looked up by the SDK (its
    `get_router_address` fetches with plain urllib, outside the guard): a
    bundle names its enclave. `base_url`: the SDK client's own address,
    which it never uses here (it only verifies)."""
    # Imported here: the SDK pulls in sigstore and the OpenAI client, which
    # nothing else needs, and the app shouldn't pay for them at startup.
    from tinfoil import SecureClient
    from tinfoil.github import fetch_latest_release

    from sealedlore.providers import sdk_guard

    # Before it fetches anything: its requests, and Sigstore's TUF client,
    # follow a redirect anywhere, https to plain http included, and read a
    # reply whole. guard holds a redirect's target to the rule for any
    # address (require_https) and raises Unguarded where Sigstore's or TUF's
    # internals aren't as it expects: the attestation is refused, saying why
    # (providers/sdk_guard.py).
    sdk_guard.guard(is_local_host, check=require_https)
    windows_roots()

    # The secret is passed explicitly, or the SDK would create one in
    # ~/.tinfoil; this client only verifies, it never sends a request.
    client = SecureClient(
        base_url=(base_url or bundle_url) + "/",
        attestation_bundle_url=bundle_url,
        transport="ehbp",
        user_cache_secret=cache_secret,
    )
    truth = client.verify()
    document = client.get_verification_document().to_dict()
    steps = document.get("steps") or {}
    incomplete = [
        step for step in REQUIRED_STEPS if (steps.get(step) or {}).get("status") != "success"
    ]
    if document.get("securityVerified") is not True or incomplete or not truth.hpke_public_key:
        raise ProviderError(
            "the enclave's attestation was incomplete"
            + (f" ({', '.join(incomplete)} did not succeed)" if incomplete else "")
        )
    digest = document.get("releaseDigest") or truth.digest
    tag, latest, checked = str(document.get("releaseTag") or ""), "", False
    try:
        release = fetch_latest_release(ROUTER_REPO)
        checked = True
        if release.digest == digest:
            tag = tag or release.tag
        else:
            latest = release.tag
    except Exception:  # noqa: BLE001 - reported, never fatal
        pass
    return Verified(
        hpke_public_key=truth.hpke_public_key,
        enclave=client.enclave,
        attestation=attestation_record(
            enclave=client.enclave,
            hardware=_hardware(document),
            measurement=str(document.get("enclaveFingerprint") or ""),
            release_digest=digest,
            release_tag=tag,
            latest_release=latest,
            latest_checked=checked,
            hpke_public_key=truth.hpke_public_key,
        ),
    )


def attestation_record(
    *,
    enclave: str,
    hardware: str,
    measurement: str,
    release_digest: str,
    release_tag: str,
    latest_release: str,
    latest_checked: bool,
    hpke_public_key: str,
) -> Attestation:
    release = release_tag or f"release {release_digest[:12]}"
    if latest_release:
        currency = f"an older release than the latest ({latest_release})"
    elif latest_checked:
        currency = "the latest release"
    else:
        currency = "the latest release couldn't be looked up"
    measured = f"measured by {hardware}" if hardware else "measured by its hardware"
    detail = f"{enclave} runs {ROUTER_REPO} {release} ({currency}), {measured}"
    return Attestation(
        verified=True,
        encrypted=True,
        enclave=enclave,
        hardware=hardware,
        measurement=measurement,
        release_digest=release_digest,
        release_tag=release_tag,
        latest_release=latest_release,
        latest_checked=latest_checked,
        hpke_key_sha256=hashlib.sha256(hpke_public_key.encode()).hexdigest(),
        detail=detail,
    )


Verifier = Callable[[str, str], Verified]


class Enclave:
    """The attested enclave one provider and its siblings seal to.

    Shared by `sibling()` and `detached()`, so a background summary is sealed
    to the same attested key as the turn it follows. Thread-safe: a background
    chapter and a turn may ask for the key at once. `relay`: where the
    attestation bundle is fetched (NanoGPT's relay, or Tinfoil's ATC).
    """

    def __init__(
        self,
        relay: str,
        *,
        verifier: Verifier = sdk_verify,
        # A clock that runs on while the machine sleeps (time.monotonic stops):
        # after a night's suspend the attestation is hours old, not minutes.
        clock: Callable[[], float] = since_boot,
    ) -> None:
        self.relay = relay
        # Ours, not the relay's: see the module docstring. One per provider,
        # never written anywhere, so a memory-only chat leaves nothing.
        self.cache_secret = secrets.token_hex(32)
        self._verifier = verifier
        self._clock = clock
        self._lock = threading.Lock()
        self._verified: Verified | None = None
        self._at = 0.0

    @property
    def attestation(self) -> Attestation | None:
        return self._verified.attestation if self._verified is not None else None

    def current(self) -> Verified:
        """The attested key, attesting first if there is none or it has aged."""
        with self._lock:
            fresh = self._verified is not None and 0 <= self._clock() - self._at < MAX_AGE_SECONDS
            if not fresh:
                self._verified = None
                try:
                    self._verified = self._verifier(self.relay, self.cache_secret)
                except ProviderError:
                    raise
                except Exception as exc:  # noqa: BLE001 - the SDK's own errors
                    # They can quote the bundle, which came through the relay.
                    # A redirect the guard refused says why, where sigstore
                    # says only "Failed to refresh TUF metadata".
                    from sealedlore.providers.sdk_guard import not_followed

                    refused = not_followed(exc)
                    why = (
                        f" ({relay_said(refused, 500)})"
                        if refused and refused not in str(exc)
                        else ""
                    )
                    raise ProviderError(
                        "the private model's enclave couldn't be attested: "
                        f"{relay_said(str(exc), 500)}{why}; nothing was sent"
                    ) from exc
                self._at = self._clock()
            return self._verified

    def invalidate(self, verified: Verified | None = None) -> None:
        """Forget the key (it was rotated): the next request attests again."""
        with self._lock:
            if verified is None or self._verified is verified:
                self._verified = None


def _sealing_transport(public_key: str, route: str | None = None) -> httpx.BaseTransport:
    """EHBP over a transport that goes through `route`, the environment's
    proxy for the relay (`environment_proxy`), when there is one. httpx gives
    a client with a transport of its own no proxy from the environment, so
    the sealed request, alone of all the app's, went direct: from this
    machine's own address, Tor or a company's proxy notwithstanding, beside
    a preflight and an attestation that had gone through it."""
    from ehbp import EHBPTransport

    # A fresh connection per request, as the plain client does, so the trace
    # hook sees each socket and Stop can shut it down (the EHBP transport
    # passes the request's extensions through to this one, and the proxy's
    # own steps are traced too: http.traced_socket).
    try:
        inner = _NonEmptyBodyTransport(
            proxy=route, limits=httpx.Limits(max_keepalive_connections=0)
        )
    except ImportError as exc:  # a SOCKS proxy: httpx needs socksio for one
        raise ProviderError(
            "the environment's proxy is a SOCKS proxy, which SealedLore can't use "
            "(httpx's socksio isn't installed); nothing was sent"
        ) from exc
    return EHBPTransport.from_public_key_hex(public_key, inner=inner)


# The sealed transport for an attested key, through a proxy (or None).
SealingTransport = Callable[[str, str | None], httpx.BaseTransport]


class _NonEmptyBodyTransport(httpx.HTTPTransport):
    """EHBP passes a request with an empty body through as it came, unsealed:
    under it, nothing goes to the relay that EHBP didn't seal. (DA Vibe
    Manager's, DA Private Coder's and DA Toolkit's, Nov 2026.)"""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        from ehbp.protocol import ENCAPSULATED_KEY_HEADER

        if ENCAPSULATED_KEY_HEADER not in request.headers:  # httpx's headers ignore case
            raise ProviderError("refusing to send an unsealed request to the enclave")
        return super().handle_request(request)


class SealedEhbpProvider(OpenAICompatibleProvider):
    """An OpenAI-compatible chat sealed by EHBP to an attested enclave: the
    attestation, the sealing client (through the environment's proxy), the
    reply's seal, a stream's end, and the one resend after the enclave says
    our key has changed. Who carries it is the subclass's (`sealed_url`,
    `_prepare`, `_accept`, `_may_resend`, `relay_name`)."""

    sealed_replies = True
    # Whose words an unsealed refusal is, for `relay_said`.
    relay_name = "the relay"

    def __init__(
        self,
        config: ProviderConfig,
        *,
        enclave: Enclave,
        transport_factory: SealingTransport = _sealing_transport,
    ) -> None:
        super().__init__(config)
        self.enclave = enclave
        self._transport_factory = transport_factory
        self._client_key: tuple[str, str | None] | None = None
        self._verified: Verified | None = None

    def sibling(self) -> SealedEhbpProvider:
        if self._sibling is None:
            self._sibling = self._twin()
        self._sibling.reasoning_watch = self.reasoning_watch
        self._sibling.tee_watch = self.tee_watch
        return self._sibling

    def detached(self) -> SealedEhbpProvider:
        twin = self._twin()
        twin.reasoning_watch = self.reasoning_watch
        twin.tee_watch = self.tee_watch
        return twin

    def _twin(self) -> SealedEhbpProvider:
        raise NotImplementedError

    @property
    def sealed_url(self) -> str:
        """Where the sealed requests go (the client's address, for its proxy)."""
        raise NotImplementedError

    def _before_attest(self) -> None:
        """Refuse, before anything is fetched, an endpoint this client can't
        speak to."""

    def attest(self) -> Attestation:
        """Attest now (a worker thread): the window shows what was proved."""
        self._before_attest()
        return self.enclave.current().attestation

    # --- one request ---------------------------------------------------------------

    def _new_client(self, verified: Verified, route: str | None) -> httpx.Client:
        return make_client(
            self.sealed_url,
            transport=self._transport_factory(verified.hpke_public_key, route),
            timeout=httpx.Timeout(self.config.timeout_seconds),
            follow_redirects=False,
        )

    def _get_client(self) -> httpx.Client:
        verified = self.enclave.current()
        # The proxy is read at each request, as httpx reads it for every other
        # client of the app's, so the sealed one goes where they go.
        route = environment_proxy(self.sealed_url)
        wanted = (verified.hpke_public_key, route)
        if self._client is None or self._client_key != wanted:
            if self._client is not None:
                self._client.close()
            self._client = self._new_client(verified, route)
            self._client_key = wanted
        self._verified = verified
        return self._client

    def _accept(self, response: httpx.Response) -> None:
        from ehbp.protocol import RESPONSE_NONCE_HEADER

        # EHBP opens a sealed reply and refuses an unsealed success, but passes
        # any other status through as it came: one the relay made itself (a
        # 302 or a 304 with an answer in it) would pass for the enclave's, and
        # be marked as sealed. Only a success EHBP opened counts, and it keeps
        # the enclave's nonce header (DA Toolkit 27af7c7, DA Vibe Manager).
        if not 200 <= response.status_code < 300 or RESPONSE_NONCE_HEADER not in response.headers:
            raise ProviderError(
                f"the reply wasn't sealed by the enclave (HTTP {response.status_code}); refused",
                status_code=response.status_code,
            )

    def _http_error(self, url: str, response: httpx.Response) -> ProviderError:
        """What the relay wrote in refusing: never sealed, so never taken for
        the model's words (an instruction to the reader in it, say). Said as
        the relay's, on one line, without control or invisible characters
        (`relay_said`), and short; the words themselves are kept only for
        `_may_resend`, with whether the refusal came back the enclave's way."""
        from ehbp.protocol import RESPONSE_NONCE_HEADER

        error = super()._http_error(url, response)
        written = error.body or ""
        said = relay_said(written, RELAY_SAID_CHARS)
        labelled = ProviderError(
            f"{error}; {self.relay_name} said (unauthenticated): {said}" if said else str(error),
            status_code=error.status_code,
        )
        labelled.retry_after = error.retry_after
        labelled.relay_body = written
        labelled.through_enclave = RESPONSE_NONCE_HEADER in response.headers
        return labelled

    def _finish(self, finish_reason: str | None, saw_done: bool) -> None:
        if not (finish_reason and saw_done):
            # The proxy's private_stream_incomplete: whatever came may be partial.
            raise ProviderError("the encrypted reply ended before it was complete")

    def _may_resend(self, error: ProviderError) -> bool:
        """A refusal, before anything was run, that the request may be sent
        again for, after attesting again (beside the enclave's 422)."""
        return False

    def _stream(self, request: ChatRequest) -> Iterator[StreamEvent]:
        from ehbp import EHBPError, KeyConfigMismatchError

        for attempt in (0, 1):
            started = False
            try:
                for event in super()._stream(request):
                    started = True
                    yield event
                return
            except KeyConfigMismatchError as exc:
                if started or attempt:
                    # Its title is the relay's: the 422 that says so isn't sealed.
                    raise ProviderError(
                        f"the enclave refused our key twice: {relay_said(str(exc))}"
                    ) from exc
            except ProviderError as exc:
                if started or attempt or not self._may_resend(exc):
                    raise
            except EHBPError as exc:
                raise ProviderError(f"the encrypted reply couldn't be opened: {exc}") from exc
            self.enclave.invalidate(self._verified)
