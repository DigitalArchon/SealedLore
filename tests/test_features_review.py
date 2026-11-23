"""The review of two features before 3.5.0b1 (Nov 23 2026): one character's
answer asked for again (engine/session_reactions.py `regenerate_reaction`),
and lore from the full entry (engine/lore_whole_back.py, its vectors made
ahead in engine/session_lore_vectors.py). Each test here failed on the code as
it was. The mock provider and mock embeddings: no network."""

from __future__ import annotations

import pytest

from sealedlore.engine.model_tuning import sends_lore_from_full

# --- which models are Claude ------------------------------------------------------

# Every spelling a Claude model is served under: Anthropic's own and NanoGPT's
# bare ids, OpenRouter's maker prefix, Bedrock's region and "anthropic." before
# the id, a router before that.
CLAUDE_SPELLINGS = [
    "claude-sonnet-4-6",
    "Anthropic::claude-opus-5-5",
    "anthropic/claude-sonnet-4.6:thinking",
    "openrouter/anthropic/claude-sonnet-4.6",
    "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "eu.anthropic.claude-3-7-sonnet-20250219-v1:0",
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "anthropic.claude-3-5-sonnet-20240620-v1:0",
    "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0",
    "Bedrock::us.anthropic.claude-opus-4-1-20250805-v1:0",
]
# Named for Claude, and not it: a model of someone else's, or one made from
# another model, on a server of one's own.
NOT_CLAUDE = [
    "Claude-Opus-Distill-Qwen3",
    "Local::Claude-Opus-Distill-Qwen3",
    "LM Studio::someone/Claude-Opus-Distill-Qwen3-GGUF",
    "someone/claude-sonnet-4-6",
    "us.meta.llama3-3-70b-instruct-v1:0",
    "bedrock/meta.claude-like-70b",
    "claude-3.7-sonnet-reasoning-gemma3-12b",
]


@pytest.mark.parametrize("model", CLAUDE_SPELLINGS)
def test_claude_is_known_by_every_spelling_it_is_served_under(model: str) -> None:
    assert not sends_lore_from_full(model)


@pytest.mark.parametrize("model", NOT_CLAUDE)
def test_a_model_only_named_for_claude_is_not_claude(model: str) -> None:
    assert sends_lore_from_full(model)


# --- the character the author plays now ----------------------------------------------


def as_maela(text: str = "I look at the lock."):
    from sealedlore.engine.prompt import TurnRequest

    return TurnRequest(
        speaker_id="char-maela", user_text=text, controlled_character_id="char-maela"
    )


def storyteller_requests(session):
    return [r for r in session.provider.requests if r.model == session.story.defaults.main_model]


def test_the_answer_of_the_character_the_author_now_plays_isnt_asked_for_again(
    tmp_path,
) -> None:
    from tests.test_reactions import make_session, play, turn

    session = make_session(tmp_path, ["The door gives.", "MAELA-FIRST"])
    play(session, turn())
    passage = session.path()[-1]
    assert session.reaction_refusal(passage.id, "char-maela") is None
    session.hold("char-maela")  # the author takes Maela up
    refusal = session.reaction_refusal(passage.id, "char-maela")
    assert refusal is not None and "yours to play now" in refusal
    before = len(session.provider.requests)
    with pytest.raises(ValueError, match="yours to play now"):
        list(session.regenerate_reaction(passage.id, "char-maela"))
    assert len(session.provider.requests) == before


def test_a_blind_answer_by_whom_the_author_has_taken_up_isnt_sent_as_theirs(tmp_path) -> None:
    """Together: Maela answered the passage blind; the author then plays her,
    and their turn is what she does. Her model's answer stays under the
    passage, and isn't sent to the storyteller as what she means to do."""
    from tests.test_reactions import make_session, play, sent, turn

    session = make_session(tmp_path, ["The door gives.", "MAELA-PICKS-THE-LOCK", "They go on."])
    play(session, turn())
    passage = session.path()[-1]
    assert [a.text for a in passage.meta.reactions] == ["MAELA-PICKS-THE-LOCK"]
    session.hold("char-maela")
    play(session, as_maela())
    request = storyteller_requests(session)[-1]
    assert "I look at the lock." in sent(request)
    assert "MAELA-PICKS-THE-LOCK" not in sent(request)
    assert passage.meta.reactions[0].text == "MAELA-PICKS-THE-LOCK"  # kept, unsent


def test_after_the_turn_the_one_who_took_it_never_answers_it(tmp_path) -> None:
    from tests.test_reactions import MAELA_MODEL, a_story, make_session, play

    story = a_story()
    story.defaults.round_order = "after"
    session = make_session(tmp_path, ["The door gives."], story=story)
    session.hold("char-maela")
    play(session, as_maela())
    turn_node = session.path()[-2]
    assert turn_node.meta.round_after and turn_node.meta.reactions == []
    assert [r.model for r in session.provider.requests if r.model == MAELA_MODEL] == []


def test_the_others_answers_still_go_with_the_turn(tmp_path) -> None:
    """Only the answer of the character the turn is played as is left out."""
    from tests.test_answer_regenerate import two_answer
    from tests.test_reactions import play, sent, turn

    session, provider = two_answer(
        tmp_path, ["MAELA-WAITS"], ["IDRIS-DRAWS"], replies=["The door gives.", "On."]
    )
    play(session, turn())
    session.hold("char-maela")
    play(session, as_maela())
    request = storyteller_requests(session)[-1]
    text = sent(request)
    assert "IDRIS-DRAWS" in text and "MAELA-WAITS" not in text


# --- an older answer asked for again, with what she remembered then ----------------


def test_an_older_answer_is_asked_for_with_only_the_memories_the_round_then_had(
    tmp_path, monkeypatch
) -> None:
    """Memories written at later rounds cover stretches the round at that
    passage still sent word for word: asked again, the request is the round's
    there, field for field, not one that remembers what it then saw."""
    from sealedlore.engine import reactions as reactions_module
    from tests.test_answer_regenerate import asked, two_answer
    from tests.test_reactions import MAELA_MODEL, play, turn

    monkeypatch.setattr(reactions_module, "HISTORY_TOKENS", 60)
    monkeypatch.setattr(reactions_module, "RECOLLECT_MESSAGES", 2)
    replies = [f"PASSAGE-{i} " + "word " * 20 for i in range(6)]
    answers = [f"ANSWER-{i}" for i in range(6)] + ["ANSWER-AGAIN"]
    session, provider = two_answer(tmp_path, answers, replies=replies)
    passages = []
    for i in range(6):
        provider.responses = [replies[i], f"I REMEMBER {i}."]  # the passage, then a memory
        play(session, turn(f"Turn {i}."))
        passages.append(session.path()[-1])
    assert len(session.bundle.memories) >= 3  # later rounds wrote more of them
    rounds = asked(provider, MAELA_MODEL)
    for target in (2, 3):
        before = len(provider.requests)
        list(session.regenerate_reaction(passages[target].id, "char-maela"))
        [again] = provider.requests[before:]  # hers alone: nothing to remember first
        assert again == rounds[target]
    # The latest is asked as the round asked it, every memory with it.
    before = len(provider.requests)
    list(session.regenerate_reaction(passages[-1].id, "char-maela"))
    [again] = provider.requests[before:]
    assert again == rounds[-1]


# --- asked again after the passage was edited ------------------------------------------


def replies(session, *texts: str) -> None:
    """The mock's next replies, in order, from the first (it takes them by
    its count of calls)."""
    session.provider.responses = list(texts)
    session.provider._call_count = 0


def test_asked_again_after_an_edit_she_is_given_only_what_she_was_there_for(tmp_path) -> None:
    """Edited since the round, a passage that moves counts whole until its
    views are read again: the round reads them first, and so does asking one
    of them again, as a paid call said in the status bar."""
    from sealedlore.engine.reactions import apply_views
    from sealedlore.engine.session import SessionNotice
    from tests.test_passage_views import ASKS, BOTH, MOVING, TOWER
    from tests.test_reactions import MAELA_MODEL, make_session, play, sent, turn

    session = make_session(tmp_path, ["The door gives.", "Hm."])
    session.story.setup.starting_scene = BOTH
    play(session, turn())
    node = session.path()[-1]
    node.content = MOVING
    node.meta.scene = TOWER
    assert apply_views('{"Maela Orr": [1]}', node, [session.cast[1]])
    # Read as the round left it: nothing more is asked.
    replies(session, "AGAIN-1")
    before = len(session.provider.requests)
    list(session.regenerate_reaction(node.id, "char-maela"))
    [first] = session.provider.requests[before:]
    assert first.model == MAELA_MODEL and "TOWER-SECRET" not in sent(first)

    session.edit_node(node.id, MOVING.replace("argue over", "argue about"))
    replies(session, '{"Maela Orr": [1]}', "AGAIN-2")
    before = len(session.provider.requests)
    events = list(session.regenerate_reaction(node.id, "char-maela"))
    views, again = session.provider.requests[before:]
    assert ASKS in sent(views) and again.model == MAELA_MODEL
    assert "TOWER-SECRET" not in sent(again) and "argue about the map" in sent(again)
    assert any(
        isinstance(e, SessionNotice) and e.text.startswith("Working out who saw what")
        for e in events
    )
    assert [a.text for a in node.meta.reactions] == ["AGAIN-2"]


def test_asked_again_after_an_edit_the_record_reads_the_passage_again(tmp_path) -> None:
    from sealedlore.engine.session import SessionNotice
    from tests.test_public_record import character_calls, record_calls
    from tests.test_reactions import make_session, play, sent, turn

    session = make_session(
        tmp_path,
        [
            "The door gives. Across the town the bells ring noon.",
            '{"facts": ["The town bells rang at noon."]}',
            "MAELA-ONE",
        ],
        record=True,
    )
    play(session, turn())
    passage = session.path()[-1]
    assert len(record_calls(session)) == 1
    # Asked again with nothing changed: the record as the round left it.
    replies(session, "MAELA-TWO")
    list(session.regenerate_reaction(passage.id, "char-maela"))
    assert len(record_calls(session)) == 1

    session.edit_node(passage.id, "The door gives. Across the town the bells ring midnight.")
    replies(session, '{"facts": ["The town bells rang at midnight."]}', "MAELA-THREE")
    events = list(session.regenerate_reaction(passage.id, "char-maela"))
    keeper = record_calls(session)[-1]
    assert len(record_calls(session)) == 2 and "ring midnight" in sent(keeper)
    assert "The town bells rang at midnight." in sent(character_calls(session)[-1])
    assert passage.meta.public_record == ["The town bells rang at midnight."]
    assert any(
        isinstance(e, SessionNotice) and e.text.startswith("Keeping the public record")
        for e in events
    )
