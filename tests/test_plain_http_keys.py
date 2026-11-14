"""A key sent over plain http to another machine on the local network can be
read by anyone on that network, a café's or a hotel's Wi-Fi included:
Settings asks before it saves one, once for all of them, No first, and
`sealedlore configure` warns. Sent to this computer a key never leaves it,
and a blank key is nothing to ask about. Offscreen, no network."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from sealedlore.cli import main as cli_main
from sealedlore.models.config import (
    Config,
    EmbeddingProviderConfig,
    MediaEndpoint,
    ProviderConfig,
    is_local_host,
    is_loopback_host,
    key_origin,
)
from sealedlore.providers.base import ChatRequest
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


def test_configure_warns_of_a_password_in_the_address_with_no_key(tmp_path: Path, capsys):
    """It goes with every request as Basic credentials, in the clear over http:
    as much a key as the key is."""
    data = ["--data-dir", str(tmp_path)]
    address = "http://user:pw-lan@192.168.1.5:11434/v1"
    cli_main([*data, "configure", "--base-url", address, "--api-key", ""])
    err = capsys.readouterr().err
    assert "the password written into the address goes unencrypted" in err
    assert "pw-lan" not in err and "…@192.168.1.5:11434" in err
    cli_main([*data, "configure", "--base-url", address, "--model", "llama3"])
    assert capsys.readouterr().err == "", "once"
    cli_main([*data, "configure", "--base-url", "http://user:other@192.168.1.5:11434/v1"])
    assert "goes unencrypted" in capsys.readouterr().err, "another password is asked about anew"


def test_configure_says_so_when_it_keeps_a_key_for_another_machine(tmp_path: Path, capsys):
    """Changing only the address used to send the key saved for the old one,
    a NanoGPT key, unencrypted, to a server on the network. It is kept, never
    deleted, but sent nowhere but where it was saved for, and said."""
    data = ["--data-dir", str(tmp_path)]
    cli_main([*data, "configure", "--base-url", NANO, "--api-key", "nano-key"])
    capsys.readouterr()
    cli_main([*data, "configure", "--base-url", LAN])
    err = capsys.readouterr().err
    assert "was saved for https://nano-gpt.com, so it isn't sent to http://192.168.1.5:11434" in err
    assert '--api-key ""' in err and "goes unencrypted" not in err
    config = load_config(root=tmp_path)
    assert config.active_provider().api_key == "nano-key", "said, not changed"
    assert config.active_provider().sendable().api_key == ""
    cli_main([*data, "configure", "--base-url", LAN, "--api-key", ""])
    assert capsys.readouterr().err == ""


# --- Settings --------------------------------------------------------------------------

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton  # noqa: E402

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


def test_a_password_in_the_address_is_asked_about_with_no_key(app, monkeypatch):
    """http://user:pw@192.168.1.5/v1 sends Basic credentials in the clear with
    every request, a key or none."""
    cfg = nano()
    dialog = SettingsDialog(cfg, None)
    dialog._add_endpoint()
    dialog.provider_name.setText("Studio")
    dialog.base_url.setText("http://user:pw-lan@192.168.1.5:11434/v1")
    questions = asked(monkeypatch, QMessageBox.No)
    dialog._save()
    assert len(questions) == 1
    title, text, default = questions[0]
    assert title == "Send the key unencrypted?" and default == QMessageBox.No
    assert "Studio (Endpoints): http://…@192.168.1.5:11434/v1 (the password in its address)" in text
    assert "pw-lan" not in text
    assert dialog.error.text().startswith("Endpoints: the password in the address")
    assert "pw-lan" not in dialog.error.text()
    assert [p.name for p in cfg.providers] == ["NanoGPT"], "No: nothing saved"

    questions = asked(monkeypatch, QMessageBox.Yes)
    dialog._save()
    assert len(questions) == 1 and dialog.result() == QDialog.Accepted
    # Opened again: going there already. Another password is new.
    again = SettingsDialog(cfg, None)
    questions = asked(monkeypatch, QMessageBox.No)
    again._save()
    assert questions == []
    again.endpoint_list.setCurrentRow(1)
    again.base_url.setText("http://user:another@192.168.1.5:11434/v1")
    again._save()
    assert len(questions) == 1
    dialog.deleteLater()
    again.deleteLater()


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
    # The saved key sent to the new address: asked first (gui `_ask_moving_keys`), Yes here.
    moved._moves_answered[("provider:NanoGPT", "http://192.168.1.5:11434")] = True
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


def studio(dialog: SettingsDialog) -> None:
    """A second endpoint on another machine, its key typed, not saved; the
    story model on it."""
    dialog._add_endpoint()
    dialog.provider_name.setText("Studio")
    dialog.base_url.setText(LAN)
    dialog.api_key.setText("lan-key")
    dialog.endpoint_list.setCurrentRow(0)
    dialog.model.setText("Studio::llama3")


def test_browse_and_test_speed_ask_before_they_send_a_typed_key(app, monkeypatch):
    """Both send the keys as typed, before Save: the same question, No
    sending nothing, and a Yes standing for Save too."""
    from sealedlore.gui import settings_dialog as module
    from sealedlore.gui.model_picker import ModelCatalog

    opened: list[str] = []
    monkeypatch.setattr(module, "pick_model", lambda *_a, **_k: opened.append("picker"))

    class Speed:
        def __init__(self, targets, *_a, **_k):
            opened.append("speed: " + ", ".join(t.settings.name for t in targets))

        def exec(self):
            return 0

        def deleteLater(self):  # noqa: N802 - Qt naming
            pass

    monkeypatch.setattr(module, "SpeedTestDialog", Speed)
    cfg = nano()
    dialog = SettingsDialog(cfg, None, catalog=ModelCatalog())
    studio(dialog)
    questions = asked(monkeypatch, QMessageBox.No)
    assert dialog._browse_models("") is None
    dialog._test_speed()
    assert opened == [], "No: nothing sent"
    assert len(questions) == 2
    title, text, default = questions[0]
    assert title == "Send the key unencrypted?" and default == QMessageBox.No
    assert f"Studio (Endpoints): {LAN}" in text and text.endswith("No sends nothing.")
    assert f"Studio (Endpoints): {LAN}" in questions[1][1]

    questions = asked(monkeypatch, QMessageBox.Yes)
    dialog._browse_models("")
    dialog._test_speed()
    assert opened[0] == "picker" and opened[1].startswith("speed: ") and "Studio" in opened[1]
    assert len(questions) == 1, "a Yes once is enough"
    dialog._save()
    assert len(questions) == 1 and dialog.result() == QDialog.Accepted, "nor asked at Save"
    assert cfg.endpoint_for("Studio::llama3").api_key == "lan-key"
    dialog.deleteLater()


def test_test_attestation_and_detect_ask_before_they_send_the_private_key(app, monkeypatch):
    from sealedlore.gui import attest_check
    from sealedlore.gui import settings_dialog as module

    started: list[str] = []

    class Tests:
        def start(self, settings):
            started.append(settings.base_url)
            return 1

    detected: list[str] = []
    monkeypatch.setattr(
        module, "detect_context", lambda url, key, model: detected.append(key) or None
    )
    dialog = SettingsDialog(nano(), None)
    # After the button has connected to the real one's results.
    monkeypatch.setattr(attest_check, "attestation_tests", lambda: Tests())
    dialog.private_url.setText(LAN)
    dialog.private_key.setText("private-key")
    dialog.private_model.setText("TEE/glm-5.3-flash")
    questions = asked(monkeypatch, QMessageBox.No)
    dialog.private_attest.run()
    assert started == [] and dialog.private_attest.last.outcome == "error"
    assert "would have gone unencrypted" in dialog.private_attest.last.headline
    dialog._detect_private_context()
    assert detected == [] and "would have gone unencrypted" in dialog.detect_note.text()
    assert [q[1].count(f"Private scenes: {LAN}") for q in questions] == [1, 1]

    questions = asked(monkeypatch, QMessageBox.Yes)
    dialog.private_attest.run()
    dialog._detect_private_context()
    assert started == [LAN] and detected == ["private-key"] and len(questions) == 1
    dialog.deleteLater()


def test_detect_sends_the_key_save_would_keep(app, monkeypatch):
    """A blank private key is the default endpoint's on its own host, as
    Config.private has it: not the key of whichever endpoint is shown."""
    from sealedlore.gui import settings_dialog as module

    detected: list[str] = []
    monkeypatch.setattr(
        module, "detect_context", lambda url, key, model: detected.append(key) or None
    )
    dialog = SettingsDialog(nano(), None)
    dialog._add_endpoint()
    dialog.provider_name.setText("Other")
    dialog.base_url.setText(NANO)
    dialog.api_key.setText("other-key")
    dialog.private_url.setText(NANO)
    dialog.private_model.setText("llama3")
    dialog._detect_private_context()
    assert detected == ["nano-key"]
    dialog.deleteLater()


# --- nothing is grandfathered (the author, Nov 14 2026) ------------------------------------


def older(**changes) -> Config:
    """A config saved before plain http had to be confirmed: a key going to
    another machine on the network, recorded for its place, confirmed for
    nothing."""
    studio_endpoint = ProviderConfig(
        name="Studio",
        base_url=LAN,
        api_key="lan-key",
        model="llama3",
        key_saved_for=key_origin(LAN),
    )
    return Config(providers=[studio_endpoint], active_provider_name="Studio", **changes)


def test_a_key_already_going_unencrypted_is_held_back_until_confirmed(tmp_path: Path):
    """Its requests refuse in words that name the machine and where to confirm,
    nothing sent; what takes the chat key on its own host is held back with it;
    the start-up note says so. To this computer a key is never held back."""
    from sealedlore.providers import make_chat_provider
    from sealedlore.providers.base import ProviderError
    from sealedlore.storage.repository import load_config_or_recover, save_config

    cfg = older(
        embedding_provider=EmbeddingProviderConfig(),
        private_provider=ProviderConfig(name="private", base_url=LAN, model="llama3"),
    )
    held = cfg.active_provider().sendable()
    assert held.api_key == ""
    assert "go over plain http, unencrypted, to http://192.168.1.5:11434" in held.key_withheld
    assert "Settings → Endpoints, under Keys held back, Confirm…" in held.key_withheld
    assert "lan-key" not in held.key_withheld
    sent: list[httpx.Request] = []
    provider = make_chat_provider(cfg.active_provider())
    provider._client = httpx.Client(
        transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200))
    )
    with pytest.raises(ProviderError, match="isn't sent until you confirm"):
        list(provider.stream(ChatRequest(model="llama3", messages=[])))
    assert sent == []
    provider.close()
    assert cfg.embeddings().api_key == "" and cfg.images().api_key == ""
    assert cfg.private().sendable().api_key == ""
    assert cfg.active_provider().api_key == "lan-key", "never deleted"

    save_config(cfg, root=tmp_path)
    _loaded, note = load_config_or_recover(root=tmp_path)
    assert note and "isn't sent until you confirm" in note and "192.168.1.5" in note

    cfg.active_provider().key_in_clear_for = key_origin(LAN)
    assert cfg.active_provider().sendable().api_key == "lan-key"
    assert cfg.embeddings().api_key == "lan-key" and cfg.images().api_key == "lan-key"
    assert cfg.private().sendable().api_key == "lan-key"

    here = ProviderConfig(name="Here", base_url="http://127.0.0.1:1234/v1", api_key="k")
    assert here.sendable().api_key == "k", "this computer: never asked about"


def test_settings_lists_it_held_back_with_confirm_and_save_keeps_the_yes(app, monkeypatch):
    cfg = older()
    dialog = SettingsDialog(cfg, None)
    notes = [note.text() for note in dialog.held_back_notes]
    assert len(notes) == 1 and "Studio (Endpoints): its key would go over plain http" in notes[0]
    confirm = next(
        button for button in dialog.findChildren(QPushButton) if button.text() == "Confirm…"
    )
    questions = asked(monkeypatch, QMessageBox.No)
    confirm.click()
    assert len(questions) == 1 and questions[0][2] == QMessageBox.No
    dialog._save()
    assert len(questions) == 1, "Save doesn't ask about it again"
    assert cfg.active_provider().key_in_clear_for == "", "No: still held back"
    assert cfg.active_provider().sendable().api_key == ""

    again = SettingsDialog(cfg, None)
    questions = asked(monkeypatch, QMessageBox.Yes)
    next(b for b in again.findChildren(QPushButton) if b.text() == "Confirm…").click()
    assert "will be sent over plain http" in again.held_back_notes[0].text()
    again._save()
    assert len(questions) == 1
    assert cfg.active_provider().key_in_clear_for == "http://192.168.1.5:11434"
    assert cfg.active_provider().sendable().api_key == "lan-key"
    assert SettingsDialog(cfg, None).held_back_notes == [], "confirmed: nothing held back"
    dialog.deleteLater()
    again.deleteLater()


def test_browse_asks_about_a_key_held_back_and_sends_it_only_on_yes(app, monkeypatch):
    from sealedlore.gui import settings_dialog as module
    from sealedlore.gui.model_picker import ModelCatalog

    opened: list[str] = []
    monkeypatch.setattr(module, "pick_model", lambda *_a, **_k: opened.append("picker"))
    dialog = SettingsDialog(older(), None, catalog=ModelCatalog())
    questions = asked(monkeypatch, QMessageBox.No)
    assert dialog._browse_models("") is None and opened == []
    assert len(questions) == 1 and f"Studio (Endpoints): {LAN}" in questions[0][1]
    questions = asked(monkeypatch, QMessageBox.Yes)
    dialog._browse_models("")
    assert opened == ["picker"]
    provider, _source, _owned = dialog._model_source()
    assert provider.config.providers[0].sendable().api_key == "lan-key", "the Yes goes with it"
    dialog.deleteLater()


def test_configure_says_a_key_already_going_unencrypted_is_held_back(tmp_path: Path, capsys):
    """Without a window: said with the way out, giving the key again, which
    confirms it."""
    from sealedlore.storage.repository import save_config

    save_config(older(), root=tmp_path)
    data = ["--data-dir", str(tmp_path)]
    cli_main([*data, "configure", "--endpoint-name", "Studio", "--base-url", LAN, "--model", "x"])
    err = capsys.readouterr().err
    assert "isn't sent until you confirm" in err and "--api-key" in err
    assert load_config(root=tmp_path).active_provider().sendable().api_key == ""
    cli_main(
        [*data, "configure", "--endpoint-name", "Studio", "--base-url", LAN, "--api-key", "lan-key"]
    )
    assert "goes unencrypted" not in capsys.readouterr().err, "said when it was first given"
    assert load_config(root=tmp_path).active_provider().sendable().api_key == "lan-key"
