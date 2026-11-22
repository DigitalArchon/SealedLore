"""Play, looked over before 3.5.0b1, on the engine's side (no Qt;
tests/test_review_3_5_window.py has the window's). The scripted mock answers."""

from __future__ import annotations

from pathlib import Path

import pytest

from sealedlore.engine.card_condense import fingerprint
from sealedlore.engine.prompt import SECTION_ROUND
from sealedlore.engine.session import SessionNotice, StorySession
from sealedlore.engine.session_reactions import ROUND_SKIPPED
from sealedlore.models.node import DIRECTOR_SPEAKER_ID, NARRATOR_SPEAKER_ID
from sealedlore.providers.base import StreamCancelled
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import load_story_bundle, read_api_log
from tests.test_cast_condense import by_id
from tests.test_cast_condense import make_session as cast_session
from tests.test_reactions import (
    MAELA_MODEL,
    a_cast,
    a_story,
    make_session,
    play,
    round_calls,
    turn,
)
from tests.test_review_3_5_lore import MAELA, SERRIK, _debt, session_with
from tests.test_review_3_5_lore import sent as sent_text
from tests.test_review_3_5_lore import turn as lore_turn

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


# --- W4: Stop skips their answers and the turn goes on; an unfinished round is owed ---


class HungOwnModel(MockChatProvider):
    """Maela's model hangs until Stop, `hangs` times: the request ends with
    the cancel, which stays set until something resets it, as a real
    client's does."""

    hangs = 1

    def stream(self, request):
        if request.model == MAELA_MODEL and self.hangs:
            self.hangs -= 1
            self.requests.append(request)
            self._cancelled = True
            raise StreamCancelled()
        if self._cancelled:
            self.requests.append(request)
            raise StreamCancelled()
        yield from super().stream(request)


def test_a_stop_while_they_are_waited_for_goes_on_without_their_answers(tmp_path: Path) -> None:
    """The wait says "Stop skips it", and after a passage it does. Before
    one, Stop ended the job and the turn left the story with it: a model that
    hung could never be got past."""
    provider = HungOwnModel()
    session = _after(
        tmp_path, ["The door gives.", "MAELA-ANSWERS-NOW", "Another take."], provider=provider
    )
    events = play(session, turn("I put my shoulder to the door."))  # no StreamCancelled
    turn_node, passage = session.path()[-2:]
    assert passage.kind == "assistant" and passage.content == "The door gives."
    assert session.last_prompt.section(SECTION_ROUND) is None
    taken = [e for e in events if isinstance(e, SessionNotice) and e.stop_taken]
    assert [e.text for e in taken] == [ROUND_SKIPPED]
    # Not marked as answered with nobody: the next take asks them, once.
    assert not turn_node.meta.round_after and turn_node.meta.reactions == []
    before = len(provider.requests)
    list(session.regenerate())
    assert [r.model for r in provider.requests[before:]] == [MAELA_MODEL, "story-model"]
    assert "[Maela Orr] MAELA-ANSWERS-NOW" in session.last_prompt.section(SECTION_ROUND).text
    assert turn_node.meta.round_after
    before = len(provider.requests)
    list(session.regenerate())
    assert [r.model for r in provider.requests[before:]] == ["story-model"]
    assert "MAELA-ANSWERS-NOW" in session.last_prompt.section(SECTION_ROUND).text


def test_an_answer_that_came_before_the_stop_is_not_the_whole_round(tmp_path: Path) -> None:
    cast = a_cast(idris_model=MAELA_MODEL)
    story = a_story()
    story.defaults.round_order = "after"
    story.scene.present_character_ids.append("char-idris")
    provider = HungOwnModel()
    provider.hangs = 0

    def hang_the_second(request, plain=provider.stream):
        if request.model == MAELA_MODEL and len(provider.requests) == 1:
            provider.hangs = 1
        yield from plain(request)

    provider.stream = hang_the_second
    session = make_session(
        tmp_path, ["I hold the lamp.", "The door gives."], story=story, cast=cast, provider=provider
    )
    play(session, turn("I put my shoulder to the door."))
    turn_node = session.path()[-2]
    assert len(round_calls(session)) == 2  # one answered, one hung
    assert turn_node.meta.reactions == [] and not turn_node.meta.round_after
    assert session.last_prompt.section(SECTION_ROUND) is None
    saved = load_story_bundle("story-1", root=tmp_path)
    assert next(n for n in saved.nodes if n.id == turn_node.id).meta.reactions == []


class FailsOnce(MockChatProvider):
    fail = True

    def stream(self, request):
        if request.model == MAELA_MODEL and self.fail:
            self.fail = False
            self.requests.append(request)
            raise RuntimeError("the round broke")
        yield from super().stream(request)


def test_a_turn_left_in_the_story_by_a_round_that_broke_is_asked_on_the_retry(
    tmp_path: Path,
) -> None:
    """A phone's turn stays in the story when its job fails (the desktop
    takes its own back). Marked as answered before anyone had been asked, its
    retry never asked the characters."""
    provider = FailsOnce()
    session = _after(tmp_path, ["MAELA-ANSWERS-NOW", "The door gives."], provider=provider)
    with pytest.raises(RuntimeError):
        play(session, turn("I put my shoulder to the door."))
    leaf = session.full_path()[-1]
    assert leaf.kind == "user" and not leaf.meta.round_after
    before = len(provider.requests)
    list(session.regenerate())  # the retry
    assert [r.model for r in provider.requests[before:]] == [MAELA_MODEL, "story-model"]
    assert "MAELA-ANSWERS-NOW" in session.last_prompt.section(SECTION_ROUND).text


def test_a_turn_answered_at_the_same_time_keeps_those_answers_on_another_take(
    tmp_path: Path,
) -> None:
    """A guard: the story changed to "after" since, the turn sent with the
    passage before's answers is not asked again, and they go as they went."""
    session = make_session(
        tmp_path, ["The door gives.", "BLIND-ANSWER", "The lock turns.", "It turns again."]
    )
    play(session, turn())
    play(session, turn("I hold the lamp."))
    session.story.defaults.round_order = "after"
    before = len(session.provider.requests)
    list(session.regenerate())
    assert [r.model for r in session.provider.requests[before:]] == ["story-model"]
    assert "BLIND-ANSWER" in session.last_prompt.section(SECTION_ROUND).text


def test_a_stop_after_a_passage_still_ends_there_with_what_came_kept(tmp_path: Path) -> None:
    """A guard for answering at the same time: Stop skips the round and the
    passage is kept, as it was."""
    provider = HungOwnModel()
    session = make_session(tmp_path, ["The door gives."], provider=provider)
    with pytest.raises(StreamCancelled):
        play(session, turn())
    assert session.path()[-1].content == "The door gives."


# --- a leftover: {{user}} naming a card since removed is settled again --------


def test_user_naming_a_card_no_longer_there_is_settled_again(tmp_path: Path, story, cast) -> None:
    """The card the story's {{user}} named was removed: read as nobody, it
    followed whoever was played from then on ("Maela Orr owes Maela Orr"),
    and was never settled again, since something was still written there."""
    story.held_character_id = SERRIK
    story.setup.placeholders.user_id = "char-removed"
    story.setup.placeholders.char_id = "char-also-removed"
    session = session_with(tmp_path, story, cast, _debt())
    list(session.send(lore_turn("I open the door.")))
    assert session.story.setup.placeholders.user_id == SERRIK
    assert "Maela Orr owes Serrik Vaun forty rounds." in sent_text(session)
    session.hold(MAELA)
    list(session.send(lore_turn("I count the rounds.", held=MAELA)))
    assert session.story.setup.placeholders.user_id == SERRIK
    assert "Maela Orr owes Serrik Vaun forty rounds." in sent_text(session)
    # {{char}}'s own choice is not this fix's: left as it was.
    assert session.story.setup.placeholders.char_id == "char-also-removed"


def test_a_user_whose_card_is_there_stays_whoever_is_played(tmp_path: Path, story, cast) -> None:
    """A guard: the author's choice of {{user}} stands while its card does."""
    story.held_character_id = SERRIK
    story.setup.placeholders.user_id = MAELA
    session = session_with(tmp_path, story, cast, _debt())
    list(session.send(lore_turn("I open the door.")))
    assert session.story.setup.placeholders.user_id == MAELA
