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


def test_region_and_world_never_put_the_play_view_back_on_the_way_out(app, window):
    """Live (Oct 17 2026): Region did nothing from the play view. Showing the
    map resized it there and then, and the resize refitted the level still
    set, local, which put the play view back (a later resize can hide that
    here, so what the click does to the play view is recorded)."""
    window.resize(1600, 900)
    window.show()
    app.processEvents()
    view = window.world_view
    view.go_local()
    app.processEvents()
    assert view.play_shown()
    shown: list[bool] = []
    real = view._show_play
    view._show_play = lambda on: (shown.append(on), real(on))[1]
    view.view.resize(320, 200)  # a size of its own while put away, as live
    view.region_button.click()
    assert True not in shown and not view.play_shown() and view.level() == "region"
    view._show_play = real
    view.local_button.click()
    app.processEvents()
    assert view.play_shown()
    view.world_button.click()
    app.processEvents()
    assert not view.play_shown() and view.level() == "world"


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
    assert view.edge_button.isVisible() or view.edge_button.text() == "Leave north"
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


def test_the_tile_set_gallery_lists_every_set_marks_the_one_in_use_and_uses_another(
    app, window, monkeypatch
):
    """The author's ruling (Oct 16 2026): show the user the available tile
    sets, and ask whether they want to generate new ones."""
    from PySide6.QtWidgets import QMessageBox

    from sealedlore.gui.tile_set_gallery import TileSetGallery
    from sealedlore.models.world import OwnTileSet
    from tests.test_world_pictures import png

    session = window.session
    session.pictures.write("images/map/tileset/floor-x.png", png(8, 8))
    art = session.world_art
    art.tile_sets["neon"] = OwnTileSet(
        title="Neon alleys",
        assets={"floor": "images/map/tileset/floor-x.png"},
        words={"style": "neon noir, wet concrete"},
    )
    dialog = TileSetGallery(session, window)
    titles = [c.card.title for c in dialog.cards]
    assert "Snowbound farm" in titles and "Neon alleys" in titles
    in_use = [c for c in dialog.cards if c.card.in_use]
    assert len(in_use) == 1 and not in_use[0].use_button.isEnabled()
    assert in_use[0].card.title in dialog.heading.text()
    # The thumbnails, read a few at a time: a shipped set's from its folder,
    # an own set's from the story's pictures; a shipped set can't be removed.
    for _ in range(40):
        dialog._read_thumbs()
    assert not dialog._pending
    farm = next(c for c in dialog.cards if c.card.name == "doomsville")
    neon = next(c for c in dialog.cards if c.card.name == "own:neon")
    assert not farm.thumbs[0][1].pixmap().isNull() and not neon.thumbs[0][1].pixmap().isNull()
    assert neon.thumbs[1][1].pixmap().isNull()  # the own set has no ground: a dot
    assert farm.remove_button is None and neon.remove_button is not None
    assert "neon noir" in neon.style.text() and neon.keywords.text() == "Drawn for this story"
    assert "farm" in farm.keywords.text() and "bleak" in farm.style.text()
    neon.use_button.click()
    assert session.map_art_copy().tile_set == "own:neon"
    neon = next(c for c in dialog.cards if c.card.name == "own:neon")
    assert neon.card.in_use and "Neon alleys" in dialog.heading.text()
    # Removed: gone from the story with its pictures, and the program chooses again.
    monkeypatch.setattr(QMessageBox, "question", lambda *_a, **_k: QMessageBox.Yes)
    neon.remove_button.click()
    assert "neon" not in session.map_art_copy().tile_sets
    assert session.pictures.read("images/map/tileset/floor-x.png") is None
    assert not any(c.card.own for c in dialog.cards)
    art = session.map_art_copy()  # chosen again by the program, and kept
    assert art.tile_set_auto and not art.tile_set.startswith("own:")
    dialog.design_button.click()
    assert dialog.result() == TileSetGallery.DESIGN
    dialog.deleteLater()
    pump(app)


NEON_DESIGN = {
    "world": "rain-slick cyberpunk alleys",
    "style": "gritty neon noir, wet concrete and pink light",
    "floor": "cracked concrete",
    "cap": "a steel wall top",
    "face": "a graffitied concrete wall",
    "rug": "a soaked cardboard mat",
    "decor": ["a vending machine"],
    "out-ground": "wet asphalt",
    "out-road": "a rain-slick street",
    "out-wall": "a concrete facade with neon tubes",
    "out-door": "a steel door under a buzzing lamp",
    "light": [255, 100, 200],
    "prompts": {
        "floor": "Pixel art, gritty neon noir, wet concrete and pink light: cracked concrete, "
        "a seamless tileable texture seen from above."
    },
}


def test_the_designer_shows_the_words_and_every_prompt_with_the_price_before_drawing(
    app, window, monkeypatch
):
    """The author describes the set, the designer writes it, and nothing is
    drawn until every prompt and the price have been seen: what they
    approve is what is sent."""
    import json

    from PySide6.QtWidgets import QDialog, QMessageBox

    from sealedlore.engine import session_area
    from sealedlore.gui import window_area
    from sealedlore.gui.tile_set_gallery import TileSetDesigner
    from tests.test_world_pictures import FakeImages

    session = window.session
    session.config.image_endpoint.base_url = "https://nano-gpt.com/api/v1"
    session.config.image_endpoint.api_key = "k"
    FakeImages.sent = []
    monkeypatch.setattr(session_area, "ImageClient", FakeImages)
    session.provider.responses = [json.dumps(NEON_DESIGN)]
    dialog = TileSetDesigner(session, window)
    # Prefilled with the world's first sentence and the style in use; the
    # designer field offers the recommended models and says the fallback.
    assert "Calder Keep" in dialog.description.toPlainText()
    assert session.tile_style() in dialog.description.toPlainText()
    assert dialog.designer.placeholderText() == session.tileset_designer
    assert dialog.designer.recommended_button is not None
    assert session.model_name(session.config.map_picture_model) in dialog.drawn_by.text()
    assert not dialog.draw_button.isEnabled() and dialog.scroll.isHidden()
    dialog.name.setText("Neon alleys")
    dialog.designer.setText("z-ai/glm-5.3-flash")
    dialog.write_button.click()
    assert not dialog.busy.isHidden() and "designing" in dialog.status.text()
    session._design_thread.join(5)
    dialog._check()
    assert dialog.busy.isHidden() and not dialog.scroll.isHidden()
    assert dialog.draw_button.isEnabled() and session.map_art_copy().tile_sets == {}
    rows = dialog._rows
    assert rows["floor"].prompt.toPlainText() == NEON_DESIGN["prompts"]["floor"]
    assert "a steel wall top" in rows["cap"].prompt.toPlainText()  # the template, as sent
    assert (
        "out-water" not in rows
        and "Water" in dialog.rows.itemAt(dialog.rows.count() - 2).widget().text()
    )
    assert dialog.world_edit.text() == NEON_DESIGN["world"]
    assert dialog.style_edit.text() == NEON_DESIGN["style"]
    count, _price = session.tile_set_price(dialog.design())
    assert count == len(rows)
    assert (
        f"{count} pictures by {session.model_name(session.config.map_picture_model)}"
        in dialog.price.text()
    )
    # Changed words re-render a template prompt; a designed prompt, or one
    # the author rewrote, is left as it is.
    rows["cap"].words.setText("a brass wall top")
    assert "a brass wall top" in rows["cap"].prompt.toPlainText()
    rows["floor"].words.setText("wet cracked concrete")
    assert rows["floor"].prompt.toPlainText() == NEON_DESIGN["prompts"]["floor"]
    rows["out-door"].prompt.setPlainText("My own door prompt.")
    rows["out-door"].words.setText("a brass door")
    assert rows["out-door"].prompt.toPlainText() == "My own door prompt."
    # A row with no words is left out of the set and the price.
    rows["rug"].words.setText("")
    assert f"{count - 1} pictures" in dialog.price.text()
    asked: list[str] = []

    def question(_parent, _title, text, *_a, **_k):
        asked.append(text)
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", question)
    dialog.draw_button.click()
    assert asked and f"{count - 1} pictures" in asked[-1] and "Neon alleys" in asked[-1]
    assert dialog.result() == QDialog.Accepted
    session._own_set_thread.join(10)
    sent = [p["prompt"] for p in FakeImages.sent]
    assert len(sent) == count - 1
    assert NEON_DESIGN["prompts"]["floor"] in sent and "My own door prompt." in sent
    assert all("a brass wall top" in p for p in sent if "wall top" in p)
    art = session.map_art_copy()
    assert art.tile_set == "own:neon-alleys"
    own = art.tile_sets["neon-alleys"]
    assert own.words["cap"] == "a brass wall top" and "rug" not in own.assets
    assert own.words["prompt:out-door"] == "My own door prompt."
    assert dialog.designer_model() == "z-ai/glm-5.3-flash"
    dialog.deleteLater()
    pump(app)
    # Through Look ▾: the designer chosen in the dialog is kept, as the
    # picture dialogs keep their prompt writer.
    window.config.tileset_model = None

    class Designer:
        def __init__(self, *_a, **_k):
            pass

        def exec(self):
            return QDialog.Rejected

        def designer_model(self):
            return "z-ai/glm-5.3-flash"

        def deleteLater(self):  # noqa: N802 - Qt naming
            pass

    monkeypatch.setattr(window_area, "TileSetDesigner", Designer)
    window._area_own_look()
    assert window.config.tileset_model == "z-ai/glm-5.3-flash"


def test_the_tile_sets_are_offered_once_when_a_map_story_first_opens(app, window, monkeypatch):
    """A line over the play view, not a dialog in the way: which set the
    map is drawn in, with the gallery a button away; answered or dismissed,
    it is kept with the story and never shown again."""
    from PySide6.QtWidgets import QDialog

    from sealedlore.gui import window_area

    session = window.session
    view = window.area_view
    assert not view.offer.isHidden()
    text = view.offer_label.text()
    titles = dict(session.tile_set_choices())
    title = titles[session.tile_set_name()]
    assert text == f"The map is drawn in {title}. Choose another, or design one for this world."
    # The gallery and the designer, a button each.
    assert view.offer_button.text() == "Choose a set…"
    assert view.offer_design.text() == "Design a new set…"
    view.offer_close.click()
    assert session.story.tile_set_offered and view.offer.isHidden()
    window.refresh_area()
    assert view.offer.isHidden()
    # Kept with the story: opened again, it isn't asked.
    window.open_story(session.story.id)
    pump(app)
    assert window.session.story.tile_set_offered and window.area_view.offer.isHidden()
    # The button opens the gallery, which answers it too.
    window.session.story.tile_set_offered = False
    window.refresh_area()
    assert not window.area_view.offer.isHidden()

    class Gallery:
        def __init__(self, *_a, **_k):
            pass

        def exec(self):
            return QDialog.Rejected

        def deleteLater(self):  # noqa: N802 - Qt naming
            pass

    monkeypatch.setattr(window_area, "TileSetGallery", Gallery)
    window.area_view.offer_button.click()
    assert window.session.story.tile_set_offered and window.area_view.offer.isHidden()
    # A story whose set was chosen is never asked.
    window.session.story.tile_set_offered = False
    window.session.set_tile_set("doomsville")
    window.refresh_area()
    assert window.area_view.offer.isHidden()


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


def test_a_place_the_map_has_in_the_area_is_marked_on_the_play_view(app, window):
    """The author's Direction made a rift at a mark: it was on the map and
    told to the storyteller, and the play view showed nothing (Oct 17 2026).
    A place that isn't a building drawn in the picture is marked where it
    stands, its sprite when there is one, its name over it."""
    from sealedlore.gui.area_view import PlaceMark

    view = window.area_view
    area = view.area
    assert area is not None
    rift = PlaceMark("The dark rift", 3, 4)
    view.set_area(area, None, view.walkers, key=view._key, places=[rift])
    item = view.places["The dark rift"]
    px = item.px
    assert item.pos().x() == 3.5 * px and item.pos().y() == 5 * px
    assert view._tags["place:The dark rift"].text() == "The dark rift"
    assert item.pixmap is None  # no sprite yet: a marker stands for it
    small = QImage(8, 12, QImage.Format_ARGB32)
    small.fill(QColor("blue"))
    view.set_area(
        area, None, view.walkers, key=view._key, places=[PlaceMark("The dark rift", 3, 4, small)]
    )
    assert view.places["The dark rift"].pixmap is not None
    view.set_area(area, None, view.walkers, key=view._key, places=[])
    assert "The dark rift" not in view.places and "place:The dark rift" not in view._tags


def test_an_avatar_drawn_from_instructions_is_kept_and_chosen_among_the_others(
    app, window, monkeypatch
):
    """The author (Oct 17 2026): draw an avatar again with new instructions,
    choose among those drawn, and see them all in Media with the one in
    use marked."""
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    from sealedlore.engine import session_area
    from tests.test_world_pictures import FakeImages

    FakeImages.sent = []
    monkeypatch.setattr(session_area, "ImageClient", FakeImages)
    session = window.session
    session.config.image_endpoint.base_url = "https://nano-gpt.com/api/v1"
    session.config.image_endpoint.api_key = "k"
    panel = window.cast_panel
    panel.select_character("char-serrik")
    assert panel.avatar.choose_button.isHidden()  # none kept yet
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(
        QInputDialog, "getMultiLineText", lambda *a, **k: ("a hooded figure in a red scarf", True)
    )
    panel.avatar.words_button.click()
    session._asset_thread.join(5)
    pump(app)
    window._art_drawn()
    assert "hooded figure in a red scarf" in FakeImages.sent[-1]["prompt"]
    first = session.walker_pictures("Serrik Vaun")
    assert len(first) == 1 and first[0].file == session.walker_file("Serrik Vaun")
    assert first[0].prompt and first[0].model
    # A second, from the card: both kept, the newest walking for them.
    panel.avatar.draw_button.click()
    session._asset_thread.join(5)
    pump(app)
    window._art_drawn()
    kept = session.walker_pictures("Serrik Vaun")
    assert len(kept) == 2 and kept[0].file == session.walker_file("Serrik Vaun")
    choices = [a.text() for a in panel.avatar.choose_menu.actions()]
    assert len(choices) == 2 and choices[0].endswith("(in use)")
    # Choosing the older one on the card.
    older = kept[1]
    panel.avatar.choose_menu.actions()[1].trigger()
    pump(app)
    assert session.walker_file("Serrik Vaun") == older.file
    assert [a.isChecked() for a in panel.avatar.choose_menu.actions()] == [False, True]
    # Media lists both avatars, the one walking for them marked, and chooses too.
    window.refresh_images()
    items = [
        window.images_panel.map_list.item(i).text()
        for i in range(window.images_panel.map_list.count())
    ]
    avatars = [t for t in items if t.startswith("Avatar\nSerrik Vaun")]
    assert len(avatars) == 2 and sum("✓ walks on the map" in t for t in avatars) == 1
    window._map_picture_action("use", kept[0].id)
    assert session.walker_file("Serrik Vaun") == kept[0].file
    assert not session.use_walker_picture("Serrik Vaun", "nobody's")


# --- what became of the things, on the play view ---------------------------------


def _send(app, window, text: str, responses) -> None:
    window._provider = MockChatProvider(list(responses))
    window._refresh_composer()
    window.composer.input.setPlainText(text)
    window.send_turn()
    deadline = time.monotonic() + 20
    while window._busy:
        assert time.monotonic() < deadline, "the turn never finished"
        app.processEvents()
        time.sleep(0.005)
    pump(app)


def _house_with_a_lantern(window) -> None:
    """The house's plan with a lantern in it, as if drawn so from the start."""
    from sealedlore.engine.world_art import rasterise
    from sealedlore.models.world import PlaceLayout
    from tests.test_area import HOUSE_SPEC, LANTERN

    spec = {**HOUSE_SPEC, "features": [*HOUSE_SPEC["features"], LANTERN]}
    rows, objects, labels = rasterise(spec, 40, 24)
    window.session.world_art.layouts["house"] = {
        "": PlaceLayout(
            place_id="house", spec=spec, rows=rows, objects=objects, labels=labels, made_at="2"
        )
    }
    window.refresh_area()


def test_a_thing_taken_leaves_the_floor_and_is_carried_dropped_or_given(app, window):
    """The author's ruling: a taken thing is recorded with its taker, in
    code. On the view: gone from the floor, under Carrying ▾, and on the
    Cast page; Drop and Give write their turns."""
    session = window.session
    _house_with_a_lantern(window)
    stand(window, {"Serrik Vaun": ("house", 26, 5), "Maela Orr": ("house", 26, 7)})
    view = window.area_view
    lantern = next(p for p in view.area.placements if "lantern" in p.name)
    assert view.carry_button.isHidden()
    window._area_action("take", lantern.name, lantern.id)
    walk_all(app, window)
    assert window.composer.text() == "I take the brass lantern."
    _send(app, window, window.composer.text(), ["He pockets it.", "{}"])
    assert not any("lantern" in p.name for p in view.area.placements)
    assert view.area.walkable(28, 5)
    assert not view.carry_button.isHidden()
    view.carry_menu.aboutToShow.emit()
    [entry] = [a for a in view.carry_menu.actions() if a.menu() is not None]
    assert entry.text() == "Brass lantern"
    labels = [a.text() for a in entry.menu().actions()]
    assert labels == ["Drop", "Give to Maela Orr"]
    # The Cast page says so under the avatar.
    panel = window.cast_panel
    panel.select_character("char-serrik")
    assert not panel.carrying.isHidden() and panel.carrying.text() == "Carrying: brass lantern"
    panel.select_character("char-maela")
    assert panel.carrying.isHidden()
    # Give to Maela: her turn's record, and her card's line.
    entry.menu().actions()[1].trigger()
    assert window.composer.text() == "I give the brass lantern to Maela Orr."
    _send(app, window, window.composer.text(), ["She takes it.", "{}"])
    assert view.carry_button.isHidden()
    panel.select_character("char-maela")
    assert panel.carrying.text() == "Carrying: brass lantern"
    assert session.carried_things("Maela Orr") == [(lantern.id, "brass lantern")]


def test_a_thing_dropped_lies_at_the_feet_again(app, window):
    session = window.session
    _house_with_a_lantern(window)
    stand(window, {"Serrik Vaun": ("house", 26, 5), "Maela Orr": ("house", 26, 7)})
    view = window.area_view
    lantern = next(p for p in view.area.placements if "lantern" in p.name)
    session.map_action("take", lantern.name, lantern.id)
    _send(app, window, "I take the brass lantern.", ["Taken.", "{}"])
    view.carry_menu.aboutToShow.emit()
    [entry] = [a for a in view.carry_menu.actions() if a.menu() is not None]
    entry.menu().actions()[0].trigger()  # Drop
    assert window.composer.text() == "I put the brass lantern down."
    _send(app, window, window.composer.text(), ["Down it goes.", "{}"])
    me = session.area_state().spots["Serrik Vaun"]
    dropped = next(p for p in view.area.placements if "lantern" in p.name)
    assert abs(dropped.x - me.x) <= 1 and abs(dropped.y - me.y) <= 1
    assert view.carry_button.isHidden()
    assert view.area.placement_at(dropped.x, dropped.y) is dropped  # clickable again


def test_a_broken_thing_is_drawn_broken(app):
    """Something broken is visibly broken (the author's ruling): the
    placement drawn darker, cracked, with rubble at its foot."""
    from sealedlore.engine.area import apply_things
    from sealedlore.gui.area_render import px_for
    from sealedlore.models.area import ThingRecord

    world, art = world_and_art()
    house = build_area(world, art, "house")
    stove = next(p for p in house.placements if "stove" in p.name)
    broken = apply_things(house, {stove.id: ThingRecord(state="broken", name=stove.name)})
    assert broken is not house and next(p for p in broken.placements if "stove" in p.name).broken
    assert house.placements[0].broken is False  # the built area is left as built
    plain = TileSource(TileSetInfo(name="plain"), lambda role: None)
    whole = render_area(house, plain, dress=False)
    after = render_area(broken, plain, dress=False)
    px = px_for(house)
    box = [
        (x, y)
        for x in range(stove.x * px, (stove.x + stove.w) * px)
        for y in range(stove.y * px, (stove.y + stove.h) * px)
    ]
    differ = sum(1 for x, y in box if whole.pixelColor(x, y) != after.pixelColor(x, y))
    assert differ > len(box) * 0.3  # most of its tile, not a pixel or two
    whole_lum = sum(whole.pixelColor(x, y).lightness() for x, y in box) / len(box)
    after_lum = sum(after.pixelColor(x, y).lightness() for x, y in box) / len(box)
    assert after_lum < whole_lum  # darker
    # Elsewhere the picture is the same.
    assert whole.pixelColor(5 * px, 10 * px) == after.pixelColor(5 * px, 10 * px)
    # With a sprite: the broken copy is darker and loses its top.
    from sealedlore.gui.area_render import broken_sprite

    sprite = QImage(20, 30, QImage.Format_ARGB32)
    sprite.fill(QColor(200, 160, 120))
    piece = broken_sprite(sprite)
    assert piece.pixelColor(18, 2).alpha() == 0  # the top corner gone
    assert piece.pixelColor(10, 25).lightness() < sprite.pixelColor(10, 25).lightness()
    assert piece.pixelColor(10, 25).alpha() == 255


def test_weather_the_story_set_lies_over_an_outdoor_area_and_clear_weather_nothing(app, window):
    from sealedlore.gui.area_view import weather_look

    assert weather_look("snowing hard") == "snow"
    assert weather_look("Sleet and wind") == "snow"
    assert weather_look("a cold drizzle") == "rain"
    assert weather_look("a thunderstorm breaking") == "rain"
    assert weather_look("thick fog on the river") == "fog"
    assert weather_look("a sandstorm from the east") == "dust"
    assert weather_look("ash falling from the sky") == "dust"
    assert weather_look("clear and cold") is None and weather_look("") is None
    session, view = window.session, window.area_view
    # Indoors (the house the story opens in): nothing drawn whatever the weather.
    session.story.world_base.weather = "snowing hard"
    window.refresh_area()
    assert view.area.indoor and view.weather_shown() is None
    # Out in the yard: snow over the area, over the tint and under the names.
    stand(window, {"Serrik Vaun": ("farm", 30, 0)})
    assert not view.area.indoor and view.weather_shown() == "snow"
    assert view._weather.zValue() == 92
    assert view._weather.rect().width() == view._tint.rect().width()
    assert view._weather_timer.isActive() == view.isVisible()
    view._weather_tick()
    assert view._weather.phase > 0
    # Clear weather: the layer goes and its timer with it.
    session.story.world_base.weather = "clear and cold"
    window.refresh_area()
    assert view.weather_shown() is None and not view._weather_timer.isActive()


def test_a_painting_s_reference_is_the_area_as_shown_with_the_people_and_things_on_it(app):
    """A painting is painted from the play view's own picture (the author,
    Oct 17 2026: consistent with the actual map, with the characters and
    items present): the tiles, each walker's sprite at their tile and the
    places' marks, fitted whole into the model's own shape."""
    from PySide6.QtGui import QColor, QImage

    from sealedlore.gui.area_render import px_for
    from sealedlore.gui.area_view import PERSON_TILES, PlaceMark, Walker
    from sealedlore.gui.image_codec import image_from_data
    from sealedlore.gui.map_render import area_scene_image, area_scene_png

    world, art = world_and_art()
    farm = build_area(world, art, "farm")
    plain = TileSource(TileSetInfo(name="plain"), lambda role: None)
    px = px_for(farm)
    sprite = QImage(8, 16, QImage.Format_ARGB32)
    sprite.fill(QColor(255, 0, 255))
    mark = QImage(8, 8, QImage.Format_ARGB32)
    mark.fill(QColor(0, 255, 255))
    walkers = [Walker("Jane", 10, 10, sprite, held=True), Walker("Ben", 20, 5, None)]
    places = [PlaceMark("The rift", 30, 12, mark)]
    # At the picture's own size, the sprite's colour stands on Jane's tile.
    whole = area_scene_image(farm, plain, walkers, places, (farm.width * px, farm.height * px))
    assert whole.size().width() == farm.width * px
    feet = whole.pixelColor(int(10.5 * px), int(11 * px) - 2)
    assert (feet.red(), feet.green(), feet.blue()) == (255, 0, 255)
    head = whole.pixelColor(int(10.5 * px), int(11 * px - px * PERSON_TILES[False]) + 2)
    assert (head.red(), head.green(), head.blue()) == (255, 0, 255)
    plain_figure = whole.pixelColor(int(20.5 * px), int(6 * px) - 2)
    assert (plain_figure.red(), plain_figure.green(), plain_figure.blue()) == (90, 110, 170)
    rift = whole.pixelColor(int(30.5 * px), int(13 * px) - 2)
    assert (rift.red(), rift.green(), rift.blue()) == (0, 255, 255)
    # Fitted into the model's shape: the whole area, centred, margins in the
    # ground's own colour rather than bars; PNG through the codec.
    fitted = image_from_data(area_scene_png(farm, plain, walkers, places, (1024, 576)))
    assert (fitted.width(), fitted.height()) == (1024, 576)
    scale = min(1024 / whole.width(), 576 / whole.height())
    left = (1024 - round(whole.width() * scale)) // 2
    top = (576 - round(whole.height() * scale)) // 2
    at = fitted.pixelColor(int(left + 10.5 * px * scale), int(top + (11 * px - 2) * scale))
    assert at.red() > 200 and at.blue() > 200 and at.green() < 80
    margin = fitted.pixelColor(2, 2) if left > 4 else fitted.pixelColor(2, 2)
    assert margin.alpha() == 255 and (margin.red(), margin.green(), margin.blue()) != (0, 0, 0)


def test_paint_this_place_paints_the_area_stood_in_and_media_lists_the_painting(
    app, window, monkeypatch
):
    """Paintings ▾ on the play view (the author, Oct 17 2026): Paint this
    place… with the model and its price, the painting made through the map
    pictures' own path (counted, logged, kept, chosen for the map), the
    storyteller's clock and the people in the prompt, the menu then listing
    it, and Media calling it a Painting."""
    import json

    from sealedlore.engine import session_world_art
    from sealedlore.gui import window_area
    from tests.test_world_pictures import FakeImages

    session, view = window.session, window.area_view
    session.config.image_endpoint.base_url = "https://nano-gpt.com/api/v1"
    session.config.image_endpoint.api_key = "k"
    session.config.image_models = [
        {
            "id": session.config.map_isometric_model,
            "supported_parameters": {
                "resolutions": ["1024x768", "1024x576"],
                "max_input_images": 3,
            },
        }
    ]
    FakeImages.sent = []
    monkeypatch.setattr(session_world_art, "ImageClient", FakeImages)
    monkeypatch.setattr(session_world_art, "list_image_models", lambda *a, **k: [])
    session.config.map_pictures_mode = "off"  # the press is the approval
    stand(window, {"Serrik Vaun": ("house", 5, 10), "Maela Orr": ("house", 6, 10)})
    assert view.area.place_id == "house"
    window._area_paintings_menu()
    actions = [a for a in view.paintings_menu.actions() if not a.isSeparator()]
    klein = "blackforestlabs/flux-3/klein/text-to-image"
    assert actions[0].text().startswith(f"Paint this place… ({klein}")
    assert actions[0].isEnabled() and actions[1].text() == "No painting of The house yet"
    actions[0].trigger()
    assert session.isometric_painting == "house" and "Painting The house" in view.hint.text()
    session._isometric_thread.join(10)
    pump(app)
    painted = session.map_art_copy().picture_for("isometric", "house")
    assert painted is not None and painted.model == session.config.map_isometric_model
    payload = FakeImages.sent[-1]
    assert payload["size"] == "1024x576"
    assert "data:image/png;base64" in json.dumps(payload)  # the area's picture went with it
    prompt = payload["prompt"]
    assert "It is the small hours." in prompt  # the clock's hour, never the day
    assert "A veteran duellist with a debt he cannot repay" in prompt  # Serrik, by his looks
    assert "A locksmith who talks too much" in prompt  # Maela too
    assert "Serrik" not in prompt and "Maela" not in prompt and "Day" not in prompt
    assert "cast-iron stove" in prompt  # a thing at hand, by name
    assert painted.scene == "It is the small hours. 2 there."
    assert session.map_pictures_left_today() == session.config.map_picture_daily_limit - 1
    # The menu now lists it; choosing it opens the painting whole.
    opened: list[str] = []
    monkeypatch.setattr(
        window_area.IsometricDialog, "exec", lambda self: opened.append(self.windowTitle()) or 1
    )
    window._area_paintings_menu()
    actions = [a for a in view.paintings_menu.actions() if not a.isSeparator()]
    assert actions[0].text().startswith("Paint this place…")
    assert len(actions) == 2 and actions[1].text().startswith("Painted ")
    assert session.config.map_isometric_model in actions[1].text()
    assert "2 there" in actions[1].text()
    actions[1].trigger()
    assert opened == ["The house: a painting"]
    # Media calls it a Painting, the one on the map.
    window.refresh_images()
    items = [
        window.images_panel.map_list.item(i).text()
        for i in range(window.images_panel.map_list.count())
    ]
    assert "Painting\nThe house\n✓ on the map" in items
    # Open country has no plan to paint: the entry says so, disabled.
    view.area.rect = (0.0, 0.0, 1600.0, 1600.0)  # a window of the land, for the menu's check
    window._area_paintings_menu()
    actions = [a for a in view.paintings_menu.actions() if not a.isSeparator()]
    assert not actions[0].isEnabled() and "Open country" in actions[0].toolTip()


def test_the_map_s_heading_is_neutral_under_the_play_view_and_a_plan_never_labels_itself(window):
    """Live (Oct 17 2026): the heading said "The farm" in Hellsville (it
    followed the map put away), and the farmhouse's plan marked itself, "Old
    Two-Storey Farmhouse", in the middle of a room."""
    from dataclasses import replace

    view = window.world_view
    view.go_region()
    view.go_local()
    assert view.play_shown() and view.title.text() == "Map"
    area = window.area_view.area
    labelled = replace(
        area, labels={**area.labels, "Old Two-Storey House": (3, 3), "A rift": (4, 4)}
    )
    marks = window._place_marks(labelled, window.session, window.session.map_art_copy())
    names = [m.name for m in marks]
    assert "Old Two-Storey House" not in names and "A rift" in names


def test_map_pictures_that_wait_show_on_the_map_and_go_as_approved(app, window, monkeypatch):
    """Settings → Images → Draw map pictures: Ask before drawing. The Map
    page says how many wait and their price; Review… shows each prompt, and
    what is ticked goes as it stands, the rest left out for the session."""
    from dataclasses import replace

    from PySide6.QtWidgets import QDialog

    from sealedlore.engine.world_pictures import PictureJob
    from sealedlore.gui.map_pictures_dialog import MapPicturesDialog

    session, view = window.session, window.world_view
    session.config.image_endpoint.base_url = "https://nano-gpt.com/api/v1"
    session.config.image_endpoint.api_key = "k"
    session.config.map_art = True
    session.config.map_pictures_mode = "ask"
    jobs = [
        PictureJob(
            key="walker:Maela Orr",
            subject="walker",
            subject_id="Maela Orr",
            name="Maela Orr",
            prompt="Maela, small.",
        ),
        PictureJob(
            key="texture:out-roof-stone",
            subject="texture",
            subject_id="out-roof-stone",
            name="stone roof",
            prompt="A stone roof.",
        ),
    ]
    assert session._held_for_approval("tile", jobs) == []  # nothing drawn: asked first
    window._sync_progress()
    assert not view.waiting.isHidden()
    assert view.waiting_label.text().startswith("2 map pictures wait for your approval")
    approved: list = []
    monkeypatch.setattr(
        session,
        "approve_map_pictures",
        lambda jobs, start=True: approved.append((list(jobs), start)) or False,
    )

    def review(dialog: MapPicturesDialog) -> int:
        assert [r.tick.text() for r in dialog.rows] == ["Avatar: Maela Orr", "Texture: stone roof"]
        dialog.rows[0].prompt.setPlainText("Maela, smaller.")
        dialog.rows[1].tick.setChecked(False)
        assert dialog.draw_button.text() == "Draw 1"
        return QDialog.Accepted

    monkeypatch.setattr(MapPicturesDialog, "exec", review)
    window._review_map_pictures()
    assert approved == [([replace(jobs[0], prompt="Maela, smaller.")], True)]
    # The roof was left out: not asked about again while the story is open.
    assert [w.job.key for w in session.map_pictures_waiting()] == ["walker:Maela Orr"]
    window._decline_map_pictures()
    assert session.map_pictures_waiting() == [] and view.waiting.isHidden()
    session._held_for_approval("tile", jobs)
    assert session.map_pictures_waiting() == []
