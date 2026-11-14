"""Tinfoil's own API, end-to-end encrypted, with no relay in between.

Preliminary: built and tested offline against captures of Tinfoil's public
listing and attestation bundle (Nov 14 2026), never yet run with an account.

The same router as NanoGPT's `private/` models (providers/private_mode.py),
reached directly: `https://inference.tinfoil.sh/v1`. What differs from the
relay (design: SealedLoreDocs design/e2ee-providers.md §1):

- **Attestation** from Tinfoil's own bundle service, `https://atc.tinfoil.sh`
  (`direct_verify`): the SDK's bundle path, under the same guard, never its
  router lookup (`get_router_address`, plain urllib). The bundle names the
  enclave; it must be this endpoint's host, or nothing is attested: the key
  and the headers go only where the key was bound. The whole attempt has
  `ATTEMPT_SECONDS`, as every attestation has.
- **The TLS key is pinned** (`TlsPin`): the router terminates TLS inside the
  enclave, and the attestation binds its key. As the handshake with the host
  completes, before a byte of the request leaves, the certificate's key must
  be the attested one, behind a proxy's tunnel too; and no request's headers
  go out on a connection whose key wasn't checked. A mismatch sends nothing
  and attests again next time.
- **Requests**: `POST {base}/chat/completions`, sealed by EHBP, with the key
  and nothing else of ours in the clear; no preflight, no relay's headers. A
  reply counts only as a 2xx EHBP opened, a stream only with its finish
  reason and `[DONE]`.
- **The listing** (`GET {base}/models`) is the one unsealed request to the
  host, and goes without the key. Its chat models only, their prices per
  million tokens (a reply's cost is estimated from them: Tinfoil reports none
  in a stream), context windows and reasoning efforts. `reasoning_effort` is
  sent only as one the model lists (anything else is a 400). A model gone
  from it is `ModelRetired`.

What isn't proved here: that the router hands a request only to attested
model enclaves (that is its published code, whose build the attestation
checks, not something this app checks), and what Tinfoil's servers see: the
account, the model, timing, sizes and cost, never the text.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable, Iterator
from functools import partial
from typing import Any
from urllib.parse import urlparse

import httpx

from sealedlore.models.config import ProviderConfig, require_https, shown
from sealedlore.models.generation import EFFORT_SCALE
from sealedlore.providers.base import ChatRequest, ProviderError, StreamEvent
from sealedlore.providers.http import make_client, reply_json
from sealedlore.providers.privacy import TINFOIL
from sealedlore.providers.sealed_ehbp import (
    CACHE_SECRET_FIELD,
    Enclave,
    ModelRetired,
    SealedEhbpProvider,
    SealingTransport,
    Verified,
    _sealing_transport,
    sdk_verify,
)
from sealedlore.providers.wire import to_wire_messages
from sealedlore.threads import BackgroundThread

# Tinfoil's attestation bundle service: one GET of /attestation is the whole
# trust root, checked by the SDK (design §1.1).
ATC_URL = "https://atc.tinfoil.sh"
SERVICE_NAME = "Tinfoil"
# A whole attestation, the SDK's every fetch in it, as the standard gives one.
ATTEMPT_SECONDS = 180.0
LISTING_TIMEOUT_SECONDS = 30.0
# The listing is read again after this long (a model retired meanwhile).
LISTING_MAX_AGE_SECONDS = 10 * 60
# The fields a request may carry, the router's for a direct client: NanoGPT's
# list (private_mode.ALLOWED_FIELDS) less `chat_template_kwargs`, which is the
# relay's own per-family shaping. The cache secret is added after.
DIRECT_FIELDS = frozenset(
    {
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
PIN_MISMATCH = (
    "Tinfoil's TLS key isn't the attested one: nothing was sent. The enclave is "
    "attested again before the next try."
)
NOT_PINNED = (
    "the connection to Tinfoil wasn't checked against the attested TLS key, so nothing was sent"
)


def _host(url: str) -> str:
    try:
        return (urlparse(str(url or "").strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


# --- the attestation -------------------------------------------------------------------


def within(seconds: float, work: Callable[[], Verified]) -> Verified:
    """`work` on a thread of its own, given `seconds`: what it returns, or
    what it raised, or a refusal once the time is up (the thread is left to
    finish on its own; nothing it brings is used)."""
    done = threading.Event()
    result: list[Verified] = []
    failure: list[BaseException] = []

    def run() -> None:
        try:
            result.append(work())
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller
            failure.append(exc)
        finally:
            done.set()

    BackgroundThread(target=run, name="tinfoil-attest").start()
    if not done.wait(seconds):
        raise ProviderError(
            f"Tinfoil's router wasn't attested within {seconds / 60:g} minutes; nothing was sent"
        )
    if failure:
        raise failure[0]
    return result[0]


def direct_verify(
    host: str,
    bundle_url: str,
    cache_secret: str,
    *,
    seconds: float = ATTEMPT_SECONDS,
    verify: Callable[..., Verified] = sdk_verify,
) -> Verified:
    """Attest Tinfoil's router from its own bundle service (network; worker
    thread), for the endpoint at `host`: refused unless the bundle's enclave,
    as the SDK verified it (its certificate's names included), is `host`,
    and the attestation names a TLS key to pin."""
    verified = within(
        seconds,
        lambda: verify(bundle_url, cache_secret, base_url=f"https://{host}", service=TINFOIL),
    )
    attested = _host(f"https://{verified.enclave}")
    if attested != host:
        raise ProviderError(
            f"Tinfoil's attestation is for {attested or 'no host'}, not {host}, this "
            "endpoint's address: its key goes only where it was saved for, so nothing was "
            f"sent. Set the endpoint's address to https://{attested}/v1 if that is Tinfoil's."
        )
    key = verified.tls_public_key
    if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise ProviderError(
            "Tinfoil's attestation names no TLS key to check the connection against, so "
            "nothing was sent"
        )
    return verified


# --- the TLS pin -----------------------------------------------------------------------


class TlsPinMismatch(ProviderError):
    """The host's TLS key isn't the attested one, or wasn't checked."""


def spki_sha256(certificate_der: bytes) -> str:
    """The sha256 of a certificate's key (its SubjectPublicKeyInfo, DER), in
    hex: what Tinfoil's attestation binds, as its SDK compares it."""
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    certificate = x509.load_der_x509_certificate(certificate_der)
    key = certificate.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(key).hexdigest()


class TlsPin:
    """For httpcore's `trace` extension: the TLS key of `host` must be
    `expected`, checked as each handshake with it completes, before anything
    is written on the connection; and no request's headers go out until one
    was (a CONNECT to a proxy excepted: it carries none of ours). Behind a
    proxy the handshake with the host is the one inside the tunnel; the
    proxy's own (an https proxy) is told apart by the name it was made for."""

    def __init__(self, host: str, expected: str) -> None:
        self.host = host.lower().rstrip(".")
        self.expected = expected.lower()
        self.checked = False
        self._making: str | None = None

    def __call__(self, event_name: str, info: dict[str, Any]) -> None:
        if event_name.endswith(".start_tls.started"):
            self._making = str(info.get("server_hostname") or "").lower().rstrip(".")
        elif event_name.endswith(".start_tls.complete"):
            name, self._making = self._making, None
            if name and name != self.host:
                return  # a proxy's own TLS: the host's is made inside it
            self._check(info.get("return_value"))
        elif event_name.endswith(".send_request_headers.started"):
            request = info.get("request")
            if getattr(request, "method", b"") == b"CONNECT":
                return
            if not self.checked:
                raise TlsPinMismatch(NOT_PINNED)

    def _check(self, stream: Any) -> None:
        extra = getattr(stream, "get_extra_info", None)
        ssl_object = extra("ssl_object") if callable(extra) else None
        try:
            certificate = ssl_object.getpeercert(True) if ssl_object is not None else None
            found = spki_sha256(certificate) if certificate else ""
        except Exception:  # noqa: BLE001 - anything unreadable is a mismatch
            found = ""
        if found != self.expected:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - going anyway
                    pass
            raise TlsPinMismatch(PIN_MISMATCH)
        self.checked = True


# --- the listing -----------------------------------------------------------------------


def listing_entries(data: Any) -> dict[str, dict[str, Any]]:
    """Tinfoil's `/v1/models` as the app's listing entries (engine/catalog.py,
    engine/pricing.py, engine/reasoning.py read them): its chat models only,
    each with its prices per million tokens, context window and the efforts
    it takes (`reasoning_efforts`, Tinfoil's `supported_efforts`)."""
    items = data.get("data") if isinstance(data, dict) else None
    entries: dict[str, dict[str, Any]] = {}
    for item in items or []:
        if not isinstance(item, dict) or item.get("type") != "chat":
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        efforts = item.get("supported_efforts")
        efforts = [e for e in efforts if e in EFFORT_SCALE] if isinstance(efforts, list) else []
        entry: dict[str, Any] = {
            "id": model_id,
            "name": item.get("name") if isinstance(item.get("name"), str) else model_id,
            "description": item.get("description")
            if isinstance(item.get("description"), str)
            else "",
            "owned_by": "Tinfoil",
            "reasoning_efforts": efforts,
            "capabilities": {
                "reasoning": item.get("reasoning") is True,
                "vision": item.get("multimodal") is True,
            },
        }
        context = item.get("context_window")
        if isinstance(context, int) and not isinstance(context, bool) and context > 0:
            entry["context_length"] = context
        pricing = item.get("pricing")
        if isinstance(pricing, dict):
            prompt = _price(pricing.get("inputTokenPricePer1M"))
            completion = _price(pricing.get("outputTokenPricePer1M"))
            if prompt is not None and completion is not None:
                prices: dict[str, Any] = {
                    "prompt": prompt,
                    "completion": completion,
                    "unit": "per_million_tokens",
                }
                cached = _price(pricing.get("cachedInputTokenPricePer1M"))
                if cached is not None:
                    prices["input_cache_read"] = cached
                entry["pricing"] = prices
        entries[model_id] = entry
    return entries


def _price(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


class Listing:
    """Tinfoil's listing, fetched without the key and kept a while; shared
    by a provider and its siblings."""

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = base_url.rstrip("/") + "/models"
        self._client = client
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] | None = None
        self._at = 0.0

    def fetch(self) -> dict[str, dict[str, Any]]:
        """The chat models; ProviderError when the listing can't be read."""
        with self._lock:
            fresh = self._entries is not None and 0 <= self._clock() - self._at < (
                LISTING_MAX_AGE_SECONDS
            )
            if fresh:
                return dict(self._entries or {})
        client = self._client or make_client(self.url, follow_redirects=False)
        try:
            # No key, no header of ours but what httpx sends: it is public.
            response = client.get(self.url, timeout=LISTING_TIMEOUT_SECONDS)
            if response.status_code >= 300:
                raise ProviderError(
                    f"Tinfoil's model list answered HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            entries = listing_entries(reply_json(response))
        except httpx.HTTPError as exc:
            raise ProviderError(f"Tinfoil's model list couldn't be read: {exc}") from exc
        except ValueError as exc:
            raise ProviderError("Tinfoil's model list wasn't JSON") from exc
        finally:
            if self._client is None:
                client.close()
        with self._lock:
            self._entries, self._at = entries, self._clock()
        return dict(entries)

    def known(self) -> dict[str, dict[str, Any]] | None:
        """The listing as last read, fetched once if never; None when it
        can't be read."""
        try:
            return self.fetch()
        except ProviderError:
            return None


# --- the request -----------------------------------------------------------------------


def chosen_effort(wanted: str | None, efforts: list[str] | None) -> str | None:
    """The `reasoning_effort` to send: `wanted` if the model lists it, else
    the nearest it lists (the higher on a tie; "none" only when asked for),
    and nothing when it lists none or the listing is unknown: a value the
    model doesn't list is refused with a 400."""
    if not wanted or not efforts:
        return None
    if wanted in efforts:
        return wanted
    if wanted == "none" or wanted not in EFFORT_SCALE:
        return None
    listed = [e for e in efforts if e in EFFORT_SCALE and e != "none"]
    if not listed:
        return None
    target = EFFORT_SCALE.index(wanted)
    return min(listed, key=lambda e: (abs(EFFORT_SCALE.index(e) - target), -EFFORT_SCALE.index(e)))


def direct_body(
    payload: dict[str, Any], request: ChatRequest, efforts: list[str] | None
) -> dict[str, Any]:
    """The body sealed to the router: its own fields, the messages as plain
    text (cache_control blocks mean nothing to vLLM), a stream with its
    usage, and the effort only as the model lists one."""
    body = dict(payload)
    body["messages"] = to_wire_messages(request.messages, use_cache_control=False)
    body["model"] = request.model
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}
    reasoning = body.pop("reasoning", None)
    wanted = reasoning.get("effort") if isinstance(reasoning, dict) else None
    effort = chosen_effort(str(wanted) if wanted else None, efforts)
    if effort:
        body["reasoning_effort"] = effort
    else:
        body.pop("reasoning_effort", None)
    return {key: value for key, value in body.items() if key in DIRECT_FIELDS}


class TinfoilDirect(SealedEhbpProvider):
    relay_name = "Tinfoil"

    def __init__(
        self,
        config: ProviderConfig,
        *,
        enclave: Enclave | None = None,
        transport_factory: SealingTransport = _sealing_transport,
        listing: Listing | None = None,
    ) -> None:
        base = config.base_url.strip().rstrip("/")
        if not urlparse(base).path.strip("/"):
            base += "/v1"  # Tinfoil's API is under /v1
        self.base = base
        self.host = _host(base)
        super().__init__(
            config,
            enclave=enclave or Enclave(ATC_URL, verifier=partial(direct_verify, self.host)),
            transport_factory=transport_factory,
        )
        self.listing = listing or Listing(base)
        self._pin: TlsPin | None = None

    def _twin(self) -> TinfoilDirect:
        return TinfoilDirect(
            self.config,
            enclave=self.enclave,
            transport_factory=self._transport_factory,
            listing=self.listing,
        )

    @property
    def sealed_url(self) -> str:
        return self.base

    @property
    def endpoint(self) -> str:
        return self.base + "/chat/completions"

    def _before_attest(self) -> None:
        """Only https, and to a host: the attestation names the host the key
        may go to."""
        try:
            require_https(self.base)
        except ValueError as exc:
            raise ProviderError(f"{exc}; nothing was sent") from exc
        if urlparse(self.base).scheme != "https" or not self.host:
            raise ProviderError(
                f"{shown(self.base)} isn't an https address, which Tinfoil's attestation "
                "and its TLS pin need; nothing was sent"
            )

    # --- the listing ------------------------------------------------------------------

    def fetch_model_prices(self) -> dict[str, dict[str, Any]]:
        """Tinfoil's chat models, fetched without the key."""
        return self.listing.fetch()

    def fetch_reasoning_defaults(self) -> dict[str, dict[str, Any]] | None:
        return None  # the listing says what each takes, not what it does unasked

    def still_offered(self, model: str) -> bool | None:
        """Whether Tinfoil still lists `model` (public: no key sent). None when
        the list couldn't be read."""
        entries = self.listing.known()
        return None if entries is None else model in entries

    def check_offered(self, model: str) -> None:
        """Raise `ModelRetired`, with what is offered instead, if Tinfoil no
        longer lists `model` (a worker thread)."""
        entries = self.listing.known()
        if entries is not None and model not in entries:
            raise ModelRetired(model, sorted(entries), service=SERVICE_NAME)

    # --- one request ------------------------------------------------------------------

    def _trace(self, event_name: str, info: dict[str, Any]) -> None:
        super()._trace(event_name, info)  # the socket, for Stop
        pin = self._pin
        if pin is None:
            raise TlsPinMismatch(NOT_PINNED)
        pin(event_name, info)

    def _prepare(self, request: ChatRequest) -> tuple[str, dict[str, Any], dict[str, str]]:
        self._before_attest()
        # Attested first: nothing goes to the host before the enclave has
        # proved what it is, and the pin is the attested key.
        self._get_client()
        verified = self._verified
        assert verified is not None
        entries = self.listing.known()
        if entries is not None and request.model not in entries:
            raise ModelRetired(request.model, sorted(entries), service=SERVICE_NAME)
        efforts = None
        if entries is not None:
            efforts = list(entries[request.model].get("reasoning_efforts") or [])
        body = direct_body(self.build_payload(request), request, efforts)
        body[CACHE_SECRET_FIELD] = self.enclave.cache_secret
        self._pin = TlsPin(self.host, verified.tls_public_key)
        headers = {**self._headers(), "Accept": "text/event-stream"}
        return self.endpoint, body, headers

    def _stream(self, request: ChatRequest) -> Iterator[StreamEvent]:
        try:
            yield from super()._stream(request)
        except TlsPinMismatch:
            # Whatever answers at the host isn't the enclave attested: attest
            # again before anything more is tried.
            self.enclave.invalidate(self._verified)
            raise
        finally:
            self._pin = None


def tinfoil_context(base_url: str, model: str) -> Any:
    """A Tinfoil model's context window from its listing, read without the
    key (Settings → Private → Detect); None when it isn't listed."""
    from sealedlore.providers.context_probe import ContextSize

    entry = (Listing(base_url).known() or {}).get(model)
    tokens = entry.get("context_length") if entry else None
    return ContextSize(tokens=tokens, source="Tinfoil's model list") if tokens else None
