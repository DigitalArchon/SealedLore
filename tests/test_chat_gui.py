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
from sealedlore.storage.repository import (  # noqa: E402
    StoryBundle,
    load_story_bundle,
    save_story_bundle,
)
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
    # A memory chat still open would ask before going, with no one to answer
    # (the test's own patches are undone by now): a failed test hung the run.
    window._chat_allows_close = lambda: True
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


def wait_for(app: QApplication, condition, timeout: float = 20.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        app.processEvents()
        time.sleep(0.005)


def send(app, window, text: str) -> None:
    window.composer.input.setPlainText(text)
    window.send_turn()
    wait_for(app, lambda: not window._busy)


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
    # Of a story's controls a chat keeps one: a part of it can be held in
    # private, as a scene of a story can.
    composer = window.composer
    assert not composer.controls_host.isHidden()
    for control in (composer.held, composer.speaker, composer.scope, composer.length):
        assert control.isHidden()
    for control in (composer.agency, composer.skill, composer.ooc_toggle):
        assert control.isHidden()
    assert not composer.private_toggle.isHidden() and not composer.private_keep.isHidden()
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
    # Pictures work, kept in memory with the chat (storage.picture_store);
    # every way in reaches the dialog.
    assert window.image_action.isEnabled() and window.composer.image_button.isEnabled()
    opened: list = []

    class NoDialog:
        def __init__(self, *args, **kwargs):
            opened.append(1)

        def exec(self):  # noqa: A003 - Qt naming
            return 0

        def writer_model(self):
            return None

    monkeypatch.setattr("sealedlore.gui.window_images.ImageDialog", NoDialog)
    from sealedlore.models.config import ProviderConfig

    window.config.providers = [ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1")]
    window.config.active_provider_name = "nano"
    window.generate_image()
    window.generate_image(anchor_id=session.path()[-1].id)
    assert opened == [1, 1]
    assert listing(tmp_path) == before

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


def test_a_memory_chat_keeps_its_private_part_in_memory_with_no_choice_shown(
    app, window, monkeypatch, tmp_path
):
    """The author: a chat started in memory only still offers Private, but
    asking In memory or On disk makes no sense there."""
    from sealedlore.models.config import ProviderConfig
    from sealedlore.providers.mock import MockChatProvider

    make_chat(window, monkeypatch, model="z-ai/glm-5.3", keep="memory")
    composer = window.composer
    app.processEvents()
    assert composer.private_toggle.isVisible()
    assert not composer.private_keep.isVisible()

    window.config.private_keep = "disk"
    composer.private_keep.setCurrentIndex(composer.private_keep.findData("disk"))
    window.private_provider_factory = lambda settings: MockChatProvider(["Quietly."])
    window.config.private_provider = ProviderConfig(
        name="private", base_url="http://localhost:11434/v1", model="local/model"
    )
    window.config.private_intro_seen = True
    before = listing(tmp_path)
    window.begin_private()
    assert window.session.in_private
    assert window.session.open_span.keep == "memory"
    assert window.config.private_keep == "disk", "a memory chat changed the setting"
    assert listing(tmp_path) == before

    # A chat kept on disk offers the choice again.
    window.session.discard_private()
    window._sync_private_ui()
    window._chat_allows_close = lambda: True
    make_chat(window, monkeypatch, keep="disk")
    app.processEvents()
    assert composer.private_keep.isVisible()


def test_a_chat_can_hold_a_part_in_private_and_take_back_a_summary(app, window, monkeypatch):
    """A beta tester asked for it: switch to private mid-chat, as a story
    can. The Private button is all of a story's controls a chat shows."""
    from PySide6.QtWidgets import QMessageBox

    from sealedlore.gui.private_dialogs import PrivateSummaryDialog
    from sealedlore.models.config import ProviderConfig
    from sealedlore.providers.mock import MockChatProvider

    make_chat(window, monkeypatch)
    composer = window.composer
    window.show()
    app.processEvents()
    assert composer.private_toggle.isVisible() and composer.private_keep.isVisible()
    assert not composer.held.isVisible() and not composer.length.isVisible()
    assert not composer.ooc_toggle.isVisible() and not composer.skill.isVisible()
    assert "chat" in composer.private_toggle.toolTip()
    assert "story" not in composer.private_toggle.toolTip()

    send(app, window, "What is a torus?")
    private = MockChatProvider(["Between us, then: MARKER-IN-PRIVATE."])
    window.private_provider_factory = lambda settings: private
    window.config.private_provider = ProviderConfig(
        name="private", base_url="http://localhost:11434/v1", model="local/model"
    )
    window.config.private_intro_seen = True
    composer.private_keep.setCurrentIndex(composer.private_keep.findData("disk"))
    window.begin_private()
    assert window.session.in_private
    assert composer.private_toggle.text() == "End private"
    assert composer.input.property("private") == "true"
    assert "chat's own model" in window.statusBar().currentMessage()
    assert not window.stories.isEnabled()

    main_calls = len(window.session.provider.requests)
    send(app, window, "Something for the private model only.")
    assert len(private.requests) == 1
    assert len(window.session.provider.requests) == main_calls

    # Ending it: the private model writes the summary, the author approves.
    private.responses = ["They talked something over in private and settled it."]
    seen: list[PrivateSummaryDialog] = []

    def approve(dialog):
        seen.append(dialog)
        dialog.choice = PrivateSummaryDialog.APPROVE
        return PrivateSummaryDialog.Accepted

    monkeypatch.setattr(PrivateSummaryDialog, "exec", approve)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    window.end_private()
    wait_for(app, lambda: not window._busy and not window.session.in_private)
    assert seen and seen[0].windowTitle() == "Private part summary"
    assert not window.session.in_private
    assert composer.private_toggle.text() == "Private"
    assert composer.input.property("private") == "false"
    last = window.session.path()[-1]
    assert last.meta.private_summary_of and "settled it" in last.content

    send(app, window, "And a sphere?")
    sent = str(window.session.provider.payloads[-1])
    assert "MARKER-IN-PRIVATE" not in sent and "for the private model only" not in sent
    assert "settled it" in sent and "held in private" in sent


def test_a_tee_chat_offers_no_private_part(app, window, monkeypatch):
    make_chat(window, monkeypatch, model="TEE/glm-5.3-flash")
    window.show()
    app.processEvents()
    assert not window.composer.private_toggle.isVisible()
    assert window.composer.input.property("private") == "true"  # private throughout
    # A story opened afterwards has its Private button back.
    window.composer.set_chat_mode(False)
    app.processEvents()
    assert window.composer.private_toggle.isVisible() and window.composer.held.isVisible()


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


def test_the_story_list_opens_a_story_s_folder(app, window, monkeypatch, tmp_path):
    import sealedlore.gui.main_window as main_window

    story_id = window.stories.list.item(0).data(0x0100)
    opened: list[str] = []
    monkeypatch.setattr(
        main_window.QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    menu = window.stories.menu_for(story_id)
    next(action for action in menu.actions() if action.text() == "Open folder").trigger()
    assert opened == [str(tmp_path / "stories" / story_id)]


def test_close_story_goes_back_to_the_start_page(app, window, tmp_path):
    before = listing(tmp_path)
    assert window.close_story_action.isEnabled()
    window.close_story()
    assert window.session is None
    assert not window.close_story_action.isEnabled()
    assert listing(tmp_path) == before, "closing a story changed its files"
    # Nothing of the story is left showing: its branch or who was played.
    assert window.branch_bar.button.text().startswith("—")
    assert not window.branch_bar.map_button.isEnabled()
    assert window.composer.held.count() == 0 and window.composer.speaker.count() == 0

    # Opened again, you speak as the character you play, not as the narrator.
    window.open_story(window.stories.list.item(0).data(0x0100))
    assert window.composer.speaker.currentData() == window.composer.held.currentData()


def test_close_story_asks_before_a_memory_chat_goes(app, window, monkeypatch, tmp_path):
    before = listing(tmp_path)
    monkeypatch.setattr(window, "_attest_chat", lambda: None)
    make_chat(window, monkeypatch, model="TEE/gemma-3-27b", keep="memory")
    session = window.session
    session.provider.responses = ["Understood."]
    list(session.send(TurnRequest(speaker_id=CHAT_USER_ID, user_text="Keep this secret.")))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Cancel)
    window.close_story()
    assert window.session is session
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    window.close_story()
    assert window.session is None
    assert listing(tmp_path) == before, "a memory-only chat reached the disk"


def test_close_story_waits_while_a_job_runs(app, window):
    window._busy = True
    window._update_controls()
    assert not window.close_story_action.isEnabled()
    window.close_story()
    assert window.session is not None
    window._busy = False
    window._update_controls()


def test_any_chat_may_be_kept_in_memory_and_says_what_that_means(app):
    dialog = window_chat.NewChatDialog(model="z-ai/glm-5.3")
    assert not dialog.keep.isHidden()
    dialog.keep.setCurrentIndex(dialog.keep.findData("memory"))
    assert dialog.keep_choice() == "memory"
    assert "provider still receives every message" in dialog.keep_hint.text()
    dialog.model.setText("TEE/gemma-3-27b")
    assert "Attested" in dialog.keep_hint.text()
    dialog.deleteLater()


def test_a_memory_chat_on_a_plain_model_stays_off_the_disk_and_out_of_the_config(
    app, window, monkeypatch, tmp_path
):
    before = listing(tmp_path)
    # The fixture's close asks before a memory chat goes.
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    make_chat(window, monkeypatch, model="z-ai/glm-5.3", keep="memory")
    session = window.session
    assert session.memory_only and not session.tee_chat
    assert "(in memory only)" in window.windowTitle()
    session.provider.responses = ["Understood."]
    list(session.send(TurnRequest(speaker_id=CHAT_USER_ID, user_text="Keep this quiet.")))
    window.reload_transcript()
    window._refresh_story_cost()
    # The model button changes the chat's model, never the endpoint's default:
    # that would carry the chat's choice into config.json.
    from sealedlore.models.config import ProviderConfig

    window.config.providers = [
        ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1", model="z-ai/glm-5.3")
    ]
    window.config.active_provider_name = "nano"
    config_before = window.config.model_dump_json()
    monkeypatch.setattr(
        window, "_browse_models", lambda _current, **_limits: "moonshotai/kimi-k2.6"
    )
    window.choose_story_model()
    assert session.model == "moonshotai/kimi-k2.6"
    assert window.config.model_dump_json() == config_before
    assert listing(tmp_path) == before, "a memory-only chat wrote to disk"
    # Left before the test ends: the fixture's close would ask, unanswered.
    window.close_story()
    assert window.session is None


def test_a_new_chat_takes_its_own_route(app, window, monkeypatch, tmp_path):
    """A chat chooses its model in the New chat dialog, so its route is there
    too, and belongs to the chat: never the settings, memory chat or not."""
    from sealedlore.engine.catalog import ModelInfo
    from sealedlore.models.config import ProviderConfig
    from sealedlore.models.route import ModelRoute

    nano = ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1", model="z-ai/glm-5.3")
    dialog = window_chat.NewChatDialog(model="z-ai/glm-5.3", endpoint=nano)
    assert not dialog.model.route_button.isHidden()
    dialog.model.setText("TEE/glm-5.3")
    assert dialog.model.route_button.isHidden(), "a TEE chat has no route"
    dialog.deleteLater()
    assert window_chat.NewChatDialog(model="z-ai/glm-5.3").model.route_button.isHidden(), (
        "no endpoint (mock): no route"
    )

    window.catalog.models = {"z-ai/glm-5.3": ModelInfo(id="z-ai/glm-5.3", hosts=("a", "b"))}
    config_before = window.config.model_dump_json()

    class Dialog(window_chat.NewChatDialog):
        def exec(self):  # noqa: A003 - Qt naming
            self._endpoint = nano
            self.title.setText("Quick")
            self.model.setText("z-ai/glm-5.3")
            self.model.set_route(ModelRoute(priority="latency"))
            self.keep.setCurrentIndex(self.keep.findData("memory"))
            return window_chat.NewChatDialog.Accepted

    monkeypatch.setattr(window_chat, "NewChatDialog", Dialog)
    before = listing(tmp_path)
    window.new_chat()
    session = window.session
    assert session.story.defaults.main_route == ModelRoute(priority="latency")
    assert window.config.model_dump_json() == config_before, "the chat's route reached the settings"
    assert listing(tmp_path) == before
    window.catalog.models = None
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    window.close_story()


def test_the_storytellers_route_can_be_changed_beside_its_model(app, window, monkeypatch, tmp_path):
    """A route chosen only in New chat or Settings couldn't be swapped when a
    host let the storyteller down. The route button beside the model button
    changes it: a story's is the storyteller's in the settings, a chat's its
    own and never the settings'."""
    import sealedlore.gui.main_window as main_window_module
    from sealedlore.engine.catalog import ModelInfo
    from sealedlore.models.config import ProviderConfig
    from sealedlore.models.route import ModelRoute

    nano = ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1", model="z-ai/glm-5.3")
    window.config.providers = [nano]
    window.config.active_provider_name = "nano"
    monkeypatch.setattr(window, "use_mock", False)
    monkeypatch.setattr(window, "_fetch_host_prices", lambda: None)  # no network
    hosts = {"z-ai/glm-5.3": ModelInfo(id="z-ai/glm-5.3", hosts=("a", "b"))}
    monkeypatch.setattr(window.catalog, "models", hosts)
    seen: list[ModelRoute | None] = []
    chosen: list[ModelRoute | None] = []

    class Dialog:  # the real one fetches the model's hosts
        def __init__(self, endpoint, model, route, parent):
            seen.append(route)

        def exec(self):  # noqa: A003 - Qt naming
            return main_window_module.QDialog.Accepted

        def route(self):
            return chosen[-1]

        def deleteLater(self):  # noqa: N802 - Qt naming
            pass

    monkeypatch.setattr(main_window_module, "RouteDialog", Dialog)

    # An ordinary story: the storyteller's route, for every story.
    window.session.story.defaults.main_model = "z-ai/glm-5.3"
    window._update_controls()
    assert not window.route_button.isHidden() and window.route_button.text() == "Route…"
    chosen.append(ModelRoute(priority="latency"))
    window.choose_story_route()
    assert seen == [None]
    assert window.config.model_routes["story"] == ModelRoute(priority="latency")
    assert window.session.route_for("story")["provider"]["sort"] == "latency"
    assert window.route_button.text() == "⚡ Fastest start"
    chosen.append(None)
    window.choose_story_route()
    assert "story" not in window.config.model_routes

    # A chat: its own route, never the settings.
    make_chat(window, monkeypatch, model="z-ai/glm-5.3")
    session = window.session
    window._update_controls()
    assert window.route_button.text() == "Route…"
    config_before = window.config.model_dump_json()
    chosen.append(ModelRoute(priority="host", host="b", host_model="z-ai/glm-5.3"))
    window.choose_story_route()
    assert session.story.defaults.main_route == chosen[-1]
    assert window.route_button.text() == "⚡ b"
    assert session.route_for("story")["provider"]["order"] == ["b"]
    assert window.config.model_dump_json() == config_before, "the chat's route reached the settings"
    reloaded = load_story_bundle(session.story.id, root=tmp_path)
    assert reloaded.story.defaults.main_route == chosen[-1]

    # A model with one host, or a TEE model: no route to change.
    hosts["z-ai/glm-5.3"] = ModelInfo(id="z-ai/glm-5.3", hosts=("a",))
    window._update_controls()
    assert window.route_button.isHidden()
