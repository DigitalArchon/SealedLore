"""The map is withdrawn while it is redesigned (`models/story.MAPS_OFFERED`,
the author, Oct 24 2026): nothing offers one, a story saved with one plays on
as a story without it (its choice kept), and the text-only record that may
take its place is measured first. Every other test runs with maps offered
(conftest `_maps_offered`); these run as the app ships."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from sealedlore.engine.world import SECTION_RECORD_HEADING, render_place_record  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.new_chat_dialog import NewChatDialog  # noqa: E402
from sealedlore.gui.new_story_guide import NewStoryGuide  # noqa: E402
from sealedlore.gui.setup_dialog import SetupDialog  # noqa: E402
from sealedlore.models.node import NodeMeta  # noqa: E402
from sealedlore.models.story import keeps_map  # noqa: E402
from sealedlore.models.world import (  # noqa: E402
    WorldDelta,
    WorldLink,
    WorldMap,
    WorldPlace,
    WorldThing,
)
from sealedlore.storage.library import story_listings  # noqa: E402
from sealedlore.storage.paths import sample_stories  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402
from tests.test_area import make_session, turn  # noqa: E402

pytestmark = pytest.mark.maps_withdrawn


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _world() -> WorldMap:
    farm = WorldPlace(
        id="farm", name="The farm", kind="site", what="A snowbound farm.", x=0, y=0, size=400
    )
    house = WorldPlace(
        id="house", name="The farmhouse", kind="building", inside="farm", x=10, y=20, size=20
    )
    barn = WorldPlace(
        id="barn", name="The barn", kind="building", inside="farm", state="burnt out", x=80, y=0
    )
    town = WorldPlace(
        id="town", name="Hellsville", kind="settlement", what="A walled town.", x=0, y=2000
    )
    region = WorldPlace(id="cell", name="North Woodlot", kind="region", x=0, y=400)
    return WorldMap(
        places={p.id: p for p in (farm, house, barn, town, region)},
        links=[WorldLink(a="farm", b="town", direction="north", distance="an hour's walk")],
        where={"Serrik Vaun": "house", "Maela Orr": "town", "Captain Idris": "cell"},
        things={
            "t1": WorldThing(id="t1", name="the rifle", carried_by="Serrik Vaun"),
            "t2": WorldThing(id="t2", name="the anvil", at="barn", state="broken"),
        },
        outside=["Maela Orr"],
    )


def test_the_record_is_text_only_and_leaves_the_designer_s_regions_out():
    text = render_place_record(_world(), "Serrik Vaun", exclude={"cell"})
    assert text.startswith(SECTION_RECORD_HEADING)
    assert "Serrik Vaun is at: The farmhouse, in The farm" in text
    assert "- The farm (site): A snowbound farm. Inside it: The farmhouse, The barn." in text
    assert "- The barn (building, in The farm): Now: burnt out." in text
    assert "- from The farm to Hellsville: north, an hour's walk" in text
    assert "- Maela Orr: just outside Hellsville" in text
    assert (
        "- the rifle: carried by Serrik Vaun" in text and "- the anvil: at The barn, broken" in text
    )
    # Nothing the program worked out, and nothing of the designer's.
    assert not re.search(r"\d+(\.\d+)? ?k?m\b", text) and "North Woodlot" not in text
    assert "Captain Idris" not in text  # stood in an excluded region
    assert render_place_record(WorldMap(), "Serrik Vaun") == ""


def test_the_record_goes_in_the_map_s_place_only_when_asked(tmp_path: Path, story, cast):
    session = make_session(tmp_path, story, cast, ["Quiet.", "{}"])
    assert not session.map_on and keeps_map(story.defaults) is False
    assert story.defaults.world_map  # the choice is kept for the day maps return
    list(session.send(turn("I look around.")))
    told = [r for r in session.provider.requests if "REMEMBER:" in r.messages[-1].text]
    tail = told[-1].messages[-1].text
    assert "# THE MAP HERE" not in tail and SECTION_RECORD_HEADING not in tail
    assert "Location: The undercroft beneath Calder Keep" in tail
    session.config.place_record = True
    list(session.send(turn("I wait.")))
    told = [r for r in session.provider.requests if "REMEMBER:" in r.messages[-1].text]
    tail = told[-1].messages[-1].text
    assert SECTION_RECORD_HEADING in tail and "# THE MAP HERE" not in tail
    assert "Places the story has established:" in tail


def test_a_story_saved_with_a_map_opens_as_a_story_without_one(app, tmp_path: Path, story, cast):
    nodes = make_exchange(2)
    nodes[-1].meta = NodeMeta(
        world=WorldDelta(places=list(_world().places.values()), where={"Serrik Vaun": "house"})
    )
    story.defaults.world_map = True
    bundle = StoryBundle(story=story, cast=cast, nodes=nodes)
    bundle.story.active_leaf_id = nodes[-1].id
    save_story_bundle(bundle, root=tmp_path)
    assert not story_listings(tmp_path)[0].keeps_map
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(story.id)
    session = window.session
    assert not session.map_on and session.story.defaults.world_map
    assert window.world_button.isHidden() and not window.world_shown()
    assert window.right_tabs.currentWidget() is window.people_page
    assert not window.without_map_action.isVisible()
    assert session.map_context(session.path(), "char-serrik") == ""
    window.close()


def test_nothing_offers_a_map(app, tmp_path: Path, story, cast):
    window = MainWindow(root=tmp_path, use_mock=True)
    setup = SetupDialog(story, cast, parent=window)
    assert not setup.world_map.isVisibleTo(setup)
    assert not setup.map_density.isVisibleTo(setup) and not setup.scarce.isVisibleTo(setup)
    guide = NewStoryGuide(window, model="anthropic/claude-sonnet-4.6")
    assert not guide.map_box.isVisibleTo(guide)
    chat = NewChatDialog(model="z-ai/glm-5.3", parent=window)
    assert not chat.world_map.isVisibleTo(chat) and not chat.chat_character.isVisibleTo(chat)
    window.close()


def test_the_sample_that_exists_for_its_map_is_not_offered(tmp_path: Path):
    for name in ("Doomsville.md", "Doomsville with a map.md", "Brasshaven.md"):
        (tmp_path / name).write_text("# x\n", encoding="utf-8")
    assert [name for name, _ in sample_stories(tmp_path)] == ["Brasshaven", "Doomsville"]
