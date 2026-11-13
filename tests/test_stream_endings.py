"""How a reply ended, and what is done with it: a stream that ended before
the model said it was done (a gateway closing the connection partway reads as
a clean end), or a reply the model declined to write, is kept under a passage
with a note, and never taken for a whole summary."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from sealedlore.engine.archival import summary_unfit
from sealedlore.engine.ledger import check_ledger
from sealedlore.engine.session import DECLINED, UNFINISHED, SessionNotice, StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.config import ProviderConfig
from sealedlore.providers.anthropic import AnthropicProvider
from sealedlore.providers.base import (
    ChatRequest,
    ProviderError,
    StreamCompleted,
    declined,
    why_not_whole,
)
from sealedlore.providers.mock import MockChatProvider
from sealedlore.providers.openai_compat import OpenAICompatibleProvider
from sealedlore.storage.repository import StoryBundle, save_story_bundle
from tests.conftest import make_exchange
from tests.test_merge import chapters
from tests.test_session import make_config, session, turn  # noqa: F401 - fixture

UNTERMINATED = {"finish_reason": None, "unterminated": True}


class Ending(MockChatProvider):
    """A mock whose replies end as `how` says, the summariser's only when
    `summaries_only` (a story turn's prompt carries its directives)."""

    def __init__(self, responses, how: dict, *, summaries_only: bool = False) -> None:
        super().__init__(responses)
        self.how = how
        self.summaries_only = summaries_only

    def stream(self, request):
        turn_prompt = "TURN DIRECTIVES" in json.dumps([m.text for m in request.messages])
        for event in super().stream(request):
            if isinstance(event, StreamCompleted) and not (self.summaries_only and turn_prompt):
                event = replace(event, **self.how)
            yield event


# --- the providers say how the stream ended -------------------------------------------


def openai_body(*, finish: bool, done: bool) -> bytes:
    chunks = [{"choices": [{"delta": {"content": "Half a"}}]}]
    if finish:
        chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


@pytest.mark.parametrize(
    ("finish", "done", "unterminated"),
    [(False, False, True), (True, False, False), (False, True, False), (True, True, False)],
)
def test_an_openai_stream_with_neither_marker_is_said_to_be_unfinished(finish, done, unterminated):
    body = openai_body(finish=finish, done=done)
    provider = OpenAICompatibleProvider(
        ProviderConfig(name="t", base_url="https://example.test/v1", api_key="k"),
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))
        ),
    )
    text, completed = provider.complete(ChatRequest(model="m", messages=[]))
    assert text == "Half a" and completed.unterminated is unterminated


def anthropic_body(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


START = {"type": "message_start", "message": {"id": "msg_1", "usage": {"input_tokens": 5}}}
DELTA = {
    "type": "content_block_delta",
    "index": 0,
    "delta": {"type": "text_delta", "text": "Half a"},
}


@pytest.mark.parametrize(
    ("events", "unterminated"),
    [
        ((START, DELTA), True),
        ((START, DELTA, {"type": "message_stop"}), False),
        ((START, DELTA, {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}), False),
    ],
)
def test_an_anthropic_stream_with_neither_marker_is_said_to_be_unfinished(events, unterminated):
    text, completed = anthropic_reply(*events)
    assert text == "Half a" and completed.unterminated is unterminated


def anthropic_reply(*events: dict) -> tuple[str, StreamCompleted]:
    body = anthropic_body(*events)
    provider = AnthropicProvider(
        ProviderConfig(
            name="a", base_url="https://api.anthropic.com", api_key="k", api="anthropic"
        ),
        client=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))
        ),
    )
    hello = [PromptMessage(role="user", parts=(ContentPart(text="Hi."),))]
    return provider.complete(ChatRequest(model="claude-sonnet-4-6", messages=hello))


# --- a passage keeps what came, and says so -------------------------------------------


def test_an_unfinished_passage_is_kept_with_a_note_that_continue_takes_away(
    session: StorySession,  # noqa: F811
):
    session.provider = Ending(["The door gives, and"], UNTERMINATED)
    list(session.send(turn()))
    passage = session.path()[-1]
    assert passage.content == "The door gives, and"
    assert any(note.startswith(UNFINISHED) for note in passage.meta.notes)
    session.provider = MockChatProvider(["beyond it the dark."])
    list(session.continue_passage())
    assert passage.content.endswith("beyond it the dark.")
    assert not any(note.startswith(UNFINISHED) for note in passage.meta.notes)


def test_a_whole_passage_has_no_such_note(session: StorySession):  # noqa: F811
    list(session.send(turn()))
    assert session.path()[-1].meta.notes == []


# --- a summary never stands in for the prose half-written ------------------------------


def test_every_summary_refuses_what_its_reply_left_unfinished():
    assert summary_unfit(StreamCompleted(finish_reason="stop")) is None
    assert "wasn't whole" in summary_unfit(StreamCompleted(**UNTERMINATED))
    ledger = "\n".join(f"## {h}\n- a line" for h in ("Bonds", "Who knows what"))
    assert check_ledger(ledger, None, finish_reason=None, unfit="it ended early")


def test_an_unfinished_chapter_is_not_archived(session: StorySession):  # noqa: F811
    for _ in range(2):
        list(session.send(turn()))
    session.provider = Ending(["Serrik forced the door and"], UNTERMINATED)
    with pytest.raises(ProviderError, match="wasn't whole"):
        session.archive(session.path()[:2])
    assert session.summaries == []


def test_an_unfinished_rebuild_leaves_the_chapter_as_it_was(session: StorySession):  # noqa: F811
    for _ in range(2):
        list(session.send(turn()))
    summary = session.archive(session.path()[:2])
    session.provider = Ending(["Serrik forced the door and"], UNTERMINATED)
    with pytest.raises(ProviderError, match="wasn't whole"):
        session.rebuild_summary(summary.id)
    assert summary.content == "The door gives with a groan."


def test_an_unfinished_merge_is_not_a_part(tmp_path: Path, story, cast):
    nodes = make_exchange(16)
    bundle = StoryBundle(story=story, cast=cast, nodes=nodes, summaries=chapters(nodes, 6))
    story.active_leaf_id = nodes[-1].id
    save_story_bundle(bundle, root=tmp_path)
    merging = StorySession(
        bundle,
        make_config(),
        Ending(["Merged, half"], UNTERMINATED),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    merging.story.defaults.context_token_budget = 2_000
    assert merging.merge_run(), "the run is due"
    assert merging.merge_now() is None
    assert "wasn't whole" in (merging.merge_problem or "")
    assert not any(s.merged_from for s in merging.summaries)


def test_an_unfinished_private_summary_is_refused(tmp_path: Path):
    from tests.test_private import build, play
    from tests.test_private import turn as private_turn

    scene, _main, held = build(tmp_path)
    play(scene, private_turn(held, "Before the scene."))
    private = Ending(["A quiet reply."], UNTERMINATED, summaries_only=True)
    scene.enter_private(keep="memory", provider=private, model="local/model")
    play(scene, private_turn(held, "A private turn."))
    private.responses = ["They spoke and"]
    with pytest.raises(ProviderError, match="wasn't whole"):
        scene.summarise_private()


def test_an_unfinished_condensed_card_is_refused(session: StorySession):  # noqa: F811
    card = session.cast[0]
    card.full_description = "A long account of him. " * 200
    session.provider = Ending(["He is a man who"], UNTERMINATED)
    with pytest.raises(ValueError, match="wasn't whole"):
        session.condense_card(card)
    assert card.condensed is None


# --- a refusal ---------------------------------------------------------------------------

REFUSED = {"finish_reason": "refusal", "refusal_category": "cyber"}


def test_anthropic_s_refusal_is_kept_by_its_name_with_its_category():
    refusal = {
        "type": "message_delta",
        "delta": {
            "stop_reason": "refusal",
            "stop_details": {"type": "refusal", "category": "cyber", "explanation": "..."},
        },
    }
    _text, completed = anthropic_reply(START, DELTA, refusal, {"type": "message_stop"})
    assert completed.finish_reason == "refusal" and completed.refusal_category == "cyber"
    assert declined(completed)
    # The category may be null even on a refusal: the stop reason is what counts.
    unsaid = {"type": "message_delta", "delta": {"stop_reason": "refusal", "stop_details": None}}
    _text, completed = anthropic_reply(START, unsaid)
    assert declined(completed) and completed.refusal_category is None


def test_a_content_filter_is_a_refusal_too():
    assert declined(StreamCompleted(finish_reason="content_filter"))
    assert "content filter" in why_not_whole(StreamCompleted(finish_reason="content_filter"))
    assert not declined(StreamCompleted(finish_reason="stop"))


def test_a_refused_passage_says_so_and_is_not_read(session: StorySession):  # noqa: F811
    session.config.scene_reads = "every_turn"
    session.provider = Ending(["The door gives, and then"], REFUSED)
    list(session.send(turn()))
    passage = session.path()[-1]
    assert passage.content == "The door gives, and then", "what came is kept as it was"
    (note,) = passage.meta.notes
    assert note.startswith(DECLINED) and "cyber" in note
    assert len(session.provider.requests) == 1, "no scene read on a refusal"
    assert session.last_result.refused and session.last_result.unread


def test_a_chat_s_refused_reply_speaks_of_a_chat(tmp_path: Path):
    from tests.test_chat import chat_session, say

    chat, _provider = chat_session(tmp_path, [])
    chat.provider = Ending(["Well, I"], REFUSED)
    say(chat, "Tell me something.")
    (note,) = chat.path()[-1].meta.notes
    assert note.startswith(DECLINED + " reply") and "scene" not in note


def test_a_refusal_before_any_text_says_so_under_the_turn(session: StorySession):  # noqa: F811
    session.provider = Ending([""], REFUSED)
    said = [e.text for e in session.send(turn()) if isinstance(e, SessionNotice)]
    assert [node.kind for node in session.nodes] == ["user"]
    assert any(text.startswith(DECLINED) for text in said)


def test_a_refused_summary_is_not_a_chapter(session: StorySession):  # noqa: F811
    for _ in range(2):
        list(session.send(turn()))
    session.provider = Ending(["I won't summarise this."], REFUSED)
    with pytest.raises(ProviderError, match="declined"):
        session.archive(session.path()[:2])
    assert session.summaries == []
    ledger = "\n".join(f"## {h}\n- a line" for h in ("Bonds", "Who knows what"))
    refused = why_not_whole(StreamCompleted(**REFUSED))
    assert "declined" in check_ledger(ledger, None, finish_reason="refusal", unfit=refused)


def test_a_refused_take_of_an_older_passage_is_noted_under_its_own_turn(
    session: StorySession,  # noqa: F811
):
    """Regenerate on an older passage, declined before any text: the note
    went under the story's last message, three exchanges on."""
    for _ in range(3):
        list(session.send(turn()))
    path = session.path()
    first_turn, first_passage, last = path[0], path[1], path[-1]
    session.provider = Ending([""], REFUSED)
    list(session.regenerate(first_passage.id))
    assert any(note.startswith(DECLINED) for note in first_turn.meta.notes)
    assert not any(note.startswith(DECLINED) for note in last.meta.notes)
