"""How private a model is, as a property of the endpoint it is reached through.

Every decision the app makes about a private model goes through here: which
client is built, whether it must be attested, where a call may go, how its
passages are marked, what a chat may move to, its reasoning level, whether a
route is sent, and how the pickers label it. `tests/test_privacy_seam.py`
fails on a module outside the allowlist that reads a model's prefix to decide
any of it.

A model is one of three kinds (`Privacy.kind`):

- "sealed": every request is sealed on this computer to an attested enclave's
  key; the service in between relays ciphertext. NanoGPT's `private/` models
  (its Private Mode, providers/private_mode.py), and every model on an
  endpoint that speaks Tinfoil's or Chutes' end-to-end encryption
  (`models/config.py` `sealed_service`: by the endpoint's `api`, else its
  host, whatever `api` says).
- "tee": the model runs in an attested enclave and its replies are signed,
  but the text passes the service in the clear: NanoGPT's `TEE/` models
  (providers/tee.py).
- "": anything else.

`Privacy.service` names who seals or attests it, and so which client and
which checks apply: "nanogpt-private", "nanogpt-tee", "tinfoil", "chutes",
or "" for none. NanoGPT has two because one host offers both kinds, each
with its own protocol; a sealed endpoint's service is the same for every
model on it.

When in doubt, sealed: a sealed host is sealed whatever the model and
whatever its `api` says, and a `private/` model on any other endpoint is
still NanoGPT's Private Mode, whose client refuses to send it anywhere but
NanoGPT (`private_mode.not_nanogpt`), as it always has. A model on an
endpoint that isn't set up is judged by its id alone.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from sealedlore.models.config import (
    Config,
    ProviderConfig,
    key_origin,
    sealed_service,
    shown,
)
from sealedlore.models.private import PrivacyRecord
from sealedlore.providers.base import ChatProvider, ChatRequest, ProviderError, StreamEvent
from sealedlore.providers.tee import is_private_mode, is_tee

PrivacyKind = Literal["", "tee", "sealed"]

NANOGPT_PRIVATE = "nanogpt-private"
NANOGPT_TEE = "nanogpt-tee"
TINFOIL = "tinfoil"
CHUTES = "chutes"
# The service behind each sealed protocol an endpoint may speak (`ChatApi`).
_SEALED_SERVICES = {"tinfoil": TINFOIL, "chutes-e2ee": CHUTES}
SERVICE_NAMES = {
    NANOGPT_PRIVATE: "NanoGPT's Private Mode",
    NANOGPT_TEE: "NanoGPT's TEE models",
    TINFOIL: "Tinfoil",
    CHUTES: "Chutes E2EE",
}
# The services whose client this version has. A sealed endpoint of any other
# sends nothing (`NotBuiltProvider`): a plain client would send it in the clear.
# Every service has one now; a sealed service added before its client is
# refused by `NotBuiltProvider`, never sent in the clear.
BUILT_SERVICES = frozenset({NANOGPT_PRIVATE, NANOGPT_TEE, TINFOIL, CHUTES})


@dataclass(frozen=True)
class Privacy:
    kind: PrivacyKind = ""
    service: str = ""

    @property
    def sealed(self) -> bool:
        """End-to-end encrypted: never sent in the clear."""
        return self.kind == "sealed"

    @property
    def private(self) -> bool:
        """A TEE or end-to-end encrypted model: kept to its enclave, attested,
        its calls never routed or handed to another host."""
        return self.kind != ""

    @property
    def built(self) -> bool:
        """This version has the client its service needs (anything not
        private needs none of its own)."""
        return not self.private or self.service in BUILT_SERVICES

    def label(self) -> tuple[str, str]:
        """(short label, what it protects) for the pickers and the session
        line, or ("", "")."""
        if self.service == NANOGPT_PRIVATE:
            return "🔐 End-to-end encrypted", E2EE_NOTE
        if self.service == TINFOIL:
            return "🔐 End-to-end encrypted (Tinfoil)", TINFOIL_NOTE
        if self.service == CHUTES:
            return "🔐 End-to-end encrypted (Chutes, partial)", CHUTES_NOTE
        if self.sealed:
            name = SERVICE_NAMES.get(self.service, self.service)
            return "🔐 End-to-end encrypted", (
                f"End-to-end encrypted through {name}: every message would be sealed on this "
                "computer to a key only the model's attested enclave holds. Its client isn't "
                "built into this version yet, so nothing is sent to it."
            )
        if self.kind == "tee":
            return "TEE", TEE_NOTE
        return "", ""


NONE = Privacy()

E2EE_NOTE = (
    "End-to-end encrypted: every message is sealed on this machine to a key only the "
    "model's attested enclave holds. nano-gpt relays ciphertext; it sees your account, "
    "the model, timing, sizes and cost, never the text."
)
TINFOIL_NOTE = (
    "End-to-end encrypted, straight to Tinfoil (preliminary): every message is sealed on this "
    "machine to a key only Tinfoil's attested router enclave holds, over a connection "
    "pinned to the enclave's attested TLS key. Tinfoil sees your account, the model, "
    "timing, sizes and cost, never the text."
)
CHUTES_NOTE = (
    "End-to-end encrypted to one Chutes instance (preliminary, always partial): each "
    "message is sealed on this machine to the key of an instance whose Intel TDX quote, "
    "for our nonce, binds that key, whose GPUs NVIDIA vouches for, and whose VM image is "
    "one Chutes publishes. Not "
    "attested: the workload that holds the key, which Chutes signs; and the published "
    "images are Chutes' list. Chutes sees your account, the model, timing, sizes and "
    "cost; the instance's operator, ciphertext. Replies come whole, not streamed."
)
TEE_NOTE = (
    "TEE: the model runs in an attested enclave and its replies are signed, but the text "
    "passes nano-gpt's gateway in the clear."
)


def privacy_at(model: str, base_url: str = "", api: str = "auto") -> Privacy:
    """`model`'s privacy on the endpoint at `base_url` speaking `api`: a
    sealed endpoint's service for every model on it, else NanoGPT's kinds by
    the id (its capitals and stray spaces never make it less private,
    `tee.is_tee`). With no address, by the id alone. `model` may be a bare
    id or a reference naming its endpoint; the address decides, never the
    endpoint's name."""
    sealed = sealed_service(base_url, api)
    if sealed:
        return Privacy("sealed", _SEALED_SERVICES[sealed])
    if is_private_mode(model):
        return Privacy("sealed", NANOGPT_PRIVATE)
    if is_tee(model):
        return Privacy("tee", NANOGPT_TEE)
    return NONE


def privacy_for(model: str, endpoint: ProviderConfig | None) -> Privacy:
    """`model`'s privacy on `endpoint` (its address and what it speaks), for
    a caller holding the endpoint and not the config: a private scene's own,
    a client's settings. None: by the id alone."""
    if endpoint is None:
        return privacy_at(model)
    return privacy_at(model, endpoint.base_url, endpoint.api)


def privacy_of(ref: str, config: Config) -> Privacy:
    """A model reference's privacy on the endpoint it names
    (`Config.endpoint_for`: "Tinfoil::glm-5-3", or a bare id on the default
    endpoint). One naming an endpoint that isn't set up is judged by its id."""
    return privacy_for(ref, config.endpoint_for(ref))


Judge = Callable[[str], Privacy]

# --- what a chat or a scene was made, and must stay -------------------------------

_RANK = {"": 0, "tee": 1, "sealed": 2}


def rank(privacy: Privacy) -> int:
    """How private: 2 end-to-end encrypted, 1 TEE, 0 neither."""
    return _RANK.get(privacy.kind, 0)


def more_private(one: Privacy, other: Privacy) -> Privacy:
    """The more private of two (sealed over TEE over neither); `one` when
    they are as private."""
    return one if rank(one) >= rank(other) else other


def held(record: PrivacyRecord | None) -> Privacy:
    """The privacy a chat or a scene was recorded with (models/private.py
    `PrivacyRecord`), or NONE."""
    if record is None or record.kind not in _RANK:
        return NONE
    return Privacy(record.kind, record.service)


def record_of(privacy: Privacy, base_url: str = "") -> PrivacyRecord:
    """`privacy` as a chat or a scene keeps it, with the origin its calls go to."""
    return PrivacyRecord(kind=privacy.kind, service=privacy.service, origin=key_origin(base_url))


def holds(kept: Privacy, found: Privacy) -> bool:
    """Whether `found`, where a call would go now, keeps what `kept`
    promised: at least as private, and an end-to-end encrypted one sealed by
    the same service, whose own client alone seals to it. An explicit move to
    another service's sealed model (D8) records the new one first."""
    if rank(found) < rank(kept):
        return False
    return not (kept.sealed and found.service != kept.service)


def held_judge(judge: Judge, model: str, kept: Privacy) -> Judge:
    """`judge`, with `model` (a chat's or a scene's own) judged at least as
    private as it was recorded: a move check must never let an endpoint
    edited under the chat (one Save) approve a move out of its privacy."""

    def judged(other: str) -> Privacy:
        found = judge(other)
        return more_private(found, kept) if other == model else found

    return judged


def held_back(
    kept: Privacy,
    found: Privacy,
    model: str,
    *,
    origin: str = "",
    now: str = "",
    what: str = "This chat",
    way_out: str = "",
) -> str | None:
    """Why nothing of a chat or a scene recorded as `kept` goes where its
    `model` would go now (`found`, at the address `now`, "" for an endpoint
    that isn't set up), or None when that keeps its privacy (`holds`)."""
    if holds(kept, found):
        return None
    name = SERVICE_NAMES.get(kept.service, kept.service)
    began = f"end-to-end encrypted through {name}" if kept.sealed else f"on {name}"
    if origin:
        began += f" ({urlparse(origin).hostname or origin})"
    if now:
        host = urlparse(shown(now)).hostname or shown(now)
        goes = f"its model {model} now goes to {host}, {service_words(found)}"
    else:
        goes = f"its model {model} names an endpoint that isn't set up any more"
    return f"{what} began {began}, and {goes}, so nothing was sent. {way_out}".rstrip()


def service_words(privacy: Privacy) -> str:
    """Who a model's messages go through, as a note says it."""
    if not privacy.private:
        return "not end-to-end encrypted or TEE"
    name = SERVICE_NAMES.get(privacy.service, privacy.service)
    return f"{name}, end-to-end encrypted" if privacy.sealed else name


def move_refusal(current: str, new: str, *, privacy: Judge) -> str | None:
    """Why a chat on `current` may not move to `new`, or None, each judged by
    `privacy`: an end-to-end encrypted chat moves only to another sealed
    model, on any service, and a TEE chat to a TEE or sealed one."""
    now, then = privacy(current), privacy(new)
    if now.sealed and not then.sealed:
        choose = (
            "a private/ model"
            if now.service == NANOGPT_PRIVATE
            else "another end-to-end encrypted model"
        )
        return (
            f"{new} isn't end-to-end encrypted, and this chat is: choose {choose}, or start "
            "a new chat."
        )
    if now.private and not then.private:
        return (
            f"{new} isn't a TEE model, and this chat is kept to TEE models: choose a "
            "TEE/ or private/ model, or start a new chat."
        )
    return None


def not_built(privacy: Privacy, base_url: str = "") -> str | None:
    """Why nothing is sent to a sealed endpoint whose client this version
    doesn't have, with what to do instead; None when it has it."""
    if privacy.built:
        return None
    name = SERVICE_NAMES.get(privacy.service, privacy.service)
    where = f" to {shown(base_url)}" if base_url else ""
    return (
        f"{name}'s end-to-end encryption isn't built into this version of SealedLore yet, so "
        f"nothing was sent{where}: an ordinary client would send your text in the clear. "
        "For now, choose a model on another endpoint, or one of NanoGPT's end-to-end "
        "encrypted private/ models (Settings → Endpoints and Models)."
    )


CHAT_WAY_OUT = "Choose a model on another endpoint (Settings → Endpoints and Models)."
EMBEDDINGS_WAY_OUT = "Give embeddings an endpoint of their own (Settings → Lore)."
MEDIA_WAY_OUT = (
    "Give pictures and videos endpoints of their own (Settings → Images, Settings → Video)."
)


def plain_refusal(
    url: str, what: str = "this request", *, api: str = "auto", way_out: str = CHAT_WAY_OUT
) -> str | None:
    """Why a client that sends in the clear must not send `what` to `url`,
    or None: `url`'s host takes only end-to-end encrypted requests, or `api`
    names a sealed protocol (`sealed_service`). For the chat, embeddings,
    picture and video clients, before anything is sent."""
    sealed = sealed_service(url, api)
    if not sealed:
        return None
    name = SERVICE_NAMES[_SEALED_SERVICES[sealed]]
    host = urlparse(shown(url)).hostname or shown(url)
    return (
        f"{host} is {name}'s end-to-end encrypted endpoint, and {what} would go to it in "
        f"the clear, so nothing was sent. {way_out}"
    )


class NotBuiltProvider(ChatProvider):
    """Stands in for a sealed endpoint whose client this version hasn't:
    every request is refused, saying why and what to do (`not_built`),
    before anything is sent. A listing too: it would carry the key."""

    def __init__(self, config: ProviderConfig, reason: str) -> None:
        self.config = config
        self.reason = reason

    def build_payload(self, request: ChatRequest) -> dict[str, Any]:
        # Shown (the Prompt page's request), never sent.
        from sealedlore.providers.wire import build_chat_payload

        return build_chat_payload(request, provider_extra_body=self.config.extra_body)

    def stream(self, request: ChatRequest) -> Iterator[StreamEvent]:
        raise ProviderError(self.reason)
        yield  # noqa: B901 - a generator that never yields

    def fetch_model_prices(self) -> dict[str, dict[str, Any]]:
        raise ProviderError(self.reason)

    def close(self) -> None:
        return None
