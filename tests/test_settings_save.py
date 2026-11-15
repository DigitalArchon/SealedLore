"""Settings' three buttons (the author, Nov 15 2026): Save writes and applies
everything and the dialog stays open, as if just opened on what was saved;
Save and Close is what Save did; Close asks only about what wasn't saved.
Offscreen, no network."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QMessageBox  # noqa: E402

from sealedlore.gui import main_window as main_window_module  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.settings_dialog import SettingsDialog  # noqa: E402
from sealedlore.models.config import Config, ProviderConfig  # noqa: E402
from sealedlore.storage.repository import StoryBundle, load_config, save_story_bundle  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402

NANO = "https://nano-gpt.com/api/v1"
LAN = "http://192.168.1.5:11434/v1"


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def asked(monkeypatch, answer) -> list[str]:
    """Every question's title, answered `answer`."""
    titles: list[str] = []

    def question(_parent, title, _text, *_args, **_kwargs):
        titles.append(title)
        return answer

    monkeypatch.setattr(QMessageBox, "question", question)
    return titles


def nano() -> Config:
    endpoint = ProviderConfig(name="NanoGPT", base_url=NANO, api_key="nano-key", model="glm")
    return Config(providers=[endpoint], active_provider_name="NanoGPT")


def test_the_three_buttons_and_enter_is_still_save_and_close(app):
    dialog = SettingsDialog(nano(), None)
    box = dialog.save_button.parent()
    assert isinstance(box, QDialogButtonBox)
    assert {b.text().replace("&", "") for b in box.buttons()} == {
        "Save",
        "Save and Close",
        "Close",
    }
    assert box.buttonRole(dialog.save_button) == QDialogButtonBox.ButtonRole.ApplyRole
    assert box.buttonRole(dialog.save_close_button) == QDialogButtonBox.ButtonRole.AcceptRole
    assert box.buttonRole(dialog.close_button) == QDialogButtonBox.ButtonRole.RejectRole
    assert dialog.save_close_button.isDefault() and not dialog.save_button.isDefault()
    dialog.deleteLater()


def test_save_writes_and_stays_open_and_close_then_asks_nothing(app, monkeypatch):
    config = nano()
    dialog = SettingsDialog(config, None)
    told: list[int] = []
    dialog.saved.connect(lambda: told.append(1))
    dialog.show()
    app.processEvents()
    dialog.summary_words.setValue(321)
    # A new endpoint, saved, and then the dialog still there to choose its models.
    dialog._add_endpoint()
    dialog.provider_name.setText("Second")
    dialog.base_url.setText("https://second.example.com/v1")
    dialog.api_key.setText("second-key")
    questions = asked(monkeypatch, QMessageBox.Discard)
    dialog.save_button.click()
    assert told == [1], "the window is told, to apply it at once"
    assert dialog.isVisible() and dialog.result() != QDialog.Accepted
    assert config.summary_target_words == 321
    assert [p.name for p in config.providers] == ["NanoGPT", "Second"]
    assert config.endpoint_for("Second::m").api_key == "second-key"
    # Close now: everything was saved, so nothing to ask about.
    dialog.close_button.click()
    assert questions == [] and dialog.result() == QDialog.Rejected
    dialog.deleteLater()


def test_close_after_a_save_still_asks_about_what_came_after_it(app, monkeypatch):
    config = nano()
    dialog = SettingsDialog(config, None)
    dialog.show()
    app.processEvents()
    dialog.summary_words.setValue(222)
    dialog.save_button.click()
    dialog.summary_words.setValue(333)
    questions = asked(monkeypatch, QMessageBox.Cancel)
    dialog.close_button.click()
    assert questions == ["Discard your changes?"] and dialog.isVisible()
    assert config.summary_target_words == 222
    dialog.deleteLater()


def test_save_and_close_is_what_save_did(app):
    config = nano()
    dialog = SettingsDialog(config, None)
    told: list[int] = []
    dialog.saved.connect(lambda: told.append(1))
    dialog.summary_words.setValue(444)
    dialog.save_close_button.click()
    assert dialog.result() == QDialog.Accepted and config.summary_target_words == 444
    assert told == [], "the window applies an accepted dialog itself"
    dialog.deleteLater()


def test_a_key_question_answered_at_a_save_isnt_asked_again(app, monkeypatch):
    """A key going over plain http to another machine, confirmed at a Save that
    stayed open: the next Save asks nothing about it."""
    config = nano()
    dialog = SettingsDialog(config, None)
    dialog._add_endpoint()
    dialog.provider_name.setText("Studio")
    dialog.base_url.setText(LAN)
    dialog.api_key.setText("lan-key")
    questions = asked(monkeypatch, QMessageBox.Yes)
    dialog.save_button.click()
    assert questions == ["Send the key unencrypted?"]
    assert dialog.error.isHidden()
    questions = asked(monkeypatch, QMessageBox.No)
    dialog.summary_words.setValue(250)
    dialog.save_button.click()
    assert questions == [], "answered already, and saved"
    assert config.summary_target_words == 250
    assert config.endpoint_for("Studio::llama3").api_key == "lan-key"
    dialog.deleteLater()


def test_a_save_that_stays_open_applies_it_in_the_window_at_once(
    app, tmp_path: Path, story, cast, monkeypatch
):
    """Everything an accepted dialog does happens on Save, while it is open: the
    settings on disk, a new provider for the story, the controls; and Close
    afterwards does it no second time."""
    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(story.id)
    seen: dict[str, object] = {}
    applied: list[int] = []
    original = window._settings_saved

    def counted(*args):
        applied.append(1)
        original(*args)

    monkeypatch.setattr(window, "_settings_saved", counted)

    class Stays(SettingsDialog):
        def exec(self) -> int:
            seen["provider_before"] = window.session.provider
            self.summary_words.setValue(287)
            self.save_button.click()
            seen["on_disk"] = load_config(root=tmp_path).summary_target_words
            seen["provider_after"] = window.session.provider
            seen["questions"] = asked(monkeypatch, QMessageBox.Discard)
            self.close_button.click()
            return self.result()

    monkeypatch.setattr(main_window_module, "SettingsDialog", Stays)
    try:
        window.open_settings()
        assert seen["on_disk"] == 287, "written as Save was pressed"
        assert seen["provider_after"] is not seen["provider_before"], "the provider renewed"
        assert seen["questions"] == []
        assert applied == [1], "once, on Save; Close after it applies nothing more"
    finally:
        window.close()
