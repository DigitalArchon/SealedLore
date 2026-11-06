"""How much a premise draft sets up, in New story from a premise
(gui/generate_dialog.py), and how many newcomers a plot brings and how fully
(gui/plot_wishes.py): the choices reach the draft and the plot's wishes, are
looked at and not changed while the draft is written, and name the numbers
their words ask for. The model is the scripted mock."""

from __future__ import annotations

import json
import os
import re
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QComboBox, QDialog  # noqa: E402

from sealedlore.engine.authoring import build_generation_messages  # noqa: E402
from sealedlore.engine.draft_detail import STANDARD, STANDARD_DETAIL, DraftDetail  # noqa: E402
from sealedlore.engine.plot_draft import PlotWishes  # noqa: E402
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, TEXTS  # noqa: E402
from sealedlore.gui import generate_dialog  # noqa: E402
from sealedlore.gui.generate_dialog import GenerateDialog  # noqa: E402
from sealedlore.gui.plot_wishes import DESCRIBED, MADE_UP, NEWCOMER_CHOICES, NONE  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from tests.test_authoring import DRAFT  # noqa: E402

PREMISE = "A frontier mining town, and a seam that shouldn't be there."
NUMBER = re.compile(r"\d+(?:,\d{3})*")


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def wait_until(app: QApplication, condition, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        app.processEvents()
        time.sleep(0.005)


def choose(combo: QComboBox, value: str) -> None:
    index = combo.findData(value)
    assert index >= 0, value
    combo.setCurrentIndex(index)


def draft(app: QApplication, dialog: GenerateDialog) -> None:
    dialog.premise.setPlainText(PREMISE)
    dialog._generate()
    wait_until(app, lambda: dialog._thread is None)


def test_the_choices_start_at_standard_and_send_today_s_request(app):
    provider = MockChatProvider([json.dumps(DRAFT)])
    dialog = GenerateDialog(provider, model="", default_model="m")
    assert dialog.detail() == STANDARD_DETAIL
    draft(app, dialog)
    assert dialog.result() == QDialog.Accepted
    assert dialog.draft.detail == STANDARD_DETAIL
    sent = [message.text for message in provider.last_request.messages]
    assert sent == [message.text for message in build_generation_messages(PREMISE)]


def test_the_choices_reach_the_draft_and_its_request(app):
    provider = MockChatProvider([json.dumps(DRAFT)])
    dialog = GenerateDialog(provider, model="", default_model="m")
    choose(dialog.world, "detailed")
    choose(dialog.people, "handful")
    choose(dialog.depth, "deep")
    draft(app, dialog)
    assert dialog.draft.detail == DraftDetail(world="detailed", people="handful", depth="deep")
    user = provider.last_request.messages[-1].text
    for key in (
        "authoring.draft.world.detailed",
        "authoring.draft.people.handful",
        "authoring.draft.depth.deep",
    ):
        assert DEFAULT_TEXTS[key] in user, key


def test_the_choices_are_looked_at_not_changed_while_drafting(app):
    provider = MockChatProvider([json.dumps(DRAFT)], chunk_size=100_000)
    provider.delay = 0.3
    dialog = GenerateDialog(provider, model="", default_model="m")
    dialog.premise.setPlainText(PREMISE)
    dialog._generate()
    combos = (dialog.world, dialog.people, dialog.depth)
    assert not any(combo.isEnabled() for combo in combos)
    wait_until(app, lambda: dialog._thread is None)
    assert all(combo.isEnabled() for combo in combos)


def test_the_progress_line_counts_the_model_s_thinking_too(app):
    """A model that reasons first can think for minutes before the draft's
    first word: the line said nothing meanwhile."""
    provider = MockChatProvider(
        [json.dumps(DRAFT)], reasoning="Weighing the town. " * 30, chunk_size=100_000
    )
    provider.delay = 0.3
    dialog = GenerateDialog(provider, model="", default_model="m")
    dialog.premise.setPlainText(PREMISE)
    dialog._generate()
    wait_until(app, lambda: dialog.progress.text().startswith("Thinking the story through"))
    # 570 characters, at about six to a word.
    assert dialog.progress.text() == "Thinking the story through… about 95 words so far."
    wait_until(app, lambda: dialog._thread is None)
    assert dialog.result() == QDialog.Accepted
    # Once the draft is being written, its words are what is counted.
    dialog._received = 0
    dialog._on_delta("x" * 60)
    dialog._on_reasoning("y" * 600)
    assert dialog.progress.text() == "Drafting… about 10 words so far."


def test_a_plot_for_a_story_drafted_in_depth_writes_its_newcomers_in_depth(app):
    dialog = GenerateDialog(MockChatProvider(["{}"]), model="", default_model="m")
    assert dialog.plot_wishes() is None, "no plot unless asked"
    dialog.plot.set_choice(MADE_UP)
    assert dialog.plot_wishes() == PlotWishes()
    choose(dialog.depth, "deep")
    assert dialog.plot_wishes() == PlotWishes(depth="deep")
    choose(dialog.depth, "sketch")
    assert dialog.plot_wishes() == PlotWishes(depth="sketch")
    dialog.plot.set_choice(DESCRIBED)
    assert dialog.plot_wishes().depth == "sketch"
    # The plot's own choice stands.
    choose(dialog.plot.depth, "deep")
    assert dialog.plot_wishes().depth == "deep"
    choose(dialog.plot.depth, "")
    # Nobody new: nothing to write in depth.
    choose(dialog.plot.newcomers, "none")
    assert (dialog.plot_wishes().newcomers, dialog.plot_wishes().depth) == ("none", "")
    choose(dialog.plot.newcomers, "many")
    assert (dialog.plot_wishes().newcomers, dialog.plot_wishes().depth) == ("many", "sketch")
    # A story drafted as standard leaves the plot's depth to the model.
    choose(dialog.depth, STANDARD)
    assert dialog.plot_wishes().depth == ""
    dialog.plot.set_choice(NONE)
    assert dialog.plot_wishes() is None


def numbers(text: str) -> list[str]:
    return NUMBER.findall(text)


def test_each_choice_names_the_numbers_its_words_ask_for():
    """What the author reads in the list is what the model is asked for: a
    level's numbers are in its words, and the standard level's in the
    drafting prompt's own."""
    for choice, levels in (
        ("world", generate_dialog.WORLD_CHOICES),
        ("people", generate_dialog.PEOPLE_CHOICES),
        ("depth", generate_dialog.DEPTH_CHOICES),
    ):
        assert [value for value, _ in levels][1] == STANDARD
        for value, label in levels:
            words = (
                TEXTS["authoring.draft"].default
                if value == STANDARD
                else DEFAULT_TEXTS[f"authoring.draft.{choice}.{value}"]
            )
            for number in numbers(label):
                assert re.search(rf"(?<![\d,]){number}(?![\d,])", words), (label, number)
    for value, label in NEWCOMER_CHOICES:
        if value != "none":
            words = DEFAULT_TEXTS[f"authoring.plot.newcomers.{value}"]
            assert numbers(label) and all(n in numbers(words) for n in numbers(label)), label


def test_with_the_plot_closed_the_dialog_needs_no_more_than_it_opens_at(app):
    """It opens at three quarters of the screen's height, 810px at 1080p;
    the choices' room comes out of the premise's spare height, never past
    the premise's own minimum."""
    from sealedlore.gui.app import load_stylesheet
    from tests.test_ui_tidy import needs_real_fonts

    needs_real_fonts()
    before = app.styleSheet()
    app.setStyleSheet(load_stylesheet())
    try:
        dialog = GenerateDialog(MockChatProvider(["{}"]), model="", default_model="m")
        dialog.resize(864, 810)
        dialog.show()
        app.processEvents()
        assert dialog.minimumSizeHint().height() <= 810
        assert dialog.minimumSizeHint().width() <= 864
        assert dialog.premise.height() >= dialog.premise.minimumHeight()
        dialog.close()
    finally:
        app.setStyleSheet(before)
