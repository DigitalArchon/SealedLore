"""The pre-beta review's fixes: what may leave the machine, what a private
scene leaves behind, and what a torn file costs. Nothing here touches the
network: HTTP goes through an httpx MockTransport."""

from __future__ import annotations

import base64
import json
import sys
import threading
from pathlib import Path

import httpx
import pytest

from sealedlore.engine.images import ImageRequest, run_image_request
from sealedlore.models.config import Config
from sealedlore.models.generation import GenerationParams
from sealedlore.providers.base import (
    ChatRequest,
    ProviderError,
    StreamCompleted,
    UnconfiguredProvider,
    error_detail,
)
from sealedlore.providers.http import is_secure, make_client
from sealedlore.providers.images import ImageClient
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.atomic import append_jsonl, read_jsonl
from sealedlore.storage.paths import story_dir
from sealedlore.storage.repository import (
    load_config_or_recover,
    load_story_bundle,
    save_config,
)
from tests.test_private import MARKER, build, enter, play, turn

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 16


# --- what leaves the machine ---------------------------------------------------


def test_https_or_loopback_http_and_nothing_else():
    assert is_secure("https://nano-gpt.com/api/v1")
    assert is_secure("http://localhost:11434/v1")
    assert is_secure("http://127.0.0.1:8080/v1")
    assert not is_secure("http://nano-gpt.com/api/v1")
    assert not is_secure("http://192.168.1.10:11434/v1")


def test_a_local_server_is_never_reached_through_a_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.test:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:3128")
    local = make_client("http://localhost:11434/v1")
    remote = make_client("https://nano-gpt.com/api/v1")
    try:
        assert local.trust_env is False
        assert remote.trust_env is True
    finally:
        local.close()
        remote.close()


def _image_client(handler, *, follow: bool = True) -> ImageClient:
    client = make_client(
        "https://api.test/v1", transport=httpx.MockTransport(handler), follow_redirects=follow
    )
    return ImageClient("https://api.test/v1", "k", client=client)


def test_a_redirect_off_https_is_refused_before_the_body_is_sent_again():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(307, headers={"location": "http://elsewhere.test/collect"})
        return httpx.Response(200, json={"data": []})

    with pytest.raises(ProviderError, match="insecure"):
        _image_client(handler).generate({"model": "m", "prompt": "the whole scene"})
    assert seen == ["https://api.test/v1/images/generations"]


def test_a_redirect_that_stays_on_https_is_followed():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(308, headers={"location": "https://api.test/v2/images"})
        return httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}], "cost": 0.01}
        )

    reply = _image_client(handler).generate({"model": "m", "prompt": "p"})
    assert len(seen) == 2 and reply.images == [PNG]


def test_without_an_endpoint_nothing_is_written_as_story():
    provider = UnconfiguredProvider()
    request = ChatRequest(model="m", messages=[], params=GenerationParams())
    with pytest.raises(ProviderError, match="File → Settings"):
        list(provider.stream(request))


def test_an_error_dialog_carries_the_endpoints_own_words():
    exc = ProviderError(
        "https://x.test/v1/chat/completions returned HTTP 402",
        status_code=402,
        body='{"error": "insufficient balance"}',
    )
    assert "insufficient balance" in error_detail(exc)
    assert error_detail(ProviderError("plain")) == "plain"


# --- what a private scene leaves behind ---------------------------------------


def _on_disk(session, root: Path) -> str:
    bundle = load_story_bundle(session.story.id, root=root)
    return json.dumps(bundle.model_dump(), ensure_ascii=False)


def test_the_scene_and_the_plot_are_frozen_in_a_private_scene(tmp_path: Path):
    session, _main, held = build(tmp_path)
    play(session, turn(held, "Before."))
    enter(session)
    play(session, turn(held, f"In the scene, {MARKER}."))
    scene = session.story.scene.model_copy(deep=True)
    scene.situation = f"They are alone now: {MARKER}"
    with pytest.raises(ValueError, match="private scene"):
        session.set_scene(scene)
    with pytest.raises(ValueError, match="private scene"):
        session.hold(session.cast[1].id)
    with pytest.raises(ValueError, match="private scene"):
        session.set_clock(0)
    with pytest.raises(ValueError, match="private scene"):
        session.undo_scene_update()
    session.discard_private()
    assert MARKER not in _on_disk(session, tmp_path)


def test_a_turn_with_no_private_model_leaves_nothing_in_the_tree(tmp_path: Path):
    session, _main, held = build(tmp_path)
    play(session, turn(held, "Before."))
    enter(session)
    # As after a restart into a disk-kept scene with no private model set up.
    session.private_provider = None
    before = [node.id for node in session.nodes]
    with pytest.raises(ValueError, match="no model"):
        play(session, turn(held, "Is anyone there?"))
    assert [node.id for node in session.nodes] == before


def test_a_branch_onto_a_memory_scenes_message_still_saves_a_valid_leaf(tmp_path: Path):
    session, _main, held = build(tmp_path)
    play(session, turn(held, "Before."))
    start = session.story.active_leaf_id
    enter(session)
    play(session, turn(held, f"Scene {MARKER}."))
    private_node = session.span_nodes(session.open_span)[0].id
    for _ in session.close_private("They talked, and parted."):
        pass
    # The message is still shown, so "Branch from here" can land on it.
    session.branch_from(private_node)
    saved = load_story_bundle(session.story.id, root=tmp_path)
    assert saved.story.active_leaf_id == start
    assert saved.story.active_leaf_id in {node.id for node in saved.nodes}
    assert MARKER not in json.dumps(saved.model_dump())


def test_closing_a_scene_forgets_what_was_worked_out_from_it(tmp_path: Path):
    session, _main, held = build(tmp_path)
    play(session, turn(held, "Before."))
    enter(session)
    play(session, turn(held, f"Scene {MARKER}."))
    assert session.last_retrieval is not None
    for _ in session.close_private("A summary."):
        pass
    assert session.last_retrieval is None
    assert session.private_provider is None


def test_a_memory_scenes_picture_direction_is_not_written_down(tmp_path: Path):
    session, _main, _held = build(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}], "cost": 0.02}
        )

    client = ImageClient(
        "https://api.test/v1", "k", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    request = ImageRequest(
        story_id=session.story.id,
        model="m",
        prompt="a dim room, two figures",
        size="1k",
        n=1,
        references=(),
        anchor_node_id=None,
        direction=f"the moment {MARKER}",
        direction_kept=False,
    )
    records = run_image_request(client, request, root=tmp_path)
    assert records[0].direction == ""
    log = (story_dir(session.story.id, tmp_path) / "api_log.jsonl").read_text()
    assert MARKER not in log


# --- summaries that ran out of room ---------------------------------------------


class CutOff(MockChatProvider):
    """A mock whose every reply stopped at the token limit."""

    def stream(self, request):
        for event in super().stream(request):
            if isinstance(event, StreamCompleted):
                event = StreamCompleted(
                    usage=event.usage,
                    finish_reason="length",
                    raw_usage=event.raw_usage,
                    response_id=event.response_id,
                )
            yield event


def test_a_cut_off_summary_never_stands_in_for_the_prose(tmp_path: Path):
    session, _main, held = build(tmp_path)
    session.config.auto_archive = False  # the chunk is archived by hand below
    for i in range(6):
        play(session, turn(held, f"Turn {i}."))
    session.provider = CutOff(["The town, in half a chap"])
    chunk = session.next_chunk()
    assert chunk
    with pytest.raises(ProviderError, match="cut off"):
        session.archive(chunk)
    assert not session.bundle.summaries


# --- files ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="mode bits; Windows: test_windows.py")
def test_the_settings_file_is_readable_by_its_owner_only(tmp_path: Path):
    save_config(Config(), root=tmp_path)
    assert (tmp_path / "config.json").stat().st_mode & 0o777 == 0o600


def test_unreadable_settings_start_the_app_with_the_defaults(tmp_path: Path):
    (tmp_path / "config.json").write_text("{not json", encoding="utf-8")
    config, notice = load_config_or_recover(root=tmp_path)
    assert config.active_provider() is None
    assert notice and "config.json.broken" in notice
    assert (tmp_path / "config.json.broken").read_text() == "{not json"


def test_a_corrupt_nodes_file_falls_back_to_its_backup(tmp_path: Path):
    session, _main, held = build(tmp_path)
    play(session, turn(held, "One."))
    play(session, turn(held, "Two."))
    folder = story_dir(session.story.id, tmp_path)
    (folder / "nodes.json").write_text("[{torn", encoding="utf-8")
    bundle = load_story_bundle(session.story.id, root=tmp_path)
    assert bundle.nodes, "the previous save stands in"
    assert bundle.notices and "backup" in bundle.notices[0]


def test_a_bad_log_line_costs_only_itself(tmp_path: Path):
    path = tmp_path / "api_log.jsonl"
    path.write_text('{"id": 1}\n{not json}\n{"id": 3}\n', encoding="utf-8")
    assert [record["id"] for record in read_jsonl(path)] == [1, 3]


def test_concurrent_appends_land_one_record_per_line(tmp_path: Path):
    path = tmp_path / "api_log.jsonl"

    def writer(number: int) -> None:
        for i in range(150):
            append_jsonl(path, {"writer": number, "i": i, "pad": "x" * 4000})

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 600
    assert all(isinstance(json.loads(line), dict) for line in lines)


def test_a_provider_url_assigned_later_is_checked_like_one_parsed():
    from sealedlore.models.config import ProviderConfig

    provider = ProviderConfig(name="p", base_url="https://api.test/v1")
    provider.base_url = "http://localhost:11434/v1"
    with pytest.raises(ValueError, match="https"):
        provider.base_url = "http://api.test/v1"


# --- the audit before the beta (Sept 2026) ---------------------------------------


def test_every_http_client_in_src_comes_from_make_client():
    """`make_client` sets the proxy rule for loopback endpoints and the redirect
    guard; a client built anywhere else has neither."""
    source = Path(__file__).resolve().parents[1] / "src" / "sealedlore"
    offenders = [
        str(path.relative_to(source))
        for path in sorted(source.rglob("*.py"))
        if path.name != "http.py" and "httpx.Client(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


@pytest.mark.skipif(sys.platform == "win32", reason="mode bits; Windows: test_windows.py")
def test_the_data_folder_and_its_logs_are_the_owners_alone(tmp_path: Path):
    """config.json holds the key and api_log.jsonl every prompt: the folder is
    0700 and an appended log 0600, whatever the umask."""
    import os

    from sealedlore.storage.paths import ensure_data_home

    old = os.umask(0o022)
    try:
        folder = ensure_data_home(tmp_path / "data")
        assert folder.stat().st_mode & 0o777 == 0o700
        log = folder / "api_log.jsonl"
        append_jsonl(log, {"kind": "request"})
        assert log.stat().st_mode & 0o777 == 0o600
        from sealedlore.storage.images import write_bytes_atomic

        picture = folder / "images" / "p.png"
        write_bytes_atomic(picture, PNG)
        assert picture.stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(old)


def test_a_memory_only_chat_teaches_the_config_nothing(tmp_path: Path):
    """The correction factor and the price cache used to be updated in memory
    and written by the window's next save, keyed by the chat's model."""
    from sealedlore.engine.session import StorySession
    from sealedlore.engine.tokens import TokenEstimator, fallback_counter
    from sealedlore.models.story import Story
    from sealedlore.storage.repository import StoryBundle

    class Priced(MockChatProvider):
        def fetch_model_prices(self):
            return {"TEE/gemma-3-27b": {"pricing": {"prompt": "1", "completion": "2"}}}

    story = Story(title="Incognito", mode="chat", chat_prompt="Be brief.", chat_keep="memory")
    story.defaults.main_model = "TEE/gemma-3-27b"
    config = Config()
    before = config.model_dump()
    session = StorySession(
        StoryBundle(story=story),
        config,
        Priced(["Reply."]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
    )
    session.private_tee = None
    from tests.test_chat import say

    say(session, "Hello there.")
    assert session.price_for("TEE/gemma-3-27b") is not None
    assert config.model_dump() == before
    assert not (tmp_path / "config.json").exists()
