"""A key never silently follows an edited address (the DA apps' design, Nov 2026).

Each saved key records the place (scheme, host, port) it was saved for, and
a request carries it only there: config.json edited by hand, `sealedlore
configure` given a new address alone, or No in Settings to "Use the saved key
with …?", and the key is kept, never deleted, but sent nowhere else, the
refusal saying why and how to put it right. A key saved before places were
recorded takes its address's on first load, with no question."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.config import (
    Config,
    EmbeddingProviderConfig,
    MediaEndpoint,
    ProviderConfig,
    key_origin,
    rebind_keys,
)
from sealedlore.models.generation import GenerationParams
from sealedlore.providers import make_chat_provider
from sealedlore.providers.base import ChatRequest, ProviderError
from sealedlore.providers.images import ImageClient
from sealedlore.storage import keychain
from sealedlore.storage.repository import load_config_or_recover, save_config

NANO = "https://nano-gpt.com/api/v1"
ELSEWHERE = "https://api.elsewhere.example/v1"
KEY = "sk-nano-secret-1234"


def nano(**fields) -> Config:
    return Config(
        providers=[ProviderConfig(name="NanoGPT", base_url=NANO, api_key=KEY, model="m")],
        active_provider_name="NanoGPT",
        **fields,
    )


def edited_by_hand(root: Path, change) -> None:
    path = root / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def request() -> ChatRequest:
    return ChatRequest(
        model="m",
        messages=[PromptMessage(role="user", parts=(ContentPart(text="hello"),))],
        params=GenerationParams(),
    )


def test_where_a_key_is_for_is_its_scheme_host_and_port():
    assert key_origin("https://alice:pw@Nano-GPT.com:443/api/v1") == "https://nano-gpt.com"
    assert key_origin("http://192.168.1.5:80/v1") == "http://192.168.1.5"
    assert key_origin("http://192.168.1.5:11434/v1") == "http://192.168.1.5:11434"
    assert key_origin("http://[fe80::1]:11434/v1") == "http://[fe80::1]:11434"
    assert key_origin("") == "" and key_origin("not an address") == ""


def test_a_key_saved_before_places_were_recorded_takes_its_address_with_no_question(
    tmp_path: Path, memory_keychain
):
    save_config(nano(), root=tmp_path)
    edited_by_hand(tmp_path, lambda data: data["providers"][0].pop("key_saved_for", None))
    keychain.forget()
    config, note = load_config_or_recover(root=tmp_path)
    assert config.providers[0].key_saved_for == "https://nano-gpt.com"
    assert note is None
    assert config.providers[0].sendable().api_key == KEY


def test_a_key_never_follows_an_address_edited_by_hand(tmp_path: Path, memory_keychain):
    save_config(nano(), root=tmp_path)
    edited_by_hand(tmp_path, lambda data: data["providers"][0].update(base_url=ELSEWHERE))
    keychain.forget()
    config, note = load_config_or_recover(root=tmp_path)
    assert config.providers[0].api_key == KEY, "kept, never deleted"
    assert "saved for https://nano-gpt.com" in note and "api.elsewhere.example" in note
    assert KEY not in note

    sent: list[httpx.Request] = []
    provider = make_chat_provider(config.providers[0])
    provider._client = httpx.Client(
        transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200))
    )
    with pytest.raises(ProviderError, match="isn't sent there") as refused:
        list(provider.stream(request()))
    assert sent == [] and KEY not in str(refused.value)
    assert "Settings" in str(refused.value) and "--api-key" in str(refused.value)

    # Saved again as it stands: the key stays, still for nano-gpt.com alone.
    save_config(config, root=tmp_path)
    keychain.forget()
    again, _note = load_config_or_recover(root=tmp_path)
    assert again.providers[0].api_key == KEY
    assert again.providers[0].key_saved_for == "https://nano-gpt.com"
    # Put back by hand: sent again.
    edited_by_hand(tmp_path, lambda data: data["providers"][0].update(base_url=NANO + "/"))
    keychain.forget()
    back, note = load_config_or_recover(root=tmp_path)
    assert note is None and back.providers[0].sendable().api_key == KEY


def test_pictures_videos_embeddings_and_private_scenes_keep_their_keys_where_saved():
    config = nano(
        image_endpoint=MediaEndpoint(
            base_url=ELSEWHERE, api_key="img", key_saved_for="https://openrouter.ai"
        ),
        video_endpoint=MediaEndpoint(
            base_url=ELSEWHERE, api_key="vid", key_saved_for="https://api.wavespeed.ai"
        ),
        embedding_provider=EmbeddingProviderConfig(
            base_url=ELSEWHERE, api_key="emb", key_saved_for="https://nano-gpt.com"
        ),
        private_provider=ProviderConfig(
            name="private", base_url=NANO, model="TEE/x", key_saved_for=""
        ),
    )
    images, videos, embeddings = config.images(), config.videos(), config.embeddings()
    for resolved in (images, videos, embeddings):
        assert resolved.api_key == "" and "isn't sent there" in resolved.key_withheld
    # The chat's key, taken by a blank one on its own host, keeps its place.
    config.providers[0].key_saved_for = "https://nano-gpt.com"
    assert config.private().sendable().api_key == KEY
    config.providers[0].key_saved_for = "https://old.example"
    assert config.private().sendable().api_key == ""

    sent: list[httpx.Request] = []
    client = ImageClient(
        images.base_url,
        images.api_key,
        key_withheld=images.key_withheld,
        client=httpx.Client(transport=httpx.MockTransport(lambda r: sent.append(r))),
    )
    with pytest.raises(ProviderError, match="pictures was saved for https://openrouter.ai"):
        client.generate({"model": "m", "prompt": "the whole scene"})
    assert sent == []


def test_a_key_typed_anew_is_for_its_address_and_a_kept_one_stays_for_its_own():
    before = nano()
    before.providers[0].key_saved_for = "https://nano-gpt.com"
    after = before.model_copy(deep=True)
    after.providers[0].base_url = ELSEWHERE
    [move] = rebind_keys(before, after)
    assert (move.saved_for, move.now) == ("https://nano-gpt.com", "https://api.elsewhere.example")
    assert after.providers[0].sendable().api_key == "", "not sent until allowed"
    move.allow()
    assert after.providers[0].sendable().api_key == KEY

    typed = before.model_copy(deep=True)
    typed.providers[0].base_url = ELSEWHERE
    typed.providers[0].api_key = "sk-elsewhere"
    assert rebind_keys(before, typed) == []
    assert typed.providers[0].key_saved_for == "https://api.elsewhere.example"

    path_only = before.model_copy(deep=True)
    path_only.providers[0].base_url = "https://nano-gpt.com/api/v2"
    assert rebind_keys(before, path_only) == [], "the same place"

    renamed = before.model_copy(deep=True)
    renamed.providers[0].name = "Nano"
    renamed.providers[0].base_url = ELSEWHERE
    assert len(rebind_keys(before, renamed, {"Nano": "NanoGPT"})) == 1

    # A key the keychain couldn't give back this session shows blank: where
    # it is for is kept, for when it can be read again.
    locked = before.model_copy(deep=True)
    locked.video_endpoint = MediaEndpoint(
        base_url="https://api.wavespeed.ai/api/v3", key_saved_for="https://api.wavespeed.ai"
    )
    typed = locked.model_copy(deep=True)
    typed.video_endpoint = MediaEndpoint(base_url="https://api.wavespeed.ai/api/v3")
    assert rebind_keys(locked, typed) == []
    assert typed.video_endpoint.key_saved_for == "https://api.wavespeed.ai"


def test_a_no_is_remembered_for_its_place_and_written_with_the_settings(
    tmp_path: Path, memory_keychain
):
    """`KeyMove.decline` keeps the No in config.json (no secret in it), so a
    later Save knows it was said; a new key, or the address back where the
    key was saved for, lets it go. Never a key deleted."""
    before = nano()
    before.providers[0].key_saved_for = "https://nano-gpt.com"
    after = before.model_copy(deep=True)
    after.providers[0].base_url = ELSEWHERE
    [move] = rebind_keys(before, after)
    assert not move.declined
    move.decline()
    save_config(after, root=tmp_path)
    loaded, _note = load_config_or_recover(root=tmp_path)
    provider = loaded.providers[0]
    assert provider.key_declined_for == "https://api.elsewhere.example"
    assert provider.api_key == KEY and provider.sendable().api_key == ""

    again = loaded.model_copy(deep=True)
    [move] = rebind_keys(loaded, again)
    assert move.declined and again.providers[0].key_declined_for == move.now
    move.allow()
    assert again.providers[0].key_declined_for == "" and again.providers[0].sendable().api_key

    typed = loaded.model_copy(deep=True)
    typed.providers[0].api_key = "sk-elsewhere"
    assert rebind_keys(loaded, typed) == [] and typed.providers[0].key_declined_for == ""
    home = loaded.model_copy(deep=True)
    home.providers[0].base_url = NANO
    assert rebind_keys(loaded, home) == [] and home.providers[0].key_declined_for == ""


# --- Settings ----------------------------------------------------------------------------

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from sealedlore.gui.settings_dialog import SettingsDialog  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def answering(monkeypatch, answer) -> list[tuple[str, str, object]]:
    questions: list[tuple[str, str, object]] = []

    def question(_parent, title, text, _buttons, default):
        questions.append((title, text, default))
        return answer

    monkeypatch.setattr(QMessageBox, "question", question)
    return questions


def saved_for_nano() -> Config:
    config = nano()
    config.providers[0].key_saved_for = "https://nano-gpt.com"
    return config


def test_settings_asks_before_a_saved_key_goes_to_an_edited_address(app, monkeypatch):
    config = saved_for_nano()
    questions = answering(monkeypatch, QMessageBox.No)
    dialog = SettingsDialog(config, None)
    dialog.base_url.setText(ELSEWHERE)
    dialog._save()
    [(title, text, default)] = questions
    assert title == "Use the saved key with api.elsewhere.example?"
    assert default == QMessageBox.No and KEY not in text
    assert "saved for https://nano-gpt.com" in text
    provider = config.active_provider()
    assert provider.base_url == ELSEWHERE, "No still saves"
    assert provider.api_key == KEY, "kept, never deleted"
    assert (
        provider.sendable().api_key == "" and "isn't sent there" in provider.sendable().key_withheld
    )
    dialog.deleteLater()

    # The No is remembered for that place: a later Save doesn't ask again (a
    # question asked at every Save is soon answered Yes unread; the DA apps'
    # standard), and the Endpoints page says the key is held back.
    assert provider.key_declined_for == "https://api.elsewhere.example"
    again = SettingsDialog(config, None)
    questions = answering(monkeypatch, QMessageBox.Yes)
    again._save()
    assert questions == [], "not asked again at the next Save"
    assert config.active_provider().sendable().api_key == ""
    assert config.active_provider().key_saved_for == "https://nano-gpt.com"
    again.deleteLater()

    shown = SettingsDialog(config, None)
    [note] = shown.held_back_notes
    assert "is for https://nano-gpt.com, not sent to https://api.elsewhere.example" in note.text()
    assert "You said No" in note.text() and KEY not in note.text()
    shown.deleteLater()


def test_ask_again_puts_the_question_once_more_and_save_keeps_the_answer(app, monkeypatch):
    config = saved_for_nano()
    answering(monkeypatch, QMessageBox.No)
    first = SettingsDialog(config, None)
    first.base_url.setText(ELSEWHERE)
    first._save()
    first.deleteLater()

    still = SettingsDialog(config, None)
    questions = answering(monkeypatch, QMessageBox.No)
    [move] = still._held_back()
    still._ask_again(move, still.held_back_notes[0])
    assert len(questions) == 1 and questions[0][2] == QMessageBox.No
    still._save()
    assert config.active_provider().sendable().api_key == "", "No again: still held back"
    still.deleteLater()

    yes = SettingsDialog(config, None)
    questions = answering(monkeypatch, QMessageBox.Yes)
    [move] = yes._held_back()
    yes._ask_again(move, yes.held_back_notes[0])
    assert "will be sent to https://api.elsewhere.example once you Save" in (
        yes.held_back_notes[0].text()
    )
    assert config.active_provider().sendable().api_key == "", "nothing moves before Save"
    yes._save()
    assert len(questions) == 1, "Save keeps the answer it was given"
    provider = config.active_provider()
    assert provider.sendable().api_key == KEY
    assert (provider.key_saved_for, provider.key_declined_for) == (
        "https://api.elsewhere.example",
        "",
    )
    assert SettingsDialog(config, None).held_back_notes == []


def test_a_no_is_for_the_place_it_was_said_of(app, monkeypatch):
    """Another place again is a new question; back where the key was saved
    for, there is nothing to ask and the No is let go."""
    config = saved_for_nano()
    answering(monkeypatch, QMessageBox.No)
    dialog = SettingsDialog(config, None)
    dialog.base_url.setText(ELSEWHERE)
    dialog._save()
    dialog.deleteLater()

    third = SettingsDialog(config, None)
    questions = answering(monkeypatch, QMessageBox.No)
    third.base_url.setText("https://third.example/v1")
    third._save()
    assert len(questions) == 1 and "third.example" in questions[0][0]
    assert config.active_provider().key_declined_for == "https://third.example"
    third.deleteLater()

    home = SettingsDialog(config, None)
    questions = answering(monkeypatch, QMessageBox.No)
    home.base_url.setText(NANO)
    home._save()
    assert questions == []
    provider = config.active_provider()
    assert provider.sendable().api_key == KEY and provider.key_declined_for == ""
    home.deleteLater()


def test_a_key_sent_before_save_is_asked_about_even_after_a_no(app, monkeypatch):
    """Browse…, Test speed…, Test attestation and Detect send the key now:
    nothing else would say why nothing was sent, so a place said No to at an
    earlier Save is asked about there (once a dialog), No sending nothing."""
    config = saved_for_nano()
    answering(monkeypatch, QMessageBox.No)
    dialog = SettingsDialog(config, None)
    dialog.base_url.setText(ELSEWHERE)
    dialog._save()
    dialog.deleteLater()

    later = SettingsDialog(config, None)
    questions = answering(monkeypatch, QMessageBox.No)
    assert later._may_send([]) is False
    assert later._may_send([]) is False
    assert len(questions) == 1, "once a dialog"
    later.deleteLater()


def test_settings_asks_nothing_for_a_key_typed_for_the_new_address(app, monkeypatch):
    config = saved_for_nano()
    questions = answering(monkeypatch, QMessageBox.No)
    dialog = SettingsDialog(config, None)
    dialog.base_url.setText(ELSEWHERE)
    dialog.api_key.setText("sk-elsewhere")
    dialog._save()
    assert questions == []
    assert config.active_provider().key_saved_for == "https://api.elsewhere.example"
    dialog.deleteLater()


def test_a_send_before_save_asks_too_and_no_sends_nothing(app, monkeypatch):
    """Browse…, Test speed…, Test attestation and Detect send the keys as
    typed, before Save: a saved key moving to an edited address is asked
    about there, and a Yes stands for Save."""
    config = saved_for_nano()
    questions = answering(monkeypatch, QMessageBox.No)
    dialog = SettingsDialog(config, None)
    dialog.base_url.setText(ELSEWHERE)
    assert dialog._may_send([]) is False
    assert len(questions) == 1
    assert dialog._may_send([]) is False and len(questions) == 1, "asked once a dialog"

    allowed = SettingsDialog(config, None)
    questions = answering(monkeypatch, QMessageBox.Yes)
    allowed.base_url.setText(ELSEWHERE)
    assert allowed._may_send([]) is True
    assert allowed._endpoints_as_typed()[0].sendable().api_key == KEY, "Browse… may send it"
    allowed._save()
    assert len(questions) == 1, "the Yes stands for Save"
    assert config.active_provider().sendable().api_key == KEY
    dialog.deleteLater()
    allowed.deleteLater()
