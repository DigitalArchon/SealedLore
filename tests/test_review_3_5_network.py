"""What the review of 3.5.0b1 found in what endpoints and servers may answer
(Nov 22 2026), one test a fault: each fails on 0f289da, but those marked as
guards. Every server is a MockTransport or one on 127.0.0.1 started here;
every key is made up."""

from __future__ import annotations

import math

import httpx
import pytest

from sealedlore.engine.speed_test import _reasoning_tokens as speed_reasoning_tokens
from sealedlore.engine.usage import usage_report
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.config import ProviderConfig
from sealedlore.providers.anthropic import AnthropicProvider
from sealedlore.providers.base import (
    COST_MOST,
    TOKENS_MOST,
    ChatRequest,
    ProviderError,
    StreamCompleted,
    parse_usage,
)
from sealedlore.providers.context_probe import detect_context
from sealedlore.providers.openai_compat import OpenAICompatibleProvider

KEY = "sk-test-PROBE"


def _request(model: str = "m") -> ChatRequest:
    return ChatRequest(
        model=model, messages=[PromptMessage(role="user", parts=(ContentPart(text="hi"),))]
    )


def _sse(*events: str) -> bytes:
    return "".join(f"data: {event}\n\n" for event in events).encode()


def _stream_client(body: bytes) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _chat(body: bytes) -> OpenAICompatibleProvider:
    config = ProviderConfig(name="t", base_url="https://chat.example/v1", api_key=KEY, model="m")
    return OpenAICompatibleProvider(config, client=_stream_client(body))


def _anthropic(body: bytes) -> AnthropicProvider:
    config = ProviderConfig(
        name="a",
        base_url="https://api.anthropic.com",
        api_key=KEY,
        model="claude-sonnet-4-6",
        api="anthropic",
    )
    return AnthropicProvider(config, client=_stream_client(body))


# --- N6: a reply's numbers are taken only when they are numbers a count or a
# --- charge can be, and JSON nothing can read is said as the endpoint's ------------

PASSAGE = '{"choices":[{"delta":{"content":"The passage."},"finish_reason":"stop"}]}'


@pytest.mark.parametrize(
    "value",
    [
        float("inf"),
        float("-inf"),
        float("nan"),
        True,
        -5,
        -0.5,
        "12",
        None,
        [3],
        pytest.param(10**400, id="400 digits"),
        1e300,
    ],
)
def test_a_count_or_a_cost_that_is_no_such_number_is_as_if_absent(value):
    usage = parse_usage(
        {
            "prompt_tokens": value,
            "completion_tokens": value,
            "cache_read_input_tokens": value,
            "prompt_tokens_details": {"cache_write_tokens": value},
            "cost": value,
        }
    )
    assert (usage.prompt_tokens, usage.completion_tokens) == (0, 0)
    assert (usage.cache_read_tokens, usage.cache_creation_tokens) == (0, 0)
    assert usage.cost == 0.0 and not usage.cost_reported, "estimated or unpriced, never this"


def test_what_an_endpoint_really_reports_is_read_as_it_was():
    usage = parse_usage(
        {
            "prompt_tokens": 1200,
            "completion_tokens": 80.0,
            "prompt_tokens_details": {"cached_tokens": 1000, "cache_write_tokens": 150},
            "cost": 0,
        }
    )
    assert (usage.prompt_tokens, usage.completion_tokens) == (1200, 80)
    assert (usage.cache_read_tokens, usage.cache_creation_tokens) == (1000, 150)
    assert usage.cost == 0.0 and usage.cost_reported, "a stated zero is a cost: an included model"
    assert parse_usage({"input_tokens": 7, "total_cost": 0.25}).cost == 0.25
    assert parse_usage({"prompt_tokens": TOKENS_MOST, "cost": COST_MOST}).cost == COST_MOST
    assert parse_usage({"prompt_tokens": TOKENS_MOST + 1}).prompt_tokens == 0
    assert not parse_usage({"cost": COST_MOST * 2}).cost_reported
    # A name whose value is no count gives way to the next name for the same.
    assert parse_usage({"prompt_tokens": None, "input_tokens": 9}).prompt_tokens == 9
    assert parse_usage({"cost": "free", "total_cost": 0.5}).cost == 0.5


@pytest.mark.parametrize(
    "usage",
    [
        '{"prompt_tokens":1e999,"completion_tokens":5}',
        '{"prompt_tokens":NaN,"completion_tokens":5}',
        '{"prompt_tokens":10,"completion_tokens":5,"cost":true}',
        '{"prompt_tokens":10,"completion_tokens":5,"cost":-5}',
        '{"prompt_tokens":10,"completion_tokens":5,"cost":NaN}',
        '{"prompt_tokens":10,"completion_tokens":5,"completion_tokens_details":'
        '{"reasoning_tokens":1e999}}',
    ],
)
def test_a_passage_is_kept_whatever_numbers_its_usage_holds(usage):
    provider = _chat(_sse(PASSAGE, '{"choices":[],"usage":' + usage + "}", "[DONE]"))
    text, completed = provider.complete(_request())
    assert text == "The passage."
    assert completed.usage.completion_tokens == 5
    assert completed.usage.prompt_tokens in (0, 10)
    assert completed.usage.cost == 0.0 and not completed.usage.cost_reported
    assert 0 <= completed.reasoning_tokens <= TOKENS_MOST


def test_nanogpts_pricing_record_fills_in_only_a_cost_that_is_one():
    def cost_with(usage: str, pricing: str) -> tuple[float, bool]:
        chunk = '{"choices":[],"usage":' + usage + ',"x_nanogpt_pricing":' + pricing + "}"
        completed = _chat(_sse(PASSAGE, chunk, "[DONE]")).complete(_request())[1]
        return completed.usage.cost, completed.usage.cost_reported

    assert cost_with('{"prompt_tokens":10}', '{"cost":0.004}') == (0.004, True)
    # A cost the usage states wrongly doesn't keep the pricing record's out.
    assert cost_with('{"prompt_tokens":10,"cost":true}', '{"cost":0.004}') == (0.004, True)
    assert cost_with('{"prompt_tokens":10}', '{"cost":-1}') == (0.0, False)
    assert cost_with('{"prompt_tokens":10}', '{"cost":1e999}') == (0.0, False)


ANTHROPIC_EVENTS = (
    '{"type":"message_start","message":{"id":"msg_1","usage":%s}}',
    '{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
    '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"The passage."}}',
    '{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}',
    '{"type":"message_stop"}',
)


@pytest.mark.parametrize(
    "usage",
    [
        '{"input_tokens":1e999}',
        '{"input_tokens":10,"cache_read_input_tokens":-1e999}',
        '{"input_tokens":NaN,"cache_creation_input_tokens":true}',
    ],
)
def test_anthropics_usage_is_counted_whatever_numbers_it_holds(usage):
    events = [ANTHROPIC_EVENTS[0] % usage, *ANTHROPIC_EVENTS[1:]]
    text, completed = _anthropic(_sse(*events)).complete(_request("claude-sonnet-4-6"))
    assert text == "The passage."
    assert completed.usage.completion_tokens == 5
    assert completed.usage.prompt_tokens in (0, 10)


def test_a_number_of_thousands_of_digits_in_a_stream_is_a_malformed_event():
    """A guard: `outside_json` has said it as a JSONDecodeError since the
    review's files fixes; both stream parses must go on taking it so."""
    long = '{"choices":[],"x":' + "9" * 5000 + "}"
    with pytest.raises(ProviderError, match="malformed stream chunk"):
        _chat(_sse(PASSAGE, long, "[DONE]")).complete(_request())
    events = [ANTHROPIC_EVENTS[0] % ('{"input_tokens":' + "9" * 5000 + "}"), *ANTHROPIC_EVENTS[1:]]
    with pytest.raises(ProviderError, match="malformed stream event"):
        _anthropic(_sse(*events)).complete(_request("claude-sonnet-4-6"))


def test_the_speed_tests_reasoning_count_is_one_or_none():
    def tokens(raw: dict) -> int | None:
        return speed_reasoning_tokens(StreamCompleted(raw_usage=raw))

    assert tokens({"completion_tokens_details": {"reasoning_tokens": 40}}) == 40
    assert tokens({"reasoning_tokens": 12.0}) == 12
    assert tokens({"reasoning_tokens": float("inf")}) is None
    assert tokens({"completion_tokens_details": {"reasoning_tokens": float("nan")}}) is None
    assert tokens({}) is None


def test_a_logged_cost_that_is_no_cost_is_unpriced_not_free():
    entries = [
        {"id": "r1", "kind": "response", "usage": {"prompt_tokens": 5, "cost": float("nan")}},
        {"id": "r2", "kind": "response", "usage": {"prompt_tokens": 5, "cost": True}},
        {"id": "r3", "kind": "response", "usage": {"prompt_tokens": 5, "cost": -2}},
        {"id": "r4", "kind": "response", "usage": {"prompt_tokens": 5, "cost": 0}},
    ]
    total = usage_report(entries).total
    assert total.calls == 4 and total.cost == 0.0 and math.isfinite(total.cost)
    assert total.unpriced == 3, "only the stated zero is a charge of nothing"


def _probe(handler) -> object:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return detect_context("http://127.0.0.1:11434/v1", KEY, "m", client=client)


@pytest.mark.parametrize(
    "body",
    [
        b"[1,2,3]",
        b'"models"',
        b"7",
        b'{"data":{"id":"m"}}',
        b'{"data":[{"id":"m","context_length":1e999}]}',
        b'{"data":[{"id":"m","context_length":NaN}]}',
        b'{"data":[{"id":"m","context_length":true}]}',
        b'{"data":[{"id":"m","context_length":-4096}]}',
        b'{"data":[{"id":"m","top_provider":[1]}]}',
        b'{"data":[{"id":"m","context_length":' + b"9" * 5000 + b"}]}",
        b'{"models":[{"name":5,"context_length":4096}]}',
        b'{"models":[{"key":"m","loaded_instances":[7,{"config":[1]}]}]}',
        b'{"models":[{"key":"m","loaded_instances":5}]}',
        b'{"model_info":[1],"parameters":{"num_ctx":1}}',
        b'{"default_generation_settings":[4096]}',
        b'{"default_generation_settings":{"n_ctx":1e999}}',
    ],
)
def test_a_server_that_answers_detect_oddly_has_not_said(body):
    """Whatever JSON every one of the servers' ways of asking gets back, the
    answer is a context size or none: never an exception past Detect."""
    assert _probe(lambda request: httpx.Response(200, content=body)) is None


def test_detect_still_reads_what_the_servers_really_say():
    def listing(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/models"):
            return httpx.Response(200, json={"data": [{"id": "m", "context_length": 32768}]})
        return httpx.Response(404)

    assert _probe(listing).tokens == 32768

    def ollama(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "m:latest", "context_length": 0}]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"parameters": "num_ctx 8192\nstop x"})
        return httpx.Response(404)

    assert _probe(ollama).tokens == 8192

    def llama_cpp(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 4096.0}})
        return httpx.Response(404)

    assert _probe(llama_cpp).tokens == 4096
