"""Continue: more of the latest passage, written into the same message."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.continuation import (
    SECTION_CONTINUE,
    SECTION_SO_FAR,
    continuable,
    ending_of,
    joiner,
    repeat_length,
)
from sealedlore.engine.prompt import SECTION_REMINDER, TurnRequest
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS
from sealedlore.engine.session import CUT_OFF, SessionNotice, StorySession, TurnShape
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.models.character import Character
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.node import Node
from sealedlore.models.story import Story
from sealedlore.providers.base import ProviderError, TextDelta
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import (
    StoryBundle,
    load_story_bundle,
    read_api_log,
    save_story_bundle,
)
from tests.test_private import build as build_private
from tests.test_private import enter as enter_private

CUT = "Serrik leaned on the door and it gave, and beyond it the"
MORE = "stairs went down into the dark."


def make_config(scene_reads: str = "manual") -> Config:
    return Config(
        providers=[ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1")],
        active_provider_name="nano",
        min_cacheable_tokens=1,
        scene_reads=scene_reads,
        scene_model="small/fast-model",
        story_ledger=False,
    )


def make_session(
    tmp_path: Path, story: Story, cast, replies: list[str], *, scene_reads: str = "manual"
) -> StorySession:
    story.defaults.main_model = "anthropic/claude-sonnet-4.6"
    bundle = StoryBundle(story=story, cast=cast)
    save_story_bundle(bundle, root=tmp_path)
    return StorySession(
        bundle,
        make_config(scene_reads),
        MockChatProvider(replies),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def turn(text: str = "I put my shoulder to the door.") -> TurnRequest:
    return TurnRequest(
        speaker_id="char-serrik", user_text=text, controlled_character_id="char-serrik"
    )


@pytest.fixture
def session(tmp_path: Path, story, cast) -> StorySession:
    session = make_session(tmp_path, story, cast, [CUT, MORE])
    list(session.send(turn()))
    return session


# --- the joining rule and what can be carried on --------------------------------


@pytest.mark.parametrize(
    ("before", "start", "between"),
    [
        ("and beyond it the", "stairs went down.", " "),
        ("and beyond it the", " stairs went down.", ""),
        ("She turned.", "Then she ran.", "\n\n"),
        ('"Go," she said.', "He went.", "\n\n"),
        ('"Is that all?"', "He shrugged.", "\n\n"),
        ("She turned", ", then ran.", ""),
        ("She said", "…nothing.", ""),
        ("He waited.\n", "Then left.", ""),
        ("He waited.", "\n\nThen left.", ""),
        ("", "Anything.", ""),
        # Still speaking: the next sentence of the speech, in its paragraph.
        ("She laughed. \"Cellar's still stocked.", "But the dead are there.", " "),
        ("She laughed. “Cellar’s still stocked.", "But the dead are there.", " "),
        ('"Go," she said.\n\nHe stood.', "Then he went.", "\n\n"),
    ],
)
def test_the_new_text_joins_the_old_as_prose_would(before: str, start: str, between: str):
    assert joiner(before, start) == between


def test_only_a_passage_answering_a_turn_can_be_carried_on():
    turn_node = Node(kind="user", speaker_id="char-serrik", content="I wait.")
    passage = Node(kind="assistant", speaker_id="__narrator__", content="Nothing comes.")
    opening = Node(kind="assistant", speaker_id="__narrator__", content="The keep stood empty.")
    summary = passage.model_copy(deep=True)
    summary.meta.private_summary_of = "span-1"
    empty = passage.model_copy(update={"content": "  "})

    assert continuable([turn_node, passage])
    assert not continuable([turn_node])
    assert not continuable([opening])
    assert not continuable([turn_node, summary])
    assert not continuable([turn_node, empty])
    assert not continuable([])


def test_the_request_quotes_where_the_passage_stops():
    assert ending_of("One two three.") == "One two three."
    assert ending_of("a b c d e f g h i j") == "…c d e f g h i j"


def test_a_passage_written_out_again_before_going_on_is_found():
    before = "The eye behind the crack didn't move.\n\nThen the"
    again = " The eye behind the crack didn't move. Then the rifle dipped."
    assert again[repeat_length(before, again) :] == " rifle dipped."
    # Not the passage from its first word, or not all of it: not a repeat.
    assert repeat_length(before, " Then the rifle dipped.") == 0
    assert repeat_length(before, " The eye behind the crack blinked.") == 0
    assert repeat_length("Short one.", " Short one. More.") == 0


# --- the request ------------------------------------------------------------------


def test_the_passage_grows_in_place(session: StorySession):
    before = [node.id for node in session.nodes]
    passage = session.nodes[-1]

    list(session.continue_passage())

    assert [node.id for node in session.nodes] == before
    assert session.story.active_leaf_id == passage.id
    assert passage.content == f"{CUT} {MORE}"
    assert passage.edited_at is not None


def test_the_request_is_the_turns_own_then_the_passage_and_the_ask(session: StorySession):
    first = session.provider.requests[0]

    list(session.continue_passage())

    carried = session.provider.requests[-1]
    # Byte for byte the take's own request first: it rides the same cache.
    assert carried.messages[: len(first.messages)] == first.messages
    so_far, ask = carried.messages[len(first.messages) :]
    assert so_far.role == "assistant" and so_far.text == CUT
    assert ask.role == "user"
    assert DEFAULT_TEXTS.fill("turn.continue", ending=ending_of(CUT)) in ask.text
    assert "“…door and it gave, and beyond it the”" in ask.text
    reminder = session.last_prompt.section(SECTION_REMINDER).text
    assert ask.text.endswith(reminder)
    assert reminder.startswith("REMEMBER")
    names = [section.name for section in session.last_prompt.sections]
    assert names[-2:] == [SECTION_SO_FAR, SECTION_CONTINUE]


def test_the_composers_length_is_for_what_is_added_and_the_turn_keeps_its_own(
    session: StorySession,
):
    user_node = session.nodes[0]

    list(session.continue_passage(TurnShape(response_style="brief")))

    ask = session.provider.requests[-1].messages[-1].text
    assert DEFAULT_TEXTS["length.brief.short"] in ask
    assert user_node.meta.response_style is None


def test_the_cost_is_added_and_the_cut_off_note_goes(session: StorySession):
    passage = session.nodes[-1]
    passage.meta.notes = [f"{CUT_OFF} (4,000 tokens).", "Something else went wrong."]
    first = passage.meta.usage.model_copy()

    list(session.continue_passage())

    assert passage.meta.usage.prompt_tokens == 2 * first.prompt_tokens
    assert passage.meta.usage.completion_tokens == 2 * first.completion_tokens
    assert passage.meta.notes == ["Something else went wrong."]


def test_it_is_logged_as_the_storys_own_call(session: StorySession, tmp_path: Path):
    passage = session.nodes[-1]

    list(session.continue_passage())

    log = read_api_log(session.story.id, root=tmp_path)
    assert [entry["kind"] for entry in log] == ["request", "response"] * 2
    assert log[2]["continues"] == passage.id
    assert log[3]["continues"] == passage.id
    assert log[3]["request_log_ref"] == log[2]["id"]
    assert log[3]["text"] == f" {MORE}"


def test_it_survives_a_reload(session: StorySession, tmp_path: Path):
    list(session.continue_passage())

    reloaded = load_story_bundle(session.story.id, root=tmp_path)
    assert reloaded.nodes[-1].content == f"{CUT} {MORE}"


def test_a_repeat_of_the_passage_is_left_out_and_said(tmp_path: Path, story, cast):
    session = make_session(tmp_path, story, cast, [CUT, f"{CUT} {MORE}"])
    list(session.send(turn()))

    events = list(session.continue_passage())

    assert session.nodes[-1].content == f"{CUT} {MORE}"
    notices = [event.text for event in events if isinstance(event, SessionNotice)]
    assert any("writing the passage out again" in text for text in notices)
    # The log keeps the reply as it came.
    log = read_api_log(session.story.id, root=tmp_path)
    assert log[-1]["text"] == f" {CUT} {MORE}"


def test_a_continuation_that_only_repeats_changes_nothing(tmp_path: Path, story, cast):
    session = make_session(tmp_path, story, cast, [CUT, CUT])
    list(session.send(turn()))

    events = list(session.continue_passage())

    assert session.nodes[-1].content == CUT
    notices = [event.text for event in events if isinstance(event, SessionNotice)]
    assert any("only wrote the passage out again" in text for text in notices)


def test_the_window_sees_the_joiner_with_the_first_text(session: StorySession):
    events = [event for event in session.continue_passage() if isinstance(event, TextDelta)]

    assert "".join(event.text for event in events) == f" {MORE}"


def test_the_reasoning_is_kept_after_the_first(tmp_path: Path, story, cast):
    session = make_session(tmp_path, story, cast, [CUT, MORE])
    session.provider.reasoning = "First thoughts."
    list(session.send(turn()))
    session.provider.reasoning = "More thoughts."

    list(session.continue_passage())

    assert session.nodes[-1].meta.reasoning == "First thoughts.\n\nMore thoughts."


# --- what can't be carried on, and when nothing comes --------------------------------


def test_an_author_turn_with_no_passage_has_nothing_to_carry_on(tmp_path: Path, story, cast):
    session = make_session(tmp_path, story, cast, [CUT])
    session.provider = MockChatProvider(error=ProviderError("down"))
    with pytest.raises(ProviderError):
        list(session.send(turn()))

    with pytest.raises(ValueError, match="Continue carries on"):
        list(session.continue_passage())


def test_a_failed_continuation_leaves_the_passage_as_it_was(session: StorySession):
    passage = session.nodes[-1]
    session.provider = MockChatProvider(error=ProviderError("upstream down"))

    with pytest.raises(ProviderError):
        list(session.continue_passage())

    assert passage.content == CUT
    assert len(session.nodes) == 2


def test_a_stopped_continuation_keeps_what_came(session: StorySession):
    session.provider = MockChatProvider([MORE], chunk_size=6)
    events = session.continue_passage()
    while not isinstance(next(events), TextDelta):
        pass
    events.close()

    assert session.nodes[-1].content == f"{CUT} stairs"
    assert session.last_result.stopped_early


# --- the reads run over the whole passage -----------------------------------------


def test_the_scene_is_read_again_over_the_whole_passage(tmp_path: Path, story, cast):
    story.scene.tracked = True
    session = make_session(
        tmp_path,
        story,
        cast,
        [
            "Maela climbed to the yard.",
            json.dumps(
                {
                    "location": "The yard",
                    "scene_closed": {"gist": "They left the undercroft."},
                }
            ),
        ],
        scene_reads="every_turn",
    )
    list(session.send(turn()))
    passage = session.nodes[-1]
    assert len(session.story.scene_log) == 1
    session.provider = MockChatProvider(
        [
            "Captain Idris was waiting there.",
            json.dumps(
                {
                    "location": "The yard",
                    "arrived": [{"name": "Captain Idris", "how": "was waiting in the yard"}],
                    "scene_closed": {"gist": "They left the undercroft."},
                }
            ),
        ]
    )

    list(session.continue_passage())

    read = session.provider.requests[-1]
    assert read.model == "small/fast-model"
    passage_text = "Maela climbed to the yard.\n\nCaptain Idris was waiting there."
    assert passage_text in read.messages[-1].text
    # From the scene the turn found, not the one the first read left.
    assert "The undercroft beneath Calder Keep" in read.messages[-1].text
    assert "char-idris" in passage.meta.scene.present_character_ids
    assert session.story.scene.location == "The yard"
    assert len(session.story.scene_log) == 1


def test_a_stopped_continuation_is_not_read(tmp_path: Path, story, cast):
    session = make_session(tmp_path, story, cast, [CUT, json.dumps({})], scene_reads="every_turn")
    list(session.send(turn()))
    session.provider = MockChatProvider([MORE, json.dumps({})], chunk_size=6)
    events = session.continue_passage()
    while not isinstance(next(events), TextDelta):
        pass
    events.close()

    assert len(session.provider.requests) == 1


# --- a chat and a private scene ---------------------------------------------------


def test_a_chat_reply_is_carried_on_with_the_chats_own_words(tmp_path: Path):
    story = Story(title="Tutor", mode="chat", chat_prompt="You are a patient maths tutor.")
    story.defaults.main_model = "anthropic/claude-sonnet-4.6"
    bundle = StoryBundle(story=story, cast=[Character(id="c1", name="Nils Carrow")])
    save_story_bundle(bundle, root=tmp_path)
    session = StorySession(
        bundle,
        make_config(),
        MockChatProvider(["Divide both sides by", "two."]),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )
    list(session.send(TurnRequest(speaker_id="__user__", user_text="Solve 2x = 4.")))

    list(session.continue_passage())

    assert session.nodes[-1].content == "Divide both sides by two."
    ask = session.provider.requests[-1].messages[-1].text
    assert ask == DEFAULT_TEXTS.fill("chat.continue", ending="Divide both sides by")
    assert "REMEMBER" not in ask


def test_in_a_private_scene_it_goes_to_the_private_model_only(tmp_path: Path):
    session, main, held = build_private(tmp_path)
    private = enter_private(session)
    for _event in session.send(
        TurnRequest(speaker_id=held, user_text="I whisper to her.", controlled_character_id=held)
    ):
        pass
    sent_to_main = len(main.requests)
    private.responses = ["and she whispered back."]

    for _event in session.continue_passage():
        pass

    assert len(main.requests) == sent_to_main
    assert private.requests[-1].messages[-1].role == "user"
    assert session.full_path()[-1].content.endswith("and she whispered back.")
    assert session.full_path()[-1].meta.private_span == session.open_span.id
    usage = session.full_path()[-1].meta.usage
    assert usage.completion_tokens == 2 * private.usage.completion_tokens
