"""Video endpoints: where they are, what their listings say, what a video
costs, and how a job is sent and asked after. No network: the listings are
real captures (tests/data/video_models_*.json), the replies are mocked in
the shapes NanoGPT gave live."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from sealedlore.engine.video_price import quote_video, seedance_tokens
from sealedlore.models.config import Config, MediaEndpoint, ProviderConfig, media_api
from sealedlore.providers.base import ProviderError
from sealedlore.providers.images import build_image_payload, loggable_payload
from sealedlore.providers.media_http import SpendingCapReached
from sealedlore.providers.videos import (
    FrameInputs,
    VideoClient,
    VideoJob,
    build_video_payload,
    loggable_video_payload,
    nanogpt_root,
    parse_nanogpt_video_models,
    parse_openrouter_video_models,
)

DATA = Path(__file__).parent / "data"


def nanogpt_models():
    listing = json.loads((DATA / "video_models_nanogpt.json").read_text())
    return {m.id: m for m in parse_nanogpt_video_models(listing)}


def openrouter_models():
    listing = json.loads((DATA / "video_models_openrouter.json").read_text())
    return {m.id: m for m in parse_openrouter_video_models(listing)}


# --- where pictures and videos go ---------------------------------------------------------


def config(**kwargs) -> Config:
    chat = ProviderConfig(name="default", base_url="https://nano-gpt.com/api/v1", api_key="chat")
    return Config(providers=[chat], active_provider_name="default", **kwargs)


def test_the_service_is_read_from_the_address_unless_chosen():
    assert media_api("https://nano-gpt.com/api/v1") == "nanogpt"
    assert media_api("https://openrouter.ai/api/v1") == "openrouter"
    assert media_api("https://api.wavespeed.ai/api/v3") == "wavespeed"
    assert media_api("https://example.com/v1") == "openai"
    assert media_api("https://example.com/v1", "wavespeed") == "wavespeed"


def test_pictures_default_to_the_chat_endpoint_and_its_key():
    images = config().images()
    assert images.base_url == "https://nano-gpt.com/api/v1" and images.api_key == "chat"
    assert images.api == "nanogpt"


def test_pictures_elsewhere_never_get_the_chat_key():
    cfg = config(image_endpoint=MediaEndpoint(base_url="https://openrouter.ai/api/v1"))
    images = cfg.images()
    assert images.api == "openrouter" and images.api_key == ""
    cfg.image_endpoint.api_key = "or-key"
    assert cfg.images().api_key == "or-key"


def test_video_is_off_until_it_has_a_key_of_its_own():
    cfg = config()
    assert cfg.videos() is None, "never the chat's key by default"
    cfg.video_endpoint.api_key = "video-key"
    videos = cfg.videos()
    assert videos.api_key == "video-key" and not videos.shared_key
    assert videos.base_url == "https://nano-gpt.com/api/v1"


def test_the_chat_key_entered_for_video_by_hand_is_marked_shared():
    cfg = config()
    cfg.video_endpoint.api_key = "chat"
    assert cfg.videos().shared_key
    cfg.video_endpoint.api_key = "pics"
    cfg.image_endpoint.api_key = "pics"
    assert cfg.videos().shared_key


def test_a_media_address_must_be_https():
    with pytest.raises(ValueError):
        MediaEndpoint(base_url="http://example.com")
    assert MediaEndpoint(base_url="http://localhost:8000").base_url


# --- listings ---------------------------------------------------------------------------


def test_nanogpt_listing_gives_settings_frames_and_prices():
    model = nanogpt_models()["bytedance/seedance-2.5"]
    assert [p.key for p in model.params] == [
        "resolution",
        "duration",
        "aspect_ratio",
        "generate_audio",
    ]
    assert model.param("duration").options[0] == ("4", "4 seconds")
    assert model.start_frame and not model.end_frame
    assert model.max_references == 0, "NanoGPT takes reference pictures only as links"
    fast = nanogpt_models()["bytedance-seedance-2-0-fast"]
    assert fast.end_frame, "its listing declares last_image"
    assert ("enable_web_search", False) in fast.fixed
    assert all(p.key not in ("last_image", "reference_images") for p in fast.params)


def test_a_value_the_model_does_not_offer_is_never_sent():
    # Live: NanoGPT took resolution "9000p", made 720p and charged $1.80.
    model = nanogpt_models()["bytedance/seedance-2.5"]
    settings = model.clean({"resolution": "9000p", "duration": "5", "colour": "blue"})
    assert settings["resolution"] == "720p" and "colour" not in settings
    assert model.clean({"resolution": "480p"})["resolution"] == "480p"


def test_openrouter_listing_gives_its_own_choices():
    model = openrouter_models()["bytedance/seedance-2.5"]
    assert model.start_frame and model.end_frame and model.max_references
    assert model.param("duration").kind == "select"
    assert model.audio_key == "generate_audio"


# --- prices ----------------------------------------------------------------------------


def test_the_default_video_is_priced_as_it_was_charged():
    model = nanogpt_models()["bytedance/seedance-2.5"]
    quote = quote_video(model, model.clean({"resolution": "480p", "duration": "5"}))
    assert quote.amount == pytest.approx(0.90)  # pre-charged $0.90 live
    quote = quote_video(
        model, model.clean({"resolution": "480p", "duration": "4"}), start_frame=True
    )
    assert quote.amount == pytest.approx(0.72)  # pre-charged $0.72 live


def test_a_price_shape_that_proved_wrong_gives_no_price():
    # Listed at $0.35 for 5 s at 720p; NanoGPT charged $0.60.
    model = nanogpt_models()["bytedance-seedance-2-0-fast"]
    quote = quote_video(model, model.clean({"resolution": "720p", "duration": "5"}))
    assert quote.amount is None and "wrong" in quote.basis


def test_other_listed_shapes_are_read():
    models = nanogpt_models()
    base = models["seedance-video"]
    assert quote_video(base, base.clean({"resolution": "480p", "duration": "10"})).amount == (
        pytest.approx(0.24004)
    )
    kling = models["kling-v26-pro"]
    assert quote_video(kling, kling.clean({"duration": "10"})).amount is not None


def test_openrouter_prices_by_the_second_and_by_seedance_tokens():
    models = openrouter_models()
    kling = models["kwaivgi/kling-v3.0-std"]
    quote = quote_video(kling, kling.clean({"duration": "5", "generate_audio": False}))
    assert quote.amount == pytest.approx(0.42)
    seedance = models["bytedance/seedance-2.5"]
    quote = quote_video(
        seedance, seedance.clean({"duration": "5", "resolution": "480p", "aspect_ratio": "16:9"})
    )
    tokens = seedance_tokens("480p", "16:9", 5)
    assert tokens == pytest.approx(853.33 * 480 * 24 * 5 / 1024, rel=1e-3)
    assert quote.amount == pytest.approx(tokens * 0.0000107)


# --- payloads ------------------------------------------------------------------------------


def test_nanogpt_payload_carries_frames_as_data_urls_and_the_prompt_as_given():
    model = nanogpt_models()["bytedance-seedance-2-0-fast"]
    prompt = "  He raises the lantern.\n"
    payload = build_video_payload(
        model,
        prompt,
        model.clean({"resolution": "720p"}),
        FrameInputs(start="data:image/jpeg;base64,AA", end="data:image/jpeg;base64,BB"),
    )
    assert payload["prompt"] == prompt
    assert payload["imageDataUrl"].startswith("data:") and payload["last_image"].startswith("data:")
    assert payload["enable_web_search"] is False
    logged = loggable_video_payload(payload, {"start": "Jane", "end": "the harbour"})
    assert logged["imageDataUrl"] == "<Jane>" and logged["last_image"] == "<the harbour>"


def test_openrouter_payload_uses_frame_images_and_whole_seconds():
    model = openrouter_models()["bytedance/seedance-2.5"]
    payload = build_video_payload(
        model, "p", model.clean({"duration": "5"}), FrameInputs(start="data:image/png;base64,AA")
    )
    assert payload["duration"] == 5
    assert payload["frame_images"][0]["frame_type"] == "first_frame"


def test_openrouter_picture_payload():
    payload = build_image_payload("m", "p", "16:9", 1, ["data:x"], api="openrouter")
    assert payload["aspect_ratio"] == "16:9" and "size" not in payload
    assert payload["input_references"][0]["image_url"]["url"] == "data:x"
    assert loggable_payload(payload, ["Jane"])["input_references"] == ["<Jane>"]
    assert build_image_payload("m", "p", "2K", 1, [], api="openrouter")["resolution"] == "2K"


# --- the client ----------------------------------------------------------------------------


def client(handler, base="https://nano-gpt.com/api/v1", api=None) -> VideoClient:
    return VideoClient(
        base, "video-key", api=api, client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_nanogpt_paths_sit_beside_v1():
    assert nanogpt_root("https://nano-gpt.com/api/v1") == "https://nano-gpt.com/api"


def test_a_nanogpt_job_is_sent_asked_after_and_fetched_without_the_key():
    seen: list[httpx.Request] = []
    video = b"\x00\x00\x00\x18ftypisom"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/generate-video":
            return httpx.Response(
                202,
                json={"runId": "vid_1", "status": "pending", "cost": 0.9, "remainingBalance": 3.2},
            )
        if request.url.path == "/api/video/status":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "status": "COMPLETED",
                        "output": {"video": {"url": "https://cdn.example.net/v.mp4"}},
                    }
                },
            )
        return httpx.Response(200, content=video)

    videos = client(handler)
    job = videos.submit({"model": "m", "prompt": "p"})
    assert job.id == "vid_1" and job.charged == 0.9
    assert "remainingBalance" not in job.raw, "the account's balance never reaches the log"
    status = videos.status(job)
    assert status.state == "done" and status.urls == ("https://cdn.example.net/v.mp4",)
    assert videos.fetch(status.urls[0]) == video
    assert seen[1].url.params["requestId"] == "vid_1"
    assert "authorization" not in seen[2].headers, "a storage host never gets the key"


def test_a_failed_job_says_why():
    def handler(_request):
        return httpx.Response(
            200,
            json={"data": {"status": "FAILED", "error": "x", "userFriendlyError": "Flagged."}},
        )

    status = client(handler).status(VideoJob("vid_1", "nanogpt", "https://nano-gpt.com/api/v1"))
    assert status.state == "failed" and status.error == "Flagged."


def test_a_video_is_never_sent_twice_after_an_outage():
    calls = []

    def handler(_request):
        calls.append(1)
        return httpx.Response(503, text="busy")

    with pytest.raises(ProviderError):
        client(handler).submit({"model": "m"})
    assert len(calls) == 1, "a 503 may have been accepted: sending again could pay twice"


def test_a_refused_video_is_sent_again_after_a_short_rate_limit(monkeypatch):
    calls = []

    def handler(_request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "2"}, text="slow down")
        return httpx.Response(202, json={"runId": "vid_2"})

    videos = client(handler)
    monkeypatch.setattr(videos.http._wake, "wait", lambda _delay: False)
    assert videos.submit({"model": "m"}).id == "vid_2" and len(calls) == 2


def test_a_spending_cap_is_said_not_waited_out():
    # NanoGPT's per-key daily limit: 429 with Retry-After until midnight UTC.
    def handler(_request):
        return httpx.Response(429, headers={"retry-after": "30000"}, text="limit")

    with pytest.raises(SpendingCapReached, match="spending limit"):
        client(handler).submit({"model": "m"})


def test_an_openrouter_job_polls_its_url_and_fetches_with_the_key():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(
                202,
                json={"id": "gen-1", "polling_url": "/api/v1/videos/gen-1", "status": "pending"},
            )
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"mp4")
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "unsigned_urls": ["https://openrouter.ai/api/v1/videos/gen-1/content?index=0"],
                "usage": {"cost": 0.51},
            },
        )

    videos = client(handler, base="https://openrouter.ai/api/v1")
    job = videos.submit({"model": "m", "prompt": "p"})
    status = videos.status(job)
    assert str(seen[1].url) == "https://openrouter.ai/api/v1/videos/gen-1"
    assert status.cost == 0.51
    assert videos.fetch(status.urls[0]) == b"mp4"
    assert seen[2].headers["authorization"] == "Bearer video-key", "its own host needs the key"


def test_a_wavespeed_job_uploads_its_frames_first():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/media/upload/binary"):
            return httpx.Response(
                200, json={"data": {"download_url": "https://files.example/f.png"}}
            )
        if path.endswith("/result"):
            return httpx.Response(
                200,
                json={"data": {"id": "p1", "status": "completed", "outputs": ["https://o/v.mp4"]}},
            )
        return httpx.Response(200, json={"data": {"id": "p1", "status": "created"}})

    videos = client(handler, base="https://api.wavespeed.ai/api/v3")
    job = videos.submit(
        {
            "model": "bytedance/seedance-2.0/image-to-video",
            "prompt": "p",
            "image": "data:image/png;base64,AAAA",
        }
    )
    submitted = json.loads(seen[1].content)
    assert seen[1].url.path == "/api/v3/bytedance/seedance-2.0/image-to-video"
    assert submitted["image"] == "https://files.example/f.png" and "model" not in submitted
    assert videos.status(job).urls == ("https://o/v.mp4",)
