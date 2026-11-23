"""The review of two features before 3.5.0b1 (Nov 23 2026), in the window:
one character's answer asked for again (gui/window_answers.py), and the
vectors lore from the full entry is ranked by (gui/window_always_on.py). Each
test here failed on the code as it was. Offscreen, the mock provider and mock
embeddings: no network."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from sealedlore.engine.prompt import TurnRequest  # noqa: E402
from sealedlore.gui.note_dialog import NoteDialog  # noqa: E402
from sealedlore.gui.window_answers import (  # noqa: E402
    ANSWERED_AGAIN_PRIVATE,
    EDIT_HINT_PRIVATE,
    EDIT_KEEP,
    written_from,
)
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from tests.test_answer_regenerate_gui import together  # noqa: E402
from tests.test_background_queue import _send, wait_for  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


# --- an answer whose next passage is a private scene's ----------------------------------


def after_a_private_scene(app, tmp_path: Path, story, cast):
    """Maela answered P1 at the same time as the author; the author's next
    turn opened a private scene, which has ended with its summary."""
    window = together(tmp_path, story, cast, ["P1.", "MAELA-ONE", "MAELA-AGAIN"])
    window.config.scene_reads = "manual"
    _send(window, "T1")
    wait_for(app, lambda: not window._busy)
    session = window.session
    first = session.path()[-1]
    assert [a.text for a in first.meta.reactions] == ["MAELA-ONE"]
    private = MockChatProvider(["PRIVATE-PASSAGE.", "They talked, and agreed."])
    session.enter_private(keep="disk", provider=private, model="local/model")
    list(
        session.send(
            TurnRequest(
                speaker_id="char-serrik",
                user_text="We talk.",
                controlled_character_id="char-serrik",
            )
        )
    )
    summary = session.summarise_private()
    list(session.close_private(summary))
    window._reload_unless_editing()
    return window, first


def test_the_passage_after_an_answer_is_never_a_private_scene_s(app, tmp_path, story, cast):
    window, first = after_a_private_scene(app, tmp_path, story, cast)
    path = window.session.full_path()
    index = path.index(first)
    assert path[index + 2].meta.private_span is not None  # the scene's passage, shown
    after, spent = written_from(path, index)
    assert after is None and spent
    window.close()


def test_edit_on_it_writes_nothing_again_and_says_why(app, tmp_path, story, cast, monkeypatch):
    window, first = after_a_private_scene(app, tmp_path, story, cast)
    seen: list[tuple[str, str]] = []

    def run(dialog: NoteDialog) -> int:
        seen.append((dialog.go.text(), dialog.hint.text()))
        return QDialog.Rejected

    monkeypatch.setattr(NoteDialog, "exec", run)
    window._edit_answer(first.id, "char-maela")
    assert seen == [(EDIT_KEEP, EDIT_HINT_PRIVATE)]
    window.close()


def test_regenerate_on_it_offers_no_passage_and_says_why(app, tmp_path, story, cast):
    """Nothing is offered (an unanswered question box fails the test), and
    nothing is written: the scene's passage isn't the story model's to write."""
    window, first = after_a_private_scene(app, tmp_path, story, cast)
    before = len(window._provider.requests)
    leaf = window.session.full_path()[-1]
    window._regenerate_answer(first.id, "char-maela")
    wait_for(app, lambda: not window._busy)
    app.processEvents()
    assert [a.text for a in first.meta.reactions] == ["MAELA-AGAIN"]
    assert len(window._provider.requests) == before + 1  # hers alone
    assert window.session.full_path()[-1] is leaf
    assert window.statusBar().currentMessage() == ANSWERED_AGAIN_PRIVATE.format(name="Maela Orr")
    window.close()


# --- the vectors lore from the full entry is ranked by, after a Stop --------------------


def stopped_turn(app, window) -> None:
    """A turn sent and stopped while its passage is written: the job ends
    without reaching the end of send(), where the vectors are set going."""
    provider = MockChatProvider(["A passage written slowly."])
    provider.delay = 5.0
    window._provider = provider
    window.composer.select_speaker("char-serrik")
    window.composer.input.setPlainText("I go to the guild.")
    window.send_turn()
    wait_for(app, lambda: bool(provider.requests))
    window.stop_generation()
    wait_for(app, lambda: not window._busy)
    provider.delay = 0.0
    app.processEvents()


def test_after_a_stop_the_window_sets_the_vectors_going(app, tmp_path, story, cast) -> None:
    from tests.test_lore_vectors_ahead import gated, lore, window_on

    window = window_on(tmp_path, story, cast, lore())
    session = window.session
    backend = gated(session)
    window._lore_vectors_soon.stop()
    stopped_turn(app, window)
    assert not session.lore_vectors_out()
    # A moment after, as after a story opens: never "being made" for good.
    assert window._lore_vectors_soon.isActive()
    window._lore_vectors_soon.stop()
    window._lore_vectors_if_needed()
    assert session.lore_vectors_out()
    backend.gate.set()
    window.close()


def test_a_stop_leaves_a_job_out_making_them(app, tmp_path, story, cast) -> None:
    """Stop is the turn's: the vectors made in the background go on, and only
    closing the story stops them."""
    from tests.test_lore_vectors_ahead import finished, gated, lore, window_on

    window = window_on(tmp_path, story, cast, lore())
    session = window.session
    backend = gated(session)
    window._lore_vectors_soon.stop()
    window._lore_vectors_if_needed()
    job = session._vectors_job
    assert job is not None
    stopped_turn(app, window)
    assert not job.stop.is_set()
    backend.gate.set()
    finished(session)
    window._poll_lore_vectors()
    assert session.lore_without_vectors() == []
    window.close()


def test_edit_on_the_answer_of_the_character_played_now_says_why_and_opens_nothing(
    app, tmp_path, story, cast, monkeypatch
) -> None:
    """Their answers are never sent with their turns, so Edit's hint ("given
    with your next turn") was untrue for them; Regenerate refuses in words."""
    from sealedlore.gui import window_answers
    from sealedlore.gui.window_answers import HELD_EDIT

    window = together(tmp_path, story, cast, ["P1.", "MAELA-ONE"])
    window.config.scene_reads = "manual"
    _send(window, "T1")
    wait_for(app, lambda: not window._busy)
    session = window.session
    passage = session.path()[-1]
    session.hold("char-maela")  # the author takes Maela up
    opened: list = []
    monkeypatch.setattr(window_answers, "NoteDialog", lambda *a, **k: opened.append(a))
    said: list[str] = []
    window.statusBar().messageChanged.connect(said.append)
    maela = next(r for r in passage.meta.reactions)
    window._edit_answer(passage.id, maela.character_id)
    assert not opened
    assert HELD_EDIT.format(name=maela.name) in said
    window.close()
