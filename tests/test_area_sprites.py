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


class _Set:
    """A tile set's reader with three item pictures, each a tall block."""

    names = ("a-battered-tin-washbasin-on-a-stand", "water-barrels", "rope")

    def __call__(self, role: str) -> bytes | None:
        if role.removeprefix("items/") not in self.names:
            return None
        from sealedlore.gui.image_codec import encoded

        image = QImage(40, 80, QImage.Format_ARGB32)
        image.fill(QColor(0, 0, 0, 0))
        for y in range(4, 76):
            for x in range(4, 36):
                image.setPixelColor(x, y, QColor("#806040"))
        return encoded(image, "PNG")

    def items(self) -> list[str]:
        return list(self.names)


def test_what_a_person_carries_is_drawn_small_and_borrows_only_its_own_kind(app):
    """The author (Oct 26 2026): indoors a tin of beans looked like a bath, a
    bottle of water like a barrel. A shared word ("tin", "water") chose the
    picture, and anything on the floor was drawn as tall as the player."""
    from sealedlore.engine.tile_sets import carried_head, closest_item
    from sealedlore.gui.area_render import CARRIED_TILES, _placement_sprite, carried

    source = TileSource(TileSetInfo(name="set"), _Set())
    px = 40

    def thing(name: str, verbs=("use", "take", "destroy")) -> Placement:
        return Placement(id=name, name=name, x=4, y=4, w=1, h=1, verbs=verbs, kind="object")

    tin, water, rope = thing("a tin of beans"), thing("a bottle of water"), thing("a coil of rope")
    assert carried(tin) and not carried(thing("a tin tub", ("use", "destroy")))
    assert carried_head("a coil of rope") == "rope" and carried_head("a tin of beans") == "tin"
    names = _Set.names
    assert closest_item("a tin of beans", names) == "a-battered-tin-washbasin-on-a-stand"
    assert closest_item("a tin of beans", names, same_kind=True) is None
    assert closest_item("a bottle of water", names, same_kind=True) is None
    # No picture of its kind: a small bundle, never the washbasin or the barrels.
    for small in (tin, water):
        assert _placement_sprite(source, small, px, indoor=True) is None
        drawn = thing_picture(source, small, px, indoor=True).image
        assert drawn.height() <= 2 * px
        assert any(
            drawn.pixelColor(x, y).alpha()
            for x in range(drawn.width())
            for y in range(drawn.height())
        )
    # Its own kind's picture, at a carried thing's size.
    sprite = _placement_sprite(source, rope, px, indoor=True)
    assert sprite is not None and sprite.height() == int(px * CARRIED_TILES[True])
    # What can't be carried still borrows by a shared word, at its old size.
    tub = _placement_sprite(source, thing("a tin tub", ("use", "destroy")), px, indoor=True)
    assert tub is not None and tub.height() == int(px * 2.2)


def _png(colour: str, w: int = 12, h: int = 24) -> bytes:
    from sealedlore.gui.image_codec import encoded

    image = QImage(w, h, QImage.Format_ARGB32)
    image.fill(QColor(colour))
    return encoded(image, "PNG")


def test_an_abandoned_place_shows_a_things_drawn_abandoned_version_else_its_picture_faded(app):
    from sealedlore.engine.area import Placement
    from sealedlore.gui.area_render import thing_picture

    assets = {"items/a-chair": _png("#c08040"), "items/a-table": _png("#c08040")}
    assets["aged/a-chair"] = _png("#5060a0")  # drawn abandoned; the table has none
    source = TileSource(TileSetInfo(name="set"), assets.get)
    assert source.aged_item("a chair") is not None and source.aged_item("a table") is None
    chair = Placement(id="c", name="a chair", x=2, y=2, w=1, h=1, verbs=(), kind="furniture")
    table = Placement(id="t", name="a table", x=4, y=2, w=1, h=1, verbs=(), kind="furniture")

    def middle(picture) -> QColor:
        image = picture.image
        return image.pixelColor(image.width() // 2, image.height() // 2 - 4)

    fresh, drawn = (thing_picture(source, chair, 24, indoor=True, aged=a) for a in (False, True))
    assert middle(fresh).red() > middle(fresh).blue() and middle(drawn).blue() > middle(drawn).red()
    plain, faded = (thing_picture(source, table, 24, indoor=True, aged=a) for a in (False, True))
    assert middle(faded).red() < middle(plain).red()  # no drawing: the picture faded


def test_an_abandoned_room_has_debris_and_no_lamp_light(app):
    from sealedlore.engine.area import build_area

    world, art = world_and_art([{"kind": "object", "name": "a brass lantern", "x": 6, "y": 6}])
    house = build_area(world, art, "house")
    assets = {f"aged/debris{i}": _png("#ff00ff", 20, 20) for i in range(3)}
    source = TileSource(TileSetInfo(name="set"), assets.get)
    kept = render_ground(house, source)
    left = render_ground(house, source, abandoned=True)
    assert kept.image.size() == left.image.size()

    def magenta(image: QImage) -> int:
        return sum(
            1
            for y in range(0, image.height(), 3)
            for x in range(0, image.width(), 3)
            if (c := image.pixelColor(x, y)).red() > 150 and c.blue() > 150 and c.green() < 120
        )

    assert magenta(kept.image) == 0 and magenta(left.image) > 0
    # The lantern lights the kept room, not the abandoned one.
    lantern = next(p for p in house.placements if "lantern" in p.name)
    x, y = int((lantern.x + 0.5) * px_for(house)), int((lantern.y - 1) * px_for(house))
    assert kept.image.pixelColor(x, y).lightness() > left.image.pixelColor(x, y).lightness()


def test_every_abandoned_picture_a_shipped_set_has_is_of_a_picture_it_shows():
    from sealedlore.storage.tile_sets import read_asset, shipped_sets

    for name, (_info, folder) in shipped_sets().items():
        for path in sorted((folder / "aged").glob("*.webp")):
            slug = path.stem
            own = slug.startswith(("debris", "decor")) or slug == "rug"
            assert own or read_asset(folder, f"items/{slug}"), (name, slug)
