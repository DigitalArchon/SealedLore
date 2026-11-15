"""How private a model is, as a property of the endpoint it is reached through.

Every decision the app makes about a private model goes through here: which
client is built, whether it must be attested, where a call may go, how its
passages are marked, what a chat may move to, its reasoning level, whether a
route is sent, and how the pickers label it. `tests/test_privacy_seam.py`
fails on a module outside the allowlist that reads a model's prefix to decide
any of it.

A model is one of three kinds (`Privacy.kind`), the mechanism:

- "sealed": every request is sealed on this computer to an attested enclave's
  key; the service in between relays ciphertext. NanoGPT's `private/` models
  (its Private Mode, providers/private_mode.py), every model on an endpoint
  that speaks Tinfoil's protocol (`models/config.py` `sealed_service`: by the
  endpoint's `api`, else its host, whatever `api` says), and Chutes' -TEE
  models on any of Chutes' hosts (`chutes_tee`, providers/chutes_provider.py).
- "tee": the model runs in an attested enclave and its replies are signed,
  but the text passes the service in the clear: NanoGPT's `TEE/` models
  (providers/tee.py).
- "": anything else, Chutes' other models included.

And at one of three levels (`Privacy.level`, the author's ruling, Nov 15
2026), what it is worth: "highest" (NanoGPT's `private/` models, Tinfoil),
"partial" (NanoGPT's `TEE/` models, Chutes' -TEE ones: `PRIVACY_LEVELS`
says why) or "" (not private). A chat or a scene keeps to its level: it
moves only to a model at its own level or higher, on any service
(`move_refusal`), and nothing of it goes where its model is less private
than it was made (`holds`).

`Privacy.service` names who seals or attests it, and so which client and
which checks apply: "nanogpt-private", "nanogpt-tee", "tinfoil", "chutes",
or "" for none. NanoGPT has two because one host offers both kinds, each
with its own protocol; Tinfoil's is the same for every model on it; Chutes'
is its -TEE models', beside its ordinary ones.

When in doubt, sealed: a sealed host is sealed whatever the model and
whatever its `api` says, a Chutes id that may end "-TEE" is one, and a
`private/` model on any other endpoint is still NanoGPT's Private Mode,
whose client refuses to send it anywhere but NanoGPT
(`private_mode.not_nanogpt`), as it always has. A model on an endpoint that
isn't set up is judged by its id alone.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from sealedlore.models.config import (
    Config,
    ProviderConfig,
    chutes_tee,
    key_origin,
    on_chutes,
    sealed_service,
    shown,
)
from sealedlore.models.private import PrivacyRecord
from sealedlore.providers.base import ChatProvider, ChatRequest, ProviderError, StreamEvent
from sealedlore.providers.tee import is_private_mode, is_tee

PrivacyKind = Literal["", "tee", "sealed"]
# How private, as the author ranks them (Nov 15 2026).
PrivacyLevel = Literal["highest", "partial", ""]
HIGHEST: PrivacyLevel = "highest"
PARTIAL: PrivacyLevel = "partial"
NOT_PRIVATE: PrivacyLevel = ""

NANOGPT_PRIVATE = "nanogpt-private"
NANOGPT_TEE = "nanogpt-tee"
TINFOIL = "tinfoil"
CHUTES = "chutes"
# The service behind each sealed protocol an endpoint may speak (`ChatApi`).
_SEALED_SERVICES = {"tinfoil": TINFOIL}
SERVICE_NAMES = {
    NANOGPT_PRIVATE: "NanoGPT's Private Mode",
    NANOGPT_TEE: "NanoGPT's TEE models",
    TINFOIL: "Tinfoil",
    CHUTES: "Chutes",
}
# Each sealed service's level: a sealed service not named here (one added
# before its client, `NotBuiltProvider`) is taken as the highest, so a chat
# recorded on it moves to nothing less.
_SEALED_LEVELS: dict[str, PrivacyLevel] = {
    NANOGPT_PRIVATE: HIGHEST,
    TINFOIL: HIGHEST,
    CHUTES: PARTIAL,
}
_RANK = {HIGHEST: 2, PARTIAL: 1, NOT_PRIVATE: 0}
# The level's own words, leading every note that says how private a model is.
LEVEL_WORDS = {
    HIGHEST: "Highest privacy",
    PARTIAL: "Partial privacy",
    NOT_PRIVATE: "Not private",
}
# The three levels in one sentence (the author's ruling, Nov 15 2026): in the
# notes, Test attestation and the manual.
PRIVACY_LEVELS = (
    "SealedLore names three levels of privacy: highest, end-to-end encrypted to an "
    "attested enclave whose software is measured (NanoGPT's private/ models, and "
    "Tinfoil's); partial, attested but with a gap in what is proved (NanoGPT's TEE/ "
    "models, whose text passes NanoGPT's gateway in the clear, and Chutes' -TEE models, "
    "encrypted all the way to a key that rests on software Chutes signs); and not "
    "private, every other model, whose provider can read your text."
)
# The services whose client this version has. A sealed endpoint of any other
# sends nothing (`NotBuiltProvider`): a plain client would send it in the clear.
# Every service has one now; a sealed service added before its client is
# refused by `NotBuiltProvider`, never sent in the clear.
BUILT_SERVICES = frozenset({NANOGPT_PRIVATE, NANOGPT_TEE, TINFOIL, CHUTES})


@dataclass(frozen=True)
class Privacy:
    kind: PrivacyKind = ""
    service: str = ""
    # A model that isn't private on a service that also has private ones
    # (CHUTES, for its ordinary models): for the picker's note alone, never
    # deciding anything (`service` stays "").
    plain_on: str = ""

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
    def level(self) -> PrivacyLevel:
        """How private (the author's ruling, Nov 15 2026): "highest" for
        NanoGPT's `private/` models and Tinfoil's, "partial" for NanoGPT's
        `TEE/` models and Chutes' -TEE ones, "" for every other."""
        if self.kind == "sealed":
            return _SEALED_LEVELS.get(self.service, HIGHEST)
        if self.kind == "tee":
            return PARTIAL
        return NOT_PRIVATE

    @property
    def built(self) -> bool:
        """This version has the client its service needs (anything not
        private needs none of its own)."""
        return not self.private or self.service in BUILT_SERVICES

    def label(self) -> tuple[str, str]:
        """(short label, what it protects, its level's words first) for the
        pickers and the session line, or ("", "") (a Chutes ordinary model:
        ("", its note))."""
        if self.service == NANOGPT_PRIVATE:
            return "🔐 End-to-end encrypted", E2EE_NOTE
        if self.service == TINFOIL:
            return "🔐 End-to-end encrypted (Tinfoil)", TINFOIL_NOTE
        if self.service == CHUTES:
            return "🔐 End-to-end encrypted (Chutes, partial)", CHUTES_NOTE
        if self.sealed:
            name = SERVICE_NAMES.get(self.service, self.service)
            return "🔐 End-to-end encrypted", (
                f"{LEVEL_WORDS[self.level]}. End-to-end encrypted through {name}: every "
                "message would be sealed on this computer to a key only the model's attested "
                "enclave holds. Its client isn't built into this version yet, so nothing is "
                "sent to it."
            )
        if self.kind == "tee":
            return "TEE", TEE_NOTE
        if self.plain_on == CHUTES:
            return "", CHUTES_PLAIN_NOTE
        return "", ""


NONE = Privacy()

E2EE_NOTE = (
    "Highest privacy. End-to-end encrypted: every message is sealed on this machine to a "
    "key only the model's attested enclave holds. nano-gpt relays ciphertext; it sees your "
    "account, the model, timing, sizes and cost, never the text."
)
TINFOIL_NOTE = (
    "Highest privacy. End-to-end encrypted, straight to Tinfoil (preliminary): every "
    "message is sealed on this machine to a key only Tinfoil's attested router enclave "
    "holds, over a connection pinned to the enclave's attested TLS key. Tinfoil sees your "
    "account, the model, timing, sizes and cost, never the text."
)
CHUTES_NOTE = (
    "Partial privacy. End-to-end encrypted to one Chutes instance (preliminary): each "
    "message is sealed on this machine to the key of an instance whose Intel TDX quote, "
    "for our nonce, binds that key, whose GPUs NVIDIA vouches for, and whose VM image is "
    "one Chutes publishes. Why partial: that only the model can read your text rests on "
    "software Chutes signs. The program that makes and holds the key (Chutes' aegis "
    "library, a closed binary) is admitted by Chutes' signature, not measured, and the "
    "list of measured images is Chutes' own. Beside NanoGPT's TEE models: your text is "
    "encrypted all the way here, where theirs passes NanoGPT's gateway in the clear; but "
    "what they attest is the enclave as measured, where here the program holding the key "
    "is outside the measurement. Replies come whole, not streamed. Chutes sees your "
    "account, the model, timing, sizes and cost; the instance's operator, ciphertext."
)
TEE_NOTE = (
    "Partial privacy. TEE: the model runs in an attested enclave and its replies are "
    "signed, but the text passes nano-gpt's gateway in the clear."
)
# The picker's note for one of Chutes' ordinary models (Privacy.plain_on).
CHUTES_PLAIN_NOTE = (
    "Not private. Chutes reads this model's text, as any provider of an ordinary model "
    "does. Chutes' -TEE models are the ones sent end-to-end encrypted (partial privacy)."
)


def level_line(privacy: Privacy) -> str:
    """The one sentence leading what a note says of `privacy`'s level, for
    Test attestation's details and the model button."""
    if privacy.level == HIGHEST:
        return "Highest privacy: end-to-end encrypted to an attested enclave."
    if privacy.service == CHUTES:
        return (
            "Partial privacy: end-to-end encrypted, but to a key that rests on software "
            "Chutes signs, not on what is measured."
        )
    if privacy.level == PARTIAL:
        return (
            "Partial privacy: an attested enclave, but your text passes NanoGPT's gateway "
            "in the clear."
        )
    return "Not private: the model's provider can read your text."


def privacy_at(model: str, base_url: str = "", api: str = "auto") -> Privacy:
    """`model`'s privacy on the endpoint at `base_url` speaking `api`: a
    sealed endpoint's service for every model on it, Chutes' for a -TEE
    model on one of its hosts whatever it speaks (`chutes_tee`), else
    NanoGPT's kinds by the id (its capitals and stray spaces never make it
    less private, `tee.is_tee`). With no address, by the id alone. `model`
    may be a bare id or a reference naming its endpoint; the address
    decides, never the endpoint's name."""
    sealed = sealed_service(base_url, api)
    if sealed:
        return Privacy("sealed", _SEALED_SERVICES[sealed])
    if chutes_tee(model, base_url):
        return Privacy("sealed", CHUTES)
    if is_private_mode(model):
        return Privacy("sealed", NANOGPT_PRIVATE)
    if is_tee(model):
        return Privacy("tee", NANOGPT_TEE)
    if on_chutes(base_url):
        return Privacy(plain_on=CHUTES)
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


def rank(privacy: Privacy) -> int:
    """How private, by level: 2 the highest, 1 partial, 0 not private."""
    return _RANK.get(privacy.level, 0)


def more_private(one: Privacy, other: Privacy) -> Privacy:
    """The more private of two, by level; `one` when they are as private."""
    return one if rank(one) >= rank(other) else other


def held(record: PrivacyRecord | None) -> Privacy:
    """The privacy a chat or a scene was recorded with (models/private.py
    `PrivacyRecord`), or NONE."""
    if record is None or record.kind not in ("", "tee", "sealed"):
        return NONE
    return Privacy(record.kind, record.service)


def record_of(privacy: Privacy, base_url: str = "") -> PrivacyRecord:
    """`privacy` as a chat or a scene keeps it, with the origin its calls go to."""
    return PrivacyRecord(kind=privacy.kind, service=privacy.service, origin=key_origin(base_url))


def holds(kept: Privacy, found: Privacy) -> bool:
    """Whether `found`, where a call would go now, keeps what `kept`
    promised: at its level or higher, and an end-to-end encrypted one sealed
    by the same service, whose own client alone seals to it. An explicit
    move to another service's model (D8 as amended: its level or higher)
    records the new one first."""
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


def level_phrase(privacy: Privacy) -> str:
    """Its level as a sentence says it: "the highest privacy", "partial
    privacy", "no privacy"."""
    return {HIGHEST: "the highest privacy", PARTIAL: "partial privacy"}.get(
        privacy.level, "no privacy"
    )


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
    began += f", with {level_phrase(kept)}"
    if now:
        host = urlparse(shown(now)).hostname or shown(now)
        goes = f"its model {model} now goes to {host}, {service_words(found)}"
    else:
        goes = f"its model {model} names an endpoint that isn't set up any more"
    return f"{what} began {began}, and {goes}, so nothing was sent. {way_out}".rstrip()


def service_words(privacy: Privacy) -> str:
    """Who a model's messages go through, and its level, as a note says it."""
    if not privacy.private:
        return "not end-to-end encrypted or TEE: not private"
    name = SERVICE_NAMES.get(privacy.service, privacy.service)
    level = LEVEL_WORDS[privacy.level].lower()
    return f"{name}, end-to-end encrypted: {level}" if privacy.sealed else f"{name}: {level}"


# What a chat or scene at each level may move to, as the refusals and the
# picker's note say it.
LEVEL_CHOICES = {
    HIGHEST: "NanoGPT's private/ models or Tinfoil's",
    PARTIAL: "NanoGPT's TEE/ or private/ models, Chutes' -TEE models or Tinfoil's",
}


def move_refusal(current: str, new: str, *, privacy: Judge) -> str | None:
    """Why a chat on `current` may not move to `new`, or None, each judged by
    `privacy`: only to a model at its own level or higher, on any service
    (the author's ruling, Nov 15 2026): a chat with the highest privacy to
    NanoGPT's private/ models or Tinfoil's, one with partial privacy to
    those, NanoGPT's TEE/ models or Chutes' -TEE ones."""
    now, then = privacy(current), privacy(new)
    if rank(then) >= rank(now):
        return None
    if then.sealed:
        name = SERVICE_NAMES.get(then.service, then.service)
        what = f"{new} is end-to-end encrypted through {name}, but has {level_phrase(then)}"
    elif then.private:
        what = f"{new} isn't end-to-end encrypted (a TEE model), so it has {level_phrase(then)}"
    elif now.level == HIGHEST:
        what = f"{new} isn't end-to-end encrypted, so it has {level_phrase(then)}"
    else:
        what = f"{new} isn't a TEE or end-to-end encrypted model, so it has {level_phrase(then)}"
    return (
        f"{what}, and this chat is kept to {level_phrase(now)}: choose one of "
        f"{LEVEL_CHOICES[now.level]}, or start a new chat."
    )


def move_note(privacy: Privacy) -> str:
    """What a chat at `privacy`'s level may move to, for the picker's list."""
    if privacy.level == HIGHEST:
        return (
            "this chat has the highest privacy: NanoGPT's private/ and Tinfoil's "
            "end-to-end encrypted models only"
        )
    return "this chat has partial privacy: TEE and end-to-end encrypted models only"


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
    url: str,
    what: str = "this request",
    *,
    api: str = "auto",
    model: str = "",
    way_out: str = CHAT_WAY_OUT,
) -> str | None:
    """Why a client that sends in the clear must not send `what` to `url`,
    or None: `url`'s host takes only end-to-end encrypted requests, or `api`
    names a sealed protocol (`sealed_service`); or `model`, the request's
    own, is one of Chutes' TEE models on one of Chutes' hosts
    (`chutes_tee`), sent only end-to-end encrypted, which SealedLore does
    for chat alone (Chutes' other models go). For the chat, embeddings,
    picture and video clients, before anything is sent."""
    sealed = sealed_service(url, api)
    host = urlparse(shown(url)).hostname or shown(url)
    if sealed:
        name = SERVICE_NAMES[_SEALED_SERVICES[sealed]]
        return (
            f"{host} is {name}'s end-to-end encrypted endpoint, and {what} would go to it "
            f"in the clear, so nothing was sent. {way_out}"
        )
    if model and chutes_tee(model, url):
        return (
            f"{model.strip()} is one of Chutes' TEE models, which SealedLore sends only "
            f"end-to-end encrypted (partial privacy), and that only for chat: {what} would go "
            f"to {host} in the clear, so nothing was sent. Choose one of Chutes' other "
            "models for this, or a model on another endpoint."
        )
    return None


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
