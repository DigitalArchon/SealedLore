"""Regenerate with a note: another take, steered by a few words of the
author's that go with that take alone."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.archival import render_chunk
from sealedlore.engine.chat import render_chat_chunk
from sealedlore.engine.prompt import (
    SECTION_AUTHOR_TURN,
    SECTION_REMINDER,
    SECTION_RETAKE_NOTE,
    TurnRequest,
    assemble_chat,
    assemble_prompt,
    assemble_question,
)
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.node import Node, NodeMeta
from sealedlore.models.story import Story
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, read_api_log, save_story_bundle
from tests.test_continue import make_config, make_session, turn
from tests.test_private import MARKER
from tests.test_private import build as build_private
from tests.test_private import enter as enter_private

NOTE = "She should refuse this time, and say why."


def sent(request) -> str:
    return "\n".join(message.text for message in request.messages)


@pytest.fixture
def session(tmp_path: Path, story, cast) -> StorySession:
    session = make_session(tmp_path, story, cast, ["She nods.", "She shakes her head: no."])
    list(session.send(turn("I ask her to open the gate.")))
    return session


def test_the_note_rides_the_take_after_the_turn_and_before_the_reminder(session: StorySession):
    first = session.provider.requests[0]

    list(session.regenerate(note=f"  {NOTE}  "))

    retake = session.provider.requests[-1]
    # The same request up to the tail: it rides the same cache.
    assert retake.messages[:-1] == first.messages[:-1]
    names = [section.name for section in session.last_prompt.sections]
    at = names.index(SECTION_RETAKE_NOTE)
    assert names[at - 1] == SECTION_AUTHOR_TURN and names[at + 1] == SECTION_REMINDER
    note = session.last_prompt.section(SECTION_RETAKE_NOTE).text
    words = DEFAULT_TEXTS.fill("turn.retake_note", note=NOTE)
    assert note == f"# A NOTE FOR THIS PASSAGE\n\n{words}"
    tail = retake.messages[-1].text
    assert tail.index("I ask her to open the gate.") < tail.index(NOTE) < tail.index("REMEMBER")
    # The take being replaced is not shown.
    assert "She nods." not in sent(retake)


def test_the_note_is_kept_on_the_new_take_and_never_on_the_turn(session: StorySession):
    user_node, first = session.nodes[0], session.nodes[1]

    list(session.regenerate(note=NOTE))

    new = session.path()[-1]
    assert new.id != first.id and new.parent_id == user_node.id
    assert new.content == "She shakes her head: no."
    assert new.meta.retake_note == NOTE
    assert first.meta.retake_note is None
    assert NOTE not in user_node.model_dump_json()
    assert session.takes(new.id) == [first, new]


def test_a_plain_regenerate_afterwards_goes_without_it(session: StorySession):
    list(session.regenerate(note=NOTE))

    list(session.regenerate())

    assert NOTE not in sent(session.provider.requests[-1])
    assert session.last_prompt.section(SECTION_RETAKE_NOTE) is None
    assert session.path()[-1].meta.retake_note is None


def test_a_blank_note_is_a_plain_regenerate(session: StorySession):
    first = session.provider.requests[0]

    list(session.regenerate(note="   \n "))

    assert session.provider.requests[-1].messages == first.messages
    assert session.path()[-1].meta.retake_note is None


def test_the_next_turn_never_carries_the_note(session: StorySession):
    list(session.regenerate(note=NOTE))

    list(session.send(turn("I walk away.")))

    assert NOTE not in sent(session.provider.requests[-1])


def test_the_note_reaches_no_other_request_either(story, cast):
    """Kept for the author to see, like the reasoning: no question, chat,
    summary or read is given it."""
    estimator = TokenEstimator(counter=fallback_counter)
    asked = Node(id="u0", kind="user", speaker_id="char-serrik", content="I try the door.")
    node = Node(
        id="a0",
        kind="assistant",
        speaker_id="__narrator__",
        content="The door opens.",
        meta=NodeMeta(retake_note=NOTE),
    )
    history = [asked, node]
    following = TurnRequest(speaker_id="char-serrik", user_text="I go in.")
    prompts = (
        assemble_prompt(
            story=story, cast=cast, history_nodes=history, turn=following, estimator=estimator
        ),
        assemble_question(
            story=story, cast=cast, history_nodes=history, question="Why?", estimator=estimator
        ),
        assemble_chat(story=story, history_nodes=history, tail="And then?", estimator=estimator),
    )
    texts = [message.text for prompt in prompts for message in prompt.messages]
    texts += [render_chunk(history, cast), render_chat_chunk(history)]
    assert all("The door opens." in text for text in texts[-2:])
    assert not any(NOTE in text for text in texts)


def test_a_take_swapped_in_under_later_messages_carries_its_note(tmp_path: Path, story, cast):
    session = make_session(tmp_path, story, cast, ["One.", "Two.", "One, but colder."])
    list(session.send(turn("First.")))
    list(session.send(turn("Second.")))
    first_passage, later = session.path()[1], session.path()[2:]

    list(session.regenerate(first_passage.id, note="Colder."))

    path = session.path()
    assert path[1].content == "One, but colder." and path[1].meta.retake_note == "Colder."
    assert path[2:] == later
    assert "Colder." in sent(session.provider.requests[-1])


def test_it_is_logged_with_the_request(session: StorySession, tmp_path: Path):
    list(session.regenerate(note=NOTE))

    log = read_api_log(session.story.id, root=tmp_path)
    assert [entry["kind"] for entry in log] == ["request", "response"] * 2
    assert log[2]["retake_note"] == NOTE and "retake_note" not in log[0]


def test_a_chat_reply_is_retaken_with_the_chats_own_words(tmp_path: Path):
    story = Story(title="Tutor", mode="chat", chat_prompt="You are a patient maths tutor.")
    story.chat_tail = "Answer briefly."
    story.defaults.main_model = "anthropic/claude-sonnet-4.6"
    bundle = StoryBundle(story=story, cast=[])
    save_story_bundle(bundle, root=tmp_path)
    session = StorySession(
        bundle,
        make_config(),
        MockChatProvider(["x is 2.", "Divide both sides by two: x is 2."]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    list(session.send(TurnRequest(speaker_id="__user__", user_text="Solve 2x = 4.")))

    list(session.regenerate(note="Show the step."))

    last = session.provider.requests[-1].messages[-1].text
    note = DEFAULT_TEXTS.fill("chat.retake_note", note="Show the step.")
    assert last.index("Solve 2x = 4.") < last.index(note) < last.index("Answer briefly.")
    assert "A NOTE FOR THIS PASSAGE" not in last
    assert session.path()[-1].meta.retake_note == "Show the step."
    list(session.send(TurnRequest(speaker_id="__user__", user_text="And 3x = 9?")))
    assert "Show the step." not in sent(session.provider.requests[-1])


@pytest.mark.parametrize("keep", ["memory", "disk"])
def test_in_a_private_scene_the_note_goes_to_the_private_model_only(tmp_path: Path, keep: str):
    session, main, held = build_private(tmp_path)
    for _event in session.send(
        TurnRequest(speaker_id=held, user_text="I walk in.", controlled_character_id=held)
    ):
        pass
    private = enter_private(session, keep)
    for _event in session.send(
        TurnRequest(speaker_id=held, user_text="I whisper to her.", controlled_character_id=held)
    ):
        pass
    sent_to_main = len(main.requests)

    for _event in session.regenerate(note=f"Quieter, with {MARKER}."):
        pass

    assert len(main.requests) == sent_to_main
    assert f"Quieter, with {MARKER}." in private.requests[-1].messages[-1].text
    new = session.full_path()[-1]
    assert new.meta.private_span == session.open_span.id
    assert new.meta.retake_note == f"Quieter, with {MARKER}."
    private.responses = ["They spoke quietly."]
    for _event in session.close_private(session.summarise_private()):
        pass
    for _event in session.send(
        TurnRequest(speaker_id=held, user_text="I leave.", controlled_character_id=held)
    ):
        pass
    assert MARKER not in json.dumps(main.payloads)
    on_disk = "".join(
        path.read_text(errors="ignore")
        for path in (tmp_path / "stories" / session.story.id).rglob("*")
        if path.is_file()
    )
    assert (MARKER in on_disk) == (keep == "disk")
