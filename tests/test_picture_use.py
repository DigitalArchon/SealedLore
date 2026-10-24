"""A picture the story made kept as a card's own (the author, Oct 24 2026):
Use as a character's picture… / Use as a place's picture… frame the part that
matters, keep it as the portrait or the place's picture (or among the card's
pictures), or keep it and draw a new picture from it. Offscreen, never the
network."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.engine.picture_use import (  # noqa: E402
    FROM_SCENE_CAPTION,
    KEEP_DRAW,
    KEEP_MAIN,
    KEEP_OTHER,
    from_picture_direction,
    keep_picture,
)
from sealedlore.engine.places import (  # noqa: E402
    PLACE_CAPTION,
    PLACE_CAPTION_AGAIN,
    newest_picture,
)
from sealedlore.gui.image_dialog import ImageDialog  # noqa: E402
from sealedlore.gui.images_panel import (  # noqa: E402
    ADDED_ACTIONS,
    BACKGROUND_ACTIONS,
    CARD_ACTIONS,
)
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.picture_use import CHARACTER, NEW_PLACE, PLACE, UsePictureDialog  # noqa: E402
from sealedlore.gui.picture_words import USE_CHARACTER_LABEL, USE_PLACE_LABEL  # noqa: E402
from sealedlore.gui.ref_images import _encoded, image_of  # noqa: E402
from sealedlore.gui.transcript import IMAGE_ACTIONS  # noqa: E402
from sealedlore.gui.video_view import VIDEO_ACTIONS  # noqa: E402
from sealedlore.ids import utc_now_iso  # noqa: E402
from sealedlore.models.character import Character  # noqa: E402
from sealedlore.models.image import ImageRef  # noqa: E402
from sealedlore.models.lore import LoreEntry  # noqa: E402
from sealedlore.storage.images import write_story_file  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402
from tests.test_images import LISTING, PNG  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _ref(name: str) -> ImageRef:
    return ImageRef(id=name, file=f"images/refs/{name}.png")


def test_a_kept_picture_goes_where_the_card_shows_it():
    # A character's main picture is the portrait, first; the others after.
    person = Character(id="c", name="Ann", reference_images=[_ref("old")])
    keep_picture(person, _ref("main"), KEEP_MAIN)
    keep_picture(person, _ref("other"), KEEP_OTHER)
    keep_picture(person, _ref("draw"), KEEP_DRAW)
    assert [r.id for r in person.reference_images] == ["main", "old", "other", "draw"]
    # A place's main picture is the newest, last (shown behind the story);
    # one among the others goes before the current one, so that stays current.
    place = LoreEntry(id="p", title="The bar", content="A bar.", reference_images=[_ref("old")])
    keep_picture(place, _ref("main"), KEEP_MAIN)
    assert newest_picture(place).id == "main"
    keep_picture(place, _ref("other"), KEEP_OTHER)
    assert newest_picture(place).id == "main"
    keep_picture(place, _ref("draw"), KEEP_DRAW)
    assert [r.id for r in place.reference_images] == ["old", "other", "main", "draw"]
    # With no picture yet, whatever is kept is the only one.
    empty = LoreEntry(id="e", title="The well", content="A well.")
    keep_picture(empty, _ref("only"), KEEP_OTHER)
    assert [r.id for r in empty.reference_images] == ["only"]
    with pytest.raises(AssertionError):
        keep_picture(empty, _ref("x"), "elsewhere")


def test_the_direction_to_draw_from_a_kept_part_names_the_card():
    person = from_picture_direction(Character(id="c", name="Ann Vale"))
    assert person.startswith("A portrait of Ann Vale, the person shown in image 1")
    assert "Nobody else" in person
    place = from_picture_direction(LoreEntry(id="p", title="The bar", content="A bar."))
    assert place.startswith("A picture of The bar, the place shown in image 1")
    assert "nobody in it" in place


def test_every_picture_menu_offers_the_two_uses():
    for actions in (IMAGE_ACTIONS, CARD_ACTIONS, ADDED_ACTIONS):
        assert (CHARACTER, USE_CHARACTER_LABEL) in actions
        assert (PLACE, USE_PLACE_LABEL) in actions
    assert (PLACE, USE_PLACE_LABEL) in BACKGROUND_ACTIONS
    assert ("frame", "Use its last frame as a card's picture…") in VIDEO_ACTIONS
    assert not any(key == "reference" for key, _ in IMAGE_ACTIONS)


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
    bundle.story.held_character_id = "char-maela"
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


def _scene_picture() -> bytes:
    """A 600 × 400 picture of the story, a dark left half and a light right."""
    image = QImage(600, 400, QImage.Format_RGB32)
    image.fill(QColor("#203040"))
    for y in range(400):
        for x in range(300, 600):
            image.setPixelColor(x, y, QColor("#e0d0c0"))
    return _encoded(image, "PNG")


def test_the_dialog_frames_a_part_for_the_chosen_card(window: MainWindow):
    session = window.session
    data = _scene_picture()
    dialog = UsePictureDialog(session, data, PLACE, window)
    # For a place: the scene's own place first, in a place picture's shape.
    assert dialog.card().id == "lore-undercroft"
    assert dialog.shape.currentData() == "picture"
    assert dialog.keep_main.isChecked() and dialog.keep_other.isEnabled()
    assert "shown behind the story" in dialog.keep_main.text()
    # The whole picture is kept as it is.
    dialog._take_whole()
    assert dialog.whole() and dialog.size_label.text().startswith("The whole picture, 600 × 400")
    dialog._approve()
    use = dialog.result_use
    assert use.card.id == "lore-undercroft" and use.how == KEEP_MAIN
    assert use.caption == PLACE_CAPTION and use.suffix == ".png" and use.data == data
    dialog.deleteLater()
    # For a character: the one played first, a portrait's shape; a part is cut out.
    dialog = UsePictureDialog(session, data, CHARACTER, window)
    assert dialog.card().id == "char-maela" and dialog.shape.currentData() == "portrait"
    assert not dialog.keep_other.isEnabled()  # no picture yet: it would be the only one
    dialog.shape.setCurrentIndex(dialog.shape.findData("free"))
    dialog.view.set_rect((300.0, 0.0, 300.0, 400.0))
    assert not dialog.whole()
    dialog.caption.setText("  as she came in  ")
    dialog.keep_draw.setChecked(True)
    assert dialog.keep_button.text() == "Keep and draw…"
    dialog._approve()
    use = dialog.result_use
    assert use.card.id == "char-maela" and use.how == KEEP_DRAW
    assert use.caption == "as she came in" and use.suffix == ".jpg"
    part = image_of(use.data)
    assert (part.width(), part.height()) == (300, 400)
    assert part.pixelColor(150, 200).lightness() > 180  # the light half alone
    dialog.deleteLater()
    # Somewhere no entry names: a new entry for the scene's location is offered.
    session.story.scene.location = "A frozen pond, under the moon"
    dialog = UsePictureDialog(session, data, PLACE, window)
    assert dialog.card_choice.currentData() == NEW_PLACE and dialog.card() is None
    assert dialog.new_title() == "A frozen pond"
    assert "the picture of A frozen pond" in dialog.keep_main.text()
    dialog._approve()
    assert dialog.result_use.card is None and dialog.result_use.new_title == "A frozen pond"
    dialog.deleteLater()


def test_a_kept_part_lands_on_its_card_and_a_drawing_is_asked_from_it(
    window: MainWindow, monkeypatch
):
    session = window.session
    character = session.card("char-maela")
    data = _scene_picture()

    class Chosen:
        """The dialog answered: the whole picture, kept to draw from."""

        def __init__(self, sess, picture, kind, parent, **_kw):
            from sealedlore.gui.picture_use import PictureUse

            assert sess is session and picture == data and kind == CHARACTER
            self.result_use = PictureUse(
                card=character,
                new_title="",
                data=picture,
                suffix=".png",
                caption=FROM_SCENE_CAPTION,
                how=KEEP_DRAW,
            )

        def exec(self):
            return True

        def deleteLater(self):
            pass

    asked: list[dict] = []
    monkeypatch.setattr("sealedlore.gui.window_images.UsePictureDialog", Chosen)
    monkeypatch.setattr(window, "generate_image", lambda **kwargs: asked.append(kwargs) or True)
    window._use_picture(data, CHARACTER)
    assert len(character.reference_images) == 1
    ref = character.reference_images[0]
    assert ref.caption == FROM_SCENE_CAPTION and session.pictures.read(ref.file) == data
    assert asked == [{"card": character, "source": ref}]
    # The picture dialog it opens: a portrait from the part alone, which
    # becomes the portrait once drawn.
    dialog = ImageDialog(session, parent=window, card=character, source=ref)
    assert dialog.windowTitle() == "A portrait of Maela Orr from the picture"
    assert dialog.direction.toPlainText().startswith(
        "A portrait of Maela Orr, the person shown in image 1"
    )
    assert [use.ref_id for use in dialog._refs] == [ref.id]
    assert dialog.model.text() == window.config.portrait_model
    dialog.model.setText("bytedance/seedream-v5.0-pro")
    dialog.size_choice.setCurrentText("1k")
    dialog.prompt.setPlainText("A woman with ink-stained fingers, head and shoulders.")
    dialog._approve()
    request = dialog.request
    assert request.card_id == character.id and request.card_first
    assert request.card_caption == "portrait" and request.anchor_node_id is None
    assert [use.ref_id for use in request.references] == [ref.id]
    dialog.deleteLater()


def test_a_part_kept_as_a_place_s_picture_stands_behind_the_story(window: MainWindow, monkeypatch):
    session = window.session
    entry = session.card("lore-undercroft")
    assert window._place_shown == "ref-under"
    data = _scene_picture()

    class Chosen:
        def __init__(self, sess, picture, kind, parent, **_kw):
            from sealedlore.gui.picture_use import PictureUse

            self.result_use = PictureUse(
                card=entry, new_title="", data=picture, suffix=".png", caption="", how=KEEP_MAIN
            )

        def exec(self):
            return True

        def deleteLater(self):
            pass

    monkeypatch.setattr("sealedlore.gui.window_images.UsePictureDialog", Chosen)
    window._use_picture(data, PLACE)
    assert [r.id for r in entry.reference_images][0] == "ref-under"
    kept = newest_picture(entry)
    assert kept.id != "ref-under" and session.pictures.read(kept.file) == data
    assert window._place_shown == kept.id  # the place's picture now, behind the story
    assert window.statusBar().currentMessage() == "Kept on The undercroft's card."
    # Drawn from a kept part: the picture dialog asks for the place with
    # nobody in it, the part as image 1, and the drawing goes newest.
    dialog = ImageDialog(
        session, parent=window, place_mode=True, place=entry, location=entry.title, source=kept
    )
    assert dialog.windowTitle() == "The undercroft from the picture"
    assert dialog.direction.toPlainText().startswith(
        "A picture of The undercroft, the place shown in image 1"
    )
    assert [use.ref_id for use in dialog._refs] == [kept.id]
    dialog.model.setText("bytedance/seedream-v5.0-pro")
    dialog.size_choice.setCurrentText("1k")
    dialog.prompt.setPlainText("The vaults, empty.")
    dialog._approve()
    request = dialog.request
    assert request.card_id == entry.id and not request.card_first
    assert request.card_caption == PLACE_CAPTION_AGAIN
    assert [use.ref_id for use in request.references] == [kept.id]
    dialog.deleteLater()


def test_a_new_entry_is_made_for_a_place_no_entry_names(window: MainWindow, monkeypatch):
    session = window.session
    session.story.scene.location = "A frozen pond, under the moon"
    window._refresh_scene_panel()
    data = _scene_picture()

    class Chosen:
        def __init__(self, sess, picture, kind, parent, **_kw):
            from sealedlore.gui.picture_use import PictureUse

            self.result_use = PictureUse(
                card=None,
                new_title="A frozen pond",
                data=picture,
                suffix=".png",
                caption="",
                how=KEEP_MAIN,
            )

        def exec(self):
            return True

        def deleteLater(self):
            pass

    monkeypatch.setattr("sealedlore.gui.window_images.UsePictureDialog", Chosen)
    window._use_picture(data, PLACE)
    entry = next(e for e in session.bundle.lore if e.title == "A frozen pond")
    assert entry.content == "A frozen pond, under the moon"
    assert len(entry.reference_images) == 1
    assert session.current_place() is entry
    assert window._place_shown == entry.reference_images[0].id
