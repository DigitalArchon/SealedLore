"""The Plot tab and the clock in the status bar, offscreen."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QComboBox  # noqa: E402

from sealedlore.engine.plot_md import parse_plot_markdown  # noqa: E402
from sealedlore.engine.prompt import TurnRequest  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.repository import save_story_bundle  # noqa: E402
from sealedlore.storage.scenario import bundle_from_scenario  # noqa: E402

SAMPLE = Path(__file__).resolve().parents[1] / "SampleStories" / "Doomsville.md"


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_the_plot_tab_follows_the_story(app, tmp_path: Path):
    bundle = bundle_from_scenario(parse_plot_markdown(SAMPLE.read_text(encoding="utf-8")).scenario)
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.scene_reads = "manual"
    window.open_story(bundle.story.id)
    session = window.session
    assert session is not None

    assert window.clock_button.text() == "Day 1 · 15:30"
    assert window.plot_panel.facts.rowCount() == len(bundle.story.plot.facts)
    # No passage yet, so nothing to set a fact on.
    assert not window.plot_panel.facts.cellWidget(0, 1).isEnabled()

    john = session.character_by_name("John")
    list(session.begin(john.id))
    assert [c.name for c in session.cast if c.name in ("John", "Jane")] == ["John"]
    session.provider = MockChatProvider(
        [
            # No director call: "Welcome to Hellsville" happens at a place
            # that is neither here nor mentioned.
            "John pushed the farmhouse door open.",
            json.dumps({"elapsed": {"amount": 2, "unit": "hours"}}),
        ]
    )
    list(
        session.send(
            TurnRequest(speaker_id=john.id, user_text="I go in.", controlled_character_id=john.id)
        )
    )
    window._refresh_scene_panel()
    assert window.clock_button.text() == "Day 1 · 17:30"
    assert window.plot_panel.changes_strip.isVisibleTo(window.plot_panel)

    names = [
        window.plot_panel.facts.item(row, 0).text()
        for row in range(window.plot_panel.facts.rowCount())
    ]
    combo = window.plot_panel.facts.cellWidget(names.index("hellsville"), 1)
    assert isinstance(combo, QComboBox) and combo.isEnabled()
    combo.setCurrentText("fallen")
    assert session.chronicle.facts["hellsville"] == "fallen"
    # The author's correction replaces the read's, so there's no Undo to offer.
    assert not window.plot_panel.changes_strip.isVisibleTo(window.plot_panel)

    # Spoilers off: no facts, and no event the character doesn't know of.
    assert not window.plot_panel.facts.isVisibleTo(window.plot_panel)
    assert window.plot_panel.events.rowCount() == 0
    window.plot_panel.spoilers.setChecked(True)
    assert window.config.show_plot_spoilers
    panel = window.plot_panel
    assert panel.events.rowCount() == len(bundle.story.plot.events)
    fall = panel._event_ids.index("the_fall_of_hellsville")
    assert panel.events.item(fall, 2).text().startswith("day ")
    panel.events.selectRow(fall)
    panel.mark_skipped.click()
    assert session.chronicle.events["the_fall_of_hellsville"].state == "missed"
    assert panel.events.item(fall, 1).text() == "skipped"
    window.close()


def _names(widget) -> list[str]:
    return [widget.item(row).text() for row in range(widget.count())]


def test_hidden_cast_and_places_stay_out_of_the_tabs_until_brought_in(app, tmp_path: Path):
    bundle = bundle_from_scenario(parse_plot_markdown(SAMPLE.read_text(encoding="utf-8")).scenario)
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.scene_reads = "manual"
    window.config.show_plot_spoilers = False
    window.open_story(bundle.story.id)
    session = window.session
    list(session.begin(session.character_by_name("John").id))
    window.refresh_panels()
    window._refresh_scene_panel()

    assert not any("Marcus" in name for name in _names(window.cast_panel.list))
    assert not any("Hidden Caves" in name for name in _names(window.lore_panel.list))
    assert any("Hellsville" in name for name in _names(window.lore_panel.list))
    panel = window.plot_panel
    assert panel.where.text() == "Where: none of the named places"
    # Held back is the plot's business: only with spoilers on.
    assert not panel.held_back.isVisibleTo(panel)

    panel.spoilers.setChecked(True)
    assert any("Marcus" in name for name in _names(window.cast_panel.list))
    assert panel.held_back.isVisibleTo(panel)
    rows = [panel.held_back.item(row, 0).text() for row in range(panel.held_back.rowCount())]
    marcus = next(row for row, name in enumerate(rows) if "Marcus" in name)
    panel.held_back.selectRow(marcus)
    panel.bring_in_button.click()
    marcus_id = session.character_by_name("Marcus Webb").id
    assert marcus_id in session.chronicle.brought_in
    assert marcus_id not in session.hidden_ids()

    # Spoilers off again: Marcus is in the story now, the others still aren't.
    panel.spoilers.setChecked(False)
    names = _names(window.cast_panel.list)
    assert any("Marcus" in name for name in names) and not any("Elena" in name for name in names)

    session.set_place("Hellsville")
    window._refresh_plot_panel()
    assert panel.where.text() == "Where: Hellsville"
    window.close()


def test_a_skipped_over_event_shows_how_it_was_told(app, tmp_path: Path):
    bundle = bundle_from_scenario(parse_plot_markdown(SAMPLE.read_text(encoding="utf-8")).scenario)
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.scene_reads = "manual"
    window.config.show_plot_spoilers = False
    window.open_story(bundle.story.id)
    session = window.session
    list(session.begin(session.character_by_name("John").id))
    chronicle = session.chronicle
    status = chronicle.events["first_bandit_meeting"]
    status.state, status.revealed = "happened", True
    status.account = "Bandits stopped John on the north road and let him pass."
    session._set_chronicle(chronicle)
    window._refresh_plot_panel()
    panel = window.plot_panel
    assert panel.events.rowCount() == 1
    assert panel.events.item(0, 1).text() == "happened in the time that passed"
    assert panel.events.item(0, 0).toolTip() == status.account
    window.close()
