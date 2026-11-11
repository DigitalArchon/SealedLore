"""Private scenes in the window: one click in, the controls that would leak
locked, the summary approved on the way out. Offscreen, mocks only."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import sealedlore.gui.main_window as main_window_module  # noqa: E402
import sealedlore.gui.window_private as window_private  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.models.config import ProviderConfig  # noqa: E402
from sealedlore.providers.base import ProviderError  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.repository import (  # noqa: E402
    StoryBundle,
    load_story_bundle,
    save_story_bundle,
)
from tests.conftest import make_exchange  # noqa: E402

MARKER = "ZQX-PRIVATE"


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


@pytest.fixture
def window(app, tmp_path: Path, story, cast):
    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    bundle.story.held_character_id = "char-serrik"
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.scene_reads = "manual"
    window.config.private_provider = ProviderConfig(
        name="private", base_url="http://localhost:11434/v1", model="local/tiny"
    )
    window.config.private_budget = 50_000
    window.config.private_intro_seen = True  # its own test below
    private = MockChatProvider([f"A private reply with {MARKER}."])
    window.private_provider_factory = lambda _settings: private
    window.open_story(story.id)
    window._provider = MockChatProvider(["The story model's reply."])
    window.private_mock = private
    yield window
    # Closing mid-scene asks first (a modal question): end the scene before.
    if window.session is not None and window.session.in_private:
        window.session.discard_private()
    window.close()


def send(app, window: MainWindow, text: str) -> None:
    window.composer.input.setPlainText(text)
    window.send_turn()
    wait_for(app, lambda: not window._busy)


def test_one_click_in_locks_what_would_leak(app, window: MainWindow):
    window.composer.private_keep.setCurrentIndex(window.composer.private_keep.findData("memory"))
    window.composer.private_toggle.click()
    assert window.session.in_private
    assert window.composer.private_toggle.isChecked()
    assert not window.composer.private_keep.isEnabled()
    assert "🔒" in window.model_button.text() and "memory only" in window.model_button.text()
    # Pictures stay available: the private model writes the prompt, and the
    # dialog warns that the picture itself is neither private nor unsaved.
    assert window.composer.image_button.isEnabled()
    assert window.image_action.isEnabled()
    assert not window.stories.isEnabled()
    assert "End private" in window.stories.toolTip()
    assert window.composer.input.property("private") == "true"
    assert window.minimumSizeHint().width() <= 1280


def test_a_private_scene_from_first_turn_to_approved_summary(app, window: MainWindow, monkeypatch):
    window.composer.private_toggle.click()
    send(app, window, f"I whisper {MARKER}.")
    assert window.private_mock.payloads
    headers = [
        w.header.text()
        for w in window.transcript.findChildren(type(window.transcript.add_message("x", "y", "z")))
    ]
    assert any("private" in header for header in headers)

    class Approve:
        APPROVE, AGAIN, BACK, DISCARD = "approve", "again", "back", "discard"

        def __init__(self, summary, parent=None, *, note=None, chat=False):
            self.choice = self.APPROVE
            self._summary = summary

        def exec(self):
            return 1

        def text(self):
            return "They spoke in private and agreed to meet at dawn."

    monkeypatch.setattr(window_private, "PrivateSummaryDialog", Approve)
    window.private_mock.responses = ["They spoke in private."]
    window.composer.private_toggle.click()  # end the scene: the summary job runs
    wait_for(app, lambda: not window._busy and not window.session.in_private)
    leaf = window.session.full_path()[-1]
    assert leaf.meta.private_summary_of is not None
    assert "agreed to meet at dawn" in leaf.content
    assert not window.composer.private_toggle.isChecked()
    assert window.composer.private_keep.isEnabled()
    assert "🔒" not in window.model_button.text()
    assert not window.composer.regenerate_button.isEnabled()

    saved = load_story_bundle(window.session.story.id, root=window.root)
    assert MARKER not in "".join(node.content for node in saved.nodes)


def test_closing_mid_scene_in_memory_asks_first(app, window: MainWindow, monkeypatch):
    window.composer.private_toggle.click()
    send(app, window, f"{MARKER} a")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Cancel)
    assert not window._private_allows_close()
    assert window.session.in_private
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    assert window._private_allows_close()
    assert not window.session.in_private


def test_close_story_mid_memory_scene_asks_and_discards(app, window: MainWindow, monkeypatch):
    window.composer.private_toggle.click()
    send(app, window, f"{MARKER} a")
    session = window.session
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Cancel)
    window.close_story()
    assert window.session is session and session.in_private
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    window.close_story()
    assert window.session is None
    assert not session.in_private


def test_continue_waits_for_the_scene_s_own_passage(app, window: MainWindow):
    passage = window.session.full_path()[-1]
    assert window.composer.continue_button.isEnabled()
    window.composer.private_toggle.click()
    assert window.session.in_private
    # The latest passage is the story model's: carried on in the scene, it
    # would be sent there.
    assert not window.composer.continue_button.isEnabled()
    assert not window.transcript.message_widget(passage.id).actions_["continue"].isVisible()
    window.continue_passage()
    assert not window._busy
    send(app, window, "I lower my voice.")
    assert window.composer.continue_button.isEnabled()


class _Approve:
    """The summary dialog, answered Approve as it stands."""

    APPROVE, AGAIN, BACK, DISCARD = "approve", "again", "back", "discard"

    def __init__(self, summary, parent=None, *, note=None, chat=False):
        self.choice = self.APPROVE

    def exec(self):
        return 1

    def text(self):
        return "They spoke in private."


def _ended_scene(app, window: MainWindow, monkeypatch, keep: str):
    window.composer.private_keep.setCurrentIndex(window.composer.private_keep.findData(keep))
    window.composer.private_toggle.click()
    for turn in ("a", "b"):
        window.private_mock.responses = [f"A private reply with {MARKER} {turn}."]
        send(app, window, f"I whisper {MARKER} {turn}.")
    monkeypatch.setattr(window_private, "PrivateSummaryDialog", _Approve)
    window.private_mock.responses = ["They spoke."]
    window.composer.private_toggle.click()
    wait_for(app, lambda: not window._busy and not window.session.in_private)
    path = window.session.full_path()
    return [node for node in path if node.meta.private_span]


@pytest.mark.parametrize("keep", ["disk", "memory"])
def test_an_ended_scenes_turn_is_never_taken_back_into_the_box(app, window, monkeypatch, keep):
    """Take back on an ended scene's turn put its words in the public box,
    deleted the scene and its approved summary, and Send gave them to the
    story's model and the reads (the leak rule)."""
    scene = _ended_scene(app, window, monkeypatch, keep)
    turn = next(node for node in scene if node.kind == "user")
    widget = window.transcript.message_widget(turn.id)
    assert not widget.actions_["take_back"].isVisible(), "not offered"
    warned: list[str] = []
    monkeypatch.setattr(window, "_warn_plain", lambda title, _text: warned.append(title))
    before = len(window.session.nodes)
    window._take_back(turn.id)
    assert warned == ["Not from a private scene that has ended"]
    assert window.composer.text() == "" and len(window.session.nodes) == before


def test_retakes_and_branches_are_offered_only_where_the_session_allows(app, window, monkeypatch):
    """Right after a scene began, Regenerate was on and ⋯ offered it on every
    earlier passage, and after it ended on its own passages: each ended in
    "Generation failed". New branch from here… on an ended scene's message
    raised in a slot, closing the editor and losing what was typed."""
    public = window.session.full_path()[-1]
    window.composer.private_toggle.click()
    assert not window.composer.regenerate_button.isEnabled()
    actions = window.transcript.message_widget(public.id).actions_
    assert not actions["regenerate"].isVisible() and not actions["rewrite"].isVisible()
    window.private_mock.responses = [f"A private reply with {MARKER}."]
    send(app, window, f"I whisper {MARKER}.")
    scene_passage = window.session.full_path()[-1]
    assert window.composer.regenerate_button.isEnabled(), "the scene's own passage"
    assert window.transcript.message_widget(scene_passage.id).actions_["regenerate"].isVisible()
    monkeypatch.setattr(window_private, "PrivateSummaryDialog", _Approve)
    window.private_mock.responses = ["They spoke."]
    window.composer.private_toggle.click()
    wait_for(app, lambda: not window._busy and not window.session.in_private)
    actions = window.transcript.message_widget(scene_passage.id).actions_
    assert not actions["regenerate"].isVisible() and not actions["rewrite"].isVisible()
    assert window.transcript.message_widget(public.id).actions_["regenerate"].isVisible()
    window._regenerate_node(scene_passage.id)
    assert not window._busy and "ended" in window.statusBar().currentMessage()
    # Reached anyway, a new branch from it is refused with the text kept.
    widget = window.transcript.message_widget(scene_passage.id)
    widget.begin_edit(rewrite=True)
    widget._editor.setPlainText("Rewritten, and kept.")
    widget.commit_edit()
    widget = window.transcript.message_widget(scene_passage.id)
    assert widget.editing() and widget._editor.toPlainText() == "Rewritten, and kept."
    assert "can't start a branch" in window.statusBar().currentMessage()
    widget.cancel_edit()


def test_what_was_typed_in_a_private_scene_goes_with_it(app, window, monkeypatch):
    """A memory-only scene's unsent text was kept as the story's draft: the
    story opened again without the scene, put it back, and Enter sent it to
    the story's model. A draft from before the scene, untouched, stays."""
    window.composer.private_keep.setCurrentIndex(window.composer.private_keep.findData("memory"))
    window.composer.private_toggle.click()
    send(app, window, f"I whisper {MARKER}.")
    window.composer.input.setPlainText(f"Unsent, still private: {MARKER}")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    story_id = window.session.story.id
    window.close_story()
    window.open_story(story_id)
    assert MARKER not in window.composer.text() and story_id not in window._drafts
    window.composer.input.setPlainText("A public draft.")
    window.composer.private_keep.setCurrentIndex(window.composer.private_keep.findData("disk"))
    window.composer.private_toggle.click()
    window.composer.private_toggle.click()  # nothing written: ended at once
    assert not window.session.in_private and window.composer.text() == "A public draft."
    window.composer.private_toggle.click()
    window.composer.input.setPlainText(f"Typed in the scene: {MARKER}")
    window.composer.private_toggle.click()
    assert not window.session.in_private and window.composer.text() == ""


def test_the_scenes_draft_goes_only_once_its_summary_is_in(app, window, monkeypatch):
    """Approving the summary dropped what was typed in the scene at once,
    while "Adding the summary" still ran: when adding it failed, the scene
    was still open and its draft gone (the review after 3.1.0b8)."""
    window.composer.private_toggle.click()
    send(app, window, f"I whisper {MARKER}.")
    monkeypatch.setattr(window_private, "PrivateSummaryDialog", _Approve)
    warned: list[str] = []
    monkeypatch.setattr(window, "_warn_plain", lambda title, _text: warned.append(title))
    session = window.session

    def failing(_text):
        raise RuntimeError("HTTP 503 from the story's endpoint")
        yield

    adding = session.close_private
    monkeypatch.setattr(session, "close_private", failing)
    window.composer.input.setPlainText("The next thing I was writing in the scene")
    window.private_mock.responses = ["They spoke."]
    window.composer.private_toggle.click()  # end: the summary, approved, then adding it
    for _ in range(3):  # the summary's job, then the adding job
        wait_for(app, lambda: not window._busy)
        app.processEvents()
    assert warned == ["Adding the summary failed"]
    assert session.in_private
    assert window.composer.text() == "The next thing I was writing in the scene"

    monkeypatch.setattr(session, "close_private", adding)  # it works again
    window.private_mock.responses = ["They spoke."]
    window.composer.private_toggle.click()
    wait_for(app, lambda: not window._busy and not session.in_private)
    for _ in range(5):
        app.processEvents()
    assert window.composer.text() == ""


def test_play_as_on_a_people_card_says_why_it_waits_in_a_scene(app, window):
    """Play as … on a People card did nothing and said nothing in a private
    scene, where who is played is paused (the review after 3.1.0b8)."""
    window.right_tabs.setCurrentWidget(window.people_page)

    def play() -> None:
        app.processEvents()
        card = window.people_page.cards["char-maela"]
        dict(card.menu_actions())["Play as Maela Orr"]()
        app.processEvents()

    window.composer.private_toggle.click()
    window.statusBar().clearMessage()
    play()
    assert window.composer.held_character_id() == "char-serrik"
    assert "paused while the private scene is open" in window.statusBar().currentMessage()
    window.composer.private_toggle.click()  # nothing written: ended at once
    play()
    assert window.composer.held_character_id() == "char-maela"


def test_a_failed_summary_says_why_in_the_box_that_follows(app, window, monkeypatch):
    """The No summary box came before the failure's reason, and Try again
    started a job that cleared it: the reason was never shown."""
    window.composer.private_toggle.click()
    send(app, window, f"{MARKER} a")
    window.private_mock.error = ProviderError("upstream overloaded")
    told: list[str | None] = []
    warned: list[str] = []
    monkeypatch.setattr(window, "_warn_plain", lambda title, _text: warned.append(title))
    monkeypatch.setattr(window, "_ask_no_summary", lambda why=None: told.append(why) or "keep")
    window.composer.private_toggle.click()  # end: the summary fails
    wait_for(app, lambda: not window._busy)
    assert told and "upstream overloaded" in told[0]
    assert warned == [], "said once, in the box, not after it"


def test_archive_now_waits_for_the_private_scene(app, window):
    """The session refuses to archive in a scene ("Archiving failed")."""
    archive = window.summaries_panel.archive_button
    window.composer.private_toggle.click()
    assert not archive.isEnabled() and "private scene" in archive.toolTip()
    window.composer.private_toggle.click()  # nothing written: ended at once
    assert archive.toolTip() == ""


def test_a_memory_scene_is_asked_about_only_once_the_new_story_is_certain(
    app, window, monkeypatch, tmp_path
):
    """New… and Import… asked before anything was certain: Yes, then a guide
    cancelled at its first step or a plot file with errors, cost the scene
    for nothing. And the guide saved its story before the question, so a
    No left a story nobody had opened."""
    from PySide6.QtWidgets import QDialog, QFileDialog

    from sealedlore.gui.new_story_guide import NewStoryGuide
    from sealedlore.models.story import Story
    from sealedlore.storage.paths import stories_dir

    window.composer.private_keep.setCurrentIndex(window.composer.private_keep.findData("memory"))
    window.composer.private_toggle.click()
    send(app, window, f"I whisper {MARKER}.")
    titles: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "question", lambda _p, t, *_a, **_k: titles.append(t) or QMessageBox.Cancel
    )
    monkeypatch.setattr(NewStoryGuide, "exec", lambda guide: QDialog.Rejected)
    window.new_story()  # cancelled at its first step
    broken = tmp_path / "broken.sealedlore-plot.md"
    broken.write_text("---\ntitle: Broken\n---\n# Events\n## \n", encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(broken), ""))
    monkeypatch.setattr(QMessageBox, "exec", lambda box: QMessageBox.Ok)  # its errors, read
    window.import_file()
    assert titles == [] and window.session.in_private, "nothing new: nothing asked, nothing lost"
    stories = sorted(stories_dir(tmp_path).iterdir())
    assert not window.guide_make_story(StoryBundle(story=Story(title="Made by the guide")))
    assert titles == ["Private scene still open"] and window.session.in_private
    assert sorted(stories_dir(tmp_path).iterdir()) == stories, "a No saves nothing"


@pytest.mark.parametrize("how", ["sample", "duplicate settings", "duplicate story"])
def test_a_memory_chat_is_asked_about_once_as_something_new_opens(app, window, monkeypatch, how):
    """A sample, Duplicate settings and Duplicate entire story asked about a
    chat kept in memory only, kept it on Yes, then asked again as the new
    story opened."""
    from pathlib import Path

    import sealedlore.gui.window_chat as window_chat
    from sealedlore.gui.setup_dialog import SetupDialog
    from sealedlore.gui.start_dialog import StartDialog

    story_id = window.session.story.id

    class Dialog(window_chat.NewChatDialog):
        def exec(self):  # noqa: A003 - Qt naming
            self.title.setText("Tutor")
            self.model.setText("z-ai/glm-5.3")
            self.keep.setCurrentIndex(self.keep.findData("memory"))
            return window_chat.NewChatDialog.Accepted

    monkeypatch.setattr(window_chat, "NewChatDialog", Dialog)
    window.new_chat()
    assert window.session.memory_only
    monkeypatch.setattr(SetupDialog, "exec", lambda dialog: 0)
    monkeypatch.setattr(StartDialog, "exec", lambda dialog: 0)
    titles: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "question", lambda _p, t, *_a, **_k: titles.append(t) or QMessageBox.Yes
    )
    if how == "sample":
        sample = Path(__file__).parent.parent / "SampleStories" / "Doomsville.md"
        window.import_sample(sample)
    elif how == "duplicate settings":
        window.duplicate_story_settings(story_id)
    else:
        window.duplicate_entire_story(story_id)
    assert titles == ["Chat kept in memory only"], titles
    assert window.session is not None and not window.session.memory_only


def test_questions_that_lose_something_default_to_no(app, window, monkeypatch):
    """Enter answered Yes on boxes that lose a scene, a chat, a phone's
    pairing, every phone's address, the prompt edits, or share what is
    private: each defaults to No (or Cancel) now."""
    from sealedlore.gui.phone_dialog import PhoneDevicesDialog
    from sealedlore.gui.prompt_editor import PromptEditorDialog

    seen: dict[str, object] = {}

    def question(_parent, title, _text, buttons=None, default=None):
        seen[title] = default
        return QMessageBox.No if buttons is None or buttons & QMessageBox.No else QMessageBox.Cancel

    monkeypatch.setattr(QMessageBox, "question", question)
    window.composer.private_toggle.click()
    send(app, window, f"{MARKER} a")
    window._confirm_discard_private()
    window.session.private_provider = None
    window.end_private()  # no private model: discard instead?
    window.phone_confirm_share(window.session.story.id, True)
    phones = PhoneDevicesDialog(window)
    phones._revoke("p1", "Pixel")
    phones._new_address()
    phones.deleteLater()
    editor = PromptEditorDialog(window.config, window.session.story, parent=window)
    editor._reset_all()
    editor.done(0)
    monkeypatch.setattr(type(window), "memory_chat", property(lambda _self: True))
    window._chat_allows_close()
    assert seen == {
        "Discard the private scene": QMessageBox.No,
        "No private model": QMessageBox.No,
        "Share something private?": QMessageBox.No,
        "Revoke a phone": QMessageBox.No,
        "New address": QMessageBox.No,
        "Reset all": QMessageBox.No,
        "Chat kept in memory only": QMessageBox.Cancel,
    }
    assert window.session.in_private, "nothing was discarded"


def test_no_private_model_says_where_to_set_one(app, window: MainWindow, monkeypatch):
    window.config.private_provider = None
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    window.composer.private_toggle.click()
    assert not window.session.in_private
    assert shown and "Settings → Private" in shown[0]


def test_the_image_dialog_warns_in_a_private_scene(app, window: MainWindow):
    from sealedlore.gui.image_dialog import ImageDialog

    window.config.image_models_fetched_at = "2099-01-01T00:00:00+00:00"
    window.composer.private_toggle.click()
    dialog = ImageDialog(window.session, parent=window)
    assert not dialog.private_warning.isHidden()
    assert "not private" in dialog.private_warning.text()
    assert "saved with the story on disk" in dialog.private_warning.text()
    dialog.prompt.setPlainText("A picture.")
    assert dialog.generate_button.text().startswith("Generate anyway")
    # The private model writes the prompt; the writer field can't send the
    # scene to another one.
    assert dialog.writer.text() == window.session.open_span.model
    assert dialog.writer.edit.isReadOnly() and dialog.writer_model() is None


def test_a_private_scene_freezes_the_scene_and_plot_controls(app, window: MainWindow):
    window.begin_private()
    assert window.in_private
    for widget in (window.scene_panel, window.plot_panel, window.composer.held):
        assert not widget.isEnabled()
    assert not window.branch_bar.isEnabled()
    # Nothing was written in it: ending discards at once, and the controls return.
    window.end_private()
    assert not window.in_private
    for widget in (window.scene_panel, window.composer.held):
        assert widget.isEnabled()


def test_without_an_endpoint_the_window_never_falls_back_to_the_mock(app, tmp_path, story, cast):
    from sealedlore.providers.base import UnconfiguredProvider

    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(1))
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path)
    try:
        assert isinstance(window._build_provider(), UnconfiguredProvider)
        window.open_story(story.id)
        assert window.session is not None, "a story opens without an endpoint"
    finally:
        window.close()


def test_leaving_the_story_mid_memory_scene_asks_first(app, window: MainWindow, monkeypatch):
    window.composer.private_toggle.click()
    send(app, window, f"{MARKER} whispered")
    asked = []

    def cancel(*args, **kwargs):
        asked.append(args[1])
        return QMessageBox.Cancel

    monkeypatch.setattr(QMessageBox, "question", cancel)
    # New… asks as the guide makes its story; Import… once the file is known
    # to open another story (a scenario).
    from PySide6.QtWidgets import QDialog

    from sealedlore.gui.new_story_guide import NewStoryGuide
    from sealedlore.models.scenario import Scenario
    from sealedlore.storage.scenario import SCENARIO_SUFFIX, write_scenario

    def through_the_first_step(guide) -> int:
        guide.title.setText("From the guide")
        assert not guide._make(), "asked, and Cancel makes nothing"
        return QDialog.Rejected

    monkeypatch.setattr(NewStoryGuide, "exec", through_the_first_step)
    scenario = window.root / f"other{SCENARIO_SUFFIX}"
    write_scenario(scenario, Scenario(title="Another story"))
    monkeypatch.setattr(
        "sealedlore.gui.main_window.QFileDialog.getOpenFileName",
        lambda *a, **k: (str(scenario), ""),
    )
    window.new_story()
    window.import_file()
    window.duplicate_entire_story(window.session.story.id)
    assert len(asked) == 3 and all("Private scene" in title for title in asked)
    assert window.session.in_private


def test_a_card_imported_mid_memory_scene_leaves_the_scene_be(
    app, window: MainWindow, monkeypatch, tmp_path
):
    """A card goes into Your characters and the story stays open: nothing
    about the scene is asked, and nothing of it is lost."""
    from tests.test_tavern import png_card, v2

    window.composer.private_toggle.click()
    send(app, window, f"{MARKER} whispered")
    card = png_card(tmp_path, {"chara": v2()})
    monkeypatch.setattr(
        "sealedlore.gui.main_window.QFileDialog.getOpenFileName", lambda *a, **k: (str(card), "")
    )
    asked: list = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a) or 0)
    monkeypatch.setattr(QMessageBox, "exec", lambda box: QMessageBox.Ok)
    window.import_file()
    assert asked == [] and window.session.in_private
    assert [c.name for c in window._kept()] == ["Elena"]
    # Pictures for her wait for the scene, as the story's cards' do.
    assert window._card_pictures_refused(window._collection_session())
    window.show_collection(True, window._kept()[0].id)
    page = window.collection_page
    assert page.chat_button.isVisibleTo(page)  # her card is shown
    assert not page.add_button.isVisibleTo(page)  # but the story is paused
    window.show_collection(False)


def test_a_story_opens_speaking_as_the_character_played(app, window: MainWindow):
    # Playing Serrik: the speaker defaults to him, not to the narrator (seen
    # live: "Playing John / Speaking as Narration", and dice couldn't roll).
    assert window.composer.held_character_id() == "char-serrik"
    assert window.composer.speaker.currentData() == "char-serrik"


def test_a_tee_reply_is_marked_by_what_its_check_proved(app, window: MainWindow):
    # "verified" only where the record was for the exact text received;
    # "signed" where it couldn't be compared (providers/tee.py check_reply).
    window.composer.private_toggle.click()
    send(app, window, f"I whisper {MARKER}.")
    leaf = window.session.full_path()[-1]
    for mark, words in (
        ("verified", "TEE verified"),
        ("signed", "TEE signed, text not compared"),
        ("unchecked", "TEE ⚠ reply not verified"),
    ):
        leaf.meta.tee = mark
        window.reload_transcript()
        headers = [
            w.header.text()
            for w in window.transcript.findChildren(
                type(window.transcript.add_message("x", "y", "z"))
            )
        ]
        assert any(words in header for header in headers), mark


def test_every_tee_mark_is_one_a_saved_story_loads_again():
    # "signed" was stored by check_reply and refused by NodeMeta on loading:
    # the story's nodes.json wouldn't have read back.
    from sealedlore.gui.transcript import TEE_MARK_TIPS, TEE_MARKS
    from sealedlore.models.node import NodeMeta

    assert set(TEE_MARKS) == set(TEE_MARK_TIPS)
    for mark in TEE_MARKS:
        assert NodeMeta.model_validate_json(NodeMeta(tee=mark).model_dump_json()).tee == mark


def test_a_scene_says_what_it_pauses_until_told_not_to(app, window: MainWindow, monkeypatch):
    window.config.private_intro_seen = False
    shown: list[str] = []

    def fake_exec(box):
        shown.append(box.text())
        box.checkBox().setChecked(True)  # "Don't show this again"
        return QMessageBox.Ok

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    window.composer.private_toggle.click()
    assert shown and "Settings" in shown[0] and "End private" in shown[0]
    assert window.config.private_intro_seen
    window.session.discard_private()
    window._after_private_ended("x")
    window.composer.private_toggle.click()
    assert len(shown) == 1, "not shown again once ticked"


def test_settings_during_a_scene_says_why_instead_of_opening(app, window, monkeypatch):
    window.composer.private_toggle.click()
    told: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[2]))
    opened: list = []
    monkeypatch.setattr(main_window_module, "SettingsDialog", lambda *a, **k: opened.append(1))
    assert window.settings_action.isEnabled()
    window.settings_action.trigger()
    assert told and "End private" in told[0] and opened == []


def test_a_failed_summary_never_traps_the_author_in_the_scene(app, window, monkeypatch):
    window.composer.private_toggle.click()
    send(app, window, f"{MARKER} a")
    window.private_mock.error = ProviderError("the model took too long")
    monkeypatch.setattr(window, "_warn_plain", lambda *a: None)
    asked: list[str] = []
    monkeypatch.setattr(window, "_ask_no_summary", lambda _w=None: asked.append("asked") or "keep")
    window.composer.private_toggle.click()  # end: the summary fails
    wait_for(app, lambda: not window._busy)
    assert asked and window.session.in_private, "asked, and keep playing keeps the scene"

    monkeypatch.setattr(window, "_ask_no_summary", lambda _why=None: "discard")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    window.composer.private_toggle.click()
    wait_for(app, lambda: not window._busy)
    assert not window.session.in_private
    assert not window.composer.private_toggle.isChecked()
    assert window.stories.isEnabled()


def test_on_windows_a_private_scene_is_kept_out_of_screen_capture(app, window, monkeypatch):
    """Recall and screenshots see black while the scene is open, and the
    window again once it ends (winprivacy; a no-op off Windows)."""
    calls: list[tuple[object, bool]] = []
    monkeypatch.setattr(
        window_private, "exclude_from_capture", lambda widget, on: calls.append((widget, on))
    )
    window._sync_private_ui()
    assert calls == [], "nothing private yet"
    window.composer.private_toggle.click()
    assert calls == [(window, True)]
    window._sync_private_ui()
    assert calls == [(window, True)], "set once, not on every refresh"
    window.session.discard_private()
    window._sync_private_ui()
    assert calls == [(window, True), (window, False)]


def test_a_scene_on_a_retired_model_says_to_choose_another(app, window, monkeypatch):
    """nano-gpt retired private/glm-5-3-flash (Oct 2026). Its enclave is
    Tinfoil's router, so it would attest; the relay's list says it's gone."""
    from tests.test_private_mode import retired_setup

    provider, attested, _ = retired_setup(["private/glm-5-3"])
    window.private_provider_factory = lambda _settings: provider
    window.config.private_provider = ProviderConfig(
        name="private",
        base_url="https://nano-gpt.com/api/v1",
        api_key="k",
        model="private/glm-5-3-flash",
    )
    said: list[tuple[str, bool]] = []
    monkeypatch.setattr(window, "_say_model_retired", lambda m, chat, **_: said.append((m, chat)))
    reattest: list[str] = []
    monkeypatch.setattr(window, "_offer_reattest", lambda reason, chat: reattest.append(reason))

    window.composer.private_toggle.click()
    wait_for(app, lambda: window._tee_thread is None and said)
    assert said == [("private/glm-5-3-flash", False)] and attested == []
    assert "no longer offered" in window._private_model_text()
    assert "no longer offered" in window.session.tee_block

    # Sending says it again, never "enclave refused", and nothing is sent.
    window.composer.input.setPlainText(f"{MARKER} hello")
    window.send_turn()
    assert len(said) == 2 and reattest == []
    assert not window.session.span_nodes(window.session.open_span)

    # Nothing was written in it: ending it simply ends it.
    window._end_without_model()
    assert not window.session.in_private
    assert window._retired_model is None and window.session.tee_block is None


def test_a_scene_whose_model_was_retired_is_summarised_by_another(app, window, monkeypatch):
    """A scene with messages on a model nano-gpt has since retired: the
    author picks another as private, it is attested, and it writes the
    summary. Before, the only way out was to end with nothing carried over."""
    from sealedlore.providers.private_mode import ModelRetired
    from tests.test_private_mode import FakeEnclave, sealed_setup, sse

    window.composer.private_toggle.click()
    send(app, window, f"I whisper {MARKER}.")
    span = window.session.open_span
    span.model = "private/glm-5-3-flash"  # as begun, before nano-gpt retired it

    enclave = FakeEnclave()
    enclave.reply = sse("They spoke in private.")
    made: list[str] = []
    sealed = sealed_setup(enclave, ["private/glm-5-3", "private/kimi-k3"])
    window.private_provider_factory = lambda settings: made.append(settings.model) or sealed

    buttons: list[list[str]] = []
    picks = ["Keep it open", "Summarise with another model…"]

    def answer(box):
        buttons.append([b.text() for b in box.buttons()])
        want = picks.pop(0)
        next(b for b in box.buttons() if b.text() == want).click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", answer)
    offered: list[list[str]] = []
    monkeypatch.setattr(
        window_private.QInputDialog,
        "getItem",
        staticmethod(lambda *a: (offered.append(list(a[3])) or "private/glm-5-3", True)),
    )

    class Approve:
        APPROVE, AGAIN, BACK, DISCARD = "approve", "again", "back", "discard"

        def __init__(self, summary, parent=None, *, note=None, chat=False):
            self.choice = self.APPROVE
            self._summary = summary

        def exec(self):
            return 1

        def text(self):
            return self._summary

    monkeypatch.setattr(window_private, "PrivateSummaryDialog", Approve)

    # Found retired when the scene was resumed: keep it open for now.
    retired = ModelRetired("private/glm-5-3-flash", ["private/glm-5-3", "private/kimi-k3"])
    window._on_attested(span.id, None, [], [], [retired])
    assert "Use another model…" in buttons[0] and window.session.in_private

    # Ending it: another model writes the summary.
    window.composer.private_toggle.click()
    wait_for(app, lambda: not window._busy and not window.session.in_private)
    assert "Summarise with another model…" in buttons[1]
    assert "End without a summary…" in buttons[1]
    assert offered == [["private/glm-5-3", "private/kimi-k3"]], "only as private, still offered"
    assert made == ["private/glm-5-3"]
    assert span.model == "private/glm-5-3" and span.summary_tee == "encrypted"
    leaf = window.session.full_path()[-1]
    assert leaf.meta.private_summary_of == span.id
    assert "They spoke in private." in leaf.content
    # The scene's text went sealed to the new model, and nowhere else.
    assert MARKER in json.dumps(enclave.requests[-1])
    assert all(MARKER.encode() not in body for body in enclave.wire)
    assert window._retired_model is None


def test_a_scene_moves_only_to_a_model_as_private(app, window, monkeypatch):
    window.composer.private_toggle.click()
    window.session.open_span.model = "private/glm-5-3-flash"
    with pytest.raises(ValueError, match="isn't as private"):
        window.session.switch_private_model(provider=window.private_mock, model="TEE/glm-5.3")
    assert window.session.open_span.model == "private/glm-5-3-flash"


def test_a_resumed_scene_is_checked_on_its_own_model(app, window, monkeypatch):
    """Every request in a scene names span.model: resuming one must attest
    and check that model, not the one Settings → Private names now."""
    window.composer.private_toggle.click()
    window.session.open_span.model = "private/glm-5-3-flash"
    window.config.private_provider = ProviderConfig(
        name="private", base_url="https://nano-gpt.com/api/v1", api_key="k", model="private/kimi-k3"
    )
    made: list[str] = []
    window.private_provider_factory = lambda settings: (
        made.append(settings.model) or window.private_mock
    )
    attested: list[str] = []
    monkeypatch.setattr(
        window, "_attest_or_say_not", lambda settings: attested.append(settings.model)
    )
    window._resume_private()
    assert made == attested == ["private/glm-5-3-flash"]
