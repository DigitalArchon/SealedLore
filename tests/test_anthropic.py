"""Anthropic's own Messages API (providers/anthropic.py): the request shape,
the stream, the models list, and how the app comes to choose it.

Nothing here touches the network: the client is driven through an httpx
MockTransport, as the OpenAI-compatible provider's tests are.
"""

from __future__ import annotations

import json

import httpx
import pytest

from sealedlore.engine.catalog import chat_models
from sealedlore.engine.reasoning import default_note, listed, reasoning_for
from sealedlore.messages import PART_JOINER, ContentPart, PromptMessage
from sealedlore.models.config import (
    Config,
    EmbeddingProviderConfig,
    ProviderConfig,
    chat_api,
)
from sealedlore.models.generation import GenerationParams, ReasoningConfig
from sealedlore.providers import make_chat_provider
from sealedlore.providers.anthropic import (
    API_VERSION,
    EXTENDED_TTL_BETA,
    AnthropicProvider,
    build_messages_payload,
    render_messages,
)
from sealedlore.providers.anthropic_models import BUDGETS, parse_model, surface_of
from sealedlore.providers.base import (
    ChatRequest,
    ProviderError,
    ReasoningDelta,
    StreamCompleted,
    TextDelta,
)
from sealedlore.providers.openai_compat import OpenAICompatibleProvider
from sealedlore.providers.private_mode import PrivateModeProvider

ANTHROPIC = "https://api.anthropic.com"
NANO = "https://nano-gpt.com/api/v1"

MESSAGES = [
    PromptMessage(
        role="system",
        parts=(ContentPart(text="rules"), ContentPart(text="cast", cache_breakpoint=True)),
    ),
    PromptMessage(role="user", parts=(ContentPart(text="The opening."),)),
    PromptMessage(role="assistant", parts=(ContentPart(text="The door gives."),)),
    PromptMessage(
        role="user",
        parts=(ContentPart(text="history"), ContentPart(text="the turn", cache_breakpoint=True)),
    ),
]


def config(**changes) -> ProviderConfig:
    return ProviderConfig(**{"name": "a", "base_url": ANTHROPIC, "api_key": "sk-ant", **changes})


def provider_with(handler) -> AnthropicProvider:
    return AnthropicProvider(config(), client=httpx.Client(transport=httpx.MockTransport(handler)))


def request_for(**kwargs) -> ChatRequest:
    kwargs.setdefault("model", "claude-sonnet-4-6")
    kwargs.setdefault("messages", MESSAGES)
    kwargs.setdefault("params", GenerationParams())
    return ChatRequest(**kwargs)


def asked(effort: str) -> GenerationParams:
    return GenerationParams(reasoning=ReasoningConfig(enabled=True, effort=effort))


def sse(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def reply(
    text: str = "The door gives.",
    *,
    thinking: str | None = None,
    stop: str = "end_turn",
    usage: dict | None = None,
) -> bytes:
    """A streamed reply as Anthropic sends one: a thinking block when asked,
    the text in two pieces, a ping, the stop reason with the output usage."""
    events: list[dict] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "usage": {
                    "input_tokens": 120,
                    "cache_creation_input_tokens": 900,
                    "cache_read_input_tokens": 3000,
                    "output_tokens": 1,
                },
            },
        }
    ]
    index = 0
    if thinking:
        events += [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": thinking},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "abc"},
            },
            {"type": "content_block_stop", "index": 0},
        ]
        index = 1
    events += [
        {"type": "content_block_start", "index": index, "content_block": {"type": "text"}},
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text[:8]},
        },
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text[8:]},
        },
        {"type": "content_block_stop", "index": index},
        {"type": "ping"},
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": usage or {"output_tokens": 40},
        },
        {"type": "message_stop"},
    ]
    return sse(*events)


# --- the request ------------------------------------------------------------


def test_the_system_and_the_history_are_blocks_with_the_markers_where_assigned():
    payload = build_messages_payload(request_for(use_cache_control=True, cache_ttl="1h"))

    assert payload["model"] == "claude-sonnet-4-6"
    assert payload["stream"] is True
    assert payload["max_tokens"] == 4000
    assert payload["system"] == [
        {"type": "text", "text": "rules"},
        {
            "type": "text",
            "text": PART_JOINER + "cast",
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        },
    ]
    assert [m["role"] for m in payload["messages"]] == ["user", "assistant", "user"]
    assert payload["messages"][-1]["content"] == [
        {"type": "text", "text": "history"},
        {
            "type": "text",
            "text": PART_JOINER + "the turn",
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        },
    ]
    assert "provider" not in payload and "reasoning" not in payload


def test_blocks_read_as_the_plain_text_does():
    # The separator lives in the blocks (Anthropic joins them with nothing),
    # at the start of each later block so an added part leaves the earlier
    # bytes, and the cached prefix, as they were.
    system, wire = render_messages(MESSAGES, use_cache_control=True)
    assert "".join(b["text"] for b in system) == MESSAGES[0].text
    assert "".join(b["text"] for b in wire[-1]["content"]) == MESSAGES[-1].text
    longer = [*MESSAGES, PromptMessage(role="user", parts=(ContentPart(text="more"),))]
    again = render_messages(longer, use_cache_control=True)[1]
    assert again[-1]["content"][: len(wire[-1]["content"])] == wire[-1]["content"]


def test_consecutive_messages_of_one_role_become_one_message():
    # Anthropic wants user and assistant strictly alternating.
    messages = [
        PromptMessage(role="system", parts=(ContentPart(text="rules"),)),
        PromptMessage(role="system", parts=(ContentPart(text="more rules"),)),
        PromptMessage(role="user", parts=(ContentPart(text="one"),)),
        PromptMessage(role="user", parts=(ContentPart(text="two", cache_breakpoint=True),)),
        PromptMessage(role="assistant", parts=(ContentPart(text="reply"),)),
        PromptMessage(role="user", parts=(ContentPart(text="three"),)),
    ]
    system, wire = render_messages(messages, use_cache_control=True)
    assert [b["text"] for b in system] == ["rules", PART_JOINER + "more rules"]
    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
    assert wire[0]["content"] == [
        {"type": "text", "text": "one"},
        {"type": "text", "text": PART_JOINER + "two", "cache_control": {"type": "ephemeral"}},
    ]


def test_an_empty_part_makes_no_block_and_its_marker_moves_back():
    messages = [
        PromptMessage(
            role="user",
            parts=(ContentPart(text="turn"), ContentPart(text="", cache_breakpoint=True)),
        )
    ]
    _system, wire = render_messages(messages, use_cache_control=True)
    assert wire[0]["content"] == [
        {"type": "text", "text": "turn", "cache_control": {"type": "ephemeral"}}
    ]


def test_without_cache_control_the_blocks_carry_no_markers():
    payload = build_messages_payload(request_for(use_cache_control=False))
    blocks = payload["system"] + [b for m in payload["messages"] for b in m["content"]]
    assert all("cache_control" not in block for block in blocks)


def test_sampling_goes_only_where_the_api_takes_it():
    params = GenerationParams(temperature=0.8, top_p=0.9, max_tokens=500, stop=["END"])
    old = build_messages_payload(request_for(params=params))
    assert old["temperature"] == 0.8 and "top_p" not in old, "one of the two, never both"
    assert old["max_tokens"] == 500 and old["stop_sequences"] == ["END"]
    assert "presence_penalty" not in old and "seed" not in old

    alone = build_messages_payload(request_for(params=GenerationParams(top_p=0.9)))
    assert alone["top_p"] == 0.9

    # Opus 4.7 and later refuse temperature and top_p outright.
    new = build_messages_payload(request_for(model="claude-opus-5-5", params=params))
    assert "temperature" not in new and "top_p" not in new

    # And no model takes them beside thinking.
    thinking = GenerationParams(
        temperature=0.8, reasoning=ReasoningConfig(enabled=True, effort="low")
    )
    assert "temperature" not in build_messages_payload(request_for(params=thinking))


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        ("claude-sonnet-4-6", "low", {"thinking": {"type": "adaptive"}, "effort": "low"}),
        ("claude-opus-4-6", "max", {"thinking": {"type": "adaptive"}, "effort": "max"}),
        # No xhigh before Opus 4.7: the nearest below.
        ("claude-sonnet-4-6", "xhigh", {"thinking": {"type": "adaptive"}, "effort": "high"}),
        (
            "claude-opus-5-5",
            "high",
            {"thinking": {"type": "adaptive", "display": "summarized"}, "effort": "high"},
        ),
        (
            "claude-opus-4-7",
            "xhigh",
            {"thinking": {"type": "adaptive", "display": "summarized"}, "effort": "xhigh"},
        ),
    ],
)
def test_thinking_is_adaptive_with_an_effort_from_claude_4_6_on(model, effort, expected):
    payload = build_messages_payload(request_for(model=model, params=asked(effort)))
    assert payload["thinking"] == expected["thinking"]
    assert payload["output_config"] == {"effort": expected["effort"]}
    assert payload["max_tokens"] == 4000


def test_older_models_take_a_token_budget_with_room_above_it():
    for model in ("claude-haiku-4-5", "claude-sonnet-4-5-20250929", "claude-opus-4-1-20250805"):
        payload = build_messages_payload(request_for(model=model, params=asked("medium")))
        assert payload["thinking"] == {"type": "enabled", "budget_tokens": BUDGETS["medium"]}
        assert payload["max_tokens"] == BUDGETS["medium"] + 4000
        assert "output_config" not in payload


def test_nothing_asked_sends_nothing_unless_thinking_cant_be_turned_off():
    quiet = build_messages_payload(request_for(model="claude-sonnet-4-6"))
    assert "thinking" not in quiet and "output_config" not in quiet
    # Opus 5.5 thinks whatever is sent, at medium by default: the least is low.
    least = build_messages_payload(request_for(model="claude-opus-5-5"))
    assert "thinking" not in least and least["output_config"] == {"effort": "low"}


@pytest.mark.parametrize(
    ("model", "family", "version"),
    [
        ("claude-sonnet-4-6", "sonnet", (4, 6)),
        ("claude-opus-4-1-20250805", "opus", (4, 1)),
        ("claude-opus-4-20250514", "opus", (4, 0)),
        ("claude-3-5-sonnet-20241022", "sonnet", (3, 5)),
        ("claude-haiku-4-5", "haiku", (4, 5)),
        ("claude-fable-5-1", "fable", (5, 1)),
        ("anthropic/claude-opus-5-5", "opus", (5, 5)),
        ("claude-3-7-sonnet-latest", "sonnet", (3, 7)),
        ("some-other-model", "", (0, 0)),
    ],
)
def test_a_model_id_reads_as_its_family_and_version(model, family, version):
    assert parse_model(model) == (family, version)


def test_the_surface_follows_the_generation():
    assert surface_of("claude-haiku-4-5").adaptive is False
    assert surface_of("claude-sonnet-4-6").samples and not surface_of("claude-sonnet-4-6").xhigh
    assert not surface_of("claude-opus-4-8").thinks_by_default
    assert surface_of("claude-sonnet-5").thinks_by_default
    assert surface_of("claude-haiku-5-5").thinks_by_default
    assert surface_of("claude-mythos-5-1").thinks_by_default
    # Unread: the newest shape, nothing assumed about its default.
    unknown = surface_of("claude-next")
    assert unknown.adaptive and not unknown.samples and not unknown.thinks_by_default


def test_a_nanogpt_route_and_the_authors_extra_body_are_treated_apart():
    provider = AnthropicProvider(config(extra_body={"metadata": {"user_id": "u1"}}))
    payload = provider.build_payload(
        request_for(extra_body={"provider": {"sort": "latency"}, "service_tier": "auto"})
    )
    assert "provider" not in payload
    assert payload["metadata"] == {"user_id": "u1"} and payload["service_tier"] == "auto"
    provider.close()


def test_the_endpoint_takes_the_address_with_or_without_v1():
    assert AnthropicProvider(config()).endpoint == "https://api.anthropic.com/v1/messages"
    with_v1 = AnthropicProvider(config(base_url="https://api.anthropic.com/v1/"))
    assert with_v1.endpoint == "https://api.anthropic.com/v1/messages"


# --- the stream -------------------------------------------------------------


def test_the_request_goes_out_as_the_payload_with_anthropics_headers():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=reply())

    provider = provider_with(handler)
    request = request_for(use_cache_control=True, cache_ttl="1h")
    list(provider.stream(request))
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["x-api-key"] == "sk-ant"
    assert seen["headers"]["anthropic-version"] == API_VERSION
    assert seen["headers"]["anthropic-beta"] == EXTENDED_TTL_BETA
    assert "authorization" not in seen["headers"]
    assert seen["body"] == provider.build_payload(request), "the log shows what was sent"

    list(provider.stream(request_for(use_cache_control=True)))
    assert "anthropic-beta" not in seen["headers"], "five minutes needs no beta header"


def test_the_stream_parses_text_thinking_usage_and_the_stop_reason():
    provider = provider_with(
        lambda request: httpx.Response(200, content=reply(thinking="weighing it up"))
    )
    events = list(provider.stream(request_for()))

    assert [e.text for e in events if isinstance(e, ReasoningDelta)] == ["weighing it up"]
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "The door gives."
    completed = events[-1]
    assert isinstance(completed, StreamCompleted)
    assert completed.finish_reason == "stop"
    assert completed.response_id == "msg_1"
    # The app counts a prompt whole, cached tokens included.
    assert completed.usage.prompt_tokens == 120 + 900 + 3000
    assert completed.usage.cache_creation_tokens == 900
    assert completed.usage.cache_read_tokens == 3000
    assert completed.usage.completion_tokens == 40
    assert not completed.usage.cost_reported and completed.usage.cost == 0, "unpriced, never $0"
    assert completed.raw_usage["input_tokens"] == 120, "the record keeps Anthropic's own fields"
    assert completed.reasoning_tokens == len("weighing it up") // 4


def test_a_reply_cut_off_by_max_tokens_says_length_and_a_refusal_says_so():
    cut = provider_with(lambda request: httpx.Response(200, content=reply(stop="max_tokens")))
    assert list(cut.stream(request_for()))[-1].finish_reason == "length"
    refused = provider_with(lambda request: httpx.Response(200, content=reply(stop="refusal")))
    assert list(refused.stream(request_for()))[-1].finish_reason == "refusal"


def test_an_error_event_in_the_stream_raises():
    body = sse({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
    provider = provider_with(lambda request: httpx.Response(200, content=body))
    with pytest.raises(ProviderError, match="overloaded_error: Overloaded"):
        list(provider.stream(request_for()))


def test_a_refused_request_carries_the_endpoints_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "model: claude-x"},
            },
        )

    with pytest.raises(ProviderError, match="HTTP 400: model: claude-x") as info:
        list(provider_with(handler).stream(request_for()))
    assert info.value.status_code == 400


def test_overloaded_is_waited_out_before_anything_streams(monkeypatch):
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(529, headers={"retry-after": "3"}, text="overloaded")
        return httpx.Response(200, content=reply())

    provider = provider_with(handler)
    waits: list[float] = []
    monkeypatch.setattr(provider._wake, "wait", lambda seconds: waits.append(seconds) or False)
    text, _completed = provider.complete(request_for())
    assert text == "The door gives." and waits == [3.0]


def test_a_malformed_event_raises():
    provider = provider_with(lambda request: httpx.Response(200, content=b"data: {nope}\n\n"))
    with pytest.raises(ProviderError, match="malformed stream event"):
        list(provider.stream(request_for()))


def test_a_prompt_that_begins_with_the_model_is_refused_before_it_is_sent():
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=reply())

    backwards = [PromptMessage(role="assistant", parts=(ContentPart(text="Hello."),))]
    with pytest.raises(ProviderError, match="begin with a user message"):
        list(provider_with(handler).stream(request_for(messages=backwards)))
    assert not calls


def test_a_sibling_is_of_the_same_kind_and_cancelled_with_its_provider():
    provider = AnthropicProvider(config())
    side = provider.sibling()
    assert isinstance(side, AnthropicProvider) and provider.sibling() is side
    assert isinstance(provider.detached(), AnthropicProvider)
    provider.cancel()
    assert side._cancelled
    provider.reset_cancel()
    assert not side._cancelled
    provider.close()


# --- the models list --------------------------------------------------------


def listing_handler(seen: list) -> object:
    pages = {
        None: {
            "data": [
                {
                    "id": "claude-opus-5-5",
                    "display_name": "Claude Opus 5.5",
                    "created_at": "2026-06-01T00:00:00Z",
                    "type": "model",
                    "max_input_tokens": 1_000_000,
                    "max_tokens": 128_000,
                    "capabilities": {"thinking": {"supported": True}},
                }
            ],
            "has_more": True,
            "first_id": "claude-opus-5-5",
            "last_id": "claude-opus-5-5",
        },
        "claude-opus-5-5": {
            "data": [
                {
                    "id": "claude-sonnet-4-6",
                    "display_name": "Claude Sonnet 4.6",
                    "created_at": "2026-02-01T00:00:00Z",
                    "type": "model",
                }
            ],
            "has_more": False,
            "first_id": "claude-sonnet-4-6",
            "last_id": "claude-sonnet-4-6",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json=pages[request.url.params.get("after_id")])

    return handler


def test_the_listing_is_fetched_page_by_page_into_the_catalogs_shape():
    seen: list = []
    provider = provider_with(listing_handler(seen))
    listing = provider.fetch_model_prices()
    assert len(seen) == 2 and seen[1].url.params["after_id"] == "claude-opus-5-5"
    assert seen[0].headers["x-api-key"] == "sk-ant"

    models = chat_models(listing)
    opus = models["claude-opus-5-5"]
    assert opus.name == "Claude Opus 5.5" and opus.owner == "anthropic"
    assert opus.context_length == 1_000_000 and opus.max_output_tokens == 128_000
    assert opus.prompt_price is None and opus.completion_price is None, "no prices listed"
    assert opus.reasoning and "xhigh" in opus.reasoning_efforts
    assert listing["claude-opus-5-5"]["capabilities"]["thinking"] == {"supported": True}
    sonnet = models["claude-sonnet-4-6"]
    assert sonnet.reasoning_efforts == ("low", "medium", "high", "max")


def test_the_defaults_say_which_models_think_unasked_so_the_least_is_asked_for():
    provider = provider_with(listing_handler([]))
    listing = provider.fetch_model_prices()
    defaults = provider.fetch_reasoning_defaults()

    opus = listed(listing["claude-opus-5-5"], defaults["claude-opus-5-5"])
    assert opus.reasons_by_default is True
    assert reasoning_for("least", "claude-opus-5-5", opus) == ReasoningConfig(
        enabled=True, effort="low"
    )
    assert default_note(opus) == "Left to itself, it reasons."

    sonnet = listed(listing["claude-sonnet-4-6"], defaults["claude-sonnet-4-6"])
    assert sonnet.reasons_by_default is False
    assert reasoning_for("least", "claude-sonnet-4-6", sonnet) == ReasoningConfig()
    assert reasoning_for("high", "claude-sonnet-4-6", sonnet).effort == "high"
    assert default_note(sonnet) == "It reasons only when asked."


def test_a_listing_that_fails_is_a_provider_error():
    provider = provider_with(
        lambda request: httpx.Response(401, json={"error": {"message": "key"}})
    )
    with pytest.raises(ProviderError, match="HTTP 401: key"):
        provider.fetch_model_prices()


# --- choosing it --------------------------------------------------------------


def test_the_api_is_read_from_the_address_unless_chosen():
    assert chat_api(ANTHROPIC) == "anthropic"
    assert chat_api("https://api.anthropic.com/v1") == "anthropic"
    assert chat_api(NANO) == "openai"
    assert chat_api("https://openrouter.ai/api/v1") == "openai"
    assert chat_api("https://proxy.example/v1", "anthropic") == "anthropic"
    assert chat_api(ANTHROPIC, "openai") == "openai"
    assert ProviderConfig(name="p", base_url=ANTHROPIC).speaks() == "anthropic"
    assert ProviderConfig(name="p").api == "auto", "an older config.json reads as before"


def test_the_factory_is_the_one_place_that_chooses():
    assert isinstance(make_chat_provider(config()), AnthropicProvider)
    assert isinstance(
        make_chat_provider(ProviderConfig(name="n", base_url=NANO)), OpenAICompatibleProvider
    )
    chosen = ProviderConfig(name="x", base_url="https://proxy.example/v1", api="anthropic")
    assert isinstance(make_chat_provider(chosen), AnthropicProvider)
    sealed = make_chat_provider(ProviderConfig(name="s", base_url=NANO, model="private/glm"))
    assert isinstance(sealed, PrivateModeProvider)


def test_pictures_and_embeddings_never_inherit_from_an_anthropic_endpoint():
    cfg = Config(
        providers=[config()],
        active_provider_name="a",
        embedding_provider=EmbeddingProviderConfig(model="BAAI/bge-m3"),
    )
    assert cfg.images() is None, "Anthropic makes no pictures"
    assert cfg.embeddings() is None, "and no vectors: keywords alone"
    cfg.image_endpoint.base_url = NANO
    assert cfg.images().base_url == NANO and cfg.images().api_key == ""
    # The same config on an OpenAI-compatible endpoint inherits as before.
    cfg.providers[0] = ProviderConfig(name="a", base_url=NANO, api_key="k")
    assert cfg.embeddings().base_url == NANO and cfg.embeddings().api_key == "k"
