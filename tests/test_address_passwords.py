"""A password written into an endpoint's address (https://user:pass@host/v1) is kept
in the keychain as an API key is, and never in config.json; nor is it shown in a
message, a notice, the CLI or a story's records. Ported from DA Assistant's tests
(729a415, Nov 2026) to SealedLore's keychain layer (storage/keychain.py).

Every test runs on conftest's MemoryKeychain: never the real one."""

# ruff: noqa: F811 - the saved story's fixture comes from test_video_jobs

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from sealedlore.models.config import (
    Config,
    EmbeddingProviderConfig,
    MediaEndpoint,
    ProviderConfig,
    addresses_shown,
    require_https,
    shown,
    without_password,
)
from sealedlore.providers.base import ChatRequest, ProviderError, error_detail
from sealedlore.providers.openai_compat import OpenAICompatibleProvider
from sealedlore.storage import keychain
from sealedlore.storage.repository import load_config, load_config_or_recover, save_config
from tests.test_video_jobs import saved  # noqa: F401 - a fixture

ENDPOINT = "https://bob:hunter2@llm.example/v1"
OLLAMA = "http://carol:pw-ollama@192.168.1.20:11434/v1"
PICTURES = "https://dave:pw-pics@pictures.example/v1"
VIDEO = "https://erin:pw-video@video.example/v1"
PRIVATE = "https://frank:pw-private@private.example/v1"
PASSWORDS = ("hunter2", "pw-ollama", "pw-pics", "pw-video", "pw-private")


def configured(base_url: str = ENDPOINT, **fields) -> Config:
    return Config(
        providers=[ProviderConfig(name="default", base_url=base_url, api_key="sk-test")],
        active_provider_name="default",
        **fields,
    )


def on_disk(root: Path) -> str:
    return (root / "config.json").read_text(encoding="utf-8")


def as_new_process(root: Path) -> Config:
    keychain.forget()
    return load_config(root=root)


# --- the address, with and without -------------------------------------------------------


def test_the_user_name_and_password_go_and_the_address_stays():
    for url, plain, said in (
        ("https://alice:s3cret@proxy.example:3128", "https://proxy.example:3128", None),
        ("socks5h://u:p@127.0.0.1:9050", "socks5h://127.0.0.1:9050", None),
        ("https://token@host.example/v1?a=b#c", "https://host.example/v1?a=b#c", None),
        ("https://u:p@ss@host.example/", "https://host.example/", None),
        ("http://u:p@[fd00::5]:11434", "http://[fd00::5]:11434", None),
        ("u:p@proxy:3128", "proxy:3128", "…@proxy:3128"),
        ("https://u:p@host:port/", "https://host:port/", "https://…@host:port/"),
    ):
        assert without_password(url) == plain, url
        assert shown(url) == (said or plain.replace("://", "://…@", 1)), url


def test_an_address_without_one_is_left_as_it_is():
    for url in (
        "",
        "http://localhost:11434",
        "HTTPS://Nano-GPT.com/api/v1/",
        "https://host.example/a@b",
        "https://host.example/?to=a@b",
    ):
        assert without_password(url) == url == shown(url), url


def test_no_message_shows_it():
    """Not an error the app writes, not httpx's own words, not Settings'
    refusal of an address that isn't https."""
    assert "hunter2" not in addresses_shown(
        f"Client error '401 Unauthorized' for url '{ENDPOINT}/models' and https://x.example/?a=b@c"
    )
    with pytest.raises(ValueError) as refused:
        require_https("http://bob:hunter2@llm.example/v1")
    assert "hunter2" not in str(refused.value) and "…@llm.example" in str(refused.value)
    assert "hunter2" not in str(ProviderError(f"{ENDPOINT}/chat/completions returned HTTP 401"))

    seen: list[httpx.Request] = []
    refusing = httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(401, text="nope"))
    provider = OpenAICompatibleProvider(
        ProviderConfig(name="p", base_url=ENDPOINT, model="m"),
        client=httpx.Client(transport=refusing),
    )
    with pytest.raises(ProviderError) as failed:
        list(provider.stream(ChatRequest(model="m", messages=[])))
    assert seen, "the request went, with its password"
    assert "hunter2" not in error_detail(failed.value)


# --- kept in the keychain -----------------------------------------------------------------


def test_an_address_typed_with_a_password_is_written_without_it(tmp_path, memory_keychain):
    config = configured(
        embedding_provider=EmbeddingProviderConfig(base_url=OLLAMA),
        image_endpoint=MediaEndpoint(base_url=PICTURES),
        video_endpoint=MediaEndpoint(base_url=VIDEO, api_key="sk-video"),
        private_provider=ProviderConfig(name="private", base_url=PRIVATE, model="m"),
    )
    save_config(config, root=tmp_path)
    text = on_disk(tmp_path)
    assert not any(secret in text for secret in PASSWORDS + ("bob", "carol"))
    data = json.loads(text)
    assert data["providers"][0]["base_url"] == "https://llm.example/v1"
    assert data["embedding_provider"]["base_url"] == "http://192.168.1.20:11434/v1"
    assert set(data["keychain_slots"]) >= {
        "address:provider:default",
        "address:embeddings",
        "address:images",
        "address:videos",
        "address:private",
    }
    assert ENDPOINT in memory_keychain.store.values()
    # In use as typed, now and at the next start.
    assert config.providers[0].base_url == ENDPOINT
    again = as_new_process(tmp_path)
    assert again.providers[0].base_url == ENDPOINT
    assert again.embedding_provider.base_url == OLLAMA
    assert (again.image_endpoint.base_url, again.video_endpoint.base_url) == (PICTURES, VIDEO)
    assert again.private_provider.base_url == PRIVATE


def test_one_left_in_the_file_moves_to_the_keychain(tmp_path, memory_keychain):
    """As an old config.json's keys do: at the next save, which the window
    makes once it is up (`keys_in_file`)."""
    (tmp_path / "config.json").write_text(json.dumps(configured().model_dump()))
    config = load_config(root=tmp_path)
    assert config.providers[0].base_url == ENDPOINT and keychain.keys_in_file(config)
    save_config(config, root=tmp_path)
    assert "hunter2" not in on_disk(tmp_path) and not keychain.keys_in_file(config)
    assert as_new_process(tmp_path).providers[0].base_url == ENDPOINT


def test_the_password_goes_only_to_the_address_it_was_typed_for(tmp_path, memory_keychain):
    save_config(configured(), root=tmp_path)
    # config.json edited by hand, or by another copy of the app, to another host.
    data = json.loads(on_disk(tmp_path))
    data["providers"][0]["base_url"] = "https://elsewhere.example/v1"
    (tmp_path / "config.json").write_text(json.dumps(data))
    assert as_new_process(tmp_path).providers[0].base_url == "https://elsewhere.example/v1"


def test_taking_the_password_out_takes_it_out_of_the_keychain(tmp_path, memory_keychain):
    config = configured()
    save_config(config, root=tmp_path)
    config.providers[0].base_url = "https://llm.example/v1"
    save_config(config, root=tmp_path)
    assert ENDPOINT not in memory_keychain.store.values()
    assert "address:provider:default" not in json.loads(on_disk(tmp_path))["keychain_slots"]
    assert as_new_process(tmp_path).providers[0].base_url == "https://llm.example/v1"


def test_a_renamed_endpoint_takes_it_along_and_leaves_nothing_behind(tmp_path, memory_keychain):
    config = configured()
    save_config(config, root=tmp_path)
    config.providers[0].name = "local"
    config.active_provider_name = "local"
    save_config(config, root=tmp_path)
    names = [name for _, name in memory_keychain.store if name.startswith("address:")]
    assert names == [f"address:provider:local @ {tmp_path.resolve()}"]
    assert as_new_process(tmp_path).providers[0].base_url == ENDPOINT


def test_saving_other_settings_writes_nothing_to_the_keychain_again(tmp_path, memory_keychain):
    writes = []
    original = memory_keychain.set_password
    memory_keychain.set_password = lambda *a: (writes.append(a[1]), original(*a))
    config = configured()
    for _ in range(3):
        save_config(config, root=tmp_path)
    assert sorted(writes) == sorted(
        keychain.account(slot, tmp_path)
        for slot in ("provider:default", "address:provider:default")
    )


def test_an_address_without_a_password_never_reaches_the_keychain(tmp_path, memory_keychain):
    save_config(configured("http://192.168.1.20:11434/v1"), root=tmp_path)
    assert json.loads(on_disk(tmp_path))["providers"][0]["base_url"] == (
        "http://192.168.1.20:11434/v1"
    )
    assert not any(name.startswith("address:") for _, name in memory_keychain.store)


def test_a_listings_source_keeps_no_password(tmp_path, memory_keychain):
    """Which endpoint the picture and video listings came from is kept in
    config.json: by the address without its password, the chat's own too
    when the picture address is blank and inherits it, and the listing is
    still taken for that endpoint's."""
    pytest.importorskip("PySide6")
    from sealedlore.gui.image_jobs import ImageCatalog
    from sealedlore.gui.video_jobs import VideoCatalog
    from sealedlore.providers.images import parse_image_models
    from tests.test_images import LISTING
    from tests.test_videos import nanogpt_models

    config = configured(
        image_endpoint=MediaEndpoint(base_url=PICTURES),
        video_endpoint=MediaEndpoint(base_url=VIDEO, api_key="sk-video"),
    )
    pictures = ImageCatalog(config, tmp_path)
    pictures._on_finished(parse_image_models(LISTING))
    videos = VideoCatalog(config, tmp_path)
    videos._on_finished(list(nanogpt_models().values()), config.videos().base_url)
    text = on_disk(tmp_path)
    assert not any(secret in text for secret in PASSWORDS + ("dave", "erin")), text
    assert config.image_models_source == "https://pictures.example/v1"
    assert pictures.models and videos.models, "the listings are still this endpoint's"

    # A blank picture address takes the chat's, password and all: not into the file.
    inherited = configured()
    ImageCatalog(inherited, tmp_path)._on_finished(parse_image_models(LISTING))
    assert "hunter2" not in on_disk(tmp_path)
    again = as_new_process(tmp_path)
    assert ImageCatalog(again, tmp_path).models, "after a restart too"


def test_an_older_files_listing_source_loses_its_password(tmp_path, memory_keychain):
    pytest.importorskip("PySide6")
    from sealedlore.gui.image_jobs import ImageCatalog
    from tests.test_images import LISTING

    data = configured(image_endpoint=MediaEndpoint(base_url=PICTURES)).model_dump()
    data["image_models"] = LISTING["data"]
    data["image_models_source"] = PICTURES
    data["video_models_source"] = VIDEO
    (tmp_path / "config.json").write_text(json.dumps(data))
    config = load_config(root=tmp_path)
    assert config.image_models_source == "https://pictures.example/v1"
    assert config.video_models_source == "https://video.example/v1"
    assert ImageCatalog(config, tmp_path).models, "the listing kept stands"
    save_config(config, root=tmp_path)
    assert not any(secret in on_disk(tmp_path) for secret in PASSWORDS)


# --- where the keychain can't take it, or isn't wanted ------------------------------------


def test_kept_in_the_file_by_choice(tmp_path, memory_keychain):
    """Config.key_storage "file" keeps the keys in config.json (the CLI, a
    test setup): and the address as typed, with them."""
    config = configured()
    save_config(config, root=tmp_path)
    config.key_storage = "file"
    save_config(config, root=tmp_path)
    assert ENDPOINT in on_disk(tmp_path)
    assert not any(name.startswith("address:") for _, name in memory_keychain.store)
    assert as_new_process(tmp_path).providers[0].base_url == ENDPOINT


def test_a_keychain_that_refuses_leaves_it_in_the_file_never_lost(tmp_path, memory_keychain):
    def refuse(*args):
        raise RuntimeError("Prompt dismissed")

    memory_keychain.set_password = refuse
    save_config(configured(), root=tmp_path)
    assert ENDPOINT in on_disk(tmp_path)
    assert "Prompt dismissed" in keychain.last_problem
    assert as_new_process(tmp_path).providers[0].base_url == ENDPOINT


def test_a_keychain_that_cant_be_read_is_said_and_the_password_never_deleted(
    tmp_path, memory_keychain
):
    save_config(configured(), root=tmp_path)
    keychain.forget()
    reads = memory_keychain.get_password

    def locked(*args):
        raise RuntimeError("Collection is locked")

    memory_keychain.get_password = locked
    config, notice = load_config_or_recover(root=tmp_path)
    assert config.providers[0].base_url == "https://llm.example/v1"
    assert "couldn't be read" in notice and "Collection is locked" in notice
    save_config(config, root=tmp_path)  # the window saves often: nothing is lost
    assert "address:provider:default" in json.loads(on_disk(tmp_path))["keychain_slots"]
    memory_keychain.get_password = reads
    assert as_new_process(tmp_path).providers[0].base_url == ENDPOINT


def test_one_missing_from_the_keychain_is_said(tmp_path, memory_keychain):
    save_config(configured(), root=tmp_path)
    keychain.forget()
    for entry in [e for e in memory_keychain.store if e[1].startswith("address:")]:
        del memory_keychain.store[entry]
    config, notice = load_config_or_recover(root=tmp_path)
    assert config.providers[0].base_url == "https://llm.example/v1"
    assert config.providers[0].api_key == "sk-test", "the key comes back all the same"
    assert "lost the password written into the address for provider:default" in notice


# --- the CLI, and a story's records -------------------------------------------------------


def test_configure_never_prints_it(tmp_path, memory_keychain, capsys):
    from sealedlore.cli import main

    main(["--data-dir", str(tmp_path), "configure", "--base-url", OLLAMA, "--api-key", "sk-1"])
    said = capsys.readouterr()
    assert "pw-ollama" not in said.out + said.err
    assert "…@192.168.1.20:11434" in said.out + said.err
    assert "pw-ollama" not in on_disk(tmp_path)
    assert as_new_process(tmp_path).providers[0].base_url == OLLAMA


def test_a_videos_record_keeps_its_address_without_the_password(tmp_path, saved):
    """A story's videos.json goes with the story, and with any backup of it: the
    record keeps the address without the password, and the video key's own
    address gives it back to ask after the video, only while it is that one."""
    from types import SimpleNamespace

    from sealedlore.engine.videos import submit_video
    from sealedlore.gui.window_videos import VideosWindow
    from sealedlore.providers.videos import VideoClient
    from sealedlore.storage.picture_store import DiskPictures
    from tests.test_video_jobs import Endpoint, request

    endpoint = Endpoint()
    typed = "https://erin:pw-video@nano-gpt.com/api/v1"
    videos = VideoClient(
        typed, "video-key", client=httpx.Client(transport=httpx.MockTransport(endpoint))
    )
    record = submit_video(videos, request(saved), DiskPictures(saved.story.id, tmp_path))
    assert endpoint.requests, "sent, with its password"
    assert record.base_url == "https://nano-gpt.com/api/v1"
    window = SimpleNamespace(
        config=Config(video_endpoint=MediaEndpoint(base_url=typed, api_key="video-key"))
    )
    assert VideosWindow._client_for(window, record).base_url == typed
    window.config.video_endpoint.base_url = "https://nano-gpt.com/api/v1"
    assert VideosWindow._client_for(window, record).base_url == "https://nano-gpt.com/api/v1"


def test_settings_carries_it_to_a_renamed_endpoint_or_refuses_the_rename(tmp_path, memory_keychain):
    """As a key is carried (`_carry_keys`): read back now when this session
    couldn't, so the rename doesn't lose it with the old name's slot."""
    from types import SimpleNamespace

    pytest.importorskip("PySide6")
    from sealedlore.gui.settings_dialog import SettingsDialog

    save_config(configured(), root=tmp_path)
    keychain.forget()
    dialog = SimpleNamespace(config=load_config(root=tmp_path), _root=tmp_path)
    renamed = ProviderConfig(name="local", base_url="https://llm.example/v1")
    SettingsDialog._carry_address(dialog, "default", renamed)
    assert renamed.base_url == ENDPOINT

    def locked(*args):
        raise RuntimeError("Collection is locked")

    keychain.forget()
    memory_keychain.get_password = locked
    renamed = ProviderConfig(name="local", base_url="https://llm.example/v1")
    with pytest.raises(ValueError, match="would lose it"):
        SettingsDialog._carry_address(dialog, "default", renamed)
