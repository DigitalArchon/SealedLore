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
    return json.loads((root / "config.json").read_text(encoding="utf-8"))


def test_keys_go_to_the_keychain_and_config_json_holds_none(tmp_path: Path, memory_keychain):
    config = configured(
        video_endpoint=MediaEndpoint(base_url="https://x.example", api_key="sk-video-secret")
    )
    save_config(config, root=tmp_path)

    text = (tmp_path / "config.json").read_text(encoding="utf-8")
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
    assert KEY not in (tmp_path / "config.json").read_text(encoding="utf-8")
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


def test_the_keychain_is_named_with_its_own_capitals(tmp_path: Path, memory_keychain, monkeypatch):
    monkeypatch.setattr(keychain.sys, "platform", "linux")
    save_config(configured(), root=tmp_path)
    keychain.forget()
    memory_keychain.store.clear()
    _config, notice = load_config_or_recover(root=tmp_path)
    assert notice.startswith("The system keychain (GNOME Keyring or KWallet) has no key"), notice
    assert keychain.where() == "the system keychain (GNOME Keyring or KWallet)"


def missing_at_the_start(tmp_path: Path, memory_keychain, **fields) -> Config:
    """A key the keychain is asked for at the start and hasn't got: its entry
    deleted outside the app, or the endpoint never needing one again."""
    save_config(configured(**fields), root=tmp_path)
    keychain.forget()  # as a new process would
    memory_keychain.store.clear()
    config, notice = load_config_or_recover(root=tmp_path)
    assert "no key for provider:default" in notice
    return config


def notice_at_the_next_start(tmp_path: Path) -> str | None:
    keychain.forget()
    return load_config_or_recover(root=tmp_path)[1]


def test_a_missing_key_stays_looked_for_until_the_author_says(tmp_path: Path, memory_keychain):
    """The window saves the config after nearly everything: none of that is
    the author's word that the key is gone for good, so it is still said."""
    config = missing_at_the_start(tmp_path, memory_keychain)
    save_config(config, root=tmp_path)
    assert on_disk(tmp_path)["keychain_slots"] == ["provider:default"]
    assert "no key for provider:default" in notice_at_the_next_start(tmp_path)


def test_settings_saved_with_a_missing_key_left_blank_stops_asking_for_it(
    tmp_path: Path, memory_keychain
):
    """The way out of "enter it again in Settings" for an endpoint that needs no
    key any more: Settings saved with it blank. Not said again at every start."""
    import os

    import pytest

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from sealedlore.gui.settings_dialog import SettingsDialog

    QApplication.instance() or QApplication([])
    config = missing_at_the_start(tmp_path, memory_keychain)
    dialog = SettingsDialog(config, None, root=tmp_path)
    dialog._save()
    dialog.close()
    save_config(config, root=tmp_path)
    assert on_disk(tmp_path)["keychain_slots"] == []
    assert notice_at_the_next_start(tmp_path) is None


def test_one_the_keychain_couldnt_be_asked_about_is_never_let_go_of(
    tmp_path: Path, memory_keychain
):
    """Locked, not gone: Settings saved, or the keys moved to the file, it is
    still looked for, and comes back once the keychain opens."""
    save_config(configured(), root=tmp_path)
    keychain.forget()
    reads = memory_keychain.get_password

    def locked(*args):
        raise RuntimeError("Collection is locked")

    memory_keychain.get_password = locked
    config = load_config(root=tmp_path)
    assert keychain.let_go_of_gone(config, tmp_path) == []
    config.key_storage = "file"
    save_config(config, root=tmp_path)
    assert on_disk(tmp_path)["keychain_slots"] == ["provider:default"]
    memory_keychain.get_password = reads
    keychain.forget()
    assert load_config(root=tmp_path).providers[0].api_key == KEY


def test_keys_kept_in_the_file_by_choice_stop_asking_for_one_the_keychain_hasnt_got(
    tmp_path: Path, memory_keychain
):
    config = missing_at_the_start(tmp_path, memory_keychain)
    config.key_storage = "file"
    save_config(config, root=tmp_path)
    assert on_disk(tmp_path)["keychain_slots"] == []
    assert notice_at_the_next_start(tmp_path) is None


def test_an_addresses_lost_password_can_be_let_go_of_too(tmp_path: Path, memory_keychain):
    config = configured()
    config.providers[0].base_url = "https://bob:pw@nano-gpt.com/api/v1"
    save_config(config, root=tmp_path)
    keychain.forget()
    for entry in [e for e in memory_keychain.store if e[1].startswith("address:")]:
        del memory_keychain.store[entry]
    config, notice = load_config_or_recover(root=tmp_path)
    assert "lost the password written into the address for provider:default" in notice
    save_config(config, root=tmp_path)
    assert "address:provider:default" in on_disk(tmp_path)["keychain_slots"], "still looked for"
    assert keychain.let_go_of_gone(config, tmp_path) == ["address:provider:default"]
    save_config(config, root=tmp_path)
    assert notice_at_the_next_start(tmp_path) is None
    assert load_config(root=tmp_path).providers[0].api_key == KEY, "the key itself stays"


def test_the_phones_key_is_never_let_go_of_by_settings(tmp_path: Path, memory_keychain):
    """Not a key the author types: only Phone devices… → New address… replaces it."""
    config = configured()
    config.phone.onion.api_key = "ED25519-V3:secret"
    save_config(config, root=tmp_path)
    keychain.forget()
    for entry in [e for e in memory_keychain.store if e[1].startswith("phone")]:
        del memory_keychain.store[entry]
    config = load_config(root=tmp_path)
    assert keychain.let_go_of_gone(config, tmp_path) == []
    assert "phone" in config.keychain_slots


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
    assert all(f"Saved in {keychain.where()}" in n.text() for n in dialog._key_notes)

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
    assert KEY in (tmp_path / "config.json").read_text(encoding="utf-8"), (
        "not before the window is up"
    )
    window._move_keys_to_keychain()
    assert KEY not in (tmp_path / "config.json").read_text(encoding="utf-8")
    assert window.config.active_provider().api_key == KEY
    assert "moved out of config.json" in window.statusBar().currentMessage()
    window.close()
