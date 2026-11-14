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
from sealedlore.models.config import Config, ProviderConfig, is_local_host, require_https
from sealedlore.models.generation import GenerationParams
from sealedlore.providers.base import (
    ChatRequest,
    ProviderError,
    StreamCompleted,
    UnconfiguredProvider,
    error_detail,
)
from sealedlore.providers.http import environment_proxy, is_secure, make_client
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


def test_https_or_http_on_the_local_network_and_nothing_else():
    """Plain http goes to this computer and the local network only (the
    author, Nov 4 2026: a local server on another machine at home)."""
    assert is_secure("https://nano-gpt.com/api/v1")
    assert is_secure("http://localhost:11434/v1")
    assert is_secure("http://127.0.0.1:8080/v1")
    for local in (
        "http://192.168.1.10:11434/v1",
        "http://10.0.0.5/v1",
        "http://172.20.1.2:1234/v1",
        "http://169.254.10.1/v1",
        "http://[fd12::3]:8080/v1",
        "http://studio.local:1234/v1",
    ):
        assert is_secure(local), local
    for public in (
        "http://nano-gpt.com/api/v1",
        "http://172.32.0.1/v1",
        "http://8.8.8.8/v1",
        "http://100.64.0.1/v1",
    ):
        assert not is_secure(public), public


def test_a_name_after_an_addresss_percent_sign_is_not_local():
    """An address cut at its "%" read as local, though the whole name is what
    DNS is asked for; an IPv6 zone id is still part of its address."""
    for url in (
        "http://192.168.1.1%.attacker.example/v1",
        "http://127.0.0.1%2e.example:8080/v1",
        "http://10.0.0.5%25.example/v1",
        "http://printer%2f.local/v1",
    ):
        assert not is_secure(url), url
        with pytest.raises(ValueError):
            require_https(url)
    assert is_local_host("fe80::1%eth0")
    assert is_local_host("studio.local.") and is_local_host("dev.localhost")


def test_an_address_with_a_dot_after_it_is_a_name_not_that_address():
    """glibc doesn't read "192.168.1.50." as that address (AI_NUMERICHOST
    fails): it asks DNS for the name, which may answer anything. It read as
    local, so plain http and the key went to wherever DNS said (DA Vibe
    Manager's netpolicy)."""
    from sealedlore.models.config import is_loopback_host

    for host in ("192.168.1.50.", "127.0.0.1.", "10.0.0.5..", "::1."):
        assert not is_local_host(host), host
        assert not is_loopback_host(host), host
    for url in ("http://192.168.1.50./v1", "http://127.0.0.1.:11434/v1"):
        assert not is_secure(url), url
        with pytest.raises(ValueError, match="must use https"):
            require_https(url)
    assert is_local_host("192.168.1.50") and is_loopback_host("127.0.0.1")
    assert is_local_host("localhost.") and is_loopback_host("localhost.")
    assert is_local_host("studio.local.")


@pytest.mark.parametrize(
    "url",
    [
        "https://nano-gpt.com /api/v1",
        "https://nano-gpt.com\\@evil.example/v1",
        "https://nano-gpt.com\t/api/v1",
        "https://nano-gpt.com\x00.evil.example/v1",
        "https://nano-gpt.com\u3000/v1",
        "https://nano%2dgpt.com/api/v1",
        "https://127.0.0.1%2e.evil.example/v1",
        "https://nano-gpt.com:99999/api/v1",
        "https://nano-gpt.com:443x/api/v1",
        "https://[fd00::5/v1",
        "https://:443/v1",
        "https://",
    ],
)
def test_an_address_that_cant_be_read_one_way_is_refused(url):
    """A backslash, whitespace or a control character, a port no address can
    have and a "%" escape in the host were let through: Python's parser and
    httpx's (or the resolver) may read such an address apart, and the key
    went where the reading the app didn't check said (DA Assistant's and DA
    Media Browser's netguard)."""
    with pytest.raises(ValueError) as refused:
        require_https(url)
    assert "s3cret" not in str(refused.value)
    with pytest.raises(ValueError):
        ProviderConfig(name="x", base_url=url)


@pytest.mark.parametrize(
    "url",
    [
        "https://nano-gpt.com:8443/api/v1",
        "  https://nano-gpt.com/api/v1  ",
        "http://[fe80::1%25eth0]:11434/v1",
        "https://bücher.example/v1",
        "https://alice:s3cret@nano-gpt.com/api/v1",
    ],
)
def test_an_address_read_one_way_is_kept_as_it_was(url):
    assert require_https(url) == url


def test_a_refused_address_never_shows_its_password():
    with pytest.raises(ValueError) as refused:
        require_https("https://alice:s3cret@nano-gpt.com /api/v1")
    assert "s3cret" not in str(refused.value) and "…@" in str(refused.value)


def test_the_address_a_server_listens_on_says_which_to_use():
    with pytest.raises(ValueError, match="127.0.0.1"):
        require_https("http://0.0.0.0:11434/v1")


def test_a_server_on_the_local_network_is_never_reached_through_a_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.test:3128")
    assert make_client("http://192.168.1.10:11434/v1")._trust_env is False
    assert make_client("https://nano-gpt.com/api/v1")._trust_env is True


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


# (environment, address): the proxy, or None.
PROXY_CASES = [
    ({"HTTPS_PROXY": "http://p.test:1"}, "https://nano-gpt.com/api/v1"),
    ({"HTTPS_PROXY": "p.test:1"}, "https://nano-gpt.com/api/v1"),
    ({"HTTP_PROXY": "http://p.test:1"}, "https://nano-gpt.com/api/v1"),
    ({"ALL_PROXY": "http://p.test:2"}, "https://nano-gpt.com/api/v1"),
    ({"HTTPS_PROXY": "http://p.test:1", "ALL_PROXY": "http://p.test:2"}, "https://nano-gpt.com"),
    ({"HTTPS_PROXY": "http://p.test:1", "NO_PROXY": "nano-gpt.com"}, "https://nano-gpt.com/api"),
    ({"HTTPS_PROXY": "http://p.test:1", "NO_PROXY": ".nano-gpt.com"}, "https://nano-gpt.com/api"),
    ({"HTTPS_PROXY": "http://p.test:1", "NO_PROXY": ".nano-gpt.com"}, "https://a.nano-gpt.com"),
    ({"HTTPS_PROXY": "http://p.test:1", "NO_PROXY": "gpt.com"}, "https://nano-gpt.com/api"),
    ({"HTTPS_PROXY": "http://p.test:1", "NO_PROXY": "x.test, *"}, "https://nano-gpt.com/api"),
    (
        {"HTTPS_PROXY": "http://p.test:1", "NO_PROXY": "https://nano-gpt.com"},
        "https://nano-gpt.com",
    ),
    ({"HTTPS_PROXY": "http://p.test:1", "NO_PROXY": "http://nano-gpt.com"}, "https://nano-gpt.com"),
    ({"HTTPS_PROXY": "http://p.test:1", "NO_PROXY": "nano-gpt.com:8443"}, "https://nano-gpt.com"),
    ({"HTTPS_PROXY": "http://p.test:1", "NO_PROXY": "203.0.113.5"}, "https://203.0.113.5/v1"),
    ({"HTTPS_PROXY": "http://p.test:1", "NO_PROXY": "2001:db8::5"}, "https://[2001:db8::5]/v1"),
    ({"HTTP_PROXY": "http://p.test:1"}, "http://192.168.1.10:11434/v1"),
    ({"HTTPS_PROXY": "http://p.test:1"}, "https://localhost:8443/v1"),
    ({}, "https://nano-gpt.com/api/v1"),
]


@pytest.mark.parametrize(("environment", "url"), PROXY_CASES)
def test_a_client_with_its_own_transport_is_given_the_proxy_make_client_would_use(
    monkeypatch, environment, url
):
    """`environment_proxy` reads the environment as httpx does for the clients
    `make_client` builds (checked here against httpx's own choice), so the
    sealed request goes where the preflight before it went."""
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    client = make_client(url)
    try:
        pool = client._transport_for_url(httpx.URL(url))._pool
        proxy = getattr(pool, "_proxy_url", None)
        expected = None if proxy is None else (proxy.host.decode(), proxy.port)
    finally:
        client.close()
    found = environment_proxy(url)
    assert (None if found is None else (httpx.URL(found).host, httpx.URL(found).port)) == expected


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


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_a_pictures_request_is_never_sent_on_by_a_redirect_even_on_https(code):
    """A 307/308 re-sends the body: the prompt and every reference picture went
    to whatever https host the endpoint named (DA Media Browser's
    follows_redirects). Now it is said, and where it pointed."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(code, headers={"location": "https://elsewhere.test/v2/images?s=1"})

    with pytest.raises(ProviderError, match="redirected") as refused:
        _image_client(handler).generate({"model": "m", "prompt": "the whole scene"})
    assert seen == ["https://api.test/v1/images/generations"]
    said = str(refused.value)
    assert f"(HTTP {code}) to https://elsewhere.test/v2/images." in said
    assert "Nothing was sent there" in said and "s=1" not in said


def test_a_request_carrying_the_key_follows_no_redirect():
    from sealedlore.providers.media_http import MediaHttp

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"location": "https://api.test/v1/moved"})

    http = MediaHttp(
        "https://api.test/v1",
        "k",
        timeout_seconds=30,
        client=make_client("https://api.test/v1", transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderError, match=r"redirected \(HTTP 302\) to https://api.test"):
        http.request("GET", "https://api.test/v1/videos/1")
    assert len(seen) == 1


def test_a_download_without_the_key_follows_a_relative_redirect():
    """A signed link's storage may redirect: a keyless GET still follows it,
    under make_client's guard. httpx runs response hooks before it resolves
    `Location`: read raw, a relative one ("/signed/p.png") had no scheme and
    every one was refused."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.test":
            return httpx.Response(200, json={"data": [{"url": "https://cdn.test/p.png"}]})
        if request.url.path == "/p.png":
            return httpx.Response(302, headers={"location": "/signed/p.png"})
        return httpx.Response(200, content=PNG)

    reply = _image_client(handler).generate({"model": "m", "prompt": "p"})
    assert [str(r.url) for r in seen] == [
        "https://api.test/v1/images/generations",
        "https://cdn.test/p.png",
        "https://cdn.test/signed/p.png",
    ]
    assert all("authorization" not in r.headers for r in seen[1:])
    assert reply.images == [PNG]


def test_a_paid_video_request_is_never_sent_on_by_a_redirect():
    from sealedlore.providers.videos import VideoClient

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(308, headers={"location": "https://collector.test/generate-video"})

    videos = VideoClient(
        "https://nano-gpt.com/api/v1",
        "video-key",
        client=make_client("https://nano-gpt.com/api/v1", transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderError, match="Nothing was sent there"):
        videos.submit({"model": "m", "prompt": "the whole scene"})
    assert seen == ["https://nano-gpt.com/api/generate-video"]


def test_an_upload_follows_no_redirect():
    from sealedlore.providers.media_http import follows_redirects

    assert not follows_redirects({}, {"files": {"file": ("a.png", b"x", "image/png")}})
    assert not follows_redirects({}, {"json": {}})
    assert not follows_redirects({"X-API-Key": "k"}, {})
    assert follows_redirects({}, {"params": {"a": "1"}})


@pytest.mark.parametrize(
    "location",
    ["https://192.168.1.1/admin", "http://192.168.1.1/admin", "http://localhost:8080/x"],
)
def test_a_server_on_the_internet_cannot_redirect_into_the_local_network(location):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": location})

    with pytest.raises(ProviderError, match="local network"):
        _image_client(handler).generate({"model": "m", "prompt": "p"})
    assert len(seen) == 1, "the redirect was not followed"


def test_a_local_server_may_redirect_on_the_local_network():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(307, headers={"location": "http://192.168.1.20:7860/v1/x"})
        return httpx.Response(200, content=PNG)

    client = make_client(
        "http://192.168.1.10:7860/v1", transport=httpx.MockTransport(handler), follow_redirects=True
    )
    assert client.get("http://192.168.1.10:7860/v1/file").content == PNG
    assert seen[-1] == "http://192.168.1.20:7860/v1/x"


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
    log = (story_dir(session.story.id, tmp_path) / "api_log.jsonl").read_text(encoding="utf-8")
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
    assert (tmp_path / "config.json.broken").read_text(encoding="utf-8") == "{not json"


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


def test_the_phones_server_is_the_only_one_in_src():
    """Phone access (gui/phone_server.py) listens on a unix socket in a folder
    only this user can open, for tor to forward to. Nothing else listens."""
    source = Path(__file__).resolve().parents[1] / "src" / "sealedlore"
    servers = ("QLocalServer(", "QTcpServer(", "HTTPServer(", "socketserver", ".bind(")
    offenders = {
        str(path.relative_to(source)): [s for s in servers if s in path.read_text(encoding="utf-8")]
        for path in sorted(source.rglob("*.py"))
        if path.name != "phone_server.py"
    }
    assert {name: found for name, found in offenders.items() if found} == {}


def test_tor_is_the_only_program_sealedlore_starts():
    """Phone access runs tor (gui/tor_service.py); nothing else is started."""
    source = Path(__file__).resolve().parents[1] / "src" / "sealedlore"
    starts = ("QProcess", "subprocess", "os.system(", "os.exec", "os.spawn", "Popen(")
    offenders = {
        str(path.relative_to(source)): [s for s in starts if s in path.read_text(encoding="utf-8")]
        for path in sorted(source.rglob("*.py"))
        if path.name != "tor_service.py"
    }
    assert {name: found for name, found in offenders.items() if found} == {}


def test_an_onion_address_is_no_exception_to_https_going_out():
    """The phones' page is plain http over the onion (Tor encrypts it end to
    end): that carve-out is for the one server SealedLore runs, never for an
    endpoint it calls."""
    from sealedlore.models.config import require_https

    with pytest.raises(ValueError):
        require_https("http://" + "a" * 56 + ".onion/v1")
    assert not is_secure("http://" + "a" * 56 + ".onion/v1")


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


def test_a_memory_only_chat_teaches_the_windows_config_nothing_either(tmp_path: Path):
    """The window's estimator holds the config's own factors: a factor learned
    from a memory-only chat was set there, and written by the next save."""
    from sealedlore.engine.session import StorySession
    from sealedlore.engine.tokens import TokenEstimator, fallback_counter
    from sealedlore.models.node import Usage
    from sealedlore.models.story import Story
    from sealedlore.storage.repository import StoryBundle
    from tests.test_chat import say

    story = Story(title="Incognito", mode="chat", chat_prompt="Be brief.", chat_keep="memory")
    story.defaults.main_model = "some/model"
    config = Config()
    before = config.model_dump()
    estimator = TokenEstimator(factors=config.token_correction_factors, counter=fallback_counter)
    session = StorySession(
        StoryBundle(story=story),
        config,
        MockChatProvider(["Reply."], usage=Usage(prompt_tokens=5_000, completion_tokens=5)),
        root=tmp_path,
        estimator=estimator,
    )
    say(session, "Hello there.")
    assert estimator.factor_for("some/model") > 1.0  # learned, for this run
    assert config.model_dump() == before
