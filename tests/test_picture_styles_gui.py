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
