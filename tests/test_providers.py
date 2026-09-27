"""Provider layer: mock behaviour, payload building, SSE parsing, usage parsing.

Nothing here touches the network — the HTTP client is driven through an httpx
MockTransport.
"""

from __future__ import annotations

import json

import httpx
import pytest

from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.config import ProviderConfig
from sealedlore.models.generation import GenerationParams, ReasoningConfig
from sealedlore.models.node import Usage
from sealedlore.providers.base import (
    ChatRequest,
    ProviderError,
    ReasoningDelta,
    StreamCancelled,
    StreamCompleted,
    TextDelta,
    parse_usage,
)
from sealedlore.providers.mock import MockChatProvider
from sealedlore.providers.openai_compat import OpenAICompatibleProvider

MESSAGES = [
    PromptMessage(role="system", parts=(ContentPart(text="rules", cache_breakpoint=True),)),
    PromptMessage(role="user", parts=(ContentPart(text="the turn"),)),
]


def sse(*chunks: dict) -> bytes:
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def delta_chunk(content: str | None = None, **extra) -> dict:
    delta = {"content": content} if content is not None else {}
    delta.update(extra)
    return {"choices": [{"delta": delta}]}


def provider_with(handler) -> OpenAICompatibleProvider:
    config = ProviderConfig(name="test", base_url="https://example.test/api/v1", api_key="sk-x")
    return OpenAICompatibleProvider(
        config, client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def request_for(**kwargs) -> ChatRequest:
    kwargs.setdefault("model", "anthropic/claude-sonnet-4.5")
    kwargs.setdefault("messages", MESSAGES)
    return ChatRequest(**kwargs)


# --- mock provider --------------------------------------------------------


def test_mock_streams_text_then_completes():
    provider = MockChatProvider(["Hello there."], chunk_size=5)
    events = list(provider.stream(request_for()))

    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello there."
    assert isinstance(events[-1], StreamCompleted)
    assert events[-1].finish_reason == "stop"


def test_mock_records_requests_for_assertions():
    provider = MockChatProvider()
    list(provider.stream(request_for(use_cache_control=True)))
    assert provider.last_request is not None
    assert provider.last_request.model == "anthropic/claude-sonnet-4.5"
    assert provider.payloads[0]["messages"][0]["content"][0]["text"] == "rules"


def test_mock_cycles_through_scripted_responses():
    provider = MockChatProvider(["first", "second"])
    assert provider.complete(request_for())[0] == "first"
    assert provider.complete(request_for())[0] == "second"
    assert provider.complete(request_for())[0] == "first"


def test_mock_can_emit_reasoning():
    provider = MockChatProvider(["text"], reasoning="thinking")
    events = list(provider.stream(request_for()))
    assert isinstance(events[0], ReasoningDelta)
    assert events[0].text == "thinking"


def test_mock_can_fail():
    provider = MockChatProvider(error=ProviderError("boom"))
    with pytest.raises(ProviderError):
        list(provider.stream(request_for()))


def test_complete_drains_the_stream():
    provider = MockChatProvider(["one two three"], chunk_size=2)
    text, completed = provider.complete(request_for())
    assert text == "one two three"
    assert completed.usage.prompt_tokens == 1200


# --- payload --------------------------------------------------------------


def test_payload_sends_blocks_when_cache_control_is_on():
    provider = provider_with(lambda request: httpx.Response(200))
    payload = provider.build_payload(request_for(use_cache_control=True))

    assert payload["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}


def test_payload_sends_plain_strings_when_cache_control_is_off():
    provider = provider_with(lambda request: httpx.Response(200))
    payload = provider.build_payload(request_for(use_cache_control=False))
    assert payload["messages"][0]["content"] == "rules"


def test_payload_omits_unset_generation_params():
    provider = provider_with(lambda request: httpx.Response(200))
    payload = provider.build_payload(
        request_for(params=GenerationParams(temperature=0.8, max_tokens=500))
    )

    assert payload["temperature"] == 0.8
    assert payload["max_tokens"] == 500
    assert "top_p" not in payload
    assert "seed" not in payload
    assert "stop" not in payload
    assert "reasoning" not in payload


def test_payload_includes_reasoning_when_enabled():
    provider = provider_with(lambda request: httpx.Response(200))
    params = GenerationParams(reasoning=ReasoningConfig(enabled=True, effort="high"))
    payload = provider.build_payload(request_for(params=params))
    assert payload["reasoning"] == {"enabled": True, "effort": "high"}


def test_extra_body_is_merged_last():
    config = ProviderConfig(
        name="test",
        base_url="https://example.test/api/v1",
        extra_body={"usage": {"include": True}},
    )
    provider = OpenAICompatibleProvider(config)
    payload = provider.build_payload(request_for(extra_body={"transforms": ["middle-out"]}))

    assert payload["usage"] == {"include": True}
    assert payload["transforms"] == ["middle-out"]


def test_mock_and_real_provider_build_the_same_payload():
    """The inspector and the API log must show the same request in mock mode."""
    provider = provider_with(lambda request: httpx.Response(200))
    request = request_for(
        use_cache_control=True, params=GenerationParams(temperature=0.7, max_tokens=800)
    )
    assert MockChatProvider().build_payload(request) == provider.build_payload(request)


def test_endpoint_is_built_from_the_base_url():
    config = ProviderConfig(name="test", base_url="https://nano-gpt.com/api/v1/")
    assert (
        OpenAICompatibleProvider(config).endpoint == "https://nano-gpt.com/api/v1/chat/completions"
    )


# --- streaming ------------------------------------------------------------


def test_stream_parses_text_reasoning_and_usage():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse(
                delta_chunk(reasoning="weighing it up"),
                delta_chunk("The door "),
                delta_chunk("gives."),
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": 2000,
                        "completion_tokens": 40,
                        "cache_read_input_tokens": 1800,
                        "cache_creation_input_tokens": 120,
                        "cost": 0.0021,
                    },
                },
            ),
        )

    events = list(provider_with(handler).stream(request_for()))
    assert [e.text for e in events if isinstance(e, ReasoningDelta)] == ["weighing it up"]
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "The door gives."

    completed = events[-1]
    assert isinstance(completed, StreamCompleted)
    assert completed.finish_reason == "stop"
    assert completed.usage.prompt_tokens == 2000
    assert completed.usage.cache_read_tokens == 1800
    assert completed.usage.cache_creation_tokens == 120
    assert completed.usage.cost == pytest.approx(0.0021)


def test_stream_sends_the_payload_to_the_endpoint():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=sse(delta_chunk("ok")))

    list(provider_with(handler).stream(request_for(use_cache_control=True)))
    assert seen["url"] == "https://example.test/api/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-x"
    assert seen["body"]["model"] == "anthropic/claude-sonnet-4.5"


def test_stream_handles_anthropic_style_block_deltas():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse({"choices": [{"delta": {"content": [{"type": "text", "text": "hi"}]}}]}),
        )

    events = list(provider_with(handler).stream(request_for()))
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["hi"]


def test_http_error_raises_provider_error_with_the_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    with pytest.raises(ProviderError) as info:
        list(provider_with(handler).stream(request_for()))
    assert info.value.status_code == 401
    assert "bad key" in (info.value.body or "")


def test_error_inside_the_stream_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse({"error": {"message": "upstream exploded"}}))

    with pytest.raises(ProviderError, match="upstream exploded"):
        list(provider_with(handler).stream(request_for()))


def test_malformed_chunk_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"data: {not json}\n\n")

    with pytest.raises(ProviderError, match="malformed stream chunk"):
        list(provider_with(handler).stream(request_for()))


def test_a_timeout_raises_provider_error():
    # Callers that must never lose the author's turn catch ProviderError only.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(ProviderError, match="timed out"):
        list(provider_with(handler).stream(request_for()))


def test_a_connection_error_raises_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("", request=request)

    with pytest.raises(ProviderError, match="ConnectError"):
        list(provider_with(handler).stream(request_for()))


def test_a_chunk_that_is_not_an_object_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"data: [1, 2]\n\n")

    with pytest.raises(ProviderError, match="malformed stream chunk"):
        list(provider_with(handler).stream(request_for()))


def test_odd_choice_shapes_are_skipped_not_fatal():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse(
                {"choices": ["junk", {"delta": None}], "usage": "n/a"}, delta_chunk("fine")
            ),
        )

    events = list(provider_with(handler).stream(request_for()))
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "fine"


def test_stream_ignores_keepalive_and_comment_lines():
    def handler(request: httpx.Request) -> httpx.Response:
        body = b": keepalive\n\n" + sse(delta_chunk("fine"))
        return httpx.Response(200, content=body)

    events = list(provider_with(handler).stream(request_for()))
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "fine"


# --- usage parsing --------------------------------------------------------


def test_parse_usage_reads_openai_cached_token_details():
    usage = parse_usage(
        {
            "prompt_tokens": 900,
            "completion_tokens": 30,
            "prompt_tokens_details": {"cached_tokens": 800},
        }
    )
    assert usage.cache_read_tokens == 800
    assert usage.prompt_tokens == 900


def test_parse_usage_reads_anthropic_field_names():
    usage = parse_usage({"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 7})
    assert usage.prompt_tokens == 10
    assert usage.completion_tokens == 5
    assert usage.cache_creation_tokens == 7


def test_parse_usage_tolerates_nothing():
    assert parse_usage(None) == Usage()
    assert parse_usage({}) == Usage()
    assert parse_usage("n/a") == Usage()  # type: ignore[arg-type]
    assert parse_usage({"prompt_tokens": 3, "prompt_tokens_details": 7}).prompt_tokens == 3


def test_parse_usage_ignores_non_numeric_cost():
    assert parse_usage({"prompt_tokens": 1, "cost": "free"}).cost == 0.0


def test_a_missing_cost_is_taken_from_nano_gpts_pricing_record():
    """nano-gpt's usage.cost comes and goes; x_nanogpt_pricing carries the charge."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse(
                delta_chunk("Hi."),
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    "x_nanogpt_pricing": {"amount": 0.000105, "cost": 0.000105},
                },
            ),
        )

    completed = list(provider_with(handler).stream(request_for()))[-1]
    assert completed.usage.cost == pytest.approx(0.000105)
    assert completed.raw_usage["cost_source"] == "x_nanogpt_pricing"


def test_a_reported_cost_is_never_overwritten():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse(
                {
                    "choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "cost": 0.5},
                    "x_nanogpt_pricing": {"cost": 9.9},
                }
            ),
        )

    completed = list(provider_with(handler).stream(request_for()))[-1]
    assert completed.usage.cost == 0.5 and "cost_source" not in completed.raw_usage


def test_a_sibling_is_built_once_and_cancelled_with_its_provider():
    from sealedlore.models.config import ProviderConfig
    from sealedlore.providers.openai_compat import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(ProviderConfig(name="t", base_url="https://example.test"))
    side = provider.sibling()
    assert provider.sibling() is side and side is not provider
    provider.cancel()
    assert side._cancelled
    provider.reset_cancel()
    assert not side._cancelled
    provider.close()


def test_a_rate_limit_is_waited_out_before_anything_streams(monkeypatch):
    import sealedlore.providers.openai_compat as compat

    waits: list[float] = []
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "2"}, text="slow down")
        return httpx.Response(200, content=sse(delta_chunk("Hi")))

    provider = provider_with(handler)
    monkeypatch.setattr(provider._wake, "wait", lambda seconds: waits.append(seconds) or False)
    text, _ = provider.complete(request_for())
    assert text == "Hi" and len(calls) == 3 and waits == [2.0, 2.0]
    assert compat.RETRY_ATTEMPTS >= 2


def test_a_rate_limit_that_persists_still_fails(monkeypatch):
    provider = provider_with(lambda request: httpx.Response(503, text="unavailable"))
    monkeypatch.setattr(provider._wake, "wait", lambda seconds: False)
    with pytest.raises(ProviderError) as error:
        provider.complete(request_for())
    assert error.value.status_code == 503


def test_other_errors_are_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(400, text="bad request")

    with pytest.raises(ProviderError):
        provider_with(handler).complete(request_for())
    assert len(calls) == 1


def test_a_cancel_ends_the_wait_for_a_retry():
    import threading

    provider = provider_with(lambda request: httpx.Response(429, headers={"retry-after": "20"}))
    threading.Timer(0.05, provider.cancel).start()
    with pytest.raises(StreamCancelled):
        provider.complete(request_for())


def test_an_empty_reply_cut_off_by_reasoning_is_asked_for_again():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                200, content=sse({"choices": [{"delta": {}, "finish_reason": "length"}]})
            )
        return httpx.Response(200, content=sse(delta_chunk('{"ok": true}')))

    text, _ = provider_with(handler).complete(request_for())
    assert text == '{"ok": true}' and len(calls) == 2


def test_a_reported_zero_cost_is_a_cost():
    from sealedlore.providers.base import parse_usage

    usage = parse_usage({"prompt_tokens": 100, "completion_tokens": 10, "cost": 0})
    assert usage.cost == 0 and usage.cost_reported
    assert not parse_usage({"prompt_tokens": 100, "completion_tokens": 10}).cost_reported
