"""Chutes' end-to-end encrypted models: every message sealed on this computer to
the key of one Chutes instance whose TDX quote, for our nonce, binds that key
(preliminary: built and tested without an account, then run with the author's
on Nov 15 2026: five instances discovered and each attested, its key binding
held live, sealed replies opened, a nonce never used twice, the listed prices
what Chutes charged, a story turn and a private scene).

The framing is providers/chutes_e2ee.py; the hardware's checks are tee.py's
own for a Chutes instance (`TeeClient.check_instance`: Intel's DCAP chain, no
debug TD, the TLS certificate's key bound into the quote and signing our nonce
and that quote, NVIDIA's verdict on the GPUs for the nonce the quote binds).
What this adds, for each request (design/e2ee-providers.md 2.3):

1. **Discovery** (`_discover`): `GET api.chutes.ai/e2e/instances/{chute_id}`,
   with the key: at most five instances, each with its ML-KEM-768 key and ten
   single-use nonces, good for 60 seconds. The chute comes from the model list
   (llm.chutes.ai, keyless), its confidential models alone.
2. **Attestation** (`_attest_pool`), before any nonce of an instance is used:
   every instance not attested in the last REATTEST_SECONDS (by its id and its
   key's hash, on a clock that runs on while the machine sleeps) is. Chutes'
   evidence is limited to 60 calls a minute for all its users together, so as
   few are made as will do: one for the whole chute
   (`/chutes/{id}/evidence?nonce=`, keyless), or, for an instance first seen
   since the last of those, its own (`/instances/{id}/evidence`, with the
   key). A 429 is waited out within ATTEMPT_SECONDS only. Each instance's
   evidence goes through tee.py's checks, then two of this provider's own:
   the quote's report data begins with sha256(nonce + its key), so the
   hardware vouches for the key a request is sealed to (`key_bound`), and its
   MRTD and RTMR0-3 are a set Chutes publishes (`measured`). One that fails
   is left out, said, and never sealed to; with none left the model is
   refused. Leaving one out is safe here, unlike for TEE/ models through
   NanoGPT: this provider chooses the instance, and what is sealed to one key
   can't be opened by another.
3. **The request** (`_stream`): sealed to the chosen instance, `POST
   /e2e/invoke`, not streamed (the author's D6), the nonce used once and
   never again. The reply is opened with our response key or refused; a seal
   that doesn't hold refuses and doubts every instance's attestation, so
   they are attested again before the next request (`ChutesState.doubt`).
   Anything Chutes writes in refusing is said as its, unauthenticated
   (`relay_said`).

The attestation is always "partial" (`WORKLOAD_UNMEASURED`,
`MEASUREMENTS_LISTED`): the workload that makes and uses the key is admitted
by Chutes' signature, not measured, and the measured sets are Chutes' list.

A Chutes endpoint (llm.chutes.ai, or claude.chutes.ai speaking Anthropic's
Messages API) offers every model Chutes lists (the author, Nov 15 2026); its
-TEE models (models/config.py `chutes_tee`) come here, always, whatever the
endpoint speaks, and its others go through the endpoint's ordinary client.

**The key** goes from an endpoint at one of Chutes' own hosts
(`CHUTES_KEY_ORIGINS`: llm.chutes.ai, api.chutes.ai, claude.chutes.ai), the
key saved for one of them, to https://api.chutes.ai, where Chutes' end-to-end
encryption is, over https, and nowhere else (`not_chutes`, `_key`). That is
the one stated exception to "a key goes only to the origin it was entered
for" (the DA apps' security standard): Chutes takes one key on all three of
its hosts, as NanoGPT's key from the environment is bound to NanoGPT's own
origin. A key saved for any other origin never comes here
(`ProviderConfig.sendable`, then `_key`), and an edited address is asked
about as for any key. Nothing is ever redirected, and nothing is ever sent
unsealed but the discovery, the evidence and the two public lists.
"""

from __future__ import annotations

import hashlib
import random
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

from sealedlore.models.config import (
    CHUTES_E2EE_ORIGIN,
    CHUTES_KEY_ORIGINS,
    ProviderConfig,
    key_origin,
    shown,
    split_model,
)
from sealedlore.models.private import Attestation
from sealedlore.providers.base import (
    ChatRequest,
    ProviderError,
    ReasoningDelta,
    StreamCancelled,
    StreamCompleted,
    StreamEvent,
    TextDelta,
    checked_key,
    parse_usage,
    relay_said,
)
from sealedlore.providers.chutes_catalog import MODELS_URL
from sealedlore.providers.chutes_e2ee import (
    MeasurementSet,
    SealBroken,
    SealError,
    confidential_models,
    is_chute_id,
    key_bound,
    measured,
    mlkem_refusal,
    open_reply,
    public_key,
    published_measurements,
    seal_request,
)
from sealedlore.providers.dcap import since_boot
from sealedlore.providers.http import REPLY_MAX_BYTES, make_client
from sealedlore.providers.privacy import CHUTES
from sealedlore.providers.streaming import StreamingHttpProvider, read_body, read_start
from sealedlore.providers.tee import (
    ATTEMPT_SECONDS,
    ATTESTATION_TIMEOUT_SECONDS,
    GPU_SHORTFALLS,
    GPUS_UNVERIFIED,
    MEASUREMENTS_LISTED,
    NO_GPU_EVIDENCE,
    REATTEST_SECONDS,
    WORKLOAD_UNMEASURED,
    TeeClient,
    TeeRefused,
    _Checks,
    failure_words,
)
from sealedlore.providers.wire import build_chat_payload, to_wire_messages
from sealedlore.storage.atomic import outside_json
from sealedlore.threads import thread_pool

CHUTES_ORIGIN = CHUTES_E2EE_ORIGIN
MEASUREMENTS_URL = CHUTES_ORIGIN + "/servers/tdx/measurements"
INVOKE_URL = CHUTES_ORIGIN + "/e2e/invoke"
# What the instance's workload maps to its model's own route (chutes run.py).
E2E_PATH = "/v1/chat/completions"
# Discovery: at most this many instances, each with this many nonces (chutes-api
# router.py), good for NONCE_LIFE_SECONDS; a nonce is used only while this much
# of its life is left, for the request to reach Chutes in time.
MAX_INSTANCES = 5
MAX_NONCES = 10
NONCE_LIFE_SECONDS = 60
NONCE_MARGIN_SECONDS = 10
DISCOVERY_TIMEOUT_SECONDS = 30.0
# The model list and the published measurements are read again after this.
LISTING_SECONDS = 60 * 60
# Since the last whole chute's evidence, an instance first seen is attested
# with its own evidence, while there are this many or fewer: more, and the
# chute's again (a single call either way, of the 60 a minute Chutes allows
# everyone together).
INSTANCE_LEVEL_MOST = 2
# A 429 asking to wait this long, or more than the attempt has left, isn't waited out.
EVIDENCE_WAIT_DEFAULT_SECONDS = 5.0
# What Chutes writes in refusing, as much as is said of it.
SAID_CHARS = 200
# What a sealed request carries of the body the app builds (the rest is dropped
# before sealing): what vLLM's chat route takes and SealedLore sends.
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
        "temperature",
        "top_p",
        "user",
    }
)
# Chutes' models run on vLLM, which takes no reasoning effort for them: their
# chat template switches thinking on or off. Live (Nov 15 2026, every -TEE
# model, a one-line question): sent nothing, the Qwen models thought 435-468
# tokens and ran out of a 400-token limit before a word (16-25 s, the reply
# whole, so nothing showed), and Qwen3.8 27B thought 1-2,400 tokens a story
# call and once 8,001, which lost a scene read; with these, none at all,
# 1.6-1.8 s. A template that knows neither name (DeepSeek V3.2 thinks only
# when asked) is unchanged.
THINKING_OFF = {"enable_thinking": False, "thinking": False}
THINKING_OFF_EFFORTS = frozenset({"none", "minimal", "low"})
# A discovery nonce as Chutes makes them (secrets.token_urlsafe(24)): nothing
# else goes into a header.
_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,128}")
EVIDENCE_LIMIT = (
    "Chutes allows 60 calls a minute for everyone's evidence together, and the limit was "
    "reached; this may pass if tried again in a minute"
)


# What the keyless check (`attest_public`) can't show: that the hardware binds
# the key a request would be sealed to (only discovery, with the key, names it).
KEY_BINDING_UNCHECKED = "the key binding unchecked (it needs a Chutes key)"


def not_chutes(base_url: str, key_saved_for: str = "") -> str | None:
    """Why Chutes' end-to-end encryption can't go through the endpoint at
    `base_url`, its key saved for `key_saved_for`, or None: its discovery,
    evidence and sealed requests are api.chutes.ai's, and the key goes there
    only from an endpoint at one of Chutes' own hosts, saved for one of them
    (`CHUTES_KEY_ORIGINS`: the one exception to a key going only to the
    origin it was entered for)."""
    if key_origin(base_url) not in CHUTES_KEY_ORIGINS:
        return (
            f"{shown(base_url)} isn't one of Chutes' own addresses: its end-to-end encrypted "
            f"models are sent only through {CHUTES_ORIGIN}, from an endpoint at "
            f"{CHUTES_BASE_HINT}, so nothing was sent. Set the endpoint's address to that."
        )
    if key_saved_for and key_saved_for not in CHUTES_KEY_ORIGINS:
        return (
            f"The key was saved for {key_saved_for}, which isn't one of Chutes' own "
            f"addresses, so it isn't sent to {CHUTES_ORIGIN}; nothing was sent. Enter your "
            "Chutes key for this endpoint (Settings → Endpoints)."
        )
    return None


CHUTES_BASE_HINT = "https://llm.chutes.ai/v1 or https://claude.chutes.ai"


class NotOffered(ProviderError):
    """Chutes doesn't list the model as one that runs in a TEE: never sent.
    A variant ("…-TEE:THINKING") is none of those Chutes lists either: its
    router reads the variant on the ordinary route alone, which is in the
    clear."""

    def __init__(self, model: str, offered: list[str]) -> None:
        variant = ":" in model
        super().__init__(
            f"{relay_said(model, 120)} isn't one of Chutes' end-to-end encrypted models "
            "(those it lists as confidential compute); nothing was sent"
            + (
                ". A variant after a colon isn't one Chutes lists: choose the model itself"
                if variant
                else ""
            )
        )
        self.model = model
        self.offered = offered


@dataclass
class _Instance:
    """One instance as discovery lists it: its id, key and unused nonces."""

    instance_id: str
    pubkey: str
    nonces: list[str]

    @property
    def key_sha256(self) -> str:
        return hashlib.sha256(self.pubkey.encode()).hexdigest()


@dataclass
class _Pool:
    """A discovery's instances, good until `until` (on the state's clock)."""

    chute_id: str
    instances: list[_Instance]
    until: float
    # Those discovery listed with a key that isn't one (never sealed to).
    unusable: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class InstanceState:
    """What attesting one instance found: attested (with what its checks
    found and the published set it matched) or left out, and why."""

    instance_id: str
    key_sha256: str
    attested_at: float
    passed: bool
    why: str = ""
    checks: _Checks | None = None
    measurement: str = ""


class ChutesState:
    """What a provider, its siblings and its detached copies share: the
    lists, the instances' attestations, discovery's nonces. Thread-safe."""

    def __init__(self, clock: Callable[[], float] = since_boot) -> None:
        self.clock = clock
        self.lock = threading.Lock()
        # One attestation at a time: a second request waits, then finds it done.
        self.attesting = threading.Lock()
        self.models: dict[str, dict[str, Any]] | None = None
        self.models_at = 0.0
        self.measurements: list[MeasurementSet] | None = None
        self.measurements_at = 0.0
        self.instances: dict[tuple[str, str], InstanceState] = {}
        self.pools: dict[str, _Pool] = {}
        self.chute_level_at: dict[str, float] = {}
        self.attestations: dict[str, Attestation] = {}

    def fresh(self, instance: _Instance) -> InstanceState | None:
        """The instance's attestation while it is in force (REATTEST_SECONDS)."""
        state = self.instances.get((instance.instance_id, instance.key_sha256))
        if state is not None and 0 <= self.clock() - state.attested_at < REATTEST_SECONDS:
            return state
        return None

    def doubt(self) -> None:
        """A reply's seal didn't hold: every instance is attested again, with the
        whole chute's evidence, and discovered again, before the next request."""
        with self.lock:
            self.instances.clear()
            self.pools.clear()
            self.chute_level_at.clear()
            self.attestations.clear()


def gpus_unproved(check: _Checks, short: str) -> tuple[str, bool] | None:
    """Why an instance whose GPUs NVIDIA didn't vouch for (tee.py
    `GPU_SHORTFALLS`) is left out, and whether only for now (NVIDIA's service
    or its keys out of reach: tried again next time); None when its verdict
    held. The model runs on those GPUs and a request is sealed to that
    instance alone, so a TEE/ model's partial is no instance's here."""
    found = [what for what in check.shortfalls if what in GPU_SHORTFALLS]
    if not found:
        return None
    if NO_GPU_EVIDENCE in found:
        return f"instance {short} offered no GPU evidence for NVIDIA to check", False
    if GPUS_UNVERIFIED in found:
        return (
            f"instance {short}'s GPUs couldn't be checked just now: NVIDIA's attestation "
            "service couldn't be reached",
            True,
        )
    return (
        f"instance {short}'s GPUs couldn't be checked just now: NVIDIA's signing keys "
        "couldn't be fetched, so its verdict is unchecked",
        True,
    )


class ChutesE2EEProvider(StreamingHttpProvider):
    sealed_replies = True
    # Chutes' hosts aren't sealed endpoints (`seals` is Tinfoil's): this client
    # seals each request itself, and reaches only api.chutes.ai (`_key`).
    seals = ""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        client: httpx.Client | None = None,
        listing_client: httpx.Client | None = None,
        checks_client: httpx.Client | None = None,
        intel: Callable[..., Any] | None = None,
        nvidia: Callable[..., int] | None = None,
        state: ChutesState | None = None,
        clock: Callable[[], float] = since_boot,
    ) -> None:
        super().__init__(config, client=client)
        self.state = state or ChutesState(clock)
        self._listing_client = listing_client
        # Intel's and NVIDIA's checks (tee.py), and the client their fetches go
        # through: no key is ever on it. Replaceable for tests.
        self._checks_client = checks_client
        self._intel = intel
        self._nvidia = nvidia
        # The instance the last request was sealed to.
        self.last_instance: str | None = None

    def _twin(self) -> ChutesE2EEProvider:
        return ChutesE2EEProvider(
            self.config,
            client=None if self._owns_client else self._client,
            listing_client=self._listing_client,
            checks_client=self._checks_client,
            intel=self._intel,
            nvidia=self._nvidia,
            state=self.state,
        )

    @property
    def endpoint(self) -> str:
        return INVOKE_URL

    # --- the public lists, keyless -------------------------------------------------

    def fetch_model_prices(self) -> dict[str, dict[str, Any]]:
        """Chutes' end-to-end encrypted models (`confidential_models`), priced."""
        return dict(self._models())

    def _models(self) -> dict[str, dict[str, Any]]:
        state = self.state
        with state.lock:
            if state.models is not None and 0 <= state.clock() - state.models_at < LISTING_SECONDS:
                return state.models
        client = self._listing_client or make_client(MODELS_URL)
        try:
            listing = self._public_json(client, MODELS_URL, "Chutes' model list")
        except StreamCancelled:
            raise
        except ProviderError:
            with state.lock:
                if state.models is not None:
                    return state.models  # an older list still says which chute is which
            raise
        finally:
            if self._listing_client is None:
                client.close()
        models = confidential_models(listing)
        with state.lock:
            state.models, state.models_at = models, state.clock()
        return models

    def _measurements(self) -> list[MeasurementSet]:
        state = self.state
        with state.lock:
            if (
                state.measurements is not None
                and 0 <= state.clock() - state.measurements_at < LISTING_SECONDS
            ):
                return state.measurements
        try:
            sets = published_measurements(
                self._public_json(
                    self._get_client(), MEASUREMENTS_URL, "Chutes' published measurements"
                )
            )
        except StreamCancelled:
            raise
        except (ProviderError, SealError) as exc:
            raise TeeRefused(
                f"Chutes' published measurements couldn't be read ({failure_words(exc)}), so "
                "no instance can be checked; nothing was sent"
            ) from exc
        with state.lock:
            state.measurements, state.measurements_at = sets, state.clock()
        return sets

    def _public_json(self, client: httpx.Client, url: str, what: str) -> Any:
        """A public list (no key, nothing of ours in it): its JSON, or ProviderError."""
        status, _headers, body = self._request(client, "GET", url, timeout=30.0)
        if status != 200:
            raise ProviderError(f"{what} couldn't be read (HTTP {status})", status_code=status)
        try:
            return outside_json(body)
        except ValueError as exc:
            raise ProviderError(f"{what} isn't JSON") from exc

    def still_offered(self, model: str) -> bool | None:
        """Whether Chutes still lists `model` as end-to-end encrypted; None
        when its list couldn't be read."""
        try:
            return split_model(model)[1].strip() in self._models()
        except ProviderError:
            return None

    def check_offered(self, model: str) -> None:
        """Raise NotOffered (with what is offered) if Chutes doesn't list `model`."""
        self._chute_for(model)

    def _chute_for(self, model: str) -> tuple[str, str]:
        bare = split_model(model)[1].strip()
        models = self._models()
        entry = models.get(bare)
        if entry is None:
            raise NotOffered(bare, sorted(models))
        return bare, str(entry["chute_id"])

    # --- HTTP ------------------------------------------------------------------------

    def _key(self) -> str:
        """The key, once it may go to api.chutes.ai: refused, nothing sent,
        otherwise. The endpoint is at one of Chutes' own hosts and the key
        saved for one of them (`not_chutes`), the one exception to a key going
        only to the origin it was entered for; and it is sendable there at all
        (`ProviderConfig.sendable`: not one saved elsewhere, nor one held back)."""
        refusal = not_chutes(self.config.base_url, self.config.key_saved_for)
        if refusal:
            raise ProviderError(refusal)
        if self.config.key_withheld:
            raise ProviderError(self.config.key_withheld)
        if not self.config.api_key:
            raise ProviderError(
                "Chutes' end-to-end encrypted models need a Chutes API key; nothing was sent"
            )
        return checked_key(self.config.api_key, self.config.name or "Chutes")

    def _request(
        self,
        client: httpx.Client,
        method: str,
        url: str,
        *,
        keyed: bool = False,
        timeout: float,
        **kwargs: Any,
    ) -> tuple[int, httpx.Headers, bytes]:
        """One request: its status, headers and body (a refusal's to 64 KB, any
        other to REPLY_MAX_BYTES). The key goes only to api.chutes.ai; no
        redirect is followed, nor read as anything but a refusal."""
        headers = dict(kwargs.pop("headers", None) or {})
        if keyed:
            key = self._key()
            if key_origin(url) != CHUTES_ORIGIN:  # never: every keyed URL is built here
                raise ProviderError(f"refusing to send the Chutes key to {shown(url)}")
            headers["Authorization"] = f"Bearer {key}"
        try:
            with client.stream(
                method,
                url,
                headers=headers,
                timeout=timeout,
                extensions={"trace": self._trace},
                **kwargs,
            ) as response:
                if 300 <= response.status_code < 400:
                    raise self._redirected(url, response)
                if response.status_code >= 400:
                    return response.status_code, response.headers, read_start(response)
                return response.status_code, response.headers, read_body(response, REPLY_MAX_BYTES)
        except httpx.HTTPError as exc:
            if self._cancelled:
                raise StreamCancelled() from exc
            raise ProviderError(
                f"{httpx.URL(url).host} couldn't be reached ({type(exc).__name__})"
            ) from exc

    @staticmethod
    def _said(body: bytes) -> str:
        """What Chutes wrote in refusing, as it may be quoted (never sealed)."""
        try:
            data = outside_json(body)
        except ValueError:
            return relay_said(body.decode("utf-8", errors="replace"), SAID_CHARS)
        if isinstance(data, dict):
            data = data.get("detail") or data.get("error") or data.get("message") or ""
            if isinstance(data, dict):
                data = data.get("message") or ""
        return relay_said(str(data), SAID_CHARS) if isinstance(data, (str, int, float)) else ""

    def _refusal(
        self, what: str, status: int, body: bytes, headers: httpx.Headers
    ) -> ProviderError:
        said = self._said(body)
        error = ProviderError(
            f"{what} (HTTP {status})"
            + (f"; Chutes said (unauthenticated): {said}" if said else ""),
            status_code=status,
        )
        retry = headers.get("retry-after")
        try:
            error.retry_after = float(retry) if retry is not None else None
        except ValueError:
            error.retry_after = None
        return error

    # --- discovery ------------------------------------------------------------------

    def _discover(self, model: str, chute_id: str) -> _Pool:
        """Discovery's instances, keys and nonces (with the key), held to their shape."""
        asked = self.state.clock()
        status, headers, body = self._request(
            self._get_client(),
            "GET",
            f"{CHUTES_ORIGIN}/e2e/instances/{chute_id}",
            keyed=True,
            timeout=DISCOVERY_TIMEOUT_SECONDS,
        )
        if status != 200:
            raise self._refusal(
                f"Chutes listed no instance of {model} to seal to", status, body, headers
            )
        try:
            data = outside_json(body)
        except ValueError as exc:
            raise ProviderError("Chutes' list of instances isn't JSON") from exc
        listed = data.get("instances") if isinstance(data, dict) else None
        if not isinstance(listed, list) or not listed:
            raise ProviderError(f"Chutes listed no instance of {model} to seal to")
        life = data.get("nonce_expires_in")
        if isinstance(life, bool) or not isinstance(life, (int, float)) or life <= 0:
            life = NONCE_LIFE_SECONDS
        pool = _Pool(
            chute_id=chute_id,
            instances=[],
            until=asked + min(float(life), NONCE_LIFE_SECONDS) - NONCE_MARGIN_SECONDS,
        )
        seen: set[str] = set()
        for item in listed[:MAX_INSTANCES]:
            if not isinstance(item, dict) or not is_chute_id(item.get("instance_id")):
                continue
            instance_id = item["instance_id"]
            if instance_id in seen:
                continue
            seen.add(instance_id)
            nonces = [
                n
                for n in dict.fromkeys(item.get("nonces") or [])
                if isinstance(n, str) and _TOKEN.fullmatch(n)
            ][:MAX_NONCES]
            pubkey = item.get("e2e_pubkey")
            try:
                public_key(pubkey)
            except SealError as exc:
                pool.unusable[instance_id] = str(exc)
                continue
            pool.instances.append(_Instance(instance_id, pubkey, nonces))
        if not pool.instances:
            reasons = "; ".join(sorted(set(pool.unusable.values()))) or "none had a usable id"
            raise TeeRefused(
                f"Chutes listed no instance of {model} with a key to seal to ({reasons}); "
                "nothing was sent"
            )
        return pool

    # --- attestation ----------------------------------------------------------------

    def attest(self, model: str) -> Attestation:
        """Discover `model`'s instances and attest any not attested in the last
        REATTEST_SECONDS (network; a worker thread): what was proved, for the
        window. Takes no nonce."""
        refusal = mlkem_refusal()
        if refusal:
            raise ProviderError(refusal)
        self._key()  # before anything is fetched: the address and the key may go there
        bare, chute_id = self._chute_for(model)
        pool = self._pool(bare, chute_id)
        return self._attest_pool(bare, pool)

    def _pool(self, model: str, chute_id: str) -> _Pool:
        with self.state.lock:
            pool = self.state.pools.get(chute_id)
            if pool is not None and self.state.clock() < pool.until:
                return pool
        pool = self._discover(model, chute_id)
        with self.state.lock:
            self.state.pools[chute_id] = pool
        return pool

    def _tee(self, model: str) -> TeeClient:
        return TeeClient(
            CHUTES_ORIGIN,
            "",  # no key: the checks' own fetches are Intel's and NVIDIA's
            model,
            client=self._checks_client,
            intel=self._intel,
            nvidia=self._nvidia,
            clock=self.state.clock,
        )

    def _attest_pool(self, model: str, pool: _Pool) -> Attestation:
        """Every instance of `pool` attested, or left out and said; the record."""
        state = self.state
        with state.attesting:
            needing = [i for i in pool.instances if state.fresh(i) is None]
            left_out: dict[str, str] = dict(pool.unusable)
            if needing:
                deadline = time.monotonic() + ATTEMPT_SECONDS
                tee = self._tee(model)
                try:
                    with tee.attempt(deadline):
                        left_out.update(self._attest(tee, model, pool.chute_id, needing, deadline))
                finally:
                    tee.close()
            attestation = self._record(model, pool, left_out)
            with state.lock:
                state.attestations[pool.chute_id] = attestation
            return attestation

    def _attest(
        self,
        tee: TeeClient,
        model: str,
        chute_id: str,
        needing: list[_Instance],
        deadline: float,
    ) -> dict[str, str]:
        """Attest `needing`; what couldn't even be checked this time, by id (not
        kept: tried again next time). What was checked is kept in the state."""
        sets = self._measurements()
        state = self.state
        with state.lock:
            last = state.chute_level_at.get(chute_id)
        recent = last is not None and 0 <= state.clock() - last < REATTEST_SECONDS
        whole = len(needing) > INSTANCE_LEVEL_MOST or not recent
        found: dict[str, tuple[dict[str, Any], str]] = {}
        unheard: dict[str, str] = {}
        if whole:
            try:
                body, nonce = self._evidence(f"/chutes/{chute_id}/evidence", model, deadline)
            except StreamCancelled:
                raise
            except ProviderError as exc:
                if time.monotonic() >= deadline:
                    raise
                why = f"its evidence couldn't be had ({failure_words(exc)})"
                return {i.instance_id: why for i in needing}
            with state.lock:
                state.chute_level_at[chute_id] = state.clock()
            items = body.get("evidence") if isinstance(body.get("evidence"), list) else []
            for item in items:
                if isinstance(item, dict) and is_chute_id(item.get("instance_id")):
                    found.setdefault(item["instance_id"], (item, nonce))
            for name, why in (
                ("failed_instance_ids", "Chutes couldn't fetch its evidence"),
                ("excluded_instance_ids", "Chutes left its evidence out"),
            ):
                listed = body.get(name)
                for instance_id in listed if isinstance(listed, list) else []:
                    if isinstance(instance_id, str) and instance_id not in found:
                        unheard[instance_id] = why
        for instance in needing:
            if instance.instance_id in found or instance.instance_id in unheard:
                continue
            # First seen since the chute's last evidence: its own, with the key.
            try:
                item, nonce = self._evidence(
                    f"/instances/{instance.instance_id}/evidence", model, deadline, keyed=True
                )
            except StreamCancelled:
                raise
            except ProviderError as exc:
                if time.monotonic() >= deadline:
                    raise
                unheard[instance.instance_id] = (
                    f"its evidence couldn't be had ({failure_words(exc)})"
                )
                continue
            found[instance.instance_id] = (item, nonce)
        checkable = [i for i in needing if i.instance_id in found]
        with thread_pool(max(1, min(8, len(checkable)))) as pool:
            results = list(
                pool.map(
                    lambda i: self._check(tee, i, *found[i.instance_id], sets, deadline),
                    checkable,
                )
            )
        with state.lock:
            for instance, result in zip(checkable, results, strict=True):
                if isinstance(result, str):
                    unheard[instance.instance_id] = result  # not checked: tried again next time
                else:
                    state.instances[(result.instance_id, result.key_sha256)] = result
        for instance in needing:
            if instance.instance_id not in found:
                unheard.setdefault(instance.instance_id, "Chutes sent no evidence for it")
        return {i.instance_id: unheard[i.instance_id] for i in needing if i.instance_id in unheard}

    def _evidence(
        self, path: str, model: str, deadline: float, *, keyed: bool = False
    ) -> tuple[dict[str, Any], str]:
        """Chutes' evidence for a fresh nonce of ours, and the nonce. A 429
        (Chutes' 60 a minute for everyone) is waited out while the attempt has
        time; past that, refused in words."""
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TeeRefused(
                    f"{model}'s attestation wasn't done within {ATTEMPT_SECONDS / 60:g} minutes, "
                    "and was given up; this may pass if tried again"
                )
            nonce = secrets.token_hex(32)
            status, headers, body = self._request(
                self._get_client(),
                "GET",
                CHUTES_ORIGIN + path,
                keyed=keyed,
                params={"nonce": nonce},
                timeout=min(ATTESTATION_TIMEOUT_SECONDS, left),
            )
            if status == 429:
                error = self._refusal("Chutes' evidence", status, body, headers)
                wait = max(1.0, error.retry_after or EVIDENCE_WAIT_DEFAULT_SECONDS)
                if wait >= deadline - time.monotonic() - 1:
                    raise TeeRefused(EVIDENCE_LIMIT)
                if self._wake.wait(wait) or self._cancelled:
                    raise StreamCancelled()
                continue
            if status != 200:
                raise self._refusal(f"Chutes didn't give {model}'s evidence", status, body, headers)
            try:
                data = outside_json(body)
            except ValueError as exc:
                raise ProviderError(f"Chutes' evidence for {model} isn't JSON") from exc
            if not isinstance(data, dict):
                raise ProviderError(f"Chutes' evidence for {model} isn't an object")
            return data, nonce

    def _check(
        self,
        tee: TeeClient,
        instance: _Instance,
        item: dict[str, Any],
        nonce: str,
        sets: list[MeasurementSet],
        deadline: float,
    ) -> InstanceState | str:
        """tee.py's checks of one instance's evidence, then the key binding and
        the measurements: attested, or left out and why (kept until it is
        REATTEST_SECONDS old); or, when it couldn't be checked at all this
        time (a fetch of Intel's that failed), why, as text (not kept). The
        attempt's time running out refuses the whole attestation."""
        now = self.state.clock()
        short = instance.instance_id[:8]

        def left_out(why: str) -> InstanceState:
            return InstanceState(instance.instance_id, instance.key_sha256, now, False, why)

        try:
            check = tee.check_instance(item, nonce)
        except StreamCancelled:
            raise
        except ProviderError as exc:
            if time.monotonic() >= deadline:
                raise
            if isinstance(exc, TeeRefused):
                return left_out(failure_words(exc))
            return f"it couldn't be checked just now ({failure_words(exc)})"
        gpus = gpus_unproved(check, short)
        if gpus is not None:
            why, for_now = gpus
            return why if for_now else left_out(why)
        facts = check.quote
        if facts is None or not key_bound(facts.report_data, nonce, instance.pubkey):
            return left_out(
                f"instance {short}'s quote doesn't bind the key Chutes lists for it to our nonce"
            )
        found = measured(facts.mrtd, facts.rtmrs, sets)
        if found is None:
            return left_out(
                f"instance {short}'s measurements aren't among the sets Chutes publishes"
            )
        return InstanceState(
            instance.instance_id,
            instance.key_sha256,
            now,
            True,
            checks=check,
            measurement=f"{found.name} ({found.version})" if found.version else found.name,
        )

    def _record(self, model: str, pool: _Pool, left_out: dict[str, str]) -> Attestation:
        """What was proved of `pool`'s instances: always partial. Refuses when
        none was attested."""
        attested: list[InstanceState] = []
        for instance in pool.instances:
            state = self.state.fresh(instance)
            if state is None:
                left_out.setdefault(instance.instance_id, "it couldn't be attested")
            elif state.passed:
                attested.append(state)
            else:
                left_out[instance.instance_id] = state.why
        if not attested:
            reasons = "; ".join(f"{i[:8]}: {why}" for i, why in left_out.items())
            raise TeeRefused(
                f"none of the instances Chutes listed for {model} could be attested ({reasons}); "
                "nothing was sent"
            )
        check = _Checks(
            f"{len(attested)} of {len(pool.instances) + len(pool.unusable)} instances Chutes "
            f"listed for {model} attested: each one's TDX quote binds the key a request is "
            "sealed to, for our nonce"
        )
        for state in attested:
            if state.checks is not None:
                check.merge(state.checks)
        check.fall_short(WORKLOAD_UNMEASURED)
        check.fall_short(MEASUREMENTS_LISTED)
        names = sorted({state.measurement for state in attested if state.measurement})
        check.note(
            "attested: "
            + ", ".join(f"{state.instance_id[:8]} ({state.measurement})" for state in attested)
        )
        for instance_id, why in left_out.items():
            check.note(f"left out, never sealed to: {instance_id[:8]} ({why})")
        attestation = check.attestation("instance", instances=len(attested))
        attestation.encrypted = True
        attestation.service = CHUTES
        attestation.hardware = "Intel TDX"
        attestation.measurement = "; ".join(names)
        return attestation

    def attest_public(self, model: str) -> Attestation:
        """What can be checked of `model`'s instances without the key (network;
        a worker thread): Chutes' public evidence for the whole chute, for a
        nonce of ours, the first MAX_INSTANCES of it through tee.py's checks
        and the published measurements. The key binding isn't checked: only
        discovery, with the key, names each instance's key
        (KEY_BINDING_UNCHECKED). For Test attestation without a key and the
        live check; never for a request: nothing is sealed to what it found."""
        bare, chute_id = self._chute_for(model)
        sets = self._measurements()
        deadline = time.monotonic() + ATTEMPT_SECONDS
        tee = self._tee(bare)
        try:
            with tee.attempt(deadline):
                body, nonce = self._evidence(f"/chutes/{chute_id}/evidence", bare, deadline)
                listed = body.get("evidence") if isinstance(body.get("evidence"), list) else []
                items = [
                    item
                    for item in listed
                    if isinstance(item, dict) and is_chute_id(item.get("instance_id"))
                ][:MAX_INSTANCES]

                def check(item: dict[str, Any]) -> tuple[str, _Checks | str, str]:
                    short = item["instance_id"][:8]
                    try:
                        found = tee.check_instance(item, nonce)
                    except StreamCancelled:
                        raise
                    except ProviderError as exc:
                        if time.monotonic() >= deadline:
                            raise
                        return item["instance_id"], failure_words(exc), ""
                    gpus = gpus_unproved(found, short)
                    if gpus is not None:
                        return item["instance_id"], gpus[0], ""
                    facts = found.quote
                    known = measured(facts.mrtd, facts.rtmrs, sets) if facts is not None else None
                    if known is None:
                        why = f"instance {short}'s measurements aren't a set Chutes publishes"
                        return item["instance_id"], why, ""
                    name = f"{known.name} ({known.version})" if known.version else known.name
                    return item["instance_id"], found, name

                with thread_pool(max(1, min(8, len(items)))) as pool:
                    results = list(pool.map(check, items))
        finally:
            tee.close()
        attested = [(i, c, n) for i, c, n in results if isinstance(c, _Checks)]
        left_out = {i: c for i, c, _n in results if isinstance(c, str)}
        if not attested:
            reasons = "; ".join(f"{i[:8]}: {why}" for i, why in left_out.items()) or (
                "Chutes' evidence listed none"
            )
            raise TeeRefused(
                f"none of {bare}'s instances in Chutes' public evidence hold up ({reasons})"
            )
        record = _Checks(
            f"{len(attested)} of the {len(results)} instances of {bare} checked from Chutes' "
            "public evidence, for our nonce, hold up: Intel's chain, NVIDIA's verdict and a "
            "published image"
        )
        for _instance, found, _name in attested:
            record.merge(found)
        record.fall_short(WORKLOAD_UNMEASURED)
        record.fall_short(MEASUREMENTS_LISTED)
        record.fall_short(KEY_BINDING_UNCHECKED)
        record.note("attested: " + ", ".join(f"{i[:8]} ({name})" for i, _c, name in attested))
        for instance_id, why in left_out.items():
            record.note(f"left out: {instance_id[:8]} ({why})")
        attestation = record.attestation("instance", instances=len(attested))
        attestation.encrypted = True
        attestation.service = CHUTES
        attestation.hardware = "Intel TDX"
        attestation.measurement = "; ".join(sorted({name for _i, _c, name in attested}))
        return attestation

    def _instance_and_nonce(self, model: str, chute_id: str) -> tuple[_Instance, str]:
        """An attested instance and one of its nonces, taken: never used again,
        whatever becomes of the request. Discovered again when the nonces have
        run out or aged past their use."""
        for _round in range(2):
            pool = self._pool(model, chute_id)
            self._attest_pool(model, pool)
            with self.state.lock:
                if self.state.clock() < pool.until:
                    ready = [
                        i
                        for i in pool.instances
                        if i.nonces and (s := self.state.fresh(i)) is not None and s.passed
                    ]
                    if ready:
                        instance = random.choice(ready)
                        return instance, instance.nonces.pop(0)
                if self.state.pools.get(chute_id) is pool:
                    del self.state.pools[chute_id]
        raise ProviderError(
            f"Chutes' nonces for {model} ran out or expired before a request could be sealed; "
            "nothing was sent"
        )

    # --- one request ------------------------------------------------------------------

    def build_payload(self, request: ChatRequest) -> dict[str, Any]:
        """The body sealed to the instance (less our response key): plain text
        messages, not streamed, only the fields vLLM's route takes. The app's
        lowest reasoning (as low as possible, which a private model is
        always asked for, or low) switches thinking off (`THINKING_OFF`); a
        higher level leaves the model's own."""
        body = dict(build_chat_payload(request, provider_extra_body=self.config.extra_body))
        body["messages"] = to_wire_messages(request.messages, use_cache_control=False)
        body["model"] = split_model(request.model)[1].strip()
        body["stream"] = False
        if request.params.reasoning.effort in THINKING_OFF_EFFORTS:
            own = body.get("chat_template_kwargs")
            body["chat_template_kwargs"] = {
                **THINKING_OFF,
                **(own if isinstance(own, dict) else {}),
            }
        return {key: value for key, value in body.items() if key in ALLOWED_FIELDS}

    def _stream(self, request: ChatRequest) -> Iterator[StreamEvent]:
        refusal = mlkem_refusal()
        if refusal:
            raise ProviderError(refusal)
        self._key()  # before anything is fetched: the address and the key may go there
        model, chute_id = self._chute_for(request.model)
        body = self.build_payload(request)
        instance, nonce = self._instance_and_nonce(model, chute_id)
        sealed = seal_request(instance.pubkey, body)
        self.last_instance = instance.instance_id
        sent_at = time.time()
        status, headers, blob = self._request(
            self._get_client(),
            "POST",
            INVOKE_URL,
            keyed=True,
            content=sealed.blob,
            headers={
                "Content-Type": "application/octet-stream",
                "Accept": "application/octet-stream",
                "X-Chute-Id": chute_id,
                "X-Instance-Id": instance.instance_id,
                "X-E2E-Nonce": nonce,
                "X-E2E-Stream": "false",
                "X-E2E-Path": E2E_PATH,
            },
            timeout=self.config.timeout_seconds,
        )
        if status != 200:
            if status == 403:
                # "Invalid, expired, or already-used nonce": the rest of this
                # discovery's nonces are no better; the next request discovers again.
                with self.state.lock:
                    if self.state.pools.get(chute_id) is not None:
                        del self.state.pools[chute_id]
            raise self._refusal("Chutes refused the sealed request", status, blob, headers)
        try:
            reply = open_reply(blob, sealed.response_key)
        except SealBroken as exc:
            self.state.doubt()
            raise ProviderError(
                f"the encrypted reply couldn't be opened ({exc}); refused, and Chutes' instances "
                "are attested again before anything more is sent"
            ) from exc
        except SealError as exc:
            raise ProviderError(f"the encrypted reply couldn't be read ({exc}); refused") from exc
        if self._cancelled:
            raise StreamCancelled()
        events, completed = self._read_reply(reply, sent_at)
        completed = replace(completed, sealed_to=instance.instance_id)
        yield from events
        self._report_reasoning(request, completed)
        self._report_tee(request, completed)
        yield completed

    @staticmethod
    def _read_reply(
        reply: dict[str, Any], sent_at: float
    ) -> tuple[list[StreamEvent], StreamCompleted]:
        """The opened reply (an OpenAI chat completion, the model server's own) as
        the passage's text, its reasoning, and how it ended."""
        error = reply.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else error
            raise ProviderError(f"the model's server reported an error: {relay_said(str(message))}")
        choices = reply.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderError("the encrypted reply held no answer")
        choice = choices[0]
        message = choice.get("message")
        message = message if isinstance(message, dict) else {}
        events: list[StreamEvent] = []
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            events.append(ReasoningDelta(text=reasoning))
        content = message.get("content")
        if isinstance(content, str) and content:
            events.append(TextDelta(text=content))
        elif isinstance(content, list):
            for block in content:
                text = block.get("text") if isinstance(block, dict) else None
                if isinstance(text, str) and text:
                    events.append(TextDelta(text=text))
        finish = choice.get("finish_reason")
        finish = finish if isinstance(finish, str) and finish else None
        usage = reply.get("usage") if isinstance(reply.get("usage"), dict) else {}
        details = usage.get("completion_tokens_details")
        counted = details.get("reasoning_tokens") if isinstance(details, dict) else None
        counted = counted if isinstance(counted, int) and not isinstance(counted, bool) else 0
        response_id = reply.get("id") if isinstance(reply.get("id"), str) else None
        return events, StreamCompleted(
            usage=parse_usage(usage),
            finish_reason=finish,
            raw_usage=usage,
            response_id=response_id,
            sent_at=sent_at,
            sealed=True,
            reasoning_tokens=max(counted, len(reasoning) // 4 if isinstance(reasoning, str) else 0),
            unterminated=finish is None,
        )


def chutes_context(model: str) -> Any:
    """A Chutes model's context from its public listing, read without the key
    (Settings → Private → Detect), any model Chutes lists; None when it isn't."""
    from sealedlore.providers.chutes_catalog import fetch_chutes_models
    from sealedlore.providers.context_probe import ContextSize

    client = make_client(MODELS_URL)
    try:
        entry = fetch_chutes_models(client).get(split_model(model)[1].strip())
    except ProviderError:
        return None
    finally:
        client.close()
    tokens = entry.get("context_length") if entry else None
    return ContextSize(tokens=tokens, source="Chutes' model list") if tokens else None
