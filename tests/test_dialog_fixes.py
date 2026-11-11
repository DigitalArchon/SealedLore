"""The picture, video and other dialogs as the Nov 2026 review found them:
Enter in a field pressing a paid button, Esc losing what was typed, a quote
shown for other settings, and the like. Offscreen, never the network: the
prompt writer is the scripted mock, and nothing is sent anywhere."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton,
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QLineEdit,
    QMessageBox,
    QRadioButton,
)

from sealedlore.engine.look_sheet import sheet_for  # noqa: E402
from sealedlore.gui.expression_set import ExpressionSetDialog  # noqa: E402
from sealedlore.gui.fill_gaps import FillGapsDialog  # noqa: E402
from sealedlore.gui.generate_dialog import GenerateDialog  # noqa: E402
from sealedlore.gui.image_dialog import ImageDialog  # noqa: E402
from sealedlore.gui.lore_cast_dialog import LoreCastDialog  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.picture_use import CHARACTER, UsePictureDialog  # noqa: E402
from sealedlore.gui.picture_variant import PictureVariantDialog  # noqa: E402
from sealedlore.gui.plot_dialog import PlotDialog  # noqa: E402
from sealedlore.gui.portraits_dialog import PortraitsDialog  # noqa: E402
from sealedlore.gui.restyle_dialog import RestyleDialog  # noqa: E402
from sealedlore.gui.review_dialog import ReviewRequestDialog  # noqa: E402
from sealedlore.gui.video_dialog import VideoDialog  # noqa: E402
from sealedlore.gui.video_jobs import model_entry  # noqa: E402
from sealedlore.ids import utc_now_iso  # noqa: E402
from sealedlore.models.image import ImageRef  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.images import write_story_file  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402
from tests.test_images import LISTING, PNG  # noqa: E402
from tests.test_videos import nanogpt_models  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def settle(app: QApplication, rounds: int = 5) -> None:
    for _ in range(rounds):
        app.processEvents()


def wait_for(app: QApplication, condition, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        app.processEvents()
        time.sleep(0.005)


PICTURES = (
    ("ref-coat", "winter coat", "neutral"),
    ("ref-happy", "winter coat", "happy"),
    ("ref-armour", "combat armour", "neutral"),
)


@pytest.fixture
def window(app, tmp_path: Path, story, cast) -> MainWindow:
    """The small test story, Serrik with three labelled pictures, an image
    listing and a video one, so every picture and video dialog can open."""
    serrik = cast[0]
    for ref_id, look, expression in PICTURES:
        serrik.reference_images.append(
            ImageRef(id=ref_id, file=f"images/refs/{ref_id}.png", look=look, expression=expression)
        )
    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    bundle.story.held_character_id = "char-serrik"
    save_story_bundle(bundle, root=tmp_path)
    for ref_id, _look, _expression in PICTURES:
        write_story_file(story.id, f"images/refs/{ref_id}.png", PNG, tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.image_models = LISTING["data"]
    window.config.image_models_fetched_at = utc_now_iso()
    window.config.video_models = [model_entry(m) for m in nanogpt_models().values()]
    window.config.video_models_fetched_at = utc_now_iso()
    window.config.video_endpoint.api_key = "video-key"
    window.open_story(story.id)
    yield window
    window.image_jobs.stop_all()
    window.image_jobs.wait_all()
    window.video_jobs.stop_all()
    window.video_jobs.wait_all()
    window.close()


def _serrik(window: MainWindow):
    return window.session.cast[0]


# --- Enter in a field never spends ------------------------------------------------


def _image(window):
    dialog = ImageDialog(window.session, parent=window)
    dialog.prompt.setPlainText("A man at a farmhouse door.")
    return dialog, dialog.generate_button


def _video(window):
    dialog = VideoDialog(window.session, catalog=window.video_catalog, parent=window)
    dialog.prompt.setPlainText("A lantern swings in the rain.")
    dialog.accept_cost.setChecked(True)
    return dialog, dialog.generate_button


def _portraits(window):
    dialog = PortraitsDialog(
        window.session, list(window.session.cast)[1:2], catalog=None, browse=None, parent=window
    )
    wait_for(QApplication.instance(), lambda: not dialog.writing())
    return dialog, dialog.draw_button


def _variant(window):
    serrik = _serrik(window)
    dialog = PictureVariantDialog(
        window.session,
        serrik,
        serrik.reference_images[0],
        edit=True,
        catalog=None,
        browse=None,
        parent=window,
    )
    dialog.change.setText("a fresh scar")
    return dialog, dialog.draw_button


def _restyle(window):
    dialog = RestyleDialog(
        window.session, [_serrik(window)], catalog=None, browse=None, parent=window
    )
    return dialog, dialog.draw_button


def _expressions(window):
    dialog = ExpressionSetDialog(
        window.session, _serrik(window), source_id=None, catalog=None, browse=None, parent=window
    )
    dialog.others.setText("laughing")
    return dialog, dialog.draw_button


def _gaps(window):
    serrik = _serrik(window)
    gaps = [gap for gap in sheet_for(serrik).drawable()][:2]
    dialog = FillGapsDialog(window.session, serrik, gaps, catalog=None, browse=None, parent=window)
    return dialog, dialog.draw_button


def _use(window):
    data = window.session.pictures.read(_serrik(window).reference_images[0].file)
    dialog = UsePictureDialog(window.session, data, CHARACTER, window)
    dialog.caption.setText("at the door")
    return dialog, dialog.keep_button


def _review(window):
    dialog = ReviewRequestDialog(
        sizes=window.session.review_sizes(), model="m", default_model="m", parent=window
    )
    dialog.complaint.setPlainText("Too long.")
    ok = next(b for b in dialog.findChildren(QAbstractButton) if b.text() == "Review")
    return dialog, ok


def _draft(window):
    dialog = GenerateDialog(MockChatProvider([]), model="m", default_model="m", parent=window)
    dialog.premise.setPlainText("A lighthouse keeper finds a letter.")
    return dialog, dialog.generate_button


def _lore_cast(window):
    dialog = LoreCastDialog(window, model="m", lore="Hamlet")
    return dialog, dialog.ok


def _plot(window):
    dialog = PlotDialog(window, model="m", replacing=False, begun=False)
    return dialog, dialog.write_button


PAID = {
    "picture": _image,
    "video": _video,
    "portraits": _portraits,
    "change a picture": _variant,
    "restyle": _restyle,
    "expression set": _expressions,
    "fill gaps": _gaps,
    "use a picture": _use,
    "review": _review,
    "draft": _draft,
    "lore characters": _lore_cast,
    "plot": _plot,
}


def _fields(dialog) -> list:
    """Every field Enter might be pressed in: line edits (a spin box's and an
    editable list's included), lists, spin boxes and ticks, shown and usable."""
    found = []
    for kind in (QLineEdit, QComboBox, QAbstractSpinBox, QCheckBox, QRadioButton):
        for field in dialog.findChildren(kind):
            if not (field.isVisible() and field.isEnabled()):
                continue
            if isinstance(field, QLineEdit) and (
                field.isReadOnly() or isinstance(field.parent(), (QComboBox, QAbstractSpinBox))
            ):
                continue  # a spin box's or a list's own line: the box is tried
            found.append(field)
    return found


@pytest.mark.parametrize("which", list(PAID))
def test_enter_in_a_field_never_presses_the_button_that_spends(app, window, which, monkeypatch):
    """The button box makes its accept button the default as the dialog
    shows, whatever was set before: Enter in Model, Size, Pictures or Prompt
    written by drew a picture, Enter in a video's Model or writer sent it
    with the price ticked, Enter in Drawn by drew every card, and Enter in a
    review's Model started the review. Enter on the focused button still
    presses it, as Space does."""
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *_a, **_k: QMessageBox.Discard)
    )
    make = PAID[which]
    dialog, button = make(window)
    dialog.show()
    settle(app)
    fields = _fields(dialog)
    assert fields, "there is a field to press Enter in"
    pressed: list[str] = []

    def note(button):
        button.clicked.connect(lambda _c=False: pressed.append(button.text()))

    for index in range(len(fields)):
        dialog.hide()
        dialog.deleteLater()
        dialog, button = make(window)
        dialog.show()
        settle(app)
        assert button.isEnabled(), "the button would spend now"
        pressed.clear()
        for other in dialog.findChildren(QAbstractButton):
            note(other)
        field = _fields(dialog)[index]
        field.setFocus()
        settle(app, 2)
        QTest.keyClick(field, Qt.Key_Return)
        settle(app, 3)
        assert pressed == [] and dialog.isVisible() and dialog.result() == 0, (
            f"Enter in {type(field).__name__} #{index} pressed {pressed}"
        )
    # On the button itself, Enter presses it (nothing sent: only noted).
    button.clicked.disconnect()
    pressed.clear()
    note(button)
    button.setFocus()
    QTest.keyClick(button, Qt.Key_Return)
    settle(app, 3)
    assert pressed == [button.text()], "Enter on the button presses it"
    dialog.hide()
    dialog.deleteLater()
