"""Pictures of the places a story goes to (the author, Oct 24 2026): a
picture of the place itself, with nobody in it, kept on the place's lore
entry and shown behind the story's text while the scene is there; a button
above the story to draw one, or a new one from the last as the place is now.
Offscreen, never the network: the image endpoint is an httpx MockTransport."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.engine.places import (  # noqa: E402
    PLACE_CAPTION,
    PLACE_CAPTION_AGAIN,
    newest_picture,
    place_for,
    place_size,
    place_title,
)
from sealedlore.gui.image_dialog import ImageDialog  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.settings_dialog import SettingsDialog  # noqa: E402
from sealedlore.ids import utc_now_iso  # noqa: E402
from sealedlore.models.image import ImageRef  # noqa: E402
from sealedlore.models.lore import LoreEntry  # noqa: E402
from sealedlore.providers.images import parse_image_models  # noqa: E402
from sealedlore.storage.images import write_story_file  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402
from tests.test_images import LISTING, PNG  # noqa: E402
from tests.test_images_gui import flush, mock_image_client, wait_for  # noqa: E402

KLEIN = {
    "data": [
        {
            "id": "klein",
            "name": "Klein",
            "pricing": {"per_image": {"768x768": 0.008, "1536x1536": 0.011, "2048x2048": 0.014}},
            "supported_parameters": {"resolutions": ["768x768", "1536x1536", "2048x2048"]},
        },
        {
            "id": "wide",
            "name": "Wide",
            "pricing": {"per_image": {"1024x1024": 0.01, "1280x720": 0.01}},
            "supported_parameters": {"resolutions": ["1024x1024", "1280x720"]},
        },
    ]
}


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_the_scene_s_location_names_its_lore_entry():
    entries = [
        LoreEntry(title="The undercroft", content="Vaults under the keep."),
        LoreEntry(title="Calder Keep", content="A ruin.", keywords=["the keep"]),
        LoreEntry(title="Inn", content="Where the road meets the river."),
    ]
    # The place named first in the location is the nearer one.
    assert place_for("The undercroft beneath Calder Keep", entries).title == "The undercroft"
    assert place_for("Calder Keep, by the undercroft stair", entries).title == "Calder Keep"
    assert place_for("the keep's gate", entries).title == "Calder Keep"  # a keyword
    assert place_for("An inn by the ford", entries).title == "Inn"
    assert place_for("the beginning of the road", entries) is None  # no "inn" inside a word
    assert place_for(None, entries) is None and place_for("  ", entries) is None


def test_a_new_entry_s_title_comes_from_the_location():
    assert place_title("the kitchen of the farmhouse, lamplit") == "The kitchen of the farmhouse"
    assert place_title("Main street — Hellsville") == "Main street"
    assert (
        place_title("The undercroft beneath Calder Keep.") == "The undercroft beneath Calder Keep"
    )
    assert len(place_title("x" * 90)) == 60


def test_a_place_s_picture_is_wide_or_the_largest_square():
    klein, wide = parse_image_models(KLEIN)
    assert place_size(klein) == "1536x1536"  # squares alone: not the 2048
    assert place_size(wide) == "1280x720"
    assert place_size(None) == "1024x768"
    entry = LoreEntry(title="x", content="")
    assert newest_picture(entry) is None and newest_picture(None) is None
    entry.reference_images = [ImageRef(id="a", file="a.png"), ImageRef(id="b", file="b.png")]
    assert newest_picture(entry).id == "b"


@pytest.fixture
def window(app, tmp_path: Path, story, cast) -> MainWindow:
    lore = [
        LoreEntry(
            id="lore-undercroft",
            title="The undercroft",
            content="Vaults under the keep.",
            reference_images=[ImageRef(id="ref-under", file="images/refs/undercroft.png")],
        ),
        LoreEntry(id="lore-gate", title="The gatehouse", content="A broken gate."),
    ]
    bundle = StoryBundle(story=story, cast=cast, lore=lore, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    save_story_bundle(bundle, root=tmp_path)
    write_story_file(story.id, "images/refs/undercroft.png", PNG, tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.image_models = LISTING["data"]
    window.config.image_models_fetched_at = utc_now_iso()
    window.open_story(story.id)
    yield window
    window.image_jobs.stop_all()
    window.image_jobs.wait_all()
    window.close()


def test_the_place_s_picture_stands_behind_the_story_and_the_button_offers_a_new_one(
    window: MainWindow,
):
    session = window.session
    assert session.current_place().id == "lore-undercroft"
    assert window.transcript.has_background and window._place_shown == "ref-under"
    assert not window.place_button.isHidden()
    assert window.place_button.text() == "New picture of this place…"
    assert window.place_action.isVisible() and "The undercroft" in window.place_button.toolTip()
    # The scene moves to a place with no picture: the story's own background
    # (none here) comes back, and the button offers a first picture.
    session.story.scene.location = "The gatehouse"
    window._refresh_scene_panel()
    assert not window.transcript.has_background and window._place_shown == ""
    assert window.place_button.text() == "Picture of this place…"
    # Somewhere no entry names: the button still offers one (an entry is made).
    session.story.scene.location = "A frozen pond"
    window._refresh_scene_panel()
    assert session.current_place() is None and not window.place_button.isHidden()
    # Off: the story's own background stays, whatever the place has.
    window.config.location_backgrounds = False
    session.story.scene.location = "The undercroft beneath Calder Keep"
    window._refresh_scene_panel()
    assert not window.transcript.has_background
    window.config.location_backgrounds = True
    window._refresh_scene_panel()
    assert window.transcript.has_background
    # No location: nothing to picture.
    session.story.scene.location = None
    window._refresh_scene_panel()
    assert window.place_button.isHidden() and not window.transcript.has_background


def test_a_picture_of_a_new_place_makes_its_entry_lands_on_it_and_stands_behind_the_story(
    app, window: MainWindow
):
    session = window.session
    session.story.scene.location = "The frozen pond, under the moon"
    window._refresh_scene_panel()
    dialog = ImageDialog(
        session,
        parent=window,
        place_mode=True,
        place=session.current_place(),
        location=session.story.scene.location,
    )
    assert dialog.windowTitle() == "Picture of The frozen pond" and not dialog.again
    assert dialog.place_name.text() == "The frozen pond" and not dialog.place_name.isReadOnly()
    assert dialog.direction.toPlainText().startswith(
        "A picture of The frozen pond, the place itself"
    )
    assert dialog.model.text() == window.config.portrait_model  # Klein by default
    dialog.model.setText("hidream")
    dialog.size_choice.setCurrentText("1024x1024")
    dialog.prompt.setPlainText("A frozen pond under the moon, snow on the reeds.")
    dialog._approve()
    request = dialog.request
    assert dialog.made_place
    entry = next(e for e in session.bundle.lore if e.title == "The frozen pond")
    assert entry.content == "The frozen pond, under the moon"
    assert request.card_id == entry.id and request.card_first
    assert request.card_caption == PLACE_CAPTION and request.anchor_node_id is None
    assert request.references == ()
    dialog.deleteLater()
    window.image_jobs.start(request, mock_image_client(delay=0.1), session.pictures)
    wait_for(app, lambda: not window.image_jobs.pending())
    flush(app)
    assert len(entry.reference_images) == 1
    assert session.pictures.read(entry.reference_images[0].file) == PNG
    assert window.transcript.has_background
    assert window._place_shown == entry.reference_images[0].id
    assert window.place_button.text() == "New picture of this place…"
    # Again: the last picture goes as the reference, and the new one after it.
    dialog = ImageDialog(
        session, parent=window, place_mode=True, place=entry, location=session.story.scene.location
    )
    assert dialog.again and dialog.windowTitle() == "New picture of The frozen pond"
    assert dialog.place_name.isReadOnly()
    assert "again, as it is now" in dialog.direction.toPlainText()
    assert [use.ref_id for use in dialog._refs] == [entry.reference_images[0].id]
    dialog.model.setText("bytedance/seedream-v5.0-pro")
    dialog.size_choice.setCurrentText("1k")
    dialog.prompt.setPlainText("The same pond, the ice broken and black water showing.")
    dialog._approve()
    request = dialog.request
    assert not dialog.made_place
    assert request.card_id == entry.id and not request.card_first
    assert request.card_caption == PLACE_CAPTION_AGAIN
    assert [use.ref_id for use in request.references] == [entry.reference_images[0].id]
    dialog.deleteLater()
    window.image_jobs.start(request, mock_image_client(delay=0.1), session.pictures)
    wait_for(app, lambda: not window.image_jobs.pending())
    flush(app)
    assert len(entry.reference_images) == 2
    assert window._place_shown == entry.reference_images[1].id  # the newest shows


def test_settings_keep_the_choice(app, window: MainWindow):
    dialog = SettingsDialog(window.config, window.session.story, window)
    assert dialog.location_backgrounds.isChecked()
    dialog.location_backgrounds.setChecked(False)
    dialog._save()
    assert window.config.location_backgrounds is False
    dialog.deleteLater()
