"""Choosing a picture style and a portrait's framing in the window: Setup, the
portraits dialog (a change writes the prompts again, all but those changed by
hand) and the style it saves for the story. Offscreen; the mock provider."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.engine.picture_styles import DEFAULT_LABEL, OWN_LABEL, STYLES  # noqa: E402
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS  # noqa: E402
from sealedlore.engine.session import StorySession  # noqa: E402
from sealedlore.engine.tokens import TokenEstimator, fallback_counter  # noqa: E402
from sealedlore.gui import window_images  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.picture_style import OWN, PictureStyleField  # noqa: E402
from sealedlore.gui.portraits_dialog import PortraitsDialog  # noqa: E402
from sealedlore.gui.setup_dialog import SetupDialog  # noqa: E402
from sealedlore.models.config import Config  # noqa: E402
from sealedlore.models.story import Story  # noqa: E402
from sealedlore.providers.images import ImageModelInfo  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402

KLEIN = ImageModelInfo(
    id="blackforestlabs/flux-3/klein/text-to-image",
    name="FLUX.3 Klein",
    resolutions=("768x768", "1024x1024", "1536x1536", "2048x2048"),
    max_inputs=10,
    per_image={"1024x1024": 0.009, "1536x1536": 0.011},
)


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def wait_for(app: QApplication, condition, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        app.processEvents()
        time.sleep(0.01)


class _Catalog:
    """The window's ImageCatalog, as the dialog uses it: a listing."""

    models = [KLEIN]

    class changed:  # noqa: N801 - a signal's stand-in
        @staticmethod
        def connect(_slot):
            return None

    def ensure(self, *args, **kwargs):
        return None


def replies(cast, words: str) -> str:
    return json.dumps(
        {"portraits": [{"name": c.name, "prompt": f"{words} {c.name}."} for c in cast]}
    )


def _session(tmp_path: Path, story, cast, answers: list[str]) -> StorySession:
    bundle = StoryBundle(story=story, cast=cast)
    save_story_bundle(bundle, root=tmp_path)
    return StorySession(
        bundle,
        Config(),
        MockChatProvider(answers),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def test_the_field_offers_the_default_every_style_and_your_own_words(app):
    field = PictureStyleField("", "")
    labels = [field.combo.itemText(i) for i in range(field.combo.count())]
    assert labels == [DEFAULT_LABEL, *(s.label for s in STYLES), OWN_LABEL]
    assert field.value() == ("", "") and field.own.isHidden()
    # Each style's tooltip is what it puts into a prompt.
    anime = field.combo.findData("anime")
    assert field.combo.itemData(anime, 3) == DEFAULT_TEXTS["pictures.styles.anime"]
    field.combo.setCurrentIndex(anime)
    assert field.value() == ("anime", "")
    field.combo.setCurrentIndex(field.combo.findData(OWN))
    assert not field.own.isHidden()
    field.own.setText("  ink   wash ")
    assert field.value() == ("", "ink wash")
    # A story's own words open as such; a ready-made style wins over old words.
    assert PictureStyleField("", "oil paint").value() == ("", "oil paint")
    assert PictureStyleField("comic", "oil paint").value() == ("comic", "")


def test_setup_saves_the_style_chosen(app):
    story = Story(title="T", image_style="oil paint")
    dialog = SetupDialog(story, [])
    assert dialog.picture_style.value() == ("", "oil paint")
    dialog.picture_style.combo.setCurrentIndex(dialog.picture_style.combo.findData("gritty"))
    dialog._save()
    assert (story.picture_style, story.image_style) == ("gritty", "")
    dialog.deleteLater()


def test_the_dialog_draws_in_the_style_and_framing_chosen_and_rewrites_what_was_not_changed(
    app, tmp_path: Path, story, cast
):
    # Written on opening, on the style's change, and once more for the framing
    # chosen while that was being written: in both.
    answers = [replies(cast, w) for w in ("A face for", "Anime, head for", "A whole figure for")]
    session = _session(tmp_path, story, cast, answers)
    dialog = PortraitsDialog(session, cast, catalog=_Catalog(), browse=None)
    wait_for(app, lambda: not dialog.writing())
    assert dialog.chosen_framing() == "head" and dialog.chosen_style() == ("", "")
    first = DEFAULT_TEXTS["portraits.style"] + ", head and shoulders."
    assert all(row.prompt.toPlainText().startswith(first) for row in dialog.rows)
    # The author changes one prompt, then asks for anime, full length.
    dialog.rows[0].prompt.setPlainText("My own words.")
    dialog.style.combo.setCurrentIndex(dialog.style.combo.findData("anime"))
    dialog.framing.setCurrentIndex(dialog.framing.findData("full"))
    wait_for(app, lambda: not dialog.writing())
    app.processEvents()
    assert dialog.rows[0].prompt.toPlainText() == "My own words."
    anime = DEFAULT_TEXTS["pictures.styles.anime"]
    for row in dialog.rows[1:]:
        text = row.prompt.toPlainText()
        assert text.startswith(f"{anime}, full body") and "A whole figure for" in text
    # Written once more, for the rows not changed, in the last style and framing.
    sent = json.dumps(session.provider.payloads[-1])
    assert DEFAULT_TEXTS["portraits.framing.full"] in sent and cast[0].name not in sent
    requests = dialog.requests()
    assert {r.size for r in requests} == {"1536x1536"}
    assert {r.card_framing for r in requests} == {"full"}
    assert dialog.chosen_style() == ("anime", "")
    dialog.reject()


def test_a_style_chosen_in_the_dialog_becomes_the_story_s_when_drawn(
    app, tmp_path: Path, story, cast, monkeypatch
):
    bundle = StoryBundle(story=story, cast=cast)
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(story.id)
    try:

        class Chosen:
            def __init__(self, *args, **kwargs):
                pass

            def exec(self):
                return True

            def chosen_model(self):
                return window.config.portrait_model

            def chosen_style(self):
                return ("watercolour", "")

            class style:  # noqa: N801 - the dialog's field, as the window reads it
                @staticmethod
                def picture_choice():
                    return ("keep", None)

            def requests(self):
                return []

            def deleteLater(self):
                pass

        monkeypatch.setattr(window_images, "PortraitsDialog", Chosen)
        monkeypatch.setattr(window, "_card_pictures_refused", lambda _session: False)
        window.draw_card_pictures(window.session.cast[:1], [])
        assert window.session.story.picture_style == "watercolour"
        from sealedlore.storage.repository import load_story_bundle

        assert load_story_bundle(story.id, root=tmp_path).story.picture_style == "watercolour"
    finally:
        window.close()


# --- step 2: a picture's own style, restyling, reframing, a style from a picture --------


@pytest.fixture
def window(app, tmp_path: Path, story, cast):
    from sealedlore.models.image import ImageRef
    from tests.conftest import make_exchange

    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(story.id)
    session = window.session
    refs = []
    for ref_id, look, expression, style in (
        ("p", "normal clothes", "neutral", ""),
        ("h", "normal clothes", "happy", "anime"),
        ("a", "combat armour", "neutral", ""),
    ):
        session.pictures.write(f"images/refs/{ref_id}.png", PNG)
        refs.append(
            ImageRef(
                id=ref_id,
                file=f"images/refs/{ref_id}.png",
                look=look,
                expression=expression,
                style=style,
            )
        )
    session.cast[0].reference_images = refs
    yield window
    window.close()


def _png() -> bytes:
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    raw = b"\0\x80\x80\x80" * 2
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


PNG = _png()


def test_a_picture_of_the_story_may_have_its_own_style(app, window):
    from sealedlore.gui.image_dialog import ImageDialog

    session = window.session
    session.story.picture_style = "gritty"
    first = '{"references": [], "prompt": "Two figures by a fire."}'
    again = '{"references": [], "prompt": "Two figures laughing by a fire."}'
    session.provider = MockChatProvider([first, again])
    dialog = ImageDialog(session, parent=window)
    assert dialog.style.value() == ("gritty", "")
    assert "Gritty and realistic" in dialog.style_note.text()
    dialog._write()
    wait_for(app, lambda: dialog._worker is None)
    gritty = DEFAULT_TEXTS["pictures.styles.gritty"]
    assert dialog.prompt.toPlainText() == f"{gritty}. Two figures by a fire."
    # Another style for this picture: written again, untouched as it was.
    dialog.style.combo.setCurrentIndex(dialog.style.combo.findData("anime"))
    wait_for(app, lambda: dialog._worker is None)
    anime = DEFAULT_TEXTS["pictures.styles.anime"]
    assert dialog.prompt.toPlainText() == f"{anime}. Two figures laughing by a fire."
    # Changed by hand, only its style line is swapped.
    dialog.prompt.setPlainText(f"{anime}. My own scene.")
    dialog.style.combo.setCurrentIndex(dialog.style.combo.findData("comic"))
    comic = DEFAULT_TEXTS["pictures.styles.comic"]
    assert dialog.prompt.toPlainText() == f"{comic}. My own scene."
    assert session.story.picture_style == "gritty"  # the story's is unchanged
    dialog.reject()


def test_change_this_picture_redraws_it_in_a_style_or_shows_more_of_them(app, window):
    from sealedlore.gui.picture_variant import PictureVariantDialog

    session = window.session
    card = session.cast[0]
    dialog = PictureVariantDialog(
        session, card, card.reference_images[0], edit=True, catalog=_Catalog(), browse=None
    )
    dialog.model.setText(KLEIN.id)
    assert dialog.style.value()[0] == "keep" and dialog.chosen_framing() == ""
    assert "change nothing else" in dialog.prompt.toPlainText()  # an edit, as before
    assert not dialog.draw_button.isEnabled()  # nothing asked for yet
    # Another framing alone, or another style alone, is enough to draw.
    dialog.framing.setCurrentIndex(dialog.framing.findData("torso"))
    assert dialog.draw_button.isEnabled()
    dialog.framing.setCurrentIndex(0)
    assert not dialog.draw_button.isEnabled()
    dialog.style.combo.setCurrentIndex(dialog.style.combo.findData("watercolour"))
    assert dialog.draw_button.isEnabled()
    dialog.framing.setCurrentIndex(dialog.framing.findData("full"))
    prompt = dialog.prompt.toPlainText()
    watercolour = DEFAULT_TEXTS["pictures.styles.watercolour"]
    assert prompt.startswith(f"Redraw this picture entirely in this style: {watercolour}.")
    assert DEFAULT_TEXTS["pictures.reframe.full"] in prompt
    request = dialog.request()
    assert (request.card_framing, request.card_style, request.size) == (
        "full",
        "watercolour",
        "1536x1536",
    )
    assert request.card_look == "normal clothes"  # its labels kept
    dialog.reject()


def test_restyle_pictures_ticks_those_not_in_the_style_and_each_takes_its_place(
    app, window, monkeypatch
):
    from sealedlore.gui.restyle_dialog import RestyleDialog

    session = window.session
    card = session.cast[0]
    session.story.picture_style = "anime"
    dialog = RestyleDialog(session, [card], catalog=_Catalog(), browse=None)
    # The one already drawn in anime is left out to begin with.
    assert [row.tick.isChecked() for row in dialog.rows] == [True, False, True]
    anime = DEFAULT_TEXTS["pictures.styles.anime"]
    assert all(anime in row.prompt.toPlainText() for row in dialog.rows)
    requests = dialog.requests()
    assert [r.card_replaces for r in requests] == ["p", "a"]
    assert {r.card_style for r in requests} == {"anime"}
    assert [(r.card_look, r.card_expression) for r in requests] == [
        ("normal clothes", "neutral"),
        ("combat armour", "neutral"),
    ]
    # Another style rewrites every prompt not changed by hand.
    dialog.rows[0].prompt.setPlainText("My own words.")
    dialog.style.combo.setCurrentIndex(dialog.style.combo.findData("comic"))
    comic = DEFAULT_TEXTS["pictures.styles.comic"]
    assert dialog.rows[0].prompt.toPlainText() == "My own words."
    assert comic in dialog.rows[2].prompt.toPlainText()
    dialog.reject()
    # With no listing yet it opens all the same, with no price.
    unlisted = RestyleDialog(session, [card], catalog=None, browse=None)
    assert "price isn't listed" in unlisted.note.text()
    unlisted.reject()
    # The window's Story → Restyle pictures… and the board's Restyle….
    asked: list = []
    monkeypatch.setattr(
        window_images,
        "ask_restyle",
        lambda parent, session, cards, **kw: asked.append([c.id for c in cards]) or [],
    )
    # No image endpoint here: the check would say so in a message box.
    monkeypatch.setattr(window, "_card_pictures_refused", lambda _session: False)
    window._update_image_controls()
    window.restyle_action.trigger()
    window.show_cast_board(True)
    window.cast_board.show_character(card.id)
    window.cast_board.restyle_button.click()
    assert asked == [[card.id], [card.id]]


def test_a_story_s_style_can_be_taken_from_a_picture(app, window, monkeypatch, tmp_path: Path):
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    from sealedlore.gui import style_from_picture

    field = PictureStyleField("", "", from_picture=lambda: ("woodcut, bold lines", PNG, ".png"))
    taken: list[bool] = []
    field.taken.connect(lambda: taken.append(True))
    field.from_picture.click()
    assert field.value() == ("", "woodcut, bold lines") and not field.own.isHidden()
    # The picture goes with its words, until they are left for another style.
    assert taken == [True] and field.picture_wanted()
    assert field.picture_choice() == ("set", (PNG, ".png"))
    field.with_picture.setChecked(False)
    assert field.picture_choice() == ("keep", None)  # never kept: nothing to let go
    kept = PictureStyleField("", "woodcut", picture=True)
    assert kept.picture_choice() == ("keep", None)
    kept.combo.setCurrentIndex(kept.combo.findData("anime"))
    assert kept.picture_choice() == ("clear", None)

    session = window.session
    session.provider = MockChatProvider(["Woodblock print, flat muted colours."])
    picture = tmp_path / "style.png"
    picture.write_bytes(PNG)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(picture), ""))
    monkeypatch.setattr(style_from_picture, "cropped_picture", lambda *a, **k: (PNG, ".png"))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    found = style_from_picture.describe_style(window, session, {})
    assert found == ("Woodblock print, flat muted colours", PNG, ".png")
    assert session.provider.requests[-1].messages[0].parts[0].image.startswith("data:image/png")

    # A prompt writer known not to see pictures is said, and nothing is sent.
    class Blind:
        vision = False

    said: list[str] = []
    monkeypatch.setattr(QMessageBox, "information", lambda _p, _t, text: said.append(text))
    sent = len(session.provider.requests)
    assert (
        style_from_picture.describe_style(window, session, {session.image_prompt_model: Blind()})
        is None
    )
    assert "can't see pictures" in said[0] and len(session.provider.requests) == sent


def test_the_style_picture_goes_with_portraits_and_restyles_when_ticked(app, window):
    from sealedlore.engine.picture_styles import STYLE_REF_ID
    from sealedlore.gui.restyle_dialog import RestyleDialog

    session = window.session
    card = session.cast[0]
    session.story.picture_style, session.story.image_style = "", "woodcut, bold black lines"
    # Setup's choice, as the window keeps it.
    window._apply_style_picture(("set", (PNG, ".png")))
    assert session.style_reference() is not None

    reply = json.dumps({"portraits": [{"name": card.name, "prompt": "A tired man."}]})
    session.provider = MockChatProvider([reply, reply])
    dialog = PortraitsDialog(session, [card], catalog=_Catalog(), browse=None)
    wait_for(app, lambda: not dialog.writing())
    assert dialog.style.picture_wanted() and "as image 1" in dialog.sends.text()
    reference = DEFAULT_TEXTS["pictures.style.reference"].replace("{number}", "1")
    assert dialog.rows[0].prompt.toPlainText().startswith(reference)
    requests = dialog.requests()
    assert [r.ref_id for r in requests[0].references] == [STYLE_REF_ID]
    # Unticked, the prompts are written again without it and it stays home.
    dialog.style.with_picture.setChecked(False)
    wait_for(app, lambda: not dialog.writing())
    assert not dialog.rows[0].prompt.toPlainText().startswith(reference)
    assert dialog.requests()[0].references == ()
    assert dialog.style.picture_choice() == ("clear", None)
    dialog.reject()

    restyle = RestyleDialog(session, [card], catalog=_Catalog(), browse=None)
    prompt = restyle.rows[0].prompt.toPlainText()
    assert prompt.startswith("Redraw image 1 entirely in the art style of image 2")
    assert [r.ref_id for r in restyle.requests()[0].references] == ["p", STYLE_REF_ID]
    restyle.style.with_picture.setChecked(False)
    assert restyle.rows[0].prompt.toPlainText().startswith("Redraw this picture entirely")
    restyle.reject()

    window._apply_style_picture(("clear", None))
    assert session.story.style_picture == ""
