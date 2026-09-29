"""A video from approval to file: sent, recorded, followed, stripped, kept;
where videos live, what a backup takes of them, and the prompt writer.

Never the network: the endpoint is an httpx MockTransport in the shapes
NanoGPT gave live, the writer the scripted mock provider. The video is
tests/data/tiny.mp4, a second of a test pattern made with ffmpeg."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.usage import usage_report
from sealedlore.engine.video_prompt import build_video_question, seconds_words
from sealedlore.engine.videos import MissingFrame, VideoRequest, follow_video, submit_video
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.image import ImageRef, RefUse
from sealedlore.providers.base import StreamCancelled
from sealedlore.providers.mock import MockChatProvider
from sealedlore.providers.videos import VideoClient, parse_nanogpt_video_models
from sealedlore.storage.archive import read_archive, write_archive
from sealedlore.storage.image_meta import stripped_or_same
from sealedlore.storage.images import all_image_files, is_safe_relative, write_story_file
from sealedlore.storage.picture_store import DiskPictures, MemoryPictures
from sealedlore.storage.repository import StoryBundle, read_api_log, save_story_bundle
from sealedlore.storage.video_meta import strip_video_metadata
from tests.conftest import make_exchange

DATA = Path(__file__).parent / "data"
MP4 = (DATA / "tiny.mp4").read_bytes()
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5E"
    "rkJggg=="
)


def seedance():
    listing = json.loads((DATA / "video_models_nanogpt.json").read_text())
    return next(m for m in parse_nanogpt_video_models(listing) if m.id == "bytedance/seedance-2.5")


def start_frame() -> RefUse:
    return RefUse(
        owner_kind="character",
        owner_id="char-serrik",
        owner_name="Serrik Vaun",
        ref_id="ref-1",
        file="images/refs/serrik.png",
        caption="in his coat",
    )


@pytest.fixture
def saved(tmp_path: Path, story, cast) -> StoryBundle:
    import base64

    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    save_story_bundle(bundle, root=tmp_path)
    write_story_file(story.id, "images/refs/serrik.png", base64.b64decode(PNG_B64), tmp_path)
    return bundle


class Endpoint:
    """NanoGPT's video API, as it answered live."""

    def __init__(self, *, states=("IN_PROGRESS", "COMPLETED"), fail: str | None = None):
        self.states = list(states)
        self.fail = fail
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/generate-video":
            return httpx.Response(
                202,
                json={"runId": "vid_1", "status": "pending", "cost": 0.9, "remainingBalance": 9},
            )
        if request.url.path == "/api/video/status":
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            if state == "FAILED":
                return httpx.Response(200, json={"data": {"status": "FAILED", "error": self.fail}})
            data: dict = {"status": state}
            if state == "COMPLETED":
                data["output"] = {"video": {"url": "https://cdn.example.net/v.mp4"}}
            return httpx.Response(200, json={"data": data})
        return httpx.Response(200, content=MP4)


def client(endpoint: Endpoint) -> VideoClient:
    videos = VideoClient(
        "https://nano-gpt.com/api/v1",
        "video-key",
        client=httpx.Client(transport=httpx.MockTransport(endpoint)),
    )
    videos.http._wake.wait = lambda _delay: False  # no real waiting between polls
    return videos


def request(saved: StoryBundle, **kwargs) -> VideoRequest:
    model = seedance()
    return VideoRequest(
        story_id=saved.story.id,
        model=model,
        prompt=kwargs.pop("prompt", "  He raises the lantern.\n"),
        settings=model.clean({"resolution": "480p", "duration": "5"}),
        anchor_node_id="a1",
        quote=0.9,
        **kwargs,
    )


# --- the job ------------------------------------------------------------------------


def test_a_video_is_recorded_as_soon_as_it_is_accepted_then_kept_stripped(
    tmp_path: Path, saved: StoryBundle
):
    endpoint = Endpoint()
    store = DiskPictures(saved.story.id, tmp_path)
    videos = client(endpoint)
    pending = submit_video(videos, request(saved, start_frame=start_frame()), store)
    assert pending.status == "pending" and pending.job_id == "vid_1" and pending.charged == 0.9
    sent = json.loads(endpoint.requests[0].content)
    assert sent["prompt"] == "  He raises the lantern.\n", "sent exactly as approved"
    assert sent["imageDataUrl"].startswith("data:image/png;base64,")
    assert sent["resolution"] == "480p" and sent["duration"] == "5"
    store.put_video(pending)
    assert store.videos()[0].status == "pending", "a paid video is on disk before it is made"

    seen: list[str] = []
    done = follow_video(videos, pending, store, progress=seen.append, poll_seconds=0)
    assert done.status == "done" and seen == ["IN_PROGRESS"]
    kept = store.read(done.file)
    assert kept == strip_video_metadata(MP4) and b"x264 - core" not in kept
    assert done.cost == 0.9 and done.cost_reported and not done.over_quote
    log = read_api_log(saved.story.id, root=tmp_path)
    kinds = [entry["kind"] for entry in log]
    assert kinds == ["video_request", "video_accepted", "video_response"]
    logged = json.dumps(log)
    assert "base64" not in logged and "remainingBalance" not in logged
    assert "<Serrik Vaun>" in logged
    lines = {line.label: line for line in usage_report(log).lines}
    assert lines["Videos"].cost == pytest.approx(0.9)


def test_a_video_that_fails_is_marked_and_not_counted(tmp_path: Path, saved: StoryBundle):
    store = DiskPictures(saved.story.id, tmp_path)
    videos = client(Endpoint(states=["FAILED"], fail="Content flagged."))
    pending = submit_video(videos, request(saved), store)
    failed = follow_video(videos, pending, store, poll_seconds=0)
    assert failed.status == "failed" and failed.error == "Content flagged."
    report = usage_report(read_api_log(saved.story.id, root=tmp_path))
    assert "Videos" not in {line.label for line in report.lines}, "NanoGPT refunds a failure"


def test_stop_waiting_leaves_it_pending_to_be_followed_again(tmp_path: Path, saved: StoryBundle):
    store = DiskPictures(saved.story.id, tmp_path)
    endpoint = Endpoint(states=["IN_PROGRESS"])
    videos = client(endpoint)
    pending = submit_video(videos, request(saved), store)
    videos.http._wake.wait = lambda _delay: True  # Stop pressed while waiting
    with pytest.raises(StreamCancelled):
        follow_video(videos, pending, store, poll_seconds=0)
    # Opened again later: the same record, a new client.
    endpoint.states = ["COMPLETED"]
    done = follow_video(client(endpoint), pending, store, poll_seconds=0)
    assert done.status == "done"


def test_a_missing_frame_sends_nothing(tmp_path: Path, saved: StoryBundle):
    endpoint = Endpoint()
    frame = start_frame().model_copy(update={"file": "images/refs/gone.png"})
    with pytest.raises(MissingFrame):
        submit_video(
            client(endpoint),
            request(saved, start_frame=frame),
            DiskPictures(saved.story.id, tmp_path),
        )
    assert endpoint.requests == []


def test_a_memory_chats_video_stays_in_memory(tmp_path: Path, saved: StoryBundle):
    logged: list[tuple[str, dict]] = []

    def log(kind, body):
        logged.append((kind, body))
        return str(len(logged))

    store = MemoryPictures(log)
    videos = client(Endpoint())
    pending = submit_video(videos, request(saved), store)
    store.put_video(pending)
    done = follow_video(videos, pending, store, poll_seconds=0)
    store.put_video(done)
    assert (
        store.read(done.file) and not (tmp_path / "stories" / saved.story.id / done.file).exists()
    )
    assert json.loads(store.all_files()["videos.json"])[0]["status"] == "done"
    store.close()
    assert store.videos() == []


# --- hidden data ---------------------------------------------------------------------


def test_a_videos_hidden_data_is_blanked_in_place():
    stripped = strip_video_metadata(MP4)
    assert len(stripped) == len(MP4), "nothing moves: the sample offsets stay right"
    assert b"test title" not in stripped and b"Lavf" not in stripped
    assert b"x264 - core" not in stripped
    assert strip_video_metadata(stripped) == stripped
    assert stripped_or_same(MP4) == stripped, "coming in from a backup, it is stripped too"


def test_a_file_that_isnt_a_video_is_refused():
    from sealedlore.storage.image_meta import MetadataError

    with pytest.raises(MetadataError):
        strip_video_metadata(b"not a video at all")
    with pytest.raises(MetadataError):
        strip_video_metadata(MP4[:40])


# --- where videos live ---------------------------------------------------------------


def test_videos_travel_in_a_backup_unless_left_out(tmp_path: Path, saved: StoryBundle):
    store = DiskPictures(saved.story.id, tmp_path)
    videos = client(Endpoint())
    done = follow_video(videos, submit_video(videos, request(saved), store), store, poll_seconds=0)
    store.put_video(done)
    assert is_safe_relative("videos.json") and is_safe_relative(done.file)
    whole = all_image_files(saved.story.id, tmp_path)
    assert done.file in whole and "videos.json" in whole
    lean = all_image_files(saved.story.id, tmp_path, videos=False)
    assert done.file not in lean and "videos.json" not in lean
    assert "images/refs/serrik.png" in lean, "pictures stay"
    path = tmp_path / "backup.json"
    write_archive(path, saved, [], whole)
    bundle, _log = read_archive(path)
    assert bundle.pending_files[done.file] == store.read(done.file)


# --- the prompt writer ---------------------------------------------------------------


def test_the_video_question_asks_for_motion_in_its_length_and_sound():
    question = build_video_question(
        direction="",
        held_name="Serrik Vaun",
        style="",
        seconds=seconds_words("5"),
        audio=True,
        start=start_frame(),
    )
    assert "# VIDEO PROMPT" in question and "5 seconds" in question
    assert "camera" in question and "made with sound" in question
    assert "starts from a picture: Serrik Vaun (in his coat)" in question
    assert "frozen moment" not in question, "a picture's rule, not a video's"
    silent = build_video_question(
        direction="him at the gate",
        held_name=None,
        style="oil painting",
        seconds="4 seconds",
        audio=False,
    )
    assert "no sound" in silent and "oil painting" in silent and "him at the gate" in silent


def make_session(tmp_path: Path, bundle: StoryBundle, replies: list[str]) -> StorySession:
    config = Config(
        providers=[
            ProviderConfig(
                name="nano",
                base_url="https://nano-gpt.com/api/v1",
                model="anthropic/claude-sonnet-4.6",
            )
        ],
        active_provider_name="nano",
        scene_reads="manual",
    )
    return StorySession(
        bundle,
        config,
        MockChatProvider(replies),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
    )


def test_the_writer_rides_the_story_and_is_logged_as_a_video_prompt(
    tmp_path: Path, saved: StoryBundle
):
    saved.cast[0].reference_images.append(ImageRef(id="ref-1", file="images/refs/serrik.png"))
    session = make_session(tmp_path, saved, ['{"prompt": "He turns; the camera holds."}'])
    prompt = session.write_video_prompt("", duration="5", audio=False, start=start_frame())
    assert prompt == "He turns; the camera holds."
    tail = session.provider.requests[-1].messages[-1].text
    assert "# VIDEO PROMPT" in tail and "5 seconds" in tail and "no sound" in tail
    kinds = [entry["kind"] for entry in read_api_log(saved.story.id, root=tmp_path)]
    assert kinds[-2:] == ["video_prompt_request", "video_prompt_response"]
    offered = session.video_frame_choices()
    assert [use.ref_id for use in offered] == ["ref-1"]


def test_video_is_off_without_a_key_of_its_own(tmp_path: Path, saved: StoryBundle):
    session = make_session(tmp_path, saved, [])
    assert session.video_client() is None
    session.config.video_endpoint.api_key = "video-key"
    assert session.video_client() is not None
