"""Painted backdrops (engine/backdrops.py, engine/session_backdrops.py,
gui/backdrop_render.py): the play view's ground is a painting of the area
when it has one of the map as it is now, with the loose things, people and
marks as sprites over it; one is painted when the map's pictures allow."""

from __future__ import annotations

import os

import pytest

from sealedlore.engine.backdrops import (
    backdrop_job,
    backdrop_request,
    current_backdrop,
    door_words,
    expanded_prompt,
    expander_messages,
    loose_things,
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


def test_the_request_names_the_look_the_place_and_every_door(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast)
    house = session.area("house")
    farm = session.area("farm")
    assert "door out in the" in door_words(house)
    assert "the door of The house in its" in door_words(farm)
    text = backdrop_request(house, words="the inside of The house", style="a storybook look")
    assert text.startswith(
        "Repaint this picture as a storybook look: a lavishly detailed illustrated game map of "
        "the inside of The house, seen from directly above."
    )
    assert "top-down view of the inside of one building" in text
    assert (
        "Keep the layout: every building, wall, fence, road, path, doorway, water and rock" in text
    )
    assert "Everything else is yours to make beautiful" in text
    assert "No people, no writing" in text
    job = backdrop_job(session.world(), session.map_art_copy(), house, words="x", style="y")
    assert job.subject == "backdrop" and job.subject_id == "house" and job.key == "backdrop:house"
    # The expander sees the request with the render first; its reply is one block.
    system, user = expander_messages(text, "data:image/png;base64,AAAA")
    assert system.role == "system" and "prompt upsampler" in system.parts[0].text
    assert user.parts[0].image.startswith("data:image/png") and user.parts[0].text == text
    assert expander_messages(text, None)[1].parts[0].image == ""
    assert expanded_prompt(
        "<think>hm</think>\n```\nConvert the entire image into a painting.\n```"
    ) == ("Convert the entire image into a painting.")
    assert expanded_prompt("ok") == ""


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


def test_a_backdrop_is_wanted_only_with_map_pictures_on_and_none_current(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast)
    house = session.area("house")
    assert not session.backdrop_wanted(house)  # no image endpoint, pictures off
    session.config.map_art = True
    session.config.map_pictures_mode = "auto"
    session.config.providers[0].api_key = "k"
    assert session.map_pictures_on
    assert session.backdrop_wanted(house)
    plan = area_plan(session.world(), session.map_art_copy(), "house")
    art = session.world_art
    art.pictures.append(
        MapPicture(
            subject="backdrop",
            subject_id="house",
            name="The house",
            file="images/map/b.jpg",
            plan=plan.made_at if plan is not None else "",
        )
    )
    session._save_art(art)
    assert not session.backdrop_wanted(house)


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
