"""Change this passage…: the passage written again with a change the author
asks for, as a new take beside it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.prompt import SECTION_REMINDER, TurnRequest
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS
from sealedlore.engine.revision import (
    NOTHING_CAME,
    SECTION_REVISE,
    SECTION_REVISE_FROM,
    nothing_came,
    revisable,
)
from sealedlore.engine.session import PassageDone, SessionNotice, StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.node import Node
from sealedlore.models.story import Story
from sealedlore.models.summary import Summary
from sealedlore.providers.base import ProviderError, TextDelta
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import (
    StoryBundle,
    load_story_bundle,
    read_api_log,
    save_story_bundle,
)
from tests.test_continue import make_config, make_session, turn
from tests.test_private import MARKER
from tests.test_private import build as build_private
from tests.test_private import enter as enter_private

FIRST = "She opens the gate and waves him through. The yard beyond is empty."
CHANGED = "She opens the gate and waves him through. The yard beyond is full of soldiers."
CHANGE = "The yard should be full of soldiers."


def sent(request) -> str:
    return "\n".join(message.text for message in request.messages)


@pytest.fixture
def session(tmp_path: Path, story, cast) -> StorySession:
    session = make_session(tmp_path, story, cast, [FIRST, CHANGED])
    list(session.send(turn("I ask her to open the gate.")))
    return session


# --- the request -----------------------------------------------------------------


def test_the_request_is_the_turns_own_then_the_passage_and_the_change(session: StorySession):
    first = session.provider.requests[0]
    passage = session.path()[-1]

    list(session.revise_passage(passage.id, f"  {CHANGE} "))

    asked = session.provider.requests[-1]
    # Byte for byte the take's own request first: it rides the same cache.
    assert asked.messages[: len(first.messages)] == first.messages
    was, ask = asked.messages[len(first.messages) :]
    assert was.role == "assistant" and was.text == FIRST
    assert ask.role == "user"
    assert ask.text.startswith("# CHANGE THE PASSAGE\n\n")
    assert DEFAULT_TEXTS.fill("turn.revise", change=CHANGE) in ask.text
    reminder = session.last_prompt.section(SECTION_REMINDER).text
    assert reminder.startswith("REMEMBER") and ask.text.endswith(reminder)
    names = [section.name for section in session.last_prompt.sections]
    assert names[-2:] == [SECTION_REVISE_FROM, SECTION_REVISE]


def test_the_text_says_to_keep_what_was_not_asked_about():
    text = DEFAULT_TEXTS["turn.revise"]
    assert "Change only what it asks for" in text and "the whole passage" in text
    assert "nothing else" in text


# --- the take it makes -------------------------------------------------------------


def test_the_change_is_a_new_take_beside_the_passage(session: StorySession):
    user_node, original = session.path()

    events = list(session.revise_passage(original.id, CHANGE))

    new = session.path()[-1]
    assert new.id != original.id and new.parent_id == user_node.id
    assert new.content == CHANGED and original.content == FIRST
    assert session.takes(new.id) == [original, new]
    assert session.take_position(new.id) == (2, 2)
    assert new.meta.revised_from == original.id and new.meta.revise_note == CHANGE
    assert original.meta.revised_from is None and original.meta.revise_note is None
    assert CHANGE not in user_node.model_dump_json()
    assert any(isinstance(event, PassageDone) for event in events)
    # The one before is a ‹ away.
    session.switch_take(new.id, -1)
    assert session.path()[-1] is original


def test_the_latest_passage_is_read_again_as_after_regenerate(tmp_path: Path, story, cast):
    session = make_session(
        tmp_path, story, cast, [FIRST, "{}", CHANGED, "{}"], scene_reads="every_turn"
    )
    list(session.send(turn("I ask her to open the gate.")))
    before = len(session.provider.requests)

    list(session.revise_passage(session.path()[-1].id, CHANGE))

    after = session.provider.requests[before:]
    assert [request.model for request in after] == [
        "anthropic/claude-sonnet-4.6",
        "small/fast-model",
    ]
    assert CHANGED in after[-1].messages[-1].text and FIRST not in after[-1].messages[-1].text
    assert session.path()[-1].meta.scene_read


def older(tmp_path: Path, story, cast, *, scene_reads: str = "manual") -> StorySession:
    replies = ["One.", "Two.", "Three."] if scene_reads == "manual" else ["One.", "{}"]
    session = make_session(tmp_path, story, cast, replies, scene_reads=scene_reads)
    for text in ("First.", "Second.", "Third."):
        list(session.send(turn(text)))
    return session


def test_an_older_passage_is_changed_in_its_place(tmp_path: Path, story, cast):
    session = older(tmp_path, story, cast)
    original, later, leaf = session.path()[1], session.path()[2:], session.path()[-1]
    chapter = Summary(
        covered_node_ids=[node.id for node in session.path()[:4]], content="One and two."
    )
    session.bundle.summaries.append(chapter)
    session.provider = MockChatProvider(["One, in the rain."])

    events = list(session.revise_passage(original.id, "It is raining."))

    path = session.path()
    assert path[1].content == "One, in the rain." and path[2:] == later
    assert session.story.active_leaf_id == leaf.id
    assert path[1].meta.revised_from == original.id
    assert original.children == [] and session.take_position(path[1].id) == (2, 2)
    # The chapter covering the old take covers the new one, and is out of date.
    assert chapter.stale and chapter.covered_node_ids[1] == path[1].id
    assert any(isinstance(event, PassageDone) for event in events)
    # The passage only: the reads look at where the story ends.
    assert len(session.provider.requests) == 1


def test_an_older_change_runs_no_reads(tmp_path: Path, story, cast):
    session = older(tmp_path, story, cast, scene_reads="every_turn")
    session.provider = MockChatProvider(["One, in the rain.", "{}"])

    list(session.revise_passage(session.path()[1].id, "It is raining."))

    assert len(session.provider.requests) == 1


def test_a_branchs_first_passage_stays_that_branchs(session: StorySession):
    original = session.path()[-1]
    original.meta.branch_name = "The yard"

    list(session.revise_passage(original.id, CHANGE))

    assert session.path()[-1].meta.branch_name == "The yard"


def test_it_survives_a_reload(session: StorySession, tmp_path: Path):
    original = session.path()[-1]
    list(session.revise_passage(original.id, CHANGE))

    reloaded = load_story_bundle(session.story.id, root=tmp_path)
    leaf = next(node for node in reloaded.nodes if node.id == reloaded.story.active_leaf_id)
    assert leaf.content == CHANGED
    assert leaf.meta.revised_from == original.id and leaf.meta.revise_note == CHANGE


def test_it_is_logged_as_the_storys_own_call(session: StorySession, tmp_path: Path):
    original = session.path()[-1]

    list(session.revise_passage(original.id, CHANGE))

    log = read_api_log(session.story.id, root=tmp_path)
    assert [entry["kind"] for entry in log] == ["request", "response"] * 2
    assert log[2]["revises"] == original.id and log[3]["revises"] == original.id
    assert log[3]["node_id"] == session.path()[-1].id and log[3]["text"] == CHANGED


# --- never sent again ----------------------------------------------------------------


def test_the_next_turn_carries_the_new_take_and_nothing_of_the_change_asked(
    session: StorySession,
):
    list(session.revise_passage(session.path()[-1].id, CHANGE))

    list(session.send(turn("I walk in.")))

    following = sent(session.provider.requests[-1])
    assert CHANGED in following and FIRST not in following
    assert CHANGE not in following and "CHANGE THE PASSAGE" not in following


def test_a_plain_regenerate_afterwards_knows_nothing_of_it(session: StorySession):
    list(session.revise_passage(session.path()[-1].id, CHANGE))

    list(session.regenerate())

    again = sent(session.provider.requests[-1])
    assert CHANGE not in again and FIRST not in again and CHANGED not in again
    new = session.path()[-1]
    assert new.meta.revised_from is None and new.meta.revise_note is None
    assert session.take_position(new.id) == (3, 3)


# --- what can't be changed, and when nothing comes ----------------------------------


def test_only_a_passage_answering_a_turn_can_be_changed(session: StorySession):
    user_node, passage = session.path()
    opening = Node(kind="assistant", speaker_id="__narrator__", content="The keep stood empty.")
    summary = passage.model_copy(deep=True)
    summary.meta.private_summary_of = "span-1"

    assert revisable([user_node, passage])
    assert not revisable([opening]) and not revisable([user_node, summary])
    assert session.can_revise(passage.id)
    assert not session.can_revise(user_node.id) and not session.can_revise("no-such-id")
    with pytest.raises(ValueError, match="answer"):
        list(session.revise_passage(user_node.id, CHANGE))
    with pytest.raises(ValueError, match="Say what should change"):
        list(session.revise_passage(passage.id, "  \n"))
    assert len(session.provider.requests) == 1


def test_a_stopped_change_attaches_nothing(session: StorySession, tmp_path: Path):
    original = session.path()[-1]
    session.provider = MockChatProvider([CHANGED], chunk_size=6)

    events = session.revise_passage(original.id, CHANGE)
    while not isinstance(next(events), TextDelta):
        pass
    events.close()  # Stop

    assert session.path()[-1] is original and len(session.nodes) == 2
    assert original.content == FIRST and original.meta.notes == []
    assert session.last_result.node is None and session.last_result.stopped_early
    # The log keeps what came.
    log = read_api_log(session.story.id, root=tmp_path)
    assert log[-1]["stopped_early"] and log[-1]["node_id"] is None and log[-1]["text"]


def test_a_stopped_change_of_an_older_passage_swaps_nothing(tmp_path: Path, story, cast):
    session = older(tmp_path, story, cast)
    path = list(session.path())
    session.provider = MockChatProvider(["One, in the rain, at length."], chunk_size=6)

    events = session.revise_passage(path[1].id, "It is raining.")
    while not isinstance(next(events), TextDelta):
        pass
    events.close()

    assert session.path() == path and len(session.nodes) == 6


def test_an_empty_reply_attaches_nothing_and_says_so(session: StorySession):
    original = session.path()[-1]
    session.provider = MockChatProvider(["  \n"])

    events = list(session.revise_passage(original.id, CHANGE))

    assert session.path()[-1] is original and len(session.nodes) == 2
    notices = [event.text for event in events if isinstance(event, SessionNotice)]
    assert notices == [NOTHING_CAME]
    assert not any(isinstance(event, PassageDone) for event in events)
    assert original.meta.notes == []


def test_what_is_said_when_nothing_came_names_the_cause():
    assert nothing_came("stop", False) == NOTHING_CAME
    assert "length limit" in nothing_came("length", False)
    assert "declined" in nothing_came("content_filter", True)


def test_a_failed_change_leaves_the_passage_as_it_was(session: StorySession):
    original = session.path()[-1]
    session.provider = MockChatProvider(error=ProviderError("upstream down"))

    with pytest.raises(ProviderError):
        list(session.revise_passage(original.id, CHANGE))

    assert session.path()[-1] is original and len(session.nodes) == 2


# --- a chat and a private scene -----------------------------------------------------


def test_a_chat_reply_is_changed_with_the_chats_own_words(tmp_path: Path):
    story = Story(title="Tutor", mode="chat", chat_prompt="You are a patient maths tutor.")
    story.defaults.main_model = "anthropic/claude-sonnet-4.6"
    bundle = StoryBundle(story=story, cast=[])
    save_story_bundle(bundle, root=tmp_path)
    session = StorySession(
        bundle,
        make_config(),
        MockChatProvider(["x is 2.", "x is two."]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    list(session.send(TurnRequest(speaker_id="__user__", user_text="Solve 2x = 4.")))
    reply = session.path()[-1]

    list(session.revise_passage(reply.id, "Write the number as a word."))

    was, ask = session.provider.requests[-1].messages[-2:]
    assert was.role == "assistant" and was.text == "x is 2."
    assert ask.text == DEFAULT_TEXTS.fill("chat.revise", change="Write the number as a word.")
    assert "REMEMBER" not in ask.text
    assert session.path()[-1].content == "x is two."
    assert session.takes(reply.id) == [reply, session.path()[-1]]


def test_a_private_scene_changes_only_its_own_passages(tmp_path: Path):
    session, main, held = build_private(tmp_path)
    for _event in session.send(
        TurnRequest(speaker_id=held, user_text="I walk in.", controlled_character_id=held)
    ):
        pass
    public = session.full_path()[-1]
    assert session.can_revise(public.id)
    private = enter_private(session)
    # The scene is open: changing a passage from before it would send the
    # request to the story's model, which the scene pauses.
    assert not session.can_revise(public.id)
    with pytest.raises(ValueError, match="leave the private scene"):
        list(session.revise_passage(public.id, "Shorter."))
    for _event in session.send(
        TurnRequest(speaker_id=held, user_text="I whisper.", controlled_character_id=held)
    ):
        pass
    scene_passage = session.full_path()[-1]
    assert session.can_revise(scene_passage.id)
    private.responses = ["They spoke quietly."]
    for _event in session.close_private(session.summarise_private()):
        pass
    # Ended: its messages stay as they are, and its summary is no passage.
    assert not session.can_revise(scene_passage.id)
    with pytest.raises(ValueError, match="has ended"):
        list(session.revise_passage(scene_passage.id, "Louder."))
    assert not session.can_revise(session.full_path()[-1].id)
    assert MARKER not in json.dumps(main.payloads)


@pytest.mark.parametrize("keep", ["memory", "disk"])
def test_in_a_private_scene_it_goes_to_the_private_model_only(tmp_path: Path, keep: str):
    session, main, held = build_private(tmp_path)
    private = enter_private(session, keep)
    for _event in session.send(
        TurnRequest(speaker_id=held, user_text="I whisper to her.", controlled_character_id=held)
    ):
        pass
    original = session.full_path()[-1]
    sent_to_main = len(main.requests)
    private.responses = [f"A quieter reply that holds {MARKER} closer."]

    for _event in session.revise_passage(original.id, f"Quieter, about {MARKER}."):
        pass

    assert len(main.requests) == sent_to_main
    new = session.full_path()[-1]
    assert new.content == f"A quieter reply that holds {MARKER} closer."
    assert new.meta.private_span == session.open_span.id == original.meta.private_span
    assert new.meta.revised_from == original.id
    assert private.requests[-1].messages[-2].text == original.content
    assert f"Quieter, about {MARKER}." in private.requests[-1].messages[-1].text
    log = json.dumps(read_api_log(session.story.id, root=tmp_path))
    assert (MARKER in log) == (keep == "disk")
