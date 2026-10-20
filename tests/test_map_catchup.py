"""An older map story opened again (the author, Oct 19 2026): Doomsville with
a map, played on Oct 16-17, drew 33 map pictures in two minutes of being
opened, the catch-up of everything added since. Fixed: a double-click opened
the story twice (the second cancelling what the first had sent); "wooden
chair" was drawn anew beside the set's "a wooden chair"; the set's own pieces
now stand for a furnisher's names they suit; and drawing automatically asks
first when more than `Config.map_pictures_ask_over` (10) are wanted at once.
Offscreen, mocks only."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.engine.tile_sets import item_slugs, suitable_item  # noqa: E402
from sealedlore.engine.world_pictures import PictureJob  # noqa: E402
from sealedlore.models.config import Config  # noqa: E402
from sealedlore.models.story import Story  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.test_area import make_session  # noqa: E402
from tests.test_map_drawing import _pictures_on  # noqa: E402

DOOMSVILLE_ITEMS = sorted(
    path.stem
    for path in (
        Path(__file__).resolve().parents[1] / "SampleStories/tilesets/doomsville/items"
    ).glob("*")
)


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def double_click(widget, at: QPoint) -> None:
    """A double-click as the system sends one: a click, then the second
    press as a double-click, and its release (QTest's own sends the
    double-click alone)."""
    QTest.mousePress(widget, Qt.LeftButton, Qt.NoModifier, at)
    QTest.mouseRelease(widget, Qt.LeftButton, Qt.NoModifier, at)
    QTest.mouseDClick(widget, Qt.LeftButton, Qt.NoModifier, at)
    QTest.mouseRelease(widget, Qt.LeftButton, Qt.NoModifier, at)


# --- a double-click opens a story once ---------------------------------------------------


def test_a_double_click_in_the_stories_list_opens_the_story_once(app, tmp_path):
    from sealedlore.gui.stories import StoryListPanel

    save_story_bundle(StoryBundle(story=Story(title="One"), cast=[]), root=tmp_path)
    panel = StoryListPanel(tmp_path)
    panel.refresh()
    panel.resize(300, 400)
    panel.show()
    app.processEvents()
    opened: list[str] = []
    panel.story_activated.connect(opened.append)
    item = panel.list.item(0)
    double_click(panel.list.viewport(), panel.list.visualItemRect(item).center())
    app.processEvents()
    assert opened == [item.data(Qt.UserRole)]
    panel.close()


def test_a_double_click_on_a_start_page_card_chooses_it_once(app):
    from sealedlore.gui.start_page import Card

    card = Card("Doomsville", "Four years after the end of the world.")
    card.resize(240, 120)
    card.show()
    app.processEvents()
    chosen: list[bool] = []
    card.activated.connect(lambda: chosen.append(True))
    double_click(card, card.rect().center())
    assert chosen == [True]
    QTest.mouseClick(card, Qt.LeftButton, Qt.NoModifier, card.rect().center())
    assert chosen == [True, True]  # a click after it is a choice again
    # A double-click whose release went elsewhere never eats the next click.
    QTest.mouseDClick(card, Qt.LeftButton, Qt.NoModifier, card.rect().center())
    QTest.mouseClick(card, Qt.LeftButton, Qt.NoModifier, card.rect().center())
    assert chosen == [True, True, True]
    card.close()


# --- the set's own pieces ----------------------------------------------------------------


def test_a_thing_s_picture_is_found_with_or_without_its_article():
    assert item_slugs("wooden chair") == ["wooden-chair", "a-wooden-chair", "the-wooden-chair"]
    assert item_slugs("an armchair")[:2] == ["an-armchair", "armchair"]
    assert "a-wooden-chair" in DOOMSVILLE_ITEMS


@pytest.mark.parametrize(
    ("name", "piece"),
    [
        # The same kind of thing, described more or less.
        ("workbench", "a-heavy-wooden-workbench"),
        ("armchair", "a-worn-wooden-armchair"),
        ("cast iron stove", "a-cast-iron-wood-stove"),
        ("low coffee table", "a-low-pine-coffee-table"),
        ("hanging drying rack", "a-wooden-drying-rack"),
        ("worn couch by the hearth", "couch-with-patched-cushions"),
        # Not another kind of the thing ("rifle rack", a "hydroponic planter
        # bed" for a bed), nor of another material, state or contents.
        ("leaning rifle rack", None),
        ("small bedside crate", None),
        ("crates of salvaged timber", None),
        ("wardrobe with missing door", None),
        ("broken chair", None),
        ("a filleting knife", None),
    ],
)
def test_a_piece_of_the_set_stands_for_a_name_it_suits(name, piece):
    assert suitable_item(name, DOOMSVILLE_ITEMS) == piece


def test_suitable_means_the_same_kind_material_and_state():
    pieces = ["a-hydroponic-planter-bed", "a-simple-wooden-bed", "a-broken-wooden-crate"]
    assert suitable_item("a wooden bed", pieces) == "a-simple-wooden-bed"
    assert suitable_item("a metal bed", ["a-simple-wooden-bed"]) is None
    assert suitable_item("a crate", pieces) is None  # broken, and this one isn't
    assert suitable_item("a broken crate", pieces) == "a-broken-wooden-crate"
    assert suitable_item("温泉", pieces) is None


WORKSHOP = json.dumps(
    {
        "rooms": [
            {
                "room": 0,
                "purpose": "workshop",
                "items": [
                    {"name": "workbench", "w": 2, "h": 1, "where": "wall"},
                    {"name": "a weaving loom", "w": 2, "h": 1, "where": "wall"},
                ],
            }
        ]
    }
)


def test_a_piece_the_set_suits_is_not_drawn_and_the_play_view_shows_that_piece(
    app, tmp_path, story, cast
):
    from sealedlore.gui.area_render import TileSource

    session = make_session(tmp_path, story, cast, [WORKSHOP] * 3)
    _pictures_on(session)
    session.set_tile_set("doomsville")
    furnish, _pictures = session.tile_art_jobs()
    session._tile_run([a for a in furnish if a.id == "house"], [], None, session.provider)
    _furnish, pictures = session.tile_art_jobs()
    keys = {job.key for job in pictures}
    assert "item:a-weaving-loom" in keys  # no set has one: drawn
    assert "item:workbench" not in keys  # the set's own stands for it
    info, read = session.tile_set()
    source = TileSource(info, read)
    shown = source.item("workbench")
    assert shown is not None and shown is source.image("items/a-heavy-wooden-workbench")


# --- asking first when too many are wanted at once ---------------------------------------


def jobs(*keys: str) -> list[PictureJob]:
    return [PictureJob(key=k, subject="item", subject_id=k, name=k, prompt="p") for k in keys]


def keys_of(found) -> list[str]:
    return [getattr(item, "job", item).key for item in found]


def test_drawing_automatically_asks_first_when_more_are_wanted_at_once(
    tmp_path, story, cast, monkeypatch
):
    """ "More than N at once" counts what this run wants with everything let
    through unasked lately (the last ten minutes, since the author last
    answered): live (Oct 20 2026) a sample's map drew 30 unasked in runs of
    3, 10, 1, 3, 5, 6 and 2 a minute apart, each under the limit on its own."""
    from sealedlore.engine import session_map_approval

    clock = [1000.0]
    monkeypatch.setattr(session_map_approval.time, "monotonic", lambda: clock[0])
    session = make_session(tmp_path, story, cast)
    _pictures_on(session)
    session.config.map_pictures_ask_over = 3
    # Three at once are drawn as they come.
    assert keys_of(session._held_for_approval("tile", jobs("a", "b", "c"))) == ["a", "b", "c"]
    assert session.map_pictures_waiting() == []
    # A minute later two more are five within the window: they wait.
    clock[0] += 60
    assert session._held_for_approval("map", jobs("d", "e")) == []
    assert keys_of(session.map_pictures_waiting()) == ["d", "e"]
    # What waited for being too many waits on, even when fewer are wanted
    # now; what auto let through once is drawn again unasked, but a new one
    # beside it is a fourth within the window.
    assert session._held_for_approval("map", jobs("d", "e")) == []
    assert keys_of(session._held_for_approval("tile", jobs("a", "b", "c", "f"))) == ["a", "b", "c"]
    assert keys_of(session.map_pictures_waiting()) == ["d", "e", "f"]
    # Approved, they are drawn, a run's own cap at a time; the author's answer
    # starts the count afresh.
    session.approve_map_pictures(jobs("d", "e"), start=False)
    assert keys_of(session.map_pictures_waiting()) == ["f"]
    assert keys_of(session._held_for_approval("map", jobs("d", "e"), limit=1)) == ["d"]
    assert keys_of(session._held_for_approval("map", jobs("d", "e"))) == ["e"]
    # Declined are never drawn, asked or not.
    assert session._held_for_approval("map", jobs("g", "h", "i", "j")) == []
    session.decline_map_pictures(["g", "h", "i", "j"])
    assert keys_of(session._held_for_approval("map", jobs("g", "k"))) == ["k"]
    # Ten minutes on, the window has passed: three more go unasked.
    clock[0] += 601
    assert keys_of(session._held_for_approval("tile", jobs("l", "m", "n"))) == ["l", "m", "n"]
    # Any number: never asks.
    session.config.map_pictures_ask_over = 0
    many = [chr(c) for c in range(ord("o"), ord("z"))]
    assert keys_of(session._held_for_approval("tile", jobs(*many))) == many


def test_a_burst_of_small_runs_a_minute_apart_asks_as_one(tmp_path, story, cast, monkeypatch):
    """The live run: 3, 10, 1, 3, 5, 6 and 2 a minute apart, never asked.
    Now the second run is thirteen within the window with the first's three,
    and from then on the new ones wait with them."""
    from sealedlore.engine import session_map_approval

    clock = [0.0]
    monkeypatch.setattr(session_map_approval.time, "monotonic", lambda: clock[0])
    session = make_session(tmp_path, story, cast)
    _pictures_on(session)
    session.config.map_pictures_ask_over = 10
    drawn, n = 0, 0
    waiting: list[str] = []
    for size in (3, 10, 1, 3, 5, 6, 2):
        clock[0] += 60
        # A run wants what waited and what is new, never what it has drawn.
        new = [f"p{n + i}" for i in range(size)]
        n += size
        drawn += len(session._held_for_approval("tile", jobs(*waiting, *new)))
        waiting = keys_of(session.map_pictures_waiting())
    # The first three went; the ten were thirteen within the window and
    # waited, and new ones join what waits.
    assert drawn == 3 and len(waiting) == 27


def test_approvals_keep_until_drawn_and_the_day_s_limit_only_caps_the_run(tmp_path, story, cast):
    """The review (Oct 20 2026): approvals beyond the day's room were dropped
    as "no longer wanted", and at the day's limit every approval was deleted
    and the waiting line cleared with no word; the next day they were all
    waiting again."""
    session = make_session(tmp_path, story, cast)
    _pictures_on(session)
    session.config.map_pictures_mode = "ask"
    wanted = jobs(*[f"t{i}" for i in range(35)])
    assert session._held_for_approval("tile", wanted, limit=12) == []
    assert len(session.map_pictures_waiting()) == 35
    session.approve_map_pictures(wanted, start=False)
    assert len(session._pictures_approved) == 35 and session.map_pictures_waiting() == []
    # The day has room for 30: a run draws 12 of them, and 12, and 6; the 5
    # beyond the room stay approved. (A run wants what it hasn't drawn.)
    drawn = []
    for room in (30, 18, 6, 0):
        got = session._held_for_approval("tile", wanted, limit=12, room=room)
        drawn.append(len(got))
        wanted = [job for job in wanted if job not in got]
    assert drawn == [12, 12, 6, 0]
    assert len(session._pictures_approved) == 5 and session.map_pictures_waiting() == []
    # At the limit the note says so; with room, nothing to say.
    session.config.map_picture_daily_limit = 0
    assert session.map_pictures_room_note() == (
        "5 approved map pictures wait for tomorrow: today's 0 are drawn "
        "(Settings → Images sets the day's limit)."
    )
    assert not session.map_pictures_approved_pending  # no run to start today
    session.config.map_picture_daily_limit = 40
    assert session.map_pictures_room_note() == "" and session.map_pictures_approved_pending
    # Next day: the five are drawn first, nothing is asked again.
    assert keys_of(session._held_for_approval("tile", wanted, limit=12, room=40)) == [
        f"t{i}" for i in range(30, 35)
    ]
    assert session._pictures_approved == {} and session.map_pictures_waiting() == []
    # An approval for a picture the map no longer wants (with room to draw it) goes.
    more = jobs("u0", "u1", "u2")
    session._held_for_approval("tile", more, limit=12)
    session.approve_map_pictures(more, start=False)
    assert session._held_for_approval("tile", more[1:], limit=12, room=40) == more[1:]
    assert session._pictures_approved == {}


def test_what_waited_under_ask_goes_once_drawing_is_automatic(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast)
    _pictures_on(session)
    session.config.map_pictures_mode = "ask"
    assert session._held_for_approval("tile", jobs("a", "b")) == []
    assert keys_of(session.map_pictures_waiting()) == ["a", "b"]
    session.config.map_pictures_mode = "auto"
    assert session.map_pictures_waiting() == []  # only what the limit held back shows
    assert keys_of(session._held_for_approval("tile", jobs("a", "b"))) == ["a", "b"]


def test_the_map_page_says_why_pictures_wait(app):
    from sealedlore.gui.world_view import WorldView

    view = WorldView()
    view.set_waiting(12, 0.12, over=10)
    assert view.waiting_label.text() == (
        "12 map pictures wait for your approval (more than 10 at once; about $0.12)."
    )
    view.set_waiting(2, 0.02)
    assert view.waiting_label.text() == "2 map pictures wait for your approval (about $0.02)."


def test_settings_sets_how_many_are_drawn_without_asking(app):
    from sealedlore.gui.settings_dialog import SettingsDialog

    config = Config()
    assert config.map_pictures_ask_over == 10
    dialog = SettingsDialog(config, None)
    assert dialog.map_pictures_over.value() == 10 and dialog.map_pictures_over.isEnabled()
    dialog.map_pictures.setCurrentIndex(dialog.map_pictures.findData("ask"))
    assert not dialog.map_pictures_over.isEnabled()
    dialog.map_pictures.setCurrentIndex(dialog.map_pictures.findData("auto"))
    dialog.map_pictures_over.setValue(25)
    dialog._save()
    assert config.map_pictures_ask_over == 25 and config.map_pictures_mode == "auto"
    dialog.map_pictures_over.setValue(0)
    assert dialog.map_pictures_over.text() == "any number"
    dialog.deleteLater()


def test_settings_sets_how_much_the_world_designer_puts_in_a_region(app):
    """Settings → Models → Open country, beside the world designer's model
    (the review, Oct 20 2026)."""
    from sealedlore.gui.settings_dialog import SettingsDialog

    config = Config()
    dialog = SettingsDialog(config, None)
    assert dialog.open_country.currentData() == "normal"
    labels = [dialog.open_country.itemText(i) for i in range(dialog.open_country.count())]
    assert labels == ["Sparse", "Normal", "Rich"]
    dialog.open_country.setCurrentIndex(dialog.open_country.findData("rich"))
    dialog._save()
    assert config.open_country == "rich"
    dialog.deleteLater()


def test_the_map_page_never_says_drawn_over_pictures_that_wait(app):
    """With pictures waiting for approval the progress line read "✓ The map
    is drawn" right above the line saying they wait (the review, Oct 20
    2026)."""
    from sealedlore.gui.world_view import MAP_DRAWN, MAP_DRAWN_BUT_WAITING, WorldView

    view = WorldView()
    view.set_progress(0, 0)
    assert view.progress_label.text() == MAP_DRAWN
    view.set_waiting(2, 0.02)
    assert view.progress_label.text() == MAP_DRAWN_BUT_WAITING
    view.set_progress(0, 0)  # the next tick keeps it
    assert view.progress_label.text() == MAP_DRAWN_BUT_WAITING
    view.set_progress(1, 3)  # drawing again: the count, whatever waits
    view.set_waiting(2, 0.02)
    assert view.progress_label.text() == "Drawing the map: 1 of 3"
    view.set_progress(0, 0)
    view.set_waiting(0, None)
    assert view.progress_label.text() == MAP_DRAWN and view.waiting.isHidden()
