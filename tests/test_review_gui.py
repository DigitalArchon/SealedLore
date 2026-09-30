"""The review dialogs and the window's review flow, offscreen.

Offscreen only, and serial: the author's machine has a tiny virtual GPU.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from sealedlore.engine.session import ReviewSizes, StorySession  # noqa: E402
from sealedlore.gui.composer import Composer  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.review_dialog import ReviewRequestDialog, ReviewResultDialog  # noqa: E402
from sealedlore.gui.transcript import TranscriptView  # noqa: E402
from sealedlore.models.authoring import ReviewRecord, SettingsChange, SettingsReview  # noqa: E402
from sealedlore.models.config import ModelPrice  # noqa: E402
from sealedlore.models.node import DIRECTOR_SPEAKER_ID  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def bundle(story, cast) -> StoryBundle:
    return StoryBundle(story=story, cast=cast)


def review_with(*changes: SettingsChange, **fields) -> SettingsReview:
    return SettingsReview(summary="Because.", changes=list(changes), **fields)


def test_a_warned_change_starts_unticked_and_the_director_turn_counts(app, bundle):
    world = SettingsChange(kind="world", value="Short.", warning="drops 7 of 8 lines")
    notes = SettingsChange(kind="style", field="notes", value="Terse.", reason="Less.")
    review = review_with(world, notes, director_turn="From here, Maela stands her ground.")
    dialog = ReviewResultDialog(review, bundle, complaint="Too long.", model="m")

    assert [c.id for c in dialog.selected_changes()] == [notes.id]
    assert dialog.send_director_turn()
    dialog._tick(False)
    assert dialog.selected_changes() == [] and dialog.apply_button.isEnabled()
    dialog.director_box.setChecked(False)
    assert not dialog.apply_button.isEnabled()
    dialog._tick(True)
    assert len(dialog.selected_changes()) == 2 and dialog.apply_button.isEnabled()


def test_a_generation_change_is_a_suggestion_with_no_box_to_tick(app, bundle):
    """Generation settings are the author's for every story: the review only
    suggests them (the author, Sept 30 2026)."""
    from PySide6.QtWidgets import QLabel

    suggestion = SettingsChange(
        kind="generation", field="temperature", value=0.7, before=1.1, reason="Calmer."
    )
    notes = SettingsChange(kind="style", field="notes", value="Terse.")
    dialog = ReviewResultDialog(review_with(suggestion, notes), bundle, model="m")
    assert [c.id for c in dialog.selected_changes()] == [notes.id]
    dialog._tick(True)
    assert [c.id for c in dialog.selected_changes()] == [notes.id]
    texts = [label.text() for label in dialog.findChildren(QLabel)]
    assert any(text.startswith("Suggestion: Generation") for text in texts)
    assert any("Settings → Generation" in text for text in texts)


def test_a_past_review_is_shown_read_only_with_what_was_applied(app, bundle):
    a = SettingsChange(kind="style", field="pacing", value="slow")
    b = SettingsChange(kind="style", field="notes", value="x")
    review = review_with(a, b, director_turn="Go.")
    dialog = ReviewResultDialog(review, bundle, applied_ids=[a.id, "director"], read_only=True)

    assert not dialog.apply_button.isVisible()
    boxes = [box for box, _ in dialog._boxes]
    assert boxes[0].isChecked() and "applied" in boxes[0].text()
    assert not boxes[1].isChecked() and "not applied" in boxes[1].text()
    assert dialog.director_box.isChecked() and not dialog.director_box.isEnabled()


def test_the_request_dialog_estimates_the_cost_and_prefills_the_complaint(app):
    sizes = ReviewSizes(
        settings=1000,
        system_prompt=2000,
        tail=500,
        whole_prompt=9000,
        exchanges=(300, 300, 300),
        in_prompt=3,
        marked=(("a0", 300),),
    )
    price = ModelPrice(prompt=3.0, completion=15.0)
    cleared: list[bool] = []
    dialog = ReviewRequestDialog(
        sizes=sizes,
        model="",
        default_model="m",
        context_for=lambda model: 200_000,
        price_for=lambda model: price,
        output_limit_for=lambda model: 64_000,
        last_complaint="Too long.",
        clear_marks=lambda: cleared.append(True),
    )
    assert dialog.complaint_text() == "Too long."
    assert dialog.reply_count() == 3
    text = dialog.size_label.text()
    # settings + system + tail + three exchanges + the marked passage
    assert "About 4,700 tokens" in text
    assert "$0.014 to send" in text and "$0.060 for the answer" in text
    assert "at most $0.96" in text
    assert "1 passage you marked" in dialog.marked.text()

    dialog.clear_marks_button.click()
    assert cleared == [True]
    assert "About 4,400 tokens" in dialog.size_label.text()
    assert not dialog.clear_marks_button.isEnabled()


def test_the_composer_takes_a_director_turn(app, cast, story):
    composer = Composer()
    composer.refresh(cast, story.scene, "char-serrik")
    composer.set_direction("  From here, Maela stands her ground.  ")
    assert composer.text() == "From here, Maela stands her ground."
    assert composer.speaker_id() == DIRECTOR_SPEAKER_ID
    assert composer.ooc_text() is None


def test_a_passage_can_be_marked_from_its_menu(app, cast):
    from tests.conftest import make_exchange

    view = TranscriptView()
    marks: list[str] = []
    view.review_mark_requested.connect(marks.append)
    nodes = make_exchange(2)
    view.show_path(nodes, cast, marked_ids=frozenset({"a1"}))
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    widgets = {w.node_id: w for w in view.findChildren(type(view.add_message("x", "y", "z")))}
    assert "review_mark" not in widgets["u0"].actions_
    assert widgets["a1"].actions_["review_mark"].isChecked()
    assert not widgets["a0"].actions_["review_mark"].isChecked()
    widgets["a0"].actions_["review_mark"].trigger()
    assert marks == ["a0"]


@pytest.fixture
def window(app, tmp_path: Path, story, cast) -> MainWindow:
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(story.id)
    assert window.session is not None
    return window


def played(session: StorySession, turns: int) -> None:
    from sealedlore.engine.prompt import TurnRequest

    for number in range(turns):
        session.provider = MockChatProvider([f"Maela Orr hides, turn {number}."])
        list(
            session.send(
                TurnRequest(
                    speaker_id="char-serrik",
                    user_text=f"Turn {number}",
                    controlled_character_id="char-serrik",
                )
            )
        )


def test_the_window_applies_a_review_offers_a_branch_and_can_undo(window: MainWindow, monkeypatch):
    session = window.session
    played(session, 3)
    window.reload_transcript()
    review = SettingsReview(
        summary="Maela is written timid.",
        changes=[
            SettingsChange(kind="character", target="Maela Orr", field="voice", value="Firm."),
            SettingsChange(kind="character", target="Nobody", field="voice", value="x"),
        ],
        director_turn="Maela has had enough of hiding.",
        history_bound=True,
    )
    session.bundle.reviews.append(
        ReviewRecord(model="m", complaint="Maela is a coward.", review=review)
    )
    window._pending_review = review
    window._review_request = ("Maela is a coward.", "m")

    monkeypatch.setattr(ReviewResultDialog, "exec", lambda self: QDialog.Accepted)
    offers: list[str] = []
    warned: list[str] = []

    def fake_box_exec(self):
        offers.append(self.informativeText())
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_box_exec)
    monkeypatch.setattr(window, "_warn_plain", lambda title, message: warned.append(message))

    window._offer_pending_review()

    assert session.cast[1].voice_notes == "Firm."
    assert window.composer.text() == "Maela has had enough of hiding."
    assert window.composer.speaker_id() == DIRECTOR_SPEAKER_ID
    assert warned and "Nobody" in warned[0]
    # The offer named the first appearance: Maela is in every reply, so the
    # branch point is before the first reply (message 2).
    assert offers and "message 2" in offers[0]
    assert session.last_review.applied_ids == [review.changes[0].id, "director"]
    assert window.undo_review_action.isEnabled() and window.last_review_action.isEnabled()

    window.undo_review()
    assert session.cast[1].voice_notes is None
    assert not window.undo_review_action.isEnabled()
    assert session.last_review.undone

    shown: list[bool] = []
    monkeypatch.setattr(ReviewResultDialog, "exec", lambda self: shown.append(self._read_only))
    window.show_last_review()
    assert shown == [True]


def test_a_review_request_from_the_window_reaches_the_session(window: MainWindow, monkeypatch):
    session = window.session
    played(session, 1)
    reply = json.dumps({"summary": "", "changes": [], "cannot_fix": []})
    monkeypatch.setattr(window, "_current_provider", lambda: MockChatProvider([reply]))

    def fake_exec(self):
        self.complaint.setPlainText("Too much action.")
        return QDialog.Accepted

    monkeypatch.setattr(ReviewRequestDialog, "exec", fake_exec)
    # Run the job inline rather than on the worker thread.
    monkeypatch.setattr(window, "_start", lambda factory, **kw: list(factory()))
    window.review_settings()
    assert window._pending_review is not None
    assert session.last_review.complaint == "Too much action."
