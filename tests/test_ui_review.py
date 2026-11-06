"""What a look over the window before 3.0.0b2 found, held: nothing typed is
lost without a question, Enter in a field never saves a whole dialog, the
keyboard reaches what it should, and what the plot hides stays hidden."""

# ruff: noqa: F811 - the window's fixtures come from test_ui_tidy

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from sealedlore.gui.prompt_editor import PromptEditorDialog  # noqa: E402
from sealedlore.gui.setup_dialog import SetupDialog  # noqa: E402
from sealedlore.gui.transcript import MessageWidget  # noqa: E402
from sealedlore.models.tavern import TavernFields  # noqa: E402
from tests.test_ui_tidy import app, window  # noqa: E402, F401


def asked(monkeypatch, answer) -> list[str]:
    """Answers every question with `answer`, keeping their titles."""
    titles: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda _parent, title, *_a, **_k: titles.append(title) or answer,
    )
    return titles


def open_edit(app, window):
    widget = window.transcript.message_widget("u1")
    assert isinstance(widget, MessageWidget)
    widget.begin_edit()
    app.processEvents()
    widget._editor.setFocus()
    app.processEvents()
    return widget


def test_ctrl_enter_in_a_message_being_edited_saves_it_and_sends_nothing(app, window):
    window.composer.input.setPlainText("A turn waiting in the box.")
    widget = open_edit(app, window)
    widget._editor.setPlainText("The turn, changed.")
    before = len(window.session.nodes)
    QApplication.focusWidget().setFocus()
    window.send_turn()  # what Ctrl+Enter runs
    app.processEvents()
    node = next(n for n in window.session.nodes if n.id == "u1")
    assert node.content == "The turn, changed." and len(window.session.nodes) == before
    assert window.composer.text() == "A turn waiting in the box.", "the draft stays unsent"


def test_esc_in_a_changed_edit_asks_and_an_unchanged_one_just_closes(app, window, monkeypatch):
    titles = asked(monkeypatch, QMessageBox.Cancel)
    widget = open_edit(app, window)
    window._on_escape()
    assert not widget.editing() and titles == [], "nothing changed: closed, nothing asked"
    widget = open_edit(app, window)
    widget._editor.setPlainText("Typed and not saved.")
    window._on_escape()
    assert widget.editing() and titles == ["Discard this edit?"], "Cancel keeps it"


def test_leaving_the_story_with_an_edit_open_asks_first(app, window, monkeypatch):
    titles = asked(monkeypatch, QMessageBox.Cancel)
    widget = open_edit(app, window)
    widget._editor.setPlainText("Typed and not saved.")
    story_id = window.session.story.id
    window.close_story()
    assert window.session is not None and window.session.story.id == story_id
    assert titles == ["Discard your edit?"] and widget.editing()


def test_removing_a_card_asks_and_defaults_to_no(app, window, monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda _p, title, _t, buttons=None, default=None: (
            seen.append((title, default)) or QMessageBox.No
        ),
    )
    panel = window.cast_panel
    before = len(window.session.cast)
    panel._select(window.session.cast[0].id)
    panel._remove_character()
    assert len(window.session.cast) == before
    assert seen and seen[0] == ("Remove this character?", QMessageBox.No)


def test_removing_a_card_that_brought_lore_says_the_lore_stays(app, window, monkeypatch):
    """The entries a card's lorebook made aren't taken out with the card
    (nothing says which they were): the question says so, and only for a
    card that had a lorebook."""
    texts: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda _p, _title, text, *_a, **_k: texts.append(text) or QMessageBox.No,
    )
    panel = window.cast_panel
    character = window.session.cast[0]
    panel._select(character.id)
    panel._remove_character()
    assert "Lore page" not in texts[-1], "a card of our own brought no lore"
    character.tavern = TavernFields(character_book={"entries": [{"keys": ["x"], "content": "y"}]})
    panel._select(character.id)
    panel._remove_character()
    assert "lore that came in with their card stays" in texts[-1]
    assert "Lore page" in texts[-1]


def test_enter_in_the_prompt_editors_search_saves_nothing(app, window, monkeypatch):
    dialog = PromptEditorDialog(window.config, window.session.story, parent=window)
    dialog.show()
    dialog.search.setFocus()
    dialog.search.setText("remember")
    for kind in (QKeyEvent.KeyPress, QKeyEvent.KeyRelease):
        QApplication.sendEvent(dialog.search, QKeyEvent(kind, Qt.Key_Return, Qt.NoModifier))
    app.processEvents()
    assert dialog.isVisible() and dialog.result() == 0, "still open, nothing saved"
    # Cancel with nothing changed closes at once; with a change, it asks.
    titles = asked(monkeypatch, QMessageBox.Cancel)
    key = next(iter(dialog.edits["app"]), None) or "rules.length_line"
    dialog.edits["app"][key] = dialog.edits["app"].get(key) or _edit_of(key)
    dialog.reject()
    assert dialog.isVisible() and titles == ["Discard your changes?"]
    dialog.edits["app"].pop(key)
    dialog.reject()
    assert not dialog.isVisible()


def _edit_of(key: str):
    from sealedlore.models.prompt_edit import PromptEdit

    return PromptEdit(text="Something else entirely.")


def test_setup_asks_before_cancel_loses_an_edit_and_enter_never_saves(app, window, monkeypatch):
    dialog = SetupDialog(window.session.story, window.session.cast, parent=window)
    dialog.show()
    for _ in range(3):
        app.processEvents()
    dialog.title.setFocus()
    for kind in (QKeyEvent.KeyPress, QKeyEvent.KeyRelease):
        QApplication.sendEvent(dialog.title, QKeyEvent(kind, Qt.Key_Return, Qt.NoModifier))
    app.processEvents()
    assert dialog.isVisible(), "Enter in the title doesn't save and close Setup"
    titles = asked(monkeypatch, QMessageBox.Cancel)
    dialog.title.setText("A new title")
    dialog.reject()
    assert dialog.isVisible() and titles == ["Discard your changes?"]
    dialog.title.setText(window.session.story.title)
    dialog.close()


def test_a_plot_hidden_character_is_offered_nowhere_before_they_come(app, window):
    session = window.session
    hidden = session.cast[-1]
    dialog = SetupDialog(session.story, session.cast, parent=window, hidden=frozenset({hidden.id}))
    assert hidden not in dialog.cast and session.cast[0] in dialog.cast
    dialog.close()


# --- the review's last items (after 3.0.0b2) -------------------------------------------


def test_the_changes_dialog_marks_the_words_that_differ():
    from sealedlore.gui.card_changes import marked

    pieces = marked(
        "She trusts nobody, and keeps the farm.", "She trusts John slowly, and keeps the farm."
    )
    assert pieces == [
        ("She trusts ", False),
        ("nobody,", True),
        (" and keeps the farm.", False),
    ]
    assert marked("Same words.", "Same words.") == [("Same words.", False)]


def test_the_changes_dialog_marks_the_right_words_and_quickly(app):
    """Marks went at Python's offsets into a document that counts UTF-16 (an
    emoji is two) and reads "\\r\\n" as one break: after either, every mark
    was on the wrong words. And the diff, over every word with no junk,
    took 1-2 seconds a side for a 3,000-word description."""
    import random
    import time

    from PySide6.QtGui import QTextFormat
    from PySide6.QtWidgets import QPlainTextEdit

    from sealedlore.gui.card_changes import _mark, marked, marked_both

    def shaded(mine: str, other: str) -> list[str]:
        box = QPlainTextEdit()
        box.setPlainText(mine)
        _mark(box, marked(mine, other))
        found, block = [], box.document().begin()
        while block.isValid():
            fragments = block.begin()
            while not fragments.atEnd():
                fragment = fragments.fragment()
                if fragment.charFormat().hasProperty(QTextFormat.BackgroundBrush):
                    found.append(fragment.text())
                fragments += 1
            block = block.next()
        assert box.toPlainText() == QPlainTextEdit(mine).toPlainText(), "the text as it was"
        return found

    for before in ("", "🔥 ", "Line one.\r\n"):
        mine = f"{before}She trusts nobody and keeps the farm."
        assert shaded(mine, f"{before}She trusts John and keeps the farm.") == ["nobody"]
    random.seed(3)
    words = ["the", "farm", "she", "keeps", "trusts", "John", "winter", "and", "a", "road."]
    text = " ".join(random.choice(words) for _ in range(6000))
    added = text[: len(text) // 2] + " A new sentence about the creek." + text[len(text) // 2 :]
    started = time.perf_counter()
    ours, theirs = marked_both(text, added)
    rewritten = marked_both(text, " ".join(random.choice(words) for _ in range(6000)))
    assert time.perf_counter() - started < 1.0, "quick at 6,000 words"
    assert "".join(p for p, _c in ours) == text and "".join(p for p, _c in theirs) == added
    assert any("creek" in piece for piece, changed in theirs if changed)
    assert not any(changed for _p, changed in ours[:1]), "the shared start is not marked"
    assert any(changed for _p, changed in rewritten[0])


# --- the window's second look (Nov 2026): what a dialog holds -------------------------


def settle(app, rounds: int = 5) -> None:
    for _ in range(rounds):
        app.processEvents()


def press_enter(widget) -> None:
    for kind in (QKeyEvent.KeyPress, QKeyEvent.KeyRelease):
        QApplication.sendEvent(widget, QKeyEvent(kind, Qt.Key_Return, Qt.NoModifier))


def test_a_new_chat_asks_on_cancel_once_something_is_typed(app, monkeypatch):
    """`typed_texts` gave findChildren a union of types, which PySide6
    refuses: New simple chat and Chat with them… never asked."""
    from sealedlore.gui.card_chat_dialog import CardChatDialog
    from sealedlore.gui.new_chat_dialog import NewChatDialog
    from sealedlore.models.character import Character

    titles = asked(monkeypatch, QMessageBox.Cancel)
    untouched = NewChatDialog(model="zai-org/glm-5.3")
    untouched.show()
    settle(app)
    untouched.reject()
    assert not untouched.isVisible() and titles == [], "nothing typed: closed, nothing asked"
    typed = NewChatDialog(model="zai-org/glm-5.3")
    typed.show()
    settle(app)
    typed.prompt.insertPlainText("You are a terse assistant.")
    typed.reject()
    assert typed.isVisible() and titles == ["Discard what you wrote?"], "Cancel keeps it"
    typed.done(0)
    card = Character(id="c1", name="Jane Moss", summary="A farmer.", description="Keeps a farm.")
    chat = CardChatDialog(card, model="zai-org/glm-5.3")
    chat.show()
    settle(app)
    chat.reject()
    assert not chat.isVisible() and titles == ["Discard what you wrote?"]


@pytest.mark.parametrize("which", ["setup", "prompts"])
def test_enter_presses_only_the_push_button_it_is_on(app, window, which):
    """The button box made Save the default again as the dialog showed, and
    Enter anywhere on a button was handed on to it: Enter on Cancel, a check
    box, a radio button or Browse… saved the dialog and closed it."""
    from PySide6.QtWidgets import QCheckBox, QDialogButtonBox, QRadioButton, QToolButton

    def make():
        if which == "setup":
            return SetupDialog(window.session.story, window.session.cast, parent=window)
        return PromptEditorDialog(window.config, window.session.story, parent=window)

    dialog = make()
    dialog.show()
    settle(app)
    pages = dialog.tabs.count() if which == "setup" else 1
    tried = []
    for index in range(pages):
        if which == "setup":
            dialog.tabs.setCurrentIndex(index)
            settle(app)
        for kind in (QCheckBox, QRadioButton, QToolButton):
            found = [
                b
                for b in dialog.findChildren(kind)
                if b.isVisible() and b.isEnabled() and b.focusPolicy() != Qt.NoFocus
            ]
            for button in found[:1]:
                button.setFocus()
                settle(app)
                if QApplication.focusWidget() is not button:
                    continue
                press_enter(button)
                settle(app)
                tried.append(kind)
                assert dialog.isVisible() and dialog.result() == 0, f"Enter on a {kind.__name__}"
    # The prompt editor has no button but push buttons to put the focus on.
    assert tried or which == "prompts", "something besides a push button had the focus"
    cancel = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Cancel)
    cancel.setFocus()
    settle(app)
    assert QApplication.focusWidget() is cancel
    press_enter(cancel)
    settle(app)
    assert not dialog.isVisible() and dialog.result() == 0, "Enter on Cancel cancels"
    saved = make()
    saved.show()
    settle(app)
    save = saved.findChild(QDialogButtonBox).button(QDialogButtonBox.Save)
    save.setFocus()
    settle(app)
    press_enter(save)
    settle(app)
    assert not saved.isVisible() and saved.result() == 1, "Enter on Save saves"


def test_setup_asks_about_changes_no_typing_made(app, window, monkeypatch):
    """Setup watched typing and choosing only: From a picture…, Style's Edit
    text (read-only when the watch ran), a model chosen with Recommended or
    Browse…, and Use the models in Settings were dropped by Cancel without a
    word. It compares what Save would write with what it would have written
    as it opened."""
    from sealedlore.gui.setup_dialog import ModelChoices

    session = window.session
    session.story.defaults.scene_model = "some/own-model"
    titles = asked(monkeypatch, QMessageBox.Cancel)

    def opened() -> SetupDialog:
        dialog = SetupDialog(
            session.story,
            session.cast,
            parent=window,
            models=ModelChoices(window.config),
            describe_style=lambda: ("sepia ink, cross-hatched", b"\x89PNG not really", ".png"),
        )
        dialog.show()
        settle(app)
        return dialog

    untouched = opened()
    untouched.reject()
    assert not untouched.isVisible() and titles == [], "nothing changed: closed, nothing asked"

    def edit_style_text(dialog: SetupDialog) -> None:
        dialog.style_panel.detach_button.click()
        dialog.style_panel.preview.appendPlainText("Mine.")

    for change in (
        lambda d: d.picture_style.from_picture.click(),
        edit_style_text,
        lambda d: d.model_fields["story"]._choose_recommended("anthropic/claude-opus-5.5"),
        lambda d: d.reset_models.click(),
    ):
        dialog = opened()
        change(dialog)
        settle(app)
        before = len(titles)
        dialog.reject()
        assert dialog.isVisible() and titles[before:] == ["Discard your changes?"]
        dialog.done(0)
    assert session.story.defaults.scene_model == "some/own-model", "Cancel wrote nothing"


def test_setup_sees_a_change_on_a_starting_scene_built_again(app, window, monkeypatch):
    """The watch kept the widgets it had seen by id(), which Python gives
    again to new ones: rebuilt twice for a changed cast, the Starting scene
    tab's fields could be skipped, and Cancel dropped a change made there."""
    from PySide6.QtWidgets import QComboBox, QWidget

    from sealedlore.gui.start_presence import StartPresence
    from sealedlore.models.character import Character

    session = window.session
    titles = asked(monkeypatch, QMessageBox.Cancel)
    dialog = SetupDialog(session.story, session.cast, parent=window, characters=QWidget())
    dialog.show()
    settle(app)
    scene = dialog.tabs.indexOf(dialog.scene_tab)
    for round_ in range(3):
        dialog.tabs.setCurrentIndex(0)
        settle(app)
        session.cast.append(Character(id=f"char-new{round_}", name=f"Nadia Venn {round_}"))
        dialog.tabs.setCurrentIndex(scene)
        settle(app)
    choice = dialog.scene_tab.findChild(StartPresence).findChildren(QComboBox)[-1]
    choice.setCurrentIndex(1)  # the newest, pinned present
    dialog.reject()
    assert dialog.isVisible() and titles == ["Discard your changes?"]
    dialog.done(0)


def test_what_the_plot_hides_keeps_its_place_through_scene_setup_and_start(app, window):
    """The Scene page rewrote the roster from the rows it shows, and Setup's
    Save and Start wrote the start's pins from theirs: a plot-hidden
    character on the card, or pinned at the start, was dropped by any edit."""
    from sealedlore.gui.start_dialog import StartDialog
    from sealedlore.models.scene import OffstageCharacter

    session = window.session
    hidden, nearby = session.cast[-1], session.cast[-2]
    scene = session.story.scene
    scene.present_character_ids = [session.cast[0].id, hidden.id]
    scene.offstage_but_nearby = [OffstageCharacter(character_id=nearby.id, note="on the stair")]
    window.scene_panel.set_scene(scene, [c for c in session.cast if c.id != nearby.id][:-1])
    window.scene_panel.location.setText("The undercroft, by the north stair")
    window.scene_panel._apply_edits()  # what an edit on the page runs
    assert hidden.id in scene.present_character_ids
    assert [e.character_id for e in scene.offstage_but_nearby] == [nearby.id]
    session.story.setup.absent_at_start = [hidden.id]
    setup = SetupDialog(session.story, session.cast, parent=window, hidden=frozenset({hidden.id}))
    setup._save()  # only Save, nothing changed
    assert session.story.setup.absent_at_start == [hidden.id]
    start = StartDialog(session.story, session.cast[:-1], parent=window)
    present, absent = start.pinned()
    assert hidden.id in absent and hidden.id not in present
    start.done(0)


def test_settings_asks_once_a_key_is_typed_and_not_after_a_look_round(app, monkeypatch):
    """Esc after pasting an API key lost it without a word. Choosing another
    endpoint, which fills the fields with its own, changes nothing."""
    from sealedlore.gui.settings_dialog import SettingsDialog
    from sealedlore.models.config import Config, ProviderConfig

    def config() -> Config:
        return Config(
            providers=[
                ProviderConfig(name="NanoGPT", base_url="https://nano-gpt.com/api/v1", api_key="k"),
                ProviderConfig(name="Other", base_url="https://api.example.com/v1", api_key="o"),
            ],
            active_provider_name="NanoGPT",
        )

    titles = asked(monkeypatch, QMessageBox.Cancel)
    looked = SettingsDialog(config(), None)
    looked.show()
    settle(app)
    looked.endpoint_list.setCurrentRow(1)
    for index in range(looked.tabs.count()):
        looked.tabs.setCurrentIndex(index)
    settle(app)
    looked.reject()
    assert not looked.isVisible() and titles == [], "only looked: closed, nothing asked"
    typed = SettingsDialog(config(), None)
    typed.show()
    settle(app)
    typed.api_key.setText("sk-a-long-key-pasted-in")
    typed.endpoint_list.setCurrentRow(1)  # the key is kept on its endpoint's copy
    typed.reject()
    assert typed.isVisible() and titles == ["Discard your changes?"]
    typed.done(0)


def test_an_edited_private_summary_is_asked_about_before_it_is_lost(app, monkeypatch):
    """Esc (Back to the scene) and Write it again dropped an edited summary."""
    from PySide6.QtWidgets import QPushButton

    from sealedlore.gui.private_dialogs import PrivateSummaryDialog

    titles = asked(monkeypatch, QMessageBox.Cancel)
    dialog = PrivateSummaryDialog("They talked until dawn.")
    dialog.reject()
    assert dialog.choice == dialog.BACK and titles == [], "unchanged: back at once"
    dialog = PrivateSummaryDialog("They talked until dawn.")
    dialog.show()
    dialog.editor.setPlainText("They talked until dawn, and Maela heard none of it.")
    dialog.reject()
    assert dialog.isVisible() and titles == ["Discard what you wrote?"]
    again = next(b for b in dialog.findChildren(QPushButton) if b.text() == "Write it again")
    again.click()
    assert dialog.isVisible() and dialog.choice == dialog.BACK and len(titles) == 2
    dialog.approve.click()
    assert dialog.choice == dialog.APPROVE and len(titles) == 2, "approving loses nothing"


def test_a_premise_and_a_review_complaint_are_asked_about(app, monkeypatch):
    from sealedlore.engine.session import ReviewSizes
    from sealedlore.gui.generate_dialog import GenerateDialog
    from sealedlore.gui.review_dialog import ReviewRequestDialog
    from sealedlore.providers.mock import MockChatProvider

    titles = asked(monkeypatch, QMessageBox.Cancel)
    draft = GenerateDialog(MockChatProvider(["{}"]), model="", default_model="m")
    draft.reject()
    assert titles == [], "nothing typed: closed"
    draft = GenerateDialog(MockChatProvider(["{}"]), model="", default_model="m")
    draft.show()
    draft.premise.setPlainText("A lighthouse keeper and the thing in the fog.")
    draft.reject()
    assert draft.isVisible() and titles == ["Discard what you wrote?"]
    draft.done(0)
    sizes = ReviewSizes(
        settings=1000,
        system_prompt=2000,
        tail=500,
        whole_prompt=9000,
        exchanges=(300,),
        in_prompt=1,
        marked=(),
    )
    review = ReviewRequestDialog(sizes=sizes, model="", default_model="m", last_complaint="Long.")
    review.show()
    review.reject()
    assert not review.isVisible() and len(titles) == 1, "the last complaint, as it came"
    review = ReviewRequestDialog(sizes=sizes, model="", default_model="m")
    review.show()
    review.complaint.setPlainText("Everyone agrees with everything I suggest.")
    review.reject()
    assert review.isVisible() and titles[1:] == ["Discard what you wrote?"]
    review.done(0)


def test_a_videos_direction_is_asked_about_and_the_writers_prompt_is_not(app, window, monkeypatch):
    from sealedlore.gui.video_dialog import VideoDialog

    titles = asked(monkeypatch, QMessageBox.Cancel)
    dialog = VideoDialog(window.session, parent=window)
    dialog._written = "Snow drifts past the porch light."
    dialog._on_written()  # as the prompt writer's answer lands
    dialog.reject()
    assert titles == [], "the writer's prompt is not the author's typing"
    dialog = VideoDialog(window.session, parent=window)
    dialog.show()
    dialog.direction.setPlainText("Slow, from the porch.")
    dialog.reject()
    assert dialog.isVisible() and titles == ["Discard what you wrote?"]
    dialog.done(0)


# --- the window's second look: a message's edit -----------------------------------------


def changed_edit(app, window, node_id: str = "u0", text: str = "Typed and not saved."):
    widget = window.transcript.message_widget(node_id)
    assert isinstance(widget, MessageWidget)
    widget.begin_edit()
    settle(app)
    widget._editor.setPlainText(text)
    return widget


def test_every_changed_edit_is_asked_about_not_the_first_only(app, window, monkeypatch):
    titles = asked(monkeypatch, QMessageBox.Cancel)
    untouched = window.transcript.message_widget("u0")
    untouched.begin_edit()  # opened and left as it was
    changed_edit(app, window, "u1", "A long rewrite of turn one.")
    story_id = window.session.story.id
    window.close_story()
    assert titles == ["Discard your edit?"], "the second editor's change counts"
    assert window.session is not None and window.session.story.id == story_id
    assert len(window.transcript.open_editors()) == 2


def test_an_editor_left_alone_is_no_change_and_saving_it_writes_nothing(app, window, monkeypatch):
    """A plain text box gives a no-break space back as a space: an editor
    opened and left alone already "differed", so Esc asked about an edit
    nobody made and Save wrote the passage again, marking its chapter out of
    date for nothing."""
    titles = asked(monkeypatch, QMessageBox.Cancel)
    node = next(n for n in window.session.nodes if n.id == "a1")
    node.content = "« Bonjour », dit-elle. Ten km to the keep. Then rain."
    window.reload_transcript()
    settle(app)
    edited: list[str] = []
    monkeypatch.setattr(window.session, "edit_node", lambda *a, **k: edited.append(a[0]) or [])
    widget = window.transcript.message_widget("a1")
    widget.begin_edit()
    settle(app)
    assert not widget.edit_changed()
    window._on_escape()
    assert titles == [] and not widget.editing(), "Esc closes it, nothing asked"
    for save in ("button", "ctrl+enter"):
        widget = window.transcript.message_widget("a1")
        widget.begin_edit()
        settle(app)
        if save == "button":
            widget._save_button.click()
        else:
            widget._editor.setFocus()
            settle(app)
            window.send_turn()  # what Ctrl+Enter runs
        settle(app)
        assert not widget.editing() and edited == [], f"saved unchanged by {save}: nothing sent"
    assert " " in node.content
    widget = window.transcript.message_widget("a1")
    widget.begin_edit(rewrite=True)
    settle(app)
    assert not widget.edit_changed()
    widget._branch_name.setText("Another way")
    assert widget.edit_changed(), "a branch's name typed is something to lose"
    widget.cancel_edit()


def test_rebuilds_open_an_edit_again_with_what_was_typed(app, window, monkeypatch):
    """A rebuild for anything else destroyed the editor mid-sentence: the
    view toggle, dice shown, the text size, another message saved."""
    titles = asked(monkeypatch, QMessageBox.Cancel)
    widget = changed_edit(app, window, "u1", "Typed and not saved.")
    widget._editor.setFocus()
    settle(app)

    def still_open() -> str | None:
        editor = window.transcript.open_editor()
        return editor._editor.toPlainText() if editor is not None else None

    window.set_show_dice_rolls(not window.config.show_dice_rolls)
    assert still_open() == "Typed and not saved."
    assert window.transcript.open_editor(focused=True) is not None, "the keyboard stays in it"
    try:
        window.set_text_scale(1.1)
        assert still_open() == "Typed and not saved."
    finally:
        window.set_text_scale(1.0)
    window.set_storyteller_view(True)  # u1 is still sent word for word
    assert still_open() == "Typed and not saved."
    window.set_storyteller_view(False)
    other = window.transcript.message_widget("a0")
    other.begin_edit()
    settle(app)
    other._editor.setPlainText("The world answers, changed.")
    other._save_button.click()
    settle(app)
    assert next(n for n in window.session.nodes if n.id == "a0").content.endswith("changed.")
    assert still_open() == "Typed and not saved." and titles == []


def test_removing_a_message_being_edited_asks_and_the_rest_stay_open(app, window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox as Box

    titles = asked(monkeypatch, QMessageBox.Cancel)

    def answer(label: str):
        def press(box) -> int:
            next(b for b in box.buttons() if b.text() == label).click()
            return 0

        return press

    monkeypatch.setattr(Box, "exec", answer("Delete"))
    changed_edit(app, window, "u1")
    before = len(window.session.nodes)
    window._delete_node("a0")  # takes u1 with it: asked, and Cancel keeps all
    assert titles == ["Discard your edit?"] and len(window.session.nodes) == before
    assert window.transcript.open_editor() is not None
    window._delete_node("a1")  # after u1: no question, and the edit stays open
    assert titles == ["Discard your edit?"] and len(window.session.nodes) == before - 1
    assert window.transcript.open_editor()._editor.toPlainText() == "Typed and not saved."


def test_retaking_an_earlier_passage_asks_about_an_edit_after_it(app, window, monkeypatch):
    """The story up to the passage is shown while its new take streams in:
    an edit open on a later message went with the rest."""
    titles = asked(monkeypatch, QMessageBox.Cancel)
    changed_edit(app, window, "u1", "Typed and not saved.")
    window._regenerate_node("a0")
    assert titles == ["Discard your edit?"] and not window._busy
    assert window.transcript.open_editor()._editor.toPlainText() == "Typed and not saved."


def test_the_window_keeps_an_edit_far_back_built(app, tmp_path, story, cast, monkeypatch):
    """Going to a chapter or a Find match far off, or sending a turn while
    reading back, dropped the page with the editor, and its text with it."""
    from sealedlore.gui.main_window import MainWindow
    from sealedlore.storage.repository import StoryBundle, save_story_bundle
    from tests.conftest import make_exchange

    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(60))
    bundle.story.active_leaf_id = "a59"
    bundle.story.held_character_id = "char-serrik"
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.resize(1366, 768)
    window.show()
    window.open_story(story.id)
    try:
        window.transcript.scroll_to_node("u2")
        settle(app)
        changed_edit(app, window, "u2", "Turn two, rewritten and not saved.")
        window.transcript.scroll_to_node("a58")  # far off
        settle(app)
        assert window.transcript.open_editor()._editor.toPlainText().startswith("Turn two")
        window.transcript.jump_to_start()
        window.transcript.ensure_at_latest()
        settle(app)
        assert window.transcript.open_editor().node_id == "u2"
        assert window.transcript.message_widget("a59") is not None
    finally:
        window.transcript.open_editor().cancel_edit()
        window.close()


# --- the window's second look: closing while a job runs ---------------------------------


def wait_for(app, condition, timeout: float = 20.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        app.processEvents()
        time.sleep(0.005)


def test_a_close_cancelled_during_a_job_stops_nothing_and_closes_nothing_later(
    app, window, monkeypatch
):
    """Quit while a passage streamed stopped it first, then asked about the
    edit; answered Cancel, the window stayed half finished (still streaming,
    never rebuilt), and closed by itself when the next job ended."""
    from sealedlore.providers.mock import MockChatProvider

    long = "A long passage. " * 30
    window.config.scene_reads = "manual"
    window.config.suggest_characters = False
    window._provider = MockChatProvider([long, "Another passage."])
    window._provider.delay = 0.01
    titles = asked(monkeypatch, QMessageBox.Cancel)
    changed_edit(app, window, "u0", "An edit I mean to keep.")
    window.composer.select_speaker("char-serrik")
    window.composer.input.setPlainText("I go up the stairs.")
    window.composer.input.setFocus()
    window.send_turn()
    wait_for(app, lambda: window._busy)
    window.close()  # Ctrl+Q while it streams
    assert titles == ["Discard your edit?"] and window.isVisible()
    assert not window._close_when_idle and window._busy, "the passage goes on"
    wait_for(app, lambda: not window._busy)
    settle(app, 20)
    assert window.session.path()[-1].content == long, "written to the end, never stopped"
    assert window.transcript._streaming is None and window.composer.send_button.isEnabled()
    window.transcript.open_editor().cancel_edit()
    window.composer.input.setPlainText("I wait.")
    window.composer.input.setFocus()
    window.send_turn()
    wait_for(app, lambda: not window._busy)
    settle(app, 20)
    assert window.isVisible(), "the next job's end closes nothing"
    assert titles == ["Discard your edit?"]
