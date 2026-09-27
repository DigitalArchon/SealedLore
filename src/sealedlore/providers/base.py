"""The ChatProvider interface and its stream events.

Kept behind an interface so a native Anthropic backend can be added later
without touching the rest of the app. Streaming is a plain synchronous
generator: Qt runs it on a worker thread and httpx handles the socket, which
is all the concurrency this app needs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from sealedlore.messages import PromptMessage
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


StreamEvent = TextDelta | ReasoningDelta | StreamCompleted


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
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
        """
        for _attempt in range(2):
            chunks: list[str] = []
            completed = StreamCompleted()
            for event in self.stream(request):
                if isinstance(event, TextDelta):
                    chunks.append(event.text)
                elif isinstance(event, StreamCompleted):
                    completed = event
            text = "".join(chunks)
            if text.strip() or completed.finish_reason != "length":
                break
        return text, completed


def parse_usage(raw: dict[str, Any] | None) -> Usage:
    """Read usage out of whatever shape the endpoint reports it in.

    OpenAI-compatible proxies disagree about cache accounting: Anthropic's own
    field names appear through some, OpenAI's `prompt_tokens_details` through
    others, and cost is an OpenRouter extension. Boundary parsing, so be
    permissive here rather than everywhere downstream.
    """
    if not raw or not isinstance(raw, dict):
        return Usage()

    details = raw.get("prompt_tokens_details")
    if not isinstance(details, dict):
        details = {}

    def first_int(*keys: str, source: dict[str, Any] | None = None) -> int:
        container = source if source is not None else raw
        for key in keys:
            value = container.get(key)
            if isinstance(value, (int, float)):
                return int(value)
        return 0

    cache_creation = first_int("cache_creation_input_tokens", "cache_creation_tokens") or first_int(
        "cache_creation_tokens", source=details
    )
    cache_read = first_int("cache_read_input_tokens", "cache_read_tokens") or first_int(
        "cached_tokens", "cache_read_tokens", source=details
    )

    cost = raw.get("cost")
    if not isinstance(cost, (int, float)):
        cost = raw.get("total_cost")
    reported = isinstance(cost, (int, float))
    if not reported:
        cost = 0.0

    return Usage(
        prompt_tokens=first_int("prompt_tokens", "input_tokens"),
        completion_tokens=first_int("completion_tokens", "output_tokens"),
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
        cost=float(cost),
        cost_reported=reported,
    )


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
