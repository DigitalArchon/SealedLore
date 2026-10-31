"""The Stories list, Setup's Models tab and the text boxes, in the real window."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from sealedlore.gui.main_window import MainWindow
from sealedlore.gui.setup_dialog import ALL_IN_SETTINGS, SOME_OF_ITS_OWN, ModelChoices, SetupDialog
from sealedlore.gui.stories import OWN_MODELS, StoryListPanel, _StoryRow
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.node import Node
from sealedlore.models.plot import Plot
from sealedlore.models.route import ModelRoute
from sealedlore.models.story import Story
from sealedlore.storage.repository import (
    StoryBundle,
    load_story_bundle,
    save_config,
    save_story_bundle,
)
from tests.test_ui_tidy import needs_real_fonts, settle


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def message(node_id: str, text: str, when: str = "2026-03-01T10:00:00+00:00", parent=None) -> Node:
    node = Node(
        id=node_id,
        parent_id=parent,
        kind="assistant",
        speaker_id="__narrator__",
        content=text,
        created_at=when,
    )
    return node


def save(root: Path, story_id: str, title: str, *nodes: Node, **fields) -> Story:
    story = Story(id=story_id, title=title, **fields)
    if nodes:
        story.active_leaf_id = nodes[-1].id
    save_story_bundle(StoryBundle(story=story, nodes=list(nodes)), root=root)
    return story


def row_of(panel: StoryListPanel, story_id: str):
    return next(
        panel.list.item(row)
        for row in range(panel.list.count())
        if panel.list.item(row).data(Qt.UserRole) == story_id
    )


def order(panel: StoryListPanel) -> list[str]:
    return [panel.list.item(row).data(Qt.UserRole) for row in range(panel.list.count())]


def shown(panel: StoryListPanel) -> list[str]:
    return [story_id for story_id in order(panel) if not row_of(panel, story_id).isHidden()]


def wait_search(app, panel: StoryListPanel, seconds: float = 10) -> None:
    panel._search_wait.stop()
    panel._start_search()
    end = time.monotonic() + seconds
    while panel._thread is not None and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)
    assert panel._thread is None, "the search ended"
    app.processEvents()


# --- the rows ------------------------------------------------------------------------


def test_a_row_says_its_kind_when_it_was_started_and_when_it_was_last_played(app, tmp_path):
    first = message("n1", "One.", "2026-02-03T10:00:00+00:00")
    last = message("n2", "Two.", "2026-03-04T10:00:00+00:00", parent="n1")
    save(tmp_path, "plain", "Plain", first, last)
    save(tmp_path, "plotted", "Plotted", plot=Plot())
    save(tmp_path, "simple", "Simple one", message("n1", "Hi."), mode="chat")
    panel = StoryListPanel(root=tmp_path)
    panel.refresh()
    assert row_of(panel, "plain").text().split("\n") == [
        "Plain",
        "Standard · 2 messages",
        "Started 3 Feb 2026 · Played 4 Mar 2026",
    ]
    assert row_of(panel, "plotted").text().split("\n")[1] == "With Plot · Not begun"
    assert row_of(panel, "plotted").text().split("\n")[2].startswith("Made ")
    assert row_of(panel, "simple").text().split("\n")[1] == "Simple · 1 message"
    panel.deleteLater()


def test_a_narrow_row_wraps_between_its_parts_and_a_wide_one_doesnt(app, tmp_path):
    needs_real_fonts()
    save(tmp_path, "s", "Story", message("n1", "One."))
    panel = StoryListPanel(root=tmp_path)
    panel.refresh()
    index = panel.list.indexFromItem(row_of(panel, "s"))
    _title, meta = _StoryRow._parts(index)
    font = panel.font()
    assert _StoryRow._meta_lines(meta, font, 2000) == meta
    narrow = _StoryRow._meta_lines(meta, font, 140)
    assert "Standard" in narrow[0] and len(narrow) > len(meta)
    assert all(not line.startswith(" · ") and not line.endswith(" · ") for line in narrow)
    panel.deleteLater()


def test_a_story_on_its_own_models_is_marked_with_what_they_are(app, tmp_path):
    config = Config(
        providers=[ProviderConfig(name="p", base_url="https://e.example/v1", model="m/settings")],
        active_provider_name="p",
    )
    own = Story(id="own", title="Own")
    own.defaults.scene_model = "o/scene"
    save_story_bundle(StoryBundle(story=own), root=tmp_path)
    same = Story(id="same", title="Same")
    same.defaults.main_model = "m/settings"  # an old story, on the default
    save_story_bundle(StoryBundle(story=same), root=tmp_path)
    save(tmp_path, "none", "None")
    panel = StoryListPanel(root=tmp_path)
    panel.use_config(lambda: config)
    panel.refresh()
    assert OWN_MODELS in row_of(panel, "own").text()
    assert "Scene read: o/scene" in row_of(panel, "own").toolTip()
    assert OWN_MODELS not in row_of(panel, "same").text()
    assert OWN_MODELS not in row_of(panel, "none").text()
    # Settings moving on makes it one: it stays where it was, unlike theirs.
    config.providers[0].model = "m/later"
    panel.refresh()
    assert OWN_MODELS in row_of(panel, "same").text()
    assert OWN_MODELS not in row_of(panel, "none").text()
    panel.deleteLater()


# --- the order -----------------------------------------------------------------------


def test_stories_sort_by_when_they_were_last_played_or_by_title(app, tmp_path):
    save(tmp_path, "b", "banana", message("n1", "x", "2026-03-01T00:00:00+00:00"))
    save(tmp_path, "a", "Apple", message("n1", "x", "2026-01-01T00:00:00+00:00"))
    # Its settings changed last, but it was played first of all.
    save(
        tmp_path,
        "c",
        "Cherry",
        message("n1", "x", "2026-02-01T00:00:00+00:00"),
        updated_at="2026-09-01T00:00:00+00:00",
    )
    (tmp_path / "stories" / "broken").mkdir()
    (tmp_path / "stories" / "broken" / "story.json").write_text("{not", encoding="utf-8")
    panel = StoryListPanel(root=tmp_path)
    panel.refresh()
    assert order(panel)[:3] == ["b", "c", "a"]
    kept: list[str] = []
    panel.sort_changed.connect(kept.append)
    panel.sort.setCurrentIndex(panel.sort.findData("title"))
    assert order(panel) == ["a", "b", "c", "broken"], "ignoring case; what can't be read last"
    assert kept == ["title"]
    assert [recent.story_id for recent in panel.recent(3)] == ["b", "c", "a"], "not the start page"
    panel.deleteLater()


def test_the_chosen_order_is_kept_for_next_time(app, tmp_path):
    save(tmp_path, "s", "Story")
    window = MainWindow(root=tmp_path, use_mock=True)
    window.stories.sort.setCurrentIndex(window.stories.sort.findData("title"))
    assert window.config.stories_sort == "title"
    window.close()
    again = MainWindow(root=tmp_path, use_mock=True)
    assert again.stories.sort.currentData() == "title"
    again.close()


# --- the search ----------------------------------------------------------------------


def test_the_search_filters_the_list_and_shows_where_the_words_are(app, tmp_path):
    save(tmp_path, "yes", "Yes", message("n1", "The watchtower stood alone on the plain."))
    save(tmp_path, "also", "Also", world_bible="A watchtower overlooks everything.")
    save(tmp_path, "no", "No", message("n1", "Rain."))
    panel = StoryListPanel(root=tmp_path)
    panel.refresh()
    panel.search.setText("WATCHTOWER")
    wait_search(app, panel)
    assert sorted(shown(panel)) == ["also", "yes"]
    assert panel.search_note.text() == "2 stories with “WATCHTOWER”"
    _title, meta = _StoryRow._parts(panel.list.indexFromItem(row_of(panel, "yes")))
    assert meta[-1].startswith("Story: ") and "watchtower" in meta[-1]
    _title, meta = _StoryRow._parts(panel.list.indexFromItem(row_of(panel, "also")))
    assert meta[-1].startswith("World: ")

    panel.search.setText("zebra")
    wait_search(app, panel)
    assert shown(panel) == []
    assert panel.search_note.text() == "No story has “zebra”."

    panel.search.clear()
    assert sorted(shown(panel)) == ["also", "no", "yes"], "cleared, every story is back"
    assert panel.search_note.isHidden()
    _title, meta = _StoryRow._parts(panel.list.indexFromItem(row_of(panel, "yes")))
    assert not any("watchtower" in line for line in meta)
    panel.deleteLater()


def test_results_stand_through_a_refresh_and_an_old_search_is_ignored(app, tmp_path):
    save(tmp_path, "yes", "Yes", message("n1", "A watchtower."))
    save(tmp_path, "no", "No", message("n1", "Rain."))
    panel = StoryListPanel(root=tmp_path)
    panel.refresh()
    panel.search.setText("watchtower")
    wait_search(app, panel)
    panel.refresh(selected_id="yes")
    assert shown(panel) == ["yes"]
    stale = panel._search
    panel.search.setText("rain")
    wait_search(app, panel)
    from sealedlore.storage.story_search import StoryHit

    panel._on_found(stale, StoryHit("yes", "Story", "stale", "stale"))
    assert shown(panel) == ["no"]
    panel.deleteLater()


def test_opening_a_result_goes_to_the_words_in_the_story(app, tmp_path):
    save(
        tmp_path, "hit", "Hit", message("n1", "Rain."), message("n2", "A watchtower.", parent="n1")
    )
    save(tmp_path, "title", "Watchtower Days", message("n1", "Rain."))
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.stories_list = True
    window.show()
    window.stories.search.setText("watchtower")
    wait_search(app, window.stories)
    window.stories._activate(row_of(window.stories, "hit"))
    assert window.session.story.id == "hit"
    assert window.find_open() and window.find_bar.field.text() == "watchtower"
    QTest.qWait(300)  # the bar waits for the last key
    assert window._matches, "Find has it"
    # A title has nothing for Find to go to, and it isn't opened on nothing.
    window.close_find()
    window.stories._activate(row_of(window.stories, "title"))
    assert window.session.story.id == "title" and not window.find_open()
    window.close()


def test_closing_the_window_ends_a_search_under_way(app, tmp_path):
    for n in range(20):
        save(tmp_path, f"s{n}", f"S{n}", message("n1", "Rain."))
    window = MainWindow(root=tmp_path, use_mock=True)
    window.stories.search.setText("rain")
    window.stories._start_search()
    thread = window.stories._thread
    window.close()
    assert thread is None or not thread.is_alive()


# --- Setup's Models tab --------------------------------------------------------------


def choices(**settings) -> ModelChoices:
    config = Config(
        providers=[ProviderConfig(name="p", base_url="https://e.example/v1", model="m/settings")],
        active_provider_name="p",
        **settings,
    )
    return ModelChoices(config)


def tab_names(dialog: SetupDialog) -> list[str]:
    return [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]


def test_setup_lists_the_story_s_models_blank_with_the_settings_in_grey(app):
    story = Story(title="T")
    dialog = SetupDialog(story, [], models=choices(scene_model="s/scene"))
    assert "Models" in tab_names(dialog)
    fields = dialog.model_fields
    assert set(fields) == {"story", "summarisation", "authoring", "scene", "lore", "image_prompt"}
    assert fields["story"].text() == "" and fields["story"].placeholderText() == "m/settings"
    assert fields["scene"].placeholderText() == "s/scene"
    assert fields["authoring"].placeholderText() == "m/settings", "its fallback"
    assert dialog.models_note.text() == ALL_IN_SETTINGS and not dialog.reset_models.isEnabled()
    dialog.deleteLater()


def test_the_plot_model_is_offered_only_with_a_plot(app):
    dialog = SetupDialog(Story(title="T", plot=Plot()), [], models=choices())
    assert "plot" in dialog.model_fields
    dialog.deleteLater()


def test_chosen_models_are_saved_on_save_and_reset_goes_back_to_the_settings(app):
    story = Story(title="T")
    story.defaults.model_routes["scene"] = ModelRoute(priority="price")
    story.defaults.scene_model = "o/old-scene"
    dialog = SetupDialog(story, [], models=choices())
    fields = dialog.model_fields
    assert fields["scene"].text() == "o/old-scene"
    assert dialog.models_note.text() == SOME_OF_ITS_OWN and dialog.reset_models.isEnabled()
    fields["story"].setText("o/story")
    fields["lore"].setText("o/lore")
    assert story.defaults.main_model is None, "nothing before Save"
    dialog._save()
    assert story.defaults.main_model == "o/story" and story.defaults.lore_model == "o/lore"
    dialog.deleteLater()

    dialog = SetupDialog(story, [], models=choices())
    dialog.reset_models.click()
    assert all(not field.text() for field in dialog.model_fields.values())
    assert dialog.models_note.text() == ALL_IN_SETTINGS
    assert story.defaults.main_model == "o/story", "Cancel still cancels"
    dialog._save()
    assert story.defaults.own_models() == {} and story.defaults.model_routes == {}
    dialog.deleteLater()


def test_a_chat_chooses_its_own_three_and_always_has_a_model(app):
    chat = Story(title="T", mode="chat")
    chat.defaults.main_model = "m/chat"
    dialog = SetupDialog(chat, [], models=choices())
    assert set(dialog.model_fields) == {"story", "summarisation", "image_prompt"}
    dialog.reset_models.click()
    dialog._save()
    assert chat.defaults.main_model == "m/settings", "today's default, its own from here"
    dialog.deleteLater()


def test_a_tee_chat_has_no_models_tab_every_call_is_its_own_model_s(app):
    chat = Story(title="T", mode="chat")
    chat.defaults.main_model = "TEE/glm-5.3"
    dialog = SetupDialog(chat, [], models=choices())
    assert tab_names(dialog) == ["About", "System prompt"]
    dialog.deleteLater()


# --- the window ---------------------------------------------------------------------


@pytest.fixture
def window(app, tmp_path: Path):
    config = Config(
        providers=[ProviderConfig(name="p", base_url="https://e.example/v1", model="m/settings")],
        active_provider_name="p",
    )
    save_config(config, root=tmp_path)
    save(tmp_path, "s", "Story", message("n1", "Rain."))
    window = MainWindow(root=tmp_path, use_mock=True)
    window.show()
    window.open_story("s")
    settle(app)
    yield window
    window.close()


def test_the_model_button_is_this_story_s_alone(app, window, monkeypatch, tmp_path):
    monkeypatch.setattr(window, "_browse_models", lambda *_a, **_k: "o/story")
    window.choose_story_model()
    assert window.session.model == "o/story"
    assert window.config.active_provider().model == "m/settings", "the settings are untouched"
    assert OWN_MODELS in row_of(window.stories, "s").text()
    assert "this story's own" in window.model_button.toolTip()
    assert load_story_bundle("s", root=tmp_path).story.defaults.main_model == "o/story"

    # Choosing the settings' own model is going back to them.
    monkeypatch.setattr(window, "_browse_models", lambda *_a, **_k: "m/settings")
    window.choose_story_model()
    assert window.session.story.defaults.main_model is None
    assert OWN_MODELS not in row_of(window.stories, "s").text()
    assert "as in Settings" in window.model_button.toolTip()


def test_saving_setup_with_a_model_marks_the_story_and_sends_to_it(app, window, monkeypatch):
    def fill(dialog):
        dialog.model_fields["story"].setText("o/story")
        dialog._save()
        return SetupDialog.Accepted

    monkeypatch.setattr(SetupDialog, "exec", fill)
    assert window.open_setup()
    assert window.session.model == "o/story"
    assert window.config.active_provider().model == "m/settings"
    assert OWN_MODELS in row_of(window.stories, "s").text()


def test_a_story_following_the_settings_takes_their_route_and_its_own_keeps_its_own(
    app, window, monkeypatch
):
    fast = ModelRoute(priority="latency")
    window._set_story_route(fast)
    assert window.config.model_routes["story"] == fast, "for every story following"
    window.session.story.defaults.main_model = "o/story"
    window._set_story_route(None)
    assert window.config.model_routes["story"] == fast, "its own model's route is its own"
    cheap = ModelRoute(priority="price")
    window._set_story_route(cheap)
    assert window.session.story.defaults.model_routes["story"] == cheap
    assert window.config.model_routes["story"] == fast


def test_a_writer_picked_for_a_picture_is_kept_where_the_model_was(app, window):
    session = window.session
    assert session.keep_prompt_writer("m/writer") is True
    assert session.config.image_prompt_model == "m/writer"
    session.story.defaults.image_prompt_model = "o/writer"
    assert session.prompt_writer_chosen() == "o/writer"
    assert session.keep_prompt_writer("o/another") is False
    assert session.story.defaults.image_prompt_model == "o/another"
    assert session.config.image_prompt_model == "m/writer"


def test_settings_leaves_an_open_story_s_models_alone(app, window, tmp_path):
    from sealedlore.gui.settings_dialog import SettingsDialog

    story = window.session.story
    story.defaults.scene_model = "o/scene"
    dialog = SettingsDialog(window.config, story=story)
    dialog.model.setText("m/new-settings")
    dialog.summarization_model.setText("s/sum")
    dialog._save()
    assert window.config.active_provider().model == "m/new-settings"
    assert window.config.summarization_model == "s/sum"
    assert story.defaults.main_model is None and story.defaults.summarization_model is None
    assert window.session.model == "m/new-settings", "it follows"
    assert story.defaults.scene_model == "o/scene"
    dialog.deleteLater()
