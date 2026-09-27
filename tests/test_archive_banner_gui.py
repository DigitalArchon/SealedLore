"""With automatic archival off, an over-budget story offers archival itself, offscreen."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.repository import StoryBundle, load_config, save_story_bundle  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402
from tests.test_background_queue import wait_for  # noqa: E402
from tests.test_session import FILLER  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path: Path, story, cast) -> MainWindow:
    nodes = make_exchange(30)
    for node in nodes:
        node.content = f"{node.content} {FILLER * 12}".strip()
    story.active_leaf_id = nodes[-1].id
    story.defaults.context_token_budget = 4_000
    save_story_bundle(StoryBundle(story=story, cast=cast, nodes=nodes), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.auto_archive = False
    window.config.suggest_characters = False
    window._provider = MockChatProvider(["A chapter of what happened."])
    window.open_story(story.id)
    return window


def test_over_budget_with_archival_off_shows_the_banner(window: MainWindow):
    assert window.over_budget.isVisibleTo(window)
    assert "over its context budget" in window.over_budget.message.text()


def test_archive_now_archives_down_and_clears_it(app, window: MainWindow):
    window.over_budget.archive_button.click()
    wait_for(app, lambda: not window._busy)
    assert window.session.summaries
    assert window.session.context_overflow() is None
    assert not window.over_budget.isVisibleTo(window)


def test_archive_automatically_turns_it_on(window: MainWindow, tmp_path: Path):
    window.over_budget.automatic_button.click()
    assert window.config.auto_archive is True
    assert load_config(root=tmp_path).auto_archive is True
    window.refresh_summaries()
    assert not window.over_budget.isVisibleTo(window)


def test_no_banner_while_archival_is_automatic(window: MainWindow):
    window.config.auto_archive = True
    window.refresh_summaries()
    assert not window.over_budget.isVisibleTo(window)


def test_settings_turn_the_ledger_and_archival_on_and_off(app, story):
    from sealedlore.gui.settings_dialog import SettingsDialog
    from sealedlore.models.config import Config, ProviderConfig

    config = Config(
        providers=[ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1", model="m")],
        active_provider_name="nano",
    )
    dialog = SettingsDialog(config, story)
    assert dialog.story_ledger.isChecked() is False
    dialog.story_ledger.setChecked(True)
    dialog.auto_archive.setChecked(False)
    dialog._save()
    assert config.story_ledger is True
    assert config.auto_archive is False
