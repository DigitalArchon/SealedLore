"""Map-first play in the window (gui/area_view.py, gui/window_area.py,
gui/area_render.py). Offscreen."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.engine.area import build_area  # noqa: E402
from sealedlore.engine.prompt import TurnRequest  # noqa: E402
from sealedlore.engine.tile_sets import TileSetInfo  # noqa: E402
from sealedlore.gui.area_render import TileSource, cut_out, render_area  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.models.world import WorldDelta  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from sealedlore.storage.world_art import save_world_art  # noqa: E402
from tests.test_area import world_and_art  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def pump(app) -> None:
    for _ in range(5):
        app.processEvents()
        app.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.fixture
def window(app, tmp_path: Path, story, cast):
    world, art = world_and_art()
    story.defaults.world_map = True
    story.world_base = WorldDelta(places=list(world.places.values()), seen=list(world.seen))
    story.world_start = "house"
    story.held_character_id = "char-serrik"
    story.scene.present_character_ids = ["char-serrik", "char-maela"]
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    save_world_art(story.id, art, tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.map_art = False
    window.resize(1600, 900)
    window.open_story(story.id)
    pump(app)
    return window


def walk_all(app, window) -> None:
    view = window.area_view
    for _ in range(500):
        if not view.walking():
            break
        view._step()
    pump(app)


def test_a_map_story_opens_on_the_area_to_walk_on(window):
    assert window.world_shown() and window.world_view.play_shown()
    view = window.area_view
    assert view.area is not None and view.area.name == "The house"
    assert [w.name for w in view.walkers if w.held] == ["Serrik Vaun"]
    assert view.title.text() == "The house" and view.clock.text().startswith("Day 1")
    # Region is the map; Local is back to walking, and stays so when the map
    # is redrawn (it used to go back to the level fitted before).
    window.world_view.go_region()
    assert not window.world_view.play_shown()
    window.world_view.go_local()
    assert window.world_view.play_shown()
    window.composer.focus_input()
    window.refresh_world(keep_view=True)
    assert window.world_view.play_shown()
    assert window.focusWidget() is not window.area_view  # a redraw never takes the keyboard
    # Esc from the play view goes out to the region.
    window._on_escape()
    assert window.world_shown() and not window.world_view.play_shown()
    assert window.world_view.level() == "region"


def test_walking_into_the_door_goes_outside_and_a_menu_choice_fills_the_composer(app, window):
    session = window.session
    house = session.area("house")
    door = next(e for e in house.exits if e[1] == 23)
    window._area_walk(*door, False)
    walk_all(app, window)
    me = session.area_state().spots["Serrik Vaun"]
    assert me.area == "farm"  # through the door
    assert window.area_view.area.name == "The farm"
    well = next(p for p in session.area("farm").placements if "well" in p.name)
    window._area_action("use", well.name, well.id)
    walk_all(app, window)
    assert window.composer.text() == "I use the stone well."
    me = session.area_state().spots["Serrik Vaun"]
    assert abs(me.x - well.x) <= 1 and abs(me.y - well.y) <= 1  # walked up to it first
    window._area_action("look", "Maela Orr", "")
    assert window.composer.text().startswith("I keep out of sight and watch Maela Orr")


def test_walking_waits_while_the_story_is_being_written(app, window):
    session = window.session
    before = session.area_state().spots["Serrik Vaun"]
    window._busy = True
    window._sync_area_lock()
    window._area_walk(before.x + 1, before.y + 1, False)
    walk_all(app, window)
    assert session.area_state().spots["Serrik Vaun"] == before
    assert "being written" in window.area_view.hint.text()


def test_an_area_is_drawn_from_its_tile_set_and_a_missing_sprite_is_still_seen(app):
    from PySide6.QtCore import QBuffer, QIODevice

    from sealedlore.engine.area import Area, Doorway, find_rooms
    from sealedlore.gui.area_render import INDOOR_PX, OUTDOOR_PX, _room_masks, px_for

    world, art = world_and_art()
    farm = build_area(world, art, "farm")
    house = build_area(world, art, "house")
    plain = TileSource(TileSetInfo(name="plain"), lambda role: None)
    # Outdoors 24 px a tile (16 was soft at a 1080p screen's fit); indoors
    # the picture is a wall's face taller, so the south wall has a face too.
    assert (OUTDOOR_PX, INDOOR_PX) == (24, 40)
    for area in (farm, house):
        image = render_area(area, plain)
        assert not image.isNull()
        assert image.width() == area.width * px_for(area)
        extra = round(INDOOR_PX * 0.7) if area.indoor else 0
        assert image.height() == area.height * px_for(area) + extra
    # A sprite drawn on white comes out cut out of it.
    picture = QImage(64, 64, QImage.Format_RGB32)
    picture.fill(QColor("white"))
    for x in range(20, 40):
        for y in range(10, 50):
            picture.setPixelColor(x, y, QColor("#7d4a8f"))
    cut = cut_out(picture)
    assert cut is not None and (cut.width(), cut.height()) == (20, 40)
    assert (
        cut.pixelColor(0, 0).alpha() == 255
        and cut_out(QImage(8, 8, QImage.Format_RGB32)) is not None
    )
    # A texture drawn again or chosen for a shipped set (the reader's
    # `from_story`) is made to wrap like a story's own set's; a shipped one
    # is pre-made and kept as read. Half black, half white: wrapped, its
    # edge blends with the other half.
    texture = QImage(32, 32, QImage.Format_RGB32)
    texture.fill(QColor("black"))
    for x in range(16, 32):
        for y in range(32):
            texture.setPixelColor(x, y, QColor("white"))
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    texture.save(buffer, "PNG")
    data = bytes(buffer.data())

    def shipped(role: str) -> bytes | None:
        return data if role == "floor" else None

    def chosen(role: str) -> bytes | None:
        return data if role == "floor" else None

    chosen.from_story = lambda role: role == "floor"  # type: ignore[attr-defined]
    as_shipped = TileSource(TileSetInfo(name="doomsville"), shipped).image("floor")
    as_chosen = TileSource(TileSetInfo(name="doomsville"), chosen).image("floor")
    assert as_shipped.pixelColor(4, 16).red() == 0  # as read: still black near the edge
    assert 40 < as_chosen.pixelColor(4, 16).red() < 220  # blended with the white half
    # Fire, rubble and water indoors are drawn as such (the author's rule: a
    # barn blown up is drawn blown up; the play view showed its floor as
    # new), and a lamp's light stays in its room.
    rows = ["#" * 30]
    for y in range(1, 15):
        row = ["#"] + ["_"] * 28 + ["#"]
        if y == 7:
            row = ["#"] * 30
            row[5] = row[6] = "_"
        for x in range(1, 29):
            if y != 7 and 3 <= x <= 6 and 3 <= y <= 5:
                row[x] = "*"
            if y != 7 and 9 <= x <= 13 and 2 <= y <= 5:
                row[x] = ":"
            if y != 7 and 18 <= x <= 25 and 9 <= y <= 12:
                row[x] = "~"
        rows.append("".join(row))
    rows.append("#" * 30)
    doorways = [Doorway(5, 7, 6, 7, "h")]
    rooms, room_of = find_rooms(rows, {}, doorways)
    assert len(rooms) == 2
    burnt = Area(
        place_id="burnt",
        name="The barn",
        indoor=True,
        look="floor",
        rows=rows,
        tile_m=1.0,
        objects={},
        labels={},
        rooms=rooms,
        room_of=room_of,
        doorways=doorways,
    )
    image = render_area(burnt, plain, dress=False)
    px = INDOOR_PX

    def pixel(x: float, y: float) -> QColor:
        return image.pixelColor(int(x * px), int(y * px))

    floor = pixel(16.5, 4.5)
    water = pixel(21.5, 10.5)
    assert water.blue() > water.red() and water.blue() > floor.blue() + 30
    rubble = pixel(11.5, 3.5)
    assert abs(rubble.red() - floor.red()) + abs(rubble.blue() - floor.blue()) > 12
    flames = [
        image.pixelColor(x, y)
        for x in range(3 * px, 7 * px)
        for y in range(3 * px, 6 * px)
        if image.pixelColor(x, y).red() > 200 and image.pixelColor(x, y).blue() < 100
    ]
    assert len(flames) > 40  # orange flames stand on the scorched tiles
    mask_at = _room_masks(burnt, px, image.height(), image.width())
    upstairs = mask_at(3.5 * px, 3.5 * px)  # a light in the top room
    assert upstairs[int(4.5 * px), int(3 * px)] > 0.9  # its own room is lit
    assert upstairs[int(11.5 * px), int(3 * px)] < 0.05  # not the room below the wall
    assert mask_at(14.5 * px, 11.5 * px) is not upstairs  # the lower room has its own


def test_a_spot_marked_on_either_view_goes_into_the_turn_with_a_pin(app, window):
    from sealedlore.engine.marks import marks_in

    window.composer.input.setPlainText("A lantern burns at")
    cursor = window.composer.input.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    window.composer.input.setTextCursor(cursor)
    window._mark_tile(5, 5)
    text = window.composer.text()
    assert text.startswith("A lantern burns at ⌖(") and len(marks_in(text)) == 1
    pump(app)
    assert len(window.area_view._pins) == 1 and len(window.world_view._pins) == 1
    # The point under the pointer is shown as a mark is written.
    point = window.area_view.view.mapFromScene(window.area_view._pins[0].pos())
    window.area_view._hovered(window.area_view.view.mapToScene(point))
    assert window.area_view.coords.text() == marks_in(text)[0].token
    window._mark_point(400.0, 8000.0)  # on the region map
    assert len(marks_in(window.composer.text())) == 2
    pump(app)
    assert len(window.world_view._pins) == 2
    window.composer.input.setPlainText("")
    pump(app)
    assert window.area_view._pins == [] and window.world_view._pins == []


def test_go_here_walks_within_the_area_and_travels_to_another(app, window):
    session = window.session
    farm = session.area("farm")
    from sealedlore.engine.area import tile_point

    point = tile_point(session.world(), farm, 30, 50)
    window._go_point(*point)
    text = window.composer.text()
    assert text.startswith("I make my way to ⌖(")
    # Still in the house until the turn is sent; there once it is.
    assert session.area_state().spots["Serrik Vaun"].area == "house"
    session.provider.responses = ["She goes.", "{}"]
    list(
        session.send(
            TurnRequest(
                speaker_id="char-serrik", user_text=text, controlled_character_id="char-serrik"
            )
        )
    )
    window.refresh_area()
    me = session.area_state().spots["Serrik Vaun"]
    assert me.area == "farm" and window.area_view.area.name == "The farm"
    # Now in the farm: a spot in it is walked to.
    near = tile_point(session.world(), farm, 33, 50)
    window._go_point(*near)
    walk_all(app, window)
    me = session.area_state().spots["Serrik Vaun"]
    assert abs(me.x - 33) + abs(me.y - 50) <= 1


def test_me_centres_either_view_on_the_author_s_character(app, window):
    view = window.area_view
    view.view.scale(3, 3)
    view.view.centerOn(0, 0)
    view.centre_on_me()
    held = view.held()
    from sealedlore.gui.area_render import px_for

    px = px_for(view.area)
    middle = view.view.mapToScene(view.view.viewport().rect().center())
    assert abs(middle.x() - (held.x + 0.5) * px) < px * 2
    me = window.world_view.my_point()
    assert me is not None
    window.world_view.centre_on_me()
    middle = window.world_view.view.mapToScene(window.world_view.view.viewport().rect().center())
    assert abs(middle.x() - me[0]) < 50 and abs(-middle.y() - me[1]) < 50


def test_indoors_what_can_t_be_seen_is_shaded_and_someone_out_of_sight_is_hidden(app, window):
    from PySide6.QtCore import QPointF

    from sealedlore.engine.area import sees
    from sealedlore.gui.area_render import px_for
    from sealedlore.models.area import LiveArea, Spot

    view = window.area_view
    assert view.area.indoor and view._shade is not None
    session = window.session
    # Maela in the other room, behind the wall: in the fog of war, not shown,
    # not clickable; beside Serrik, shown.
    state = session.area_state()
    state.spots["Serrik Vaun"] = Spot(area="house", x=5, y=10)
    state.spots["Maela Orr"] = Spot(area="house", x=30, y=10)
    session.story.area_live = LiveArea(leaf_id=session.story.active_leaf_id, state=state)
    window.refresh_area()
    assert not sees(view.area, (5, 10), (30, 10))
    assert not view.people["Maela Orr"].isVisible() and not view._tags["Maela Orr"].isVisible()
    px = px_for(view.area)
    assert view.person_at(QPointF(30.5 * px, 10.5 * px)) is None
    state.spots["Maela Orr"] = Spot(area="house", x=7, y=10)
    session.story.area_live = LiveArea(leaf_id=session.story.active_leaf_id, state=state)
    window.refresh_area()
    assert view.people["Maela Orr"].isVisible()
    assert view.person_at(QPointF(7.5 * px, 10.5 * px)).name == "Maela Orr"


def test_a_turn_sent_mid_walk_carries_where_the_figure_is(app, window):
    session = window.session
    before = session.area_state().spots["Serrik Vaun"]
    steps = session.walk_route(before.x + 6, before.y)
    assert len(steps) > 4
    window.area_view.walk(steps)
    window.area_view._step()
    window.area_view._step()  # two tiles shown
    shown = (window.area_view.held().x, window.area_view.held().y)
    assert shown != (before.x, before.y)
    window._provider = MockChatProvider(["She stops.", "{}"])
    window._refresh_composer()
    window.composer.input.setPlainText("I stop.")
    window.send_turn()
    assert window._busy and not window.area_view.walking()
    deadline = time.monotonic() + 20
    while window._busy:
        assert time.monotonic() < deadline, "the turn never finished"
        app.processEvents()
        time.sleep(0.005)
    pump(app)
    stamped = next(n for n in reversed(session.nodes) if n.kind == "user").meta.area
    assert (stamped.spots["Serrik Vaun"].x, stamped.spots["Serrik Vaun"].y) == shown
    assert session.story.area_live is None  # nothing of the walk written during the job


def test_pins_keep_the_turn_s_numbering_when_a_mark_lies_elsewhere(app, window):
    window.composer.input.setPlainText("")
    window._mark_point(400.0, 8000.0)  # 1: on the region, not in this area
    window._mark_tile(5, 5)  # 2: here
    pump(app)
    assert [pin.label for pin in window.area_view._pins] == ["2"]
    assert [pin.label for pin in window.world_view._pins] == ["1", "2"]
    window.composer.input.setPlainText("")
    pump(app)


def test_walking_off_an_area_s_edge_offers_what_lies_that_way(app, window):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent

    from sealedlore.models.area import LiveArea, Spot

    session = window.session
    state = session.area_state()
    state.spots["Serrik Vaun"] = Spot(area="farm", x=30, y=0)
    session.story.area_live = LiveArea(leaf_id=session.story.active_leaf_id, state=state)
    window.refresh_area()
    view = window.area_view
    assert view.edge_button.isVisible() or view.edge_button.text() == "Leave north ▾"
    view.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Up, Qt.NoModifier))
    labels = [a.text() for a in view.edge_menu.actions()]
    assert labels and labels[0].startswith("Hellsville — ")
    view.edge_menu.hide()
    view.edge_menu.actions()[0].trigger()
    assert window.composer.text() == "I set off for Hellsville."


def test_the_tile_set_editor_lists_every_asset_and_finds_one(app, window):
    from sealedlore.gui.tile_set_editor import ROLE, TileSetEditor

    dialog = TileSetEditor(window.session, window)
    roles = [dialog.list.item(i).data(ROLE) for i in range(dialog.list.count())]
    assert "floor" in roles and "walker/Serrik Vaun" in roles
    assert dialog._current() is not None and dialog.label.text()
    for _ in range(40):
        dialog._read_icons()
    assert not dialog._unread
    dialog.search.setText("roads")
    shown = [
        dialog.list.item(i).data(ROLE)
        for i in range(dialog.list.count())
        if not dialog.list.item(i).isHidden()
    ]
    assert shown == ["out-road"]
    dialog.deleteLater()
    pump(app)


def test_the_cast_page_shows_the_avatar_and_a_new_portrait_offers_a_new_one(
    app, window, monkeypatch
):
    """The author (Oct 16 2026): see the avatar full size, change it and draw
    it again as with the portrait; a new portrait offers a new avatar."""
    from PySide6.QtWidgets import QMessageBox

    from sealedlore.engine import session_area
    from sealedlore.gui.avatar_view import pixel_scaled
    from sealedlore.gui.cast_panel import CastPanel
    from sealedlore.models.image import GeneratedImage
    from tests.test_world_pictures import FakeImages, png

    FakeImages.sent = []
    monkeypatch.setattr(session_area, "ImageClient", FakeImages)
    session = window.session
    session.config.image_endpoint.base_url = "https://nano-gpt.com/api/v1"
    session.config.image_endpoint.api_key = "k"
    panel = window.cast_panel
    panel.select_character("char-serrik")
    assert not panel.avatar.isHidden() and panel.avatar.draw_button.text() == "Draw the avatar…"
    assert panel.avatar.picture.pixmap() is not None  # the stand-in figure shows
    assert panel.avatar.reset_button.isHidden()
    # Pixel art is scaled by whole pixels, never blurred.
    small = QImage(16, 24, QImage.Format_ARGB32)
    small.fill(QColor("red"))
    assert pixel_scaled(small, 112).size().toTuple() == (64, 96)
    # Draw it: asked first (the model and price), drawn in the background,
    # then on the card and the map.
    asked: list[str] = []

    def question(_parent, _title, text, *_args, **_kwargs):
        asked.append(text)
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", question)
    panel.avatar.draw_button.click()
    assert asked and "Serrik Vaun's avatar" in asked[-1] and "what their card says" in asked[-1]
    session._asset_thread.join(5)
    pump(app)
    window._art_drawn()
    assert panel.avatar.draw_button.text() == "Draw the avatar again…"
    assert not panel.avatar.reset_button.isHidden()
    assert session.walker_asset("Serrik Vaun")[1] == "changed for this story"
    # A new portrait on the card: offered an avatar drawn from it.
    serrik = next(c for c in session.cast if c.name == "Serrik Vaun")
    session.pictures.write("images/new.png", png(8, 8))
    drawn = GeneratedImage(
        id="drawn-1",
        anchor_node_id=None,
        file="images/new.png",
        prompt="Serrik",
        model="klein",
        size="1k",
        card_id=serrik.id,
        card_first=True,
    )
    session.pictures.add_images([drawn])
    asked.clear()
    window._take_card_pictures()
    assert asked and asked[-1].startswith("Serrik Vaun has a new portrait.")
    assert "their portrait" in asked[-1]
    session._asset_thread.join(5)
    sent = FakeImages.sent[-1]
    assert sent.get("imageDataUrls")  # the portrait went with it
    assert "image 1" in sent["prompt"] and session.tile_style() in sent["prompt"]
    # Back to the set's own, and a story with no map shows no avatar block.
    panel.avatar.reset_button.click()
    assert panel.avatar.reset_button.isHidden()
    plain = CastPanel(lambda: session.pictures)
    plain.set_cast(session.cast)
    plain.select_character("char-serrik")
    assert plain.avatar.isHidden()
    assert serrik.reference_images and serrik.reference_images[0].file.startswith("images/refs/")
