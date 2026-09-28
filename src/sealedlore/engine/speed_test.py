"""How quickly each model the app calls answers.

A beta tester's request: once the models are chosen, check each for speed.
One short request goes to every distinct model (one used in four places is
tested once) and two things are measured: how long until the first word of
the answer, and how fast the answer then comes.

The first word is the first piece of *text*: a model that reasons first has
that time counted, because that is the wait the author sits through. The rate
is the answer's alone, from its first word to its last, so a model that
thinks for six seconds and then writes its answer in half a second is slow to
start and fast to finish, and the two numbers say so separately.

Network; call it off the GUI thread. Nothing here belongs to a story, so
nothing is written to any story's log.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from sealedlore.engine.pricing import estimate_cost
from sealedlore.engine.tokens import TokenEstimator
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.config import ModelPrice, ProviderConfig
from sealedlore.models.generation import GenerationParams
from sealedlore.providers.base import (
    ChatProvider,
    ChatRequest,
    ProviderError,
    ReasoningDelta,
    StreamCancelled,
    StreamCompleted,
    TextDelta,
    error_detail,
)
from sealedlore.providers.tee import is_private_mode

# Short, so it costs next to nothing, and long enough to time: a rate taken
# over a dozen tokens is mostly the network's jitter.
PROMPT = "Write about 120 words describing a lighthouse at dusk."
# Room for a model that reasons whether asked to or not: reasoning counts
# against the limit, and an answer cut off by it can't be timed. The app's own
# limit for a passage: live, an encrypted GLM spent 1,500 on reasoning alone
# and answered nothing, one run in two. Only what is used is paid for.
MAX_TOKENS = 4000
# Fewer pieces than this and the answer came in a burst, not a stream: what
# was timed is the network's delivery, not the model's writing.
MIN_CHUNKS = 4
# An answer that arrives in one piece has no rate to speak of.
MIN_SPAN = 0.05


@dataclass(frozen=True)
class Role:
    """One use of a model, as Settings has it: the model as typed (blank to
    fall back), the role it falls back to, and its route (`route`: the
    request fields, engine/routing.py; `route_label`: how it reads)."""

    name: str
    model: str
    endpoint: ProviderConfig | None
    fallback: str | None = None
    route: dict[str, Any] = field(default_factory=dict)
    route_label: str = ""


@dataclass(frozen=True)
class SpeedTarget:
    """One model on one endpoint and one route, and everything it is used for."""

    settings: ProviderConfig
    roles: tuple[str, ...]
    route: dict[str, Any] = field(default_factory=dict)
    route_label: str = ""

    @property
    def model(self) -> str:
        return self.settings.model

    @property
    def label(self) -> str:
        """The model as the table names it: with its route, if it has one."""
        return f"{self.model} · {self.route_label}" if self.route else self.model


@dataclass(frozen=True)
class SpeedResult:
    model: str
    # Seconds from sending: to the first reasoning (None: it didn't reason,
    # or didn't show it), to the first and last word of the answer, and to
    # the end of the stream.
    first_reasoning: float | None = None
    first_text: float | None = None
    last_text: float | None = None
    total: float = 0.0
    answer_tokens: int = 0
    # As the endpoint counted them; None when it didn't say.
    reasoning_tokens: int | None = None
    # Counted here from the text, not reported by the endpoint.
    tokens_estimated: bool = False
    tokens_per_second: float | None = None
    # Attesting an end-to-end encrypted model's enclave, before anything is
    # sent: once per five minutes in play, so never part of the first word.
    attest_seconds: float | None = None
    cost: float | None = None
    cost_estimated: bool = False
    finish_reason: str | None = None
    error: str | None = None
    stopped: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and not self.stopped and self.first_text is not None

    @property
    def answer_seconds(self) -> float | None:
        """How long the answer took to write, first word to last."""
        if self.first_text is None or self.last_text is None:
            return None
        return self.last_text - self.first_text


def _endpoint_key(settings: ProviderConfig, route: dict[str, Any]) -> tuple[str, str, str]:
    parsed = urlparse(settings.base_url.strip())
    where = f"{parsed.scheme}://{(parsed.hostname or '').lower()}:{parsed.port}{parsed.path}"
    return where.rstrip("/"), settings.model, json.dumps(route, sort_keys=True)


def plan_targets(roles: Sequence[Role]) -> list[SpeedTarget]:
    """The distinct models to test, in the order their first use is listed,
    each with every role it serves. A blank role takes the model of the role
    it falls back to, and that role's route; one with no model and no
    fallback is left out. The same model on two routes is two targets: the
    comparison worth seeing."""
    by_name = {role.name: role for role in roles}

    def resolved(role: Role, seen: tuple[str, ...] = ()) -> Role | None:
        if role.model.strip():
            return role
        parent = by_name.get(role.fallback or "")
        if parent is None or parent.name in seen:
            return None
        return resolved(parent, (*seen, role.name))

    found: dict[tuple[str, str, str], tuple[ProviderConfig, list[str], Role]] = {}
    for role in roles:
        source = resolved(role)
        if source is None or source.endpoint is None:
            continue
        settings = source.endpoint.model_copy(update={"model": source.model.strip()})
        key = _endpoint_key(settings, source.route)
        found.setdefault(key, (settings, [], source))[1].append(role.name)
    return [
        SpeedTarget(settings, tuple(names), route=source.route, route_label=source.route_label)
        for settings, names, source in found.values()
    ]


def provider_for(settings: ProviderConfig) -> ChatProvider:
    """The client a model is reached through: an end-to-end encrypted one is
    never sent in the clear, here as anywhere."""
    if is_private_mode(settings.model):
        from sealedlore.providers.private_mode import PrivateModeProvider

        return PrivateModeProvider(settings)
    from sealedlore.providers.openai_compat import OpenAICompatibleProvider

    return OpenAICompatibleProvider(settings)


def _reasoning_tokens(completed: StreamCompleted) -> int | None:
    raw = completed.raw_usage or {}
    details = raw.get("completion_tokens_details")
    thinking = (details or {}).get("reasoning_tokens") if isinstance(details, dict) else None
    if not isinstance(thinking, (int, float)):
        thinking = raw.get("reasoning_tokens")
    return int(thinking) if isinstance(thinking, (int, float)) else None


def _answer_tokens(
    text: str, completed: StreamCompleted, reasoned: bool, estimator: TokenEstimator
) -> tuple[int, bool]:
    """The answer's tokens without its reasoning, and whether we counted them
    ourselves. The endpoint's count is used only when it can be trusted to
    leave the reasoning out: it said how much was reasoning, or none was seen."""
    thinking = _reasoning_tokens(completed)
    reported = completed.usage.completion_tokens
    if reported > 0 and isinstance(thinking, (int, float)) and reported > thinking:
        return int(reported - thinking), False
    if reported > 0 and not reasoned and not isinstance(thinking, (int, float)):
        return reported, False
    return estimator.raw_count(text), True


def attest_first(
    provider: ChatProvider, model: str, *, clock: Callable[[], float] = time.monotonic
) -> float | SpeedResult | None:
    """Attest an end-to-end encrypted model's enclave before it is timed:
    the seconds it took, None for a model with nothing to attest, or the
    failure as a result. Checking an attestation is heavy work on this
    machine; done while other models were being timed, it made their first
    words late (live: 0.6s alone, 1.4s beside it)."""
    attest = getattr(provider, "attest", None)
    if not callable(attest):
        return None
    began = clock()
    try:
        attest()
    except StreamCancelled:
        return SpeedResult(model=model, stopped=True)
    except (ProviderError, ValueError, OSError) as exc:
        return SpeedResult(model=model, error=error_detail(exc))
    return clock() - began


def run_speed_test(
    provider: ChatProvider,
    model: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    estimator: TokenEstimator | None = None,
    prices: Mapping[str, ModelPrice] | None = None,
    attest: bool = True,
    attested: float | None = None,
    route: dict[str, Any] | None = None,
) -> SpeedResult:
    """Send the one short request and time it. Never raises: a failure, an
    empty answer and a Stop are results too. With `attest` off the enclave,
    if the model has one, was attested already (`attest_first`), in
    `attested` seconds."""
    estimator = estimator or TokenEstimator()
    attest_seconds = attested
    try:
        if attest:
            found = attest_first(provider, model, clock=clock)
            if isinstance(found, SpeedResult):
                return found
            attest_seconds = found
        request = ChatRequest(
            model=model,
            # The route as the role would send it (engine/routing.py).
            extra_body=dict(route or {}),
            messages=[PromptMessage(role="user", parts=(ContentPart(text=PROMPT),))],
            params=GenerationParams(max_tokens=MAX_TOKENS),
        )
        sent = clock()
        first_reasoning = first_text = last_text = None
        chunks: list[str] = []
        completed = StreamCompleted()
        for event in provider.stream(request):
            if isinstance(event, ReasoningDelta):
                if first_reasoning is None and event.text:
                    first_reasoning = clock() - sent
            elif isinstance(event, TextDelta):
                if first_text is None and not event.text.strip():
                    continue  # white space before the answer isn't its first word
                last_text = clock() - sent
                if first_text is None:
                    first_text = last_text
                chunks.append(event.text)
            elif isinstance(event, StreamCompleted):
                completed = event
        total = clock() - sent
    except StreamCancelled:
        return SpeedResult(model=model, attest_seconds=attest_seconds, stopped=True)
    except (ProviderError, ValueError, OSError) as exc:
        return SpeedResult(model=model, attest_seconds=attest_seconds, error=error_detail(exc))

    text = "".join(chunks)
    if first_text is None:
        spent = " (its token limit went on reasoning)" if first_reasoning is not None else ""
        return SpeedResult(
            model=model,
            first_reasoning=first_reasoning,
            total=total,
            reasoning_tokens=_reasoning_tokens(completed),
            attest_seconds=attest_seconds,
            finish_reason=completed.finish_reason,
            error=f"It answered with no text{spent}.",
        )
    tokens, estimated = _answer_tokens(text, completed, first_reasoning is not None, estimator)
    # The first piece arrived *at* the first word, so the time from there to
    # the last word is what the rest took: its share of the tokens, by length.
    span = last_text - first_text
    after_first = tokens * (len(text) - len(chunks[0])) / len(text)
    timed = span >= MIN_SPAN and len(chunks) >= MIN_CHUNKS and after_first > 0
    usage = completed.usage
    cost, cost_estimated = (usage.cost, False) if usage.cost_reported else (None, False)
    price = (prices or {}).get(model)
    if cost is None and price is not None and usage.prompt_tokens:
        cost, cost_estimated = estimate_cost(usage, price, model), True
    return SpeedResult(
        model=model,
        first_reasoning=first_reasoning,
        first_text=first_text,
        last_text=last_text,
        total=total,
        answer_tokens=tokens,
        reasoning_tokens=_reasoning_tokens(completed),
        tokens_estimated=estimated,
        tokens_per_second=after_first / span if timed else None,
        attest_seconds=attest_seconds,
        cost=cost,
        cost_estimated=cost_estimated,
        finish_reason=completed.finish_reason,
    )
