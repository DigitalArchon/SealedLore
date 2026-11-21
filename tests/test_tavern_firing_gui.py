"""The Lore page's Entries choice, SillyTavern settings… and the report of what
fired as SillyTavern would (gui/lore_panel.py, gui/tavern_settings_dialog.py,
gui/window_tavern_firing.py). Offscreen, no network."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from sealedlore.engine.retrieval import Injection, RetrievalReport  # noqa: E402
from sealedlore.gui import window_tavern_firing  # noqa: E402
from sealedlore.gui.lore_panel import LorePanel  # noqa: E402
from sealedlore.gui.tavern_settings_dialog import TavernSettingsDialog  # noqa: E402
from sealedlore.models.lore import LoreEntry, TavernWorldInfo  # noqa: E402
from sealedlore.storage.repository import load_story_bundle  # noqa: E402
from tests.test_images_gui import window  # noqa: E402,F401  (the window fixture)


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_the_choice_is_shown_without_being_announced_and_told_when_changed(app):
    panel = LorePanel()
    heard: list[str] = []
    panel.firing_changed.connect(heard.append)
    panel.set_firing("sillytavern")
    assert heard == [] and panel.tavern_settings_button.isEnabled()
    panel.firing.setCurrentIndex(panel.firing.findData("sealedlore"))
    assert heard == ["sealedlore"] and not panel.tavern_settings_button.isEnabled()
    panel.deleteLater()


def test_the_report_says_what_fired_and_what_didnt(app):
    panel = LorePanel()
    wolves = LoreEntry(title="The Grey Wolves", content="x")
    omen = LoreEntry(title="Rare Omen", content="y")
    report = RetrievalReport(
        injected=(Injection(entry=wolves, reason="tavern", tokens=12, why="key “wolf”"),),
        tokens=12,
        selector="sillytavern",
        not_fired=((omen, "its 10% chance didn't come up"),),
        tavern_budget=4000,
    )
    panel.show_retrieval(report)
    items = [panel.injected.item(i).text() for i in range(panel.injected.count())]
    assert items == [
        "The Grey Wolves — fired as SillyTavern would: key “wolf”",
        "Rare Omen — not fired: its 10% chance didn't come up",
    ]
    assert panel.status.text() == (
        "1 entries, ~12 tokens · fired as SillyTavern would, within its budget of 4,000 tokens"
    )
    panel.deleteLater()


def test_the_settings_dialog_keeps_what_is_set_and_goes_back_to_sillytaverns(app):
    dialog = TavernSettingsDialog(TavernWorldInfo(scan_depth=4, recursive=False))
    assert dialog.scan_depth.value() == 4 and not dialog.recursive.isChecked()
    dialog.budget_percent.setValue(40)
    dialog.group_scoring.setChecked(True)
    got = dialog.settings()
    assert (got.scan_depth, got.budget_percent, got.use_group_scoring) == (4, 40, True)
    dialog._load(TavernWorldInfo())
    assert dialog.settings() == TavernWorldInfo()
    dialog.deleteLater()


def test_the_window_keeps_the_choice_and_the_settings_with_the_story(app, window, monkeypatch):  # noqa: F811
    window.lore_panel.firing.setCurrentIndex(window.lore_panel.firing.findData("sillytavern"))
    story = window.session.story
    assert story.defaults.lore_firing == "sillytavern"
    assert "fires by SillyTavern's rules" in window.statusBar().currentMessage()

    def accept(dialog):
        dialog.scan_depth.setValue(6)
        return QDialog.Accepted

    monkeypatch.setattr(window_tavern_firing.TavernSettingsDialog, "exec", accept)
    window._open_tavern_settings()
    assert story.defaults.tavern_world_info.scan_depth == 6
    saved = load_story_bundle(story.id, root=window.root).story
    assert saved.defaults.lore_firing == "sillytavern"
    assert saved.defaults.tavern_world_info.scan_depth == 6
