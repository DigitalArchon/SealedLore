"""A key sent over plain http to another machine on the local network can be
read by anyone on that network, a café's or a hotel's Wi-Fi included:
Settings asks before it saves one, once for all of them, No first, and
`sealedlore configure` warns. Sent to this computer a key never leaves it,
and a blank key is nothing to ask about. Offscreen, no network."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sealedlore.cli import main as cli_main
from sealedlore.models.config import (
    Config,
    EmbeddingProviderConfig,
    MediaEndpoint,
    ProviderConfig,
    is_local_host,
    is_loopback_host,
)
from sealedlore.storage.repository import load_config

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

NANO = "https://nano-gpt.com/api/v1"
LAN = "http://192.168.1.5:11434/v1"
# This computer; other machines on the local network; neither.
HERE = ("localhost", "LocalHost.", "dev.localhost", "127.0.0.1", "127.8.9.10", "::1")
THERE = ("192.168.1.5", "10.0.0.7", "172.20.1.2", "169.254.3.4", "fd12::3", "fe80::1%eth0")
NEITHER = ("nano-gpt.com", "8.8.8.8", "0.0.0.0", "a%2f.localhost", "127.0.0.1%.example", "")


def test_this_computer_is_not_another_machine():
    for here in HERE:
        assert is_loopback_host(here) and is_local_host(here), here
    assert is_loopback_host("::ffff:127.0.0.1")
    for there in (*THERE, "studio.local"):
        assert is_local_host(there) and not is_loopback_host(there), there
    for neither in NEITHER:
        assert not is_loopback_host(neither), neither


# --- the terminal ------------------------------------------------------------------


def test_configure_warns_when_a_key_would_go_unencrypted(tmp_path: Path, capsys):
    data = ["--data-dir", str(tmp_path)]
    cli_main([*data, "configure", "--base-url", LAN, "--api-key", "lan-key"])
    assert f"Warning: {LAN} is plain http to another machine" in capsys.readouterr().err
    # Once: the same key to the same machine, on a new path too, says nothing more.
    cli_main([*data, "configure", "--base-url", LAN, "--model", "llama3"])
    cli_main([*data, "configure", "--base-url", "http://192.168.1.5:11434/api"])
    assert capsys.readouterr().err == ""
    # This computer, no key, or https: nothing to warn of.
    cli_main([*data, "configure", "--base-url", "http://localhost:1234/v1", "--api-key", "k2"])
    other = ["--endpoint-name", "Other", "--base-url", "http://10.0.0.7/v1", "--api-key", ""]
    cli_main([*data, "configure", *other])
    cli_main([*data, "configure", "--endpoint-name", "Nano", "--base-url", NANO, "--api-key", "k3"])
    assert capsys.readouterr().err == ""


def test_configure_says_so_when_it_keeps_a_key_for_another_machine(tmp_path: Path, capsys):
    """Changing only the address keeps the key saved for the old one: a
    NanoGPT key would go, unencrypted, to a server on the network."""
    data = ["--data-dir", str(tmp_path)]
    cli_main([*data, "configure", "--base-url", NANO, "--api-key", "nano-key"])
    capsys.readouterr()
    cli_main([*data, "configure", "--base-url", LAN])
    err = capsys.readouterr().err
    assert "the API key already saved for 'default' is kept" in err and LAN in err
    assert '--api-key ""' in err
    assert load_config(root=tmp_path).active_provider().api_key == "nano-key", "said, not changed"
    cli_main([*data, "configure", "--base-url", LAN, "--api-key", ""])
    assert capsys.readouterr().err == ""


# --- Settings --------------------------------------------------------------------------

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from sealedlore.gui.settings_dialog import SettingsDialog, keys_in_the_clear  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def asked(monkeypatch, answer) -> list[tuple[str, str, object]]:
    """Every question, answered `answer`: (title, text, default button)."""
    questions: list[tuple[str, str, object]] = []

    def question(_parent, title, text, _buttons, default):
        questions.append((title, text, default))
        return answer

    monkeypatch.setattr(QMessageBox, "question", question)
    return questions


def nano(**changes) -> Config:
    endpoint = ProviderConfig(name="NanoGPT", base_url=NANO, api_key="nano-key", model="glm")
    return Config(providers=[endpoint], active_provider_name="NanoGPT", **changes)


def page(dialog: SettingsDialog) -> str:
    return dialog.tabs.tabText(dialog.tabs.currentIndex())


def test_every_key_going_unencrypted_is_asked_about_once_no_first(app, monkeypatch):
    cfg = nano()
    dialog = SettingsDialog(cfg, None)
    dialog._add_endpoint()
    dialog.provider_name.setText("Studio")
    dialog.base_url.setText(LAN)
    dialog.api_key.setText("lan-key")
    dialog.image_url.setText("http://10.0.0.7:7860/v1")
    dialog.image_key.setText("picture-key")
    dialog.endpoint_list.setCurrentRow(0)
    questions = asked(monkeypatch, QMessageBox.No)
    dialog._save()
    assert len(questions) == 1
    title, text, default = questions[0]
    assert title == "Send the keys unencrypted?" and default == QMessageBox.No
    assert f"Studio (Endpoints): {LAN}" in text
    assert "Pictures (Images): http://10.0.0.7:7860/v1" in text
    # No: open still, nothing saved, and back where the first one is set,
    # its endpoint shown.
    assert dialog.result() != QDialog.Accepted
    assert [p.name for p in cfg.providers] == ["NanoGPT"] and cfg.image_endpoint.base_url == ""
    assert page(dialog) == "Endpoints" and dialog.provider_name.text() == "Studio"
    assert dialog.error.text().startswith(f"Endpoints: the key for {LAN} would go unencrypted")

    questions = asked(monkeypatch, QMessageBox.Yes)
    dialog._save()
    assert len(questions) == 1 and dialog.result() == QDialog.Accepted
    assert cfg.endpoint_for("Studio::llama3").api_key == "lan-key"
    assert cfg.images().api_key == "picture-key"

    # Opened again: keys already going there aren't asked about, nor the same
    # key on a new path of the same machine.
    again = SettingsDialog(cfg, None)
    questions = asked(monkeypatch, QMessageBox.No)
    again.endpoint_list.setCurrentRow(1)
    again.base_url.setText("http://192.168.1.5:11434/api")
    again._save()
    assert questions == []
    assert cfg.endpoint_for("Studio::llama3").base_url == "http://192.168.1.5:11434/api"
    dialog.deleteLater()
    again.deleteLater()


def test_a_blank_key_or_this_computer_is_nothing_to_ask_about(app, monkeypatch):
    cfg = nano()
    dialog = SettingsDialog(cfg, None)
    questions = asked(monkeypatch, QMessageBox.No)
    dialog._add_endpoint()
    dialog.provider_name.setText("Studio")
    dialog.base_url.setText(LAN)
    dialog._add_endpoint()
    dialog.provider_name.setText("Here")
    dialog.base_url.setText("http://localhost:1234/v1")
    dialog.api_key.setText("local-key")
    dialog.private_url.setText("http://127.0.0.1:11434/v1")
    dialog.private_model.setText("llama3")
    dialog.private_key.setText("private-key")
    dialog._save()
    assert questions == []
    assert [p.name for p in cfg.providers] == ["NanoGPT", "Studio", "Here"]
    assert cfg.private().api_key == "private-key"
    dialog.deleteLater()


def test_a_key_taken_from_the_chat_endpoint_is_counted_where_it_goes(app, monkeypatch):
    """The chat endpoint's key goes with a blank one only to its own host:
    embeddings on another machine with no key of their own take none. Moving
    the default endpoint there keeps its key, and pictures and embeddings left
    blank go with it: one question, naming the one address the key goes to."""
    cfg = nano(embedding_provider=EmbeddingProviderConfig())
    dialog = SettingsDialog(cfg, None)
    questions = asked(monkeypatch, QMessageBox.No)
    dialog.embed_base_url.setText("http://10.0.0.8:8080/v1")
    dialog._save()
    assert questions == [] and cfg.embeddings().api_key == ""

    moved = SettingsDialog(cfg, None)
    moved.embed_base_url.setText("")
    moved.base_url.setText(LAN)
    moved._save()
    assert len(questions) == 1
    title, text, _default = questions[0]
    assert title == "Send the key unencrypted?" and text.count("192.168.1.5") == 1
    assert f"NanoGPT (Endpoints): {LAN}" in text
    assert cfg.active_provider().base_url == NANO, "No: nothing saved"
    dialog.deleteLater()
    moved.deleteLater()


def test_no_goes_back_to_the_page_the_address_is_on(app, monkeypatch):
    cfg = nano(embedding_provider=EmbeddingProviderConfig())
    dialog = SettingsDialog(cfg, None)
    asked(monkeypatch, QMessageBox.No)
    dialog.embed_base_url.setText("http://10.0.0.8:8080/v1")
    dialog.embed_api_key.setText("embedding-key")
    dialog._save()
    assert page(dialog) == "Lore"
    assert dialog.error.text().startswith("Lore: the key for http://10.0.0.8:8080/v1")
    assert cfg.embedding_provider == EmbeddingProviderConfig(), "nothing saved"
    dialog.deleteLater()


def test_each_key_is_counted_with_its_page():
    cfg = Config(
        providers=[ProviderConfig(name="Studio", base_url=LAN, api_key="k")],
        active_provider_name="Studio",
        embedding_provider=EmbeddingProviderConfig(base_url="http://10.0.0.8/v1", api_key="e"),
        # Another host and no key of its own: the chat key doesn't go there.
        image_endpoint=MediaEndpoint(base_url="http://studio.local:7860/v1"),
        video_endpoint=MediaEndpoint(base_url="http://10.0.0.9/v1", api_key="v"),
        # The chat endpoint's own host: its key goes there.
        private_provider=ProviderConfig(name="private", base_url=LAN, model="llama3"),
    )
    assert [(sent.page, sent.address, sent.key) for sent in keys_in_the_clear(cfg)] == [
        ("Endpoints", LAN, "k"),
        ("Lore", "http://10.0.0.8/v1", "e"),
        ("Video", "http://10.0.0.9/v1", "v"),
        ("Private", LAN, "k"),
    ]
