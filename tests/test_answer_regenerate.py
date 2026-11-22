"""Asking one character played by their own model for their answer again.

The author (Nov 22 2026): "We also need a regenerate option that regenerates
just their response." Only that character's model is asked, with the request
the round built for them at that message, and only their answer is replaced;
logged and counted as the round's are. Whatever stops it leaves the answer as
it was.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from sealedlore.engine import reactions as reactions_module
from sealedlore.engine.session import SessionNotice
from sealedlore.engine.session_reactions import ANSWER_KEPT
from sealedlore.models.node import DIRECTOR_SPEAKER_ID
from sealedlore.providers.base import ChatRequest, ProviderError, StreamCancelled, StreamEvent
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import load_story_bundle, read_api_log
from tests.test_reactions import (
    MAELA_MODEL,
    a_cast,
    a_story,
    make_session,
    play,
    sent,
    turn,
)

IDRIS_MODEL = "Local::idris"


class ByModel(MockChatProvider):
    """The storyteller's replies in turn, and each character's model its own
    (`lines`): their answers come from threads side by side, so a single
    list would be taken in whichever order they ran."""

    def __init__(self, story: list[str], lines: dict[str, list[str]]) -> None:
        super().__init__(story)
        self.lines = {model: MockChatProvider(replies) for model, replies in lines.items()}

    def stream(self, request: ChatRequest) -> Iterator[StreamEvent]:
        line = self.lines.get(request.model)
        if line is None:
            yield from super().stream(request)
            return
        self.requests.append(request)
        yield from line.stream(request)

    def cancel(self) -> None:
        super().cancel()
        for line in self.lines.values():
            line.cancel()

    def reset_cancel(self) -> None:
        super().reset_cancel()
        for line in self.lines.values():
            line.reset_cancel()


def two_answer(tmp_path: Path, maela: list[str], idris: list[str] | None = None, **kw):
    """A story where Maela and Idris, both in the scene, answer each passage
    at the same time as the author."""
    story = kw.pop("story", None) or a_story()
    if idris is not None:
        story.scene.present_character_ids.append("char-idris")
    lines = {MAELA_MODEL: maela}
    if idris is not None:
        lines[IDRIS_MODEL] = idris
    replies = kw.pop("replies", ["The door gives.", "It swings wide."])
    provider = ByModel(replies, lines)
    cast = a_cast(idris_model=IDRIS_MODEL if idris is not None else None)
    # make_session sets the storyteller's replies on the provider it is given.
    session = make_session(tmp_path, replies, story=story, cast=cast, provider=provider, **kw)
    return session, provider


def asked(provider: MockChatProvider, model: str) -> list[ChatRequest]:
    return [request for request in provider.requests if request.model == model]


def own_log(tmp_path: Path) -> list[dict]:
    return [e for e in read_api_log("story-1", root=tmp_path) if e["kind"].startswith("own_")]


# --- who is asked, and on what ---------------------------------------------------


def test_only_that_character_is_asked_and_with_the_round_s_request(tmp_path: Path) -> None:
    session, provider = two_answer(tmp_path, ["MAELA-ONE", "MAELA-TWO"], ["IDRIS-ONE"])
    play(session, turn())
    passage = session.path()[-1]
    [round_request] = asked(provider, MAELA_MODEL)
    idris_answer = next(a for a in passage.meta.reactions if a.character_id == "char-idris")
    order = [a.character_id for a in passage.meta.reactions]
    session.edit_reaction(passage.id, "char-maela", "EDITED BY THE AUTHOR")
    session.unreported_usage.clear()
    before = len(provider.requests)

    events = list(session.regenerate_reaction(passage.id, "char-maela"))

    [again] = provider.requests[before:]
    # The same request the round sent her, word for word and field for field.
    assert again == round_request
    assert "IDRIS-ONE" not in sent(again)  # never another's answer
    # Hers replaced in its place; his untouched.
    assert [a.character_id for a in passage.meta.reactions] == order
    maela = next(a for a in passage.meta.reactions if a.character_id == "char-maela")
    assert (maela.text, maela.model, maela.edited_at) == ("MAELA-TWO", MAELA_MODEL, None)
    assert maela.usage.cost == provider.lines[MAELA_MODEL].usage.cost
    assert next(a for a in passage.meta.reactions if a.character_id == "char-idris") is (
        idris_answer
    )
    # Counted in the session's spend, and said.
    assert [u.cost for u in session.unreported_usage] == [maela.usage.cost]
    assert any(
        isinstance(e, SessionNotice) and e.text == "Maela Orr answered again." for e in events
    )
    # Kept with the story.
    reloaded = load_story_bundle("story-1", root=tmp_path)
    kept = next(n for n in reloaded.nodes if n.id == passage.id).meta.reactions
    assert [a.text for a in kept if a.character_id == "char-maela"] == ["MAELA-TWO"]


def test_asking_again_is_logged_as_the_round_s_answers_are(tmp_path: Path) -> None:
    from sealedlore.engine.usage import usage_report

    session, _provider = two_answer(tmp_path, ["MAELA-ONE", "MAELA-TWO"])
    play(session, turn())
    passage = session.path()[-1]
    list(session.regenerate_reaction(passage.id, "char-maela"))
    log = own_log(tmp_path)
    assert [e["kind"] for e in log] == ["own_request", "own_response"] * 2
    request, response = log[-2:]
    assert (request["node_id"], request["character"]) == (passage.id, "Maela Orr")
    assert response["request_log_ref"] == request["id"] and response["text"] == "MAELA-TWO"
    lines = {
        line.label: line for line in usage_report(read_api_log("story-1", root=tmp_path)).lines
    }
    assert lines["Own-model characters"].calls == 2


def test_an_older_answer_is_asked_for_on_the_story_as_it_was_then(tmp_path: Path) -> None:
    session, provider = two_answer(
        tmp_path, ["MAELA-ONE", "MAELA-TWO", "MAELA-ONE-AGAIN"], replies=["P-ONE.", "P-TWO."]
    )
    play(session, turn())
    first = session.path()[-1]
    play(session, turn("I go in."))
    second = session.path()[-1]
    round_one, round_two = asked(provider, MAELA_MODEL)
    before = len(provider.requests)

    list(session.regenerate_reaction(first.id, "char-maela"))

    [again] = provider.requests[before:]
    assert again == round_one != round_two
    assert "P-TWO." not in sent(again) and "I go in." not in sent(again)
    assert [a.text for a in first.meta.reactions] == ["MAELA-ONE-AGAIN"]
    assert [a.text for a in second.meta.reactions] == ["MAELA-TWO"]


def test_after_the_turn_she_is_asked_again_having_seen_it(tmp_path: Path) -> None:
    story = a_story()
    story.defaults.round_order = "after"
    session, provider = two_answer(tmp_path, ["LAMP-UP", "LAMP-DOWN"], story=story)
    play(session, turn("I put my shoulder to the door."))
    turn_node, passage = session.path()[-2:]
    [round_request] = asked(provider, MAELA_MODEL)
    before = len(provider.requests)

    list(session.regenerate_reaction(turn_node.id, "char-maela"))

    [again] = provider.requests[before:]  # hers alone: no passage is written
    assert again == round_request
    assert "has just taken their turn" in sent(again)
    assert [a.text for a in turn_node.meta.reactions] == ["LAMP-DOWN"]
    assert turn_node.meta.round_after and passage.meta.reactions == []


def test_what_she_remembers_is_written_first_where_the_round_would(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(reactions_module, "HISTORY_TOKENS", 30)
    monkeypatch.setattr(reactions_module, "RECOLLECT_MESSAGES", 4)
    replies = [f"PASSAGE-{i} " + "word " * 20 for i in range(4)]
    answers = [f"ANSWER-{i}" for i in range(4)] + ["ANSWER-AGAIN"]
    session, provider = two_answer(tmp_path, answers, replies=replies)
    for i in range(4):
        play(session, turn(f"Turn {i}."))
    [memory] = session.bundle.memories
    last = session.path()[-1]
    round_request = asked(provider, MAELA_MODEL)[-1]
    assert memory.content in sent(round_request)
    session.bundle.memories = []  # as if writing it had failed in the round
    # Written on the summarisation model, which falls back to the storyteller's.
    provider.responses = ["I REMEMBER THE STAIRS."]
    before = len(provider.requests)

    list(session.regenerate_reaction(last.id, "char-maela"))

    written = [r for r in provider.requests[before:] if r.model != MAELA_MODEL]
    [again] = asked(provider, MAELA_MODEL)[-1:]
    assert len(written) == 1 and provider.requests[-1] is again  # the memory first
    [rewritten] = session.bundle.memories
    assert rewritten.covered_node_ids == memory.covered_node_ids
    # The round's request, with the memory written again in it.
    assert rewritten.content == "I REMEMBER THE STAIRS."
    assert sent(again) == sent(round_request).replace(memory.content, rewritten.content)


# --- whatever stops it leaves the answer as it was ------------------------------


def test_a_failed_call_keeps_the_answer_and_says_why(tmp_path: Path) -> None:
    session, provider = two_answer(tmp_path, ["MAELA-ONE"])
    play(session, turn())
    passage = session.path()[-1]
    old = passage.meta.reactions[0]
    provider.lines[MAELA_MODEL].error = ProviderError("the endpoint is down")
    with pytest.raises(ProviderError) as raised:
        list(session.regenerate_reaction(passage.id, "char-maela"))
    assert "Maela Orr's model couldn't answer: the endpoint is down" in str(raised.value)
    assert ANSWER_KEPT in str(raised.value)
    assert passage.meta.reactions == [old] and passage.meta.reactions[0] is old
    assert own_log(tmp_path)[-1]["error"] == "the endpoint is down"


def test_a_declined_reply_keeps_the_answer_and_is_counted(tmp_path: Path) -> None:
    session, provider = two_answer(tmp_path, ["MAELA-ONE", "NOPE"])
    play(session, turn())
    passage = session.path()[-1]
    old = passage.meta.reactions[0]
    provider.lines[MAELA_MODEL].finish_reason = "content_filter"
    session.unreported_usage.clear()
    with pytest.raises(ProviderError) as raised:
        list(session.regenerate_reaction(passage.id, "char-maela"))
    assert "Maela Orr's model gave no answer" in str(raised.value)
    assert passage.meta.reactions[0] is old
    assert len(session.unreported_usage) == 1  # paid for, so counted
    assert own_log(tmp_path)[-1]["text"] == "NOPE"


def test_a_stop_keeps_the_answer(tmp_path: Path) -> None:
    session, provider = two_answer(tmp_path, ["MAELA-ONE"])
    play(session, turn())
    passage = session.path()[-1]
    old = passage.meta.reactions[0]
    provider.lines[MAELA_MODEL].error = StreamCancelled()
    with pytest.raises(StreamCancelled):
        list(session.regenerate_reaction(passage.id, "char-maela"))
    assert passage.meta.reactions[0] is old
    assert own_log(tmp_path)[-1]["stopped_early"] is True


def test_a_model_no_longer_set_up_is_said_and_nothing_is_sent(tmp_path: Path) -> None:
    """As the round says it (`_own_call`), and the answer stands."""
    from tests.test_review_b14 import NoSuchEndpoint

    session = make_session(tmp_path, ["The door gives.", "MAELA-ONE"], provider=NoSuchEndpoint())
    play(session, turn())
    passage = session.path()[-1]
    old = passage.meta.reactions[0]
    next(c for c in session.cast if c.id == "char-maela").own_model = "Gone::maela-lora"
    before = len(session.provider.requests)
    with pytest.raises(ProviderError) as raised:
        list(session.regenerate_reaction(passage.id, "char-maela"))
    assert "Maela Orr's model (Gone::maela-lora) couldn't be asked" in str(raised.value)
    assert ANSWER_KEPT in str(raised.value)
    assert len(session.provider.requests) == before
    assert passage.meta.reactions[0] is old


# --- where it is refused ---------------------------------------------------------


def test_it_is_refused_where_no_round_runs(tmp_path: Path) -> None:
    session, provider = two_answer(tmp_path, ["MAELA-ONE"], replies=["The door gives.", "Hush."])
    play(session, turn())
    passage = session.path()[-1]
    assert session.reaction_refusal(passage.id, "char-maela") is None
    # Nobody else's answer, and no message it isn't on.
    assert session.reaction_refusal(passage.id, "char-idris") is not None
    assert session.reaction_refusal("no-such-message", "char-maela") is not None
    # In a private scene nothing else is asked.
    private = MockChatProvider(["Hush."])
    session.enter_private(keep="memory", provider=private, model="local/model")
    refusal = session.reaction_refusal(passage.id, "char-maela")
    assert refusal is not None and "private scene" in refusal
    before = len(provider.requests)
    with pytest.raises(ValueError, match="private scene"):
        list(session.regenerate_reaction(passage.id, "char-maela"))
    assert len(provider.requests) == before and private.requests == []


def test_it_is_refused_in_a_chat_and_once_their_model_is_taken_away(tmp_path: Path) -> None:
    session, _provider = two_answer(tmp_path, ["MAELA-ONE"])
    play(session, turn())
    passage = session.path()[-1]
    maela = next(c for c in session.cast if c.id == "char-maela")
    maela.own_model = None
    refusal = session.reaction_refusal(passage.id, "char-maela")
    assert refusal is not None and "isn't played by a model of their own" in refusal
    maela.own_model = MAELA_MODEL
    session.story.mode = "chat"
    assert "chat" in session.reaction_refusal(passage.id, "char-maela")


def test_nobody_is_asked_again_for_a_direction(tmp_path: Path) -> None:
    """A Direction gets no round after it (`reactions.answers_turn`), so
    nothing on it is asked for again either."""
    story = a_story()
    story.defaults.round_order = "after"
    session, _provider = two_answer(tmp_path, ["MAELA-ONE"], story=story)
    play(session, turn("Five weeks pass.", speaker_id=DIRECTOR_SPEAKER_ID))
    direction = session.path()[-2]
    assert direction.meta.reactions == []
    refusal = session.reaction_refusal(direction.id, "char-maela")
    assert refusal is not None and "Direction" in refusal
