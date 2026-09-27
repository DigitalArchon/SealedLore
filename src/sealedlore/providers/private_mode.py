"""nano-gpt Private Mode: end-to-end encrypted chat with an attested enclave.

nano-gpt's `private/*` models run in Tinfoil's enclaves. Unlike the `TEE/*`
models (providers/tee.py), whose prompts pass nano-gpt's gateway in the clear,
a Private Mode request body is sealed on this machine to a key only the
attested enclave holds, and the reply comes back sealed the same way. nano-gpt
relays ciphertext: it sees the account, the model, timing, sizes and usage.

nano-gpt ships this as a Node proxy (`@nanogpt/private-mode`), run with
`npx …@latest`: the code that holds the plaintext would come from, and be
updated by, the party the encryption is meant to keep out. So this client
does what the proxy does with Tinfoil's own Python SDK, pinned by hash, and
runs no code from nano-gpt (Sept 2026):

1. **Attest** (`Enclave.current`): Tinfoil's `SecureClient.verify` fetches
   the attestation bundle through nano-gpt's relay and checks it against the
   hardware vendors' roots (AMD SEV-SNP or Intel TDX) and Sigstore's record
   of the router release (`tinfoilsh/confidential-model-router`). The report
   binds the enclave's HPKE key. Through a relay the SDK accepts any genuine
   release, so the latest one is looked up from Tinfoil directly and an older
   one is reported (`latest_release`), not refused: rollouts lag.
2. **Preflight**: nano-gpt checks balance and reserves a charge, and says the
   upstream model's name. It issues a cache scope; we send its sha256 as the
   header nano-gpt wants, but the body carries **our own** random secret, so
   no one else, nano-gpt included, can time our prompt cache (measured: a
   fresh secret gets 0 cached tokens on a prompt another secret had cached).
3. **Seal** the body with EHBP (`ehbp.EHBPTransport`: HPKE X25519, AES-GCM)
   to the attested key, and decrypt the reply. A 2xx reply that isn't sealed
   is refused by the transport, so is one nano-gpt doesn't mark as Private
   Mode, and a stream that ends without its final chunk. Nothing is ever sent
   unsealed and nothing falls back to the plain endpoint: `OpenAICompatible
   Provider` refuses a `private/*` model outright.

The attestation is redone before a request once it is `MAX_AGE_SECONDS` old
(the proxy's five minutes: laptop sleep, a key rotated underneath us), and
once more when the enclave says the key has changed.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from sealedlore.models.config import ProviderConfig
from sealedlore.models.private import Attestation
from sealedlore.providers.base import ChatRequest, ProviderError, StreamEvent
from sealedlore.providers.http import make_client
from sealedlore.providers.openai_compat import OpenAICompatibleProvider
from sealedlore.providers.tee import is_private_mode
from sealedlore.providers.wire import to_wire_messages

RELAY_PATH = "/api/v1/private/tinfoil"
ROUTER_REPO = "tinfoilsh/confidential-model-router"
MAX_AGE_SECONDS = 5 * 60
PREFLIGHT_TIMEOUT_SECONDS = 30.0
ENCLAVE_URL_HEADER = "X-Tinfoil-Enclave-Url"

# The request fields Tinfoil's router takes (the proxy's
# PRIVATE_TINFOIL_CHAT_COMPLETION_BODY_FIELDS, less tools and search, which
# SealedLore never sends). Anything else is dropped before sealing.
ALLOWED_FIELDS = frozenset(
    {
        "chat_template_kwargs",
        "frequency_penalty",
        "logit_bias",
        "max_tokens",
        "messages",
        "model",
        "presence_penalty",
        "reasoning_effort",
        "response_format",
        "seed",
        "stop",
        "stream",
        "stream_options",
        "temperature",
        "top_p",
        "user",
    }
)
# The router-only field that scopes the enclave's prompt cache: sealed with
# the body, never seen by nano-gpt.
CACHE_SECRET_FIELD = "user_cache_secret"


@dataclass(frozen=True)
class Verified:
    """What an attestation proved: the key to seal to, and the record."""

    hpke_public_key: str
    enclave: str
    attestation: Attestation


def origin_of(base_url: str) -> str:
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def relay_url(base_url: str) -> str:
    return origin_of(base_url) + RELAY_PATH


def _hardware(document: dict[str, Any]) -> str:
    kind = str(((document.get("enclaveMeasurement") or {}).get("measurement") or {}).get("type"))
    if "sev-snp" in kind:
        return "AMD SEV-SNP"
    if "tdx" in kind:
        return "Intel TDX"
    return kind


REQUIRED_STEPS = ("verifyCode", "verifyEnclave", "compareMeasurements")


def tinfoil_verify(relay: str, cache_secret: str) -> Verified:
    """Attest Tinfoil's router through nano-gpt's relay (network; worker thread)."""
    # Imported here: the SDK pulls in sigstore and the OpenAI client, which
    # nothing else needs, and the app shouldn't pay for them at startup.
    from tinfoil import SecureClient
    from tinfoil.github import fetch_latest_release

    # The secret is passed explicitly, or the SDK would create one in
    # ~/.tinfoil; this client only verifies, it never sends a request.
    client = SecureClient(
        base_url=relay + "/",
        attestation_bundle_url=relay,
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
    chapter and a turn may ask for the key at once.
    """

    def __init__(
        self,
        relay: str,
        *,
        verifier: Verifier = tinfoil_verify,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.relay = relay
        # Ours, not nano-gpt's: see the module docstring. One per provider,
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
                    raise ProviderError(
                        f"the private model's enclave couldn't be attested: {exc}; nothing was sent"
                    ) from exc
                self._at = self._clock()
            return self._verified

    def invalidate(self, verified: Verified | None = None) -> None:
        """Forget the key (it was rotated): the next request attests again."""
        with self._lock:
            if verified is None or self._verified is verified:
                self._verified = None


def _sealing_transport(public_key: str) -> httpx.BaseTransport:
    from ehbp import EHBPTransport

    # A fresh connection per request, as the plain client does, so the trace
    # hook sees each socket and Stop can shut it down (the EHBP transport
    # passes the request's extensions through to this one).
    inner = httpx.HTTPTransport(limits=httpx.Limits(max_keepalive_connections=0))
    return EHBPTransport.from_public_key_hex(public_key, inner=inner)


class PrivateModeProvider(OpenAICompatibleProvider):
    sealed_replies = True

    def __init__(
        self,
        config: ProviderConfig,
        *,
        enclave: Enclave | None = None,
        transport_factory: Callable[[str], httpx.BaseTransport] = _sealing_transport,
        preflight_client: httpx.Client | None = None,
    ) -> None:
        super().__init__(config)
        self.enclave = enclave or Enclave(relay_url(config.base_url))
        self._transport_factory = transport_factory
        self._preflight_client = preflight_client
        self._client_key: str | None = None
        self._verified: Verified | None = None

    def sibling(self) -> PrivateModeProvider:
        if self._sibling is None:
            self._sibling = self._twin()
        return self._sibling

    def detached(self) -> PrivateModeProvider:
        return self._twin()

    def _twin(self) -> PrivateModeProvider:
        return PrivateModeProvider(
            self.config,
            enclave=self.enclave,
            transport_factory=self._transport_factory,
            preflight_client=self._preflight_client,
        )

    @property
    def relay(self) -> str:
        return self.enclave.relay

    @property
    def endpoint(self) -> str:
        return self.relay + "/v1/chat/completions"

    def attest(self) -> Attestation:
        """Attest now (a worker thread): the window shows what was proved."""
        return self.enclave.current().attestation

    def fetch_model_prices(self) -> dict[str, dict[str, Any]]:
        """The endpoint's public model list, on a plain client: nothing sealed."""
        plain = OpenAICompatibleProvider(self.config)
        try:
            return plain.fetch_model_prices()
        finally:
            plain.close()

    # --- one request ---------------------------------------------------------------

    def _get_client(self) -> httpx.Client:
        verified = self.enclave.current()
        if self._client is None or self._client_key != verified.hpke_public_key:
            if self._client is not None:
                self._client.close()
            self._client = make_client(
                self.relay,
                transport=self._transport_factory(verified.hpke_public_key),
                timeout=httpx.Timeout(self.config.timeout_seconds),
                follow_redirects=False,
            )
            self._client_key = verified.hpke_public_key
        self._verified = verified
        return self._client

    def _preflight(self, model: str, body_bytes: int) -> tuple[str, str]:
        """nano-gpt's check before a sealed request: (cache scope, upstream model)."""
        client = self._preflight_client or make_client(self.relay)
        try:
            response = client.post(
                self.relay + "/preflight",
                json={"model": model, "requestBodyBytes": body_bytes},
                headers=self._headers(),
                timeout=PREFLIGHT_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"nano-gpt's Private Mode check failed: {exc}") from exc
        finally:
            if self._preflight_client is None:
                client.close()
        if response.status_code >= 400:
            error = ProviderError(
                f"nano-gpt refused the Private Mode request (HTTP {response.status_code})",
                status_code=response.status_code,
                body=response.text,
            )
            raise error
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError("nano-gpt's Private Mode check wasn't JSON") from exc
        scope = str(data.get("cacheScope") or "").strip()
        upstream = str(data.get("upstreamModel") or "").strip()
        if len(scope) != 64 or not upstream:
            raise ProviderError("nano-gpt's Private Mode check didn't say where to send it")
        return scope, upstream

    def _prepare(self, request: ChatRequest) -> tuple[str, dict[str, Any], dict[str, str]]:
        if not is_private_mode(request.model):
            raise ProviderError(f"{request.model} isn't a Private Mode model; nothing was sent")
        # Attested first: nothing, not even nano-gpt's charge, before the
        # enclave has proved what it is.
        self._get_client()
        verified = self._verified
        assert verified is not None
        payload = self.build_payload(request)
        size = len(json.dumps(payload).encode("utf-8"))
        scope, upstream = self._preflight(request.model, size)
        body = shape_body(payload, request, upstream)
        body[CACHE_SECRET_FIELD] = self.enclave.cache_secret
        headers = {
            **self._headers(),
            "Accept": "text/event-stream",
            "x-nanogpt-private-model": request.model,
            "x-nanogpt-private-stream": "true",
            "x-nanogpt-private-cache-scope": hashlib.sha256(scope.encode()).hexdigest(),
            "x-query-source": "api",
            ENCLAVE_URL_HEADER: f"https://{verified.enclave}",
        }
        return self.endpoint, body, headers

    def _accept(self, response: httpx.Response) -> None:
        # The EHBP transport has refused a success that wasn't sealed; this is
        # nano-gpt saying the request went the private way at all.
        if response.headers.get("x-nanogpt-private-mode") != "tinfoil":
            raise ProviderError("the reply didn't come back through Private Mode; refused")

    def _finish(self, finish_reason: str | None, saw_done: bool) -> None:
        if not (finish_reason and saw_done):
            # The proxy's private_stream_incomplete: whatever came may be partial.
            raise ProviderError("the encrypted reply ended before it was complete")

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
                    raise ProviderError(f"the enclave refused our key twice: {exc}") from exc
            except ProviderError as exc:
                if started or attempt or not _missing_seal(exc):
                    raise
            except EHBPError as exc:
                raise ProviderError(f"the encrypted reply couldn't be opened: {exc}") from exc
            self.enclave.invalidate(self._verified)


def _missing_seal(error: ProviderError) -> bool:
    """nano-gpt refused the request as unsealed before sending it anywhere: the
    proxy's one safe retry (after attesting again)."""
    return error.status_code == 400 and "missing_encrypted_body_header" in (error.body or "")


# --- the request body ------------------------------------------------------------------

_GLM_EFFORT = {"low": "low", "minimal": "low", "medium": "high", "high": "high", "max": "max"}


def shape_body(payload: dict[str, Any], request: ChatRequest, upstream: str) -> dict[str, Any]:
    """The body the router takes, as the proxy shapes it for each model family
    (its requestTransforms.js), for the fields SealedLore sends."""
    body = dict(payload)
    # Plain text messages: cache_control blocks mean nothing to vLLM.
    body["messages"] = to_wire_messages(request.messages, use_cache_control=False)
    body["model"] = upstream
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}
    reasoning = body.pop("reasoning", None)
    effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
    enabled = bool(reasoning.get("enabled")) if isinstance(reasoning, dict) else False
    thinking_suffix = request.model.endswith(":thinking")
    kwargs = dict(body.get("chat_template_kwargs") or {})
    if upstream.startswith("glm-5"):
        # Always reasons; the effort goes at the top level.
        kwargs["thinking"] = True
        if effort:
            body["reasoning_effort"] = _GLM_EFFORT.get(str(effort).lower(), "high")
    elif upstream.startswith("deepseek-v4"):
        kwargs["thinking"] = enabled or thinking_suffix
        if kwargs["thinking"] and effort:
            kwargs["reasoning_effort"] = (
                "low"
                if str(effort).lower() in ("low", "minimal")
                else ("high" if str(effort).lower() == "medium" else "xhigh")
            )
    elif upstream.startswith("gemma"):
        # Low effort (engine/archival.LIGHT_REASONING, for summaries) means
        # as little as the model allows: for Gemma, none unless it's the
        # :thinking model.
        light = str(effort).lower() in ("low", "minimal")
        kwargs["enable_thinking"] = thinking_suffix or (enabled and not light)
    elif upstream.startswith("kimi"):
        # Kimi K3's template fixes its own sampling and always reasons.
        for field in ("temperature", "top_p", "frequency_penalty", "presence_penalty"):
            body.pop(field, None)
        if effort:
            body["reasoning_effort"] = _GLM_EFFORT.get(str(effort).lower(), "high")
    if kwargs:
        body["chat_template_kwargs"] = kwargs
    return {key: value for key, value in body.items() if key in ALLOWED_FIELDS}
