"""Continue carried on more than once: the passage goes in pieces, all but
the newest read from the cache, and the new text is a paragraph of its own
after a reply that came back whole."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from sealedlore.engine.continuation import (
    BREAKPOINT_SO_FAR,
    SECTION_SO_FAR,
    carried_start,
    continued,
    pieces,
    text_digest,
)
from sealedlore.engine.prompt import MAX_CACHE_BREAKPOINTS, CacheBreakpoint, TurnRequest
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.messages import ContentPart, PromptMessage
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.story import Story
from sealedlore.providers.anthropic import render_messages
from sealedlore.providers.mock import MockChatProvider
from sealedlore.providers.wire import to_wire_messages
from sealedlore.storage.repository import StoryBundle, save_story_bundle

LONG = "The rain came down over the pass and the lanterns swung. " * 30


def chat(tmp_path: Path, replies: list[str], *, min_tokens: int = 1) -> StorySession:
    story = Story(title="Inn", mode="chat", chat_keep="memory", chat_prompt="You are Mara. " * 50)
    story.defaults.main_model = "anthropic/claude-sonnet-4.6"
    bundle = StoryBundle(story=story)
    save_story_bundle(bundle, root=tmp_path)
    return StorySession(
        bundle,
        Config(
            providers=[ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1")],
            active_provider_name="nano",
            min_cacheable_tokens=min_tokens,
        ),
        MockChatProvider(replies),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def say(session: StorySession, text: str) -> None:
    list(session.send(TurnRequest(speaker_id="__user__", user_text=text)))


# --- the new text's start -----------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "start", "whole", "shown"),
    [
        # The live case: a chat's reply ending on an action ran on after it.
        ("Hello there. *she smiles*", "More.", True, "\n\nMore."),
        ("Hello there. *she smiles*", "More.", None, " More."),
        ("Enjoy your stay 🙂", "Also,", True, "\n\nAlso,"),
        ("Here are the rooms:", "1. The attic", True, "\n\n1. The attic"),
        ("- the attic\n- the cellar", "Both are cold.", True, "\n\nBoth are cold."),
        # Whatever space the model starts with, one paragraph break.
        ("She turned.", "\nThen she ran.", True, "\n\nThen she ran."),
        ("She turned.", " Then she ran.", True, "\n\nThen she ran."),
        # Visibly mid-sentence, though the model said it was done.
        ("and beyond it the", "stairs went down.", True, " stairs went down."),
        ("She turned,", "then ran.", True, " then ran."),
        # Leaning on the word before: no break.
        ("She said.", "…nothing.", True, "…nothing."),
        # Stopped or cut off: the old rule.
        ("She turned", " and ran.", False, " and ran."),
    ],
)
def test_after_a_whole_reply_the_new_text_is_a_paragraph_of_its_own(before, start, whole, shown):
    assert carried_start(before, start, whole) == shown


def test_a_whole_reply_is_carried_on_in_a_new_paragraph(tmp_path: Path):
    session = chat(tmp_path, ["Welcome in. *she smiles*", "\nThe stew is hot."])
    say(session, "Evening.")
    assert session.full_path()[-1].meta.finished is True

    list(session.continue_passage())

    assert session.full_path()[-1].content == "Welcome in. *she smiles*\n\nThe stew is hot."


def test_a_reply_cut_off_at_the_limit_is_carried_on_where_it_stopped(tmp_path: Path):
    session = chat(tmp_path, ["The road climbs. *she smiles*", "and points north."])
    session.provider.finish_reason = "length"
    say(session, "Which way?")
    assert session.full_path()[-1].meta.finished is False

    list(session.continue_passage())

    assert session.full_path()[-1].content == "The road climbs. *she smiles* and points north."


# --- the pieces -----------------------------------------------------------------------


def test_pieces_are_the_text_cut_where_each_continuation_began():
    text = "One. Two. Three."
    assert pieces(text, [4, 9], text_digest(text)) == ["One.", " Two.", " Three."]
    assert "".join(pieces(text, [4, 9], text_digest(text))) == text
    # Edited since: the offsets were taken in another text.
    assert pieces(text, [4, 9], text_digest("One. Two.")) == [text]
    assert pieces(text, [], None) == [text]
    assert pieces(text, [9, 4], text_digest(text)) == [text]
    assert pieces(text, [4, 99], text_digest(text)) == [text]


def test_glued_parts_go_with_nothing_between_on_every_wire():
    message = PromptMessage(
        role="assistant",
        parts=(
            ContentPart(text="One."),
            ContentPart(text=" Two.", glued=True, cache_breakpoint=True),
        ),
    )
    assert message.text == "One. Two."
    assert to_wire_messages([message], use_cache_control=False)[0]["content"] == "One. Two."
    blocks = to_wire_messages([message], use_cache_control=True)[0]["content"]
    assert [block["text"] for block in blocks] == ["One.", " Two."]
    assert "cache_control" in blocks[1]
    _system, wire = render_messages(
        [PromptMessage(role="user", parts=(ContentPart(text="Hi"),)), message],
        use_cache_control=True,
    )
    assert [block["text"] for block in wire[1]["content"]] == ["One.", " Two."]
    # A part that isn't glued still leads with its blank line.
    apart = PromptMessage(role="user", parts=(ContentPart(text="a"), ContentPart(text="b")))
    assert apart.text == "a\n\nb"


def test_each_continue_reads_the_last_ones_passage_from_the_cache(tmp_path: Path):
    session = chat(tmp_path, [LONG, " More rain.", " Then thunder.", " Then quiet."])
    say(session, "What's the weather?")
    for _ in range(3):
        list(session.continue_passage())

    requests = session.provider.requests[1:]
    so_far = [request.messages[-2] for request in requests]
    assert [len(message.parts) for message in so_far] == [1, 2, 3]
    for message in so_far:
        # Glued: the passage reads as it is, not split by blank lines.
        assert all(part.glued for part in message.parts[1:])
        assert [part.cache_breakpoint for part in message.parts][-1]
        assert not any(part.cache_breakpoint for part in message.parts[:-1])
    # What the last Continue sent whole is the next one's pieces but the newest:
    # the cached prefix holds byte for byte.
    for first, second in zip(so_far, so_far[1:], strict=False):
        assert [part.text for part in second.parts[: len(first.parts)]] == [
            part.text for part in first.parts
        ]
    assert so_far[-1].text == LONG.rstrip() + "\n\nMore rain.\n\nThen thunder."
    prompt = session.last_prompt
    assert prompt.breakpoints[-1].label == BREAKPOINT_SO_FAR
    assert prompt.breakpoints[-1].section_name == SECTION_SO_FAR


def test_an_edited_passage_goes_whole_again(tmp_path: Path):
    session = chat(tmp_path, [LONG, " More rain.", " Then thunder."])
    say(session, "What's the weather?")
    list(session.continue_passage())
    passage = session.full_path()[-1]
    session.edit_node(passage.id, passage.content.replace("rain", "snow"))

    list(session.continue_passage())

    assert len(session.provider.requests[-1].messages[-2].parts) == 1


def test_no_cache_point_past_the_limit_or_for_too_short_a_stretch(tmp_path: Path):
    session = chat(tmp_path, [LONG, " More."], min_tokens=10**6)
    say(session, "What's the weather?")
    list(session.continue_passage())
    assert not any(
        part.cache_breakpoint for part in session.provider.requests[-1].messages[-2].parts
    )

    turn = session.last_prompt
    taken = CacheBreakpoint("history", 1, 0, 10, 10)
    full = replace(turn, breakpoints=(taken,) * MAX_CACHE_BREAKPOINTS)
    carried = continued(full, ["A.", " B."], "Go on.", cache_from=0)
    assert not any(part.cache_breakpoint for part in carried.messages[-2].parts)
