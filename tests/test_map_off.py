"""Continue without the map (the author, Oct 19 2026): someone enjoying a
story played on its map who no longer wants to pay for the map goes on from
where they are, as a story of its own without one: every character, the
plot, the world and lore, every branch and the story so far, and the scene as
the map had it. The map story stays as it was."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.area import ThingRecord
from sealedlore.models.world import WorldDelta
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import (
    StoryBundle,
    copy_without_map,
    load_story_bundle,
    save_story_bundle,
    story_dir,
)
from sealedlore.storage.world_art import save_world_art
from tests.test_area import make_session, turn, world_and_art

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _played(tmp_path, story, cast) -> StorySession:
    """A map story one passage in: Serrik in the house with a storm lantern,
    Maela away in Hellsville."""
    story.scene.present_character_ids = ["char-serrik", "char-maela"]
    session = make_session(tmp_path, story, cast, ["Serrik waited by the stove.", "{}"])
    session.story.world_base.where.update({"Serrik Vaun": "house", "Maela Orr": "town"})
    list(session.send(turn("I wait by the stove.")))
    state = session.area_state().model_copy(deep=True)
    state.things["lantern"] = ThingRecord(state="taken", name="a storm lantern", by="Serrik Vaun")
    session._set_live(state)
    return session


def test_the_card_for_going_on_without_the_map_is_the_map_s(tmp_path, story, cast):
    session = _played(tmp_path, story, cast)
    scene = session.scene_without_map()
    assert scene.location == "The house, in The farm"
    assert "char-maela" not in scene.present_character_ids  # the map has her in Hellsville
    note = next(e.note for e in scene.offstage_but_nearby if e.character_id == "char-maela")
    assert note == "at Hellsville"
    assert "Serrik Vaun carries: storm lantern." in (scene.situation or "")
    assert session.story.scene.location != scene.location  # the story's own card untouched
    # The card's own Location stays when it names where the map has them:
    # it is often finer than the map's places.
    session.story.scene.location = "the back kitchen of the house"
    assert session.scene_without_map().location == "the back kitchen of the house"


def test_the_copy_goes_on_without_the_map_and_the_map_story_stays(tmp_path, story, cast):
    session = _played(tmp_path, story, cast)
    scene = session.scene_without_map()
    session.save()
    copy = copy_without_map(story.id, title="The Sundering (without the map)", scene=scene,
                            root=tmp_path)  # fmt: skip
    again = load_story_bundle(copy.story.id, root=tmp_path)
    assert not again.story.defaults.world_map
    assert again.story.title == "The Sundering (without the map)"
    leaf = next(n for n in again.nodes if n.id == again.story.active_leaf_id)
    assert leaf.meta.scene.location == "The house, in The farm"
    assert leaf.meta.scene.set_at_node_id == leaf.id
    assert [n.content for n in again.nodes] == [n.content for n in session.nodes]
    assert again.cast == session.bundle.cast
    # The map's record stays, unused: Keep a map brings it back.
    assert (story_dir(copy.story.id, tmp_path) / "world_art.json").is_file()
    original = load_story_bundle(story.id, root=tmp_path)
    assert original.story.defaults.world_map and original.story.scene.location != scene.location

    # A turn in the copy: the passage alone, no map read, no map in the prompt.
    provider = MockChatProvider(["Serrik turned the lantern down."])
    copied = StorySession(
        again,
        session.config,
        provider,
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    assert not copied.map_on
    list(copied.send(turn("I turn the lantern down.")))
    assert len(provider.requests) == 1
    sent = provider.requests[0].messages[-1].text
    assert "# THE MAP HERE" not in sent and "The house, in The farm" in sent
    assert "storm lantern" in sent


def test_story_continue_without_the_map_makes_and_opens_the_copy(
    tmp_path: Path, story, cast, monkeypatch
):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QMessageBox

    from sealedlore.gui import main_window

    app = QApplication.instance() or QApplication([])
    world, art = world_and_art()
    story.defaults.world_map = True
    story.world_base = WorldDelta(places=list(world.places.values()), seen=list(world.seen))
    story.world_start = "house"
    story.held_character_id = "char-serrik"
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    save_world_art(story.id, art, tmp_path)
    window = main_window.MainWindow(root=tmp_path, use_mock=True)
    window.config.map_art = False
    window.open_story(story.id)
    app.processEvents()
    assert window.without_map_action.isVisible() and window.without_map_action.isEnabled()
    monkeypatch.setattr(main_window.QMessageBox, "question", lambda *a, **k: QMessageBox.Ok)
    window.without_map_action.trigger()
    app.processEvents()
    assert window.session.story.id != story.id
    assert window.session.story.title == "The Sundering (without the map)"
    assert not window.session.map_on
    assert not window.without_map_action.isVisible()  # nothing to leave behind now
    assert load_story_bundle(story.id, root=tmp_path).story.defaults.world_map
    window.close()
