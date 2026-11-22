"""Play, looked over before 3.5.0b1, on the engine's side (no Qt;
tests/test_review_3_5_window.py has the window's). The scripted mock answers."""

from __future__ import annotations

from pathlib import Path

from sealedlore.engine.card_condense import fingerprint
from sealedlore.storage.repository import read_api_log
from tests.test_cast_condense import by_id
from tests.test_cast_condense import make_session as cast_session

# --- E5: the author's own condensed text outlasts a job begun before it -------


def test_a_condense_begun_before_the_author_wrote_their_own_leaves_theirs(
    tmp_path: Path, story
) -> None:
    """`fingerprint` leaves the condensed text out (it is written from the
    card, not part of it), so a job asked for before the author typed their
    own found the card "unchanged" and put the model's text over theirs."""
    session = cast_session(tmp_path, story, ["The model's condensed card."])
    jane = by_id(session, "char-jane")
    assert session.start_condensing([jane])
    session.set_condensed(jane, "MY OWN WORDS: Jane, John's wife, keeps the stove going.")
    notices = session.adopt_condensed()
    assert jane.condensed == "MY OWN WORDS: Jane, John's wife, keeps the stove going."
    assert jane.condensed_from == fingerprint(jane)
    assert not any("Condensed the cards" in notice for notice in notices)
    # Paid for, so logged and counted; not a failure to try again.
    entries = read_api_log(session.story.id, root=tmp_path)
    assert any("wrote their own" in (entry.get("dropped") or "") for entry in entries)
    assert session.unreported_usage
    assert session.condense_failures() == {}
    assert "char-jane" not in {c.id for c in session.cards_to_condense()}


def test_write_it_again_still_replaces_the_text_it_was_asked_over(tmp_path: Path, story) -> None:
    session = cast_session(tmp_path, story, ["The model's second try."])
    jane = by_id(session, "char-jane")
    session.set_condensed(jane, "An earlier condensed card.")
    assert session.start_condensing([jane])
    session.adopt_condensed()
    assert jane.condensed == "The model's second try."
