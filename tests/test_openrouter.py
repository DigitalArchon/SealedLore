"""OpenRouter as the app meets it, from what it sent live (Nov 15 2026):
its model listing, its stream's usage and cost, and Claude's cache through
it. Offline: the shapes are copied from the live replies, the prompts are
the tests' own."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from sealedlore.engine.catalog import chat_models
from sealedlore.engine.pricing import estimate_cost, parse_price
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.config import OPENROUTER_BASE_URL, ProviderConfig
from sealedlore.models.generation import GenerationParams
from sealedlore.providers.base import ChatRequest, StreamCompleted, TextDelta
from sealedlore.providers.openai_compat import OpenAICompatibleProvider

DATA = Path(__file__).parent / "data"


def listing() -> dict[str, dict]:
    body = json.loads((DATA / "openrouter_models.json").read_text())
    return {entry["id"]: entry for entry in body["data"]}


def test_the_model_list_reads_openrouters_prices_limits_and_reasoning():
    models = chat_models(listing())
    haiku = models["anthropic/claude-haiku-5.5"]
    assert round(haiku.prompt_price, 6) == 0.1 and round(haiku.completion_price, 6) == 0.5
    assert haiku.context_length == 1_000_000
    assert haiku.max_output_tokens == 128_000
    assert haiku.reasoning and "none" in haiku.reasoning_efforts
    assert haiku.vision is True
    glm = models["z-ai/glm-5.3"]
    assert glm.reasoning and glm.reasoning_efforts == ("max", "high", "low")
    nemo = models["mistralai/mistral-nemo"]
    assert not nemo.reasoning and nemo.reasoning_efforts == ()
    # A router priced at -1 (its price is whatever it routes to) has none.
    assert models["openrouter/auto"].prompt_price is None


def test_claudes_cache_prices_through_openrouter_give_its_own_bill():
    """The app's estimate of the live write and read, from the listed
    prices, is what OpenRouter charged: $0.002617 and $0.00015365."""
    price = parse_price(listing()["anthropic/claude-haiku-5.5"])
    model = "anthropic/claude-haiku-5.5"
    from sealedlore.providers.base import parse_usage

    write = parse_usage(
        {
            "prompt_tokens": 12990,
            "completion_tokens": 43,
            "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 12965},
        }
    )
    read = parse_usage(
        {
            "prompt_tokens": 12990,
            "completion_tokens": 43,
            "prompt_tokens_details": {"cached_tokens": 12965, "cache_write_tokens": 0},
        }
    )
    assert round(estimate_cost(write, price, model, cache_ttl="1h"), 7) == 0.002617
    assert round(estimate_cost(read, price, model, cache_ttl="1h"), 8) == 0.00015365


def _final_chunk() -> dict:
    """The last chunk of a live OpenRouter stream (Claude, a cache write)."""
    return {
        "id": "gen-1794744631-example",
        "provider": "Google",
        "model": "anthropic/claude-haiku-5.5",
        "object": "chat.completion.chunk",
        "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": 4433,
            "completion_tokens": 631,
            "total_tokens": 5064,
            "cost": 0.0011151,
            "is_byok": False,
            "prompt_tokens_details": {
                "cached_tokens": 0,
                "cache_write_tokens": 3563,
                "audio_tokens": 0,
                "video_tokens": 0,
            },
            "cost_details": {
                "upstream_inference_cost": 0.0011151,
                "upstream_inference_prompt_cost": 0.0007996,
                "upstream_inference_completions_cost": 0.0003155,
            },
            "completion_tokens_details": {"reasoning_tokens": 24, "image_tokens": 0},
        },
    }


def test_a_stream_from_openrouter_carries_its_cost_and_cache_write():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        chunks = [
            {"id": "gen-1", "provider": "Google", "choices": [{"delta": {"content": "The"}}]},
            {"id": "gen-1", "provider": "Google", "choices": [{"delta": {"content": " wind"}}]},
            _final_chunk(),
        ]
        body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, content=body.encode())

    config = ProviderConfig(name="OpenRouter", base_url=OPENROUTER_BASE_URL, api_key="sk-x")
    provider = OpenAICompatibleProvider(
        config, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    system = PromptMessage("system", (ContentPart("rules"), ContentPart("world", True)))
    request = ChatRequest(
        model="anthropic/claude-haiku-5.5",
        messages=[system, PromptMessage("user", (ContentPart("go"),))],
        params=GenerationParams(max_tokens=60),
        use_cache_control=True,
        cache_ttl="1h",
    )
    events = list(provider.stream(request))
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "The wind"
    done = events[-1]
    assert isinstance(done, StreamCompleted) and done.finish_reason == "stop"
    assert done.usage.cache_creation_tokens == 3563
    assert done.usage.cost == 0.0011151 and done.usage.cost_reported
    assert done.reasoning_tokens == 24
    # As the app sends it: the hour's marker on the world, and the beta header
    # (OpenRouter billed the write at the hour's price, $0.20 a million).
    sent = json.loads(seen[0].content)
    assert sent["messages"][0]["content"][1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "extended-cache-ttl-2025-04-11" in seen[0].headers["anthropic-beta"]
    assert seen[0].headers["authorization"] == "Bearer sk-x"
