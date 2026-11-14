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
   one is reported (`latest_release`), not refused: rollouts lag. The SDK's
   fetches, and those of the Sigstore library it verifies with (its TUF
   trust root included), follow no redirect off https, nor one from the
   internet into this computer or its network; where they can't be held to
   that, nothing is attested (providers/sdk_guard.py).
2. **Preflight**: nano-gpt checks balance and reserves a charge, and says the
   upstream model's name. It issues a cache scope; we send its sha256 as the
   header nano-gpt wants, but the body carries **our own** random secret, so
   no one else, nano-gpt included, can time our prompt cache (measured: a
   fresh secret gets 0 cached tokens on a prompt another secret had cached).
3. **Seal** the body with EHBP (`ehbp.EHBPTransport`: HPKE X25519, AES-GCM)
   to the attested key, and decrypt the reply. Only a success the transport
   opened is taken (`_accept`): EHBP refuses a 2xx that isn't sealed but
   passes any other status through as it came, so a reply the relay made
   itself (a 302 with an answer in it) is refused here, as is one nano-gpt
   doesn't mark as Private Mode, and a stream that ends without its final
   chunk. What the relay writes in an error is never sealed, so it is said as
   the relay's (`_http_error`). Nothing is ever sent unsealed and nothing
   falls back to the plain endpoint: `OpenAICompatibleProvider` refuses a
   `private/*` model outright.

The attestation is redone before a request once it is `MAX_AGE_SECONDS` old
(the proxy's five minutes: laptop sleep, a key rotated underneath us), on a
clock that runs on while the machine sleeps (`dcap.since_boot`), and once more
when the enclave says the key has changed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx

from sealedlore.models.config import (
    ProviderConfig,
    require_https,  # noqa: F401 - the rule the SDK's redirects are held to, by this name
    shown,
)
from sealedlore.providers.base import ChatRequest, ProviderError, relay_said
from sealedlore.providers.http import make_client, reply_json
from sealedlore.providers.openai_compat import OpenAICompatibleProvider
from sealedlore.providers.private_catalog import fetch_private_ids, offered, offers_private_mode

# The parts that are the same whoever relays the ciphertext live in
# providers/sealed_ehbp.py; these names are kept here, where NanoGPT's client
# and its tests have always found them.
from sealedlore.providers.sealed_ehbp import (  # noqa: F401
    CACHE_SECRET_FIELD,
    MAX_AGE_SECONDS,
    RELAY_SAID_CHARS,
    REQUIRED_STEPS,
    ROUTER_REPO,
    Enclave,
    ModelRetired,
    SealedEhbpProvider,
    SealingTransport,
    Verified,
    Verifier,
    _hardware,
    _NonEmptyBodyTransport,
    _sealing_transport,
    attestation_record,
    origin_of,
    sdk_verify,
    windows_roots,
)
from sealedlore.providers.tee import is_private_mode
from sealedlore.providers.wire import to_wire_messages

RELAY_PATH = "/api/v1/private/tinfoil"
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


def relay_url(base_url: str) -> str:
    return origin_of(base_url) + RELAY_PATH


def not_nanogpt(base_url: str) -> str | None:
    """Why a `private/` model can't be sent through `base_url`, or None: it is
    NanoGPT's Private Mode, and only NanoGPT's relay speaks it. Another
    endpoint would be handed the key and the preflight, and its attestation
    bundle asked for, for a model it doesn't have. (DA Toolkit's, Nov 2026.)"""
    if offers_private_mode(base_url):
        return None
    return (
        f"{shown(base_url)} doesn't offer NanoGPT's Private Mode: an end-to-end encrypted "
        "(private/) model is sent only through NanoGPT, so nothing was sent. Choose it on "
        "the NanoGPT endpoint (https://nano-gpt.com/api/v1)."
    )


def tinfoil_verify(relay: str, cache_secret: str) -> Verified:
    """Attest Tinfoil's router through nano-gpt's relay (network; worker thread)."""
    return sdk_verify(relay, cache_secret)


class PrivateModeProvider(SealedEhbpProvider):
    """NanoGPT's relay: its preflight, its headers, its per-family body
    shapes and its list of models, around the sealed client."""

    relay_name = "NanoGPT's relay"

    def __init__(
        self,
        config: ProviderConfig,
        *,
        enclave: Enclave | None = None,
        transport_factory: SealingTransport = _sealing_transport,
        preflight_client: httpx.Client | None = None,
    ) -> None:
        super().__init__(
            config,
            enclave=enclave or Enclave(relay_url(config.base_url)),
            transport_factory=transport_factory,
        )
        self._preflight_client = preflight_client

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
    def sealed_url(self) -> str:
        return self.relay

    @property
    def endpoint(self) -> str:
        return self.relay + "/v1/chat/completions"

    def _on_nanogpt(self) -> None:
        """Refuse, before anything is fetched or sent, an endpoint that isn't
        NanoGPT's (`not_nanogpt`)."""
        refusal = not_nanogpt(self.config.base_url)
        if refusal:
            raise ProviderError(refusal)

    def _before_attest(self) -> None:
        self._on_nanogpt()

    def still_offered(self, model: str) -> bool | None:
        """Whether nano-gpt's Private Mode list still has `model` (public: no
        key sent). None when the list couldn't be read: nothing is known, and
        the preflight still refuses a model that's gone."""
        self._on_nanogpt()
        ids = fetch_private_ids(self.config.base_url, client=self._preflight_client)
        return offered(model, ids)

    def check_offered(self, model: str) -> None:
        """Raise `ModelRetired`, with what is offered instead, if nano-gpt has
        retired `model` (a worker thread)."""
        self._on_nanogpt()
        ids = fetch_private_ids(self.config.base_url, client=self._preflight_client)
        if offered(model, ids) is False:
            raise ModelRetired(model, ids)

    def fetch_model_prices(self) -> dict[str, dict[str, Any]]:
        """The endpoint's public model list, on a plain client: nothing sealed."""
        plain = OpenAICompatibleProvider(self.config)
        try:
            return plain.fetch_model_prices()
        finally:
            plain.close()

    def fetch_reasoning_defaults(self) -> dict[str, dict[str, Any]] | None:
        """As the plain provider: a public listing, nothing sealed."""
        plain = OpenAICompatibleProvider(self.config)
        try:
            return plain.fetch_reasoning_defaults()
        finally:
            plain.close()

    # --- one request ---------------------------------------------------------------

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
            if self.still_offered(model) is False:
                # Every path's backstop (a resumed scene, a summary, Test speed…):
                # say why, not just that it was refused.
                raise ModelRetired(model, status_code=response.status_code)
            # The relay's own words, said as its (error_detail would add them
            # to a dialog as they came).
            said = relay_said(response.text, RELAY_SAID_CHARS)
            raise ProviderError(
                f"nano-gpt refused the Private Mode request (HTTP {response.status_code})"
                + (f"; NanoGPT's relay said (unauthenticated): {said}" if said else ""),
                status_code=response.status_code,
            )
        try:
            data = reply_json(response)
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
        self._on_nanogpt()
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
        super()._accept(response)
        # And nano-gpt saying the request went the private way at all.
        if response.headers.get("x-nanogpt-private-mode") != "tinfoil":
            raise ProviderError("the reply didn't come back through Private Mode; refused")

    def _may_resend(self, error: ProviderError) -> bool:
        return _missing_seal(error)


def _missing_seal(error: ProviderError) -> bool:
    """nano-gpt refused the request as unsealed before sending it anywhere: the
    proxy's one safe retry (after attesting again). Not when the refusal carries
    the enclave's nonce: it came back the encrypted way, so the request may have
    been run (and paid for) already (the proxy's isMissingEncryptedBodyHeaderResponse)."""
    if getattr(error, "through_enclave", False):
        return False
    written = getattr(error, "relay_body", None) or error.body or ""
    return error.status_code == 400 and "missing_encrypted_body_header" in written


# --- the request body ------------------------------------------------------------------

_GLM_EFFORT = {
    "none": "low",
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "max",
    "max": "max",
}


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
        # Low effort (a TEE model's "as low as possible", engine/reasoning.py) means
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
