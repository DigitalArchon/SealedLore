"""The Lore panel and the status bar say when the whole lorebook is sent, offscreen."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.engine.retrieval import RetrievalReport  # noqa: E402
from sealedlore.gui.lore_panel import LorePanel  # noqa: E402
from sealedlore.gui.status import StatusStrip  # noqa: E402
from sealedlore.models.lore import LoreEntry  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def whole() -> RetrievalReport:
    entries = (
        LoreEntry(title="House Vaun", content="x"),
        LoreEntry(title="The Accord", content="y"),
    )
    return RetrievalReport(mode="whole", standing=entries)


def test_the_panel_lists_every_entry_as_sent_every_turn(app):
    panel = LorePanel()
    panel.show_retrieval(whole())
    rows = [panel.injected.item(i).text() for i in range(panel.injected.count())]
    assert rows == ["House Vaun — sent every turn", "The Accord — sent every turn"]
    assert "All 2 entries" in panel.status.text()


def test_the_status_bar_says_all_of_it(app):
    strip = StatusStrip()
    strip.set_lore(whole())
    assert strip.lore_label.text() == "lore all 2"


def test_settings_choose_how_a_large_lorebook_is_selected(app, story):
    from sealedlore.gui.settings_dialog import SettingsDialog
    from sealedlore.models.config import Config, ProviderConfig

    config = Config(
        providers=[ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1", model="m")],
        active_provider_name="nano",
    )
    dialog = SettingsDialog(config, story)
    assert dialog.lore_selector.currentData() == "picker"
    dialog.lore_selector.setCurrentIndex(dialog.lore_selector.findData("jev"))
    dialog.lore_model.setText("z-ai/glm-5.2")
    dialog._save()
    assert config.lore_selector == "jev"
    assert config.lore_model == "z-ai/glm-5.2"


def test_the_panel_says_who_picked(app):
    entry = LoreEntry(title="The Accord", content="y")
    from sealedlore.engine.retrieval import Injection

    panel = LorePanel()
    report = RetrievalReport(
        injected=(Injection(entry=entry, reason="picked", tokens=5),),
        tokens=5,
        selector="picker",
    )
    panel.show_retrieval(report)
    assert "picked after the last passage" in panel.status.text()
    assert panel.injected.item(0).text() == "The Accord — picked by the lore model"


def test_the_panel_says_how_far_back_the_keywords_reached(app):
    from sealedlore.engine.retrieval import Injection

    entry = LoreEntry(title="The Accord", content="y")
    panel = LorePanel()
    panel.show_retrieval(
        RetrievalReport(injected=(Injection(entry, "keyword", 1),), tokens=1, keyword_reach=2)
    )
    assert "keywords from the last 2 exchanges" in panel.status.text()
