"""Play, looked over before 3.5.0b1, on the engine's side (no Qt;
tests/test_review_3_5_window.py has the window's). The scripted mock answers."""

from __future__ import annotations

from pathlib import Path

import pytest

from sealedlore.engine.card_condense import fingerprint
from sealedlore.engine.prompt import SECTION_ROUND
from sealedlore.engine.session import SessionNotice, StorySession
from sealedlore.models.node import DIRECTOR_SPEAKER_ID, NARRATOR_SPEAKER_ID
from sealedlore.storage.repository import read_api_log
from tests.test_cast_condense import by_id
from tests.test_cast_condense import make_session as cast_session
from tests.test_reactions import MAELA_MODEL, a_story, make_session, play, round_calls, turn

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


# --- W3: no round after a Direction, a Narration or the opening ("after") ------


def _after(tmp_path: Path, responses: list[str], **fields) -> StorySession:
    story = a_story()
    story.defaults.round_order = "after"
    return make_session(tmp_path, responses, story=story, **fields)


@pytest.mark.parametrize("speaker", [DIRECTOR_SPEAKER_ID, NARRATOR_SPEAKER_ID])
def test_after_a_direction_or_narration_nobody_is_asked(tmp_path: Path, speaker: str) -> None:
    """They were asked what they do in the moment before a turn they can't
    act in (a Direction they never see, prose that is already fact), and the
    storyteller was told they had answered it."""
    session = _after(tmp_path, ["Three weeks later, snow.", "MAELA-WAS-ASKED"])
    events = play(session, turn("Three weeks pass.", speaker_id=speaker))
    assert [r.model for r in session.provider.requests] == ["story-model"]
    assert session.last_prompt.section(SECTION_ROUND) is None
    notices = [e.text for e in events if isinstance(e, SessionNotice)]
    assert not any("Maela" in text or "characters" in text for text in notices)
    turn_node, passage = session.path()[-2:]
    # Settled, with nobody: nothing under the turn, and nothing owed later.
    assert turn_node.meta.round_after and turn_node.meta.reactions == []
    assert passage.meta.reactions == []
    # Another take, and more of it, ask nobody either.
    list(session.regenerate())
    list(session.continue_passage())
    assert [r.model for r in session.provider.requests] == ["story-model"] * 3
    assert session.last_prompt.section(SECTION_ROUND) is None


def test_after_the_generated_opening_nobody_is_asked(tmp_path: Path) -> None:
    story = a_story()
    story.defaults.round_order = "after"
    story.held_character_id = None
    story.setup.opening_text = "Open in the undercroft: they find the sealed door."
    story.setup.opening_mode = "generate"
    story.setup.starting_scene = story.scene.model_copy(deep=True)
    session = make_session(tmp_path, ["The undercroft is cold.", "MAELA-WAS-ASKED"], story=story)
    list(session.begin("char-serrik"))
    assert [r.model for r in session.provider.requests] == ["story-model"]
    assert session.last_prompt.section(SECTION_ROUND) is None
    # Their first round is the author's first turn in character, as ever.
    play(session, turn("I try the door."))
    assert [r.model for r in session.provider.requests][1:] == [MAELA_MODEL, "story-model"]


def test_a_direction_after_the_story_changed_to_after_sends_no_answers(tmp_path: Path) -> None:
    """The passage before has answers from when they answered at the same
    time: the storyteller isn't told they answered a turn they skip."""
    session = make_session(tmp_path, ["The door gives.", "I pick it.", "Weeks later."])
    play(session, turn())  # "together": Maela answers the passage
    session.story.defaults.round_order = "after"
    play(session, turn("Three weeks pass.", speaker_id=DIRECTOR_SPEAKER_ID))
    assert session.last_prompt.section(SECTION_ROUND) is None
    assert len(round_calls(session)) == 1
    list(session.regenerate())
    assert session.last_prompt.section(SECTION_ROUND) is None


def test_answering_at_the_same_time_they_still_answer_such_a_passage(tmp_path: Path) -> None:
    """A guard: "together" is as it was, the passage being theirs to see."""
    session = make_session(tmp_path, ["Weeks pass.", "I count my coin."])
    play(session, turn("Five weeks pass.", speaker_id=DIRECTOR_SPEAKER_ID))
    assert [r.model for r in round_calls(session)] == [MAELA_MODEL]


def test_a_turn_in_character_is_answered_as_before(tmp_path: Path) -> None:
    """A guard: what an in-character turn sends is not this fix's to change."""
    session = _after(tmp_path, ["I hold the lamp.", "The door gives."])
    play(session, turn("I put my shoulder to the door."))
    assert [r.model for r in session.provider.requests] == [MAELA_MODEL, "story-model"]
    assert "[Maela Orr] I hold the lamp." in session.last_prompt.section(SECTION_ROUND).text
