"""A simple chat in the window: made from File → New simple chat…, shown with
only what a chat has, kept in memory only when asked. Offscreen, mocks only."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import sealedlore.gui.window_chat as window_chat  # noqa: E402
from sealedlore.engine.prompt import TurnRequest  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.setup_dialog import SetupDialog  # noqa: E402
from sealedlore.models.node import CHAT_USER_ID  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path: Path, story, cast):
    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    bundle.story.held_character_id = "char-serrik"
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.resize(1366, 768)
    window.show()
    window.open_story(story.id)
    yield window
    window.close()


def make_chat(window, monkeypatch, *, model="anthropic/claude-sonnet-4.6", keep="disk"):
    class Dialog(window_chat.NewChatDialog):
        def exec(self):  # noqa: A003 - Qt naming
            self.title.setText("Tutor")
            self.model.setText(model)
            self.prompt.setPlainText("You are a patient tutor.")
            self.tail.setPlainText("Answer in one paragraph.")
            self.keep.setCurrentIndex(self.keep.findData(keep))
            return window_chat.NewChatDialog.Accepted

    monkeypatch.setattr(window_chat, "NewChatDialog", Dialog)
    window.new_chat()


def listing(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_a_new_chat_shows_only_what_a_chat_has(app, window, monkeypatch):
    make_chat(window, monkeypatch)
    session = window.session
    assert session.story.chat and session.story.chat_prompt == "You are a patient tutor."
    # The tail is set with the chat, not only afterwards in Setup.
    assert session.story.chat_tail == "Answer in one paragraph."
    assert window.composer.controls_host.isHidden()
    tabs = window.right_tabs
    for page in (window.scene_panel, window.cast_panel, window.lore_panel, window.style_panel):
        assert not tabs.isTabVisible(tabs.indexOf(page))
    assert tabs.isTabVisible(tabs.indexOf(window.summaries_panel))
    assert not window.review_action.isEnabled() and not window.start_action.isEnabled()
    assert window.image_action.isEnabled(), "pictures work in a chat kept on disk"
    assert window._current_turn("Hello").speaker_id == CHAT_USER_ID

    # A story opened afterwards has everything back.
    window.open_story(window.stories.list.item(0).data(0x0100))  # Qt.UserRole
    if window.session.story.chat:  # the list's first entry may be the chat itself
        window.open_story(window.stories.list.item(1).data(0x0100))
    assert not window.composer.controls_host.isHidden()
    assert tabs.isTabVisible(tabs.indexOf(window.scene_panel))


def test_a_first_message_is_kept_in_full_and_can_be_let_go(app, window, monkeypatch):
    make_chat(window, monkeypatch)
    session = window.session
    session.provider.responses = ["Hello there."]
    list(session.send(TurnRequest(speaker_id=CHAT_USER_ID, user_text="Hi, I'm Ada.")))
    window.reload_transcript()
    first = session.path()[0]
    widget = window.transcript.message_widget(first.id)
    assert "You" in widget.header.text() and "kept in full" in widget.header.text()
    action = widget.actions_["keep_full"]
    assert action.isChecked()
    action.trigger()
    assert not first.meta.keep_full
    assert "kept in full" not in window.transcript.message_widget(first.id).header.text()
    reply = window.transcript.message_widget(session.path()[1].id)
    assert reply.header.text().startswith("Assistant")


def test_setup_for_a_chat_is_its_name_and_system_prompt(app, window, monkeypatch):
    make_chat(window, monkeypatch)
    dialog = SetupDialog(window.session.story, window.session.cast)
    names = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
    assert names == ["About", "System prompt"]
    dialog.chat_prompt.setPlainText("Answer in French.")
    dialog.chat_tail.setPlainText("Two sentences at most.")
    dialog._save()
    assert window.session.story.chat_prompt == "Answer in French."
    assert window.session.story.chat_tail == "Two sentences at most."
    dialog.deleteLater()


def test_a_memory_only_chat_is_never_written_and_asks_before_it_goes(
    app, window, monkeypatch, tmp_path
):
    before = listing(tmp_path)
    attested: list[str] = []
    monkeypatch.setattr(window, "_attest_chat", lambda: attested.append("asked"))
    make_chat(window, monkeypatch, model="TEE/gemma-3-27b", keep="memory")
    session = window.session
    assert session.memory_only and session.tee_chat
    assert "(in memory only)" in window.windowTitle()
    assert attested == ["asked"], "a TEE chat is attested when it opens"
    session.provider.responses = ["Understood."]
    list(session.send(TurnRequest(speaker_id=CHAT_USER_ID, user_text="Keep this secret.")))
    window.reload_transcript()
    window._refresh_story_cost()
    assert listing(tmp_path) == before, "a memory-only chat wrote to disk"
    for action in (window.image_action, window.export_action, window.archive_action):
        assert not action.isEnabled()
    # Every other way to a picture is closed too: the composer's button, and
    # generate_image itself (a message's picture entry, "Generate again").
    assert not window.composer.image_button.isEnabled()
    opened: list = []
    monkeypatch.setattr(
        "sealedlore.gui.window_images.ImageDialog", lambda *a, **k: opened.append(1)
    )
    window.generate_image()
    window.generate_image(anchor_id=session.path()[-1].id)
    assert opened == []

    # Leaving asks; Cancel stays.
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Cancel)
    other = window.stories.list.item(0).data(0x0100)
    window.open_story(other)
    assert window.session is session
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    window.open_story(other)
    assert window.session is not session


def test_a_tee_chat_refuses_a_model_that_isnt(app, window, monkeypatch):
    monkeypatch.setattr(window, "_attest_chat", lambda: None)
    make_chat(window, monkeypatch, model="TEE/gemma-3-27b")
    told: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[2]))
    monkeypatch.setattr(
        window, "_browse_models", lambda _current, **_limits: "anthropic/claude-sonnet-4.6"
    )
    window.choose_story_model()
    assert window.session.model == "TEE/gemma-3-27b" and told
    monkeypatch.setattr(window, "_browse_models", lambda _current, **_limits: "TEE/deepseek-v3")
    window.choose_story_model()
    assert window.session.model == "TEE/deepseek-v3"


def test_the_picture_dialog_lets_a_chat_add_a_picture_from_disk(app, window, monkeypatch):
    from sealedlore.gui.image_dialog import ImageDialog

    make_chat(window, monkeypatch)
    dialog = ImageDialog(window.session, catalog=window.image_catalog)
    dialog._fill_add_menu()
    labels = [action.text() for action in dialog.add_ref.menu().actions()]
    assert "Add a picture from disk…" in labels
    dialog.deleteLater()


def test_the_view_toggle_speaks_of_the_model_in_a_chat(app, window, monkeypatch):
    assert window.view_toggle.sent.text() == "As sent to the storyteller"
    make_chat(window, monkeypatch)
    assert window.view_toggle.sent.text() == "As sent to the model"


def test_an_encrypted_chat_is_sent_only_through_the_sealing_client(app, window, monkeypatch):
    from sealedlore.models.config import ProviderConfig
    from sealedlore.providers.openai_compat import OpenAICompatibleProvider
    from sealedlore.providers.private_mode import PrivateModeProvider, attestation_record

    monkeypatch.setattr(window, "_attest_chat", lambda: None)
    # Closing a memory-only chat asks first; the fixture's close says yes.
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    make_chat(window, monkeypatch, model="private/glm-5-3", keep="memory")
    assert window.session.tee_chat and window.session.memory_only
    window.use_mock = False  # the real providers, built but never called
    monkeypatch.setattr(
        window.config,
        "providers",
        [ProviderConfig(name="n", base_url="https://nano-gpt.com/api/v1", api_key="k")],
    )
    monkeypatch.setattr(window.config, "active_provider_name", "n")
    sealed = window._current_provider()
    assert isinstance(sealed, PrivateModeProvider) and window._current_provider() is sealed
    private = window._make_private_provider(
        ProviderConfig(name="p", base_url="https://nano-gpt.com/api/v1", model="private/kimi-k3")
    )
    assert isinstance(private, PrivateModeProvider)
    assert (
        type(window._make_private_provider(ProviderConfig(name="p", model="TEE/glm-5.3")))
        is OpenAICompatibleProvider
    )

    # An attestation that came back: the badge and the record.
    attestation = attestation_record(
        enclave="router-0.tinfoil.sh",
        hardware="AMD SEV-SNP",
        measurement="m",
        release_digest="d" * 64,
        release_tag="v1",
        latest_release="",
        latest_checked=True,
        hpke_public_key="aa",
    )
    window._on_attested(None, None, [attestation], [])
    assert window.session.story.chat_attestation.encrypted
    assert "end-to-end encrypted" in window._private_model_text()
    assert "TEE" not in window._private_model_text()
    window.use_mock = True
    window._discard_provider()
    window.open_story(window.stories.list.item(0).data(0x0100))  # leave the memory chat


def test_a_tee_chats_picture_leaves_the_prompt_writer_setting_alone(app, window, monkeypatch):
    import sealedlore.gui.window_images as window_images

    monkeypatch.setattr(window, "_attest_chat", lambda: None)
    make_chat(window, monkeypatch, model="TEE/gemma-3-27b")
    from sealedlore.models.config import ProviderConfig

    monkeypatch.setattr(window.config, "providers", [ProviderConfig(name="n", api_key="k")])
    monkeypatch.setattr(window.config, "active_provider_name", "n")
    window.config.image_prompt_model = "deepseek/deepseek-v4.1-flash"

    class Dialog:
        request = None

        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return 0

        def writer_model(self):
            return None  # a TEE chat's own model writes

    monkeypatch.setattr(window_images, "ImageDialog", Dialog)
    window.generate_image()
    assert window.config.image_prompt_model == "deepseek/deepseek-v4.1-flash"


def test_a_tee_chats_model_button_lists_only_what_it_may_move_to(app, window, monkeypatch):
    monkeypatch.setattr(window, "_attest_chat", lambda: None)
    assert window._chat_model_limits() == {}, "a story: every model"
    make_chat(window, monkeypatch, model="TEE/gemma-3-27b")
    limits = window._chat_model_limits()
    allowed = limits["allowed"]
    assert allowed("TEE/glm-5.3") and allowed("private/glm-5-3")
    assert not allowed("anthropic/claude-sonnet-4.6")
    asked: list[dict] = []
    monkeypatch.setattr(window, "_browse_models", lambda _current, **kw: asked.append(kw) or None)
    window.choose_story_model()
    assert asked and asked[0]["allowed"]("private/kimi-k3")
    assert not asked[0]["allowed"]("anthropic/claude-sonnet-4.6")


def test_a_refused_tee_chat_sends_nothing_until_attested_again(app, window, monkeypatch):
    """The refuse policy (providers/tee.TeeClient.attest): nothing reaches a
    model whose enclave is being attested or was refused."""
    from sealedlore.models.private import Attestation
    from sealedlore.providers.tee import TeeRefused

    monkeypatch.setattr(window, "_attest_chat", lambda: None)
    make_chat(window, monkeypatch, model="TEE/gemma-3-27b")
    session = window.session
    session.provider.responses = ["Should never be sent."]

    # Refused: the session refuses whatever calls it, and the window offers a retry.
    offered: list[str] = []
    monkeypatch.setattr(window, "_offer_reattest", lambda reason, for_chat: offered.append(reason))
    window._on_attested(None, _FakeTee(), [], ["TEE/gemma-3-27b offers no attestation"])
    assert offered == ["TEE/gemma-3-27b offers no attestation"]
    assert "refused" in window._tee_state and "offers no attestation" in session.tee_block
    with pytest.raises(TeeRefused, match="nothing was sent"):
        list(session.send(window._chat_turn("Hello")))
    assert session.provider.requests == [] and session.path() == []
    window.composer.input.setPlainText("Hello")
    window.send_turn()
    assert len(offered) == 2 and session.provider.requests == []

    # Still being attested: sending waits.
    session.tee_block = "TEE/gemma-3-27b's enclave is still being attested"
    window._tee_thread = object()
    window.send_turn()
    assert session.provider.requests == []
    window._tee_thread = None

    # Attested, partially: sending goes ahead and the reason is on the model button.
    partial = Attestation(
        verified=True, level="partial", shortfalls=["no GPU evidence"], signing_address="0xab"
    )
    window._on_attested(None, _FakeTee(), [partial], [])
    assert session.tee_block is None
    assert "partial: no GPU evidence" in window._private_model_text()
    list(session.send(window._chat_turn("Hello")))
    assert session.provider.requests


class _FakeTee:
    def close(self):
        pass

    def verify_reply(self, _response_id):
        return True


def test_the_authors_messages_say_how_they_were_sent(app, window, monkeypatch):
    monkeypatch.setattr(window, "_attest_chat", lambda: None)
    make_chat(window, monkeypatch, model="TEE/gemma-3-27b")
    session = window.session
    session.private_tee = _FakeTee()
    session.provider.responses = ["Hi."]
    list(session.send(window._chat_turn("Hello")))
    window.reload_transcript()
    mine, reply = session.path()
    assert "sent to the attested TEE" in window.transcript.message_widget(mine.id).header.text()
    assert "TEE signed" in window.transcript.message_widget(reply.id).header.text()
    assert "signed" not in window.transcript.message_widget(mine.id).header.text()


def test_a_tee_chat_with_checking_off_says_so_on_the_model_button(app, window, monkeypatch):
    """ "Check TEE models" off in Settings is easy to forget: an unchecked TEE
    chat says so where the attestation would otherwise show."""
    from sealedlore.models.config import ProviderConfig

    window.config.providers = [
        ProviderConfig(name="p", base_url="https://nano-gpt.com/api/v1", api_key="k")
    ]
    window.config.active_provider_name = "p"
    window.config.private_verify_tee = False
    monkeypatch.setattr(window, "use_mock", False)
    make_chat(window, monkeypatch, model="TEE/gemma-3-27b")
    assert window.session.tee_chat and window.session.tee_block is None
    assert "not checked" in window._tee_state
    assert "Check TEE models" in window._private_model_text()
