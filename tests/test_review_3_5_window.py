"""Play and the window, looked over before 3.5.0b1 (the window's side;
tests/test_review_3_5_play.py has the engine's). Offscreen, mock clients only."""

from __future__ import annotations

import gc
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QTextCursor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QToolButton  # noqa: E402

from sealedlore.engine.session_reactions import ROUND_SKIPPED  # noqa: E402
from sealedlore.gui import lorebooks_page  # noqa: E402
from sealedlore.gui.always_on_dialog import OPTIONAL_PATTERNS, AlwaysOnDialog  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.transcript import ANSWERS_SHOW  # noqa: E402
from sealedlore.gui.window_answers import EDIT_HINT_SPENT, EDIT_KEEP, EDIT_REDO  # noqa: E402
from sealedlore.models.character import Character  # noqa: E402
from sealedlore.models.lore import LoreEntry, TavernEntry  # noqa: E402
from sealedlore.models.node import DIRECTOR_SPEAKER_ID  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.lorebooks import load_lorebooks  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.test_background_queue import _send, _window, wait_for  # noqa: E402
from tests.test_lore_condense_gui import window_with  # noqa: E402
from tests.test_reactions_gui import (  # noqa: E402
    MODEL,
    answer_dialog,
    answer_texts,
    own_window,
)


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


# --- W1: a kept lorebook is condensed on Your characters' own client ---------------

VALLEY = {
    "entries": {
        "0": {
            "uid": 0,
            "comment": "The Valley",
            "constant": True,
            "key": [],
            "content": "the valley " * 200,
        }
    }
}


def test_a_kept_lorebook_is_condensed_on_its_own_client_never_the_storys(
    app, tmp_path: Path, story, cast, monkeypatch
) -> None:
    """Lorebooks → Condense the always-on entries… took the story's client:
    its Stop cut the story's passage, the story's Stop cut it, and Your
    characters' session kept the story's client from then on."""
    path = tmp_path / "valley.json"
    path.write_text(json.dumps(VALLEY), encoding="utf-8")
    window = _window(tmp_path / "data", story, cast, ["A passage of the story's."])
    monkeypatch.setattr(
        "sealedlore.gui.file_dialogs.open_file_names", lambda *a, **k: ([str(path)], "")
    )
    monkeypatch.setattr(window, "_ask_lorebook_arrival", lambda *a, **k: "keep", raising=False)
    window.show_lorebooks(True)
    window.lorebooks_page.action_requested.emit(lorebooks_page.IMPORT, "")
    (stored,) = window._lorebooks()
    own = MockChatProvider(["A valley."])
    window._collection_session().provider = own
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)

    window.lorebooks_page.action_requested.emit(lorebooks_page.CONDENSE, stored.id)

    theirs = window._current_provider()
    assert window._collection_session().provider is own
    assert own is not theirs
    assert len(own.requests) == 1 and not theirs.requests
    (saved,) = load_lorebooks(tmp_path / "data").books
    assert list(saved.condensed.values()) == ["A valley."]
    window.close()


# --- W2: a condensed text that lands is shown on the card it is for ----------------

JANE = "Jane keeps the farmhouse stove going through the long winter. " * 90


def _card_window(tmp_path: Path, story, replies: list[str]) -> MainWindow:
    cast = [
        Character(id="char-john", name="John Jacobs", summary="A survivor who walks alone."),
        Character(
            id="char-jane", name="Jane Jacobs", summary="John's wife.", full_description=JANE
        ),
    ]
    story.held_character_id = "char-john"
    story.scene.present_character_ids = ["char-john", "char-jane"]
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.scene_reads = "manual"
    window.config.cast_limit = "tokens"
    window.config.cast_token_cap = 100_000
    window._provider = MockChatProvider(replies)
    window.open_story(story.id)
    window.show_cast_page()
    window._condense_soon.stop()
    window.cast_panel.select_character("char-jane")
    return window


def test_a_condensed_text_written_for_the_card_showing_is_shown_there(
    app, tmp_path: Path, story
) -> None:
    """The box kept what it had (nothing), and the next key typed in it was
    saved as the card's whole condensed text over the one just written."""
    window = _card_window(tmp_path, story, ["Jane: John's steady, practical wife."])
    panel = window.cast_panel
    jane = panel.current
    assert panel.condensed.toPlainText() == ""
    panel.condense_button.click()
    window._poll_condensing()
    assert jane.condensed == "Jane: John's steady, practical wife."
    assert panel.condensed.toPlainText() == "Jane: John's steady, practical wife."
    # A change to it is a change to the text shown, not a new text of one key.
    panel.condensed.moveCursor(QTextCursor.End)
    panel.condensed.insertPlainText(" Loyal.")
    assert jane.condensed == "Jane: John's steady, practical wife. Loyal."
    window.close()


def test_a_condensed_text_never_lands_under_the_authors_hands(app, tmp_path: Path, story) -> None:
    """In the box when it lands, the author keeps the box as they have it:
    what they type next is what is kept, and the written text shows once they
    leave the box without typing."""
    window = _card_window(tmp_path, story, ["The model's card.", "The model's second card."])
    # Earlier tests' windows go first: one collected while this is activated
    # took PySide down (tests/test_retake_keys.py).
    app.processEvents()
    gc.collect()
    app.processEvents()
    window.show()
    window.activateWindow()
    QTest.qWaitForWindowActive(window, 2000)
    panel = window.cast_panel
    jane = panel.current
    panel.condense_button.click()
    panel.condensed.setFocus()
    assert panel.condensed.hasFocus()
    window._poll_condensing()
    assert jane.condensed == "The model's card."
    assert panel.condensed.toPlainText() == ""  # not under their cursor
    panel.name.setFocus()
    assert panel.condensed.toPlainText() == "The model's card."  # they left: shown
    # And typing while one is written: theirs stands (the engine drops the job's).
    panel.condense_button.click()
    panel.condensed.setFocus()
    panel.condensed.setPlainText("My own words for Jane.")
    window._poll_condensing()
    panel.name.setFocus()
    assert jane.condensed == "My own words for Jane."
    assert panel.condensed.toPlainText() == "My own words for Jane."
    window.close()


# --- W3: nobody answers a Direction, and nothing shows as if they had --------------


def test_a_direction_has_no_answers_row_and_nothing_to_show(
    app, tmp_path: Path, story, cast
) -> None:
    story.defaults.round_order = "after"
    window = own_window(tmp_path, story, cast, ["Three weeks later, snow.", "MAELA-WAS-ASKED"])
    window.config.scene_reads = "manual"
    said: list[str] = []
    window.statusBar().messageChanged.connect(said.append)
    window.composer.select_speaker(DIRECTOR_SPEAKER_ID)
    window.composer.input.setPlainText("Three weeks pass.")
    window.send_turn()
    wait_for(app, lambda: not window._busy)
    turn_node, passage = window.session.full_path()[-2:]
    assert turn_node.speaker_id == DIRECTOR_SPEAKER_ID and passage.content.startswith("Three")
    assert [r.model for r in window._provider.requests] == [window.session.model]
    for node in (turn_node, passage):
        assert getattr(window.transcript.message_widget(node.id), "answers_box", None) is None
    assert not any("Waiting for the characters" in text for text in said)
    window.close()


# --- W4: Stop while their answers are waited for skips them; the turn stays ---------


def test_stop_during_their_round_skips_it_and_the_passage_is_written(
    app, tmp_path: Path, story, cast, monkeypatch
) -> None:
    """The wait's words are "Stop skips it". It ended the job instead: the
    turn went back to the composer, and sent again it waited on the same
    model again."""
    story.defaults.round_order = "after"
    window = own_window(
        tmp_path, story, cast, ["MAELA-ANSWER " * 40, "The door gives.", "MAELA-ASKED-AGAIN"]
    )
    window.config.scene_reads = "manual"
    failed: list[tuple] = []
    monkeypatch.setattr(window, "_warn_plain", lambda *words: failed.append(words))
    said: list[str] = []
    window.statusBar().messageChanged.connect(said.append)
    provider = window._provider
    provider.delay = 0.05
    _send(window, "I put my shoulder to the door.")
    wait_for(app, lambda: any("Waiting for the characters" in text for text in said))
    window.stop_generation()
    provider.delay = 0.0
    wait_for(app, lambda: not window._busy)
    assert not failed
    turn_node, passage = window.session.full_path()[-2:]
    assert turn_node.kind == "user" and passage.content == "The door gives."
    assert window.composer.input.toPlainText() == ""  # the turn stayed sent
    assert ROUND_SKIPPED in said
    assert not turn_node.meta.round_after
    assert getattr(window.transcript.message_widget(turn_node.id), "answers_box", None) is None
    # Regenerate asks them, as the notice says.
    before = len(provider.requests)
    window.regenerate()
    wait_for(app, lambda: not window._busy)
    assert [r.model for r in provider.requests[before:]] == [MODEL, window.session.model]
    assert turn_node.meta.round_after
    assert "MAELA-ASKED-AGAIN" in " ".join(answer_texts(window, turn_node.id))
    window.close()


# --- W5: Show and Hide wait for the passage being written ---------------------------


def test_show_pressed_while_a_passage_streams_leaves_the_stream_alone(
    app, tmp_path: Path, story, cast
) -> None:
    """Show (and View → Show the characters' answers) rebuilt the transcript
    mid-job: the text streamed so far vanished from the page."""
    long = "The lock turns slowly and the long corridor beyond it opens. " * 6
    window = own_window(
        tmp_path,
        story,
        cast,
        ["The door gives.", "{}", "MAELA-PICKS-THE-LOCK", long, "{}", "MAELA-GOES-FIRST"],
    )
    window.show()
    _send(window, "I put my shoulder to the door.")
    wait_for(app, lambda: not window._busy)
    first = window.session.path()[-1]
    box = window.transcript.message_widget(first.id).answers_box
    show = next(b for b in box.findChildren(QToolButton) if b.text() == ANSWERS_SHOW)
    window._provider.delay = 0.05
    _send(window, "I step through.")
    streaming = lambda: window.transcript._streaming  # noqa: E731
    wait_for(app, lambda: streaming() is not None and len(streaming().body.source) > 60)
    widget = streaming()
    show.click()
    app.processEvents()
    after_click = streaming()
    window._provider.delay = 0.0
    wait_for(app, lambda: not window._busy)  # before anything is judged: no job left running
    assert after_click is widget
    assert window.answers_action.isChecked()
    # The choice took effect with the job's own rebuild: the open round's are shown.
    latest = window.session.path()[-1]
    assert latest.content.strip() == long.strip()
    assert "MAELA-GOES-FIRST" in " ".join(answer_texts(window, latest.id))
    window.close()


# --- W6: an edit is redone only where the passage after it would read it ------------


def test_an_old_blind_answer_no_take_would_read_is_just_saved(
    app, tmp_path: Path, story, cast, monkeypatch
) -> None:
    """Changed from "at the same time" to "after your turn": the next turn
    was answered after it was sent, so the passage after it is written from
    those answers. Editing the older blind one paid for a new take of that
    passage, which never read the edit."""
    story.defaults.round_order = "together"
    window = own_window(
        tmp_path, story, cast, ["P1.", "BLIND-ANSWER-TO-P1", "AFTER-ANSWER-TO-T2", "P2.", "P3."]
    )
    window.config.scene_reads = "manual"
    _send(window, "T1")
    wait_for(app, lambda: not window._busy)
    first = window.session.full_path()[-1]
    window.session.story.defaults.round_order = "after"  # Setup → They answer
    _send(window, "T2")
    wait_for(app, lambda: not window._busy)
    turn_node, second = window.session.full_path()[-2:]
    assert turn_node.meta.round_after
    seen: list = []
    answer_dialog(monkeypatch, "EDITED-BLIND-ANSWER", seen)
    before = len(window._provider.requests)
    window._edit_answer(first.id, "char-maela")
    wait_for(app, lambda: not window._busy)
    assert seen[0].go.text() == EDIT_KEEP and seen[0].hint.text() == EDIT_HINT_SPENT
    assert len(window._provider.requests) == before  # nothing written again
    assert window.session.full_path()[-1].id == second.id
    assert first.meta.reactions[0].text == "EDITED-BLIND-ANSWER"  # kept, as Save says
    assert "EDITED-BLIND-ANSWER" in " ".join(answer_texts(window, first.id))
    # The answer the passage was written from is redone as before (a guard).
    answer_dialog(monkeypatch, "EDITED-AFTER-ANSWER", seen)
    window._edit_answer(turn_node.id, "char-maela")
    wait_for(app, lambda: not window._busy)
    assert seen[1].go.text() == EDIT_REDO
    assert window.session.full_path()[-1].content == "P3."
    assert "EDITED-AFTER-ANSWER" in window.session.last_prompt.section("tail.round").text
    window.close()


# --- a leftover: an entry keyed only by patterns is not "never sent" ----------------


def _always_on_notes(window, monkeypatch) -> list[str]:
    notes: list[str] = []

    def exec_(dialog: AlwaysOnDialog) -> int:
        for row in range(dialog.table.rowCount()):
            dialog.table.item(row, 0).setCheckState(Qt.Unchecked)
        notes.extend(dialog.table.item(row, 4).text() for row in range(dialog.table.rowCount()))
        return QDialog.Rejected

    monkeypatch.setattr(AlwaysOnDialog, "exec", exec_)
    window.open_always_on()
    return notes


def test_an_entry_keyed_only_by_patterns_is_not_said_to_be_never_sent(
    app, tmp_path: Path, story, cast, monkeypatch
) -> None:
    """Under SillyTavern's rules an entry's patterns are its keys, kept with
    its SillyTavern settings and not among its keywords: unticked, the dialog
    said it would never be sent."""

    def lore() -> list[LoreEntry]:
        fields = {"key": ["/wol(f|ves)/i"], "keysecondary": [], "constant": True, "order": 100}
        return [
            LoreEntry(
                title="Wolves",
                content="wolves " * 1500,
                always_on=True,
                tavern=TavernEntry(numbered=True, fields=fields),
            ),
            LoreEntry(title="Gate", content="gate " * 1500, always_on=True),
        ]

    story.defaults.lore_firing = "sillytavern"
    window = window_with(tmp_path / "st", story, cast, lore())
    assert _always_on_notes(window, monkeypatch) == [
        OPTIONAL_PATTERNS,
        "Optional, but it has no keywords: it would never be sent",
    ]
    window.close()
    # SealedLore's own rules don't read patterns: there it is as the dialog said.
    story.defaults.lore_firing = "sealedlore"
    window = window_with(tmp_path / "own", story, cast, lore())
    assert (
        _always_on_notes(window, monkeypatch)
        == ["Optional, but it has no keywords: it would never be sent"] * 2
    )
    window.close()
