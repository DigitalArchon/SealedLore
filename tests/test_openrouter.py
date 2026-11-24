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
from sealedlore.models.config import ProviderConfig
from sealedlore.models.generation import GenerationParams
from sealedlore.providers.base import ChatRequest, StreamCompleted, TextDelta
from sealedlore.providers.openai_compat import OpenAICompatibleProvider

DATA = Path(__file__).parent / "data"

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


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


# --- pictures ---------------------------------------------------------------------


def _image_endpoint() -> httpx.Client:
    """OpenRouter's picture listing as it was live, served offline."""
    body = json.loads((DATA / "openrouter_image_models.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/images/models":
            return httpx.Response(200, json={"data": body["data"]})
        prefix, suffix = "/api/v1/images/models/", "/endpoints"
        if path.startswith(prefix) and path.endswith(suffix):
            record = body["endpoints"].get(path[len(prefix) : -len(suffix)])
            return httpx.Response(200 if record else 404, json=record or {})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _priced(model: str):
    from sealedlore.providers.images import list_image_models

    found = list_image_models(OPENROUTER_BASE_URL, client=_image_endpoint(), wanted=model)
    return next(info for info in found if info.id == model)


def test_a_picture_is_priced_by_its_resolution_where_openrouter_says_so():
    klein = _priced("black-forest-labs/flux-3-image-klein")
    assert klein.per_image == {"768": 0.008, "1K": 0.009, "1.5K": 0.011, "2K": 0.014, "4K": 0.036}
    assert klein.price("768") == 0.008 and klein.price("4K") == 0.036
    assert klein.max_inputs == 10 and "1:1" in klein.resolutions and "1K" in klein.resolutions
    assert _priced("recraft/recraft-v4.1-flash").per_image == {"*": 0.007}
    # A free reference picture (input_image at $0) is no price of the picture.
    assert _priced("qwen/qwen-image-2.1-turbo").per_image == {"1K": 0.0184, "2K": 0.0184}


def test_a_picture_priced_by_the_token_or_megapixel_has_no_price_rather_than_a_wrong_one():
    """Gemini 3.1 Flash Lite Image lists $0.00003 an image token; live it
    charged $0.0336 for a 1:1 picture (1,120 tokens). Read as a picture's
    price, the dialog said a thousandth of that."""
    gemini = _priced("google/gemini-3.1-flash-lite-image")
    assert gemini.per_image == {} and gemini.price("1:1") is None
    assert gemini.max_inputs > 0
    assert _priced("black-forest-labs/flux.2-klein-4b").per_image == {}
    # Priced by quality and tier together ("low_1k"): nothing a size here names.
    assert _priced("x-ai/grok-imagine-image-2.0").per_image == {}


def test_a_picture_and_an_edit_go_as_openrouter_takes_them():
    """The request and reply shapes of the live picture and edit (Gemini 3.1
    Flash Lite Image, Nov 15 2026): a JPEG as b64_json with its media type,
    and the charge in `usage.cost`."""
    import base64

    from sealedlore.providers.images import ImageClient, build_image_payload, data_url

    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200,
            json={
                "created": 0,
                "data": [{"b64_json": base64.b64encode(jpeg).decode(), "media_type": "image/jpeg"}],
                "usage": {
                    "prompt_tokens": 1134,
                    "completion_tokens": 1120,
                    "cost": 0.0338835,
                    "completion_tokens_details": {"image_tokens": 1120},
                },
            },
        )

    client = ImageClient(
        OPENROUTER_BASE_URL,
        "sk-x",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.api == "openrouter"
    reference = data_url(jpeg, "image/jpeg")
    payload = build_image_payload(
        "google/gemini-3.1-flash-lite-image", "Make it blue.", "1:1", 1, [reference], "openrouter"
    )
    reply = client.generate(payload)
    assert reply.images == [jpeg] and reply.cost == 0.0338835
    request = sent[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/images"
    assert request.headers["authorization"] == "Bearer sk-x"
    body = json.loads(request.content)
    assert body["aspect_ratio"] == "1:1" and "resolution" not in body
    assert body["input_references"] == [{"type": "image_url", "image_url": {"url": reference}}]
