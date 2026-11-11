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


# --- WaveSpeed's quote is for the settings it was asked about ----------------------


def test_a_quote_is_shown_only_for_the_settings_it_was_asked_for(app, window, monkeypatch):
    """WaveSpeed's quote for 5 s, answered after 10 s was chosen, was shown
    beside 10 s as "its own quote for these settings", ticked and sent as
    the price of 10 s, and 10 s was never asked: a newer ask was dropped
    while one ran. Now each answer is kept for its own settings, and the
    settings the author has are asked once the running ask is done."""
    import threading

    import sealedlore.gui.video_jobs as video_jobs
    from sealedlore.providers.videos import VideoModelInfo, VideoParam

    model = VideoModelInfo(
        id="ws/test-video",
        name="Test video",
        api="wavespeed",
        params=(
            VideoParam(
                "duration",
                "Duration",
                "select",
                options=(("5", "5 s"), ("10", "10 s")),
                default="5",
            ),
        ),
    )
    config = window.config
    config.video_endpoint.base_url = "https://api.wavespeed.ai/api/v3"
    config.video_models = [model_entry(model)]
    config.video_models_fetched_at = utc_now_iso()
    config.video_models_source = config.videos().base_url
    config.video_model = model.id
    config.video_params = {}
    gate = threading.Event()
    asked: list[str] = []

    def price(_http, _model_id, inputs):
        asked.append(str(inputs.get("duration")))
        gate.wait(5)
        return 0.30 if str(inputs.get("duration")) == "5" else 0.60

    monkeypatch.setattr(video_jobs.wavespeed, "price", price)
    catalog = window.video_catalog
    try:
        dialog = VideoDialog(window.session, catalog=catalog, parent=window)
        dialog.prompt.setPlainText("A lantern swings.")
        duration = dialog._controls["duration"]
        duration.setCurrentIndex(duration.findData("10"))  # the 5 s quote still out
        gate.set()
        wait_for(app, lambda: asked == ["5", "10"] and catalog._price_thread is None)
        settle(app, 10)
        assert "$0.60" in dialog.price.text() and "$0.30" not in dialog.price.text()
        dialog.accept_cost.setChecked(True)
        assert "$0.60" in dialog.accept_cost.text()
        dialog._approve()
        assert dialog.request.settings["duration"] == "10"
        assert dialog.request.quote == pytest.approx(0.60)
        # Back to 5 s: its own answer, given already, with no new ask; and
        # a tick for $0.60 doesn't stand for it.
        duration.setCurrentIndex(duration.findData("5"))
        settle(app, 5)
        assert "$0.30" in dialog.price.text() and asked == ["5", "10"]
        assert not dialog.accept_cost.isChecked()
    finally:
        gate.set()
        catalog.wait()


# --- a video's number setting at its lowest ----------------------------------------


def test_a_number_setting_can_be_set_to_its_lowest_and_it_is_sent(app, window):
    """The lowest value was the spin box's "—" (not set): 0 chosen for a
    setting from 0 to 1 showed "—" and was left out, so the model's default
    was sent instead of what was on screen."""
    from sealedlore.providers.videos import VideoModelInfo, VideoParam

    model = VideoModelInfo(
        id="test/video",
        name="Test video",
        api="nanogpt",
        params=(
            VideoParam("guidance", "Guidance", "number", default=0.5, minimum=0.0, maximum=1.0),
        ),
    )
    config = window.config
    config.video_models = [model_entry(model)]
    config.video_model = model.id
    config.video_params = {}
    dialog = VideoDialog(window.session, catalog=window.video_catalog, parent=window)
    guidance = dialog._controls["guidance"]
    assert guidance.value() == 0.5 and dialog.sent_settings()["guidance"] == 0.5
    guidance.setValue(0.0)
    assert guidance.text() not in ("—", ""), "the lowest is shown as itself"
    assert dialog.sent_settings()["guidance"] == 0.0, "and sent as chosen"
    guidance.stepBy(-1)
    assert guidance.text() == "—" and "guidance" not in dialog.settings(), "below it: not set"
    guidance.stepBy(1)
    assert guidance.value() == 0.0 and dialog.settings()["guidance"] == 0.0
    guidance.setValue(-0.4)  # under the lowest, above "—": the lowest
    assert guidance.value() == 0.0 and dialog.sent_settings()["guidance"] == 0.0
    dialog.done(0)


# --- Esc asks before what was typed is lost ----------------------------------------

TYPED = "A prompt written by hand, line by line."


def _esc_portraits(window):
    dialog, _button = _portraits(window)
    return dialog, lambda: dialog.rows[0].prompt.setPlainText(TYPED)


def _esc_variant(window):
    dialog, _button = _variant(window)
    dialog.change.clear()
    return dialog, lambda: dialog.change.setText("a fresh scar")


def _esc_variant_prompt(window):
    dialog, _type = _esc_variant(window)
    return dialog, lambda: dialog.prompt.setPlainText(TYPED)


def _esc_restyle(window):
    dialog, _button = _restyle(window)
    return dialog, lambda: dialog.rows[0].prompt.setPlainText(TYPED)


def _esc_expressions(window):
    dialog, _button = _expressions(window)
    dialog.others.clear()
    return dialog, lambda: next(iter(dialog._boxes.values())).setPlainText(TYPED)


def _esc_expressions_others(window):
    dialog, _type = _esc_expressions(window)
    return dialog, lambda: dialog.others.setText("laughing")


def _esc_gaps(window):
    dialog, _button = _gaps(window)
    return dialog, lambda: dialog.prompts[0].setPlainText(TYPED)


def _esc_follow_up(window):
    from sealedlore.engine.followup import STORY
    from sealedlore.gui.followup_dialog import FollowUpDialog

    dialog = FollowUpDialog(window, target=STORY, title="The Sundering", go_on="Go on")
    return dialog, lambda: dialog.request.setPlainText("More wolves, please.")


TYPING = {
    "portraits": _esc_portraits,
    "change a picture": _esc_variant,
    "change a picture, the prompt": _esc_variant_prompt,
    "restyle": _esc_restyle,
    "expression set": _esc_expressions,
    "expression set, others": _esc_expressions_others,
    "fill gaps": _esc_gaps,
    "follow-up": _esc_follow_up,
}


@pytest.mark.parametrize("which", list(TYPING))
def test_esc_asks_before_what_was_typed_is_lost(app, window, which, monkeypatch):
    """Portraits, Change this picture, Restyle, A set of expressions and Fill
    the gaps closed on Esc with prompts changed by hand (the picture dialog
    asks), and the follow-up dialog took Esc as going on, dropping a request
    typed and not sent. Untouched, Esc closes them as before."""
    titles: list[str] = []
    answers = [QMessageBox.Cancel]

    def ask(_parent, title, *_args, **_kwargs):
        titles.append(title)
        return answers[-1]

    monkeypatch.setattr(QMessageBox, "question", staticmethod(ask))
    dialog, _type = TYPING[which](window)
    dialog.show()
    settle(app)
    QTest.keyClick(dialog, Qt.Key_Escape)
    settle(app)
    assert not dialog.isVisible() and titles == [], "nothing typed: closed, nothing asked"
    dialog.deleteLater()
    dialog, type_it = TYPING[which](window)
    dialog.show()
    settle(app)
    type_it()
    QTest.keyClick(dialog, Qt.Key_Escape)
    settle(app)
    assert titles == ["Discard what you wrote?"] and dialog.isVisible(), "Cancel keeps it"
    answers.append(QMessageBox.Discard)
    QTest.keyClick(dialog, Qt.Key_Escape)
    settle(app)
    assert not dialog.isVisible(), "Discard closes it"
    dialog.deleteLater()


# --- the picture dialog with no model ----------------------------------------------


def test_generate_is_off_with_no_model_and_says_why(app, window):
    """With the Model field blank, Generate was on and did nothing."""
    dialog = ImageDialog(window.session, parent=window)
    dialog.prompt.setPlainText("A man at a farmhouse door.")
    assert dialog.generate_button.isEnabled()
    dialog.model.setText("")
    assert not dialog.generate_button.isEnabled(), "nothing to send it to"
    assert dialog.generate_button.toolTip()
    assert "No model" in dialog.warning.text() and not dialog.warning.isHidden()
    dialog.model.setText("hidream")
    assert dialog.generate_button.isEnabled() and "No model" not in dialog.warning.text()
    dialog.done(0)


# --- no word of a Cast page or tab -------------------------------------------------


def _shown_strings(path: Path) -> list[str]:
    """Every string literal in a module but its docstrings (an f-string's
    words included; words split over lines are one literal)."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
            if isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_nothing_shown_names_a_cast_page_or_tab():
    """The Cast page went into the Cast board (Nov 2026), but the picture
    dialog still said "(Cast and Lore tabs)", Portraits "the Cast or Lore
    page", the new characters' notice "in the Cast tab" and Settings "the
    Cast page says which"."""
    import re

    stale = re.compile(r"\bCast (page|tab)s?\b|\bCast and Lore (tabs|pages)\b|\bCast or Lore\b")
    root = Path(__file__).resolve().parents[1]
    found = [
        f"{path.relative_to(root)}: {text!r}"
        for path in sorted((root / "src" / "sealedlore").rglob("*.py"))
        for text in _shown_strings(path)
        if stale.search(text)
    ]
    assert found == []
    for doc in ("README.md", "docs/manual.md", "SampleStories/PLOT_FORMAT.md"):
        lines = (root / doc).read_text(encoding="utf-8").splitlines()
        assert not [
            line for line in lines if stale.search(line) and "has gone into it" not in line
        ], doc
