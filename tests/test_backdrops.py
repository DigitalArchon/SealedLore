"""Painted backdrops (engine/backdrops.py, engine/session_backdrops.py,
gui/backdrop_render.py): the play view's ground is a painting of the area
when it has one of the map as it is now, with the loose things, people and
marks as sprites over it; one is painted when the map's pictures allow."""

from __future__ import annotations

import os

import pytest

from sealedlore.engine.backdrops import (
    current_backdrop,
    loose_things,
    paint_request,
    painted_things,
)
from sealedlore.engine.world_pictures import area_plan
from sealedlore.models.world import MapPicture
from tests.test_area import make_session


def test_a_backdrop_is_current_only_for_the_map_as_it_is_now(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast)
    world, art = session.world(), session.map_art_copy()
    house = session.area("house")
    assert current_backdrop(world, art, house) is None
    plan = area_plan(world, art, "house")
    old = MapPicture(
        subject="backdrop",
        subject_id="house",
        name="The house",
        file="images/map/a.jpg",
        state="",
        plan="2020-01-01T00:00:00+00:00",
    )
    now = MapPicture(
        subject="backdrop",
        subject_id="house",
        name="The house",
        file="images/map/b.jpg",
        state="",
        plan=plan.made_at if plan is not None else "",
    )
    burnt = MapPicture(
        subject="backdrop",
        subject_id="house",
        name="The house",
        file="images/map/c.jpg",
        state="burnt out",
        plan=plan.made_at if plan is not None else "",
    )
    art.pictures += [old, now, burnt]
    assert current_backdrop(world, art, house) is now  # the latest of the plan and state
    world.places["house"].state = "burnt out"
    assert current_backdrop(world, art, house) is burnt
    world.places["house"].state = "rebuilt"
    assert current_backdrop(world, art, house) is None


def test_fixed_things_are_painted_and_loose_ones_stay_sprites(tmp_path, story, cast):
    session = make_session(
        tmp_path,
        story,
        cast,
        house_objects=[{"kind": "object", "name": "a brass lantern", "x": 6, "y": 6}],
    )
    house = session.area("house")
    painted = {f.name for f in painted_things(house)}
    loose = {f.name for f in loose_things(house)}
    assert "a cast-iron stove" in painted and "a brass lantern" in loose
    assert not (painted & loose) and len(painted) + len(loose) == len(house.placements)
    # A thing the story moved or broke since is a sprite too, over the painting.
    from sealedlore.models.area import ThingRecord

    stove = next(f for f in house.placements if "stove" in f.name)
    records = {stove.id: ThingRecord(state="dropped", name=stove.name, by="")}
    assert stove in loose_things(house, records)


@pytest.mark.skipif(
    os.environ.get("QT_QPA_PLATFORM", "offscreen") != "offscreen", reason="offscreen"
)
def test_the_play_view_lays_its_sprites_over_the_painting(tmp_path, story, cast):
    pytest.importorskip("PySide6")
    from PySide6.QtGui import QColor, QImage

    from sealedlore.gui.backdrop_render import (
        area_picture_size,
        backdrop_ground,
        padded_square,
        trim_painting,
    )
    from sealedlore.gui.image_codec import encoded

    session = make_session(tmp_path, story, cast)
    house = session.area("house")
    w, h = area_picture_size(house)
    # A render padded to a square and a painting trimmed back come out the same shape.
    render = QImage(w, h, QImage.Format_RGB32)
    render.fill(QColor("red"))
    square = padded_square(render, 512)
    assert (square.width(), square.height()) == (512, 512)
    assert square.pixelColor(256, 2) == QColor(128, 128, 128)  # the band
    painting = encoded(square, "PNG")
    trimmed = trim_painting(painting, house)
    ground = backdrop_ground(trimmed, house)
    assert ground is not None and (ground.image.width(), ground.image.height()) == (w, h)
    assert ground.light is None
    # Only the loose things are drawn over it; the fixed ones are in the painting.
    assert {f.name for f in loose_things(house)} < {f.name for f in house.placements}


def test_a_place_painted_in_play_is_painted_bare_and_told_who_lives_there(tmp_path, story, cast):
    from sealedlore.engine.prompt_texts import DEFAULT_TEXTS as texts

    session = make_session(tmp_path, story, cast)
    house = session.area("house")
    shipped = paint_request(house, words="the house")
    played = paint_request(
        house, words="the house", things=False, life=texts["visual.paint.deserted"]
    )
    assert texts["visual.paint.bare"] not in shipped
    assert texts["visual.paint.bare"] in played and "Nobody lives here" in played
    # A painting of the ground alone shows every thing as a sprite; a
    # shipped one keeps its fixed things in the picture.
    assert MapPicture(subject="backdrop", subject_id="x", name="x", file="f").things_painted
