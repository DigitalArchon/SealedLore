"""The ChatProvider interface and its stream events.

Kept behind an interface so a native Anthropic backend can be added later
without touching the rest of the app. Streaming is a plain synchronous
generator: Qt runs it on a worker thread and httpx handles the socket, which
is all the concurrency this app needs.
"""

from __future__ import annotations

import threading
import unicodedata
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from sealedlore.messages import PromptMessage
from sealedlore.models.config import addresses_shown, key_problem
from sealedlore.models.generation import GenerationParams
from sealedlore.models.node import Usage


@dataclass(frozen=True)
class ChatRequest:
    model: str
    messages: Sequence[PromptMessage]
    params: GenerationParams = field(default_factory=GenerationParams)
    use_cache_control: bool = False
    # How long Anthropic keeps the cached prefix: "5m", or "1h" (see wire.py).
    cache_ttl: str = "5m"
    extra_body: dict[str, Any] = field(default_factory=dict)
    # What the call is for, for the run's call log alone (call_log.py): the
    # API log's kind of request ("scene_request"), set as it is logged. Never
    # sent.
    purpose: str = field(default="", compare=False, repr=False)


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ReasoningDelta:
    text: str


@dataclass(frozen=True)
class StreamCompleted:
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None
    raw_usage: dict[str, Any] = field(default_factory=dict)
    # The completion's `id` (the first chunk's): a TEE model's reply is
    # signed under it (providers/tee.py).
    response_id: str | None = None
    # Decrypted from an end-to-end encrypted reply (providers/private_mode.py):
    # only the attested enclave's key could have sealed it.
    sealed: bool = False
    # A TEE reply's own record (providers/tee.py): the receipt id nano-gpt's
    # headers name, the hash of the exact bytes the stream carried
    # ("sha256:…"), and the route nano-gpt asks its checks to name, if any.
    receipt_id: str | None = None
    body_hash: str | None = None
    tee_route: str | None = None
    # When the request went (time.time(), just before it was sent): a TEE
    # reply's receipt must have been served after it, or it was another
    # request's (providers/tee.py `check`).
    sent_at: float | None = None
    # Set by a Stop of the client the reply came through (its `cancel`): the
    # wait for a TEE reply's receipt, on the reply's thread, ends with it
    # (providers/tee.py `check`). None where nothing waits.
    stop: threading.Event | None = field(default=None, compare=False, repr=False)
    # How much the reply reasoned: the reported count, or the streamed
    # reasoning text at ~4 characters a token, whichever is larger. The count
    # alone lies: Sonnet 4.6 streamed its thinking while NanoGPT reported 0.
    reasoning_tokens: int = 0
    # The stream ended with neither a finish reason nor its end marker
    # ([DONE], message_stop): a gateway closing the connection partway reads
    # as a clean end, so whatever came may be only the start of the reply.
    # Either marker counts, so a local server that sends one is whole.
    unterminated: bool = False
    # What a refusal names as its reason (Anthropic's `stop_details.category`:
    # "cyber", "bio"...), when the stream carries one; informational only.
    refusal_category: str | None = None
    # The instance a reply was sealed to, where the provider chooses one
    # (Chutes' E2EE: providers/chutes_provider.py), for its mark's tooltip.
    sealed_to: str | None = None


StreamEvent = TextDelta | ReasoningDelta | StreamCompleted

# A reply the model, or its endpoint's filter, declined to write: Anthropic's
# stop reason (kept by its own name, providers/anthropic.py), and the finish
# reason OpenAI-compatible endpoints give.
REFUSALS = frozenset({"refusal", "content_filter"})


def declined(completed: StreamCompleted) -> bool:
    return completed.finish_reason in REFUSALS


def declined_words(completed: StreamCompleted) -> str:
    """Who declined, and why when the reply said, in a few words."""
    if completed.finish_reason == "content_filter":
        return "the endpoint's content filter stopped it"
    why = f" ({completed.refusal_category})" if completed.refusal_category else ""
    return f"the model declined to write it{why}"


def why_not_whole(completed: StreamCompleted) -> str | None:
    """Why a reply that came back can't be taken for a whole one, or None.
    A summary, a ledger or a condensed card is refused for any of these, as
    it always was for running into its limit: half a chapter must never
    stand in for the prose."""
    if completed.finish_reason == "length":
        return "it was cut off at its token limit"
    if declined(completed):
        return declined_words(completed)
    if completed.unterminated:
        return "the stream ended before the model said it was done"
    return None


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, body: str | None = None):
        # Said in dialogs, notices and the CLI: never with a password written
        # into an address (models/config.py `addresses_shown`), whoever built
        # the message, httpx included.
        super().__init__(addresses_shown(message))
        self.status_code = status_code
        self.body = body
        # Seconds the endpoint asked us to wait (Retry-After), if it said.
        self.retry_after: float | None = None


class StreamCancelled(ProviderError):
    """The request was aborted by `cancel()`: a stop, not a failure.

    Raised rather than ending quietly so a cut-off stream is never taken for
    a complete one — a summary or a review must not be saved half-written.
    """

    def __init__(self) -> None:
        super().__init__("stopped")


class ChatProvider(ABC):
    # Told of every finished reply to a request that asked for a route
    # (`extra_body["provider"]`, engine/routing.py): NanoGPT names no host in
    # a reply, so the session reads its bill to say whether the route held.
    route_watch: Callable[[ChatRequest, StreamCompleted], None] | None = None

    # Told of every reply that reasoned when its request asked for nothing
    # (engine/reasoning.py); the session marks a model that reasoned at
    # length, so the next request asks for its lowest level.
    reasoning_watch: Callable[[ChatRequest, StreamCompleted], None] | None = None

    # Told of every finished reply: the session checks a TEE/ model's against
    # the record its attested key signs (engine/session_private.py `_watch_tee`),
    # on the thread the reply came back on, before the caller has it.
    tee_watch: Callable[[ChatRequest, StreamCompleted], None] | None = None

    # Asked by the routing client (providers/endpoints.py) before a request
    # goes: given its model and how private it would be where it goes
    # (providers/privacy.py `Privacy`), why it mustn't, or None. The session's
    # chat recorded as end-to-end encrypted goes nowhere less private, nor to
    # another service (engine/session_private.py `_privacy_lock`).
    privacy_lock: Callable[[str, Any], str | None] | None = None

    def _report_tee(self, request: ChatRequest, completed: StreamCompleted) -> None:
        watch = self.tee_watch
        if watch is None:
            return
        try:
            watch(request, completed)
        except Exception:  # noqa: BLE001 - a report never costs the reply
            pass

    def _report_reasoning(self, request: ChatRequest, completed: StreamCompleted) -> None:
        watch = self.reasoning_watch
        asked = request.params.reasoning
        if watch is None or not completed.reasoning_tokens or asked.enabled or asked.effort:
            return
        try:
            watch(request, completed)
        except Exception:  # noqa: BLE001 - a report never costs the reply
            pass

    def _report_route(self, request: ChatRequest, completed: StreamCompleted) -> None:
        watch = self.route_watch
        if watch is None or not request.extra_body.get("provider"):
            return
        try:
            watch(request, completed)
        except Exception:  # noqa: BLE001 - a report never costs the reply
            pass

    @abstractmethod
    def build_payload(self, request: ChatRequest) -> dict[str, Any]:
        """The exact request body, for the context inspector and the API log."""

    @abstractmethod
    def stream(self, request: ChatRequest) -> Iterator[StreamEvent]:
        """Yield deltas, then exactly one StreamCompleted.

        Closing the generator early must abort the request and is how the Stop
        button keeps partial text.
        """

    def fetch_model_prices(self) -> dict[str, dict[str, Any]]:
        """The endpoint's models list, id → entry, for pricing. Empty if unsupported."""
        return {}

    def fetch_reasoning_defaults(self) -> dict[str, dict[str, Any]] | None:
        """What each model does about reasoning when a request says nothing
        (providers/reasoning_defaults.py), id → its fields; None for an
        endpoint that doesn't say. Raises ProviderError."""
        return None

    def cancel(self) -> None:
        """Stop, from any thread: the request in flight, or the next one to start.

        Its stream raises StreamCancelled. A cancel that arrives before the
        request goes out (the job is still assembling the prompt) is kept until
        a stream starts, because a job that doesn't stream — a review, a
        summary — would otherwise run to completion under "Stopping…".
        `reset_cancel()` clears it at the start of each job. Providers that
        can't interrupt a blocked read leave it to the next chunk.
        """
        return None

    def reset_cancel(self) -> None:
        """Forget an unused cancel; called as each job begins."""
        return None

    def complete(self, request: ChatRequest) -> tuple[str, StreamCompleted]:
        """Drain a stream into a single string — for summaries and one-shot calls.

        A reply that is empty because the model reasoned until the limit is
        asked for once more: it is never usable, and the runaway is chance.
        Live (GLM 5.3), 2 scene reads in 120 spent all 4,000 tokens thinking.
        The first attempt was paid for: its usage is added to the second's, so
        the one reply the caller logs and prices counts both.
        """
        spent: StreamCompleted | None = None
        for _attempt in range(2):
            chunks: list[str] = []
            completed = StreamCompleted()
            for event in self.stream(request):
                if isinstance(event, TextDelta):
                    chunks.append(event.text)
                elif isinstance(event, StreamCompleted):
                    completed = event
            text = "".join(chunks)
            if spent is not None:
                completed = both_attempts(spent, completed)
            if text.strip() or completed.finish_reason != "length":
                break
            spent = completed
        return text, completed


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# A reply's cost as the endpoint states it (`parse_usage` reads these).
_COST_KEYS = ("cost", "total_cost")


def _summed(first: dict[str, Any], then: dict[str, Any]) -> dict[str, Any]:
    """Two replies' raw usage as one, whatever shape the endpoint reports it
    in: counts added, nested details too, anything else the later one's."""
    out = dict(first)
    for key, value in then.items():
        before = out.get(key)
        if isinstance(value, dict) and isinstance(before, dict):
            out[key] = _summed(before, value)
        elif _is_number(value) and _is_number(before):
            out[key] = before + value
        else:
            out[key] = value
    return out


def both_attempts(first: StreamCompleted, then: StreamCompleted) -> StreamCompleted:
    """The reply that came on a second attempt, with the first's usage added:
    both were paid for. A cost stated for only one of them is left out, so
    the two are priced together from their tokens rather than one dropped."""
    a, b = first.usage, then.usage
    stated = a.cost_reported and b.cost_reported
    raw = _summed(first.raw_usage or {}, then.raw_usage or {})
    if not stated:
        for key in _COST_KEYS:
            raw.pop(key, None)
    usage = Usage(
        prompt_tokens=a.prompt_tokens + b.prompt_tokens,
        completion_tokens=a.completion_tokens + b.completion_tokens,
        cache_creation_tokens=a.cache_creation_tokens + b.cache_creation_tokens,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
        cost=a.cost + b.cost if stated else 0.0,
        cost_reported=stated,
    )
    return replace(
        then,
        usage=usage,
        raw_usage=raw,
        reasoning_tokens=first.reasoning_tokens + then.reasoning_tokens,
    )


def side_by_side(provider: ChatProvider, count: int) -> list[ChatProvider]:
    """Up to `count` clients to make calls on at the same time: `provider`,
    its sibling, the sibling's own sibling, and so on. A client tracks one
    request (its socket, whether it is in flight), so two calls at once on
    one client leave the first out of reach of a Stop; each `sibling()` is
    built once and a cancel or close reaches every one down the line. A
    provider with no sibling (the scripted mock) gives fewer than asked:
    the caller makes the rest of its calls one after another on the last."""
    lines = [provider]
    while len(lines) < count:
        make = getattr(lines[-1], "sibling", None)
        line = make() if callable(make) else None
        if line is None or any(line is seen for seen in lines):
            break
        lines.append(line)
    return lines


# More tokens than any one call counts, and more dollars than any one costs:
# a number past either is an endpoint's mistake, and is no more taken than
# one below zero.
TOKENS_MOST = 1_000_000_000
COST_MOST = 1_000_000.0


def stated_count(value: Any) -> int | None:
    """A count of tokens as an endpoint stated it, or None when what it
    stated is no count: not a number (JSON's true is one to Python), NaN, an
    infinity, below zero or past `TOKENS_MOST`. JSON lets an endpoint write
    1e999 and NaN, and `int()` of either raised after a whole passage had
    come. The comparison is false for NaN and never overflows."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if 0 <= value <= TOKENS_MOST else None


def stated_cost(value: Any) -> float | None:
    """A charge in dollars as an endpoint stated it, or None when what it
    stated is no charge (as `stated_count`, to `COST_MOST`): the call is then
    priced from the listing or shown unpriced, never as NaN, a credit or
    `True` dollars."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 <= value <= COST_MOST else None


def parse_usage(raw: dict[str, Any] | None) -> Usage:
    """Read usage out of whatever shape the endpoint reports it in.

    OpenAI-compatible proxies disagree about cache accounting: Anthropic's own
    field names appear through some, OpenAI's `prompt_tokens_details` through
    others, and cost is an OpenRouter extension. Boundary parsing, so be
    permissive here rather than everywhere downstream, and total: a value
    that is no count or no charge (`stated_count`, `stated_cost`) is as if
    the endpoint hadn't stated it.
    """
    if not raw or not isinstance(raw, dict):
        return Usage()

    details = raw.get("prompt_tokens_details")
    if not isinstance(details, dict):
        details = {}

    def first_int(*keys: str, source: dict[str, Any] | None = None) -> int:
        container = source if source is not None else raw
        for key in keys:
            count = stated_count(container.get(key))
            if count is not None:
                return count
        return 0

    # OpenRouter names a cache write `prompt_tokens_details.cache_write_tokens`
    # (live, Nov 15 2026: Claude through it wrote 12,965 tokens at the hour's
    # price, and the app read none of them); Tinfoil's router
    # `created_cache_tokens` (billed at the input price, as the app estimates).
    cache_creation = first_int("cache_creation_input_tokens", "cache_creation_tokens") or first_int(
        "cache_creation_tokens", "cache_write_tokens", "created_cache_tokens", source=details
    )
    cache_read = first_int("cache_read_input_tokens", "cache_read_tokens") or first_int(
        "cached_tokens", "cache_read_tokens", source=details
    )

    cost = stated_cost(raw.get("cost"))
    if cost is None:
        cost = stated_cost(raw.get("total_cost"))

    return Usage(
        prompt_tokens=first_int("prompt_tokens", "input_tokens"),
        completion_tokens=first_int("completion_tokens", "output_tokens"),
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
        cost=cost or 0.0,
        cost_reported=cost is not None,
    )


def checked_key(key: str, whose: str) -> str:
    """`key`, once it is one a header can carry (models/config.py
    `key_problem`): otherwise refused before anything is sent, in words that
    never quote it."""
    problem = key_problem(key, whose)
    if problem:
        raise ProviderError(problem)
    return key


def relay_said(text: str, limit: int = 200) -> str:
    """What NanoGPT's relay wrote (never sealed) as an error may quote it: one line, short, and
    without what a reader can't see. Controls and line separators become spaces, format
    characters (Unicode Cf: bidi overrides, zero-width and tag characters) go. Cut before it is
    cleaned: an error body can be large. (DA Toolkit's relay_said, Nov 2026.)"""
    kept = []
    for ch in str(text or "")[:4096]:
        category = unicodedata.category(ch)
        if category != "Cf":
            kept.append(" " if category in ("Cc", "Zl", "Zp") else ch)
    return " ".join("".join(kept).split())[:limit]


def error_detail(exc: BaseException, *, body_chars: int = 300) -> str:
    """The error for a dialog: its message, and the endpoint's own words when
    it sent any. "HTTP 401" alone told a tester nothing; the body says
    "insufficient balance" or "model not found"."""
    message = str(exc) or type(exc).__name__
    body = getattr(exc, "body", None)
    if isinstance(body, str) and body.strip() and body.strip() not in message:
        message += "\n\n" + body.strip()[:body_chars]
    return message


class UnconfiguredProvider(ChatProvider):
    """Stands in until an endpoint is set up: every call fails, saying so.

    The GUI used to fall back to the scripted mock when no provider was
    configured, and a first-run tester who pressed Regenerate got canned text
    saved as a real passage with an invented cost. Only `--mock` asks for that.
    """

    MESSAGE = "No endpoint is set up: set a base URL, API key and model under File → Settings."

    def build_payload(self, request: ChatRequest) -> dict[str, Any]:
        from sealedlore.providers.wire import build_chat_payload

        return build_chat_payload(request)

    def stream(self, request: ChatRequest) -> Iterator[StreamEvent]:
        raise ProviderError(self.MESSAGE)
        yield  # noqa: B901 - a generator that never yields
