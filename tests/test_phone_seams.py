"""What phone access needs from the window and the config, before any of it
exists (step 2 of the plan): a turn from elsewhere goes through the same
sending and queueing as the composer's and leaves the composer alone; a
retake can be given its shape; the stories are listed from one scan; the
phone settings and the onion key are kept."""

from __future__ import annotations

import dataclasses
import json
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.engine.prompt import TurnRequest  # noqa: E402
from sealedlore.engine.session import TurnShape  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.models.config import Config, PhoneAccess, PhoneDevice  # noqa: E402
from sealedlore.models.node import DIRECTOR_SPEAKER_ID  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage import keychain  # noqa: E402
from sealedlore.storage.library import story_listings  # noqa: E402
from sealedlore.storage.repository import (  # noqa: E402
    VISUAL_ELSEWHERE,
    StoryBundle,
    load_config,
    save_config,
    save_story_bundle,
)


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def wait_for(app: QApplication, condition, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        app.processEvents()
        time.sleep(0.005)


def opened(tmp_path: Path, story, cast, replies: list[str], *, delay: float = 0.0) -> MainWindow:
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.scene_reads = "every_turn"
    window.open_story(story.id)
    window.session.story.held_character_id = "char-serrik"
    provider = MockChatProvider(replies)
    provider.delay = delay
    window._provider = provider
    window._refresh_composer()
    return window


def sends_recorded(window: MainWindow) -> list[TurnRequest]:
    sent: list[TurnRequest] = []
    real = window.session.send

    def send(turn):
        sent.append(turn)
        return real(turn)

    window.session.send = send
    return sent


def test_the_composer_s_turn_is_built_as_before(app, tmp_path: Path, story, cast):
    window = opened(tmp_path, story, cast, ["The door gives.", "{}"])
    sent = sends_recorded(window)
    composer = window.composer
    composer.input.setPlainText("I push the door.")
    expected = TurnRequest(
        speaker_id=composer.speaker_id(),
        user_text="I push the door.",
        controlled_character_id=composer.held_character_id(),
        ooc=composer.ooc_text(),
        npc_scope=composer.npc_scope(),
        npc_scope_ids=composer.npc_scope_ids(),
        agency_mode=composer.agency_mode(),
        response_style=composer.response_style(),
        dice_domain=composer.dice_domain(),
        difficulty=composer.difficulty_level(),
    )
    window.send_turn()
    wait_for(app, lambda: not window._busy)
    assert sent == [expected]
    assert composer.text() == ""
    window.close()


def test_a_phone_turn_goes_as_sent_and_leaves_the_composer_alone(app, tmp_path: Path, story, cast):
    window = opened(tmp_path, story, cast, ["The hall answers.", "{}"])
    sent = sends_recorded(window)
    window.composer.input.setPlainText("half a sentence the author is still writ")
    turn = dataclasses.replace(
        window._current_turn("Night falls over the hall."),
        speaker_id=DIRECTOR_SPEAKER_ID,
        response_style="brief",
        agency_mode="fiat",
        ooc=None,
    )
    window._submit_turn("Night falls over the hall.", turn, origin="phone")
    wait_for(app, lambda: not window._busy)
    assert sent == [turn]
    assert sent[0].controlled_character_id == "char-serrik"  # the desktop's, as it stands
    assert window.composer.text() == "half a sentence the author is still writ"
    assert [n.content for n in window.session.path() if n.kind == "user"] == [
        "Night falls over the hall."
    ]
    window.close()


def test_a_phone_turn_queued_then_stopped_goes_back_to_the_phone(app, tmp_path: Path, story, cast):
    window = opened(tmp_path, story, cast, ["The door gives.", "{}"], delay=0.05)
    returned: list[tuple[str, str]] = []
    window.queued_turn_returned.connect(lambda origin, text: returned.append((origin, text)))
    window.composer.input.setPlainText("I push the door.")
    window.send_turn()
    wait_for(app, lambda: window._background)

    window.composer.input.setPlainText("the author's own draft")
    window._submit_turn("I step inside.", window._current_turn("I step inside."), origin="phone")
    assert window._queued is not None and window._queued_origin == "phone"
    window.stop_generation()
    wait_for(app, lambda: not window._busy)

    assert returned == [("phone", "I step inside.")]
    assert window.composer.text() == "the author's own draft"
    assert window._queued is None and window._queued_origin == "desktop"
    window.close()


def test_a_retake_can_be_given_its_shape(app, tmp_path: Path, story, cast):
    window = opened(tmp_path, story, cast, ["The door gives.", "{}", "The door gives way.", "{}"])
    window.composer.input.setPlainText("I push the door.")
    window.send_turn()
    wait_for(app, lambda: not window._busy)
    shapes: list[TurnShape | None] = []
    real = window.session.regenerate

    def regenerate(*args, shape=None, **kwargs):
        shapes.append(shape)
        return real(*args, shape=shape, **kwargs)

    window.session.regenerate = regenerate
    phone_shape = dataclasses.replace(window._current_shape(), response_style="literary")
    window.regenerate(phone_shape)
    wait_for(app, lambda: not window._busy)
    assert shapes == [phone_shape]
    window.close()


# --- the stories, listed once for the list and the phone -----------------------


def test_stories_are_listed_with_whether_they_are_chats_or_cant_be_opened(
    tmp_path: Path, story, cast
):
    plain = story.model_copy(update={"id": "plain", "title": "Plain", "updated_at": "2026-10-01"})
    chat = story.model_copy(
        update={"id": "chat", "title": "A chat", "mode": "chat", "updated_at": "2026-10-03"}
    )
    for each in (plain, chat):
        save_story_bundle(StoryBundle(story=each, cast=cast), root=tmp_path)
    # A Visual Story played here before the visual mode became its own app.
    game = tmp_path / "stories" / "game"
    game.mkdir()
    (game / "story.json").write_text(
        '{"id": "game", "title": "Mapped", "mode": "visual", "updated_at": "2026-10-02"}'
    )
    broken = tmp_path / "stories" / "broken"
    broken.mkdir()
    (broken / "story.json").write_text("{not json")

    listings = story_listings(tmp_path)
    assert [(s.id, s.chat, s.error is None) for s in listings] == [
        ("chat", True, True),
        ("game", False, False),
        ("plain", False, True),
        ("broken", False, False),
    ]
    assert listings[1].title == "Mapped" and listings[1].error == VISUAL_ELSEWHERE
    assert listings[0].title == "A chat" and listings[0].messages == 0


# --- the phone's settings -------------------------------------------------------


def test_phone_access_is_off_and_shares_nothing_by_default():
    phone = Config().phone
    assert phone == PhoneAccess()
    assert not phone.start_on_launch and phone.shared_stories == [] and phone.devices == []


def test_the_onion_key_is_kept_in_the_keychain_and_the_rest_in_config_json(
    tmp_path: Path, memory_keychain
):
    config = Config()
    config.phone.onion.api_key = "ED25519-V3:secret-onion-key"
    config.phone.service_id = "a" * 56
    config.phone.shared_stories = ["story-1"]
    config.phone.devices = [PhoneDevice(id="d1", name="Pixel", token_sha256="ab" * 32)]
    save_config(config, root=tmp_path)

    text = (tmp_path / "config.json").read_text()
    assert "secret-onion-key" not in text
    on_disk = json.loads(text)
    assert "phone" in on_disk["keychain_slots"]
    assert on_disk["phone"]["shared_stories"] == ["story-1"]
    assert on_disk["phone"]["devices"][0]["token_sha256"] == "ab" * 32

    keychain.forget()
    loaded = load_config(root=tmp_path)
    assert loaded.phone.onion.api_key == "ED25519-V3:secret-onion-key"
    assert loaded.phone == config.phone


def test_an_older_config_has_phone_access_off(tmp_path: Path):
    (tmp_path / "config.json").write_text(json.dumps({"providers": []}))
    assert load_config(root=tmp_path).phone == PhoneAccess()
