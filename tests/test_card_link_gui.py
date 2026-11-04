"""The author's own characters in stories and chats (gui/window_card_link.py,
gui/window_card_start.py): put into a story with their pictures, a story's
changes shown and saved to the card, made a version, kept or taken back; a
chat started from a card; a story drafted around one; saved system prompts."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QInputDialog, QMessageBox  # noqa: E402

from sealedlore.engine.authoring import build_generation_messages  # noqa: E402
from sealedlore.engine.drafting import StoryDraft  # noqa: E402
from sealedlore.gui import window_card_link, window_card_start  # noqa: E402
from sealedlore.gui.card_changes import KEEP, SAVE, TAKE, VERSION, CardChangesDialog  # noqa: E402
from sealedlore.gui.card_chat_dialog import NO_GREETING, CardChatDialog  # noqa: E402
from sealedlore.gui.collection_picker import CollectionPicker  # noqa: E402
from sealedlore.gui.generate_dialog import GenerateDialog  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.saved_prompts import SavedPrompts, SavedPromptsButton  # noqa: E402
from sealedlore.models.character import Character  # noqa: E402
from sealedlore.models.generation import ReasoningConfig  # noqa: E402
from sealedlore.models.image import ImageRef  # noqa: E402
from sealedlore.models.story import Story  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage import collection  # noqa: E402
from sealedlore.storage.images import REFS_DIR  # noqa: E402
from sealedlore.storage.paths import COLLECTION_ID  # noqa: E402
from sealedlore.storage.picture_store import DiskPictures  # noqa: E402
from sealedlore.storage.repository import (  # noqa: E402
    StoryBundle,
    load_story_bundle,
    save_story_bundle,
)
from sealedlore.storage.tavern_cards import read_card  # noqa: E402
from tests.test_tavern import ELENA, png_card, tiny_png, v2  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def keep_elena(root: Path, tmp_path: Path) -> Character:
    """Elena kept in Your characters, from the test card, before any window."""
    bundle = collection.load_collection(root)
    store = DiskPictures(COLLECTION_ID, root)
    elena, _notes = collection.kept_from_card(read_card(png_card(tmp_path, {"chara": v2()})), store)
    bundle.cast.append(elena)
    save_story_bundle(bundle, root=root)
    return elena


@pytest.fixture
def window(app, tmp_path: Path):
    root = tmp_path / "data"
    keep_elena(root, tmp_path)
    john = Character(id="john", name="John")
    story = Story(id="story-1", title="Doomsville", held_character_id="john")
    story.scene.present_character_ids = ["john"]
    save_story_bundle(StoryBundle(story=story, cast=[john]), root=root)
    window = MainWindow(root=root, use_mock=True)
    window.open_story("story-1")
    yield window
    # A memory-only chat left open asks before it is lost: not at teardown.
    window._private_allows_close = lambda: True
    window.close()


def choose_elena(monkeypatch, *, to_cast: bool = True) -> None:
    def exec_(picker: CollectionPicker) -> int:
        picker.list.item(0).setCheckState(picker.list.item(0).checkState().Checked)
        (picker.to_cast if to_cast else picker.to_supporting).setChecked(True)
        return QDialog.Accepted

    monkeypatch.setattr(CollectionPicker, "exec", exec_)


def added_elena(window, monkeypatch) -> Character:
    choose_elena(monkeypatch)
    window.cast_panel.collection_button.click()
    return next(c for c in window.session.cast if c.name == "Elena")


def answer_changes(monkeypatch, choice: str, untick: tuple[str, ...] = ()) -> list:
    seen: list = []

    def exec_(dialog: CardChangesDialog) -> int:
        seen.append([change.key for change in dialog._found])
        for section in dialog._ticks.values():
            for tick, change in section:
                if change.key in untick:
                    tick.setChecked(False)
        dialog.choice = choice
        return QDialog.Accepted

    monkeypatch.setattr(CardChangesDialog, "exec", exec_)
    return seen


# --- into a story ------------------------------------------------------------------------


def test_a_kept_character_joins_the_story_with_the_names_in(app, window, monkeypatch):
    elena = added_elena(window, monkeypatch)
    session = window.session
    assert elena.linked is not None and elena.linked.user == "John"
    assert "She trusts John slowly." in elena.full_description
    assert elena.voice_notes.startswith("John: Hello?")
    # Her picture is the story's own file now.
    portrait = elena.reference_images[0]
    assert session.pictures.read(portrait.file) is not None
    assert (window.root / "stories" / "story-1" / portrait.file).is_file()
    # Her card's own lorebook is the story's lore.
    assert [entry.title for entry in session.bundle.lore] == ["Hellsville"]
    assert window.cast_panel.current is elena
    assert "In step with their card" in window.cast_panel.link_words.text()
    assert window.cast_panel.link_button.text() == window_card_link.CARD_LABEL


def test_a_storys_change_goes_to_the_card_as_the_card_writes_it(app, window, monkeypatch):
    elena = added_elena(window, monkeypatch)
    elena.full_description = elena.full_description.replace("slowly", "now")
    window._sync_card_link()
    assert "Changed in this story: description." in window.cast_panel.link_words.text()
    seen = answer_changes(monkeypatch, SAVE)
    window.cast_panel.link_button.click()
    assert seen == [["full_description"]]
    card = window._kept_card(elena.linked.card_id)
    assert card.full_description.startswith("{{char}} keeps the farmhouse")
    assert "She trusts {{user}} now." in card.full_description
    kept = collection.load_collection(window.root).cast[0]
    assert "{{user}} now" in kept.full_description
    assert "In step with their card" in window.cast_panel.link_words.text()


def test_a_new_picture_in_the_story_goes_onto_the_card(app, window, monkeypatch):
    elena = added_elena(window, monkeypatch)
    window.session.pictures.write(f"{REFS_DIR}/scar.png", tiny_png() + b"scar")
    elena.reference_images.append(
        ImageRef(id="scar", file=f"{REFS_DIR}/scar.png", look="coat", expression="angry")
    )
    window._sync_card_link()
    assert "1 picture" in window.cast_panel.link_words.text()
    answer_changes(monkeypatch, SAVE)
    window.cast_panel.link_button.click()
    card = window._kept_card(elena.linked.card_id)
    added = card.reference_images[-1]
    assert added.expression == "angry"
    assert window._collection_pictures().read(added.file) == tiny_png() + b"scar"


def test_a_storys_changes_can_be_a_new_version_of_the_card(app, window, monkeypatch):
    elena = added_elena(window, monkeypatch)
    old_card = elena.linked.card_id
    elena.personality = "harder now"
    answer_changes(monkeypatch, VERSION)
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("After the war", True))
    window.cast_panel.link_button.click()
    kept = window._kept()
    assert len(kept) == 2
    version = window._kept_card(elena.linked.card_id)
    assert version.id != old_card and version.personality == "harder now"
    assert version.collection.family == old_card and version.collection.version == "After the war"
    assert "{{char}} keeps" in version.full_description  # the card's names, back
    assert window._kept_card(old_card).personality == ELENA["personality"]
    assert "In step with their card" in window.cast_panel.link_words.text()
    assert "after the war" in window.cast_panel.link_words.text()


def test_changes_kept_in_the_story_are_said_quietly(app, window, monkeypatch):
    elena = added_elena(window, monkeypatch)
    elena.personality = "harder now"
    answer_changes(monkeypatch, KEEP)
    window.cast_panel.link_button.click()
    assert "(kept here)" in window.cast_panel.link_words.text()
    assert window._kept_card(elena.linked.card_id).personality == ELENA["personality"]


def test_the_cards_changes_since_come_into_the_story(app, window, monkeypatch):
    elena = added_elena(window, monkeypatch)
    card = window._kept_card(elena.linked.card_id)
    card.personality = "wary, and fond of {{user}}"
    window._sync_card_link()
    assert "Changed on the card since: personality." in window.cast_panel.link_words.text()
    answer_changes(monkeypatch, TAKE)
    window.cast_panel.link_button.click()
    assert elena.personality == "wary, and fond of John"
    saved = load_story_bundle("story-1", root=window.root)
    assert next(c for c in saved.cast if c.name == "Elena").personality == elena.personality


def test_a_storys_own_character_can_be_kept(app, window, monkeypatch):
    window.cast_panel.select_character("john")
    assert window.cast_panel.link_button.text() == window_card_link.KEEP_LABEL
    window.cast_panel.link_button.click()
    john = window.session.cast[0]
    kept = window._kept_card(john.linked.card_id)
    assert kept.name == "John" and kept.collection.came_from == "the story Doomsville"
    assert "In step with their card" in window.cast_panel.link_words.text()


def test_a_character_with_pictures_is_kept_before_the_page_was_ever_opened(app, window):
    # The window reads nothing of Your characters at start; keeping one must
    # open them for the pictures (the store was None, and Keep crashed).
    window.session.pictures.write(f"{REFS_DIR}/john.png", tiny_png() + b"john")
    john = window.session.cast[0]
    john.reference_images.append(ImageRef(id="john-face", file=f"{REFS_DIR}/john.png"))
    window.cast_panel.select_character("john")
    assert window._collection is None
    window.cast_panel.link_button.click()
    kept = window._kept_card(john.linked.card_id)
    assert window._collection_pictures().read(kept.reference_images[0].file) == (
        tiny_png() + b"john"
    )


def test_the_page_adds_the_character_shown_to_the_open_story(app, window, monkeypatch):
    elena = window._kept()[0]
    window.show_collection(True, elena.id)
    page = window.collection_page
    assert page.add_button.isVisibleTo(page)
    monkeypatch.setattr(window, "_ask_where_card_goes", lambda card, lore: (False, False))
    page.add_button.click()
    assert [c.name for c in window.session.supporting] == ["Elena"]
    assert window.session.bundle.lore == []


def test_nothing_crosses_while_a_private_scene_is_open(app, window, monkeypatch):
    monkeypatch.setattr(type(window.session), "in_private", property(lambda self: True))
    opened: list = []
    monkeypatch.setattr(CollectionPicker, "exec", lambda picker: opened.append(1) or 0)
    window.cast_panel.collection_button.click()
    assert opened == [] and "private scene" in window.statusBar().currentMessage()
    monkeypatch.undo()  # before the window closes, which asks about the scene


# --- a chat with them ----------------------------------------------------------------------


def chat_dialog(monkeypatch, *, greeting: bool = True, keep: str = "disk") -> list:
    seen: list = []

    def exec_(dialog: CardChatDialog) -> int:
        dialog.user.setText("John")
        if not greeting:
            dialog.greeting.setCurrentIndex(dialog.greeting.findText(NO_GREETING))
        dialog.keep.setCurrentIndex(dialog.keep.findData(keep))
        dialog.model.setText("mock-model")
        seen.append(dialog)
        return QDialog.Accepted

    monkeypatch.setattr(CardChatDialog, "exec", exec_)
    return seen


def test_a_chat_starts_from_their_card_and_greeting(app, window, monkeypatch):
    elena = window._kept()[0]
    chat_dialog(monkeypatch)
    window.chat_with_kept(elena.id)
    session = window.session
    assert session.story.chat and session.story.title == "Elena"
    assert session.story.chat_prompt == "You are Elena. Never break character."
    first, greeting = session.full_path()
    assert first.meta.keep_full and first.kind == "user"
    assert "She trusts John slowly." in first.content
    assert "Personality: wary, dry, loyal once won" in first.content
    assert "You are talking with John." in first.content
    assert greeting.kind == "assistant"
    assert greeting.content == 'Elena lowers the rifle an inch. "Who sent you, John?"'
    # Her portrait is the chat's own reference picture.
    ref = session.story.reference_images[0]
    assert ref.caption == "Elena" and session.pictures.read(ref.file)


def test_with_no_greeting_their_card_is_sent_and_answered(app, window, monkeypatch):
    elena = window._kept()[0]
    chat_dialog(monkeypatch, greeting=False)
    sent: list[str] = []
    monkeypatch.setattr(window, "_submit_turn", lambda text, turn, **k: sent.append(text))
    window.chat_with_kept(elena.id)
    assert window.session.story.chat and window.session.nodes == []
    assert sent and sent[0].startswith("The character you play in this conversation")


def test_a_memory_chat_with_them_writes_nothing(app, window, monkeypatch):
    elena = window._kept()[0]
    chat_dialog(monkeypatch, keep="memory")
    before = sorted(p.name for p in (window.root / "stories").iterdir())
    window.chat_with_kept(elena.id)
    assert window.session.memory_only and len(window.session.full_path()) == 2
    assert sorted(p.name for p in (window.root / "stories").iterdir()) == before


def test_the_card_chat_dialog_follows_your_name_until_a_box_is_changed(app, window):
    elena = window._kept()[0]
    dialog = CardChatDialog(elena, model="m")
    assert "Who sent you, User?" in dialog.greeting_text.toPlainText()
    dialog.prompt.setPlainText("My own prompt for {{char}}.")
    dialog.user.setText("Ann")
    assert "Who sent you, Ann?" in dialog.greeting_text.toPlainText()
    assert "You are talking with Ann." in dialog.first.toPlainText()
    assert dialog.prompt.toPlainText() == "My own prompt for {{char}}."
    dialog.deleteLater()


# --- saved prompts -------------------------------------------------------------------------


def test_system_prompts_are_kept_and_put_back(app, window, monkeypatch):
    saved = window.saved_prompts()
    from PySide6.QtWidgets import QPlainTextEdit

    box = QPlainTextEdit("You are a patient tutor.")
    button = SavedPromptsButton(saved, box)
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Tutor", True))
    button.keep_current()
    assert [(p.name, p.text) for p in window.config.chat_prompts] == [
        ("Tutor", "You are a patient tutor.")
    ]
    box.setPlainText("Something else.")
    button.use(window.config.chat_prompts[0])
    assert box.toPlainText() == "You are a patient tutor."
    box.undo()
    assert box.toPlainText() == "Something else."
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    button._forget("Tutor")
    assert window.config.chat_prompts == []
    assert isinstance(saved, SavedPrompts)


def test_new_chat_and_a_chats_setup_offer_them(app, window, monkeypatch):
    seen: list = []

    def exec_(dialog) -> int:
        seen.append(dialog.saved_button is not None)
        return QDialog.Rejected

    from sealedlore.gui import window_chat

    monkeypatch.setattr(window_chat.NewChatDialog, "exec", exec_)
    window.new_chat()
    assert seen == [True]


# --- a story drafted around them --------------------------------------------------------------

REPLY = {
    "title": "Salt and Iron",
    "description": "A port city.",
    "world": "A steampunk port.",
    "cast": [
        {"name": "Mara", "summary": "A first mate.", "playable": True},
        {
            "name": "Elena",
            "playable": False,
            "in_this_story": "She runs smuggled engines through the harbour.",
        },
    ],
    "supporting": [],
    "lore": [],
    "play_as": "Mara",
    "opening": "The harbour at night.",
    "starting_scene": {"location": "The docks", "present": ["Mara", "Elena"]},
    "style": {},
    "agency_mode": "contested",
}


def test_a_draft_names_their_cards_and_keeps_them_as_they_are(app, window, tmp_path):
    elena = window._kept()[0]
    messages = build_generation_messages("A heist.", characters=[elena])
    request = messages[1].parts[0].text
    assert "## Elena" in request and "She trusts the author's character slowly." in request
    assert "in_this_story" in request

    draft = StoryDraft(
        MockChatProvider(),
        "A heist.",
        "m",
        reasoning=ReasoningConfig(),
        characters=[elena],
    )
    draft.text = json.dumps(REPLY)
    bundle, warnings = draft.build(
        root=tmp_path / "drafts", kept=window._kept(), read=window._collection_pictures().read
    )
    cast = {c.name: c for c in bundle.cast}
    linked = cast["Elena"]
    assert linked.linked.card_id == elena.id and linked.linked.user == "Mara"
    assert not linked.is_player_available
    assert "She trusts Mara slowly." in linked.full_description
    assert linked.full_description.endswith(
        "In this story: She runs smuggled engines through the harbour."
    )
    # Present as the draft placed her, under the id it gave her.
    assert linked.id in bundle.story.setup.starting_scene.present_character_ids
    assert linked.reference_images and warnings == []
    saved = load_story_bundle(bundle.story.id, root=tmp_path / "drafts")
    portrait = next(c for c in saved.cast if c.name == "Elena").reference_images[0]
    assert (tmp_path / "drafts" / "stories" / bundle.story.id / portrait.file).is_file()


def test_draft_with_them_opens_the_premise_with_them(app, window, monkeypatch):
    elena = window._kept()[0]
    seen: list = []

    def exec_(dialog: GenerateDialog) -> int:
        seen.append(([c.name for c in dialog.characters], dialog.windowTitle()))
        return QDialog.Rejected

    monkeypatch.setattr(GenerateDialog, "exec", exec_)
    window.draft_with_kept(elena.id)
    assert seen == [(["Elena"], "A story with Elena")]
    assert window_card_start.CardStartWindow is not None


def wait_for(app, condition, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        app.processEvents()
        time.sleep(0.005)


# --- private, memory-only and TEE chats ------------------------------------------------------


def test_a_tee_chat_still_attesting_keeps_their_card_in_the_box(app, window, monkeypatch):
    """Nothing can be sent while its enclave is attested: their card waits
    in the box to type in rather than being refused and lost."""
    elena = window._kept()[0]
    chat_dialog(monkeypatch, greeting=False)
    after_open = window._after_open

    def attesting() -> None:
        after_open()
        window.session.tee_block = "The enclave is still being attested."

    monkeypatch.setattr(window, "_after_open", attesting)
    sent: list = []
    monkeypatch.setattr(window, "_submit_turn", lambda *a, **k: sent.append(a))
    window.chat_with_kept(elena.id)
    assert sent == [] and window.session.story.chat
    assert window.composer.input.toPlainText().startswith("The character you play")


def test_leaving_for_a_chat_with_them_is_asked_only_once_it_is_wanted(app, window, monkeypatch):
    elena = window._kept()[0]
    asked: list = []
    monkeypatch.setattr(window, "_private_allows_close", lambda: asked.append(1) or False)
    monkeypatch.setattr(CardChatDialog, "exec", lambda d: QDialog.Rejected)
    window.chat_with_kept(elena.id)
    assert asked == []  # cancelled: nothing asked, nothing lost
    chat_dialog(monkeypatch)
    window.chat_with_kept(elena.id)
    assert asked == [1] and not window.session.story.chat  # asked, and refused


def test_a_memory_chat_with_them_sent_and_answered_writes_nothing(app, window, monkeypatch):
    elena = window._kept()[0]

    def everything() -> dict[str, bytes]:
        return {
            str(path.relative_to(window.root)): path.read_bytes()
            for path in sorted(window.root.rglob("*"))
            if path.is_file()
        }

    before = everything()
    chat_dialog(monkeypatch, greeting=False, keep="memory")
    window.chat_with_kept(elena.id)
    wait_for(app, lambda: not window._busy and len(window.session.full_path()) == 2)
    assert window.session.memory_only
    assert window.session.full_path()[0].meta.keep_full
    assert everything() == before


def test_a_memory_chats_prompt_is_kept_only_when_said(app, window, monkeypatch):
    from PySide6.QtWidgets import QPlainTextEdit

    box = QPlainTextEdit("A prompt said only in memory.")
    button = SavedPromptsButton(window.saved_prompts(), box, in_memory=lambda: True)
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Secret", True))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    button.keep_current()
    assert window.config.chat_prompts == []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    button.keep_current()
    assert [p.name for p in window.config.chat_prompts] == ["Secret"]


def test_a_deleted_card_is_never_made_again_by_a_click_meant_to_open_it(app, window, monkeypatch):
    elena = added_elena(window, monkeypatch)
    assert window.cast_panel.link_button.text() == window_card_link.CARD_LABEL
    collection.forget(window._collection_session().bundle, window._kept_card(elena.linked.card_id))
    window.cast_panel.link_button.click()
    assert window._kept() == []  # not made again
    assert window.cast_panel.link_button.text() == window_card_link.KEEP_LABEL


def test_their_card_from_setup_doesnt_open_the_page_behind_it(app, window, monkeypatch):
    added_elena(window, monkeypatch)
    monkeypatch.setattr(window, "_dialog_parent", lambda: object())
    window.cast_panel.link_button.click()
    assert not window.collection_shown()


def test_a_card_added_before_anyone_is_played_names_them_at_the_start(app, window, monkeypatch):
    window.session.story.held_character_id = None
    elena = added_elena(window, monkeypatch)
    assert "{{user}}" in elena.full_description and elena.linked.user == ""
    window.session.story.held_character_id = "john"
    window.name_kept_users()
    assert "She trusts John slowly." in elena.full_description
    assert elena.linked.user == "John"
    window._sync_card_link()
    assert "In step with their card" in window.cast_panel.link_words.text()


def test_the_story_suggests_whom_a_card_talks_to_before_anyone_plays(app, window, monkeypatch):
    window.session.story.held_character_id = None
    window.session.story.setup.suggested_character_id = "john"
    elena = added_elena(window, monkeypatch)
    assert "She trusts John slowly." in elena.full_description


def test_a_name_taken_from_the_card_keeps_their_place_on_the_rosters(app, window, monkeypatch):
    elena = added_elena(window, monkeypatch)
    window.session.story.scene.present_character_ids.append(elena.id)
    card = window._kept_card(elena.linked.card_id)
    card.name = "Elena Ruiz"
    answer_changes(monkeypatch, TAKE)
    window.cast_panel.link_button.click()
    assert elena.name == "Elena Ruiz"
    assert elena.id in window.session.story.scene.present_character_ids


def test_the_greeting_written_over_is_kept_as_the_name_is_typed(app, window):
    elena = window._kept()[0]
    dialog = CardChatDialog(elena, model="m")
    dialog.greeting_text.setPlainText("Hello, stranger.")
    dialog.about.setPlainText("A drifter.")
    dialog.user.setText("Ann")
    assert dialog.greeting_text.toPlainText() == "Hello, stranger."
    # Another greeting chosen is that greeting.
    dialog.greeting.setCurrentIndex(1)
    assert dialog.greeting_text.toPlainText().startswith("The door is barred.")
    dialog.deleteLater()


def test_a_draft_can_go_back_to_nobody(app, window):
    picker = CollectionPicker(
        window._kept(),
        window._collection_pictures,
        title="t",
        lead="l",
        action="Choose",
        placing=False,
    )
    assert picker.ok.isEnabled() and picker.chosen() == []
    picker.deleteLater()
