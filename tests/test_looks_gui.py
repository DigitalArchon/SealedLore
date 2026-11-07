"""Looks in the window: drawing another look and a set of expressions
(gui/picture_variant.py, gui/expression_set.py), from the Cast and People
pages. Offscreen; nothing is sent to an image model."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

import sealedlore.gui.window_images as window_images  # noqa: E402
from sealedlore.gui.expression_set import ExpressionSetDialog  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.picture_variant import PictureVariantDialog  # noqa: E402
from sealedlore.models.image import ImageRef  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path: Path, story, cast):
    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    bundle.story.held_character_id = "char-serrik"
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(story.id)
    session = window.session
    session.pictures.write("images/refs/p.png", b"portrait")
    session.pictures.write("images/refs/a.png", b"armour")
    serrik = next(c for c in session.cast if c.id == "char-serrik")
    serrik.reference_images = [
        ImageRef(id="p", file="images/refs/p.png", caption="portrait", look="normal clothes"),
        ImageRef(id="a", file="images/refs/a.png", look="combat armour", expression="neutral"),
    ]
    yield window
    window.close()


def serrik(window: MainWindow):
    return next(c for c in window.session.cast if c.id == "char-serrik")


def test_another_look_starts_from_the_picture_s_labels_and_asks_for_what_changed(app, window):
    character = serrik(window)
    dialog = PictureVariantDialog(
        window.session,
        character,
        character.reference_images[1],
        edit=True,
        catalog=None,
        browse=None,
    )
    assert dialog.labelled
    assert (dialog.look.currentText(), dialog.expression.currentText()) == (
        "combat armour",
        "neutral",
    )
    # Nothing changed yet: nothing to draw.
    assert not dialog.draw_button.isEnabled()
    dialog.expression.setCurrentText("angry")
    prompt = dialog.prompt.toPlainText()
    assert "Change their expression or pose to: an angry expression," in prompt
    assert "clothes or state" not in prompt  # the look stays the picture's own
    assert dialog.draw_button.isEnabled()
    request = dialog.request()
    assert (request.card_look, request.card_expression) == ("combat armour", "angry")
    assert [use.ref_id for use in request.references] == ["a"]
    dialog.deleteLater()


def test_a_set_of_expressions_is_one_edit_each_every_prompt_shown(app, window):
    character = serrik(window)
    dialog = ExpressionSetDialog(
        window.session, character, source_id="a", catalog=None, browse=None
    )
    assert dialog.source_ref().id == "a"
    # Neutral is left out: the armour picture is his neutral one already.
    assert list(dialog._boxes) == ["happy", "sad", "angry"]
    dialog.ticks["neutral"].setChecked(False)
    dialog.others.setText("laughing, Angry, asleep")
    assert list(dialog._boxes) == ["happy", "sad", "angry", "laughing", "asleep"]
    # A prompt the author changed is kept as the set changes.
    dialog._boxes["sad"].setPlainText("Make him look quietly sad.")
    dialog.ticks["happy"].setChecked(False)
    assert dialog._boxes["sad"].toPlainText() == "Make him look quietly sad."
    assert dialog.draw_button.text() == "Draw 4"
    requests = dialog.requests()
    assert [r.card_expression for r in requests] == ["sad", "angry", "laughing", "asleep"]
    assert all(r.card_look == "combat armour" for r in requests)
    assert all([u.ref_id for u in r.references] == ["a"] for r in requests)
    assert requests[0].prompt == "Make him look quietly sad."
    assert "Change their expression or pose to: laughing." in requests[2].prompt
    dialog.deleteLater()


def test_a_set_of_expressions_begins_with_only_what_the_look_lacks(app, window):
    """Doomsville's John has neutral, happy, sad and angry in his coat:
    ticked anyway, they were drawn and paid for again (the lab walk)."""
    character = serrik(window)
    for ref_id, expression in (("h", "happy"), ("s", "sad"), ("g", "angry")):
        character.reference_images.append(
            ImageRef(
                id=ref_id, file="images/refs/p.png", look="normal clothes", expression=expression
            )
        )
    character.reference_images[0].expression = "neutral"
    dialog = ExpressionSetDialog(
        window.session, character, source_id="p", catalog=None, browse=None
    )
    assert dialog.expressions() == [] and not dialog.draw_button.isEnabled()
    # Another picture to draw from: its own look's gaps.
    dialog.source.setCurrentIndex(dialog.source.findData("a"))
    assert dialog.expressions() == ["happy", "sad", "angry"]
    # Once a tick has been clicked, the author's choice stands.
    dialog.ticks["afraid"].click()
    dialog.source.setCurrentIndex(dialog.source.findData("p"))
    assert dialog.expressions() == ["happy", "sad", "angry", "afraid"]
    dialog.deleteLater()


def test_the_people_page_draws_from_the_picture_shown_now(app, window, monkeypatch):
    window.session.show_look("char-serrik", "a")
    window.config.image_endpoint.base_url = "https://nano-gpt.com/api/v1"
    window._build_people()
    card = window.people_page.cards["char-serrik"]
    actions = dict(card.menu_actions())
    asked: list[tuple] = []
    monkeypatch.setattr(
        window_images,
        "ask_expression_set",
        lambda parent, session, character, **kw: (
            asked.append((character.id, kw["source_id"])) or ([], "")
        ),
    )
    monkeypatch.setattr(window, "_card_pictures_refused", lambda session: False)
    actions["A set of expressions…"]()
    assert asked == [("char-serrik", "a")]
    assert "Draw another look…" in actions


def test_a_look_found_missing_is_offered_to_draw(app, window, monkeypatch):
    from PySide6.QtWidgets import QDialog

    window.config.image_endpoint.base_url = "https://nano-gpt.com/api/v1"
    window.session.full_path()[-1].meta.looks_wanted = {"char-serrik": "combat armour · grinning"}
    window._build_people()
    actions = dict(window.people_page.cards["char-serrik"].menu_actions())
    assert "Draw them as combat armour · grinning…" in actions
    seen: list[tuple[str, str]] = []

    def rejected(dialog) -> int:
        seen.append((dialog.look.currentText(), dialog.expression.currentText()))
        return QDialog.Rejected

    monkeypatch.setattr(PictureVariantDialog, "exec", rejected)
    monkeypatch.setattr(window, "_card_pictures_refused", lambda session: False)
    # No listing fetched: tests never reach the network.
    monkeypatch.setattr(window.image_catalog, "ensure", lambda *args, **kwargs: None)
    actions["Draw them as combat armour · grinning…"]()
    assert seen == [("combat armour", "grinning")]


def test_the_stage_and_the_passages_show_who_is_there_when_asked(app, window):
    from PySide6.QtGui import QImage

    from sealedlore.gui.image_codec import encoded

    image = QImage(30, 40, QImage.Format_RGB32)
    image.fill(0x335577)
    for name in ("p", "a"):
        window.session.pictures.write(f"images/refs/{name}.png", encoded(image, "PNG"))
    window.session.story.scene.present_character_ids = ["char-serrik"]
    window.reload_transcript()
    assert not window.stage.isVisibleTo(window)
    assert all(w.faces_row is None for w in _passages(window))
    window.stage_action.setChecked(True)
    window.passage_faces_action.setChecked(True)
    assert window.config.looks_stage and window.config.looks_passages
    assert window.stage.isVisibleTo(window)
    assert [face.name for face in window.stage.faces] == ["Serrik Vaun"]
    rows = [w.faces_row for w in _passages(window)]
    assert rows and all(row is not None for row in rows)
    window.stage_action.setChecked(False)
    assert not window.stage.isVisibleTo(window)


def _passages(window):
    from sealedlore.gui.transcript import MessageWidget

    return [
        w
        for w in window.transcript.findChildren(MessageWidget)
        if window.transcript._layout.indexOf(w) >= 0 and w.kind == "assistant"
    ]
