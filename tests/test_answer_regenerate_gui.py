"""An answer's Regenerate in the window: beside its Edit, it asks that
character's model again as a job, and, when a passage was written from the
old answer, offers that passage again from the new one, where Edit would."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from sealedlore.engine.session_reactions import ANSWER_KEPT  # noqa: E402
from sealedlore.gui.transcript import ANSWER_REGENERATE  # noqa: E402
from sealedlore.gui.window_answers import (  # noqa: E402
    ANSWERED_AGAIN_KEPT,
    ANSWERED_AGAIN_OPEN,
    ANSWERED_AGAIN_SPENT,
    REDO_ASKS,
    REDO_KEEP,
    REDO_TITLE,
    REDO_WRITE,
    STOPPED_KEPT,
)
from sealedlore.providers.base import ProviderError  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from tests.test_background_queue import _send, wait_for  # noqa: E402
from tests.test_reactions_gui import MODEL, answer_texts, own_window  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def regenerate_button(window, node_id: str):
    return window.transcript.message_widget(node_id).answer_regenerate_buttons[0]


def asked_again(window, before: int) -> list[str]:
    return [request.model for request in window._provider.requests[before:]]


def question(monkeypatch, label: str) -> list[tuple[str, str, str, str]]:
    """Every question box shown, its words kept, answered with `label`."""
    seen: list[tuple[str, str, str, str]] = []

    def exec_(box: QMessageBox) -> int:
        seen.append(
            (box.windowTitle(), box.text(), box.informativeText(), box.defaultButton().text())
        )
        next(button for button in box.buttons() if button.text() == label).click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", exec_)
    return seen


def together(tmp_path: Path, story, cast, responses: list[str]):
    window = own_window(tmp_path, story, cast, responses)
    window.answers_action.setChecked(True)  # the open round's answers shown
    return window


# --- the button ------------------------------------------------------------------


def test_each_shown_answer_has_a_regenerate_beside_its_edit(app, tmp_path: Path, story, cast):
    window = own_window(tmp_path, story, cast, ["The door gives.", "{}", "MAELA-ONE"])
    _send(window, "I put my shoulder to the door.")
    wait_for(app, lambda: not window._busy)
    passage = window.session.path()[-1]
    # Hidden until the author asks: nothing to ask for again yet.
    assert window.transcript.message_widget(passage.id).answer_regenerate_buttons == []
    window.answers_action.setChecked(True)
    widget = window.transcript.message_widget(passage.id)
    button = regenerate_button(window, passage.id)
    assert button.text() == ANSWER_REGENERATE and button.isEnabled()
    assert "Maela Orr's model" in button.toolTip()
    # Off while the story is being written, as Edit is.
    widget.set_busy(True)
    assert not button.isEnabled() and not widget.answer_buttons[0].isEnabled()
    widget.set_busy(False)
    assert button.isEnabled()
    window.close()


# --- the open round: nothing to write again --------------------------------------


def test_regenerating_the_open_round_s_answer_asks_her_alone(app, tmp_path: Path, story, cast):
    window = together(tmp_path, story, cast, ["The door gives.", "{}", "MAELA-ONE", "MAELA-TWO"])
    _send(window, "I put my shoulder to the door.")
    wait_for(app, lambda: not window._busy)
    passage = window.session.path()[-1]
    before = len(window._provider.requests)

    regenerate_button(window, passage.id).click()
    wait_for(app, lambda: not window._busy)
    app.processEvents()

    assert asked_again(window, before) == [MODEL]  # hers alone, no passage
    assert [a.text for a in passage.meta.reactions] == ["MAELA-TWO"]
    assert "MAELA-TWO" in " ".join(answer_texts(window, passage.id))
    assert window.session.path()[-1] is passage
    assert window.statusBar().currentMessage() == ANSWERED_AGAIN_OPEN.format(name="Maela Orr")
    window.close()


# --- an answer a passage was written from -------------------------------------------


def older_answer(app, tmp_path: Path, story, cast, later: list[str]):
    """Maela answered P1, the author sent T2 with it, and P2 was written."""
    window = together(
        tmp_path,
        story,
        cast,
        ["P1.", "{}", "MAELA-ONE", "P2.", "{}", "MAELA-P2", "MAELA-ONE-AGAIN", *later],
    )
    _send(window, "T1")
    wait_for(app, lambda: not window._busy)
    first = window.session.path()[-1]
    _send(window, "T2")
    wait_for(app, lambda: not window._busy)
    return window, first, window.session.path()[-1]


def test_an_older_answer_asked_again_offers_the_passage_after_it(
    app, tmp_path: Path, story, cast, monkeypatch
):
    window, first, second = older_answer(
        app, tmp_path, story, cast, ["P2-AGAIN.", "{}", "MAELA-P2-AGAIN"]
    )
    seen = question(monkeypatch, REDO_WRITE)

    regenerate_button(window, first.id).click()
    wait_for(app, lambda: bool(seen) and not window._busy)

    [(title, text, informative, default)] = seen
    assert title == REDO_TITLE and text == REDO_ASKS.format(name="Maela Orr")
    assert "MAELA-ONE-AGAIN" in informative  # the new answer, to judge by
    assert default == REDO_KEEP  # Enter never pays for a passage
    take = window.session.path()[-1]
    assert take.id != second.id and take.content == "P2-AGAIN."
    # The take is written from her new answer, as Edit's would be.
    assert "MAELA-ONE-AGAIN" in window.session.last_prompt.section("tail.round").text
    assert [a.text for a in first.meta.reactions] == ["MAELA-ONE-AGAIN"]
    window.close()


def test_the_passage_can_be_kept_as_it_is(app, tmp_path: Path, story, cast, monkeypatch):
    window, first, second = older_answer(app, tmp_path, story, cast, [])
    seen = question(monkeypatch, REDO_KEEP)
    before = len(window._provider.requests)

    regenerate_button(window, first.id).click()
    wait_for(app, lambda: bool(seen) and not window._busy)
    app.processEvents()

    assert asked_again(window, before) == [MODEL]
    assert window.session.path()[-1] is second
    assert [a.text for a in first.meta.reactions] == ["MAELA-ONE-AGAIN"]
    assert window.statusBar().currentMessage() == ANSWERED_AGAIN_KEPT.format(name="Maela Orr")
    window.close()


def test_after_the_turn_the_passage_after_it_is_offered_again(
    app, tmp_path: Path, story, cast, monkeypatch
):
    story.defaults.round_order = "after"
    window = own_window(
        tmp_path,
        story,
        cast,
        ["MAELA-LAMP", "The door gives.", "{}", "MAELA-LAMP-AGAIN", "The door gives again.", "{}"],
    )
    _send(window, "I put my shoulder to the door.")
    wait_for(app, lambda: not window._busy)
    turn_node, passage = window.session.path()[-2:]
    seen = question(monkeypatch, REDO_WRITE)

    regenerate_button(window, turn_node.id).click()
    wait_for(app, lambda: bool(seen) and not window._busy)

    assert [a.text for a in turn_node.meta.reactions] == ["MAELA-LAMP-AGAIN"]
    take = window.session.path()[-1]
    assert take.id != passage.id and take.content == "The door gives again."
    assert "MAELA-LAMP-AGAIN" in window.session.last_prompt.section("tail.round").text
    # Not asked a third time for that take: the turn's round had run to its end.
    assert [r.model for r in window._provider.requests].count(MODEL) == 2
    window.close()


def test_an_answer_no_take_would_read_is_just_replaced(app, tmp_path: Path, story, cast):
    """Edit's spent case: changed to "after your turn", the next turn was
    answered after it was sent, so the passage after it never reads the
    older blind answer. No passage is offered (an unanswered box fails the
    test) and nothing is written."""
    window = together(tmp_path, story, cast, ["P1.", "BLIND", "AFTER", "P2.", "BLIND-AGAIN"])
    window.config.scene_reads = "manual"
    _send(window, "T1")
    wait_for(app, lambda: not window._busy)
    first = window.session.full_path()[-1]
    window.session.story.defaults.round_order = "after"
    _send(window, "T2")
    wait_for(app, lambda: not window._busy)
    second = window.session.full_path()[-1]
    before = len(window._provider.requests)

    regenerate_button(window, first.id).click()
    wait_for(app, lambda: not window._busy)
    app.processEvents()

    assert asked_again(window, before) == [MODEL]
    assert window.session.full_path()[-1] is second
    assert [a.text for a in first.meta.reactions] == ["BLIND-AGAIN"]
    assert window.statusBar().currentMessage() == ANSWERED_AGAIN_SPENT.format(name="Maela Orr")
    window.close()


# --- what stops it ------------------------------------------------------------------


def test_a_failure_keeps_the_answer_and_says_why(app, tmp_path: Path, story, cast, monkeypatch):
    window = together(tmp_path, story, cast, ["The door gives.", "{}", "MAELA-ONE"])
    _send(window, "I put my shoulder to the door.")
    wait_for(app, lambda: not window._busy)
    passage = window.session.path()[-1]
    old = passage.meta.reactions[0]
    warned: list[tuple[str, str]] = []
    monkeypatch.setattr(window, "_warn_plain", lambda *words: warned.append(tuple(words)))
    window._provider.error = ProviderError("the endpoint is down")

    regenerate_button(window, passage.id).click()
    wait_for(app, lambda: not window._busy)
    window._provider.error = None

    [(title, text)] = warned
    assert title == "Asking Maela Orr's model again failed"
    assert "Maela Orr's model couldn't answer: the endpoint is down" in text
    assert ANSWER_KEPT in text
    assert passage.meta.reactions == [old]
    assert "MAELA-ONE" in " ".join(answer_texts(window, passage.id))
    window.close()


def test_stop_keeps_the_answer(app, tmp_path: Path, story, cast):
    window = together(tmp_path, story, cast, ["The door gives.", "{}", "MAELA-ONE", "LATE"])
    _send(window, "I put my shoulder to the door.")
    wait_for(app, lambda: not window._busy)
    passage = window.session.path()[-1]
    old = passage.meta.reactions[0]
    provider = window._provider
    provider.delay = 5.0  # the answer waits, and the Stop lands in the wait
    before = len(provider.requests)

    regenerate_button(window, passage.id).click()
    wait_for(app, lambda: len(provider.requests) > before)
    window.stop_generation()
    wait_for(app, lambda: not window._busy)
    provider.delay = 0.0
    app.processEvents()

    assert passage.meta.reactions == [old]
    assert window.statusBar().currentMessage() == STOPPED_KEPT.format(name="Maela Orr")
    assert "MAELA-ONE" in " ".join(answer_texts(window, passage.id))
    window.close()


# --- where it is refused ----------------------------------------------------------


def test_refused_in_a_private_scene_and_while_busy(app, tmp_path: Path, story, cast):
    window = together(tmp_path, story, cast, ["The door gives.", "{}", "MAELA-ONE"])
    _send(window, "I put my shoulder to the door.")
    wait_for(app, lambda: not window._busy)
    passage = window.session.path()[-1]
    before = len(window._provider.requests)
    # While a job runs: nothing.
    window._busy = True
    window._regenerate_answer(passage.id, "char-maela")
    window._busy = False
    assert not window._busy and len(window._provider.requests) == before
    # In a private scene: said, and nothing sent to any model.
    private = MockChatProvider(["Hush."])
    window.session.enter_private(keep="disk", provider=private, model="local/model")
    regenerate_button(window, passage.id).click()
    app.processEvents()
    assert not window._busy
    assert "private scene" in window.statusBar().currentMessage()
    assert len(window._provider.requests) == before and private.requests == []
    assert passage.meta.reactions[0].text == "MAELA-ONE"
    window.session.discard_private()
    window.close()
