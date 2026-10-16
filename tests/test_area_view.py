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
    """The walk being shown, to its end: a whole tile a step (the frames
    between tiles are `_advance`'s)."""
    view = window.area_view
    for _ in range(500):
        if not view.walking():
            break
        view._step()
    pump(app)


def stand(window, spots: dict[str, tuple[str, int, int]]) -> None:
    """Everyone where the test wants them, as the session's live state."""
    from sealedlore.models.area import LiveArea, Spot

    session = window.session
    state = session.area_state()
    for name, (area, x, y) in spots.items():
        state.spots[name] = Spot(area=area, x=x, y=y)
    session.story.area_live = LiveArea(leaf_id=session.story.active_leaf_id, state=state)
    window.refresh_area()


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
    from PySide6.QtCore import QPointF, Qt

    from sealedlore.engine.area import sees
    from sealedlore.gui.area_render import px_for
    from sealedlore.gui.area_view import SHADE_ALPHA, lit_rooms
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
    # The shade is by room, never a hard checker of rays: Serrik's room is
    # lit whole, and so is the room the open door at (20, 12), in his sight,
    # leads into, though Maela in it is out of sight; the far wall, which
    # no ray reaches, is dimmed.
    mine, theirs = view.area.room_at(5, 10), view.area.room_at(30, 10)
    assert mine is not None and theirs is not None and mine.index != theirs.index
    assert lit_rooms(view.area, (5, 10)) == {mine.index, theirs.index}
    mask = view._shade_mask
    assert mask.pixelColor(5, 10).alpha() == 0 and mask.pixelColor(30, 10).alpha() == 0
    assert mask.pixelColor(39, 10).alpha() == SHADE_ALPHA
    assert view._shade.transformationMode() == Qt.SmoothTransformation
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
    # The open country past the edge first (tests/test_open_country.py),
    # then the places that way.
    assert labels[0] == "Walk on into open country" and labels[1].startswith("Hellsville — ")
    view.edge_menu.hide()
    view.edge_menu.actions()[1].trigger()
    assert window.composer.text() == "I set off for Hellsville."


def test_the_tile_set_editor_lists_every_asset_and_finds_one(app, window, monkeypatch):
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
    # The set's reader is taken once a fill, not once a row (each read
    # through the session copied the map's art).
    assert dialog._read is not None
    # A picture of the author's own: the ground and walls are cropped square
    # (they tile); a sprite or a walker keeps whatever shape it has.
    from PySide6.QtWidgets import QFileDialog

    from sealedlore.engine.picture_shapes import FREE, SQUARE
    from sealedlore.gui import tile_set_editor

    shapes: list[tuple[str, str]] = []

    def crop(_parent, _path, shape):
        shapes.append(shape)
        return None  # the author cancels

    monkeypatch.setattr(tile_set_editor, "cropped_picture", crop)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_a, **_k: ("/x/picture.png", ""))
    dialog.search.setText("")
    for role, shape in (("out-road", SQUARE), ("out-tree0", FREE), ("walker/Serrik Vaun", FREE)):
        item = next(
            dialog.list.item(i)
            for i in range(dialog.list.count())
            if dialog.list.item(i).data(ROLE) == role
        )
        dialog.list.setCurrentItem(item)
        dialog._own()
        assert shapes[-1] == shape, role
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
    assert not asked  # asked once the handler that took the pictures is done
    pump(app)
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


def test_rooms_are_lit_by_the_doorways_in_sight_and_the_rest_dimmed(app):
    """Three rooms in a row, the far door out of sight from the first: the
    first two lit whole, the third dimmed, and the tiles in no room (the
    doors, the walls) by a ray each."""
    from sealedlore.engine.area import Area, find_rooms, openings
    from sealedlore.gui.area_view import SHADE_ALPHA, lit_rooms, sight_mask

    rows = [
        "####################",
        "#____#______#______#",
        "#____+______#______#",
        "#____#______#______#",
        "#____#______#______#",
        "#____#______#______#",
        "#____#______#______#",
        "#____#______#______#",
        "#____#______+______#",
        "#____#______#______#",
        "####################",
    ]
    doorways = openings(rows)
    rooms, room_of = find_rooms(rows, {}, doorways)
    area = Area(
        place_id="t",
        name="Three rooms",
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
    first, second, third = (area.room_at(x, 5).index for x in (2, 8, 15))
    assert len({first, second, third}) == 3
    assert lit_rooms(area, (2, 2)) == {first, second}
    assert lit_rooms(area, (8, 8)) == {first, second, third}  # both doors in sight
    mask = sight_mask(area, (2, 2))
    assert mask.pixelColor(2, 2).alpha() == 0 and mask.pixelColor(8, 8).alpha() == 0
    assert mask.pixelColor(15, 5).alpha() == SHADE_ALPHA  # the third room, whole
    assert all(mask.pixelColor(x, y).alpha() == SHADE_ALPHA for x in (14, 17) for y in (1, 9))
    assert mask.pixelColor(5, 2).alpha() == 0  # the door in sight
    assert mask.pixelColor(12, 8).alpha() == SHADE_ALPHA  # the door behind the wall
    assert mask.pixelColor(19, 8).alpha() == SHADE_ALPHA  # a wall tile no ray reaches
    assert mask.pixelColor(5, 8).alpha() == 0  # a wall tile of the first room, in sight


def test_a_click_mid_walk_goes_on_from_where_the_figure_is(app, window):
    """The route used to start from the walk's start while the figure was
    mid-way, `walked` refused it, and the figure snapped back."""
    session = window.session
    view = window.area_view
    before = session.area_state().spots["Serrik Vaun"]
    steps = session.walk_route(before.x + 6, before.y)
    assert len(steps) > 4
    view.walk(steps)
    view._step()
    view._step()
    shown = (view.held().x, view.held().y)
    window._area_pending = ("use", "the well", "well")  # a walk-up the click gives up
    back = tuple(steps[1])  # a tile back the way (the start is a door)
    window._area_walk(*back, False)
    assert view.walking()
    assert view._walked[0] == shown  # the new route starts where the figure stood
    me = session.area_state().spots["Serrik Vaun"]
    assert (me.x, me.y) == shown  # and the walk so far was told
    assert window._area_pending is None and window.composer.text() == ""
    walk_all(app, window)
    me = session.area_state().spots["Serrik Vaun"]
    assert (me.x, me.y) == back


def test_a_private_scene_forgets_a_walk_and_writes_nothing_of_the_map(app, window):
    session = window.session
    view = window.area_view
    before = session.area_state().spots["Serrik Vaun"]
    steps = session.walk_route(before.x + 6, before.y)
    view.walk(steps)
    view._step()
    session.enter_private(keep="disk", provider=MockChatProvider([]), model="mock")
    assert session.in_private
    window.refresh_area()
    assert not view.walking() and view.area is None
    assert "private scene" in view.hint.text()
    me = session.area_state().spots["Serrik Vaun"]
    assert (me.x, me.y) == (before.x, before.y)
    assert session.story.area_live is None
    # A walk's end reaching the window in the scene writes nothing either.
    window._area_walked([(before.x, before.y), steps[1]])
    me = session.area_state().spots["Serrik Vaun"]
    assert (me.x, me.y) == (before.x, before.y) and session.story.area_live is None


def test_a_failed_painting_is_logged_once_and_the_plain_map_stands_in(
    app, window, monkeypatch, caplog
):
    import logging

    from PySide6.QtWidgets import QGraphicsSimpleTextItem

    from sealedlore.gui import window_area

    calls: list[int] = []

    def boom(*_args, **_kwargs):
        calls.append(1)
        raise RuntimeError("no paint today")

    view = window.area_view
    window._join_paint_threads(5)  # the story's own first painting, done
    pump(app)
    monkeypatch.setattr(window_area, "render_area", boom)
    # Something the area is painted from changes: a new key, painted anew.
    window.session.story.defaults.map_density = "sparse"
    assert not window._area_painting
    with caplog.at_level(logging.ERROR, logger="sealedlore.gui"):
        for _ in range(3):  # refreshed again and again, painted once
            window.refresh_area()
            window._join_paint_threads(5)
            pump(app)
    assert len(calls) == 1 and window._area_failed
    assert "couldn't be painted" in caplog.text and "no paint today" in caplog.text
    assert view.area is not None and view._background is None
    notes = [i.text() for i in view.scene.items() if isinstance(i, QGraphicsSimpleTextItem)]
    assert any("plain map stands in" in note for note in notes)
    assert not any(t.is_alive() for t in window._paint_threads)
    # What it is painted from changing paints again.
    window._area_failed.clear()
    window.refresh_area()
    window._join_paint_threads(5)
    assert len(calls) == 2


def test_closing_the_story_leaves_nothing_of_its_play_view(app, window):
    view = window.area_view
    assert view.area is not None and window._area_story is not None
    session = window.session
    before = session.area_state().spots["Serrik Vaun"]
    view.walk(session.walk_route(before.x + 6, before.y))
    view._step()
    window._close_story()
    pump(app)
    assert window.session is None
    assert view.area is None and not view.walking() and view.people == {}
    assert window._area_images == {} and window._walker_images == {}
    assert window._area_story is None and window._area_pending is None


def test_new_portraits_on_several_cards_are_one_question_and_one_run(app, window, monkeypatch):
    """Asked one card at a time, halfway through the handler that took the
    pictures, the second card was refused ("Another asset is being drawn
    now")."""
    from PySide6.QtWidgets import QMessageBox

    from sealedlore.engine import session_area
    from sealedlore.models.image import GeneratedImage
    from tests.test_world_pictures import FakeImages, png

    FakeImages.sent = []
    monkeypatch.setattr(session_area, "ImageClient", FakeImages)
    session = window.session
    session.config.image_endpoint.base_url = "https://nano-gpt.com/api/v1"
    session.config.image_endpoint.api_key = "k"
    asked: list[str] = []

    def question(_parent, _title, text, *_args, **_kwargs):
        asked.append(text)
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", question)
    counter = [0]

    def portraits(*names: str) -> None:
        records = []
        for name in names:
            card = next(c for c in session.cast if c.name == name)
            counter[0] += 1
            file = f"images/new-{counter[0]}.png"
            session.pictures.write(file, png(8, 8))
            records.append(
                GeneratedImage(
                    id=f"drawn-{counter[0]}",
                    anchor_node_id=None,
                    file=file,
                    prompt=name,
                    model="klein",
                    size="1k",
                    card_id=card.id,
                    card_first=True,
                )
            )
        session.pictures.add_images(records)
        window._take_card_pictures()
        pump(app)

    portraits("Serrik Vaun", "Maela Orr")
    assert len(asked) == 1
    assert asked[0].startswith("2 cards have new portraits: Serrik Vaun, Maela Orr.")
    assert "each" in asked[0]
    session._asset_thread.join(10)
    pump(app)
    assert len(FakeImages.sent) == 2 and all(p.get("imageDataUrls") for p in FakeImages.sent)
    assert session.walker_asset("Serrik Vaun")[1] == "changed for this story"
    assert session.walker_asset("Maela Orr")[1] == "changed for this story"
    # With map pictures on, a card with no avatar yet gets one under the
    # standing approval: not asked for. The two that have one are.
    monkeypatch.setattr(type(session), "map_pictures_on", property(lambda self: True))
    asked.clear()
    portraits("Serrik Vaun", "Maela Orr", "Captain Idris")
    assert len(asked) == 1 and "Captain Idris" not in asked[0] and "Maela Orr" in asked[0]
    session._asset_thread.join(10)
    # Nothing is asked for cards whose portraits didn't change.
    asked.clear()
    window._take_card_pictures()
    pump(app)
    assert asked == []


def test_the_scene_holds_the_names_over_the_top_row_and_the_ring_round_the_figure(app, window):
    from PySide6.QtCore import QRectF

    from sealedlore.gui.area_render import px_for
    from sealedlore.gui.area_view import PAD_ABOVE, PAD_AROUND

    view = window.area_view
    area = view.area
    px = px_for(area)
    rect = view.scene.sceneRect()
    assert rect.top() == -PAD_ABOVE * px and rect.left() == -PAD_AROUND * px
    assert rect.right() == (area.width + PAD_AROUND) * px
    assert rect.bottom() == (area.height + PAD_AROUND) * px
    stand(window, {"Serrik Vaun": ("house", 5, 0), "Maela Orr": ("house", 7, 10)})
    person, tag = view.people["Serrik Vaun"], view._tags["Serrik Vaun"]
    assert rect.contains(person.sceneBoundingRect())
    # The ring (and its pen) lies inside the figure's bounding rect: painted
    # outside it, it smeared while walking.
    centre, rx, ry, pen = person._ring()
    ring = QRectF(centre.x() - rx - pen, centre.y() - ry - pen, 2 * (rx + pen), 2 * (ry + pen))
    assert person.boundingRect().contains(ring)
    # The name, in the view's pixels, lies above the head and inside the scene.
    box = view._tag_box(tag)
    scale = view.view.transform().m11()
    head = view.view.mapFromScene(person.scenePos()).y() - person.height * scale
    assert box.bottom() <= head + 1
    assert box.top() >= view.view.mapFromScene(rect.topLeft()).y()


def test_the_time_of_day_lies_over_the_area(app, window, monkeypatch):
    from PySide6.QtGui import QColor

    from sealedlore.gui.area_view import NIGHT, tint_colour

    assert tint_colour(12 * 60) is None and tint_colour(8 * 60) is None
    assert tint_colour(17 * 60) is None
    midnight, dawn, dusk = tint_colour(0), tint_colour(6 * 60), tint_colour(19 * 60)
    assert midnight.blue() > midnight.red()  # deep blue
    assert dawn.red() > dawn.blue() and dusk.red() > dusk.blue()  # amber
    assert abs((255 - midnight.red()) / (255 - NIGHT[0]) - 0.45) < 0.01  # 45% at midnight
    assert tint_colour(7 * 60 + 30).blue() > dawn.blue()  # fading into the day
    session = window.session
    view = window.area_view
    monkeypatch.setattr(type(session), "map_clock", lambda self: (2, 0))
    window.refresh_area()
    assert view.clock.text() == "Day 2, 00:00"
    assert view._tint is not None and view._tint.isVisible()
    assert view._tint.colour == midnight and view._tint.zValue() == 90
    assert view._tint.rect().width() == view.area.width * view.people["Serrik Vaun"].px
    monkeypatch.setattr(type(session), "map_clock", lambda self: (2, 12 * 60))
    window.refresh_area()
    assert not view._tint.isVisible()
    monkeypatch.setattr(type(session), "map_clock", lambda self: None)
    window.refresh_area()
    assert not view._tint.isVisible() and isinstance(view._tint.colour, QColor)


def test_walking_is_drawn_between_tiles_facing_the_way_it_goes(app, window):
    from sealedlore.gui.area_render import px_for
    from sealedlore.gui.area_view import STEPS_PER_SECOND

    session = window.session
    view = window.area_view
    px = px_for(view.area)
    before = session.area_state().spots["Serrik Vaun"]
    steps = session.walk_route(before.x + 6, before.y)
    finished: list[list] = []
    view.walk_finished.connect(lambda walked: finished.append(list(walked)))
    view.walk(steps)
    person = view.people["Serrik Vaun"]
    assert view.walking() and view._timer.interval() <= 20  # about 60 Hz
    view._advance(0.5 / STEPS_PER_SECOND)
    assert (view.held().x, view.held().y) == tuple(steps[0])  # on its tile until the next
    (x0, y0), (x1, y1) = steps[0], steps[1]
    assert abs(person.pos().x() - (x0 + (x1 - x0) * 0.5 + 0.5) * px) < 1e-6
    assert abs(person.pos().y() - (y0 + (y1 - y0) * 0.5 + 1.0) * px) < 1e-6
    assert person.mirrored == (x1 < x0)
    assert view._tags["Serrik Vaun"].pos().x() == person.pos().x()
    view._advance(0.6 / STEPS_PER_SECOND)  # past the tile
    assert (view.held().x, view.held().y) == tuple(steps[1])
    assert person.bob in (0.0, 1.0)
    # Finished mid-way: on its tile, the tiles walked told once.
    view.finish_walk()
    assert not view.walking() and finished == [[tuple(steps[0]), tuple(steps[1])]]
    assert person.pos().x() == (steps[1][0] + 0.5) * px and person.bob == 0.0
    # Walking back faces the other way.
    back = session.walk_route(before.x, before.y)
    view.walk(back)
    view._advance(0.2 / STEPS_PER_SECOND)
    assert back[1][0] < back[0][0] and person.mirrored
    view.drop_walk()
    assert not view.walking() and len(finished) == 1


def test_neighbours_names_are_lifted_apart_and_the_pointer_says_what_is_under_it(app, window):
    from PySide6.QtCore import QPointF, Qt

    from sealedlore.gui import text_size
    from sealedlore.gui.area_render import px_for
    from sealedlore.gui.area_view import GOLD, RED
    from sealedlore.gui.map_art import FOG

    view = window.area_view
    px = px_for(view.area)
    stand(window, {"Serrik Vaun": ("house", 5, 10), "Maela Orr": ("house", 6, 10)})
    mine, theirs = view._tags["Serrik Vaun"], view._tags["Maela Orr"]
    assert mine.lift == 0.0 and theirs.lift > 0  # the author's character's stays put
    assert not view._tag_box(mine).intersects(view._tag_box(theirs))
    view.view.zoom_by(1.25)
    assert not view._tag_box(mine).intersects(view._tag_box(theirs))
    # The tile under the pointer: gold where it can be walked to, red where
    # not; a pointing hand over a person or a thing.
    view._hovered(QPointF(5.5 * px, 11.5 * px))
    assert view._hover.isVisible() and view._hover.rect().topLeft() == QPointF(5 * px, 11 * px)
    assert view._hover.pen().color().rgb() == GOLD.rgb()
    assert view.view.viewport().cursor().shape() == Qt.ArrowCursor
    view._hovered(QPointF(6.5 * px, 10.5 * px))  # Maela
    assert view.view.viewport().cursor().shape() == Qt.PointingHandCursor
    wall = next(
        (x, y)
        for y in range(view.area.height)
        for x in range(view.area.width)
        if view.area.at(x, y) == "#"
    )
    view._hovered(QPointF((wall[0] + 0.5) * px, (wall[1] + 0.5) * px))
    assert view._hover.pen().color().rgb() == RED.rgb()
    view._left()
    assert not view._hover.isVisible()
    # The names follow View → Text size, and the view's ground is the
    # region map's fog, not black.
    assert mine.font().pointSizeF() == text_size.points(9.0)
    assert view.view.backgroundBrush().color().name() == FOG
