"""API keys in the system keychain, config.json as the fallback (storage/keychain.py).

Every test runs on conftest's MemoryKeychain: never the real one."""

from __future__ import annotations

import json
from pathlib import Path

from sealedlore.models.config import Config, EmbeddingProviderConfig, MediaEndpoint, ProviderConfig
from sealedlore.storage import keychain
from sealedlore.storage.repository import load_config, load_config_or_recover, save_config

KEY = "sk-nano-test-1234"


def configured(**fields) -> Config:
    return Config(
        providers=[
            ProviderConfig(name="default", base_url="https://nano-gpt.com/api/v1", api_key=KEY)
        ],
        active_provider_name="default",
        **fields,
    )


def on_disk(root: Path) -> dict:
    return json.loads((root / "config.json").read_text())


def test_keys_go_to_the_keychain_and_config_json_holds_none(tmp_path: Path, memory_keychain):
    config = configured(
        video_endpoint=MediaEndpoint(base_url="https://x.example", api_key="sk-video-secret")
    )
    save_config(config, root=tmp_path)

    text = (tmp_path / "config.json").read_text()
    assert KEY not in text and "sk-video-secret" not in text
    assert on_disk(tmp_path)["keychain_slots"] == ["provider:default", "videos"]
    assert sorted(v for v in memory_keychain.store.values()) == sorted([KEY, "sk-video-secret"])
    # The config in memory keeps its keys: nothing else in the app changes.
    assert config.providers[0].api_key == KEY

    keychain.forget()  # as a new process would
    loaded = load_config(root=tmp_path)
    assert loaded.providers[0].api_key == KEY
    assert loaded.video_endpoint.api_key == "sk-video-secret"


def test_another_data_folder_never_touches_the_real_keys(tmp_path: Path, memory_keychain):
    save_config(configured(), root=tmp_path)
    [(service, name)] = memory_keychain.store
    assert service == "SealedLore"
    assert name == f"provider:default @ {tmp_path.resolve()}"
    assert keychain.account("provider:default", None) == "provider:default"


def test_an_old_plain_text_config_moves_into_the_keychain(tmp_path: Path, memory_keychain):
    (tmp_path / "config.json").write_text(json.dumps(configured(key_storage="file").model_dump()))
    data = on_disk(tmp_path)
    del data["key_storage"], data["keychain_slots"]  # as written before the keychain
    (tmp_path / "config.json").write_text(json.dumps(data))

    config = load_config(root=tmp_path)
    assert config.providers[0].api_key == KEY and config.key_storage == "keychain"
    save_config(config, root=tmp_path)
    assert KEY not in (tmp_path / "config.json").read_text()
    assert KEY in memory_keychain.store.values()


def test_kept_in_the_file_by_choice(tmp_path: Path, memory_keychain):
    """The author's option for the CLI and test setups; a key already in the
    keychain moves out of it."""
    config = configured()
    save_config(config, root=tmp_path)
    config.key_storage = "file"
    save_config(config, root=tmp_path)
    assert on_disk(tmp_path)["providers"][0]["api_key"] == KEY
    assert on_disk(tmp_path)["keychain_slots"] == []
    assert memory_keychain.store == {}


def test_no_keychain_on_the_machine_keeps_keys_in_the_file(tmp_path: Path):
    class NoKeychain:
        priority = 0

    keychain.use_backend(NoKeychain())
    save_config(configured(), root=tmp_path)
    assert on_disk(tmp_path)["providers"][0]["api_key"] == KEY
    assert keychain.last_problem == "no system keychain was found"


def test_a_keychain_that_refuses_leaves_the_key_in_the_file_never_lost(
    tmp_path: Path, memory_keychain
):
    def refuse(*args):
        raise RuntimeError("Prompt dismissed")

    memory_keychain.set_password = refuse
    save_config(configured(), root=tmp_path)
    assert on_disk(tmp_path)["providers"][0]["api_key"] == KEY
    assert "Prompt dismissed" in keychain.last_problem


def test_a_keychain_that_cant_be_read_is_said_and_its_keys_are_never_deleted(
    tmp_path: Path, memory_keychain
):
    save_config(configured(), root=tmp_path)
    keychain.forget()
    reads = memory_keychain.get_password

    def locked(*args):
        raise RuntimeError("Collection is locked")

    memory_keychain.get_password = locked
    config, notice = load_config_or_recover(root=tmp_path)
    assert config.providers[0].api_key == ""
    assert "couldn't be read" in notice and "Collection is locked" in notice

    # Saved meanwhile (the window saves often): the pointer and the key stay.
    save_config(config, root=tmp_path)
    assert on_disk(tmp_path)["keychain_slots"] == ["provider:default"]
    memory_keychain.get_password = reads
    keychain.forget()
    assert load_config(root=tmp_path).providers[0].api_key == KEY


def test_a_key_missing_from_the_keychain_is_said(tmp_path: Path, memory_keychain):
    save_config(configured(), root=tmp_path)
    keychain.forget()
    memory_keychain.store.clear()
    config, notice = load_config_or_recover(root=tmp_path)
    assert config.providers[0].api_key == ""
    assert "no key for provider:default" in notice


def test_emptying_a_key_removes_it_and_a_renamed_provider_leaves_nothing_behind(
    tmp_path: Path, memory_keychain
):
    config = configured(embedding_provider=EmbeddingProviderConfig(api_key="emb"))
    save_config(config, root=tmp_path)
    config.embedding_provider.api_key = ""
    config.providers[0].name = "nano"
    save_config(config, root=tmp_path)
    names = [name for _, name in memory_keychain.store]
    assert names == [f"provider:nano @ {tmp_path.resolve()}"]
    assert on_disk(tmp_path)["keychain_slots"] == ["provider:nano"]


def test_an_unchanged_key_is_not_written_again(tmp_path: Path, memory_keychain):
    """The window saves the config after nearly everything."""
    writes = []
    original = memory_keychain.set_password
    memory_keychain.set_password = lambda *a: (writes.append(a), original(*a))
    config = configured()
    for _ in range(3):
        save_config(config, root=tmp_path)
    assert len(writes) == 1


# --- where Settings says a key goes --------------------------------------------------------


def test_settings_says_under_every_key_where_it_goes(tmp_path: Path):
    import os

    import pytest

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from sealedlore.gui.settings_dialog import SettingsDialog

    QApplication.instance() or QApplication([])
    dialog = SettingsDialog(configured(), None, root=tmp_path)
    # Chat, pictures, video, embeddings and private: one note each.
    assert len(dialog._key_notes) == 5
    assert all("Saved in the system keychain" in n.text() for n in dialog._key_notes)

    dialog.key_storage.setCurrentIndex(dialog.key_storage.findData("file"))
    expected = f"Saved in plain text in {tmp_path / 'config.json'}."
    assert all(n.text() == expected for n in dialog._key_notes)
    dialog._save()
    assert dialog.config.key_storage == "file"

    class NoKeychain:
        priority = 0

    keychain.use_backend(NoKeychain())
    dialog = SettingsDialog(configured(), None, root=tmp_path)
    assert (
        dialog._key_notes[0]
        .text()
        .startswith("No system keychain was found, so it is saved in plain text in")
    )
    dialog.close()


def test_the_window_moves_old_plain_text_keys_once_it_is_up(tmp_path: Path, memory_keychain):
    import os

    import pytest

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from sealedlore.gui.main_window import MainWindow

    QApplication.instance() or QApplication([])
    data = configured().model_dump()
    del data["key_storage"], data["keychain_slots"]  # as written before the keychain
    (tmp_path / "config.json").write_text(json.dumps(data))

    window = MainWindow(root=tmp_path, use_mock=True)
    assert KEY in (tmp_path / "config.json").read_text(), "not before the window is up"
    window._move_keys_to_keychain()
    assert KEY not in (tmp_path / "config.json").read_text()
    assert window.config.active_provider().api_key == KEY
    assert "moved out of config.json" in window.statusBar().currentMessage()
    window.close()
