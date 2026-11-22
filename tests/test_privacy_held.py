"""A chat or a private scene stays as private as it was made (Story.chat_privacy,
PrivateSpan.privacy): a Tinfoil or Chutes model's id carries no prefix, so
without the record an endpoint edited or removed under it sent the chat, its
summaries and the scene in the clear. Offline, offscreen."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from sealedlore.engine.session import StorySession, TurnRequest  # noqa: E402
from sealedlore.gui.settings_dialog import SettingsDialog  # noqa: E402
from sealedlore.models.config import (  # noqa: E402
    CHUTES_BASE_URL,
    TINFOIL_BASE_URL,
    Config,
    ProviderConfig,
)
from sealedlore.models.node import CHAT_USER_ID  # noqa: E402
from sealedlore.models.story import Story  # noqa: E402
from sealedlore.providers.base import ChatRequest, ProviderError  # noqa: E402
from sealedlore.providers.endpoints import EndpointsProvider  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.providers.privacy import (  # noqa: E402
    CHUTES,
    NANOGPT_PRIVATE,
    NONE,
    TINFOIL,
    NotBuiltProvider,
    Privacy,
    held_judge,
    holds,
)
from sealedlore.storage.repository import (  # noqa: E402
    StoryBundle,
    load_story_bundle,
    save_story_bundle,
)
from tests.conftest import make_exchange  # noqa: E402

NANO = "https://nano-gpt.com/api/v1"
ELSEWHERE = "https://api.example.com/v1"
SECRET = "the-word-only-the-enclave-reads"


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def tinfoil(**changes) -> ProviderConfig:
    # Made by typing Tinfoil's address: Speaks stays "auto", the host decides.
    fields = {"name": "Tinfoil", "base_url": TINFOIL_BASE_URL, "api_key": "tk", **changes}
    return ProviderConfig(**fields)


def chutes() -> ProviderConfig:
    return ProviderConfig(name="Chutes", base_url=CHUTES_BASE_URL, api_key="cpk")


def config(*extra: ProviderConfig) -> Config:
    nano = ProviderConfig(name="Nano", base_url=NANO, api_key="k")
    return Config(providers=[nano, *extra], active_provider_name="Nano")


class Recorder(MockChatProvider):
    """A client the routing provider hands out, which records what reaches it."""

    made: list[ProviderConfig] = []

    def __init__(self, settings: ProviderConfig) -> None:
        super().__init__(["A reply."])
        Recorder.made.append(settings)


def chat(tmp_path: Path, cfg: Config, *, keep: str = "memory", provider=None) -> StorySession:
    story = Story(title="Sealed", mode="chat", chat_keep=keep)
    story.defaults.main_model = "Tinfoil::glm-5-3"
    nodes = make_exchange(1)
    story.active_leaf_id = nodes[-1].id
    return StorySession(
        StoryBundle(story=story, nodes=nodes),
        cfg,
        provider or MockChatProvider(["A reply."]),
        root=tmp_path,
        learn_corrections=False,
    )


def say(session: StorySession, text: str = SECRET) -> None:
    list(session.send(TurnRequest(speaker_id=CHAT_USER_ID, user_text=text)))


# --- the pure parts -------------------------------------------------------------------


def test_what_holds_a_recorded_privacy():
    sealed = Privacy("sealed", TINFOIL)
    assert holds(sealed, sealed)
    assert not holds(sealed, NONE)
    assert not holds(sealed, Privacy("tee", "nanogpt-tee"))
    assert not holds(sealed, Privacy("sealed", CHUTES)), "another service's client"
    assert holds(Privacy("tee", "nanogpt-tee"), sealed), "more private holds"
    assert holds(NONE, NONE)
    judge = held_judge(lambda m: NONE, "Tinfoil::glm-5-3", sealed)
    assert judge("Tinfoil::glm-5-3") == sealed and judge("other") == NONE


# --- a chat -------------------------------------------------------------------------------


def test_a_memory_chat_keeps_its_lock_when_its_endpoint_is_edited(tmp_path: Path):
    Recorder.made = []
    cfg = config(tinfoil())
    cfg.summarization_model = "Nano::plain-summariser"
    cfg.image_prompt_model = "Nano::plain-writer"
    session = chat(tmp_path, cfg, provider=EndpointsProvider(cfg, make=Recorder))
    record = session.story.chat_privacy
    assert (record.kind, record.service, record.origin) == (
        "sealed",
        TINFOIL,
        "https://inference.tinfoil.sh",
    )
    assert session.privacy_held() is None

    # The author edits that endpoint's address (another provider, a typo).
    cfg.providers[1] = cfg.providers[1].model_copy(update={"base_url": ELSEWHERE})
    assert session.privacy(session.model) == NONE, "where it goes now"
    assert session.tee_chat, "still the chat it was made"
    reason = session.privacy_held()
    assert "end-to-end encrypted through Tinfoil (inference.tinfoil.sh)" in reason
    assert "api.example.com" in reason and "Settings → Endpoints" in reason
    assert "File → Close story" in reason
    with pytest.raises(ValueError, match="isn't end-to-end encrypted"):
        session.check_chat_model("Nano::gpt-plain")
    # Its summaries and picture prompts stay its own model's, never Settings'.
    assert session.summarization_model == session.model
    assert session.role_model("image_prompt") == session.model
    assert session.tee_block == reason

    with pytest.raises(ProviderError, match="nothing was sent"):
        say(session)
    # The routing client refuses on its own too, whatever reaches it.
    request = ChatRequest(model=session.model, messages=[])
    with pytest.raises(ProviderError, match="began end-to-end encrypted through Tinfoil"):
        session.provider.complete(request)
    with pytest.raises(ProviderError, match="began end-to-end encrypted"):
        session.provider.detached().complete(request)
    assert Recorder.made == [], "no client was made for it"
    assert not any(tmp_path.iterdir()), "a memory-only chat writes nothing"


def test_a_chat_whose_endpoint_was_removed_moves_nowhere_plain(tmp_path: Path):
    cfg = config(tinfoil(api="tinfoil"))
    cfg.summarization_model = "Nano::plain-summariser"
    session = chat(tmp_path, cfg)
    del cfg.providers[1]
    assert "names an endpoint that isn't set up any more" in session.privacy_held()
    with pytest.raises(ValueError):
        session.check_chat_model("Nano::gpt-plain")
    assert session.summarization_model == session.model
    with pytest.raises(ProviderError):
        say(session)


def test_a_disk_chat_reopened_after_its_endpoint_changed_sends_nothing(tmp_path: Path):
    session = chat(tmp_path, config(tinfoil()), keep="disk")
    session.save()
    saved = load_story_bundle(session.story.id, root=tmp_path)
    assert saved.story.chat_privacy.service == TINFOIL, "kept with the chat"

    later = config(tinfoil(base_url=ELSEWHERE))
    plain = MockChatProvider(["A reply."])
    again = StorySession(saved, later, plain, root=tmp_path, learn_corrections=False)
    assert again.story.chat_privacy.service == TINFOIL, "not taken from what it is now"
    assert "api.example.com" in again.privacy_held()
    with pytest.raises(ProviderError, match="nothing was sent"):
        say(again)
    assert plain.requests == []


def test_a_chat_moved_on_purpose_to_another_services_sealed_model_is_recorded_there(
    tmp_path: Path,
):
    cfg = config(tinfoil(), chutes())
    session = chat(tmp_path, cfg)
    # D8 as amended (Nov 15 2026): its own level or higher, on any service. A
    # Tinfoil chat has the highest privacy; Chutes' -TEE models are partial.
    with pytest.raises(ValueError, match="partial privacy.*kept to the highest privacy"):
        session.check_chat_model("Chutes::zai-org/GLM-5.3-TEE")
    session.check_chat_model("private/glm-5-3")
    session.story.defaults.main_model = "private/glm-5-3"
    session.adopt_chat_privacy()
    assert session.story.chat_privacy.service == NANOGPT_PRIVATE
    assert session.privacy_held() is None
    with pytest.raises(ValueError):
        session.check_chat_model("Nano::gpt-plain")


def test_an_older_chat_is_recorded_as_it_is_when_opened(tmp_path: Path):
    cfg = config(ProviderConfig(name="Nano2", base_url=NANO, api_key="k"))
    story = Story(title="Old", mode="chat")
    story.defaults.main_model = "private/glm-5-3"
    assert story.chat_privacy is None
    session = StorySession(StoryBundle(story=story), cfg, MockChatProvider(), root=tmp_path)
    assert (session.story.chat_privacy.kind, session.story.chat_privacy.service) == (
        "sealed",
        NANOGPT_PRIVATE,
    )


def test_settings_asks_before_an_edit_unseals_the_open_chat(app, tmp_path: Path, monkeypatch):
    cfg = config(tinfoil())
    session = chat(tmp_path, cfg)
    asked: list[str] = []
    answers = iter([QMessageBox.No, QMessageBox.No, QMessageBox.Yes, QMessageBox.Yes])

    def question(_parent, title, text, *_args):
        asked.append(title)
        return next(answers)

    monkeypatch.setattr(QMessageBox, "question", question)
    dialog = SettingsDialog(cfg, session.story)
    dialog.endpoint_list.setCurrentRow(1)
    dialog.base_url.setText(ELSEWHERE)
    dialog._save()
    assert "The chat would send nothing" in asked
    assert cfg.endpoint_named("Tinfoil").base_url == TINFOIL_BASE_URL, "No: not saved"
    assert "not saved" in dialog.error.text()
    dialog._save()
    assert cfg.endpoint_named("Tinfoil").base_url == ELSEWHERE, "Yes: saved"
    assert session.story.defaults.main_model == "Tinfoil::glm-5-3", "the chat didn't move"
    assert "api.example.com" in session.privacy_held()
    dialog.deleteLater()


# --- a private scene -------------------------------------------------------------------------


def scene(tmp_path: Path, private: ProviderConfig) -> StorySession:
    from tests.test_private import build

    session, _main, _held = build(tmp_path)
    session.config.private_provider = private
    session.enter_private(
        keep="disk",
        provider=MockChatProvider(["A private reply."]),
        model=private.model,
        base_url=private.base_url,
    )
    return session


def resumed(tmp_path: Path, session: StorySession, now: ProviderConfig):
    saved = load_story_bundle(session.story.id, root=tmp_path)
    later = session.config.model_copy(deep=True)
    later.private_provider = now
    again = StorySession(saved, later, MockChatProvider(), root=tmp_path, learn_corrections=False)
    client = MockChatProvider(["A reply."])
    again.resume_private(client)
    return again, client


def test_a_scene_begun_on_tinfoil_resumed_on_a_plain_endpoint_sends_nothing(tmp_path: Path):
    from tests.test_private import play, turn

    began = ProviderConfig(
        name="private", base_url=TINFOIL_BASE_URL, api="tinfoil", api_key="tk", model="glm-5-3"
    )
    session = scene(tmp_path, began)
    record = session.open_span.privacy
    assert (record.kind, record.service, record.origin) == (
        "sealed",
        TINFOIL,
        "https://inference.tinfoil.sh",
    )
    session.save()
    plain = ProviderConfig(name="private", base_url=ELSEWHERE, api_key="k", model="glm-5-3")
    again, client = resumed(tmp_path, session, plain)
    assert isinstance(again.private_provider, NotBuiltProvider), "a client that sends nothing"
    reason = again.privacy_held()
    assert "This private scene began end-to-end encrypted through Tinfoil" in reason
    assert "api.example.com" in reason and "end the scene" in reason
    with pytest.raises(ProviderError):
        play(again, turn(again.story.held_character_id, SECRET))
    assert client.requests == []
    # Not to Chutes' -TEE models: partial privacy, where the scene began with
    # the highest (the author's levels, Nov 15 2026).
    chutes_tee = ProviderConfig(
        name="private", base_url=CHUTES_BASE_URL, model="zai-org/GLM-5.3-TEE"
    )
    again.private_provider = NotBuiltProvider(chutes_tee, "held")
    with pytest.raises(ValueError, match="isn't as private"):
        again.switch_private_model(provider=MockChatProvider(), model="zai-org/GLM-5.3-TEE")
    # Moving it on purpose to another service's model at its level (D8) is allowed.
    sealed = ProviderConfig(name="private", base_url=NANO, model="private/kimi-k3")
    again.private_provider = NotBuiltProvider(sealed, "held")
    moved = MockChatProvider()
    moved.config = sealed
    again.switch_private_model(provider=moved, model="private/kimi-k3")
    assert again.open_span.privacy.service == NANOGPT_PRIVATE and again.privacy_held() is None


def test_a_scene_begun_on_an_ordinary_private_model_resumes_as_before(tmp_path: Path):
    """On the server it began on, whatever model Settings → Private names
    now. On another server it is held until the author says to carry it on
    there (the review of Nov 22 2026: it used to resume there, its whole
    history sent with the next message; tests/test_review_3_5_privacy.py)."""
    began = ProviderConfig(name="private", base_url=NANO, api_key="k", model="private/glm-5-3")
    session = scene(tmp_path, began)
    session.save()
    same = ProviderConfig(name="private", base_url=NANO, api_key="k", model="x")
    again, client = resumed(tmp_path, session, same)
    assert again.private_provider is client and again.privacy_held() is None
    local = ProviderConfig(name="private", base_url="http://localhost:11434/v1", model="local/m")
    session2 = scene(tmp_path / "two", local)
    session2.save()
    same2 = ProviderConfig(name="private", base_url="http://localhost:11434/v1", model="x")
    again2, client2 = resumed(tmp_path / "two", session2, same2)
    assert again2.private_provider is client2 and again2.privacy_held() is None
    plain = ProviderConfig(name="private", base_url=ELSEWHERE, api_key="k", model="x")
    again3, client3 = resumed(tmp_path / "two", session2, plain)
    assert isinstance(again3.private_provider, NotBuiltProvider) and client3.requests == []
    assert again3.scene_moved() == ("http://localhost:11434", "https://api.example.com")


# --- the window -------------------------------------------------------------------------------


def test_the_window_says_a_held_chat_and_a_held_scene_and_offers_the_ways_out(
    app, tmp_path: Path, monkeypatch, story, cast
):
    from sealedlore.gui.main_window import MainWindow

    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    bundle.story.held_character_id = "char-serrik"
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.scene_reads = "manual"
    window.config.private_intro_seen = True
    window.config.private_provider = ProviderConfig(
        name="private", base_url=TINFOIL_BASE_URL, api="tinfoil", model="glm-5-3"
    )
    window.private_provider_factory = lambda _settings: MockChatProvider(["A private reply."])
    window.open_story(story.id)
    try:
        monkeypatch.setattr(window, "_attest_or_say_not", lambda settings: None)
        window.composer.private_toggle.click()
        assert window.session.open_span.privacy.service == TINFOIL
        window.session.save()

        boxes: list[tuple[str, list[str]]] = []

        def answer(box):
            boxes.append((box.text(), [b.text() for b in box.buttons()]))
            next(b for b in box.buttons() if b.text() == "Keep it open").click()
            return 0

        monkeypatch.setattr(QMessageBox, "exec", answer)
        # A later run: Settings → Private points at Chutes now, whose -TEE
        # models have partial privacy: no model the scene may move to is offered.
        window.config.private_provider = ProviderConfig(
            name="private", base_url=CHUTES_BASE_URL, model="zai-org/X-TEE"
        )
        window._resume_private()
        text, buttons = boxes[-1]
        assert "began end-to-end encrypted through Tinfoil" in text
        assert "Use another model…" not in buttons
        # Then at NanoGPT's Private Mode, at the scene's own level: offered.
        window.config.private_provider = ProviderConfig(
            name="private", base_url=NANO, model="private/kimi-k3"
        )
        boxes.clear()
        window._resume_private()
        text, buttons = boxes[-1]
        assert "began end-to-end encrypted through Tinfoil" in text
        assert {"Use another model…", "End the scene", "Keep it open"} <= set(buttons)
        assert window._tee_state == "⚠ held back"
        # Sending says it again, and nothing goes.
        assert not window._tee_allows_send()
        assert len(boxes) == 2 and window.session.in_private
        window.session.discard_private()

        # A chat whose endpoint was edited under it: said on opening, every send held.
        warned: list[str] = []
        monkeypatch.setattr(QMessageBox, "warning", lambda _p, _t, text, *a: warned.append(text))
        window.session.story.mode = "chat"
        window.session.story.defaults.main_model = "Tinfoil::glm-5-3"
        window.config.providers.append(tinfoil())
        window.session.adopt_chat_privacy()
        assert window.session.story.chat_privacy.service == TINFOIL
        window.config.providers[-1] = tinfoil(base_url=ELSEWHERE)
        window._attest_chat()
        assert warned and "api.example.com" in warned[-1]
        assert not window._tee_allows_send() and len(warned) == 2
    finally:
        if window.session is not None and window.session.in_private:
            window.session.discard_private()
        window._chat_allows_close = lambda: True
        window.close()
