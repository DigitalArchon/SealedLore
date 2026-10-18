"""Things and people as sprites over the ground (gui/area_sprites.py,
gui/area_render.py): the author (Oct 18 2026) asked for things on their own
layer, not painted into the area's picture, and for the play view to be
ready for figures facing and walking each way and carrying what they hold."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtGui import QColor, QImage, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QGraphicsScene  # noqa: E402

from sealedlore.engine.area import Placement, build_area  # noqa: E402
from sealedlore.engine.tile_sets import TileSetInfo  # noqa: E402
from sealedlore.gui.area_render import (  # noqa: E402
    LightMap,
    TileSource,
    compose_things,
    px_for,
    render_ground,
    thing_light,
    thing_picture,
)
from sealedlore.gui.area_sprites import (  # noqa: E402
    DEPTH,
    Frames,
    PersonSprite,
    ThingSprite,
    facing_of,
)
from sealedlore.gui.area_view import Walker  # noqa: E402
from tests.test_area import world_and_art  # noqa: E402

PLAIN = TileSource(TileSetInfo(name="plain"), lambda role: None)


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def picture(colour: str, w: int = 10, h: int = 20) -> QPixmap:
    image = QImage(w, h, QImage.Format_ARGB32)
    image.fill(QColor(colour))
    return QPixmap.fromImage(image)


def test_the_ground_has_no_things_and_the_whole_picture_has_them_lit_as_it_was(app):
    from dataclasses import replace

    world, art = world_and_art()
    house = build_area(world, art, "house")
    x, y = next(
        (x, y)
        for y in range(2, house.height - 2)
        for x in range(2, house.width - 2)
        if house.walkable(x, y) and house.placement_at(x, y) is None
    )
    crate = Placement("crate", "a crate", x=x, y=y, w=1, h=1, verbs=(), kind="object")
    house = replace(house, placements=[*house.placements, crate])
    ground = render_ground(house, PLAIN, dress=False)
    whole = compose_things(ground, house, PLAIN)
    px = px_for(house)
    assert thing_light(crate, px, indoor=True) is None
    middle = (int((x + 0.5) * px), int((y + 0.5) * px))
    assert ground.image.pixelColor(*middle) != whole.pixelColor(*middle)  # stood on the ground
    assert ground.light is not None and ground.light.cell == px // 4


def test_a_light_map_lights_a_sprite_and_keeps_its_transparency(app):
    np = pytest.importorskip("numpy")
    light = LightMap(np.full((4, 4, 3), 0.5, np.float32), np.full((4, 4, 3), 10, np.float32), 10)
    image = QImage(6, 6, QImage.Format_ARGB32)
    image.fill(QColor(200, 100, 40, 255))
    image.setPixelColor(0, 0, QColor(0, 0, 0, 0))
    lit = light.apply(image, 3, 3)
    assert lit.pixelColor(3, 3).getRgb() == (110, 60, 30, 255)
    assert lit.pixelColor(0, 0).alpha() == 0


def test_a_thing_stands_behind_whoever_is_south_of_it_and_in_front_of_whoever_is_north(app):
    px = 40
    chest = Placement("c", "a chest", x=5, y=5, w=2, h=2, verbs=(), kind="furniture")
    sprite = ThingSprite(chest, thing_picture(PLAIN, chest, px, indoor=True), px)
    assert sprite.pos().x() == 6 * px and sprite.pos().y() == 7 * px  # its foot's middle
    south = PersonSprite(Walker("A", 5, 7, None), px, 84, Frames.single(None))
    beside = PersonSprite(Walker("B", 7, 6, None), px, 84, Frames.single(None))
    north = PersonSprite(Walker("C", 5, 4, None), px, 84, Frames.single(None))
    assert north.zValue() < sprite.zValue() < beside.zValue() < south.zValue()
    assert sprite.zValue() > DEPTH
    sprite.stand_at(1, 1)  # moved: in front of nobody now
    assert sprite.zValue() < north.zValue()


def test_a_sprite_is_lit_where_it_stands_and_again_where_it_is_moved(app):
    np = pytest.importorskip("numpy")
    px = 40
    lamp = Placement("l", "a crate", x=1, y=1, w=1, h=1, verbs=(), kind="object")
    sprite = ThingSprite(lamp, thing_picture(PLAIN, lamp, px, indoor=True), px)
    mul = np.ones((40, 40, 3), np.float32)
    mul[:, 20:] = 0.2  # the east half dark
    sprite.set_light(LightMap(mul, np.zeros((40, 40, 3), np.float32), 10))

    def middle() -> QColor:
        shown = sprite.pixmap.toImage()
        return shown.pixelColor(shown.width() // 2, shown.height() - 25)

    bright = middle()
    sprite.stand_at(30, 1)
    dark = middle()
    assert dark.lightness() < bright.lightness()


def test_a_figure_s_pictures_stand_in_for_each_other_by_facing_and_pose(app):
    east = picture("red")
    one = Frames.single(east)
    assert one.frame("east", "east", "stand", 0) == (east, False)
    assert one.frame("west", "west", "walk", 3) == (east, True)  # mirrored, standing
    assert one.frame("north", "west", "stand", 0) == (east, True)  # the side it last faced
    assert one.frame("south", "east", "stand", 0) == (east, False)
    steps = [picture("blue"), picture("green")]
    south = picture("yellow")
    full = Frames({("east", "stand"): [east], ("east", "walk"): steps, ("south", "stand"): [south]})
    assert full.has("walk") and not one.has("walk")
    assert full.frame("east", "east", "walk", 3) == (steps[1], False)
    assert full.frame("west", "west", "walk", 2) == (steps[0], True)
    assert full.frame("south", "west", "stand", 0) == (south, False)
    assert Frames.single(None).frame("east", "east", "stand", 0) == (None, False)
    assert facing_of(1, 1, "south") == "east" and facing_of(0, -1, "east") == "north"
    assert facing_of(0, 0, "west") == "west"


def test_a_person_faces_the_way_they_walk_and_keeps_their_side_going_north(app):
    person = PersonSprite(Walker("A", 3, 3, None), 40, 84, Frames.single(picture("red")))
    assert not person.mirrored
    person.face(-1, 0)
    assert person.facing == "west" and person.mirrored
    person.face(0, -1)
    assert person.facing == "north" and person.mirrored  # no north picture: still the west side
    person.face(1, 0)
    assert not person.mirrored


def test_a_thing_carried_goes_with_its_carrier_and_is_left_where_they_put_it_down(app):
    scene = QGraphicsScene()
    px = 40
    rope = Placement("r", "a coil of rope", x=2, y=2, w=1, h=1, verbs=(), kind="object")
    thing = ThingSprite(rope, thing_picture(PLAIN, rope, px, indoor=True), px)
    person = PersonSprite(Walker("A", 3, 3, None), px, 84, Frames.single(picture("red")))
    scene.addItem(thing)
    scene.addItem(person)
    person.carry(thing)
    assert thing.parentItem() is person and person.carried == [thing]
    person.place_at(10, 10)
    held_at = thing.scenePos()
    assert held_at.y() < person.scenePos().y()  # in their hands, not at their feet
    person.face(-1, 0)
    assert thing.scenePos().x() < person.scenePos().x()  # in front of them, facing west
    person.put_down(thing)
    assert thing.parentItem() is None and person.carried == []
    assert thing.scene() is scene
