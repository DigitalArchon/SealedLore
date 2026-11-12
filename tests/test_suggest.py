"""Suggest my turn: a few things the author's character might do next,
offered outside the story."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.prompt import SECTION_QUESTION, SECTION_REMINDER, TurnRequest
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS
from sealedlore.engine.session import SessionNotice, StorySession
from sealedlore.engine.suggest import MAX_COUNT, parse_suggestions, suggest_request
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.usage import usage_report
from sealedlore.models.story import Story
from sealedlore.providers.base import ProviderError
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

OPTIONS = [
    '"Who else knows about this door?" I ask.',
    "I lift the lantern and go down the first three steps.",
    "I say nothing, and listen.",
]
REPLY = json.dumps(OPTIONS)


def run(events) -> tuple[list[str], list[SessionNotice]]:
    """A job's result and what it said on the way."""
    notices: list[SessionNotice] = []
    while True:
        try:
            notices.append(next(events))
        except StopIteration as done:
            return done.value, notices


def sent(request) -> str:
    return "\n".join(message.text for message in request.messages)


@pytest.fixture
def session(tmp_path: Path, story, cast) -> StorySession:
    session = make_session(tmp_path, story, cast, ["The door gives.", REPLY])
    session.story.held_character_id = "char-serrik"
    list(session.send(turn("I put my shoulder to the door.")))
    return session


# --- reading the reply ------------------------------------------------------------


def test_the_reply_is_read_as_a_list_of_options():
    assert parse_suggestions(REPLY) == OPTIONS
    fenced = f"Here are three:\n```json\n{REPLY}\n```"
    assert parse_suggestions(fenced) == OPTIONS
    labelled = json.dumps([{"kind": "speak", "text": OPTIONS[0]}, {"option": OPTIONS[1]}])
    assert parse_suggestions(labelled) == OPTIONS[:2]
    # Each once, blanks and anything that isn't words left out, no more than asked.
    assert parse_suggestions(json.dumps(["a", " a ", "", 3, None, "b", "c", "d"]), 3) == [
        "a",
        "b",
        "c",
    ]


@pytest.mark.parametrize("reply", ["I'd rather not say.", "[]", '["", "  "]', '{"a": 1}'])
def test_a_reply_that_cant_be_read_says_so_in_plain_words(reply: str):
    with pytest.raises(ValueError, match="Ask again"):
        parse_suggestions(reply)


def test_the_request_names_the_character_and_how_many():
    asked = suggest_request(held="Serrik Vaun", count=4, chat=False)
    assert asked == DEFAULT_TEXTS.fill("suggest.rules", held="Serrik Vaun", count="4")
    assert "Serrik Vaun" in asked and "JSON array of 4 strings" in asked
    assert "This is not a turn" in asked and "Use only what Serrik Vaun knows" in asked
    assert suggest_request(count=99, chat=True) == DEFAULT_TEXTS.fill(
        "chat.suggest", count=str(MAX_COUNT)
    )


# --- the call ---------------------------------------------------------------------


def test_it_returns_the_options_and_stores_nothing_in_the_story(
    session: StorySession, tmp_path: Path
):
    nodes = [node.model_dump_json() for node in session.nodes]
    leaf, result = session.story.active_leaf_id, session.last_result

    options, notices = run(session.suggest_turns())

    assert options == OPTIONS
    assert [notice.text for notice in notices] == ["Asking what Serrik Vaun might do next…"]
    # Not a message, not a Question, and the last passage is still the last result.
    assert [node.model_dump_json() for node in session.nodes] == nodes
    assert session.story.active_leaf_id == leaf and session.last_result is result
    assert session.bundle.asides == []
    saved = load_story_bundle(session.story.id, root=tmp_path)
    assert len(saved.nodes) == 2 and saved.asides == []
    stored = "".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "stories" / session.story.id).iterdir()
        if path.is_file() and path.name != "api_log.jsonl"
    )
    assert OPTIONS[1] not in stored


def test_it_rides_the_storys_prefix_as_a_question_does(session: StorySession):
    turn_request = session.provider.requests[0]
    kept = session.last_prompt

    run(session.suggest_turns())

    asked = session.provider.requests[-1]
    assert asked.model == turn_request.model
    # The same system block, so the same cache; the story so far; the ask last.
    assert asked.messages[0] == turn_request.messages[0]
    whole = sent(asked)
    assert "I put my shoulder to the door." in whole and "The door gives." in whole
    last = asked.messages[-1]
    assert last.role == "user"
    assert last.text.endswith(DEFAULT_TEXTS.fill("suggest.rules", held="Serrik Vaun", count="3"))
    # Not a turn: no directives, no REMEMBER line telling it to write on.
    assert "REMEMBER:" not in last.text and "# TURN DIRECTIVES" not in last.text
    # The Prompt page still shows the last turn's prompt.
    assert session.last_prompt is kept
    assert kept.section(SECTION_REMINDER) is not None and kept.section(SECTION_QUESTION) is None
    # It says how much to reason and where to go, as every request does.
    assert asked.params is not None and asked.extra_body is not None


def test_the_count_is_asked_for_and_held(session: StorySession):
    session.provider = MockChatProvider([json.dumps(["a", "b", "c", "d", "e"])])

    options, _ = run(session.suggest_turns(count=2))

    assert options == ["a", "b"]
    assert "JSON array of 2 strings" in session.provider.requests[-1].messages[-1].text


def test_earlier_questions_are_not_sent_with_it(session: StorySession):
    session.provider = MockChatProvider(["Because the hinge was rusted through.", REPLY])
    list(session.ask("Why did the door give so easily?"))

    run(session.suggest_turns())

    assert "rusted through" not in sent(session.provider.requests[-1])


def test_the_next_turn_carries_nothing_of_what_was_offered(session: StorySession):
    run(session.suggest_turns())
    session.provider = MockChatProvider(["The stairs go down."])

    list(session.send(turn("I go down.")))

    following = sent(session.provider.requests[-1])
    assert OPTIONS[0] not in following and OPTIONS[2] not in following
    assert "MIGHT DO NEXT" not in following and "ASKS FOR IDEAS" not in following


def test_it_is_logged_and_its_cost_counted(session: StorySession, tmp_path: Path):
    run(session.suggest_turns())

    log = read_api_log(session.story.id, root=tmp_path)
    assert [entry["kind"] for entry in log[-2:]] == ["suggest_request", "suggest_response"]
    assert log[-1]["request_log_ref"] == log[-2]["id"] and log[-1]["text"] == REPLY
    assert log[-2]["node_id"] == session.path()[-1].id
    report = usage_report(log)
    line = next(line for line in report.lines if line.label == "Turn suggestions")
    assert line.calls == 1 and line.cost == pytest.approx(session.provider.usage.cost)
    # And in the session's running cost, which the window drains.
    assert len(session.unreported_usage) == 1


def test_a_reply_that_cant_be_read_is_logged_and_refused_in_words(
    session: StorySession, tmp_path: Path
):
    session.provider = MockChatProvider(["I would rather write the next passage."])

    with pytest.raises(ValueError, match="couldn't be read"):
        run(session.suggest_turns())

    log = read_api_log(session.story.id, root=tmp_path)
    assert log[-1]["kind"] == "suggest_response"
    assert len(session.unreported_usage) == 1, "it was paid for"
    assert len(session.nodes) == 2


def test_a_failed_call_changes_nothing(session: StorySession):
    session.provider = MockChatProvider(error=ProviderError("upstream down"))

    with pytest.raises(ProviderError):
        run(session.suggest_turns())

    assert len(session.nodes) == 2 and session.bundle.asides == []


def test_nobody_played_has_nobody_to_suggest_for(session: StorySession):
    session.story.held_character_id = None
    requests = len(session.provider.requests)

    assert not session.can_suggest()
    with pytest.raises(ValueError, match="choose one under Playing"):
        run(session.suggest_turns())
    assert len(session.provider.requests) == requests


def test_a_story_not_begun_can_be_asked_too(tmp_path: Path, story, cast):
    session = make_session(tmp_path, story, cast, [REPLY])
    session.story.held_character_id = "char-serrik"

    options, _ = run(session.suggest_turns())

    assert options == OPTIONS and session.nodes == []


# --- a chat and a private scene -----------------------------------------------------


def chat_session(tmp_path: Path, replies: list[str]) -> StorySession:
    story = Story(title="Tutor", mode="chat", chat_prompt="You are a patient maths tutor.")
    story.chat_tail = "Answer in one paragraph."
    story.defaults.main_model = "anthropic/claude-sonnet-4.6"
    bundle = StoryBundle(story=story, cast=[])
    save_story_bundle(bundle, root=tmp_path)
    return StorySession(
        bundle,
        make_config(),
        MockChatProvider(replies),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def test_a_chat_is_offered_next_messages_in_its_own_words(tmp_path: Path):
    ideas = ["Can you show the step?", "What if x were negative?", "Let's try a harder one."]
    session = chat_session(tmp_path, ["x is 2.", json.dumps(ideas)])
    list(session.send(TurnRequest(speaker_id="__user__", user_text="Solve 2x = 4.")))
    assert session.can_suggest()

    options, notices = run(session.suggest_turns())

    assert options == ideas
    assert [notice.text for notice in notices] == ["Asking for a few possible next messages…"]
    asked = session.provider.requests[-1]
    assert asked.messages[-2].text == "x is 2."
    # The ask is the last message, without the chat's own tail.
    assert asked.messages[-1].text == DEFAULT_TEXTS.fill("chat.suggest", count="3")
    assert len(session.nodes) == 2
    list(session.send(TurnRequest(speaker_id="__user__", user_text="And 3x = 9?")))
    assert "possible next messages" not in sent(session.provider.requests[-1])
    assert ideas[0] not in sent(session.provider.requests[-1])


@pytest.mark.parametrize("keep", ["memory", "disk"])
def test_in_a_private_scene_it_asks_the_private_model_only(tmp_path: Path, keep: str):
    session, main, held = build_private(tmp_path)
    for _event in session.send(
        TurnRequest(speaker_id=held, user_text="I walk in.", controlled_character_id=held)
    ):
        pass
    private = enter_private(session, keep)
    for _event in session.send(
        TurnRequest(speaker_id=held, user_text=f"I whisper {MARKER}.", controlled_character_id=held)
    ):
        pass
    sent_to_main = len(main.requests)
    before = len(read_api_log(session.story.id, root=tmp_path))
    private.responses = [json.dumps([f"I ask about {MARKER}.", "I wait."])]

    options, _ = run(session.suggest_turns())

    assert options == [f"I ask about {MARKER}.", "I wait."]
    assert len(main.requests) == sent_to_main
    asked = private.requests[-1]
    # The scene as its model reads it, at the private model and budget.
    assert asked.model == "local/model" and MARKER in sent(asked)
    assert asked.extra_body == {}
    entries = read_api_log(session.story.id, root=tmp_path)[before:]
    if keep == "memory":
        assert [entry["kind"] for entry in entries] == ["private_request", "private_response"]
        assert MARKER not in json.dumps(entries)
    else:
        assert [entry["kind"] for entry in entries] == ["request", "response"]
        assert all(entry["private"] and entry["suggest"] for entry in entries)
    # After the scene, the story's model is asked, and hears nothing of it.
    private.responses = ["They spoke quietly."]
    for _event in session.close_private(session.summarise_private()):
        pass
    main.responses = [REPLY]
    run(session.suggest_turns())
    assert MARKER not in json.dumps(main.payloads)


def test_a_private_scene_with_no_model_refuses_as_a_question_does(tmp_path: Path):
    session, main, held = build_private(tmp_path)
    enter_private(session)
    session.private_provider = None
    sent_to_main = len(main.requests)

    with pytest.raises(ValueError, match="no model to ask"):
        run(session.suggest_turns())

    assert len(main.requests) == sent_to_main
