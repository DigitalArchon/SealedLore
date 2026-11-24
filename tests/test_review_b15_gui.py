"""What the review of 3.1.0b15 found in the window (Nov 21 2026), one test a
fault: each fails on da927af. Offscreen, no network."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog  # noqa: E402

from sealedlore.gui import settings_dialog as settings_module  # noqa: E402
from sealedlore.gui.modal_job import run_with_progress  # noqa: E402
from sealedlore.gui.settings_dialog import SettingsDialog  # noqa: E402
from sealedlore.gui.story_suggestions_dialog import StorySuggestionsDialog  # noqa: E402
from sealedlore.models.config import (  # noqa: E402
    Config,
    MediaEndpoint,
    ProviderConfig,
    bind_unbound_keys,
)
from sealedlore.models.lore import LoreEntry  # noqa: E402
from sealedlore.models.story_suggestion import StorySuggestion  # noqa: E402
from sealedlore.storage.lorebooks import load_lorebooks  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.test_lorebooks_gui import import_one, window_on  # noqa: E402
from tests.test_modal_job import window_with_lore  # noqa: E402

NANO = "https://nano-gpt.com/api/v1"


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def config(**changes) -> Config:
    chat = ProviderConfig(name="NanoGPT", base_url=NANO, api_key="nano-key", model="glm")
    made = Config(providers=[chat], active_provider_name="NanoGPT", **changes)
    bind_unbound_keys(made)  # as a config is when loaded
    return made


def questions(monkeypatch, answer) -> list[str]:
    asked: list[str] = []

    def question(_parent, _title, text, *_rest):
        asked.append(text)
        return answer

    monkeypatch.setattr(QMessageBox, "question", question)
    return asked


def answered(app: QApplication, dialog: SettingsDialog, timeout: float = 10.0) -> None:
    """Until the server asked on its own thread has answered (`_ask_server`)."""
    deadline = time.monotonic() + timeout
    while dialog._asking:
        if time.monotonic() > deadline:
            raise AssertionError("the server asked never answered")
        app.processEvents()
        time.sleep(0.005)


# --- Settings: a key goes nowhere unasked ------------------------------------------


def test_a_slip_in_another_field_never_sends_a_saved_key_to_a_new_place(app, monkeypatch):
    """With Extra settings that aren't JSON on the Images page, the question
    about a saved key and its new address was skipped (taken for "an address
    that can't be used"), and Detect sent the private key to the new host."""
    private = ProviderConfig(
        name="private", base_url="https://a.example/v1", api_key="private-key", model="m"
    )
    dialog = SettingsDialog(config(private_provider=private), None)
    sent: list[str] = []
    monkeypatch.setattr(
        settings_module, "detect_context", lambda url, key, model, **_: sent.append(url) or None
    )
    warned: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda _parent, title, text, *_rest: warned.append(title)
    )
    asked = questions(monkeypatch, QMessageBox.Yes)
    dialog.image_api.setCurrentIndex(dialog.image_api.findData("a1111"))
    dialog.own_server.extra.setText('{"steps": 30')
    dialog.private_url.setText("https://b.example/v1")
    dialog.private_model.setText("m")
    dialog._detect_private_context()
    assert sent == [] and warned == ["Nothing was sent"] and asked == []
    # Put right, the key's new place is asked about before anything goes.
    dialog.own_server.extra.setText("")
    dialog._detect_private_context()
    answered(app, dialog)
    assert len(asked) == 1 and "b.example" in asked[0]
    assert sent == ["https://b.example/v1"]
    dialog.deleteLater()


def test_checking_a_server_on_this_computer_asks_nothing_about_its_key(app, monkeypatch):
    """A key for http://127.0.0.1:7860 was asked about as one going "to
    another machine on your network", and No was the default."""
    listed: list[str] = []
    monkeypatch.setattr(
        settings_module, "list_own_models", lambda url, **kw: listed.append(kw["api_key"]) or []
    )
    asked = questions(monkeypatch, QMessageBox.No)
    endpoint = MediaEndpoint(api="a1111", api_key="user:password")
    dialog = SettingsDialog(config(image_endpoint=endpoint), None)
    dialog.own_server.check_button.click()
    answered(app, dialog)
    assert asked == [] and listed == ["user:password"]
    dialog.deleteLater()


def test_a_password_in_a_servers_address_is_asked_about_as_a_key_is(app, monkeypatch):
    listed: list[str] = []
    monkeypatch.setattr(
        settings_module, "list_own_models", lambda url, **kw: listed.append(url) or []
    )
    asked = questions(monkeypatch, QMessageBox.No)
    dialog = SettingsDialog(config(), None)
    dialog.image_api.setCurrentIndex(dialog.image_api.findData("a1111"))
    dialog.image_url.setText("http://user:secret@192.168.1.50:7860")
    dialog.own_server.check_button.click()
    assert len(asked) == 1 and listed == []
    assert "nothing was sent" in dialog.own_server.check_note.text()
    dialog.deleteLater()


# --- the window's pages ---------------------------------------------------------------


def test_lorebooks_then_your_characters_then_back_is_the_story_again(
    app, tmp_path: Path, story, cast
):
    """Your characters opened over Lorebooks took what Lorebooks had put
    away for hidden by hand: Back left neither composer nor side panel."""
    window = window_on(tmp_path, story, cast)
    assert not window.composer.isHidden() and not window.inspector_dock.isHidden()
    window.show_lorebooks(True)
    window.show_collection(True)
    window.show_collection(False)
    assert not window.lorebooks_shown() and not window.collection_shown()
    assert not window.composer.isHidden() and not window.inspector_dock.isHidden()
    window.close()


def test_a_name_typed_just_before_leaving_is_kept(app, tmp_path: Path, monkeypatch):
    window = window_on(tmp_path)
    stored = import_one(window, tmp_path, monkeypatch)
    page = window.lorebooks_page
    page.name.setText("Typed and gone")
    page.name.textEdited.emit("Typed and gone")  # the page waits a moment after a key
    window.show_lorebooks(False)
    (kept,) = load_lorebooks(tmp_path / "data").books
    assert kept.id == stored.id and kept.name == "Typed and gone"
    window.close()


def test_changing_how_lore_fires_is_not_taken_back_by_an_earlier_reviews_undo(
    app, tmp_path: Path, story, cast
):
    window = window_on(tmp_path, story, cast)
    window.session.review_undo = object()
    window._on_firing_changed("sillytavern")
    assert window.session.story.defaults.lore_firing == "sillytavern"
    assert window.session.review_undo is None
    window.close()


def test_lore_fired_by_sillytaverns_rules_is_not_sent_to_be_embedded(
    app, tmp_path: Path, story, cast
):
    """Its vectors are never read: every entry of a new story's lorebook
    went to the embeddings endpoint behind a progress dialog, for nothing."""
    story.defaults.lore_firing = "sillytavern"
    window = window_on(tmp_path, story, cast)
    session = window.session
    session.bundle.lore += [
        LoreEntry(title=f"Place {i}", content="A place. " * 400, keywords=[f"place{i}"])
        for i in range(30)
    ]
    session.embeddings = object()
    looked: list[bool] = []
    session.lore_without_vectors = lambda: looked.append(True) or []
    window.embed_lore_now(least=1)
    assert looked == []
    session.story.defaults.lore_firing = "sealedlore"
    window.embed_lore_now(least=1)
    assert looked == [True]
    session.embeddings = None
    window.close()


def test_lore_held_back_until_mentioned_is_not_shown_to_the_people_finder(
    app, tmp_path: Path, story, cast
):
    """No model has heard of it, and someone found in it would be a card
    every model reads from then on."""
    held = LoreEntry(
        title="The Hollow King", content="He sleeps under the keep.", until_mentioned=True
    )
    known = LoreEntry(title="Sheriff Ada Hale", content="Ada Hale is the sheriff.")
    save_story_bundle(
        StoryBundle(story=story, cast=list(cast), lore=[held, known]), root=tmp_path / "data"
    )
    window = window_on(tmp_path)
    window.open_story(story.id)
    shown: list[list[str]] = []
    window._find_people = lambda session, entries, world: (
        shown.append([e.title for e in entries]) or None
    )
    window.find_people_in_story()
    assert shown == [["Sheriff Ada Hale"]]
    window.close()


# --- dialogs --------------------------------------------------------------------------


def test_what_was_typed_in_one_suggestion_stays_when_another_is_applied(app):
    """Every card was made again after any Apply or Not this, and an edit
    waiting in another went back to the model's wording."""
    waiting = [
        StorySuggestion(kind="lore_new", title="The Ford", keywords=["ford"], content="Shallow."),
        StorySuggestion(kind="lore_new", title="The Mill", keywords=["mill"], content="Burned."),
    ]

    def apply(suggestion, values):
        waiting.remove(suggestion)
        return "Added."

    dialog = StorySuggestionsDialog(
        lambda: list(waiting),
        current=lambda s: "",
        apply=apply,
        decline=waiting.remove,
        look_over=None,
    )
    ford, mill = dialog.cards
    mill.content.setPlainText("Burned in the siege, its wheel still turning.")
    mill.title_field.setText("The Old Mill")
    mill.keywords_field.setText("mill, wheel")
    ford.apply_button.click()
    (left,) = dialog.cards
    assert left.values() == {
        "content": "Burned in the siege, its wheel still turning.",
        "title": "The Old Mill",
        "keywords": ["mill", "wheel"],
    }
    dialog.deleteLater()


def test_a_job_being_stopped_keeps_its_dialog_until_it_has_ended(app, tmp_path: Path):
    """After Stop the dialog has no button, and Esc or its close box hid it
    while the job ran on: the window free, and quitting waiting behind it."""
    window = window_with_lore(tmp_path)
    release = threading.Event()
    seen: dict[str, bool] = {}

    def job():
        while not release.wait(0.01):
            pass
        return
        yield

    def press() -> None:
        progress = window.findChildren(QProgressDialog)[-1]
        progress.canceled.emit()  # Stop (cancel() from code emits nothing)
        seen["stopping"] = progress.isVisible()
        progress.reject()  # Esc
        seen["after esc"] = progress.isVisible()
        progress.close()  # the close box
        seen["after close"] = progress.isVisible()
        release.set()

    QTimer.singleShot(100, window, press)
    result: dict = {}
    began = time.monotonic()
    run_with_progress(window, window.session, "Working…", job, result, title="A job")
    assert time.monotonic() - began < 20
    assert seen == {"stopping": True, "after esc": True, "after close": True}
    assert result.get("stopped") is True
    assert not [d for d in window.findChildren(QProgressDialog) if d.isVisible()]
    window.close()


def test_the_close_box_of_a_running_job_is_stop_and_the_dialog_stays(app, tmp_path: Path):
    window = window_with_lore(tmp_path)
    release = threading.Event()
    seen: dict[str, bool] = {}

    def job():
        while not release.wait(0.01):
            pass
        return
        yield

    def press() -> None:
        progress = window.findChildren(QProgressDialog)[-1]
        progress.close()
        seen["after close"] = progress.isVisible()
        release.set()

    QTimer.singleShot(100, window, press)
    result: dict = {}
    run_with_progress(window, window.session, "Working…", job, result, title="A job")
    assert seen == {"after close": True} and result.get("stopped") is True
    window.close()
